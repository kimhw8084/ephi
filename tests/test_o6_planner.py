"""CHG-171 O6.1 deterministic planner core regressions."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    ACKNOWLEDGE_EPISODE_CAPABILITY,
    CHECK_EXECUTE_CAPABILITY,
    CHECK_REQUEST_CAPABILITY,
    CLAIM_EPISODE_CAPABILITY,
    DECISION_LOOP_CREATE_CAPABILITY,
    DECISION_LOOP_READ_CAPABILITY,
    EPISODE_WORKFLOW_AGGREGATE_TYPE,
    AccessScope,
    CapabilityFact,
    CapabilityRequirement,
    CapabilityRole,
    CapabilityState,
    CheckExecutionMode,
    CheckTemplate,
    CheckTemplateCatalog,
    ContradictionFact,
    DecisionDeadlineFact,
    DeadlineState,
    DecisionLoopCommandService,
    DisruptionClass,
    EffortBand,
    EvidenceDependenceGroup,
    EvidenceValidityFact,
    EvidenceValidityState,
    ExclusionReason,
    MutableCurrentAuthorizationAuthority,
    NextCheckPlannerService,
    PairDiscrimination,
    PairWeight,
    PlannerPolicy,
    PlannerReadFacts,
    PrerequisiteFact,
    PrerequisiteRequirement,
    PrerequisiteState,
    Principal,
    QualificationFact,
    QualificationState,
    RevisionVector,
    TargetContext,
    TemplatePath,
    TurnaroundFact,
    TurnaroundState,
    UnknownFact,
    UnresolvedHypothesisPair,
    AuthorizationDeniedError,
    CoherentReadConflictError,
    CommandContext,
    EpisodeWorkflowCommandService,
    ValidationFailureError,
)
from ephi.infrastructure import SQLiteReferenceTransactionAdapter  # noqa: E402


class O6PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scope = AccessScope("o6-scope", site_id="site-test", family_id="family-test")
        self.principal = Principal(
            "planner-test-engineer",
            (
                ACKNOWLEDGE_EPISODE_CAPABILITY,
                CHECK_EXECUTE_CAPABILITY,
                CHECK_REQUEST_CAPABILITY,
                CLAIM_EPISODE_CAPABILITY,
                DECISION_LOOP_CREATE_CAPABILITY,
                DECISION_LOOP_READ_CAPABILITY,
                "ephi.check.approve",
            ),
            (self.scope,),
            1,
            1,
        )
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.store = SQLiteReferenceTransactionAdapter(Path(self.temp.name) / "o6.sqlite3")
        self.addCleanup(self.store.close)
        self.store.seed_aggregate(
            self.scope,
            EPISODE_WORKFLOW_AGGREGATE_TYPE,
            "episode-test",
            {"owner": None, "work_state": "OPEN"},
        )
        self.workflow = EpisodeWorkflowCommandService(self.store, self.authorization)
        self.decision_loop = DecisionLoopCommandService(self.store, self.authorization)
        self.initialized = self.decision_loop.initialize_decision_loop(
            self.context("init", 0), "episode-test"
        )
        self.planner = NextCheckPlannerService(self.decision_loop)
        self.now = datetime.now(timezone.utc)
        self.target = TargetContext("asset-test", "context-test", "mm", "characteristic-test")
        self.facts = self.facts_for(self.decision_loop.get_decision_loop(self.principal, self.scope, "episode-test"))
        self.policy = PlannerPolicy(
            "ordinal-policy-test",
            "v1",
            (PairWeight("pair-1", Decimal("0.7")), PairWeight("pair-2", Decimal("0.3"))),
        )

    def vector(self, expected):
        return RevisionVector("analysis-test", "exposure-test", "priority-test", expected, None, "qualification-manifest-test")

    def context(self, command_id, expected):
        return CommandContext(command_id, self.principal, self.scope, expected, self.vector(expected))

    def facts_for(self, snapshot, *, target=None, as_of=None, pairless=False):
        if snapshot.revision_vector is None:
            raise AssertionError("the initialized O5 aggregate must carry a revision vector")
        as_of = as_of or datetime.now(timezone.utc)
        pairs = () if pairless else (
            UnresolvedHypothesisPair("pair-1", "hypothesis-a", "hypothesis-b", ("contradiction-1",), ("dependence-1",)),
            UnresolvedHypothesisPair("pair-2", "hypothesis-a", "hypothesis-c", (), ("dependence-2",)),
        )
        contradictions = () if pairless else (
            ContradictionFact("contradiction-1", "pair-1", "evidence-left", "evidence-right"),
        )
        groups = () if pairless else (
            EvidenceDependenceGroup("evidence-group-1", "dependence-1", ("pair-1",)),
            EvidenceDependenceGroup("evidence-group-2", "dependence-2", ("pair-2",)),
        )
        capabilities = (
            CapabilityFact("measurement.read", CapabilityState.AVAILABLE, "cap-source-1", as_of - timedelta(minutes=2), "qualification-cap-1", "context-test", as_of + timedelta(days=10)),
        )
        qualifications = (
            QualificationFact("qualification-cap-1", QualificationState.QUALIFIED, "qualification-source-1", as_of + timedelta(days=10)),
            QualificationFact("qualification-template-1", QualificationState.QUALIFIED, "qualification-source-2", as_of + timedelta(days=10)),
        )
        return PlannerReadFacts(
            snapshot.episode_id,
            snapshot.active_cycle_id,
            snapshot.aggregate_version,
            snapshot.revision_vector,
            as_of,
            "family-test",
            "asset",
            target or self.target,
            pairs,
            contradictions,
            groups,
            capabilities,
            qualifications,
            (),
            (),
            DecisionDeadlineFact(DeadlineState.UNKNOWN, "deadline-source-test", None, "no supported decision deadline"),
            (TurnaroundFact("turnaround-source-test", TurnaroundState.SUPPORTED, 600, as_of + timedelta(days=10)),),
            (),
        )

    def template(
        self,
        template_id="check-a",
        *,
        pairs=(PairDiscrimination("pair-1", Decimal("1")), PairDiscrimination("pair-2", Decimal("0.5"))),
        evidence_groups=("evidence-group-1",),
        redundancy_groups=(),
        redundant_with=(),
        capability="measurement.read",
        role=CapabilityRole.GENERAL,
        effort=EffortBand.MODERATE,
        disruption=DisruptionClass.MODERATE,
        approval="ephi.check.approve",
        turnaround_source="turnaround-source-test",
        mode=CheckExecutionMode.READ_EXISTING,
        prerequisites=(),
        path=TemplatePath.DISCRIMINATION,
        context_independent=False,
        unit_independent=False,
        reuse_window_seconds=3600,
        qualification="qualification-template-1",
    ):
        required = () if capability is None else (CapabilityRequirement(capability, 7200, role),)
        return CheckTemplate(
            template_id,
            "v1",
            f"Curated {template_id}",
            ("family-test",),
            ("asset",),
            ("context-test",),
            ("mm",),
            context_independent,
            unit_independent,
            required,
            pairs,
            tuple(prerequisites),
            tuple(redundant_with),
            tuple(redundancy_groups),
            tuple(evidence_groups),
            effort,
            "effort-source-test",
            turnaround_source,
            disruption,
            approval,
            mode,
            "result-schema-test-v1",
            "interpretation-schema-test-v1",
            ("traceable-evidence", "qualified-reference"),
            qualification,
            path,
            reuse_window_seconds,
        )

    def catalog(self, *templates):
        return CheckTemplateCatalog("curated-catalog-test", "v1", tuple(templates))

    def plan(self, *, facts=None, catalog=None, policy=None, expected=None, viewed=None):
        facts = facts or self.facts
        expected = facts.workflow_version if expected is None else expected
        viewed = facts.viewed_revisions if viewed is None else viewed
        return self.planner.plan(
            self.principal,
            self.scope,
            "episode-test",
            expected_workflow_version=expected,
            viewed_revisions=viewed,
            facts=facts,
            policy=policy or self.policy,
            catalog=catalog or self.catalog(self.template()),
        )

    def current_facts(self, *, as_of=None, target=None):
        snapshot = self.decision_loop.get_decision_loop(self.principal, self.scope, "episode-test")
        return self.facts_for(snapshot, target=target, as_of=as_of)

    def add_check(self, check_id, *, template_id="check-a", mode=CheckExecutionMode.READ_EXISTING, outcome=None, cancel=False, target=None):
        snapshot = self.decision_loop.get_decision_loop(self.principal, self.scope, "episode-test")
        target = target or self.target
        requested = self.decision_loop.request_check(
            self.context(f"request-{check_id}", snapshot.aggregate_version),
            "episode-test",
            check_id,
            template_id=template_id,
            template_version="v1",
            execution_mode=mode,
            target_context=target.as_dict(),
        )
        started = self.decision_loop.start_check(
            self.context(f"start-{check_id}", requested.aggregate_version), "episode-test", check_id
        )
        if cancel:
            return self.decision_loop.cancel_check(
                self.context(f"cancel-{check_id}", started.aggregate_version), "episode-test", check_id
            )
        if outcome is not None:
            refs = (f"evidence-{check_id}",) if outcome in {"SUPPORTS_A", "SUPPORTS_B"} else ()
            return self.decision_loop.complete_check(
                self.context(f"complete-{check_id}", started.aggregate_version),
                "episode-test",
                check_id,
                outcome=outcome,
                evidence_refs=refs,
            )
        return started

    def test_identical_canonical_inputs_repeat_identity_order_reasons_and_ordinal_traces(self):
        catalog = self.catalog(self.template("check-b"), self.template("check-a"))
        first = self.plan(catalog=catalog)
        second = self.plan(catalog=catalog)
        self.assertEqual(first.plan_identity, second.plan_identity)
        self.assertEqual(first.input_identity, second.input_identity)
        self.assertEqual([item.template_id for item in first.recommendations], [item.template_id for item in second.recommendations])
        self.assertEqual([item.as_dict() for item in first.excluded_checks], [item.as_dict() for item in second.excluded_checks])
        self.assertEqual([item.template_id for item in first.recommendations], ["check-a"])
        self.assertEqual(first.excluded_checks[0].template_id, "check-b")
        self.assertEqual(first.excluded_checks[0].reasons, (ExclusionReason.REDUNDANT_WITH_SELECTED,))
        self.assertEqual(first.recommendations[0].policy_identity, first.policy_identity)
        self.assertEqual(first.recommendations[0].template_catalog_identity, catalog.identity)
        self.assertEqual(first.ranking_kind, "DETERMINISTIC_ORDINAL_ONLY")
        self.assertIn("not probability", first.interpretation_limit)
        self.assertIn("D_ordinal_discrimination", first.recommendations[0].score.as_dict())
        self.assertNotIn("probability", first.recommendations[0].score.as_dict())
        self.assertEqual(first.recommendations[0].alternatives_discriminated[0]["ordinal_discrimination"], Decimal("1"))

    def test_stale_workflow_and_viewed_revision_fail_closed(self):
        old_facts = self.facts
        self.add_check("durable-change")
        with self.assertRaises(CoherentReadConflictError):
            self.plan(facts=old_facts)
        current = self.current_facts()
        forged = RevisionVector("changed-analysis", current.viewed_revisions.exposure_revision, current.viewed_revisions.priority_revision, current.workflow_version, current.viewed_revisions.plan_version, current.viewed_revisions.qualification_manifest_id)
        forged_facts = replace(current, viewed_revisions=forged)
        with self.assertRaises(CoherentReadConflictError):
            self.plan(facts=forged_facts, expected=forged.workflow_version, viewed=forged)

    def test_scope_and_current_authorization_revocation_block_disclosure(self):
        revoked = Principal(self.principal.subject, (CHECK_REQUEST_CAPABILITY,), (self.scope,), 2, 2)
        self.authorization.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.planner.plan(
                revoked,
                self.scope,
                "episode-test",
                expected_workflow_version=self.facts.workflow_version,
                viewed_revisions=self.facts.viewed_revisions,
                facts=self.facts,
                policy=self.policy,
                catalog=self.catalog(self.template()),
            )
        self.authorization.set_principal(self.principal)
        outside_scope = AccessScope("o6-other-scope", family_id="family-test")
        with self.assertRaises(AuthorizationDeniedError):
            self.planner.plan(
                self.principal,
                outside_scope,
                "episode-test",
                expected_workflow_version=self.facts.workflow_version,
                viewed_revisions=self.facts.viewed_revisions,
                facts=self.facts,
                policy=self.policy,
                catalog=self.catalog(self.template()),
            )

    def test_missing_unavailable_stale_and_unqualified_capabilities_have_stable_reasons(self):
        templates = (
            self.template("missing-cap", capability="cap.missing"),
            self.template("unavailable-cap", capability="cap.unavailable"),
            self.template("stale-cap", capability="cap.stale"),
            self.template("unqualified-peer", capability="cap.peer", role=CapabilityRole.PEER),
        )
        base = self.facts
        cap_facts = base.capability_facts + (
            CapabilityFact("cap.unavailable", CapabilityState.UNAVAILABLE, "cap-source-u", None, None),
            CapabilityFact("cap.stale", CapabilityState.STALE, "cap-source-s", self.now - timedelta(days=3), "qualification-cap-1"),
            CapabilityFact("cap.peer", CapabilityState.UNQUALIFIED, "peer-source", None, None, "context-test"),
        )
        facts = replace(base, capability_facts=cap_facts)
        first = self.plan(facts=facts, catalog=self.catalog(*templates))
        second = self.plan(facts=facts, catalog=self.catalog(*templates))
        reasons = {item.template_id: item.reasons for item in first.excluded_checks}
        self.assertEqual(reasons, {item.template_id: item.reasons for item in second.excluded_checks})
        self.assertIn(ExclusionReason.MISSING_CAPABILITY, reasons["missing-cap"])
        self.assertIn(ExclusionReason.CAPABILITY_UNAVAILABLE, reasons["unavailable-cap"])
        self.assertIn(ExclusionReason.STALE_EVIDENCE, reasons["stale-cap"])
        self.assertIn(ExclusionReason.UNQUALIFIED_PEER_REFERENCE, reasons["unqualified-peer"])
        self.assertFalse(first.recommendations)

    def test_missing_approval_and_required_input_exclude_check(self):
        prereq_template = self.template(
            "needs-input",
            prerequisites=(PrerequisiteRequirement("reference-sample", max_age_seconds=7200),),
            approval="approval.missing",
        )
        plan = self.plan(catalog=self.catalog(prereq_template))
        reasons = plan.excluded_checks[0].reasons
        self.assertIn(ExclusionReason.MISSING_PREREQUISITE, reasons)
        self.assertIn(ExclusionReason.MISSING_APPROVAL_AUTHORIZATION, reasons)
        satisfied = PrerequisiteFact(
            "reference-sample",
            PrerequisiteState.SATISFIED,
            "sample-source",
            self.now - timedelta(minutes=1),
            self.target.target_identity,
            self.target.context_identity,
            self.now + timedelta(hours=1),
        )
        with_input = replace(self.facts, prerequisite_facts=(satisfied,))
        eligible = self.plan(facts=with_input, catalog=self.catalog(self.template("with-input", prerequisites=(PrerequisiteRequirement("reference-sample", 7200),), approval=None)))
        self.assertEqual(eligible.recommendations[0].template_id, "with-input")
        self.assertTrue(any(item.get("prerequisite_id") == "reference-sample" for item in eligible.recommendations[0].capability_and_prerequisite_facts))

    def test_valid_same_context_completion_is_suppressed_then_expiry_or_wrong_context_reenables(self):
        completed = self.add_check("prior", outcome="SUPPORTS_A")
        check = completed.state["decision_loop"]["cycles"][0]["checks"]["prior"]
        completed_at = datetime.fromisoformat(check["completed_at"].replace("Z", "+00:00"))
        snapshot = self.decision_loop.get_decision_loop(self.principal, self.scope, "episode-test")
        validity = EvidenceValidityFact("prior", check["cycle_id"], EvidenceValidityState.VALID, completed_at + timedelta(minutes=30), self.target)
        current = self.facts_for(snapshot, as_of=completed_at + timedelta(minutes=1))
        reusable = replace(current, evidence_validity_facts=(validity,))
        suppressed = self.plan(facts=reusable)
        self.assertIn(ExclusionReason.VALID_PRIOR_COMPLETION, suppressed.excluded_checks[0].reasons)
        expired = replace(self.facts_for(snapshot, as_of=completed_at + timedelta(hours=2)), evidence_validity_facts=(validity,))
        after_expiry = self.plan(facts=expired)
        self.assertEqual([item.template_id for item in after_expiry.recommendations], ["check-a"], after_expiry.as_dict())
        wrong_target = TargetContext("asset-other", "context-test", "mm", "characteristic-test")
        other_context = self.facts_for(snapshot, target=wrong_target, as_of=completed_at + timedelta(minutes=1))
        other_context = replace(other_context, evidence_validity_facts=(validity,))
        self.assertEqual(self.plan(facts=other_context).recommendations[0].template_id, "check-a")

    def test_failed_unavailable_unknown_and_cancelled_checks_are_not_reusable_evidence(self):
        for index, outcome in enumerate(("FAILED", "UNAVAILABLE", "UNKNOWN")):
            self.add_check(f"non-evidence-{index}", outcome=outcome)
        self.add_check("cancelled-check", cancel=True)
        current = self.current_facts(as_of=datetime.now(timezone.utc) + timedelta(seconds=1))
        plan = self.plan(facts=current)
        self.assertEqual(plan.recommendations[0].template_id, "check-a")
        self.assertNotIn(ExclusionReason.VALID_PRIOR_COMPLETION, plan.excluded_checks[0].reasons if plan.excluded_checks else ())

    def test_unknown_effort_and_turnaround_are_conservative_and_explicit(self):
        template = self.template("unknown-cost", effort=EffortBand.UNKNOWN, disruption=DisruptionClass.UNKNOWN, turnaround_source="unknown-turnaround")
        facts = replace(
            self.facts,
            turnaround_facts=(),
            explicit_unknowns=(UnknownFact("turnaround:unknown-turnaround", "turnaround-source-audit", "no supported estimate"),),
        )
        plan = self.plan(facts=facts, catalog=self.catalog(template))
        recommendation = plan.recommendations[0]
        self.assertEqual(recommendation.score.effort_disruption, Decimal("1"))
        self.assertGreater(recommendation.score.feasibility, Decimal("0"))
        self.assertEqual(recommendation.effort_turnaround_disruption["effort_cost_band"], Decimal("1"))
        self.assertEqual(recommendation.effort_turnaround_disruption["turnaround"]["seconds"], None)
        self.assertIn("never treated as zero time", recommendation.effort_turnaround_disruption["turnaround"]["ranking_treatment"])

    def test_infeasible_after_deadline_is_excluded_before_ranking(self):
        deadline = self.now + timedelta(minutes=5)
        supported_deadline = DecisionDeadlineFact(DeadlineState.SUPPORTED, "deadline-source", deadline)
        fast = self.template("timely", turnaround_source="turn-fast", effort=EffortBand.HIGH, disruption=DisruptionClass.HIGH)
        slow = self.template("late", turnaround_source="turn-slow", effort=EffortBand.LOW, disruption=DisruptionClass.NONE)
        facts = replace(
            self.facts,
            as_of=self.now,
            decision_deadline=supported_deadline,
            turnaround_facts=(
                TurnaroundFact("turn-fast", TurnaroundState.SUPPORTED, 60, deadline),
                TurnaroundFact("turn-slow", TurnaroundState.SUPPORTED, 600, deadline),
            ),
        )
        plan = self.plan(facts=facts, catalog=self.catalog(slow, fast))
        self.assertEqual([item.template_id for item in plan.recommendations], ["timely"])
        self.assertIn(ExclusionReason.MISSED_DECISION_WINDOW, plan.excluded_checks[0].reasons)

    def test_redundancy_and_dependent_evidence_do_not_create_independent_confirmations(self):
        first = self.template("variant-a", redundancy_groups=("same-metric",), evidence_groups=("evidence-group-1",))
        second = self.template("variant-b", redundancy_groups=("same-metric",), evidence_groups=("evidence-group-1",))
        independent = self.template("independent", pairs=(PairDiscrimination("pair-2", Decimal("1")),), evidence_groups=("evidence-group-2",))
        plan = self.plan(catalog=self.catalog(second, independent, first))
        selected = [item.template_id for item in plan.recommendations]
        self.assertEqual(len(selected), 2)
        self.assertFalse({"variant-a", "variant-b"}.issubset(selected))
        excluded = {item.template_id: item.reasons for item in plan.excluded_checks}
        self.assertTrue(any(ExclusionReason.REDUNDANT_WITH_SELECTED in reasons for reasons in excluded.values()))
        self.assertEqual(plan.recommendations[0].score.independent_coverage, Decimal("0.5"))

    def test_ties_use_template_id_and_no_pair_uses_only_validation_path(self):
        tied = self.catalog(self.template("z-check", evidence_groups=()), self.template("a-check", evidence_groups=()))
        self.assertEqual([item.template_id for item in self.plan(catalog=tied).recommendations], ["a-check", "z-check"])
        pairless = self.facts_for(self.decision_loop.get_decision_loop(self.principal, self.scope, "episode-test"), pairless=True)
        invalid_path = self.template("invalid", pairs=(), path=TemplatePath.DISCRIMINATION)
        validation = self.template("validation", pairs=(), path=TemplatePath.PREREQUISITE_VALIDATION, evidence_groups=())
        plan = self.plan(facts=pairless, catalog=self.catalog(invalid_path, validation))
        self.assertEqual([item.template_id for item in plan.recommendations], ["validation"])
        self.assertIn(ExclusionReason.NO_VALIDATION_PATH, plan.excluded_checks[0].reasons)
        self.assertEqual(plan.recommendations[0].score.discrimination, Decimal("0"))

    def test_durable_state_change_changes_identity_and_started_check_is_never_mutated(self):
        before = self.plan()
        started = self.add_check("human-started", template_id="human-started", mode=CheckExecutionMode.REQUEST_HUMAN_MEASUREMENT)
        external = self.add_check("external-started", template_id="external-started", mode=CheckExecutionMode.REQUEST_APPROVED_EXTERNAL_WORK)
        snapshot = self.decision_loop.get_decision_loop(self.principal, self.scope, "episode-test")
        before_state = snapshot.state
        facts = self.facts_for(snapshot)
        plan = self.plan(
            facts=facts,
            catalog=self.catalog(
                self.template("human-started", mode=CheckExecutionMode.REQUEST_HUMAN_MEASUREMENT),
                self.template("external-started", mode=CheckExecutionMode.REQUEST_APPROVED_EXTERNAL_WORK),
            ),
        )
        after = self.decision_loop.get_decision_loop(self.principal, self.scope, "episode-test")
        stored = after.check_state["human-started"]
        self.assertEqual(stored["status"], "STARTED")
        self.assertEqual(stored["execution_mode"], CheckExecutionMode.REQUEST_HUMAN_MEASUREMENT.value)
        self.assertEqual(before_state, after.state)
        self.assertNotEqual(before.plan_identity, plan.plan_identity)
        exclusions = {item.template_id: item.reasons for item in plan.excluded_checks}
        self.assertEqual(exclusions["human-started"], (ExclusionReason.CHECK_IN_PROGRESS,))
        self.assertEqual(exclusions["external-started"], (ExclusionReason.CHECK_IN_PROGRESS,))
        self.assertEqual(started.state["decision_loop"]["cycles"][0]["checks"]["human-started"]["status"], "STARTED")
        self.assertEqual(external.state["decision_loop"]["cycles"][0]["checks"]["external-started"]["status"], "STARTED")

    def test_approval_revocation_blocks_or_excludes_and_outputs_never_claim_probability(self):
        no_approval = self.template("approval-check", approval="ephi.check.approve.revoked")
        plan = self.plan(catalog=self.catalog(no_approval))
        self.assertIn(ExclusionReason.MISSING_APPROVAL_AUTHORIZATION, plan.excluded_checks[0].reasons)
        ok = self.plan()
        payload = ok.as_dict()
        self.assertEqual(payload["ranking_kind"], "DETERMINISTIC_ORDINAL_ONLY")
        self.assertNotIn("posterior_probability", str(payload))
        self.assertNotIn("causal_probability", str(payload))

    def test_policy_template_or_source_revision_change_changes_identity(self):
        original = self.plan()
        changed_policy = PlannerPolicy(
            "ordinal-policy-test",
            "v2",
            (PairWeight("pair-1", Decimal("0.6")), PairWeight("pair-2", Decimal("0.4"))),
        )
        self.assertNotEqual(original.plan_identity, self.plan(policy=changed_policy).plan_identity)
        changed_catalog = self.catalog(replace(self.template(), title="Changed curated title"))
        self.assertNotEqual(original.plan_identity, self.plan(catalog=changed_catalog).plan_identity)
        changed_capability = replace(
            self.facts,
            capability_facts=(replace(self.facts.capability_facts[0], source_identity="cap-source-revised"),),
        )
        self.assertNotEqual(original.plan_identity, self.plan(facts=changed_capability).plan_identity)
        revised = RevisionVector("analysis-revised", "exposure-test", "priority-test", self.facts.workflow_version, None, "qualification-manifest-test")
        changed_facts = replace(self.facts, viewed_revisions=revised)
        with self.assertRaises(CoherentReadConflictError):
            self.plan(facts=changed_facts, expected=revised.workflow_version, viewed=revised)

    def test_unsupported_context_and_units_are_excluded_and_unknowns_are_typed(self):
        bad_context = replace(self.facts, target_context=TargetContext("asset-test", "other-context", "other-unit", "characteristic-test"))
        plan = self.plan(facts=bad_context)
        self.assertIn(ExclusionReason.UNSUPPORTED_CONTEXT, plan.excluded_checks[0].reasons)
        self.assertIn(ExclusionReason.UNSUPPORTED_UNIT, plan.excluded_checks[0].reasons)
        with self.assertRaises(ValidationFailureError):
            replace(self.facts, target_context=TargetContext("asset-test", None, None, "characteristic-test"), explicit_unknowns=())


if __name__ == "__main__":
    unittest.main()
