"""CHG-189 O6.3 bounded observational RCA contract regressions."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (
    AccessScope,
    ComponentState,
    ControlQuality,
    CohortEligibility,
    CohortRole,
    EPISODE_REVISION_CUTOFF,
    EPISODE_READ_CAPABILITY,
    EpisodeBrief,
    EpisodeInvestigationQueryService,
    InvestigationProfile,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RCA_READ_CAPABILITY,
    RCA_SCHEMA_IDENTITY,
    RcaAnalysisService,
    RcaCohort,
    RcaCurrentFacts,
    RcaDataset,
    RcaEvidenceFact,
    RcaQuery,
    RcaState,
    RcaTemporalFact,
    RevisionVector,
    TemporalFactKind,
    ValidationFailureError,
    CoherentReadConflictError,
    canonical_json,
)
from ephi.application.comparable_history import (
    COMPARABLE_PROFILE_KEY,
    COMPARABLE_PROFILE_SCHEMA,
    ComparableCaseProfile,
    CurationState,
    EligibilityState,
    ExactStructuredFingerprint,
    FingerprintFeature,
    HistoricalSourceIdentity,
)
from ephi.application.planner import (
    CapabilityFact,
    CapabilityState,
    CheckTemplateCatalog,
    DeadlineState,
    DecisionDeadlineFact,
    EvidenceDependenceGroup,
    PlannerReadFacts,
    PlannerPolicy,
    PairWeight,
    QualificationFact,
    QualificationState,
    TargetContext,
    UnknownFact,
    UnresolvedHypothesisPair,
)


UTC = timezone.utc
NOW = datetime(2026, 4, 2, 15, 0, tzinfo=UTC)
SCOPE = AccessScope("synthetic-rca-scope", site_id="synthetic-site", family_id="synthetic-family")
SOURCE_IDENTITY = "synthetic-source-snapshot-v1"


def _cohort(identity, role, *, eligibility=CohortEligibility.QUALIFIED, mismatches=(), reasons=(), source=SOURCE_IDENTITY):
    return RcaCohort(
        identity,
        role,
        eligibility,
        "synthetic-recipe-r47",
        "mean-cd",
        "nm",
        NOW - timedelta(minutes=28),
        NOW,
        source,
        f"qualification-{identity}",
        ("context", "recipe", "maintenance_regime"),
        ("context", "recipe", "maintenance_regime"),
        tuple(mismatches),
        tuple(reasons),
    )


def _dataset(*, control_eligibility=CohortEligibility.QUALIFIED, controls=3, affected=3, future=False):
    cohorts = (_cohort("affected", CohortRole.AFFECTED), _cohort("peer-controls", CohortRole.CONTROL, eligibility=control_eligibility, reasons=("REFERENCE_STALE",) if control_eligibility != CohortEligibility.QUALIFIED else ()))
    facts = []
    for index in range(affected):
        facts.append(RcaEvidenceFact(
            f"a-evidence-{index}", "affected", f"wafer-a-{index}", f"affected-run-{index}", SOURCE_IDENTITY,
            NOW - timedelta(minutes=20-index), NOW - timedelta(minutes=15), "synthetic-recipe-r47", "mean-cd", "nm",
            ("measurement-head-trajectory",),
        ))
    # Multiple displayed measurements from a dependent sample share one
    # dependence identity and therefore remain one independent group.
    facts.append(RcaEvidenceFact(
        "a-evidence-repeat", "affected", "wafer-a-0", "affected-run-0", SOURCE_IDENTITY,
        NOW - timedelta(minutes=18), NOW - timedelta(minutes=14), "synthetic-recipe-r47", "mean-cd", "nm",
        ("measurement-head-trajectory",),
    ))
    for index in range(controls):
        facts.append(RcaEvidenceFact(
            f"c-evidence-{index}", "peer-controls", f"wafer-c-{index}", f"control-run-{index}", SOURCE_IDENTITY,
            NOW - timedelta(minutes=20-index), NOW - timedelta(minutes=15), "synthetic-recipe-r47", "mean-cd", "nm",
            ("measurement-head-trajectory",) if index == 0 else (),
        ))
    if future:
        facts.append(RcaEvidenceFact(
            "future-evidence", "peer-controls", "wafer-c-future", "control-run-future", "future-private-source",
            NOW - timedelta(minutes=5), NOW + timedelta(seconds=1), "synthetic-recipe-r47", "mean-cd", "nm",
            ("future-factor",),
        ))
    temporal = (
        RcaTemporalFact("head-change-after-onset", TemporalFactKind.CANDIDATE_CHANGE, NOW - timedelta(minutes=20), NOW - timedelta(minutes=19), SOURCE_IDENTITY),
        RcaTemporalFact("future-history-claim", TemporalFactKind.HISTORICAL_CLAIM, NOW - timedelta(days=1), NOW - timedelta(days=1), SOURCE_IDENTITY, NOW + timedelta(days=1)),
    )
    return RcaDataset(
        RCA_SCHEMA_IDENTITY,
        "synthetic-observational-policy-v1",
        NOW,
        NOW - timedelta(minutes=28),
        "affected",
        cohorts,
        tuple(facts),
        temporal_facts=temporal,
        limitation_codes=("SYNTHETIC_DEMONSTRATION", "NO_WIP_EXPOSURE_SOURCE"),
    )


def _operation(dataset=None):
    scope = SCOPE
    principal = Principal("synthetic-engineer", (RCA_READ_CAPABILITY,), (scope,), 1, 1)
    authority = MutableCurrentAuthorizationAuthority(principal)
    facts = dataset or _dataset()
    query = RcaQuery(
        scope,
        "episode-synthetic-cd-sem",
        "analysis-revision-1",
        9,
        "cycle-1",
        NOW,
        (SOURCE_IDENTITY,),
        facts.policy_identity,
        facts.schema_identity,
    )
    current = RcaCurrentFacts(
        query.episode_identity,
        query.analytical_revision_identity,
        query.workflow_version,
        query.active_cycle_identity,
        query.knowledge_cutoff,
        query.source_identities,
        query.policy_identity,
        query.schema_identity,
        facts,
    )
    return principal, authority, query, current


def _cycle_profile(cycle_id="cycle-current"):
    from examples.synthetic_downstream.flagship import comparable_profile, flagship_investigation_payload
    from ephi.application.investigation import investigation_policy_identity

    policy = PlannerPolicy("cycle-test-policy", "v1", (PairWeight("pair-cycle-test", Decimal("1")),))
    catalog = CheckTemplateCatalog("cycle-test-catalog", "v1", ())
    policy_id = investigation_policy_identity(policy, catalog)
    vector = RevisionVector("analysis-cycle-test", None, None, 9, None, "qualification-cycle-test")
    source = HistoricalSourceIdentity("source-cycle-test", "revision-cycle-test", "a" * 64, "b" * 64)
    payload = flagship_investigation_payload(
        episode_id="episode-cycle-test", revision_vector=vector, cycle_id=cycle_id, source=source,
        comparables=comparable_profile(
            source, feature_values={"recipe": "R47"}, case_identity="cycle-test",
        ),
        policy_identity=policy_id,
    )
    profile = InvestigationProfile.from_payload(payload, revision_known_at=NOW)
    return profile, policy, catalog


def _cycle_service(profile, policy, catalog, active_cycle_id, current_workflow_version, calls):
    from ephi.application.investigation import INVESTIGATION_PROFILE_KEY, EpisodeInvestigationQueryService

    scope = AccessScope(
        "cycle-test-scope", site_id="synthetic-site", family_id=profile.target.family_identity,
    )
    current_vector = replace(profile.planner_facts.viewed_revisions, workflow_version=current_workflow_version)
    comparable = profile.as_dict()[COMPARABLE_PROFILE_KEY]
    brief = EpisodeBrief(
        profile.planner_facts.episode_id, "revision-cycle-test", NOW, NOW,
        {INVESTIGATION_PROFILE_KEY: profile.as_dict(), COMPARABLE_PROFILE_KEY: comparable},
        {"work_state": "OPEN"}, current_vector, {"source": "READY"},
    )
    workflow = SimpleNamespace(
        aggregate_version=current_workflow_version,
        scope_key=scope.canonical_key,
        revision_vector=current_vector,
        active_cycle_id=active_cycle_id,
        state={"decision_loop": {"checks": [{"check_id": "check-cycle-test", "state": "REQUESTED"}]}},
    )

    class Briefs:
        def get_episode_brief(self, principal, scope, episode_id):
            return brief

    class Workflows:
        def get_decision_loop(self, principal, scope, episode_id):
            return workflow

    class Planner:
        def plan(self, *args, **kwargs):
            calls["planner"] = kwargs["facts"]
            calls["expected_workflow_version"] = kwargs["expected_workflow_version"]
            calls["planner_current_check_states"] = tuple(
                item["state"] for item in workflow.state["decision_loop"]["checks"]
            )
            return SimpleNamespace(plan_identity="plan-cycle-test")

    class History:
        def retrieve(self, principal, query, *, page_size):
            calls["history"] = query
            return SimpleNamespace(state=SimpleNamespace(value="READY"), result_identity="history-cycle-test")

    class Rca:
        def analyze(self, principal, query, *, load_current_facts):
            calls["rca_query"] = query
            calls["rca_facts"] = load_current_facts()
            return SimpleNamespace(state=RcaState.READY, result_identity="rca-cycle-test", reason_codes=())

    principal = Principal(
        "cycle-test-reader", (EPISODE_READ_CAPABILITY,), (scope,), 1, 1,
    )
    authorization = MutableCurrentAuthorizationAuthority(principal)
    service = EpisodeInvestigationQueryService(
        Briefs(), Workflows(), Planner(), authorization, policy, catalog,
        Rca(), History(),
    )
    return service, principal, brief, workflow, scope


class RcaContractTests(unittest.TestCase):
    def test_identical_inputs_have_restart_stable_query_and_result_identities(self):
        first = _operation()
        result_a = RcaAnalysisService(first[1]).analyze(first[0], first[2], load_current_facts=lambda: first[3])
        # A newly constructed application service represents a process restart.
        second = _operation()
        result_b = RcaAnalysisService(second[1]).analyze(second[0], second[2], load_current_facts=lambda: second[3])
        self.assertEqual(result_a.query_identity, result_b.query_identity)
        self.assertEqual(result_a.input_identity, result_b.input_identity)
        self.assertEqual(result_a.result_identity, result_b.result_identity)

    def test_authorization_precedes_candidate_profile_lookup(self):
        principal, authority, query, current = _operation()
        denied = Principal("intruder", (), (SCOPE,), 1, 1)
        touched = []
        with self.assertRaises(Exception):
            RcaAnalysisService(authority).analyze(denied, query, load_current_facts=lambda: touched.append(True) or current)
        self.assertEqual(touched, [])

    def test_revision_workflow_cycle_cutoff_source_policy_and_schema_mismatches_fail_closed(self):
        principal, authority, query, current = _operation()
        for changed in (
            replace(current, analytical_revision_identity="analysis-new"),
            replace(current, workflow_version=10),
            replace(current, active_cycle_identity="cycle-new"),
            replace(current, knowledge_cutoff=NOW - timedelta(seconds=1)),
            replace(current, source_identities=("other-source",)),
            replace(current, policy_identity="other-policy"),
            replace(current, schema_identity="other-schema"),
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(CoherentReadConflictError):
                    RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda changed=changed: changed)

    def test_future_source_facts_are_excluded_and_reported_without_factor_disclosure(self):
        principal, authority, query, current = _operation(_dataset(future=True))
        result = RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda: current)
        self.assertEqual(result.state, RcaState.READY)
        self.assertEqual(result.control_independent_group_count, 3)
        self.assertEqual([row.reason_code for row in result.excluded_evidence], ["EVIDENCE_AFTER_CUTOFF"])
        self.assertIn("EVIDENCE_EXCLUDED_AFTER_CUTOFF", result.limitation_codes)
        self.assertNotIn("future-factor", canonical_json(result.as_dict()))
        self.assertNotIn("future-private-source", canonical_json(result.as_dict()))

    def test_future_event_cannot_contribute_even_under_adversarial_typed_input(self):
        principal, authority, query, current = _operation()
        future_event = RcaEvidenceFact(
            "future-event-evidence", "peer-controls", "future-sample", "future-run", SOURCE_IDENTITY,
            NOW + timedelta(seconds=1), NOW + timedelta(seconds=2),
            "synthetic-recipe-r47", "mean-cd", "nm", ("future-event-factor",),
        )
        dataset = replace(current.dataset, evidence=(*current.dataset.evidence, future_event))
        current = replace(current, dataset=dataset)
        # Bypass the dataclass constructor to model a corrupted in-memory typed
        # object. The analyzer must still apply the exact query cutoff.
        adversarial = tuple(fact for fact in dataset.evidence if fact.dependence_identity == "affected-run-0")
        self.assertEqual(len(adversarial), 2)
        for fact in adversarial:
            object.__setattr__(fact, "event_at", NOW + timedelta(seconds=1))
            object.__setattr__(fact, "factor_identities", ("adversarial-future-factor",))
        result = RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda: current)
        self.assertEqual(result.affected_independent_group_count, 2)
        self.assertEqual(result.control_independent_group_count, 3)
        self.assertIn(
            (adversarial[0].evidence_identity, "EVIDENCE_EVENT_AFTER_CUTOFF"),
            {(item.identity, item.reason_code) for item in result.excluded_evidence},
        )
        self.assertIn(
            (future_event.evidence_identity, "EVIDENCE_EVENT_AFTER_CUTOFF"),
            {(item.identity, item.reason_code) for item in result.excluded_evidence},
        )
        self.assertNotIn("adversarial-future-factor", canonical_json(result.as_dict()))
        self.assertNotIn("future-event-factor", canonical_json(result.as_dict()))

    def test_dependent_repeated_measurements_do_not_inflate_independent_counts(self):
        principal, authority, query, current = _operation()
        result = RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda: current)
        self.assertEqual(result.affected_independent_group_count, 3)
        self.assertEqual(result.control_independent_group_count, 3)
        commonality = result.associations[0]
        self.assertEqual(commonality.affected.as_dict(), {"numerator": 3, "denominator": 3, "independent_group_count": 3})
        self.assertEqual(commonality.controls.as_dict(), {"numerator": 1, "denominator": 3, "independent_group_count": 3})
        self.assertEqual((commonality.rate_difference_numerator, commonality.rate_difference_denominator), (6, 9))

    def test_invalid_unqualified_or_insufficient_controls_suppress_commonality(self):
        for state, expected in (
            (CohortEligibility.INVALID, RcaState.INVALID_CONTROLS),
            (CohortEligibility.UNQUALIFIED, RcaState.UNQUALIFIED_CONTROLS),
            (CohortEligibility.STALE, RcaState.UNQUALIFIED_CONTROLS),
        ):
            principal, authority, query, current = _operation(_dataset(control_eligibility=state))
            result = RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda: current)
            self.assertEqual(result.state, expected)
            self.assertEqual(result.associations, ())
            self.assertNotEqual(result.control_quality, ControlQuality.QUALIFIED)
        principal, authority, query, current = _operation(_dataset(controls=1))
        result = RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda: current)
        self.assertEqual(result.state, RcaState.INSUFFICIENT_CONTROLS)
        self.assertEqual(result.associations, ())

    def test_matching_mismatch_suppresses_commonality(self):
        dataset = _dataset()
        cohorts = list(dataset.cohorts)
        cohorts[1] = replace(cohorts[1], mismatches=("MAINTENANCE_REGIME_MISMATCH",))
        dataset = replace(dataset, cohorts=tuple(cohorts))
        principal, authority, query, current = _operation(dataset)
        result = RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda: current)
        self.assertEqual(result.state, RcaState.UNQUALIFIED_CONTROLS)
        self.assertIn("MAINTENANCE_REGIME_MISMATCH", result.mismatches)
        self.assertEqual(result.associations, ())

    def test_temporal_contradictions_are_deterministic_descriptive_limitations(self):
        principal, authority, query, current = _operation()
        result = RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda: current)
        self.assertEqual(
            [(item.fact_identity, item.reason_code) for item in result.temporal_contradictions],
            [
                ("future-history-claim", "HISTORICAL_CLAIM_MATURITY_AFTER_CUTOFF"),
                ("head-change-after-onset", "CANDIDATE_CHANGE_AFTER_OBSERVED_ONSET"),
            ],
        )

    def test_unavailable_temporal_fact_is_filtered_without_identity_or_content_disclosure(self):
        base = _dataset()
        dataset = replace(base, temporal_facts=(
            *base.temporal_facts,
            RcaTemporalFact(
                "future-private-temporal-identity", TemporalFactKind.CANDIDATE_CHANGE,
                NOW + timedelta(seconds=1), NOW + timedelta(seconds=2), "future-private-source",
            ),
        ))
        principal, authority, query, current = _operation(dataset)
        # The future temporal source is intentionally outside the as-known source
        # inventory and must not cause a source-mismatch disclosure either.
        result = RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda: current)
        serialized = canonical_json(result.as_dict())
        self.assertNotIn("future-private-temporal-identity", serialized)
        self.assertNotIn("future-private-source", serialized)
        self.assertIn("TEMPORAL_FACTS_EXCLUDED_AFTER_CUTOFF", result.limitation_codes)
        self.assertNotIn("FACT_NOT_AVAILABLE_AT_CUTOFF", {item.reason_code for item in result.temporal_contradictions})

    def test_timestamp_physical_consistency_and_cutoff_boundaries_fail_closed(self):
        with self.assertRaises(ValidationFailureError):
            RcaEvidenceFact(
                "reversed-evidence", "affected", "sample", "run", SOURCE_IDENTITY,
                NOW, NOW - timedelta(seconds=1), "synthetic-recipe-r47", "mean-cd", "nm", (),
            )
        with self.assertRaises(ValidationFailureError):
            RcaTemporalFact(
                "reversed-temporal", TemporalFactKind.CANDIDATE_CHANGE,
                NOW, NOW - timedelta(seconds=1), SOURCE_IDENTITY,
            )
        with self.assertRaises(ValidationFailureError):
            replace(_dataset(), onset_at=NOW + timedelta(seconds=1))
        base = _dataset()
        for future_cohort in (
            replace(base.cohorts[1], interval_end=NOW + timedelta(seconds=1)),
            replace(base.cohorts[1], interval_start=NOW + timedelta(seconds=1), interval_end=NOW + timedelta(seconds=2)),
        ):
            with self.subTest(future_cohort=future_cohort):
                with self.assertRaises(ValidationFailureError):
                    replace(base, cohorts=(base.cohorts[0], future_cohort))

    def test_large_synchronous_population_requests_materialization_without_truncation(self):
        base = _dataset(controls=0)
        affected = base.cohorts[0]
        controls = _cohort("large-controls", CohortRole.CONTROL)
        # Count only bounded identities; no sample payload is silently dropped.
        evidence = tuple(RcaEvidenceFact(
            f"large-{i}", "affected" if i < 250 else "large-controls", f"sample-{i}", f"group-{i}", SOURCE_IDENTITY,
            NOW - timedelta(minutes=1), NOW - timedelta(seconds=1), "synthetic-recipe-r47", "mean-cd", "nm", ("factor-a",),
        ) for i in range(501))
        large = replace(base, cohorts=(affected, controls), evidence=evidence, temporal_facts=())
        principal, authority, query, current = _operation(large)
        result = RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda: current)
        self.assertEqual(result.state, RcaState.MATERIALIZATION_REQUIRED)
        self.assertIsNone(result.affected_independent_group_count)
        self.assertEqual(result.associations, ())
        self.assertIn("SYNCHRONOUS_BOUND_EXCEEDED", result.reason_codes)

    def test_public_result_emits_no_probability_or_causal_confidence_field(self):
        principal, authority, query, current = _operation()
        payload = RcaAnalysisService(authority).analyze(principal, query, load_current_facts=lambda: current).as_dict()
        forbidden = {"probability", "posterior", "most_likely_cause", "causal_confidence", "root_cause_confidence"}
        self.assertFalse(forbidden & set(payload))
        self.assertFalse(any(forbidden & set(row) for row in payload.get("associations", ())))
        self.assertEqual(payload["interpretation"], "observational; does not establish cause")


class InvestigationProfileContractTests(unittest.TestCase):
    def test_investigation_observations_and_onset_must_be_at_or_before_profile_cutoff(self):
        profile, _policy, _catalog = _cycle_profile()
        group = profile.evidence_groups[0]
        with self.assertRaises(ValidationFailureError):
            replace(group, event_at=NOW, available_at=NOW - timedelta(seconds=1))
        with self.assertRaises(ValidationFailureError):
            replace(
                profile,
                evidence_groups=(replace(group, event_at=NOW + timedelta(seconds=1), available_at=NOW + timedelta(seconds=2)), *profile.evidence_groups[1:]),
            )
        with self.assertRaises(ValidationFailureError):
            replace(
                profile,
                evidence_groups=(replace(group, available_at=NOW + timedelta(seconds=1)), *profile.evidence_groups[1:]),
            )
        with self.assertRaises(ValidationFailureError):
            replace(profile, change=replace(profile.change, onset_at=NOW + timedelta(seconds=1)))

    def test_immutable_profile_rejects_future_rca_evidence_and_unavailable_temporal_facts(self):
        profile, _policy, _catalog = _cycle_profile()
        future_evidence = RcaEvidenceFact(
            "future-profile-evidence", profile.rca_dataset.affected_cohort_identity,
            "future-profile-sample", "future-profile-run", "source-cycle-test",
            NOW + timedelta(seconds=1), NOW + timedelta(seconds=2),
            profile.target.context_identity, profile.target.characteristic_identity,
            profile.target.unit_identity, ("future-factor",),
        )
        future_temporal = RcaTemporalFact(
            "future-profile-temporal", TemporalFactKind.CANDIDATE_CHANGE,
            NOW + timedelta(seconds=1), NOW + timedelta(seconds=2), "source-cycle-test",
        )
        with self.assertRaises(ValidationFailureError):
            replace(
                profile,
                rca_dataset=replace(profile.rca_dataset, evidence=(*profile.rca_dataset.evidence, future_evidence)),
            )
        with self.assertRaises(ValidationFailureError):
            replace(
                profile,
                rca_dataset=replace(profile.rca_dataset, temporal_facts=(*profile.rca_dataset.temporal_facts, future_temporal)),
            )

    def test_same_active_cycle_accepts_newer_o5_version_without_rebinding_profile_cycle(self):
        profile, policy, catalog = _cycle_profile("cycle-current")
        calls = {}
        service, principal, _brief, workflow, scope = _cycle_service(
            profile, policy, catalog, "cycle-current", 10, calls,
        )
        view = service.get_episode_investigation(principal, scope, profile.planner_facts.episode_id)
        self.assertEqual(workflow.state["decision_loop"]["checks"][0]["state"], "REQUESTED")
        self.assertEqual(view.profile.state, ComponentState.READY)
        self.assertEqual(calls["planner"].cycle_id, "cycle-current")
        self.assertEqual(calls["planner"].workflow_version, 10)
        self.assertEqual(calls["planner"].viewed_revisions.workflow_version, 10)
        self.assertEqual(calls["expected_workflow_version"], 10)
        self.assertEqual(calls["planner_current_check_states"], ("REQUESTED",))
        self.assertEqual(calls["history"].cycle_id, "cycle-current")
        self.assertEqual(calls["history"].workflow_version, 10)
        self.assertEqual(calls["rca_query"].active_cycle_identity, "cycle-current")
        self.assertEqual(calls["rca_facts"].active_cycle_identity, "cycle-current")

    def test_reopened_cycle_marks_profile_stale_and_does_not_run_planner_history_or_rca(self):
        profile, policy, catalog = _cycle_profile("cycle-before-reopen")
        calls = {}
        service, principal, _brief, workflow, scope = _cycle_service(
            profile, policy, catalog, "cycle-after-reopen", 10, calls,
        )
        view = service.get_episode_investigation(principal, scope, profile.planner_facts.episode_id)
        self.assertEqual(view.active_cycle_id, "cycle-after-reopen")
        for component in (view.profile, view.planner, view.comparable_history, view.rca):
            self.assertEqual(component.state, ComponentState.STALE)
            self.assertEqual(component.reason_codes, ("EXACT_VIEW_CYCLE_MISMATCH",))
        self.assertEqual(calls, {})
        self.assertNotIn("measurement-head-drift", canonical_json(view.as_dict()))

        query = RcaQuery(
            scope, profile.planner_facts.episode_id, view.revision_id, 10,
            "cycle-before-reopen", profile.knowledge_cutoff, ("source-cycle-test",),
            profile.rca_dataset.policy_identity, profile.rca_dataset.schema_identity,
        )
        with self.assertRaises(CoherentReadConflictError):
            service.load_current_rca_facts(principal, query)
        self.assertEqual(calls, {})

    def test_future_profile_temporal_identity_is_not_returned_when_profile_fails_validation(self):
        from ephi.application.investigation import INVESTIGATION_PROFILE_KEY

        profile, policy, catalog = _cycle_profile()
        calls = {}
        service, principal, brief, _workflow, scope = _cycle_service(
            profile, policy, catalog, "cycle-current", 9, calls,
        )
        raw_profile = profile.as_dict()
        raw_profile["rca"]["temporal_facts"].append(RcaTemporalFact(
            "unavailable-profile-temporal-id", TemporalFactKind.CANDIDATE_CHANGE,
            NOW + timedelta(seconds=1), NOW + timedelta(seconds=2), "unavailable-profile-source",
        ).as_dict())
        brief.analytical[INVESTIGATION_PROFILE_KEY] = raw_profile
        view = service.get_episode_investigation(principal, scope, profile.planner_facts.episode_id)
        serialized = canonical_json(view.as_dict())
        self.assertEqual(view.profile.state, ComponentState.FAILED)
        self.assertNotIn("unavailable-profile-temporal-id", serialized)
        self.assertNotIn("unavailable-profile-source", serialized)
        self.assertNotIn("planner", calls)

    def test_legacy_episode_without_richer_profile_returns_truthful_unavailable_components(self):
        from types import SimpleNamespace
        from ephi.application import ComponentState, EpisodeBrief

        vector = RevisionVector("analysis-legacy", None, None, 4, None, "qualification-legacy")
        brief = EpisodeBrief(
            "episode-legacy", "revision-legacy", NOW, NOW, {"title": "Legacy issue"},
            {"work_state": "OPEN"}, vector, {"source": "READY"},
        )

        class BriefAuthority:
            def get_episode_brief(self, principal, scope, episode_id):
                self.last_read = (principal, scope, episode_id)
                return brief

        class WorkflowAuthority:
            def get_decision_loop(self, principal, scope, episode_id):
                return SimpleNamespace(
                    aggregate_version=4, scope_key=scope.canonical_key, revision_vector=vector,
                    active_cycle_id="legacy-cycle", state={}, workflow_state={}, check_state={},
                    action_state={}, recovery_state={}, closure_state={},
                )

        principal = Principal("legacy-reader", (), (SCOPE,), 1, 1)
        authorization = MutableCurrentAuthorizationAuthority(principal)
        service = EpisodeInvestigationQueryService(
            BriefAuthority(), WorkflowAuthority(), object(), authorization, object(), object(),
            RcaAnalysisService(authorization),
        )
        result = service.get_episode_investigation(principal, SCOPE, brief.episode_id)
        self.assertEqual(result.workflow.state, ComponentState.READY)
        self.assertEqual(result.profile.state, ComponentState.UNAVAILABLE)
        self.assertEqual(result.planner.state, ComponentState.UNAVAILABLE)
        self.assertEqual(result.comparable_history.state, ComponentState.UNAVAILABLE)
        self.assertEqual(result.rca.state, ComponentState.UNAVAILABLE)
        self.assertIn("NO_INVESTIGATION_PROFILE", result.profile.reason_codes)

    def test_malformed_versioned_investigation_profile_fails_closed(self):
        with self.assertRaises(ValidationFailureError):
            InvestigationProfile.from_payload({"schema": "ephi.investigation-profile.v1", "unchecked": {"arbitrary": "payload"}})

    def test_synthetic_invalid_control_fixture_suppresses_observational_summary(self):
        from examples.synthetic_downstream.flagship import invalid_control_payload

        vector = RevisionVector("synthetic-analysis-v1", None, None, 9, None, "synthetic-qualification-v1")
        source = HistoricalSourceIdentity("synthetic-source", "synthetic-revision", "a" * 64, "b" * 64)
        comparables = {
            "schema": COMPARABLE_PROFILE_SCHEMA,
            "family_identity": "synthetic-cd-metrology-family",
            "context_identity": "synthetic-recipe-r47",
            "source_identity": source.as_dict(),
            "fingerprint": {"version": "exact-structured.v1", "features": []},
            "eligibility_state": "QUALIFIED", "eligibility_identity": "synthetic-eligible",
            "qualification_evidence_identity": "synthetic-qualification",
            "curation_state": "CURATED", "curation_evidence_identity": "synthetic-curation",
            "data_completeness_limitations": [], "claims": [],
        }
        payload = invalid_control_payload(
            episode_id="synthetic-invalid-controls", revision_vector=vector,
            cycle_id="synthetic-cycle", source=source, comparables=comparables,
        )
        profile = InvestigationProfile.from_payload(payload, revision_known_at=NOW)
        query = RcaQuery(
            SCOPE, "synthetic-invalid-controls", "synthetic-revision-id", 9, "synthetic-cycle",
            NOW, (source.snapshot_id,), profile.rca_dataset.policy_identity, profile.rca_dataset.schema_identity,
        )
        current = RcaCurrentFacts(
            query.episode_identity, query.analytical_revision_identity, query.workflow_version,
            query.active_cycle_identity, query.knowledge_cutoff, query.source_identities,
            query.policy_identity, query.schema_identity, profile.rca_dataset,
        )
        principal = Principal("synthetic-engineer", (RCA_READ_CAPABILITY,), (SCOPE,), 1, 1)
        result = RcaAnalysisService(MutableCurrentAuthorizationAuthority(principal)).analyze(
            principal, query, load_current_facts=lambda: current,
        )
        self.assertEqual(result.state, RcaState.UNQUALIFIED_CONTROLS)
        self.assertEqual(result.control_quality, ControlQuality.UNQUALIFIED)
        self.assertEqual(result.associations, ())
        self.assertIn("CONTROL_NOT_QUALIFIED", result.reason_codes)

    def test_profile_is_versioned_typed_and_resolves_revision_cutoff_reference(self):
        from ephi.application.investigation import (
            EPISODE_REVISION_CUTOFF,
            INVESTIGATION_PROFILE_SCHEMA,
            InvestigationChange,
            InvestigationEvidenceGroup,
            InvestigationHypothesis,
            InvestigationTarget,
        )
        cutoff = NOW
        vector = RevisionVector("analysis-1", None, None, 9, None, "qualification-1")
        facts = PlannerReadFacts(
            "episode-synthetic-cd-sem", "cycle-1", 9, vector, cutoff, "synthetic-family", "asset",
            TargetContext("CD-SEM-12", "R47", "nm", "Mean-CD"),
            (UnresolvedHypothesisPair("pair-head-vs-process", "measurement-head-drift", "process-material-shift"),),
            (), (EvidenceDependenceGroup("dependence-head", "head-run", ("pair-head-vs-process",)),),
            (CapabilityFact("qualified-peer-remeasure", CapabilityState.AVAILABLE, "source-current", cutoff, "peer-qualified", "R47", cutoff + timedelta(days=1)),),
            (QualificationFact("peer-qualified", QualificationState.QUALIFIED, "source-policy", cutoff + timedelta(days=1)),),
            (), (), DecisionDeadlineFact(DeadlineState.UNKNOWN, "deadline-source", None, "not supplied"), (),
            (UnknownFact("wip-exposure", "source-availability", "no qualified exposure source"),),
        )
        source = HistoricalSourceIdentity("source-current", "source-revision-1", "a" * 64, "b" * 64)
        fingerprint = ExactStructuredFingerprint("exact-structured.v1", (FingerprintFeature("recipe", "c" * 64),))
        comparable = {
            "schema": COMPARABLE_PROFILE_SCHEMA,
            "family_identity": "synthetic-family",
            "context_identity": "R47",
            "source_identity": source.as_dict(),
            "fingerprint": fingerprint.as_dict(),
            "eligibility_state": "QUALIFIED",
            "eligibility_identity": "eligible-current",
            "qualification_evidence_identity": "qualified-current-evidence",
            "curation_state": "LIMITED",
            "curation_evidence_identity": None,
            "data_completeness_limitations": ["SYNTHETIC_CASE_SET"],
            "claims": [],
        }
        profile = InvestigationProfile(
            INVESTIGATION_PROFILE_SCHEMA,
            InvestigationTarget("synthetic-family", "asset", "CD-SEM-12", "CD-SEM 12", "R47", "Mean-CD", "nm"),
            InvestigationChange("CD-SEM 12 · Recipe R47 · Mean CD shifted +3.8 nm starting 14:32", "Synthetic mean CD excursion", "+3.8 nm", cutoff - timedelta(minutes=28)),
            (InvestigationHypothesis("measurement-head-drift", "Measurement-head/tool drift", "A measurement-system shift could explain the observed trajectory."),
             InvestigationHypothesis("process-material-shift", "True process/material shift", "A process or material change could explain the observed trajectory.")),
            source,
            cutoff,
            facts,
            (InvestigationEvidenceGroup("evidence-head", "head-run", "SUPPORTS_A", "Peer repeat", "Synthetic check fact", "source-current", cutoff, cutoff, "QUALIFIED", ("evidence-1",)),),
            ComparableCaseProfile(
                comparable["family_identity"], comparable["context_identity"], source, fingerprint,
                EligibilityState.QUALIFIED, "eligible-current", "qualified-current-evidence",
                CurationState.LIMITED, None, ("SYNTHETIC_CASE_SET",), (),
            ),
            _dataset(),
            ("SYNTHETIC_DEMONSTRATION", "EXPOSURE_UNAVAILABLE",),
        )
        payload = profile.as_dict()
        payload["knowledge_cutoff"] = EPISODE_REVISION_CUTOFF
        payload["planner_facts"]["as_of"] = EPISODE_REVISION_CUTOFF
        payload["rca"]["knowledge_cutoff"] = EPISODE_REVISION_CUTOFF
        parsed = InvestigationProfile.from_payload(payload, revision_known_at=cutoff)
        self.assertEqual(parsed.knowledge_cutoff, cutoff)
        self.assertEqual(parsed.planner_facts.viewed_revisions, vector)
        self.assertEqual(parsed.source_identity, source)
        self.assertEqual(parsed.identity, profile.identity)
        payload["unrestricted_extra"] = {"arbitrary": True}
        with self.assertRaises(ValidationFailureError):
            InvestigationProfile.from_payload(payload, revision_known_at=cutoff)


if __name__ == "__main__":
    unittest.main()
