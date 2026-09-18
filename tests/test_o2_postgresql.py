"""CHG-123 PostgreSQL reference/integration evidence for the O2 command core."""

from pathlib import Path
from contextlib import contextmanager
import os
import re
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
    Principal,
    RevisionVector,
    ScopeDeniedError,
    StorageFailureError,
    ValidationFailureError,
    VersionConflictError,
    VersionedAggregateCommandExecutor,
    canonical_command_payload_hash,
)
from ephi.infrastructure import (  # noqa: E402
    PostgreSQLReferenceTransactionAdapter,
    SQLiteReferenceTransactionAdapter,
)


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


class _ReceiptRaceBarrierUnitOfWork:
    def __init__(self, transaction, barrier):
        self._transaction = transaction
        self._barrier = barrier

    def __getattr__(self, name):
        return getattr(self._transaction, name)

    def get_aggregate(self, *args, **kwargs):
        self._barrier.wait(timeout=10)
        return self._transaction.get_aggregate(*args, **kwargs)


class _ReceiptRaceBarrierStore:
    def __init__(self, store, barrier):
        self._store = store
        self._barrier = barrier

    @contextmanager
    def command_transaction(self):
        with self._store.command_transaction() as transaction:
            yield _ReceiptRaceBarrierUnitOfWork(transaction, self._barrier)


class PostgreSQLDsnContractTests(unittest.TestCase):
    def test_postgresql_requires_an_explicit_dsn(self):
        with self.assertRaises(ValidationFailureError):
            PostgreSQLReferenceTransactionAdapter("")


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLTransactionTests(unittest.TestCase):
    capability = "o2.fixture.write"

    def setUp(self):
        self.store = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.store.close)
        self.store.connection.execute("TRUNCATE outbox_event, audit_event, command_receipt, aggregate_state")
        self.scope = AccessScope("pg-scope-1", site_id="site-1", area_id="area-1", family_id="family-1")
        self.principal = Principal(
            subject="subject-1",
            capabilities=(self.capability,),
            scope_grants=(self.scope,),
            auth_session_revision=7,
            security_revision=11,
        )
        self.store.seed_aggregate(self.scope, "fixture", "aggregate-1", {"effect_count": 0, "seed": "canonical"})

    def context(self, command_id="command-1", *, expected=0, principal=None, reason=None, viewed=True):
        revisions = RevisionVector(
            "analysis-1", "exposure-1", "priority-1", expected if expected is not None else 0, None, "manifest-1"
        ) if viewed else None
        return CommandContext(command_id, principal or self.principal, self.scope, expected, revisions, reason)

    def execute(self, store=None, context=None, *, payload=None, command_type="FixtureCommand", effect=None):
        return VersionedAggregateCommandExecutor(store or self.store).execute(
            context or self.context(),
            command_type=command_type,
            aggregate_type="fixture",
            aggregate_id="aggregate-1",
            payload=payload or {"value": 1},
            required_capability=self.capability,
            effect=effect,
        )

    def test_server_and_narrow_schema_are_postgresql_18_and_migration_is_idempotent(self):
        self.assertRegex(self.store.server_version(), r"^18\.")
        self.store.apply_migrations()
        tables = {
            row["table_name"]
            for row in self.store.connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name IN ('aggregate_state', 'command_receipt', 'audit_event', 'outbox_event')"
            ).fetchall()
        }
        self.assertEqual(tables, {"aggregate_state", "command_receipt", "audit_event", "outbox_event"})

    def test_backend_independent_payload_hash_matches_sqlite_result(self):
        with tempfile.TemporaryDirectory() as directory:
            sqlite = SQLiteReferenceTransactionAdapter(Path(directory) / "hash.sqlite3")
            self.addCleanup(sqlite.close)
            sqlite.seed_aggregate(self.scope, "fixture", "aggregate-1", {"effect_count": 0, "seed": "canonical"})
            sqlite_result = VersionedAggregateCommandExecutor(sqlite).execute(
                self.context(),
                command_type="FixtureCommand",
                aggregate_type="fixture",
                aggregate_id="aggregate-1",
                payload={"value": 1, "ordered": {"b": 2, "a": 1}},
                required_capability=self.capability,
            )
        postgres_result = VersionedAggregateCommandExecutor(self.store).execute(
            self.context(),
            command_type="FixtureCommand",
            aggregate_type="fixture",
            aggregate_id="aggregate-1",
            payload={"ordered": {"a": 1, "b": 2}, "value": 1},
            required_capability=self.capability,
        )
        self.assertEqual(postgres_result.payload_hash, sqlite_result.payload_hash)
        self.assertEqual(
            postgres_result.payload_hash,
            canonical_command_payload_hash(
                "FixtureCommand",
                self.scope,
                0,
                self.context().viewed_revisions,
                {"value": 1, "ordered": {"b": 2, "a": 1}},
                target={"aggregate_type": "fixture", "aggregate_id": "aggregate-1"},
            ),
        )

    def test_exact_replay_after_reconnect_has_no_second_effect_or_event(self):
        committed = self.execute()
        before = self.store.count_rows()
        self.store.close()
        self.store = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.store.close)
        replay = self.execute()
        self.assertEqual(replay, committed)
        self.assertEqual(self.store.count_rows(), before)
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1").state["effect_count"], 1)

    def test_current_authorization_allows_rotated_replay_but_revocation_and_scope_deny(self):
        committed = self.execute()
        before = self.store.count_rows()
        rotated = Principal("subject-1", (self.capability,), (self.scope,), 8, 12)
        self.assertEqual(self.execute(context=self.context(principal=rotated)), committed)
        receipt = self.store.get_command_receipt(self.scope.canonical_key, "subject-1", "command-1")
        self.assertEqual(receipt.auth_session_revision_json, "7")
        self.assertEqual(receipt.security_revision_json, "11")
        audit = self.store.list_audit_events()[0]
        self.assertEqual(audit["auth_session_revision_json"], "7")
        self.assertEqual(audit["security_revision_json"], "11")
        with self.assertRaises(AuthorizationDeniedError):
            self.execute(context=self.context(principal=Principal("subject-1", (), (self.scope,), 8, 12)))
        with self.assertRaises(ScopeDeniedError):
            self.execute(context=self.context(principal=Principal("subject-1", (self.capability,), (AccessScope("other"),), 8, 12)))
        with self.assertRaises(VersionConflictError):
            self.execute(context=self.context(principal=Principal("another-subject", (self.capability,), (self.scope,), 8, 12)))
        self.assertEqual(self.store.count_rows(), before)

    def test_same_id_different_payload_is_typed_conflict_without_new_rows(self):
        self.execute()
        before = self.store.count_rows()
        with self.assertRaises(IdempotencyConflictError) as raised:
            self.execute(payload={"value": 2})
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.store.count_rows(), before)
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1").state["effect_count"], 1)

    def test_stale_expected_version_is_typed_conflict_without_mutation(self):
        self.execute()
        before = (self.store.get_aggregate(self.scope, "fixture", "aggregate-1"), self.store.count_rows())
        with self.assertRaises(VersionConflictError) as raised:
            self.execute(context=self.context("stale-command", expected=0), payload={"value": "stale"})
        self.assertEqual(raised.exception.code, "VERSION_CONFLICT")
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1"), before[0])
        self.assertEqual(self.store.count_rows(), before[1])

    def test_concurrent_first_attempts_use_separate_connections_and_resolve_to_one_receipt(self):
        first = PostgreSQLReferenceTransactionAdapter(DSN)
        second = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def attempt(store):
            try:
                barrier.wait(timeout=10)
                results.append(self.execute(store=_ReceiptRaceBarrierStore(store, barrier), context=self.context("concurrent-command")))
            except Exception as exc:  # pragma: no cover - assertion reports any race failure
                failures.append(exc)

        threads = [threading.Thread(target=attempt, args=(store,)) for store in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(failures, failures)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.store.count_rows(), {"aggregate_state": 1, "command_receipt": 1, "audit_event": 1, "outbox_event": 1})
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1").version, 1)

    def test_audit_trigger_rolls_back_aggregate_sibling_event_and_receipt(self):
        self.store.connection.execute(
            "CREATE OR REPLACE FUNCTION ephi_test_fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test audit fault'; END; $$"
        )
        self.store.connection.execute("CREATE TRIGGER ephi_test_fail_audit_trigger BEFORE INSERT ON audit_event FOR EACH ROW EXECUTE FUNCTION ephi_test_fail_audit()")
        self.addCleanup(lambda: self.store.connection.execute("DROP TRIGGER IF EXISTS ephi_test_fail_audit_trigger ON audit_event"))
        self.addCleanup(lambda: self.store.connection.execute("DROP FUNCTION IF EXISTS ephi_test_fail_audit() CASCADE"))
        with self.assertRaises(StorageFailureError):
            self.execute()
        self.assertEqual(self.store.count_rows(), {"aggregate_state": 1, "command_receipt": 0, "audit_event": 0, "outbox_event": 0})
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1").version, 0)

    def test_outbox_trigger_rolls_back_aggregate_sibling_event_and_receipt(self):
        self.store.connection.execute(
            "CREATE OR REPLACE FUNCTION ephi_test_fail_outbox() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test outbox fault'; END; $$"
        )
        self.store.connection.execute("CREATE TRIGGER ephi_test_fail_outbox_trigger BEFORE INSERT ON outbox_event FOR EACH ROW EXECUTE FUNCTION ephi_test_fail_outbox()")
        self.addCleanup(lambda: self.store.connection.execute("DROP TRIGGER IF EXISTS ephi_test_fail_outbox_trigger ON outbox_event"))
        self.addCleanup(lambda: self.store.connection.execute("DROP FUNCTION IF EXISTS ephi_test_fail_outbox() CASCADE"))
        with self.assertRaises(StorageFailureError):
            self.execute()
        self.assertEqual(self.store.count_rows(), {"aggregate_state": 1, "command_receipt": 0, "audit_event": 0, "outbox_event": 0})
        self.assertEqual(self.store.get_aggregate(self.scope, "fixture", "aggregate-1").version, 0)

    def test_closed_connection_is_a_typed_storage_failure(self):
        self.store.close()
        with self.assertRaises(StorageFailureError):
            self.execute()


if __name__ == "__main__":
    unittest.main()
