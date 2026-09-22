"""Real PostgreSQL 18.x evidence for the unified O3/O5 Episode workflow."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    ACTION_RECORD_CAPABILITY,
    ACKNOWLEDGE_EPISODE_CAPABILITY,
    CLAIM_EPISODE_CAPABILITY,
    CLOSURE_CAPABILITY,
    DECISION_LOOP_CREATE_CAPABILITY,
    DECISION_LOOP_READ_CAPABILITY,
    EPISODE_WORKFLOW_AGGREGATE_TYPE,
    REOPEN_CAPABILITY,
    RECOVERY_OBSERVATION_CAPABILITY,
    RECOVERY_PLAN_CAPABILITY,
    AccessScope,
    ActionReconciliationState,
    AuthorizationDeniedError,
    CommandContext,
    DecisionLoopCommandService,
    EpisodeWorkflowCommandService,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RevisionVector,
    VersionConflictError,
)
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402
from ephi.recovery import ObservationOutcome, RecoveryObservation, RecoveryPolicy, Severity  # noqa: E402


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN")


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not configured")
class O5PostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.scope = AccessScope("o5-pg-scope", site_id="site-1", area_id="area-1")
        self.capabilities = (
            ACTION_RECORD_CAPABILITY,
            ACKNOWLEDGE_EPISODE_CAPABILITY,
            CLAIM_EPISODE_CAPABILITY,
            CLOSURE_CAPABILITY,
            DECISION_LOOP_CREATE_CAPABILITY,
            DECISION_LOOP_READ_CAPABILITY,
            REOPEN_CAPABILITY,
            RECOVERY_OBSERVATION_CAPABILITY,
            RECOVERY_PLAN_CAPABILITY,
        )
        self.principal = Principal("pg-engineer", self.capabilities, (self.scope,), 1, 1)
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.workflow = EpisodeWorkflowCommandService(self.adapter, self.authorization)
        self.service = DecisionLoopCommandService(self.adapter, self.authorization)
        for table in ("outbox_event", "audit_event", "command_receipt", "aggregate_state"):
            self.adapter.connection.execute(f"DELETE FROM {table} WHERE scope_key = %s", (self.scope.canonical_key,))
        self.adapter.seed_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-pg", {"owner": None, "work_state": "OPEN"})

    def context(self, command_id, expected):
        return CommandContext(command_id, self.principal, self.scope, expected, RevisionVector("analysis-pg", None, None, expected, None, "manifest-pg"))

    def init(self):
        claimed = self.workflow.claim_episode(self.context("claim", 0), "episode-pg")
        acknowledged = self.workflow.acknowledge_episode(self.context("ack", claimed.aggregate_version), "episode-pg")
        return self.service.initialize_decision_loop(self.context("init", acknowledged.aggregate_version), "episode-pg")

    @staticmethod
    def policy():
        base = RecoveryPolicy.deterministic_w0_regression()
        return RecoveryPolicy(
            policy_id=base.policy_id,
            confidence_floor=base.confidence_floor,
            minimum_eligible_independent_samples=1,
            expected_context=base.expected_context,
            expected_characteristic=base.expected_characteristic,
            expected_unit=base.expected_unit,
            affirmative_outcome=base.affirmative_outcome,
            require_reference_valid=base.require_reference_valid,
            require_capability_valid=base.require_capability_valid,
            max_observation_age=base.max_observation_age,
            max_availability_delay=base.max_availability_delay,
        )

    @staticmethod
    def observation(at):
        return RecoveryObservation("pg-observation", "episode-pg", "pg-sample", at, at, at, "W0_CONTEXT", "W0_CHARACTERISTIC", "W0_UNIT", Severity.NORMAL, ObservationOutcome.NORMAL, 0.95, "none")

    def test_server_version_unified_row_closure_reopen_and_restart(self):
        self.assertTrue(self.adapter.server_version().startswith("18."), self.adapter.server_version())
        result = self.init()
        action_at = datetime.now(timezone.utc)
        result = self.service.record_external_action(
            self.context("action", result.aggregate_version), "episode-pg", "action-1",
            action_type="APPROVED_WORK_REQUEST", external_system="approved", work_request_id="wr-1",
            reconciliation_state=ActionReconciliationState.SUCCEEDED, observed_at=action_at, evidence_refs=("action-proof",),
        )
        result = self.service.create_recovery_plan(
            self.context("plan", result.aggregate_version), "episode-pg", "plan-1", policy=self.policy(), prior_action_id="action-1",
            context_identity="W0_CONTEXT", characteristic_identity="W0_CHARACTERISTIC", unit_identity="W0_UNIT",
        )
        result = self.service.lock_recovery_plan(self.context("lock", result.aggregate_version), "episode-pg", "plan-1")
        post = action_at + timedelta(minutes=1)
        result = self.service.submit_recovery_observation(self.context("observation", result.aggregate_version), "episode-pg", "plan-1", self.observation(post), evaluated_at=post, evidence_refs=("recovery-proof",))
        self.assertEqual(result.state["decision_loop"]["cycles"][0]["recovery_plans"]["plan-1"]["state"], "PASS")
        result = self.service.close_episode(self.context("close", result.aggregate_version), "episode-pg", disposition="CONFIRMED_ISSUE", recovery_plan_id="plan-1")
        self.assertEqual(result.state["work_state"], "CLOSED")
        self.adapter.close()
        reopened = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(reopened.close)
        service = DecisionLoopCommandService(reopened, self.authorization)
        snapshot = service.get_decision_loop(self.principal, self.scope, "episode-pg")
        self.assertEqual(snapshot.workflow_state["work_state"], "CLOSED")
        self.assertEqual(snapshot.revision_vector.workflow_version, snapshot.aggregate_version)
        reopened_result = service.reopen_episode(self.context("reopen", result.aggregate_version), "episode-pg", reason="new evidence")
        self.assertEqual(reopened_result.state["work_state"], "OPEN")
        self.assertEqual(reopened_result.state["decision_loop"]["cycles"][0]["closures"][0]["recovery_plan_id"], "plan-1")
        self.assertEqual(reopened_result.aggregate_type, EPISODE_WORKFLOW_AGGREGATE_TYPE)

    def test_postgresql_o3_o5_cas_interleaving_has_one_winner(self):
        claimed = self.workflow.claim_episode(self.context("claim-race", 0), "episode-pg")
        first = PostgreSQLReferenceTransactionAdapter(DSN)
        second = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        o3 = EpisodeWorkflowCommandService(first, self.authorization)
        o5 = DecisionLoopCommandService(second, self.authorization)
        barrier = threading.Barrier(2)
        committed, conflicts, failures = [], [], []

        def attempt(service, command_id, is_o3):
            try:
                barrier.wait(timeout=10)
                if is_o3:
                    committed.append(service.acknowledge_episode(self.context(command_id, claimed.aggregate_version), "episode-pg"))
                else:
                    committed.append(service.initialize_decision_loop(self.context(command_id, claimed.aggregate_version), "episode-pg"))
            except VersionConflictError as exc:
                conflicts.append(exc)
            except Exception as exc:  # pragma: no cover - assertion reports driver failures
                failures.append(exc)

        threads = [threading.Thread(target=attempt, args=(o3, "pg-o3-race", True)), threading.Thread(target=attempt, args=(o5, "pg-o5-race", False))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(failures, failures)
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(conflicts), 1)
        row = first.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-pg")
        self.assertEqual(row.version, claimed.aggregate_version + 1)
        self.assertIsNone(first.get_aggregate(self.scope, "ephi_decision_loop", "episode-pg"))

    def test_current_authorization_blocks_o5_read_and_write_disclosure(self):
        initialized = self.init()
        revoked = Principal("pg-engineer", (), (self.scope,), 2, 2)
        self.authorization.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.service.get_decision_loop(revoked, self.scope, "episode-pg")
        with self.assertRaises(AuthorizationDeniedError):
            self.service.record_external_action(
                self.context("revoked-action", initialized.aggregate_version), "episode-pg", "revoked-action",
                action_type="APPROVED_WORK_REQUEST", external_system="approved", work_request_id="wr-revoked",
            )


if __name__ == "__main__":
    unittest.main()
