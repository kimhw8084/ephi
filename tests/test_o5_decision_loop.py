"""Focused CHG-167/O5.1 durable decision-loop evidence."""

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
    CHECK_EXECUTE_CAPABILITY,
    CHECK_REQUEST_CAPABILITY,
    CLOSURE_CAPABILITY,
    DECISION_LOOP_CREATE_CAPABILITY,
    DECISION_LOOP_READ_CAPABILITY,
    RECOVERY_OBSERVATION_CAPABILITY,
    RECOVERY_PLAN_CAPABILITY,
    REOPEN_CAPABILITY,
    AccessScope,
    ActionReconciliationState,
    AuthorizationDeniedError,
    CheckExecutionMode,
    CheckOutcome,
    ClosureDisposition,
    CommandContext,
    DecisionLoopCommandService,
    IdempotencyConflictError,
    InvalidTransitionError,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RevisionVector,
    StorageFailureError,
    ValidationFailureError,
    VersionConflictError,
)
from ephi.infrastructure import SQLiteReferenceTransactionAdapter  # noqa: E402
from ephi.recovery import (  # noqa: E402
    ObservationOutcome,
    RecoveryObservation,
    RecoveryPolicy,
    Severity,
)


class O5DecisionLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "o5.sqlite3"
        self.scope = AccessScope("o5-scope", site_id="site-1", area_id="area-1")
        self.capabilities = (
            ACTION_RECORD_CAPABILITY,
            CHECK_EXECUTE_CAPABILITY,
            CHECK_REQUEST_CAPABILITY,
            CLOSURE_CAPABILITY,
            DECISION_LOOP_CREATE_CAPABILITY,
            DECISION_LOOP_READ_CAPABILITY,
            RECOVERY_OBSERVATION_CAPABILITY,
            RECOVERY_PLAN_CAPABILITY,
            REOPEN_CAPABILITY,
        )
        self.principal = Principal("engineer-1", self.capabilities, (self.scope,), 1, 1)
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.store = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(self.store.close)
        self.service = DecisionLoopCommandService(self.store, self.authorization)
        self.service.create_episode(self.context("create", None), "episode-1")

    def context(self, command_id: str, expected: int | None, *, principal: Principal | None = None) -> CommandContext:
        principal = principal or self.principal
        workflow_version = expected if expected is not None else 0
        return CommandContext(
            command_id,
            principal,
            self.scope,
            expected,
            RevisionVector("analysis-1", "exposure-1", "priority-1", workflow_version, None, "manifest-1"),
        )

    def check(self, version: int, command_id: str = "check-request"):
        return self.service.request_check(
            self.context(command_id, version),
            "episode-1",
            "check-1",
            template_id="bounded-template",
            template_version="v1",
            execution_mode=CheckExecutionMode.READ_EXISTING,
            required_capabilities=("source.read",),
            prerequisite_state={"source": "READY"},
            target_context={"target": "unit-1"},
        )

    def policy(self, minimum: int = 3) -> RecoveryPolicy:
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

    def observation(self, name: str, now: datetime, *, outcome=ObservationOutcome.NORMAL, confidence=0.95, sampling=None, available=True, context="W0_CONTEXT", characteristic="W0_CHARACTERISTIC", unit="W0_UNIT", reference=True, capability=True, hypothesis="none"):
        observed = now - timedelta(minutes=1)
        return RecoveryObservation(
            name,
            "episode-1",
            sampling or name,
            observed,
            observed,
            observed if available else None,
            context,
            characteristic,
            unit,
            Severity.NORMAL,
            outcome,
            confidence,
            hypothesis,
            reference_valid=reference,
            capability_valid=capability,
        )

    def create_plan(self, version: int, command_id="plan", minimum=3):
        return self.service.create_recovery_plan(
            self.context(command_id, version),
            "episode-1",
            "plan-1",
            policy=self.policy(minimum),
            context_identity="W0_CONTEXT",
            characteristic_identity="W0_CHARACTERISTIC",
            unit_identity="W0_UNIT",
        )

    def test_check_lifecycle_and_immutable_result_history(self):
        result = self.check(1)
        with self.assertRaises(InvalidTransitionError):
            self.service.complete_check(self.context("complete-before-start", result.aggregate_version), "episode-1", "check-1", outcome=CheckOutcome.SUPPORTS_A, evidence_refs=("artifact-1",))
        result = self.service.start_check(self.context("check-start", result.aggregate_version), "episode-1", "check-1")
        result = self.service.complete_check(self.context("check-complete", result.aggregate_version), "episode-1", "check-1", outcome=CheckOutcome.SUPPORTS_A, evidence_refs=("artifact-1",))
        self.assertEqual(result.state["cycles"][0]["checks"]["check-1"]["status"], "COMPLETED")
        self.assertEqual(len(result.state["cycles"][0]["checks"]["check-1"]["completion_history"]), 1)
        with self.assertRaises(InvalidTransitionError):
            self.service.cancel_check(self.context("check-cancel", result.aggregate_version), "episode-1", "check-1")
        self.store.close()
        reopened = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(reopened.close)
        snapshot = DecisionLoopCommandService(reopened, self.authorization).get_decision_loop(self.principal, self.scope, "episode-1")
        self.assertEqual(snapshot.check_state["check-1"]["result"]["evidence_refs"], ["artifact-1"])
        self.assertEqual(len(reopened.list_audit_events()), 4)

    def test_stale_idempotency_and_authorization_fail_closed(self):
        result = self.check(1)
        before = self.store.count_rows()
        with self.assertRaises(VersionConflictError):
            self.check(1, "stale-check")
        self.assertEqual(self.store.count_rows(), before)
        replay = self.check(1)
        self.assertEqual(replay, result)
        with self.assertRaises(IdempotencyConflictError):
            self.service.request_check(self.context("check-request", 1), "episode-1", "different-check", template_id="bounded-template", template_version="v1", execution_mode=CheckExecutionMode.READ_EXISTING)
        revoked = Principal("engineer-1", (), (self.scope,), 2, 2)
        self.authorization.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.service.get_decision_loop(revoked, self.scope, "episode-1")
        with self.assertRaises(AuthorizationDeniedError):
            self.service.request_check(self.context("revoked", 1, principal=revoked), "episode-1", "check-revoked", template_id="t", template_version="1", execution_mode=CheckExecutionMode.READ_EXISTING)

    def test_external_unknown_is_durable_and_not_implicitly_retried(self):
        result = self.service.record_external_action(self.context("action", 1), "episode-1", "action-1", action_type="APPROVED_WORK_REQUEST", external_system="approved-system", work_request_id="wr-1")
        self.assertEqual(result.state["cycles"][0]["actions"]["action-1"]["reconciliation_state"], "UNKNOWN")
        with self.assertRaises(InvalidTransitionError):
            self.service.reconcile_external_action(self.context("unknown-again", result.aggregate_version), "episode-1", "action-1", reconciliation_state=ActionReconciliationState.UNKNOWN)
        result = self.service.reconcile_external_action(self.context("action-success", result.aggregate_version), "episode-1", "action-1", reconciliation_state=ActionReconciliationState.SUCCEEDED, evidence_refs=("external-proof",))
        self.assertEqual(result.state["cycles"][0]["actions"]["action-1"]["reconciliation_state"], "SUCCEEDED")
        with self.assertRaises(InvalidTransitionError):
            self.service.reconcile_external_action(self.context("action-retry", result.aggregate_version), "episode-1", "action-1", reconciliation_state=ActionReconciliationState.SUCCEEDED, evidence_refs=("duplicate",))

    def test_concurrent_o5_commands_have_one_effect_and_one_version_conflict(self):
        first = SQLiteReferenceTransactionAdapter(self.path)
        second = SQLiteReferenceTransactionAdapter(self.path)
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
                barrier.wait(timeout=5)
                committed.append(service.record_external_action(self.context(command_id, 1), "episode-1", command_id, action_type="APPROVED_WORK_REQUEST", external_system="approved", work_request_id=command_id))
            except VersionConflictError as exc:
                conflicts.append(exc)
            except Exception as exc:  # pragma: no cover - assertion reports a race failure
                failures.append(exc)

        threads = [
            threading.Thread(target=attempt, args=(first_service, "race-a")),
            threading.Thread(target=attempt, args=(second_service, "race-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(failures, failures)
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(conflicts), 1)
        snapshot = first_service.get_decision_loop(self.principal, self.scope, "episode-1")
        self.assertEqual(len(snapshot.action_state), 1)

    def test_locked_recovery_and_false_recovery_inputs_never_pass(self):
        result = self.create_plan(1)
        result = self.service.lock_recovery_plan(self.context("lock", result.aggregate_version), "episode-1", "plan-1")
        with self.assertRaises(InvalidTransitionError):
            self.service.update_recovery_plan(self.context("locked-update", result.aggregate_version), "episode-1", "plan-1", policy=self.policy(2))
        now = datetime.now(timezone.utc)
        bad = [
            self.observation("missing", now, available=False),
            self.observation("stale", now - timedelta(days=2), confidence=0.95),
            self.observation("low-confidence", now, confidence=0.2),
            self.observation("context-mismatch", now, context="wrong"),
            self.observation("characteristic-mismatch", now, characteristic="wrong"),
            self.observation("unit-mismatch", now, unit="wrong"),
            self.observation("reference-invalid", now, reference=False),
            self.observation("capability-invalid", now, capability=False),
            self.observation("pipeline-suspect", now, hypothesis="PIPELINE_SCHEMA_CHANGE"),
            self.observation("unknown", now, outcome=ObservationOutcome.UNKNOWN),
        ]
        version = result.aggregate_version
        for index, observation in enumerate(bad):
            result = self.service.submit_recovery_observation(self.context(f"bad-{index}", version), "episode-1", "plan-1", observation, evaluated_at=now, evidence_refs=(f"bad-ref-{index}",))
            version = result.aggregate_version
            self.assertNotEqual(result.state["cycles"][0]["recovery_plans"]["plan-1"]["state"], "PASS")
        result = self.service.submit_recovery_observation(self.context("duplicate", version), "episode-1", "plan-1", bad[-1], evaluated_at=now, evidence_refs=("duplicate-ref",))
        self.assertNotEqual(result.state["cycles"][0]["recovery_plans"]["plan-1"]["state"], "PASS")

    def test_qualified_contradiction_resets_and_required_independent_samples_pass(self):
        result = self.create_plan(1, minimum=2)
        now = datetime.now(timezone.utc)
        result = self.service.submit_recovery_observation(self.context("good-1", result.aggregate_version), "episode-1", "plan-1", self.observation("good-1", now), evaluated_at=now, evidence_refs=("r1",))
        self.assertEqual(result.state["cycles"][0]["recovery_plans"]["plan-1"]["state"], "MONITORING")
        result = self.service.submit_recovery_observation(self.context("contradiction", result.aggregate_version), "episode-1", "plan-1", self.observation("bad-abnormal", now, outcome=ObservationOutcome.ABNORMAL), evaluated_at=now, evidence_refs=("r2",))
        plan = result.state["cycles"][0]["recovery_plans"]["plan-1"]
        self.assertEqual(plan["state"], "FAIL")
        self.assertEqual(plan["eligible_independent_count"], 0)

        # A fresh plan in a separate Episode keeps the PASS proof independent
        # from the contradiction reset above.
        created = self.service.create_episode(self.context("create-2", None), "episode-2")
        result = self.service.create_recovery_plan(self.context("plan-2", created.aggregate_version), "episode-2", "plan-2", policy=self.policy(2), context_identity="W0_CONTEXT", characteristic_identity="W0_CHARACTERISTIC", unit_identity="W0_UNIT")
        for index in range(2):
            result = self.service.submit_recovery_observation(self.context(f"pass-{index}", result.aggregate_version), "episode-2", "plan-2", RecoveryObservation(f"pass-{index}", "episode-2", f"sample-{index}", now - timedelta(minutes=1), now - timedelta(minutes=1), now - timedelta(minutes=1), "W0_CONTEXT", "W0_CHARACTERISTIC", "W0_UNIT", Severity.NORMAL, ObservationOutcome.NORMAL, 0.95, "none"), evaluated_at=now, evidence_refs=(f"pass-ref-{index}",))
        self.assertEqual(result.state["cycles"][0]["recovery_plans"]["plan-2"]["state"], "PASS")
        self.assertEqual(result.state["engineering_work_state"], "OPEN")

    def test_closure_rules_and_reopen_preserve_prior_cycle(self):
        with self.assertRaises(InvalidTransitionError):
            self.service.close_episode(self.context("close-confirmed-without-pass", 1), "episode-1", disposition=ClosureDisposition.CONFIRMED_ISSUE)
        with self.assertRaises(ValidationFailureError):
            self.service.close_episode(self.context("close-exception-invalid", 1), "episode-1", disposition=ClosureDisposition.EXCEPTION, exception_review_id="review-1", residual_risk_owner="owner-1")
        with self.assertRaises(ValidationFailureError):
            self.service.close_episode(self.context("close-unresolved-invalid", 1), "episode-1", disposition=ClosureDisposition.UNRESOLVED)
        result = self.service.close_episode(self.context("close-benign", 1), "episode-1", disposition=ClosureDisposition.BENIGN, evidence_refs=("benign-evidence",))
        self.store.close()
        reopened_store = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(reopened_store.close)
        reopened_service = DecisionLoopCommandService(reopened_store, self.authorization)
        result = reopened_service.reopen_episode(self.context("reopen", result.aggregate_version), "episode-1", reason="new evidence")
        snapshot = reopened_service.get_decision_loop(self.principal, self.scope, "episode-1")
        self.assertNotEqual(snapshot.active_cycle_id, "cycle-1")
        self.assertEqual(snapshot.state["cycles"][0]["status"], "CLOSED")
        self.assertEqual(snapshot.state["cycles"][0]["closures"][0]["disposition"], "BENIGN")
        self.assertEqual(snapshot.state["cycles"][1]["status"], "OPEN")
        self.assertEqual(snapshot.state["reopen_history"][0]["from_cycle_id"], "cycle-1")

    def test_atomic_failure_injection_rolls_back_o5_mutation(self):
        self.store.connection.execute("CREATE TRIGGER fail_o5_audit BEFORE INSERT ON audit_event BEGIN SELECT RAISE(ABORT, 'test audit fault'); END")
        with self.assertRaises(StorageFailureError):
            self.check(1)
        self.assertEqual(self.store.count_rows(), {"aggregate_state": 1, "command_receipt": 1, "audit_event": 1, "outbox_event": 1})
        self.store.connection.execute("DROP TRIGGER fail_o5_audit")
        self.store.connection.execute("CREATE TRIGGER fail_o5_outbox BEFORE INSERT ON outbox_event BEGIN SELECT RAISE(ABORT, 'test outbox fault'); END")
        with self.assertRaises(StorageFailureError):
            self.check(1, "outbox-fault")
        self.assertEqual(self.store.count_rows(), {"aggregate_state": 1, "command_receipt": 1, "audit_event": 1, "outbox_event": 1})


if __name__ == "__main__":
    unittest.main()
