"""Offline CHG-121 O2 durable command transaction evidence."""

from pathlib import Path
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    AuthorizationDeniedError,
    CommandContext,
    IdempotencyConflictError,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RevisionVector,
    StorageFailureError,
    ValidationFailureError,
    VersionConflictError,
    VersionedAggregateCommandExecutor,
    canonical_command_payload_hash,
    normalize_domain_payload,
)
from ephi.infrastructure import SQLiteReferenceTransactionAdapter  # noqa: E402


class O2TransactionTests(unittest.TestCase):
    capability = "o2.fixture.write"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ephi-o2.sqlite3"
        self.scope = AccessScope("scope-1", site_id="site-1", area_id="area-1", family_id="family-1")
        self.principal = Principal(
            subject="subject-1",
            capabilities=(self.capability,),
            scope_grants=(self.scope,),
            auth_session_revision=7,
            security_revision=11,
        )
        self.store = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(self.store.close)
        self.store.seed_aggregate(self.scope, "fixture", "aggregate-1", {"effect_count": 0, "seed": "canonical"})
        self.current_authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.executor = VersionedAggregateCommandExecutor(self.store, self.current_authorization)

    def context(self, command_id="command-1", *, expected=0, principal=None, reason=None, viewed=True):
        revisions = RevisionVector("analysis-1", "exposure-1", "priority-1", expected if expected is not None else 0, None, "manifest-1") if viewed else None
        return CommandContext(
            command_id,
            principal or self.principal,
            self.scope,
            expected,
            revisions,
            reason,
        )

    def execute(self, context=None, *, payload=None, command_type="FixtureCommand", effect=None):
        return self.executor.execute(
            context or self.context(),
            command_type=command_type,
            aggregate_type="fixture",
            aggregate_id="aggregate-1",
            payload=payload or {"value": 1},
            required_capability=self.capability,
            effect=effect,
        )

    def test_contracts_and_hashing_are_typed_and_transport_independent(self):
        with self.assertRaises(TypeError):
            AccessScope("")
        with self.assertRaises(TypeError):
            Principal("subject-1", (self.capability,), ("not-a-scope",), 1)
        with self.assertRaises(TypeError):
            RevisionVector("analysis", None, None, True, None, "manifest")
        with self.assertRaises(TypeError):
            CommandContext("command", self.principal, "not-a-scope", 0)
        with self.assertRaises(ValueError):
            self.context(expected=None).require_existing_aggregate_version()

        first = canonical_command_payload_hash(
            "FixtureCommand", self.scope, 0, None, {"b": 2, "a": 1}, target={"aggregate_type": "fixture", "aggregate_id": "aggregate-1"}
        )
        second = canonical_command_payload_hash(
            "FixtureCommand", self.scope, 0, None, {"a": 1, "b": 2}, target={"aggregate_type": "fixture", "aggregate_id": "aggregate-1"}
        )
        self.assertEqual(first, second)
        self.assertNotEqual(
            first,
            canonical_command_payload_hash(
                "FixtureCommand", self.scope, 1, None, {"a": 1, "b": 2}, target={"aggregate_type": "fixture", "aggregate_id": "aggregate-1"}
            ),
        )
        with self.assertRaises(ValidationFailureError):
            normalize_domain_payload({"value": object()})
        with self.assertRaises(ValidationFailureError):
            normalize_domain_payload({"access_token": "secret"})

    def test_file_backed_adapter_rejects_memory_fallback(self):
        for path in (":memory:", "file::memory:?cache=shared", "file:demo?mode=memory"):
            with self.subTest(path=path):
                with self.assertRaises(ValidationFailureError):
                    SQLiteReferenceTransactionAdapter(path)

    def test_commit_close_reopen_and_exact_replay_are_durable_and_idempotent(self):
        committed = self.execute()
        counts = self.store.count_rows()
        self.store.close()

        reopened = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(reopened.close)
        replay = VersionedAggregateCommandExecutor(reopened, self.current_authorization).execute(
            self.context(),
            command_type="FixtureCommand",
            aggregate_type="fixture",
            aggregate_id="aggregate-1",
            payload={"value": 1},
            required_capability=self.capability,
        )

        self.assertEqual(replay, committed)
        self.assertEqual(reopened.get_aggregate(self.scope, "fixture", "aggregate-1").version, 1)
        self.assertEqual(reopened.get_aggregate(self.scope, "fixture", "aggregate-1").state["effect_count"], 1)
        self.assertEqual(reopened.count_rows(), counts)
        self.assertEqual(len(reopened.list_audit_events()), 1)
        self.assertEqual(len(reopened.list_outbox_events()), 1)

    def test_same_id_different_normalized_payload_is_conflict_without_mutation(self):
        self.execute(payload={"value": 1})
        before = self.store.count_rows()
        with self.assertRaises(IdempotencyConflictError) as raised:
            self.execute(payload={"value": 2})
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.store.count_rows(), before)
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1").state["effect_count"], 1)

    def test_stale_expected_version_rolls_back_everything(self):
        self.execute()
        before_state = self.store.get_aggregate(self.scope, "fixture", "aggregate-1")
        before_counts = self.store.count_rows()
        with self.assertRaises(VersionConflictError) as raised:
            self.execute(context=self.context("stale-command", expected=0), payload={"value": "stale"})
        self.assertEqual(raised.exception.code, "VERSION_CONFLICT")
        after_state = self.store.get_aggregate(self.scope, "fixture", "aggregate-1")
        self.assertEqual(after_state, before_state)
        self.assertEqual(self.store.count_rows(), before_counts)

    def test_receipt_replay_uses_current_authorization_not_historical_revisions(self):
        committed = self.execute()
        before = self.store.count_rows()
        reconnected = Principal("subject-1", (self.capability,), (self.scope,), 8, 12)
        self.current_authorization.set_principal(reconnected)
        replay = self.execute(context=self.context(principal=reconnected))
        self.assertEqual(replay, committed)
        self.assertEqual(self.store.count_rows(), before)
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1").state["effect_count"], 1)
        receipt = self.store.get_command_receipt(self.scope.canonical_key, "subject-1", "command-1")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.auth_session_revision_json, "7")
        self.assertEqual(receipt.security_revision_json, "11")

        revoked = Principal("subject-1", (), (self.scope,), 8, 12)
        self.current_authorization.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError) as raised:
            self.execute(context=self.context(principal=revoked))
        self.assertEqual(raised.exception.code, "FORBIDDEN_ACTION")
        out_of_scope = Principal("subject-1", (self.capability,), (AccessScope("different"),), 8, 12)
        self.current_authorization.set_principal(out_of_scope)
        with self.assertRaises(AuthorizationDeniedError):
            self.execute(context=self.context(principal=out_of_scope))
        self.assertEqual(self.store.count_rows(), before)

    def test_receipt_and_audit_subject_use_server_principal_not_domain_actor(self):
        self.execute(payload={"actor": "attacker-supplied", "value": 1})
        receipt = self.store.get_command_receipt(self.scope.canonical_key, "subject-1", "command-1")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.subject, self.principal.subject)
        self.assertEqual(self.store.list_audit_events()[0]["subject"], self.principal.subject)
        self.assertIsNone(self.store.get_command_receipt(self.scope.canonical_key, "attacker-supplied", "command-1"))

    def test_concurrent_same_command_first_attempts_have_one_logical_effect(self):
        first = SQLiteReferenceTransactionAdapter(self.path)
        second = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def attempt(store):
            try:
                barrier.wait(timeout=5)
                result = VersionedAggregateCommandExecutor(store, self.current_authorization).execute(
                    self.context("concurrent-command"),
                    command_type="FixtureCommand",
                    aggregate_type="fixture",
                    aggregate_id="aggregate-1",
                    payload={"value": 1},
                    required_capability=self.capability,
                )
                results.append(result)
            except Exception as exc:  # pragma: no cover - assertion reports any race failure
                failures.append(exc)

        threads = [threading.Thread(target=attempt, args=(store,)) for store in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(failures, failures)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.store.count_rows(), {"aggregate_state": 1, "command_receipt": 1, "audit_event": 1, "outbox_event": 1})
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1").version, 1)

    def test_audit_fault_rolls_back_aggregate_receipt_and_outbox(self):
        self.store.connection.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_event BEGIN SELECT RAISE(ABORT, 'test audit fault'); END"
        )
        with self.assertRaises(StorageFailureError):
            self.execute()
        self.assertEqual(self.store.count_rows(), {"aggregate_state": 1, "command_receipt": 0, "audit_event": 0, "outbox_event": 0})
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1").version, 0)

    def test_outbox_fault_rolls_back_aggregate_receipt_and_audit(self):
        self.store.connection.execute(
            "CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox_event BEGIN SELECT RAISE(ABORT, 'test outbox fault'); END"
        )
        with self.assertRaises(StorageFailureError):
            self.execute()
        self.assertEqual(self.store.count_rows(), {"aggregate_state": 1, "command_receipt": 0, "audit_event": 0, "outbox_event": 0})
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1").version, 0)

    def test_restart_persistence_is_queryable_without_replay(self):
        self.execute()
        self.store.close()
        reopened = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.count_rows(), {"aggregate_state": 1, "command_receipt": 1, "audit_event": 1, "outbox_event": 1})
        receipt = reopened.get_command_receipt(self.scope.canonical_key, "subject-1", "command-1")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.status, "COMMITTED")
        self.assertEqual(reopened.list_audit_events()[0]["command_id"], "command-1")
        self.assertEqual(reopened.list_outbox_events()[0]["command_id"], "command-1")


if __name__ == "__main__":
    unittest.main()
