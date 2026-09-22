"""CHG-167 O5.1 regressions for the unified Episode workflow authority."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    ACTION_RECORD_CAPABILITY,
    ACKNOWLEDGE_EPISODE_CAPABILITY,
    CHECK_EXECUTE_CAPABILITY,
    CHECK_REQUEST_CAPABILITY,
    CLOSURE_CAPABILITY,
    CLAIM_EPISODE_CAPABILITY,
    DECISION_LOOP_AGGREGATE_TYPE,
    DECISION_LOOP_CREATE_CAPABILITY,
    DECISION_LOOP_READ_CAPABILITY,
    EPISODE_WORKFLOW_AGGREGATE_TYPE,
    AccessScope,
    ActionReconciliationState,
    AuthorizationDeniedError,
    CheckExecutionMode,
    ClosureDisposition,
    CommandContext,
    CoherentReadConflictError,
    DecisionLoopCommandService,
    EpisodeBrief,
    EpisodeBriefQueryService,
    EpisodeWorkflowCommandService,
    IdempotencyConflictError,
    InvalidTransitionError,
    MutableCurrentAuthorizationAuthority,
    Principal,
    ReadRevision,
    ReadRevisionIdentity,
    RevisionVector,
    StorageFailureError,
    ValidationFailureError,
    VersionConflictError,
)
from ephi.application.read import CurrentReadBundle  # noqa: E402
from ephi.infrastructure import AggregateSnapshot, SQLiteReferenceTransactionAdapter  # noqa: E402
from ephi.recovery import ObservationOutcome, RecoveryObservation, RecoveryPolicy, Severity  # noqa: E402


class _ReadDouble:
    def __init__(self, bundle):
        self.bundle = bundle

    def read_current_bundle(self, principal, scope, entity_type, entity_id, required_read_capability):
        del principal, scope, entity_type, entity_id, required_read_capability
        return self.bundle

    def read_historical_bundle(self, *args, **kwargs):  # pragma: no cover - not used by this regression
        raise AssertionError("historical read was not expected")

    def publish_read_revision(self, *args, **kwargs):  # pragma: no cover - protocol surface only
        raise AssertionError("publish was not expected")

    def create_query_snapshot(self, *args, **kwargs):  # pragma: no cover - protocol surface only
        raise AssertionError("query snapshot was not expected")

    def read_query_snapshot_page(self, *args, **kwargs):  # pragma: no cover - protocol surface only
        raise AssertionError("query page was not expected")


class O5UnifiedAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "o5.sqlite3"
        self.scope = AccessScope("o5-scope", site_id="site-1", area_id="area-1")
        self.capabilities = (
            ACTION_RECORD_CAPABILITY,
            ACKNOWLEDGE_EPISODE_CAPABILITY,
            CHECK_EXECUTE_CAPABILITY,
            CHECK_REQUEST_CAPABILITY,
            CLAIM_EPISODE_CAPABILITY,
            CLOSURE_CAPABILITY,
            DECISION_LOOP_CREATE_CAPABILITY,
            DECISION_LOOP_READ_CAPABILITY,
            "ephi.episode.read",
            "ephi.decision_loop.reopen",
            "ephi.decision_loop.recovery.plan",
            "ephi.decision_loop.recovery.observe",
            "ephi.decision_loop.action.record",
        )
        self.principal = Principal("engineer-1", self.capabilities, (self.scope,), 1, 1)
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.store = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(self.store.close)
        self.store.seed_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-1", {"owner": None, "work_state": "OPEN"})
        self.workflow = EpisodeWorkflowCommandService(self.store, self.authorization)
        self.service = DecisionLoopCommandService(self.store, self.authorization)

    def context(self, command_id, expected, *, principal=None, reason=None):
        principal = principal or self.principal
        return CommandContext(
            command_id,
            principal,
            self.scope,
            expected,
            RevisionVector("analysis-1", "exposure-1", "priority-1", expected, None, "manifest-1"),
            reason,
        )

    def acknowledge_and_initialize(self):
        result = self.workflow.claim_episode(self.context("claim", 0), "episode-1")
        result = self.workflow.acknowledge_episode(self.context("ack", result.aggregate_version), "episode-1")
        return self.service.initialize_decision_loop(self.context("init", result.aggregate_version), "episode-1")

    @staticmethod
    def policy(minimum=2):
        base = RecoveryPolicy.deterministic_w0_regression()
        return RecoveryPolicy(
            policy_id=base.policy_id,
            confidence_floor=base.confidence_floor,
            minimum_eligible_independent_samples=minimum,
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
    def observation(observation_id, episode_id, at):
        return RecoveryObservation(
            observation_id,
            episode_id,
            f"sample-{observation_id}",
            at,
            at,
            at,
            "W0_CONTEXT",
            "W0_CHARACTERISTIC",
            "W0_UNIT",
            Severity.NORMAL,
            ObservationOutcome.NORMAL,
            0.95,
            "none",
        )

    def action(self, version, *, state=ActionReconciliationState.SUCCEEDED, observed_at=None, command_id="action"):
        observed_at = observed_at or datetime.now(timezone.utc)
        kwargs = {
            "action_type": "APPROVED_WORK_REQUEST",
            "external_system": "approved-system",
            "work_request_id": "wr-1",
            "reconciliation_state": state,
            "evidence_refs": ("action-proof",) if state != ActionReconciliationState.UNKNOWN else (),
        }
        if state != ActionReconciliationState.UNKNOWN:
            kwargs["observed_at"] = observed_at
        return self.service.record_external_action(self.context(command_id, version), "episode-1", "action-1", **kwargs)

    def test_o3_and_o5_use_one_episode_workflow_row_and_version(self):
        initialized = self.acknowledge_and_initialize()
        self.assertEqual(DECISION_LOOP_AGGREGATE_TYPE, EPISODE_WORKFLOW_AGGREGATE_TYPE)
        self.assertIsNone(self.store.get_aggregate(self.scope, "ephi_decision_loop", "episode-1"))
        rows = self.store.connection.execute(
            "SELECT aggregate_type, version FROM aggregate_state WHERE scope_key = ? AND aggregate_id = ?",
            (self.scope.canonical_key, "episode-1"),
        ).fetchall()
        self.assertEqual([(EPISODE_WORKFLOW_AGGREGATE_TYPE, initialized.aggregate_version)], [tuple(row) for row in rows])
        self.assertEqual(initialized.state["work_state"], "ACKNOWLEDGED")
        self.assertEqual(initialized.aggregate_version, initialized.state["decision_loop"]["last_viewed_revisions"]["workflow_version"])
        action = self.action(initialized.aggregate_version)
        self.assertEqual(action.aggregate_type, EPISODE_WORKFLOW_AGGREGATE_TYPE)
        self.assertEqual(action.aggregate_version, initialized.aggregate_version + 1)
        snapshot = self.service.get_decision_loop(self.principal, self.scope, "episode-1")
        self.assertEqual(snapshot.revision_vector.workflow_version, snapshot.aggregate_version)
        self.assertEqual(snapshot.workflow_state["work_state"], "ACKNOWLEDGED")

    def test_o3_o5_same_expected_version_serialize_without_split_brain(self):
        claimed = self.workflow.claim_episode(self.context("claim", 0), "episode-1")
        first = SQLiteReferenceTransactionAdapter(self.path)
        second = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        o3 = EpisodeWorkflowCommandService(first, self.authorization)
        o5 = DecisionLoopCommandService(second, self.authorization)
        barrier = threading.Barrier(2)
        committed, conflicts, failures = [], [], []

        def attempt(service, command_id, is_o3):
            try:
                barrier.wait(timeout=5)
                if is_o3:
                    committed.append(service.acknowledge_episode(self.context(command_id, claimed.aggregate_version), "episode-1"))
                else:
                    committed.append(service.initialize_decision_loop(self.context(command_id, claimed.aggregate_version), "episode-1"))
            except VersionConflictError as exc:
                conflicts.append(exc)
            except Exception as exc:  # pragma: no cover - assertion reports race failures
                failures.append(exc)

        threads = [
            threading.Thread(target=attempt, args=(o3, "o3-race", True)),
            threading.Thread(target=attempt, args=(o5, "o5-race", False)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(failures, failures)
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(conflicts), 1)
        aggregate = self.store.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-1")
        self.assertEqual(aggregate.version, claimed.aggregate_version + 1)
        self.assertNotEqual(self.store.get_aggregate(self.scope, "ephi_decision_loop", "episode-1"), aggregate)

    def test_action_recovery_closure_requires_exact_qualifying_post_action_proof(self):
        initialized = self.acknowledge_and_initialize()
        with self.assertRaises(ValidationFailureError):
            self.service.create_recovery_plan(
                self.context("bad-binding", initialized.aggregate_version), "episode-1", "missing-plan",
                policy=self.policy(), prior_action_id="does-not-exist",
                context_identity="W0_CONTEXT", characteristic_identity="W0_CHARACTERISTIC", unit_identity="W0_UNIT",
            )
        action_at = datetime.now(timezone.utc)
        action = self.action(initialized.aggregate_version, observed_at=action_at)
        plan = self.service.create_recovery_plan(
            self.context("plan", action.aggregate_version), "episode-1", "plan-1", policy=self.policy(), prior_action_id="action-1",
            context_identity="W0_CONTEXT", characteristic_identity="W0_CHARACTERISTIC", unit_identity="W0_UNIT",
        )
        plan = self.service.lock_recovery_plan(self.context("lock", plan.aggregate_version), "episode-1", "plan-1")
        pre = action_at - timedelta(minutes=1)
        plan = self.service.submit_recovery_observation(
            self.context("pre", plan.aggregate_version), "episode-1", "plan-1", self.observation("pre", "episode-1", pre),
            evaluated_at=action_at + timedelta(seconds=1), evidence_refs=("pre-proof",),
        )
        stored_plan = plan.state["decision_loop"]["cycles"][0]["recovery_plans"]["plan-1"]
        self.assertFalse(stored_plan["assessments"][-1]["post_action"])
        self.assertNotEqual(stored_plan["state"], "PASS")
        for index in range(2):
            at = action_at + timedelta(minutes=index + 1)
            plan = self.service.submit_recovery_observation(
                self.context(f"post-{index}", plan.aggregate_version), "episode-1", "plan-1", self.observation(f"post-{index}", "episode-1", at),
                evaluated_at=at, evidence_refs=(f"post-proof-{index}",),
            )
        stored_plan = plan.state["decision_loop"]["cycles"][0]["recovery_plans"]["plan-1"]
        self.assertEqual(stored_plan["state"], "PASS")
        with self.assertRaises(ValidationFailureError):
            self.service.close_episode(self.context("wrong-plan", plan.aggregate_version), "episode-1", disposition=ClosureDisposition.CONFIRMED_ISSUE, recovery_plan_id="unknown")
        closed = self.service.close_episode(self.context("close", plan.aggregate_version), "episode-1", disposition=ClosureDisposition.CONFIRMED_ISSUE, recovery_plan_id="plan-1", check_ids=())
        closure = closed.state["decision_loop"]["cycles"][0]["closures"][0]
        self.assertEqual(closure["action_id"], "action-1")
        self.assertEqual(closure["recovery_plan_id"], "plan-1")
        self.assertIn("post-proof-0", closure["evidence_ids"])
        self.assertEqual(closed.state["work_state"], "CLOSED")

    def test_unbound_pass_is_technical_only_and_cannot_confirm_issue(self):
        initialized = self.acknowledge_and_initialize()
        plan = self.service.create_recovery_plan(
            self.context("plan", initialized.aggregate_version), "episode-1", "plan-1", policy=self.policy(minimum=1),
            context_identity="W0_CONTEXT", characteristic_identity="W0_CHARACTERISTIC", unit_identity="W0_UNIT",
        )
        plan = self.service.lock_recovery_plan(self.context("lock", plan.aggregate_version), "episode-1", "plan-1")
        now = datetime.now(timezone.utc)
        plan = self.service.submit_recovery_observation(
            self.context("pass", plan.aggregate_version), "episode-1", "plan-1", self.observation("pass", "episode-1", now),
            evaluated_at=now, evidence_refs=("proof",),
        )
        self.assertEqual(plan.state["decision_loop"]["cycles"][0]["recovery_plans"]["plan-1"]["state"], "PASS")
        self.assertEqual(plan.state["work_state"], "ACKNOWLEDGED")
        with self.assertRaises(InvalidTransitionError):
            self.service.close_episode(self.context("close", plan.aggregate_version), "episode-1", disposition=ClosureDisposition.CONFIRMED_ISSUE, recovery_plan_id="plan-1")
        self.assertEqual(self.store.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-1").state["work_state"], "ACKNOWLEDGED")

    def test_closed_workflow_is_coherent_in_o3_and_o5_and_reopen_resets_owner(self):
        initialized = self.acknowledge_and_initialize()
        closed = self.service.close_episode(self.context("close", initialized.aggregate_version), "episode-1", disposition=ClosureDisposition.BENIGN, evidence_refs=("benign",), reason="not an issue")
        self.assertEqual(closed.state["work_state"], "CLOSED")
        with self.assertRaises(InvalidTransitionError):
            self.workflow.claim_episode(self.context("claim-closed", closed.aggregate_version), "episode-1")
        with self.assertRaises(InvalidTransitionError):
            self.workflow.acknowledge_episode(self.context("ack-closed", closed.aggregate_version), "episode-1")
        prior_cycle = deepcopy(closed.state["decision_loop"]["cycles"][0])
        reopened = self.service.reopen_episode(self.context("reopen", closed.aggregate_version), "episode-1", reason="new evidence")
        self.assertEqual(reopened.state["work_state"], "OPEN")
        self.assertIsNone(reopened.state["owner"])
        self.assertEqual(reopened.state["decision_loop"]["cycles"][0], prior_cycle)
        claimed = self.workflow.claim_episode(self.context("claim-new-cycle", reopened.aggregate_version), "episode-1")
        self.assertEqual(claimed.state["work_state"], "CLAIMED")
        self.assertEqual(len(claimed.state["decision_loop"]["cycles"]), 2)

    def test_revision_context_authorization_idempotency_and_atomic_faults_fail_closed(self):
        initialized = self.acknowledge_and_initialize()
        with self.assertRaises(ValidationFailureError):
            self.service.request_check(CommandContext("missing-view", self.principal, self.scope, initialized.aggregate_version, None), "episode-1", "check", template_id="t", template_version="1", execution_mode=CheckExecutionMode.READ_EXISTING)
        replay = self.service.initialize_decision_loop(self.context("init", 2), "episode-1")
        self.assertEqual(replay, initialized)
        with self.assertRaises(IdempotencyConflictError):
            self.service.request_check(self.context("init", initialized.aggregate_version), "episode-1", "different", template_id="t", template_version="1", execution_mode=CheckExecutionMode.READ_EXISTING)
        revoked = Principal(self.principal.subject, (), (self.scope,), 2, 2)
        self.authorization.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.service.get_decision_loop(revoked, self.scope, "episode-1")
        self.authorization.set_principal(self.principal)
        before = self.store.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-1")
        self.store.connection.execute("CREATE TRIGGER fail_o5_audit BEFORE INSERT ON audit_event BEGIN SELECT RAISE(ABORT, 'test audit fault'); END")
        with self.assertRaises(StorageFailureError):
            self.service.request_check(self.context("audit-fault", before.version), "episode-1", "check", template_id="t", template_version="1", execution_mode=CheckExecutionMode.READ_EXISTING)
        after = self.store.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-1")
        self.assertEqual(after, before)
        self.store.connection.execute("DROP TRIGGER fail_o5_audit")
        self.store.connection.execute("CREATE TRIGGER fail_o5_outbox BEFORE INSERT ON outbox_event BEGIN SELECT RAISE(ABORT, 'test outbox fault'); END")
        with self.assertRaises(StorageFailureError):
            self.service.request_check(self.context("outbox-fault", before.version), "episode-1", "check", template_id="t", template_version="1", execution_mode=CheckExecutionMode.READ_EXISTING)
        self.assertEqual(self.store.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-1"), before)

    def test_restart_preserves_nested_state_and_episode_brief_workflow_truth(self):
        initialized = self.acknowledge_and_initialize()
        action = self.action(initialized.aggregate_version)
        self.store.close()
        reopened = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(reopened.close)
        service = DecisionLoopCommandService(reopened, self.authorization)
        snapshot = service.get_decision_loop(self.principal, self.scope, "episode-1")
        self.assertEqual(snapshot.action_state["action-1"]["reconciliation_state"], "SUCCEEDED")
        closed = service.close_episode(self.context("close", action.aggregate_version), "episode-1", disposition=ClosureDisposition.BENIGN, evidence_refs=("benign",))
        current = reopened.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-1")
        old_workflow = AggregateSnapshot(self.scope.canonical_key, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-1", initialized.aggregate_version, initialized.state)
        revision = ReadRevision(
            ReadRevisionIdentity("read-1", self.scope, "episode", "episode-1"),
            RevisionVector("analysis-1", "exposure-1", "priority-1", old_workflow.version, None, "manifest-1"),
            {"episode_id": "episode-1", "capability_state": {"source": "READY"}},
            datetime.now(timezone.utc), datetime.now(timezone.utc), old_workflow,
        )
        brief = EpisodeBriefQueryService(_ReadDouble(CurrentReadBundle(revision, current, RevisionVector("analysis-1", "exposure-1", "priority-1", current.version, None, "manifest-1"))), self.authorization).get_episode_brief(self.principal, self.scope, "episode-1")
        self.assertEqual(brief.workflow["work_state"], "CLOSED")
        self.assertEqual(service.get_decision_loop(self.principal, self.scope, "episode-1").workflow_state["work_state"], "CLOSED")
        self.assertEqual(closed.aggregate_version, current.version)

    def test_command_target_changes_conflict_and_post_action_unknown_never_proves_recovery(self):
        initialized = self.acknowledge_and_initialize()
        replay = self.service.request_check(self.context("check", initialized.aggregate_version), "episode-1", "check-1", template_id="t", template_version="1", execution_mode=CheckExecutionMode.READ_EXISTING)
        with self.assertRaises(IdempotencyConflictError):
            self.service.request_check(self.context("check", initialized.aggregate_version), "episode-2", "check-2", template_id="t", template_version="1", execution_mode=CheckExecutionMode.READ_EXISTING)
        with self.assertRaises(IdempotencyConflictError):
            self.service.start_check(self.context("check", initialized.aggregate_version), "episode-1", "check-1")
        unknown = self.action(replay.aggregate_version, state=ActionReconciliationState.UNKNOWN, command_id="unknown")
        plan = self.service.create_recovery_plan(
            self.context("unknown-plan", unknown.aggregate_version), "episode-1", "unknown-plan", policy=self.policy(minimum=1), prior_action_id="action-1",
            context_identity="W0_CONTEXT", characteristic_identity="W0_CHARACTERISTIC", unit_identity="W0_UNIT",
        )
        plan = self.service.lock_recovery_plan(self.context("unknown-lock", plan.aggregate_version), "episode-1", "unknown-plan")
        now = datetime.now(timezone.utc)
        plan = self.service.submit_recovery_observation(self.context("unknown-observation", plan.aggregate_version), "episode-1", "unknown-plan", self.observation("unknown-observation", "episode-1", now), evaluated_at=now, evidence_refs=("proof",))
        stored = plan.state["decision_loop"]["cycles"][0]["recovery_plans"]["unknown-plan"]
        self.assertNotEqual(stored["state"], "PASS")


if __name__ == "__main__":
    unittest.main()
