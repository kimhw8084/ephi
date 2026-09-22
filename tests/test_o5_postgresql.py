"""Real PostgreSQL 18.x CHG-167 integration evidence.

The suite is intentionally DSN-gated like the existing O2/O3 PostgreSQL
reference suites.  No implicit local or in-memory fallback is permitted.
"""

from pathlib import Path
import os
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    ACTION_RECORD_CAPABILITY,
    CLOSURE_CAPABILITY,
    DECISION_LOOP_CREATE_CAPABILITY,
    DECISION_LOOP_READ_CAPABILITY,
    REOPEN_CAPABILITY,
    AccessScope,
    ActionReconciliationState,
    CheckExecutionMode,
    CommandContext,
    DecisionLoopCommandService,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RevisionVector,
    VersionConflictError,
)
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN")


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not configured")
class O5PostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.scope = AccessScope("o5-pg-scope", site_id="site-1")
        self.capabilities = (
            ACTION_RECORD_CAPABILITY,
            CLOSURE_CAPABILITY,
            DECISION_LOOP_CREATE_CAPABILITY,
            DECISION_LOOP_READ_CAPABILITY,
            REOPEN_CAPABILITY,
        )
        self.principal = Principal("pg-engineer", self.capabilities, (self.scope,), 1, 1)
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.service = DecisionLoopCommandService(self.adapter, self.authorization)
        self.adapter.connection.execute("DELETE FROM outbox_event WHERE scope_key = %s", (self.scope.canonical_key,))
        self.adapter.connection.execute("DELETE FROM audit_event WHERE scope_key = %s", (self.scope.canonical_key,))
        self.adapter.connection.execute("DELETE FROM command_receipt WHERE scope_key = %s", (self.scope.canonical_key,))
        self.adapter.connection.execute("DELETE FROM aggregate_state WHERE scope_key = %s", (self.scope.canonical_key,))
        self.service.create_episode(self.context("create", None), "episode-pg")

    def context(self, command_id: str, expected: int | None, *, principal: Principal | None = None):
        principal = principal or self.principal
        workflow_version = expected if expected is not None else 0
        return CommandContext(
            command_id,
            principal,
            self.scope,
            expected,
            RevisionVector("analysis-pg", None, None, workflow_version, None, "manifest-pg"),
        )

    def test_server_is_postgresql_18_and_restart_reopen_preserves_history(self):
        self.assertTrue(self.adapter.server_version().startswith("18."), self.adapter.server_version())
        result = self.service.record_external_action(self.context("action", 1), "episode-pg", "action-1", action_type="APPROVED_WORK_REQUEST", external_system="approved", work_request_id="wr-1", reconciliation_state=ActionReconciliationState.UNKNOWN)
        self.adapter.close()
        reopened = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(reopened.close)
        service = DecisionLoopCommandService(reopened, self.authorization)
        snapshot = service.get_decision_loop(self.principal, self.scope, "episode-pg")
        self.assertEqual(snapshot.action_state["action-1"]["reconciliation_state"], "UNKNOWN")
        result = service.close_episode(self.context("close", result.aggregate_version), "episode-pg", disposition="BENIGN", evidence_refs=("pg-evidence",))
        result = service.reopen_episode(self.context("reopen", result.aggregate_version), "episode-pg", reason="pg follow-up")
        snapshot = service.get_decision_loop(self.principal, self.scope, "episode-pg")
        self.assertEqual(len(snapshot.state["cycles"]), 2)
        self.assertEqual(snapshot.state["cycles"][0]["closures"][0]["disposition"], "BENIGN")
        self.assertNotEqual(snapshot.active_cycle_id, "cycle-1")

    def test_two_postgres_sessions_cannot_create_two_effects_at_one_version(self):
        first = PostgreSQLReferenceTransactionAdapter(DSN)
        second = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first_service = DecisionLoopCommandService(first, self.authorization)
        second_service = DecisionLoopCommandService(second, self.authorization)
        barrier = threading.Barrier(2)
        committed = []
        conflicts = []
        failures = []

        def attempt(service, command_id):
            try:
                barrier.wait(timeout=10)
                committed.append(service.record_external_action(self.context(command_id, 1), "episode-pg", command_id, action_type="APPROVED_WORK_REQUEST", external_system="approved", work_request_id=command_id))
            except VersionConflictError as exc:
                conflicts.append(exc)
            except Exception as exc:  # pragma: no cover - assertion reports a driver/race failure
                failures.append(exc)

        threads = [
            threading.Thread(target=attempt, args=(first_service, "race-a")),
            threading.Thread(target=attempt, args=(second_service, "race-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(failures, failures)
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(conflicts), 1)
        snapshot = first_service.get_decision_loop(self.principal, self.scope, "episode-pg")
        self.assertEqual(len(snapshot.action_state), 1)


if __name__ == "__main__":
    unittest.main()
