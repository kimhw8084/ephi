"""Typed, versioned composition for one coherent Episode investigation view."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import hashlib
from typing import Any

from .comparable_history import (
    COMPARABLE_FINGERPRINT_VERSION,
    COMPARABLE_HISTORY_READ_CAPABILITY,
    COMPARABLE_PROFILE_KEY,
    COMPARABLE_PROFILE_SCHEMA,
    ComparableCaseHistoryQueryService,
    ComparableCaseProfile,
    ComparableCaseQuery,
    ComparableCasesPage,
    CurationState,
    EligibilityState,
    ExactStructuredFingerprint,
    FingerprintFeature,
    HistoricalClaim,
    HistoricalSourceIdentity,
)
from .context import AccessScope, CurrentAuthorizationAuthority, Principal, RevisionVector
from .decision_loop import DecisionLoopCommandService, DecisionLoopSnapshot
from .episodes import EPISODE_READ_CAPABILITY, EpisodeBrief, EpisodeBriefQueryService
from .errors import CoherentReadConflictError, ValidationFailureError
from .hashing import canonical_json
from .planner import (
    CapabilityFact,
    CapabilityState,
    CheckTemplateCatalog,
    ContradictionFact,
    DeadlineState,
    DecisionDeadlineFact,
    EvidenceDependenceGroup,
    EvidenceValidityFact,
    EvidenceValidityState,
    NextCheckPlannerService,
    PlannerPlan,
    PlannerPolicy,
    PlannerReadFacts,
    PrerequisiteFact,
    PrerequisiteState,
    QualificationFact,
    QualificationState,
    TargetContext,
    TurnaroundFact,
    TurnaroundState,
    UnknownFact,
    UnresolvedHypothesisPair,
)
from .rca import (
    RCA_SCHEMA_IDENTITY,
    RcaAnalysisService,
    RcaCurrentFacts,
    RcaDataset,
    RcaQuery,
    RcaResult,
    RcaState,
)
from .rca_materialization import RcaMaterializationCoordinator, RcaMaterializationState


INVESTIGATION_PROFILE_SCHEMA = "ephi.investigation-profile.v1"
INVESTIGATION_PROFILE_KEY = "investigation_profile"
EPISODE_REVISION_CUTOFF = "EPISODE_REVISION_KNOWN_AT"
MAX_INVESTIGATION_EVIDENCE_GROUPS = 200
MAX_INVESTIGATION_LIMITATIONS = 100
MAX_INVESTIGATION_COMPARABLE_CLAIMS = 100


def _identity(value: object, field: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or len(value) > maximum:
        raise ValidationFailureError(f"{field} must be a bounded non-empty identity")
    return value


def _text(value: object, field: str, *, maximum: int = 320) -> str:
    result = _identity(value, field, maximum=maximum)
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise ValidationFailureError(f"{field} contains unsupported control characters")
    return result


def _sequence(value: object, field: str, maximum: int) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)) or len(value) > maximum:
        raise ValidationFailureError(f"{field} must be a bounded sequence")
    return tuple(value)


def _timestamp(value: object, field: str) -> datetime:
    if isinstance(value, Mapping) and set(value) == {"$datetime"}:
        value = value["$datetime"]
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationFailureError(f"{field} must be timezone-aware") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailureError(f"{field} must be timezone-aware")
    return value


def _revision_vector(value: object) -> RevisionVector:
    if isinstance(value, RevisionVector):
        return value
    if not isinstance(value, Mapping):
        raise ValidationFailureError("planner viewed_revisions must be a revision vector")
    try:
        return RevisionVector(
            value["analysis_revision"], value.get("exposure_revision"), value.get("priority_revision"),
            value["workflow_version"], value.get("plan_version"), value["qualification_manifest_id"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationFailureError("planner viewed_revisions is malformed") from exc


def _target_context(value: object) -> TargetContext:
    if isinstance(value, TargetContext):
        return value
    if not isinstance(value, Mapping):
        raise ValidationFailureError("planner target_context must be typed")
    try:
        return TargetContext(value["target_identity"], value.get("context_identity"), value.get("unit_identity"), value.get("characteristic_identity"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationFailureError("planner target_context is malformed") from exc


def _planner_facts(value: object, *, revision_known_at: datetime | None = None) -> PlannerReadFacts:
    if isinstance(value, PlannerReadFacts):
        return value
    if not isinstance(value, Mapping):
        raise ValidationFailureError("investigation planner facts must be an object")
    try:
        pairs = tuple(UnresolvedHypothesisPair(
            item["pair_id"], item["hypothesis_a_id"], item["hypothesis_b_id"],
            tuple(item.get("contradiction_ids", ())), tuple(item.get("dependence_group_ids", ())),
        ) for item in _sequence(value["unresolved_pairs"], "unresolved_pairs", 1000))
        contradictions = tuple(ContradictionFact(
            item["contradiction_id"], item["pair_id"], item["left_evidence_identity"], item["right_evidence_identity"]
        ) for item in _sequence(value.get("contradictions", ()), "contradictions", 1000))
        groups = tuple(EvidenceDependenceGroup(
            item["evidence_group_id"], item["dependence_group_id"], tuple(item["pair_ids"])
        ) for item in _sequence(value.get("dependence_groups", ()), "dependence_groups", 1000))
        capabilities = tuple(CapabilityFact(
            item["capability_id"], CapabilityState(item["state"]), item["source_identity"],
            None if item.get("observed_at") is None else _timestamp(item["observed_at"], "capability observed_at"),
            item.get("qualification_identity"), item.get("context_identity"),
            None if item.get("valid_until") is None else _timestamp(item["valid_until"], "capability valid_until"),
        ) for item in _sequence(value.get("capability_facts", ()), "capability_facts", 1000))
        qualifications = tuple(QualificationFact(
            item["qualification_identity"], QualificationState(item["state"]), item["source_identity"],
            None if item.get("valid_until") is None else _timestamp(item["valid_until"], "qualification valid_until"),
        ) for item in _sequence(value.get("qualification_facts", ()), "qualification_facts", 1000))
        prerequisites = tuple(PrerequisiteFact(
            item["prerequisite_id"], PrerequisiteState(item["state"]), item["source_identity"],
            None if item.get("observed_at") is None else _timestamp(item["observed_at"], "prerequisite observed_at"),
            item.get("target_identity"), item.get("context_identity"),
            None if item.get("valid_until") is None else _timestamp(item["valid_until"], "prerequisite valid_until"),
        ) for item in _sequence(value.get("prerequisite_facts", ()), "prerequisite_facts", 1000))
        evidence_validity = tuple(EvidenceValidityFact(
            item["check_id"], item["cycle_id"], EvidenceValidityState(item["state"]),
            None if item.get("valid_until") is None else _timestamp(item["valid_until"], "evidence valid_until"),
            _target_context(item["target_context"]),
        ) for item in _sequence(value.get("evidence_validity_facts", ()), "evidence_validity_facts", 1000))
        deadline = value["decision_deadline"]
        turnaround = tuple(TurnaroundFact(
            item["source_identity"], TurnaroundState(item["state"]), item.get("seconds"),
            None if item.get("valid_until") is None else _timestamp(item["valid_until"], "turnaround valid_until"), item.get("unknown_reason"),
        ) for item in _sequence(value.get("turnaround_facts", ()), "turnaround_facts", 1000))
        unknowns = tuple(UnknownFact(item["fact_identity"], item["source_identity"], item["reason"])
                         for item in _sequence(value.get("explicit_unknowns", ()), "explicit_unknowns", 1000))
        raw_as_of = value["as_of"]
        if raw_as_of == EPISODE_REVISION_CUTOFF:
            if revision_known_at is None:
                raise ValidationFailureError("planner cutoff reference requires its immutable Episode revision")
            raw_as_of = revision_known_at
        return PlannerReadFacts(
            value["episode_id"], value["cycle_id"], value["workflow_version"], _revision_vector(value["viewed_revisions"]),
            _timestamp(raw_as_of, "planner as_of"), value["family_id"], value["target_kind"], _target_context(value["target_context"]),
            pairs, contradictions, groups, capabilities, qualifications, prerequisites, evidence_validity,
            DecisionDeadlineFact(DeadlineState(deadline["state"]), deadline["source_identity"],
                                 None if deadline.get("deadline") is None else _timestamp(deadline["deadline"], "decision deadline"),
                                 deadline.get("unknown_reason")),
            turnaround, unknowns,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValidationFailureError):
            raise
        raise ValidationFailureError("investigation planner facts are malformed") from exc


def _source_identity(value: object) -> HistoricalSourceIdentity:
    if isinstance(value, HistoricalSourceIdentity):
        return value
    if not isinstance(value, Mapping):
        raise ValidationFailureError("investigation source identity must be immutable and exact")
    try:
        return HistoricalSourceIdentity(value["snapshot_id"], value["source_revision"], value["manifest_hash"], value["artifact_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationFailureError("investigation source identity is malformed") from exc


def _comparable_profile(value: object) -> ComparableCaseProfile:
    if not isinstance(value, Mapping) or value.get("schema") != COMPARABLE_PROFILE_SCHEMA:
        raise ValidationFailureError("investigation profile requires the supported O6.2 comparable-case schema")
    try:
        source = _source_identity(value["source_identity"])
        fp = value["fingerprint"]
        if not isinstance(fp, Mapping) or fp.get("version") != COMPARABLE_FINGERPRINT_VERSION:
            raise TypeError
        features = tuple(FingerprintFeature(item["feature_id"], item["value_sha256"])
                         for item in _sequence(fp["features"], "comparable fingerprint features", 500))
        raw_claims = _sequence(value.get("claims", ()), "comparable claims", MAX_INVESTIGATION_COMPARABLE_CLAIMS)
        claims = tuple(HistoricalClaim.from_dict(item) for item in raw_claims)
        limitations = tuple(_identity(item, "comparable limitation", maximum=64) for item in _sequence(value.get("data_completeness_limitations", ()), "comparable limitations", 100))
        return ComparableCaseProfile(
            value["family_identity"], value["context_identity"], source,
            ExactStructuredFingerprint(fp["version"], features), EligibilityState(value["eligibility_state"]),
            value["eligibility_identity"], value.get("qualification_evidence_identity"), CurationState(value["curation_state"]),
            value.get("curation_evidence_identity"), limitations, claims,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValidationFailureError):
            raise
        raise ValidationFailureError("O6.2 comparable profile is malformed") from exc


@dataclass(frozen=True, slots=True)
class InvestigationTarget:
    family_identity: str
    target_kind: str
    target_identity: str
    asset_identity: str
    context_identity: str
    characteristic_identity: str
    unit_identity: str

    def __post_init__(self) -> None:
        for field in ("family_identity", "target_kind", "target_identity", "asset_identity", "context_identity", "characteristic_identity", "unit_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))

    def as_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class InvestigationChange:
    headline: str
    description: str
    magnitude: str | None
    onset_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headline", _text(self.headline, "headline", maximum=240))
        object.__setattr__(self, "description", _text(self.description, "change description", maximum=320))
        if self.magnitude is not None:
            object.__setattr__(self, "magnitude", _text(self.magnitude, "change magnitude", maximum=48))
        if self.onset_at is not None:
            object.__setattr__(self, "onset_at", _timestamp(self.onset_at, "change onset"))

    def as_dict(self) -> dict[str, object]:
        return {"headline": self.headline, "description": self.description, "magnitude": self.magnitude, "onset_at": self.onset_at}


@dataclass(frozen=True, slots=True)
class InvestigationHypothesis:
    hypothesis_identity: str
    title: str
    summary: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "hypothesis_identity", _identity(self.hypothesis_identity, "hypothesis_identity"))
        object.__setattr__(self, "title", _text(self.title, "hypothesis title", maximum=120))
        object.__setattr__(self, "summary", _text(self.summary, "hypothesis summary"))

    def as_dict(self) -> dict[str, str]:
        return {"hypothesis_identity": self.hypothesis_identity, "title": self.title, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class InvestigationEvidenceGroup:
    evidence_group_identity: str
    dependence_identity: str
    polarity: str
    title: str
    summary: str
    source_identity: str
    event_at: datetime
    available_at: datetime
    qualification_state: str
    evidence_references: tuple[str, ...]
    limitation_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("evidence_group_identity", "dependence_identity", "source_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        if self.polarity not in {"SUPPORTS_A", "SUPPORTS_B", "CONTRADICTS_BOTH", "NEUTRAL"}:
            raise ValidationFailureError("evidence polarity is unsupported")
        object.__setattr__(self, "title", _text(self.title, "evidence title", maximum=120))
        object.__setattr__(self, "summary", _text(self.summary, "evidence summary"))
        for field in ("event_at", "available_at"):
            object.__setattr__(self, field, _timestamp(getattr(self, field), field))
        if self.event_at > self.available_at:
            raise ValidationFailureError("investigation evidence event_at must not follow available_at")
        if self.qualification_state not in {"QUALIFIED", "UNQUALIFIED", "STALE", "UNKNOWN"}:
            raise ValidationFailureError("evidence qualification state is unsupported")
        object.__setattr__(self, "evidence_references", tuple(_identity(item, "evidence reference") for item in _sequence(self.evidence_references, "evidence references", 20)))
        object.__setattr__(self, "limitation_codes", tuple(_identity(item, "evidence limitation", maximum=64) for item in _sequence(self.limitation_codes, "evidence limitations", 20)))

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_group_identity": self.evidence_group_identity, "dependence_identity": self.dependence_identity,
            "polarity": self.polarity, "title": self.title, "summary": self.summary,
            "source_identity": self.source_identity, "event_at": self.event_at, "available_at": self.available_at,
            "qualification_state": self.qualification_state, "evidence_references": list(self.evidence_references),
            "limitation_codes": list(self.limitation_codes),
        }


@dataclass(frozen=True, slots=True)
class InvestigationProfile:
    schema: str
    target: InvestigationTarget
    change: InvestigationChange
    hypotheses: tuple[InvestigationHypothesis, ...]
    source_identity: HistoricalSourceIdentity
    knowledge_cutoff: datetime
    planner_facts: PlannerReadFacts
    evidence_groups: tuple[InvestigationEvidenceGroup, ...]
    comparable_profile: ComparableCaseProfile
    rca_dataset: RcaDataset
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema != INVESTIGATION_PROFILE_SCHEMA:
            raise ValidationFailureError("unsupported investigation profile version")
        if not isinstance(self.target, InvestigationTarget) or not isinstance(self.change, InvestigationChange):
            raise ValidationFailureError("investigation profile target and change must be typed")
        if not isinstance(self.hypotheses, (tuple, list)) or not self.hypotheses or len(self.hypotheses) > 40 or any(not isinstance(item, InvestigationHypothesis) for item in self.hypotheses):
            raise ValidationFailureError("investigation hypotheses must be a bounded non-empty typed sequence")
        hypotheses = tuple(sorted(self.hypotheses, key=lambda item: item.hypothesis_identity))
        if len({item.hypothesis_identity for item in hypotheses}) != len(hypotheses):
            raise ValidationFailureError("investigation hypothesis identities must be unique")
        object.__setattr__(self, "hypotheses", hypotheses)
        if not isinstance(self.source_identity, HistoricalSourceIdentity) or not isinstance(self.planner_facts, PlannerReadFacts):
            raise ValidationFailureError("investigation profile source and planner facts must be typed")
        if not isinstance(self.comparable_profile, ComparableCaseProfile) or not isinstance(self.rca_dataset, RcaDataset):
            raise ValidationFailureError("investigation profile history and RCA facts must be typed")
        object.__setattr__(self, "knowledge_cutoff", _timestamp(self.knowledge_cutoff, "knowledge_cutoff"))
        if not isinstance(self.evidence_groups, (tuple, list)) or len(self.evidence_groups) > MAX_INVESTIGATION_EVIDENCE_GROUPS or any(not isinstance(item, InvestigationEvidenceGroup) for item in self.evidence_groups):
            raise ValidationFailureError("investigation evidence groups exceed the profile bound")
        groups = tuple(sorted(self.evidence_groups, key=lambda item: (item.dependence_identity, item.evidence_group_identity)))
        if len({item.evidence_group_identity for item in groups}) != len(groups):
            raise ValidationFailureError("investigation evidence group identities must be unique")
        if self.change.onset_at is not None and self.change.onset_at > self.knowledge_cutoff:
            raise ValidationFailureError("investigation change onset must not be after the knowledge cutoff")
        if any(item.event_at > self.knowledge_cutoff or item.available_at > self.knowledge_cutoff for item in groups):
            raise ValidationFailureError("investigation evidence groups must be known by the profile cutoff")
        object.__setattr__(self, "evidence_groups", groups)
        object.__setattr__(self, "limitations", tuple(_identity(item, "investigation limitation", maximum=64) for item in _sequence(self.limitations, "investigation limitations", MAX_INVESTIGATION_LIMITATIONS)))
        if self.comparable_profile.source_identity != self.source_identity:
            raise ValidationFailureError("O6.2 source identity must match the investigation source identity")
        if self.planner_facts.family_id != self.target.family_identity or self.planner_facts.target_kind != self.target.target_kind:
            raise ValidationFailureError("planner family or target kind must match investigation target")
        planner_target = self.planner_facts.target_context
        if (planner_target.target_identity, planner_target.context_identity, planner_target.characteristic_identity, planner_target.unit_identity) != (
            self.target.target_identity, self.target.context_identity, self.target.characteristic_identity, self.target.unit_identity
        ):
            raise ValidationFailureError("planner target/context/characteristic/unit must match investigation target")
        if self.comparable_profile.family_identity != self.target.family_identity or self.comparable_profile.context_identity != self.target.context_identity:
            raise ValidationFailureError("O6.2 family/context must match investigation target")
        if self.rca_dataset.knowledge_cutoff != self.knowledge_cutoff:
            raise ValidationFailureError("RCA cutoff must match the exact investigation profile cutoff")
        if any(
            fact.event_at > self.knowledge_cutoff or fact.available_at > self.knowledge_cutoff
            for fact in self.rca_dataset.evidence
        ):
            raise ValidationFailureError("RCA evidence must be known by the investigation profile cutoff")
        if any(fact.available_at > self.knowledge_cutoff for fact in self.rca_dataset.temporal_facts):
            raise ValidationFailureError("RCA temporal facts must be available by the investigation profile cutoff")
        hypothesis_ids = {item.hypothesis_identity for item in hypotheses}
        pair_hypothesis_ids = {item.hypothesis_a_id for item in self.planner_facts.unresolved_pairs} | {item.hypothesis_b_id for item in self.planner_facts.unresolved_pairs}
        if hypothesis_ids != pair_hypothesis_ids:
            raise ValidationFailureError("investigation display hypotheses must exactly describe planner hypothesis identities")

    @classmethod
    def from_payload(
        cls,
        value: object,
        *,
        revision_known_at: datetime | None = None,
    ) -> "InvestigationProfile":
        if not isinstance(value, Mapping):
            raise ValidationFailureError("Episode has no investigation profile object")
        expected = {"schema", "target", "change", "hypotheses", "source_identity", "knowledge_cutoff", "planner_facts", "evidence_groups", COMPARABLE_PROFILE_KEY, "rca", "limitations"}
        if set(value) != expected:
            raise ValidationFailureError("investigation profile fields do not match its versioned schema")
        try:
            target_raw = value["target"]
            change_raw = value["change"]
            if not isinstance(target_raw, Mapping) or not isinstance(change_raw, Mapping):
                raise TypeError
            target = InvestigationTarget(**{name: target_raw[name] for name in InvestigationTarget.__dataclass_fields__})
            change = InvestigationChange(
                change_raw["headline"], change_raw["description"], change_raw.get("magnitude"),
                None if change_raw.get("onset_at") is None else _timestamp(change_raw["onset_at"], "change onset"),
            )
            hypotheses = tuple(InvestigationHypothesis(item["hypothesis_identity"], item["title"], item["summary"])
                               for item in _sequence(value["hypotheses"], "hypotheses", 40))
            raw_cutoff = value["knowledge_cutoff"]
            if raw_cutoff == EPISODE_REVISION_CUTOFF:
                if revision_known_at is None:
                    raise ValidationFailureError("investigation cutoff reference requires its immutable Episode revision")
                raw_cutoff = revision_known_at
            return cls(
                value["schema"], target, change, hypotheses, _source_identity(value["source_identity"]),
                _timestamp(raw_cutoff, "knowledge_cutoff"), _planner_facts(value["planner_facts"], revision_known_at=revision_known_at),
                tuple(InvestigationEvidenceGroup(
                    item["evidence_group_identity"], item["dependence_identity"], item["polarity"], item["title"], item["summary"],
                    item["source_identity"], _timestamp(item["event_at"], "event_at"), _timestamp(item["available_at"], "available_at"),
                    item["qualification_state"], tuple(item.get("evidence_references", ())), tuple(item.get("limitation_codes", ())),
                ) for item in _sequence(value["evidence_groups"], "evidence_groups", MAX_INVESTIGATION_EVIDENCE_GROUPS)),
                _comparable_profile(value[COMPARABLE_PROFILE_KEY]), RcaDataset.from_dict(value["rca"], revision_known_at=revision_known_at),
                tuple(value["limitations"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValidationFailureError):
                raise
            raise ValidationFailureError("investigation profile is malformed") from exc

    def as_dict(self) -> dict[str, object]:
        planner_facts = self.planner_facts.as_dict()
        planner_facts["viewed_revisions"] = self.planner_facts.viewed_revisions.as_dict()
        return {
            "schema": self.schema, "target": self.target.as_dict(), "change": self.change.as_dict(),
            "hypotheses": [item.as_dict() for item in self.hypotheses],
            "source_identity": self.source_identity.as_dict(), "knowledge_cutoff": self.knowledge_cutoff,
            "planner_facts": planner_facts, "evidence_groups": [item.as_dict() for item in self.evidence_groups],
            COMPARABLE_PROFILE_KEY: {
                "schema": COMPARABLE_PROFILE_SCHEMA,
                "family_identity": self.comparable_profile.family_identity,
                "context_identity": self.comparable_profile.context_identity,
                "source_identity": self.comparable_profile.source_identity.as_dict(),
                "fingerprint": self.comparable_profile.fingerprint.as_dict(),
                "eligibility_state": self.comparable_profile.eligibility_state.value,
                "eligibility_identity": self.comparable_profile.eligibility_identity,
                "qualification_evidence_identity": self.comparable_profile.qualification_evidence_identity,
                "curation_state": self.comparable_profile.curation_state.value,
                "curation_evidence_identity": self.comparable_profile.curation_evidence_identity,
                "data_completeness_limitations": list(self.comparable_profile.data_completeness_limitations),
                "claims": [item.as_dict() for item in self.comparable_profile.claims],
            },
            "rca": self.rca_dataset.as_dict(), "limitations": list(self.limitations),
        }

    @property
    def identity(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict()).encode("utf-8")).hexdigest()


class ComponentState(StrEnum):
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    UNQUALIFIED = "UNQUALIFIED"
    STALE = "STALE"
    MATERIALIZATION_REQUIRED = "MATERIALIZATION_REQUIRED"
    PENDING = "PENDING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class InvestigationComponent:
    state: ComponentState
    identity: str | None
    reason_codes: tuple[str, ...]
    value: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ComponentState):
            raise ValidationFailureError("investigation component state is invalid")
        if self.identity is not None:
            object.__setattr__(self, "identity", _identity(self.identity, "component identity"))
        object.__setattr__(self, "reason_codes", tuple(sorted(set(_identity(item, "component reason", maximum=64) for item in self.reason_codes))))


@dataclass(frozen=True, slots=True)
class EpisodeInvestigation:
    scope: AccessScope
    episode_id: str
    revision_id: str
    revision_vector: RevisionVector
    known_at: datetime
    active_cycle_id: str | None
    brief: EpisodeBrief
    workflow: InvestigationComponent
    profile: InvestigationComponent
    planner: InvestigationComponent
    comparable_history: InvestigationComponent
    rca: InvestigationComponent

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AccessScope) or not isinstance(self.brief, EpisodeBrief):
            raise ValidationFailureError("Episode investigation requires exact scope and brief")
        for component in (self.workflow, self.profile, self.planner, self.comparable_history, self.rca):
            if not isinstance(component, InvestigationComponent):
                raise ValidationFailureError("Episode investigation components must be typed")
        if (self.episode_id, self.revision_id, self.revision_vector) != (self.brief.episode_id, self.brief.revision_id, self.brief.revision_vector):
            raise ValidationFailureError("Episode investigation identity must bind to its coherent brief")

    def as_dict(self) -> dict[str, object]:
        def component(value: InvestigationComponent) -> dict[str, object]:
            body = {"state": value.state.value, "identity": value.identity, "reason_codes": list(value.reason_codes)}
            payload = value.value
            if hasattr(payload, "as_dict"):
                body["value"] = payload.as_dict()
            elif isinstance(payload, tuple):
                body["value"] = [item.as_dict() if hasattr(item, "as_dict") else item for item in payload]
            return body
        return {
            "scope": self.scope.as_dict(), "episode_id": self.episode_id, "revision_id": self.revision_id,
            "revision_vector": self.revision_vector.as_dict(), "known_at": self.known_at,
            "active_cycle_id": self.active_cycle_id, "brief": self.brief.as_dict(),
            "workflow": component(self.workflow), "profile": component(self.profile), "planner": component(self.planner),
            "comparable_history": component(self.comparable_history), "rca": component(self.rca),
        }


def _failure_component(exc: BaseException, *, stale: bool = False) -> InvestigationComponent:
    reason = "STALE_VIEW" if stale or isinstance(exc, CoherentReadConflictError) else getattr(exc, "code", "COMPONENT_QUERY_FAILED")
    if reason == "COMPONENT_QUERY_FAILED" or not isinstance(reason, str) or not reason.isupper() or len(reason) > 64:
        reason = type(exc).__name__.upper()[:64] or "COMPONENT_QUERY_FAILED"
    return InvestigationComponent(ComponentState.STALE if reason == "STALE_VIEW" else ComponentState.FAILED, None, (reason,))


def _brief_without_investigation_profile(brief: EpisodeBrief) -> EpisodeBrief:
    analytical = dict(brief.analytical)
    analytical.pop(INVESTIGATION_PROFILE_KEY, None)
    return replace(brief, analytical=analytical)


def _rca_source_identities(profile: InvestigationProfile) -> tuple[str, ...]:
    """Bind cohort source IDs plus the exact O6.2 source revision tuple."""

    source_ids = {
        profile.source_identity.snapshot_id,
        *(item.source_identity for item in profile.rca_dataset.evidence),
        *(item.source_identity for item in profile.rca_dataset.temporal_facts),
        *(item.source_identity for item in profile.rca_dataset.cohorts),
    }
    exact_source_binding = hashlib.sha256(canonical_json(profile.source_identity.as_dict()).encode("utf-8")).hexdigest()
    source_ids.add(f"ephi.source-binding.v1:{exact_source_binding}")
    return tuple(sorted(source_ids))


class EpisodeInvestigationQueryService:
    """Compose brief/workflow/planner/history/RCA against one exact view."""

    def __init__(
        self,
        briefs: EpisodeBriefQueryService,
        decision_loop: DecisionLoopCommandService,
        planner: NextCheckPlannerService,
        current_authorization: CurrentAuthorizationAuthority,
        planner_policy: PlannerPolicy,
        check_catalog: CheckTemplateCatalog,
        rca: RcaAnalysisService,
        comparable_history: ComparableCaseHistoryQueryService | None = None,
        rca_materializations: RcaMaterializationCoordinator | None = None,
    ) -> None:
        self.briefs = briefs
        self.decision_loop = decision_loop
        self.planner = planner
        self.current_authorization = current_authorization
        self.planner_policy = planner_policy
        self.check_catalog = check_catalog
        self.rca_service = rca
        self.comparable_history_service = comparable_history
        self.rca_materializations = rca_materializations

    def load_current_rca_facts(self, principal: Principal, query: RcaQuery) -> RcaCurrentFacts:
        """Reload the exact current profile/workflow for a read or worker job."""

        self.current_authorization.authorize(principal, query.scope, EPISODE_READ_CAPABILITY)
        fresh_brief = self.briefs.get_episode_brief(principal, query.scope, query.episode_identity)
        fresh_loop = self.decision_loop.get_decision_loop(principal, query.scope, query.episode_identity)
        if fresh_brief.revision_id != query.analytical_revision_identity:
            raise CoherentReadConflictError("Episode analytical revision advanced during RCA read")
        current_profile = InvestigationProfile.from_payload(
            fresh_brief.analytical.get(INVESTIGATION_PROFILE_KEY), revision_known_at=fresh_brief.known_at
        )
        if current_profile.planner_facts.cycle_id != fresh_loop.active_cycle_id:
            raise CoherentReadConflictError("investigation profile cycle does not match the current active Episode cycle")
        current_sources = _rca_source_identities(current_profile)
        return RcaCurrentFacts(
            fresh_brief.episode_id, fresh_brief.revision_id, fresh_loop.aggregate_version,
            fresh_loop.active_cycle_id, current_profile.knowledge_cutoff, current_sources,
            current_profile.rca_dataset.policy_identity, current_profile.rca_dataset.schema_identity,
            current_profile.rca_dataset,
        )

    def get_episode_investigation(self, principal: Principal, scope: AccessScope, episode_id: str) -> EpisodeInvestigation:
        # The brief service performs current authorization before current-head
        # lookup. All subsequent work is pinned to this returned revision.
        brief = self.briefs.get_episode_brief(principal, scope, episode_id)
        if brief.historical:
            raise CoherentReadConflictError("investigation workspace requires the current coherent Episode view")
        workflow_snapshot: DecisionLoopSnapshot | None = None
        workflow_component: InvestigationComponent
        try:
            workflow_snapshot = self.decision_loop.get_decision_loop(principal, scope, episode_id)
            if workflow_snapshot.aggregate_version != brief.revision_vector.workflow_version:
                raise CoherentReadConflictError("Episode workflow changed while the investigation view was composed")
            if workflow_snapshot.scope_key != scope.canonical_key or workflow_snapshot.revision_vector != brief.revision_vector:
                raise CoherentReadConflictError("Episode workflow scope/revision does not match the coherent brief")
            workflow_component = InvestigationComponent(ComponentState.READY, f"{episode_id}:{workflow_snapshot.active_cycle_id}:{workflow_snapshot.aggregate_version}", (), workflow_snapshot)
        except Exception as exc:
            workflow_component = _failure_component(exc)

        try:
            raw_profile = brief.analytical.get(INVESTIGATION_PROFILE_KEY)
            if raw_profile is None:
                unavailable = InvestigationComponent(ComponentState.UNAVAILABLE, None, ("NO_INVESTIGATION_PROFILE",))
                return EpisodeInvestigation(
                    scope, brief.episode_id, brief.revision_id, brief.revision_vector, brief.known_at,
                    workflow_snapshot.active_cycle_id if workflow_snapshot else None, brief, workflow_component,
                    unavailable, InvestigationComponent(ComponentState.UNAVAILABLE, None, ("NO_INVESTIGATION_PROFILE",)),
                    InvestigationComponent(ComponentState.UNAVAILABLE, None, ("NO_INVESTIGATION_PROFILE",)),
                    InvestigationComponent(ComponentState.UNAVAILABLE, None, ("NO_INVESTIGATION_PROFILE",)),
                )
            profile = InvestigationProfile.from_payload(raw_profile, revision_known_at=brief.known_at)
            if profile.knowledge_cutoff != brief.known_at or profile.planner_facts.as_of != profile.knowledge_cutoff:
                raise CoherentReadConflictError("investigation profile cutoff does not match the immutable Episode publication")
            expected_policy_identity = investigation_policy_identity(self.planner_policy, self.check_catalog)
            if profile.rca_dataset.policy_identity != expected_policy_identity:
                raise CoherentReadConflictError("RCA policy identity does not match the current typed downstream planner/catalog policy")
            if profile.planner_facts.episode_id != episode_id:
                raise CoherentReadConflictError("planner facts Episode identity does not match the coherent read")
            stored_vector = profile.planner_facts.viewed_revisions
            current_vector = brief.revision_vector
            if replace(current_vector, workflow_version=stored_vector.workflow_version) != stored_vector:
                raise CoherentReadConflictError("planner facts analytical revision vector does not match the coherent read")
            if profile.comparable_profile.source_identity != profile.source_identity:
                raise CoherentReadConflictError("comparable history source identity does not match the Episode source identity")
            top_level_comparable = brief.analytical.get(COMPARABLE_PROFILE_KEY)
            if canonical_json(top_level_comparable) != canonical_json(profile.as_dict()[COMPARABLE_PROFILE_KEY]):
                raise CoherentReadConflictError("O6.2 profile differs from the typed investigation profile")
            profile_component = InvestigationComponent(ComponentState.READY, profile.identity, (), profile)
        except Exception as exc:
            failed = _failure_component(exc)
            return EpisodeInvestigation(
                scope, brief.episode_id, brief.revision_id, brief.revision_vector, brief.known_at,
                workflow_snapshot.active_cycle_id if workflow_snapshot else None, _brief_without_investigation_profile(brief), workflow_component, failed,
                InvestigationComponent(ComponentState.UNAVAILABLE, None, ("PROFILE_NOT_QUALIFIED",)),
                InvestigationComponent(ComponentState.UNAVAILABLE, None, ("PROFILE_NOT_QUALIFIED",)),
                InvestigationComponent(ComponentState.UNAVAILABLE, None, ("PROFILE_NOT_QUALIFIED",)),
            )

        if workflow_snapshot is None:
            workflow_unavailable = InvestigationComponent(ComponentState.STALE, None, ("WORKFLOW_VIEW_UNAVAILABLE",))
            return EpisodeInvestigation(scope, brief.episode_id, brief.revision_id, brief.revision_vector, brief.known_at, None, brief,
                                        workflow_component, profile_component, workflow_unavailable,
                                        InvestigationComponent(ComponentState.UNAVAILABLE, None, ("WORKFLOW_VIEW_UNAVAILABLE",)),
                                        InvestigationComponent(ComponentState.UNAVAILABLE, None, ("WORKFLOW_VIEW_UNAVAILABLE",)))

        if profile.planner_facts.cycle_id != workflow_snapshot.active_cycle_id:
            stale = InvestigationComponent(ComponentState.STALE, None, ("EXACT_VIEW_CYCLE_MISMATCH",))
            return EpisodeInvestigation(
                scope, brief.episode_id, brief.revision_id, brief.revision_vector, brief.known_at,
                workflow_snapshot.active_cycle_id, _brief_without_investigation_profile(brief), workflow_component, stale, stale, stale, stale,
            )

        planner_facts = replace(
            profile.planner_facts,
            workflow_version=brief.revision_vector.workflow_version,
            viewed_revisions=brief.revision_vector,
        )
        try:
            plan = self.planner.plan(
                principal, scope, episode_id, expected_workflow_version=brief.revision_vector.workflow_version,
                viewed_revisions=brief.revision_vector, facts=planner_facts,
                policy=self.planner_policy, catalog=self.check_catalog,
            )
            planner_component = InvestigationComponent(ComponentState.READY, plan.plan_identity, (), plan)
        except Exception as exc:
            planner_component = _failure_component(exc)

        history_component: InvestigationComponent
        if self.comparable_history_service is None:
            history_component = InvestigationComponent(ComponentState.UNAVAILABLE, None, ("COMPARABLE_HISTORY_AUTHORITY_UNBOUND",))
        else:
            try:
                query = ComparableCaseQuery(
                    scope, episode_id, workflow_snapshot.active_cycle_id, brief.revision_vector.workflow_version,
                    brief.revision_id, profile.knowledge_cutoff, profile.comparable_profile.source_identity,
                    profile.comparable_profile.fingerprint.identity, profile.comparable_profile.family_identity,
                    profile.comparable_profile.context_identity, 20,
                )
                page = self.comparable_history_service.retrieve(principal, query, page_size=20)
                if page.state.value == "MATERIALIZATION_REQUIRED":
                    history_component = InvestigationComponent(ComponentState.MATERIALIZATION_REQUIRED, page.query_identity, (page.materialization_reason or "CANDIDATE_LIMIT_EXCEEDED",), page)
                else:
                    history_component = InvestigationComponent(ComponentState.READY, page.result_identity, (), page)
            except Exception as exc:
                history_component = _failure_component(exc)

        try:
            sources = _rca_source_identities(profile)
            query = RcaQuery(
                scope, episode_id, brief.revision_id, brief.revision_vector.workflow_version,
                workflow_snapshot.active_cycle_id, profile.knowledge_cutoff, sources,
                profile.rca_dataset.policy_identity, profile.rca_dataset.schema_identity,
            )

            load_current_facts = lambda: self.load_current_rca_facts(principal, query)
            materialized = self.rca_materializations.status(principal, query) if self.rca_materializations is not None else None
            if materialized is not None and materialized.state in {RcaMaterializationState.PENDING, RcaMaterializationState.RUNNING}:
                rca_component = InvestigationComponent(ComponentState.PENDING, query.identity, (materialized.state.value,), materialized)
            elif materialized is not None and materialized.state == RcaMaterializationState.STALE:
                rca_component = InvestigationComponent(ComponentState.STALE, query.identity, (materialized.reason_code or "EXACT_VIEW_ADVANCED",), materialized)
            elif materialized is not None and materialized.state == RcaMaterializationState.FAILED:
                rca_component = InvestigationComponent(ComponentState.FAILED, query.identity, (materialized.reason_code or "MATERIALIZATION_FAILED",), materialized)
            elif materialized is not None and materialized.state == RcaMaterializationState.READY:
                result = self.rca_materializations.read_result(principal, query, load_current_facts=load_current_facts)
                if result is None:
                    raise CoherentReadConflictError("ready RCA materialization has no current immutable artifact")
                rca_component = InvestigationComponent(ComponentState.READY, result.result_identity, result.reason_codes, result)
            else:
                result = self.rca_service.analyze(principal, query, load_current_facts=load_current_facts)
                if result.state == RcaState.MATERIALIZATION_REQUIRED and self.rca_materializations is not None:
                    materialized = self.rca_materializations.enqueue(principal, query)
                    rca_component = InvestigationComponent(ComponentState.PENDING, query.identity, ("MATERIALIZATION_REQUIRED",), materialized)
                else:
                    rca_state = ComponentState.MATERIALIZATION_REQUIRED if result.state == RcaState.MATERIALIZATION_REQUIRED else ComponentState.READY
                    rca_component = InvestigationComponent(rca_state, result.result_identity, result.reason_codes, result)
        except Exception as exc:
            rca_component = _failure_component(exc)

        return EpisodeInvestigation(
            scope, brief.episode_id, brief.revision_id, brief.revision_vector, brief.known_at,
            workflow_snapshot.active_cycle_id, brief, workflow_component, profile_component,
            planner_component, history_component, rca_component,
        )


def investigation_policy_identity(planner_policy: PlannerPolicy, check_catalog: CheckTemplateCatalog) -> str:
    """Bind RCA observations to the exact O6.1 policy and check catalog."""

    if not isinstance(planner_policy, PlannerPolicy) or not isinstance(check_catalog, CheckTemplateCatalog):
        raise ValidationFailureError("investigation policy identity requires typed planner policy and check catalog")
    material = {
        "schema": "ephi.investigation-policy-binding.v1",
        "planner_policy_identity": planner_policy.identity,
        "check_catalog_identity": check_catalog.identity,
    }
    return "ephi.investigation-policy.v1:" + hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


__all__ = [
    "ComponentState", "EpisodeInvestigation", "EpisodeInvestigationQueryService", "INVESTIGATION_PROFILE_KEY",
    "EPISODE_REVISION_CUTOFF", "INVESTIGATION_PROFILE_SCHEMA", "InvestigationChange", "InvestigationComponent", "InvestigationEvidenceGroup",
    "InvestigationHypothesis", "InvestigationProfile", "InvestigationTarget", "MAX_INVESTIGATION_EVIDENCE_GROUPS",
    "investigation_policy_identity",
]
