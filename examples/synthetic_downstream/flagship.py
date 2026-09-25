"""Typed synthetic CD metrology investigation fixture for U2.1 qualification.

All IDs, timestamps, values, qualifications and relationships in this module
are fictional demonstration facts. They are not production data, limits or
scientific policy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from ephi.application import (
    COMPARABLE_PROFILE_SCHEMA,
    EPISODE_REVISION_CUTOFF,
    INVESTIGATION_PROFILE_SCHEMA,
    RCA_SCHEMA_IDENTITY,
    CapabilityFact,
    CapabilityState,
    CohortEligibility,
    CohortRole,
    ComparableCaseProfile,
    CurationState,
    DeadlineState,
    DecisionDeadlineFact,
    EligibilityState,
    EvidenceDependenceGroup,
    ExactStructuredFingerprint,
    FingerprintFeature,
    HistoricalSourceIdentity,
    InvestigationChange,
    InvestigationEvidenceGroup,
    InvestigationHypothesis,
    InvestigationProfile,
    InvestigationTarget,
    PairWeight,
    PlannerPolicy,
    PlannerReadFacts,
    QualificationFact,
    QualificationState,
    RcaCohort,
    RcaDataset,
    RcaEvidenceFact,
    RcaExclusion,
    RcaTemporalFact,
    TargetContext,
    TemporalFactKind,
    TurnaroundFact,
    TurnaroundState,
    UnknownFact,
    UnresolvedHypothesisPair,
    RevisionVector,
)


SYNTHETIC_DISCLAIMER = (
    "Synthetic demonstration only: every identifier, measurement, timestamp, "
    "qualification, policy and relationship is fictional."
)
FAMILY_ID = "synthetic-cd-metrology-family"
CONTEXT_ID = "synthetic-recipe-r47"
TARGET_ID = "CD-SEM 12"
CHARACTERISTIC_ID = "Mean CD"
UNIT_ID = "nm"
PAIR_ID = "head-drift-v-process-shift"
POLICY_IDENTITY = "synthetic-cd-investigation-policy-v1"
SYNTHETIC_EVENT_START = datetime(2025, 1, 15, 14, 32, tzinfo=timezone.utc)
SYNTHETIC_EVENT_END = datetime(2025, 1, 15, 14, 50, tzinfo=timezone.utc)
SYNTHETIC_SOURCE_AVAILABLE = datetime(2025, 1, 15, 15, 0, tzinfo=timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def comparable_profile(
    source: HistoricalSourceIdentity,
    *,
    feature_values: dict[str, str],
    case_identity: str,
    limitations: tuple[str, ...] = (),
    context_identity: str = CONTEXT_ID,
) -> dict[str, object]:
    """Build one bounded exact-fingerprint profile with no raw values."""

    typed = ComparableCaseProfile(
        FAMILY_ID,
        context_identity,
        source,
        ExactStructuredFingerprint(
            "exact-structured.v1",
            tuple(FingerprintFeature(name, _digest(value)) for name, value in feature_values.items()),
        ),
        EligibilityState.QUALIFIED,
        f"synthetic-eligibility-{case_identity}",
        f"synthetic-qualification-evidence-{case_identity}",
        CurationState.CURATED,
        f"synthetic-curation-{case_identity}",
        limitations,
        (),
    )
    return {
        "schema": COMPARABLE_PROFILE_SCHEMA,
        "family_identity": typed.family_identity,
        "context_identity": typed.context_identity,
        "source_identity": typed.source_identity.as_dict(),
        "fingerprint": typed.fingerprint.as_dict(),
        "eligibility_state": typed.eligibility_state.value,
        "eligibility_identity": typed.eligibility_identity,
        "qualification_evidence_identity": typed.qualification_evidence_identity,
        "curation_state": typed.curation_state.value,
        "curation_evidence_identity": typed.curation_evidence_identity,
        "data_completeness_limitations": list(typed.data_completeness_limitations),
        "claims": [],
    }


def flagship_investigation_payload(
    *,
    episode_id: str,
    revision_vector: RevisionVector,
    cycle_id: str,
    source: HistoricalSourceIdentity,
    comparables: dict[str, object],
    policy_identity: str = POLICY_IDENTITY,
    valid_controls: bool = True,
) -> dict[str, object]:
    """Return the versioned analytical profile as a JSON-compatible payload."""

    cutoff = datetime.now(timezone.utc)
    target = InvestigationTarget(FAMILY_ID, "asset", TARGET_ID, TARGET_ID, CONTEXT_ID, CHARACTERISTIC_ID, UNIT_ID)
    pair = UnresolvedHypothesisPair(PAIR_ID, "measurement-head-drift", "process-material-shift", (), ("measurement-head-trajectory",))
    target_context = TargetContext(TARGET_ID, CONTEXT_ID, UNIT_ID, CHARACTERISTIC_ID)
    planner_facts = PlannerReadFacts(
        episode_id,
        cycle_id,
        revision_vector.workflow_version,
        revision_vector,
        cutoff,
        FAMILY_ID,
        "asset",
        target_context,
        (pair,),
        (),
        (EvidenceDependenceGroup("qualified-peer-path", "peer-reference-independent", (PAIR_ID,)),),
        (
            CapabilityFact(
                "synthetic.qualified.peer-reference", CapabilityState.AVAILABLE,
                source.snapshot_id, cutoff - timedelta(minutes=5),
                "synthetic-qualified-peer-path", CONTEXT_ID,
            ),
            CapabilityFact(
                "synthetic.stale.peer-reference", CapabilityState.STALE,
                source.snapshot_id, cutoff - timedelta(hours=2),
                "synthetic-stale-peer-path", CONTEXT_ID,
            ),
        ),
        (
            QualificationFact("synthetic-qualified-peer-path", QualificationState.QUALIFIED, source.snapshot_id, None),
            QualificationFact("synthetic-stale-peer-path", QualificationState.QUALIFIED, source.snapshot_id, None),
            QualificationFact("synthetic-peer-check-qualified", QualificationState.QUALIFIED, source.snapshot_id, None),
            QualificationFact("synthetic-stale-check-qualified", QualificationState.QUALIFIED, source.snapshot_id, None),
        ),
        (),
        (),
        DecisionDeadlineFact(DeadlineState.UNKNOWN, source.snapshot_id, None, "No synthetic decision deadline is represented"),
        (
            TurnaroundFact("synthetic-qualified-peer-turnaround", TurnaroundState.SUPPORTED, 900, None),
            TurnaroundFact("synthetic-stale-peer-turnaround", TurnaroundState.UNSUPPORTED, None, None, "Stale path has no qualified turnaround"),
        ),
        (UnknownFact("exposure", source.snapshot_id, "No qualified synthetic WIP/exposure source is represented"),),
    )

    qualified = CohortEligibility.QUALIFIED if valid_controls else CohortEligibility.UNQUALIFIED
    control_reason = () if valid_controls else ("REFERENCE_PATH_STALE", "CONTROL_NOT_QUALIFIED")
    matching = ("recipe", "maintenance-regime", "characteristic", "unit")
    affected = RcaCohort(
        "affected-post-change", CohortRole.AFFECTED, CohortEligibility.QUALIFIED,
        CONTEXT_ID, CHARACTERISTIC_ID, UNIT_ID, SYNTHETIC_EVENT_START, SYNTHETIC_EVENT_END,
        source.snapshot_id, "synthetic-affected-window-qualified", matching, matching,
    )
    control = RcaCohort(
        "peer-control-window", CohortRole.CONTROL, qualified,
        CONTEXT_ID, CHARACTERISTIC_ID, UNIT_ID, SYNTHETIC_EVENT_START, SYNTHETIC_EVENT_END,
        source.snapshot_id, "synthetic-peer-control-qualified" if valid_controls else "synthetic-peer-control-stale",
        matching, matching if valid_controls else (),
        () if valid_controls else ("REFERENCE_REGIME_STALE",), control_reason,
    )
    evidence: list[RcaEvidenceFact] = []
    for index in range(3):
        evidence.append(RcaEvidenceFact(
            f"synthetic-affected-evidence-{index + 1}", affected.cohort_identity,
            f"synthetic-wafer-a-{index + 1}", f"synthetic-affected-run-{index + 1}", source.snapshot_id,
            SYNTHETIC_EVENT_START + timedelta(minutes=index * 4), SYNTHETIC_SOURCE_AVAILABLE,
            CONTEXT_ID, CHARACTERISTIC_ID, UNIT_ID, ("measurement-head-trajectory",),
        ))
    # Two displayed rows share one dependence identity and count as one sample.
    evidence.append(RcaEvidenceFact(
        "synthetic-affected-repeat-row", affected.cohort_identity, "synthetic-wafer-a-1",
        "synthetic-affected-run-1", source.snapshot_id, SYNTHETIC_EVENT_START + timedelta(minutes=1),
        SYNTHETIC_SOURCE_AVAILABLE, CONTEXT_ID, CHARACTERISTIC_ID, UNIT_ID,
        ("measurement-head-trajectory",),
    ))
    for index in range(3):
        evidence.append(RcaEvidenceFact(
            f"synthetic-control-evidence-{index + 1}", control.cohort_identity,
            f"synthetic-peer-wafer-{index + 1}", f"synthetic-peer-run-{index + 1}", source.snapshot_id,
            SYNTHETIC_EVENT_START + timedelta(minutes=index * 4), SYNTHETIC_SOURCE_AVAILABLE,
            CONTEXT_ID, CHARACTERISTIC_ID, UNIT_ID,
            ("measurement-head-trajectory",) if index == 0 else (),
        ))
    rca = RcaDataset(
        RCA_SCHEMA_IDENTITY, policy_identity, cutoff, SYNTHETIC_EVENT_START,
        affected.cohort_identity, (affected, control), tuple(evidence),
        exclusions=(RcaExclusion("synthetic-stale-reference-path", control.cohort_identity, "REFERENCE_QUALIFICATION_STALE"),),
        temporal_facts=(RcaTemporalFact(
            "synthetic-later-head-service", TemporalFactKind.CANDIDATE_CHANGE,
            SYNTHETIC_EVENT_START + timedelta(minutes=13), SYNTHETIC_SOURCE_AVAILABLE, source.snapshot_id,
        ),),
        limitation_codes=("SYNTHETIC_DEMONSTRATION", "NO_WIP_EXPOSURE_SOURCE", "OBSERVATIONAL_ONLY"),
    )
    profile = InvestigationProfile(
        INVESTIGATION_PROFILE_SCHEMA,
        target,
        InvestigationChange(
            "CD-SEM 12 · Recipe R47 · Mean CD shifted +3.8 nm starting 14:32",
            "Synthetic mean critical-dimension excursion on the current metrology context.",
            "+3.8 nm",
            SYNTHETIC_EVENT_START,
        ),
        (
            InvestigationHypothesis("measurement-head-drift", "Measurement-head / tool drift", "Head trajectory evidence may reflect a measurement path change."),
            InvestigationHypothesis("process-material-shift", "True process / material shift", "A process or material change remains unresolved at this cutoff."),
        ),
        source,
        cutoff,
        planner_facts,
        (
            InvestigationEvidenceGroup(
                "head-trajectory-group-a", "head-trajectory-run-a", "SUPPORTS_A",
                "Head trajectory changed", "Synthetic head trajectory departs from its earlier baseline.",
                source.snapshot_id, SYNTHETIC_EVENT_START + timedelta(minutes=4), SYNTHETIC_SOURCE_AVAILABLE,
                "QUALIFIED", ("synthetic-evidence-head-a",),
            ),
            InvestigationEvidenceGroup(
                "head-trajectory-group-a-repeat", "head-trajectory-run-a", "SUPPORTS_A",
                "Repeated head monitor row", "A correlated display row belongs to the same evidence group.",
                source.snapshot_id, SYNTHETIC_EVENT_START + timedelta(minutes=5), SYNTHETIC_SOURCE_AVAILABLE,
                "QUALIFIED", ("synthetic-evidence-head-a-repeat",), ("DEPENDENT_REPEAT",),
            ),
            InvestigationEvidenceGroup(
                "qualified-peer-reference", "peer-reference-independent", "CONTRADICTS_BOTH",
                "Qualified peer/reference path", "A separately qualified synthetic peer/reference is available.",
                source.snapshot_id, SYNTHETIC_EVENT_START + timedelta(minutes=6), SYNTHETIC_SOURCE_AVAILABLE,
                "QUALIFIED", ("synthetic-peer-path-qualification",),
            ),
            InvestigationEvidenceGroup(
                "stale-peer-reference", "stale-peer-reference", "NEUTRAL",
                "Stale reference path", "A superficially available reference path is stale and excluded.",
                source.snapshot_id, SYNTHETIC_EVENT_START + timedelta(minutes=7), SYNTHETIC_SOURCE_AVAILABLE,
                "STALE", (), ("REFERENCE_QUALIFICATION_STALE",),
            ),
        ),
        ComparableCaseProfile(
            FAMILY_ID, CONTEXT_ID, source,
            ExactStructuredFingerprint("exact-structured.v1", tuple(FingerprintFeature(key, _digest(value)) for key, value in {
                "recipe-regime": "R47-synthetic", "trajectory-shape": "head-shift-pattern", "maintenance-regime": "pre-service-window", "tool-class": "cd-sem-class-a",
            }.items())),
            EligibilityState.QUALIFIED, "synthetic-eligibility-current", "synthetic-current-qualification",
            CurationState.CURATED, "synthetic-current-curation", (), (),
        ),
        rca,
        ("SYNTHETIC_DEMONSTRATION", "NO_WIP_EXPOSURE_SOURCE", "NO_PRODUCTION_POLICY"),
    )
    payload = profile.as_dict()
    payload["knowledge_cutoff"] = EPISODE_REVISION_CUTOFF
    payload["planner_facts"]["as_of"] = EPISODE_REVISION_CUTOFF
    payload["rca"]["knowledge_cutoff"] = EPISODE_REVISION_CUTOFF
    return payload


def invalid_control_payload(**kwargs: object) -> dict[str, object]:
    """Separate synthetic panel fixture with stale, unqualified controls."""

    return flagship_investigation_payload(**kwargs, valid_controls=False)  # type: ignore[arg-type]


__all__ = [
    "CONTEXT_ID", "FAMILY_ID", "POLICY_IDENTITY", "SYNTHETIC_DISCLAIMER", "SYNTHETIC_EVENT_END",
    "SYNTHETIC_EVENT_START", "SYNTHETIC_SOURCE_AVAILABLE", "TARGET_ID", "comparable_profile",
    "flagship_investigation_payload", "invalid_control_payload",
]
