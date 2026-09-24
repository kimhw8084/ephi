"""Bounded, observational Episode RCA over immutable qualified cohort facts.

This module reports descriptive commonality only.  It has no causal ranking,
posterior or confidence model.  Inputs are small identity-bearing summaries;
raw source rows and feature values do not cross this application boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import hashlib

from .context import AccessScope, CurrentAuthorizationAuthority, Principal
from .errors import CoherentReadConflictError, ValidationFailureError
from .hashing import canonical_json


RCA_SCHEMA_IDENTITY = "ephi.rca.observational.v1"
RCA_READ_CAPABILITY = "ephi.rca.read"
MAX_RCA_COHORTS = 8
MAX_RCA_SAMPLE_FACTS = 5_000
MAX_RCA_EVIDENCE_FACTS = 10_000
MAX_RCA_SYNCHRONOUS_SAMPLE_FACTS = 500
MAX_RCA_SYNCHRONOUS_EVIDENCE_FACTS = 2_000
MAX_RCA_FACT_IDENTITIES = 64
MIN_INDEPENDENT_AFFECTED_GROUPS = 2
MIN_INDEPENDENT_CONTROL_GROUPS = 2


def _identity(value: object, field: str, *, max_length: int = 240) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or len(value) > max_length:
        raise ValidationFailureError(f"{field} must be a bounded non-empty identity")
    return value


def _identities(value: object, field: str, *, maximum: int = MAX_RCA_FACT_IDENTITIES) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)) or len(value) > maximum:
        raise ValidationFailureError(f"{field} must be a bounded identity sequence")
    result = tuple(sorted(_identity(item, field) for item in value))
    if len(set(result)) != len(result):
        raise ValidationFailureError(f"{field} identities must be unique")
    return result


def _sequence(value: object, field: str, maximum: int) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)) or len(value) > maximum:
        raise ValidationFailureError(f"{field} must be a bounded sequence")
    return tuple(value)


def _instant(value: object, field: str) -> datetime:
    if isinstance(value, Mapping) and set(value) == {"$datetime"}:
        value = value["$datetime"]
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationFailureError(f"{field} must be a timezone-aware timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailureError(f"{field} must be a timezone-aware timestamp")
    return value.astimezone(timezone.utc)


class CohortRole(StrEnum):
    AFFECTED = "AFFECTED"
    CONTROL = "CONTROL"


class CohortEligibility(StrEnum):
    QUALIFIED = "QUALIFIED"
    UNQUALIFIED = "UNQUALIFIED"
    STALE = "STALE"
    INVALID = "INVALID"


class RcaState(StrEnum):
    READY = "READY"
    INVALID_CONTROLS = "INVALID_CONTROLS"
    INSUFFICIENT_CONTROLS = "INSUFFICIENT_CONTROLS"
    UNQUALIFIED_CONTROLS = "UNQUALIFIED_CONTROLS"
    MATERIALIZATION_REQUIRED = "MATERIALIZATION_REQUIRED"


class ControlQuality(StrEnum):
    QUALIFIED = "QUALIFIED"
    INVALID = "INVALID"
    INSUFFICIENT = "INSUFFICIENT"
    UNQUALIFIED = "UNQUALIFIED"


class TemporalFactKind(StrEnum):
    CANDIDATE_CHANGE = "CANDIDATE_CHANGE"
    CONTROL_REGIME_START = "CONTROL_REGIME_START"
    INTERVENTION_CLAIM = "INTERVENTION_CLAIM"
    HISTORICAL_CLAIM = "HISTORICAL_CLAIM"


@dataclass(frozen=True, slots=True)
class RcaCohort:
    cohort_identity: str
    role: CohortRole
    eligibility: CohortEligibility
    context_identity: str
    characteristic_identity: str
    unit_identity: str
    interval_start: datetime
    interval_end: datetime
    source_identity: str
    qualification_identity: str
    matching_dimensions: tuple[str, ...]
    matched_dimensions: tuple[str, ...]
    mismatches: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("cohort_identity", "context_identity", "characteristic_identity", "unit_identity", "source_identity", "qualification_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        if not isinstance(self.role, CohortRole) or not isinstance(self.eligibility, CohortEligibility):
            raise ValidationFailureError("cohort role and eligibility must use supported states")
        start = _instant(self.interval_start, "cohort interval_start")
        end = _instant(self.interval_end, "cohort interval_end")
        if start > end:
            raise ValidationFailureError("cohort interval_start must not follow interval_end")
        object.__setattr__(self, "interval_start", start)
        object.__setattr__(self, "interval_end", end)
        for field in ("matching_dimensions", "matched_dimensions", "mismatches", "reason_codes"):
            object.__setattr__(self, field, _identities(getattr(self, field), field))
        if not set(self.matched_dimensions).issubset(self.matching_dimensions):
            raise ValidationFailureError("matched dimensions must be declared matching dimensions")
        if self.role == CohortRole.AFFECTED and self.eligibility != CohortEligibility.QUALIFIED:
            raise ValidationFailureError("the affected cohort must be explicitly qualified")

    def as_dict(self) -> dict[str, object]:
        return {
            "cohort_identity": self.cohort_identity,
            "role": self.role.value,
            "eligibility": self.eligibility.value,
            "context_identity": self.context_identity,
            "characteristic_identity": self.characteristic_identity,
            "unit_identity": self.unit_identity,
            "interval_start": self.interval_start,
            "interval_end": self.interval_end,
            "source_identity": self.source_identity,
            "qualification_identity": self.qualification_identity,
            "matching_dimensions": list(self.matching_dimensions),
            "matched_dimensions": list(self.matched_dimensions),
            "mismatches": list(self.mismatches),
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RcaCohort":
        expected = {
            "cohort_identity", "role", "eligibility", "context_identity", "characteristic_identity", "unit_identity",
            "interval_start", "interval_end", "source_identity", "qualification_identity", "matching_dimensions",
            "matched_dimensions", "mismatches", "reason_codes",
        }
        if set(value) != expected:
            raise ValidationFailureError("RCA cohort fields do not match the versioned schema")
        try:
            return cls(
                value["cohort_identity"], CohortRole(value["role"]), CohortEligibility(value["eligibility"]),
                value["context_identity"], value["characteristic_identity"], value["unit_identity"],
                _instant(value["interval_start"], "interval_start"), _instant(value["interval_end"], "interval_end"),
                value["source_identity"], value["qualification_identity"],
                _sequence(value["matching_dimensions"], "cohort matching dimensions", MAX_RCA_FACT_IDENTITIES),
                _sequence(value["matched_dimensions"], "cohort matched dimensions", MAX_RCA_FACT_IDENTITIES),
                _sequence(value["mismatches"], "cohort mismatches", MAX_RCA_FACT_IDENTITIES),
                _sequence(value["reason_codes"], "cohort reason codes", MAX_RCA_FACT_IDENTITIES),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailureError("RCA cohort profile is malformed") from exc


@dataclass(frozen=True, slots=True)
class RcaEvidenceFact:
    evidence_identity: str
    cohort_identity: str
    sample_identity: str
    dependence_identity: str
    source_identity: str
    event_at: datetime
    available_at: datetime
    context_identity: str
    characteristic_identity: str
    unit_identity: str
    factor_identities: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("evidence_identity", "cohort_identity", "sample_identity", "dependence_identity", "source_identity", "context_identity", "characteristic_identity", "unit_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        for field in ("event_at", "available_at"):
            object.__setattr__(self, field, _instant(getattr(self, field), field))
        if self.event_at > self.available_at:
            raise ValidationFailureError("RCA evidence event_at must not follow available_at")
        object.__setattr__(self, "factor_identities", _identities(self.factor_identities, "factor_identities"))

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_identity": self.evidence_identity,
            "cohort_identity": self.cohort_identity,
            "sample_identity": self.sample_identity,
            "dependence_identity": self.dependence_identity,
            "source_identity": self.source_identity,
            "event_at": self.event_at,
            "available_at": self.available_at,
            "context_identity": self.context_identity,
            "characteristic_identity": self.characteristic_identity,
            "unit_identity": self.unit_identity,
            "factor_identities": list(self.factor_identities),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RcaEvidenceFact":
        expected = {
            "evidence_identity", "cohort_identity", "sample_identity", "dependence_identity", "source_identity",
            "event_at", "available_at", "context_identity", "characteristic_identity", "unit_identity", "factor_identities",
        }
        if set(value) != expected:
            raise ValidationFailureError("RCA evidence fields do not match the versioned schema")
        try:
            return cls(
                value["evidence_identity"], value["cohort_identity"], value["sample_identity"], value["dependence_identity"],
                value["source_identity"], _instant(value["event_at"], "event_at"), _instant(value["available_at"], "available_at"),
                value["context_identity"], value["characteristic_identity"], value["unit_identity"],
                _sequence(value["factor_identities"], "evidence factor identities", MAX_RCA_FACT_IDENTITIES),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailureError("RCA evidence fact is malformed") from exc


@dataclass(frozen=True, slots=True)
class RcaExclusion:
    identity: str
    cohort_identity: str
    reason_code: str

    def __post_init__(self) -> None:
        for field in ("identity", "cohort_identity", "reason_code"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))

    def as_dict(self) -> dict[str, str]:
        return {"identity": self.identity, "cohort_identity": self.cohort_identity, "reason_code": self.reason_code}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RcaExclusion":
        if set(value) != {"identity", "cohort_identity", "reason_code"}:
            raise ValidationFailureError("RCA exclusion fields do not match the versioned schema")
        try:
            return cls(value["identity"], value["cohort_identity"], value["reason_code"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailureError("RCA evidence exclusion is malformed") from exc


@dataclass(frozen=True, slots=True)
class RcaTemporalFact:
    fact_identity: str
    kind: TemporalFactKind
    event_at: datetime
    available_at: datetime
    source_identity: str
    linked_event_at: datetime | None = None
    claimed_order: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_identity", _identity(self.fact_identity, "fact_identity"))
        object.__setattr__(self, "source_identity", _identity(self.source_identity, "source_identity"))
        if not isinstance(self.kind, TemporalFactKind):
            raise ValidationFailureError("temporal fact kind is unsupported")
        for field in ("event_at", "available_at"):
            object.__setattr__(self, field, _instant(getattr(self, field), field))
        if self.event_at > self.available_at:
            raise ValidationFailureError("RCA temporal event_at must not follow available_at")
        if self.linked_event_at is not None:
            object.__setattr__(self, "linked_event_at", _instant(self.linked_event_at, "linked_event_at"))
        if self.claimed_order not in {None, "PRECEDES", "FOLLOWS"}:
            raise ValidationFailureError("claimed_order must be PRECEDES or FOLLOWS")
        if self.kind == TemporalFactKind.INTERVENTION_CLAIM and (self.linked_event_at is None or self.claimed_order is None):
            raise ValidationFailureError("intervention claims require a linked event and claimed order")

    def as_dict(self) -> dict[str, object]:
        return {
            "fact_identity": self.fact_identity, "kind": self.kind.value, "event_at": self.event_at,
            "available_at": self.available_at, "source_identity": self.source_identity,
            "linked_event_at": self.linked_event_at, "claimed_order": self.claimed_order,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RcaTemporalFact":
        if set(value) != {"fact_identity", "kind", "event_at", "available_at", "source_identity", "linked_event_at", "claimed_order"}:
            raise ValidationFailureError("RCA temporal fact fields do not match the versioned schema")
        try:
            return cls(
                value["fact_identity"], TemporalFactKind(value["kind"]), _instant(value["event_at"], "event_at"),
                _instant(value["available_at"], "available_at"), value["source_identity"],
                None if value.get("linked_event_at") is None else _instant(value["linked_event_at"], "linked_event_at"),
                value.get("claimed_order"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailureError("RCA temporal fact is malformed") from exc


@dataclass(frozen=True, slots=True)
class RcaDataset:
    schema_identity: str
    policy_identity: str
    knowledge_cutoff: datetime
    onset_at: datetime
    affected_cohort_identity: str
    cohorts: tuple[RcaCohort, ...]
    evidence: tuple[RcaEvidenceFact, ...]
    exclusions: tuple[RcaExclusion, ...] = ()
    temporal_facts: tuple[RcaTemporalFact, ...] = ()
    limitation_codes: tuple[str, ...] = ()
    coverage: str = "COMPLETE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_identity", _identity(self.schema_identity, "schema_identity"))
        object.__setattr__(self, "policy_identity", _identity(self.policy_identity, "policy_identity"))
        for field in ("knowledge_cutoff", "onset_at"):
            object.__setattr__(self, field, _instant(getattr(self, field), field))
        if self.onset_at > self.knowledge_cutoff:
            raise ValidationFailureError("RCA onset must not be after the knowledge cutoff")
        object.__setattr__(self, "affected_cohort_identity", _identity(self.affected_cohort_identity, "affected_cohort_identity"))
        if not isinstance(self.cohorts, (tuple, list)) or not 1 <= len(self.cohorts) <= MAX_RCA_COHORTS or any(not isinstance(item, RcaCohort) for item in self.cohorts):
            raise ValidationFailureError("RCA cohorts must be a bounded typed sequence")
        cohorts = tuple(sorted(self.cohorts, key=lambda item: item.cohort_identity))
        if len({item.cohort_identity for item in cohorts}) != len(cohorts):
            raise ValidationFailureError("RCA cohort identities must be unique")
        by_id = {item.cohort_identity: item for item in cohorts}
        affected = by_id.get(self.affected_cohort_identity)
        if affected is None or affected.role != CohortRole.AFFECTED:
            raise ValidationFailureError("RCA affected cohort identity must select the affected cohort")
        if sum(item.role == CohortRole.AFFECTED for item in cohorts) != 1:
            raise ValidationFailureError("RCA requires exactly one affected cohort")
        if any(item.interval_start > self.knowledge_cutoff or item.interval_end > self.knowledge_cutoff for item in cohorts):
            raise ValidationFailureError("RCA cohort intervals must not extend beyond the knowledge cutoff")
        object.__setattr__(self, "cohorts", cohorts)
        if not isinstance(self.evidence, (tuple, list)) or len(self.evidence) > MAX_RCA_EVIDENCE_FACTS or any(not isinstance(item, RcaEvidenceFact) for item in self.evidence):
            raise ValidationFailureError("RCA evidence facts exceed the bounded profile contract")
        evidence = tuple(sorted(self.evidence, key=lambda item: (item.event_at, item.evidence_identity)))
        if len({item.evidence_identity for item in evidence}) != len(evidence):
            raise ValidationFailureError("RCA evidence identities must be unique")
        for fact in evidence:
            cohort = by_id.get(fact.cohort_identity)
            if cohort is None:
                raise ValidationFailureError("RCA evidence references an unknown cohort")
            if (fact.context_identity, fact.characteristic_identity, fact.unit_identity) != (cohort.context_identity, cohort.characteristic_identity, cohort.unit_identity):
                raise ValidationFailureError("RCA evidence context, characteristic and unit must match its cohort")
        object.__setattr__(self, "evidence", evidence)
        if not isinstance(self.exclusions, (tuple, list)) or len(self.exclusions) > MAX_RCA_EVIDENCE_FACTS or any(not isinstance(item, RcaExclusion) for item in self.exclusions):
            raise ValidationFailureError("RCA exclusions exceed the bounded profile contract")
        exclusions = tuple(sorted(self.exclusions, key=lambda item: (item.cohort_identity, item.identity, item.reason_code)))
        if any(item.cohort_identity not in by_id for item in exclusions):
            raise ValidationFailureError("RCA exclusion references an unknown cohort")
        object.__setattr__(self, "exclusions", exclusions)
        if not isinstance(self.temporal_facts, (tuple, list)) or len(self.temporal_facts) > MAX_RCA_EVIDENCE_FACTS or any(not isinstance(item, RcaTemporalFact) for item in self.temporal_facts):
            raise ValidationFailureError("RCA temporal facts exceed the bounded profile contract")
        object.__setattr__(self, "temporal_facts", tuple(sorted(self.temporal_facts, key=lambda item: (item.event_at, item.fact_identity))))
        object.__setattr__(self, "limitation_codes", _identities(self.limitation_codes, "limitation_codes"))
        if self.coverage not in {"COMPLETE", "PARTIAL", "UNKNOWN"}:
            raise ValidationFailureError("RCA coverage state is unsupported")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_identity": self.schema_identity, "policy_identity": self.policy_identity,
            "knowledge_cutoff": self.knowledge_cutoff, "onset_at": self.onset_at,
            "affected_cohort_identity": self.affected_cohort_identity,
            "cohorts": [item.as_dict() for item in self.cohorts],
            "evidence": [item.as_dict() for item in self.evidence],
            "exclusions": [item.as_dict() for item in self.exclusions],
            "temporal_facts": [item.as_dict() for item in self.temporal_facts],
            "limitation_codes": list(self.limitation_codes), "coverage": self.coverage,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        *,
        revision_known_at: datetime | None = None,
    ) -> "RcaDataset":
        if not isinstance(value, Mapping):
            raise ValidationFailureError("RCA dataset profile must be an object")
        expected = {
            "schema_identity", "policy_identity", "knowledge_cutoff", "onset_at", "affected_cohort_identity",
            "cohorts", "evidence", "exclusions", "temporal_facts", "limitation_codes", "coverage",
        }
        if set(value) != expected:
            raise ValidationFailureError("RCA dataset fields do not match the versioned schema")
        try:
            raw_cutoff = value["knowledge_cutoff"]
            if raw_cutoff == "EPISODE_REVISION_KNOWN_AT":
                if revision_known_at is None:
                    raise ValidationFailureError("RCA cutoff reference requires its immutable Episode revision")
                raw_cutoff = revision_known_at
            return cls(
                value["schema_identity"], value["policy_identity"], _instant(raw_cutoff, "knowledge_cutoff"),
                _instant(value["onset_at"], "onset_at"), value["affected_cohort_identity"],
                tuple(RcaCohort.from_dict(item) for item in _sequence(value["cohorts"], "RCA cohorts", MAX_RCA_COHORTS)),
                tuple(RcaEvidenceFact.from_dict(item) for item in _sequence(value["evidence"], "RCA evidence", MAX_RCA_EVIDENCE_FACTS)),
                tuple(RcaExclusion.from_dict(item) for item in _sequence(value["exclusions"], "RCA exclusions", MAX_RCA_EVIDENCE_FACTS)),
                tuple(RcaTemporalFact.from_dict(item) for item in _sequence(value["temporal_facts"], "RCA temporal facts", MAX_RCA_EVIDENCE_FACTS)),
                _sequence(value["limitation_codes"], "RCA limitations", MAX_RCA_FACT_IDENTITIES), value["coverage"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValidationFailureError):
                raise
            raise ValidationFailureError("RCA dataset profile is malformed") from exc


@dataclass(frozen=True, slots=True)
class RcaQuery:
    scope: AccessScope
    episode_identity: str
    analytical_revision_identity: str
    workflow_version: int
    active_cycle_identity: str
    knowledge_cutoff: datetime
    source_identities: tuple[str, ...]
    policy_identity: str
    schema_identity: str = RCA_SCHEMA_IDENTITY

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AccessScope):
            raise ValidationFailureError("RCA query scope must be an AccessScope")
        for field in ("episode_identity", "analytical_revision_identity", "active_cycle_identity", "policy_identity", "schema_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        if isinstance(self.workflow_version, bool) or not isinstance(self.workflow_version, int) or self.workflow_version < 0:
            raise ValidationFailureError("RCA workflow_version must be non-negative")
        object.__setattr__(self, "knowledge_cutoff", _instant(self.knowledge_cutoff, "knowledge_cutoff"))
        object.__setattr__(self, "source_identities", _identities(self.source_identities, "source_identities"))
        if not self.source_identities:
            raise ValidationFailureError("RCA query requires exact source identities")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "ephi.rca.query.v1", "scope": self.scope.as_dict(), "episode_identity": self.episode_identity,
            "analytical_revision_identity": self.analytical_revision_identity, "workflow_version": self.workflow_version,
            "active_cycle_identity": self.active_cycle_identity, "knowledge_cutoff": self.knowledge_cutoff,
            "source_identities": list(self.source_identities), "policy_identity": self.policy_identity,
            "schema_identity": self.schema_identity,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RcaQuery":
        expected = {
            "schema", "scope", "episode_identity", "analytical_revision_identity", "workflow_version",
            "active_cycle_identity", "knowledge_cutoff", "source_identities", "policy_identity", "schema_identity",
        }
        if set(value) != expected or value.get("schema") != "ephi.rca.query.v1":
            raise ValidationFailureError("RCA materialization query payload is malformed")
        scope = value.get("scope")
        if not isinstance(scope, Mapping):
            raise ValidationFailureError("RCA materialization scope is malformed")
        try:
            typed_scope = AccessScope(
                scope["scope_id"], scope.get("site_id"), scope.get("area_id"), scope.get("family_id"),
                tuple(scope.get("project_ids", ())),
            )
            return cls(
                typed_scope, value["episode_identity"], value["analytical_revision_identity"],
                value["workflow_version"], value["active_cycle_identity"],
                _instant(value["knowledge_cutoff"], "query knowledge_cutoff"),
                tuple(value["source_identities"]), value["policy_identity"], value["schema_identity"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailureError("RCA materialization query payload is malformed") from exc

    @property
    def identity(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RcaCurrentFacts:
    """Facts loaded after authorization for exact stale-view checks."""

    episode_identity: str
    analytical_revision_identity: str
    workflow_version: int
    active_cycle_identity: str
    knowledge_cutoff: datetime
    source_identities: tuple[str, ...]
    policy_identity: str
    schema_identity: str
    dataset: RcaDataset

    def __post_init__(self) -> None:
        for field in ("episode_identity", "analytical_revision_identity", "active_cycle_identity", "policy_identity", "schema_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        if isinstance(self.workflow_version, bool) or not isinstance(self.workflow_version, int) or self.workflow_version < 0:
            raise ValidationFailureError("current RCA workflow version must be non-negative")
        object.__setattr__(self, "knowledge_cutoff", _instant(self.knowledge_cutoff, "current knowledge cutoff"))
        object.__setattr__(self, "source_identities", _identities(self.source_identities, "current source identities"))
        if not isinstance(self.dataset, RcaDataset):
            raise ValidationFailureError("current RCA dataset must be typed")


@dataclass(frozen=True, slots=True)
class RcaCount:
    numerator: int
    denominator: int
    independent_group_count: int

    def __post_init__(self) -> None:
        for field in ("numerator", "denominator", "independent_group_count"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationFailureError("RCA counts must be non-negative integers")
        if self.numerator > self.denominator or self.independent_group_count != self.denominator:
            raise ValidationFailureError("RCA numerator and independent denominator are inconsistent")

    def as_dict(self) -> dict[str, int]:
        return {"numerator": self.numerator, "denominator": self.denominator, "independent_group_count": self.independent_group_count}


@dataclass(frozen=True, slots=True)
class RcaEvidenceTiming:
    evidence_identity: str
    cohort_identity: str
    source_identity: str
    event_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        for field in ("evidence_identity", "cohort_identity", "source_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        object.__setattr__(self, "event_at", _instant(self.event_at, "event_at"))
        object.__setattr__(self, "available_at", _instant(self.available_at, "available_at"))

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RcaEvidenceTiming":
        try:
            return cls(
                value["evidence_identity"], value["cohort_identity"], value["source_identity"],
                _instant(value["event_at"], "event_at"), _instant(value["available_at"], "available_at"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailureError("RCA evidence timing is malformed") from exc

    def __post_init__(self) -> None:
        for field in ("evidence_identity", "cohort_identity", "source_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        object.__setattr__(self, "event_at", _instant(self.event_at, "event_at"))
        object.__setattr__(self, "available_at", _instant(self.available_at, "available_at"))

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_identity": self.evidence_identity, "cohort_identity": self.cohort_identity,
            "source_identity": self.source_identity, "event_at": self.event_at, "available_at": self.available_at,
        }


@dataclass(frozen=True, slots=True)
class ObservationalCommonality:
    factor_identity: str
    affected: RcaCount
    controls: RcaCount
    rate_difference_numerator: int
    rate_difference_denominator: int
    interpretation: str = "descriptive_observational_commonality_only"

    def __post_init__(self) -> None:
        object.__setattr__(self, "factor_identity", _identity(self.factor_identity, "factor_identity"))
        if not isinstance(self.affected, RcaCount) or not isinstance(self.controls, RcaCount):
            raise ValidationFailureError("observational counts must use the typed count contract")
        if self.affected.denominator == 0 or self.controls.denominator == 0:
            raise ValidationFailureError("observational commonality requires independent denominators")
        if isinstance(self.rate_difference_numerator, bool) or not isinstance(self.rate_difference_numerator, int):
            raise ValidationFailureError("rate difference numerator must be an integer")
        if isinstance(self.rate_difference_denominator, bool) or not isinstance(self.rate_difference_denominator, int) or self.rate_difference_denominator <= 0:
            raise ValidationFailureError("rate difference denominator must be positive")
        expected = self.affected.numerator * self.controls.denominator - self.controls.numerator * self.affected.denominator
        denominator = self.affected.denominator * self.controls.denominator
        if self.rate_difference_numerator != expected or self.rate_difference_denominator != denominator:
            raise ValidationFailureError("observational rate difference does not match its numerator/denominator facts")
        if self.interpretation != "descriptive_observational_commonality_only":
            raise ValidationFailureError("observational commonality interpretation is unsupported")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ObservationalCommonality":
        try:
            affected = value["affected"]
            controls = value["controls"]
            if not isinstance(affected, Mapping) or not isinstance(controls, Mapping):
                raise TypeError
            return cls(
                value["factor_identity"],
                RcaCount(affected["numerator"], affected["denominator"], affected["independent_group_count"]),
                RcaCount(controls["numerator"], controls["denominator"], controls["independent_group_count"]),
                value["rate_difference_numerator"], value["rate_difference_denominator"], value["interpretation"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailureError("RCA commonality fact is malformed") from exc

    def as_dict(self) -> dict[str, object]:
        return {
            "factor_identity": self.factor_identity, "affected": self.affected.as_dict(),
            "controls": self.controls.as_dict(), "rate_difference_numerator": self.rate_difference_numerator,
            "rate_difference_denominator": self.rate_difference_denominator, "interpretation": self.interpretation,
        }


@dataclass(frozen=True, slots=True)
class TemporalContradiction:
    fact_identity: str
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_identity", _identity(self.fact_identity, "fact_identity"))
        object.__setattr__(self, "reason_code", _identity(self.reason_code, "reason_code", max_length=64))

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "TemporalContradiction":
        try:
            return cls(value["fact_identity"], value["reason_code"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailureError("RCA temporal contradiction is malformed") from exc

    def as_dict(self) -> dict[str, str]:
        return {"fact_identity": self.fact_identity, "reason_code": self.reason_code}


@dataclass(frozen=True, slots=True)
class RcaResult:
    query_identity: str
    input_identity: str
    result_identity: str
    episode_identity: str
    analytical_revision_identity: str
    workflow_version: int
    active_cycle_identity: str
    knowledge_cutoff: datetime
    source_identities: tuple[str, ...]
    policy_identity: str
    schema_identity: str
    state: RcaState
    control_quality: ControlQuality
    affected_cohort_identity: str
    control_cohort_identities: tuple[str, ...]
    cohorts: tuple[RcaCohort, ...]
    included_evidence: tuple[RcaEvidenceTiming, ...]
    affected_independent_group_count: int | None
    control_independent_group_count: int | None
    included_evidence_count: int | None
    excluded_evidence: tuple[RcaExclusion, ...]
    reason_codes: tuple[str, ...]
    matching_dimensions: tuple[str, ...]
    mismatches: tuple[str, ...]
    associations: tuple[ObservationalCommonality, ...]
    temporal_contradictions: tuple[TemporalContradiction, ...]
    limitation_codes: tuple[str, ...]
    coverage: str

    def __post_init__(self) -> None:
        for field in (
            "query_identity", "input_identity", "result_identity", "episode_identity",
            "analytical_revision_identity", "active_cycle_identity", "policy_identity", "schema_identity",
            "affected_cohort_identity",
        ):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        if isinstance(self.workflow_version, bool) or not isinstance(self.workflow_version, int) or self.workflow_version < 0:
            raise ValidationFailureError("RCA result workflow version must be non-negative")
        object.__setattr__(self, "knowledge_cutoff", _instant(self.knowledge_cutoff, "result knowledge_cutoff"))
        object.__setattr__(self, "source_identities", _identities(self.source_identities, "result source_identities"))
        object.__setattr__(self, "control_cohort_identities", _identities(self.control_cohort_identities, "result control_cohort_identities", maximum=MAX_RCA_COHORTS))
        if not isinstance(self.state, RcaState) or not isinstance(self.control_quality, ControlQuality):
            raise ValidationFailureError("RCA result state and control quality are invalid")
        for field, cls, maximum in (
            ("cohorts", RcaCohort, MAX_RCA_COHORTS),
            ("included_evidence", RcaEvidenceTiming, MAX_RCA_EVIDENCE_FACTS),
            ("excluded_evidence", RcaExclusion, MAX_RCA_EVIDENCE_FACTS),
            ("associations", ObservationalCommonality, MAX_RCA_FACT_IDENTITIES),
            ("temporal_contradictions", TemporalContradiction, MAX_RCA_EVIDENCE_FACTS),
        ):
            values = getattr(self, field)
            if not isinstance(values, (tuple, list)) or len(values) > maximum or any(not isinstance(item, cls) for item in values):
                raise ValidationFailureError(f"RCA result {field} exceeds the typed bound")
            object.__setattr__(self, field, tuple(values))
        for field in ("affected_independent_group_count", "control_independent_group_count", "included_evidence_count"):
            value = getattr(self, field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValidationFailureError(f"RCA result {field} must be a non-negative integer or unavailable")
        for field in ("reason_codes", "matching_dimensions", "mismatches", "limitation_codes"):
            object.__setattr__(self, field, _identities(getattr(self, field), field))
        if self.coverage not in {"COMPLETE", "PARTIAL", "UNKNOWN"}:
            raise ValidationFailureError("RCA result coverage state is unsupported")
        if self.state != RcaState.READY and self.associations:
            raise ValidationFailureError("non-ready RCA states cannot emit commonality summaries")
        if self.state == RcaState.MATERIALIZATION_REQUIRED and any(
            value is not None for value in (self.affected_independent_group_count, self.control_independent_group_count, self.included_evidence_count)
        ):
            raise ValidationFailureError("materialization-required RCA results cannot report partial counts")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "ephi.rca.result.v1", "query_identity": self.query_identity,
            "input_identity": self.input_identity, "result_identity": self.result_identity,
            "episode_identity": self.episode_identity,
            "analytical_revision_identity": self.analytical_revision_identity,
            "workflow_version": self.workflow_version, "active_cycle_identity": self.active_cycle_identity,
            "knowledge_cutoff": self.knowledge_cutoff, "source_identities": list(self.source_identities),
            "policy_identity": self.policy_identity, "schema_identity": self.schema_identity,
            "state": self.state.value, "interpretation": "observational; does not establish cause",
            "control_quality": self.control_quality.value, "affected_cohort_identity": self.affected_cohort_identity,
            "control_cohort_identities": list(self.control_cohort_identities),
            "cohorts": [item.as_dict() for item in self.cohorts],
            "included_evidence": [item.as_dict() for item in self.included_evidence],
            "affected_independent_group_count": self.affected_independent_group_count,
            "control_independent_group_count": self.control_independent_group_count,
            "included_evidence_count": self.included_evidence_count,
            "excluded_evidence": [item.as_dict() for item in self.excluded_evidence],
            "reason_codes": list(self.reason_codes), "matching_dimensions": list(self.matching_dimensions),
            "mismatches": list(self.mismatches), "associations": [item.as_dict() for item in self.associations],
            "temporal_contradictions": [item.as_dict() for item in self.temporal_contradictions],
            "limitation_codes": list(self.limitation_codes), "coverage": self.coverage,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RcaResult":
        expected = {
            "schema", "query_identity", "input_identity", "result_identity", "episode_identity",
            "analytical_revision_identity", "workflow_version", "active_cycle_identity", "knowledge_cutoff",
            "source_identities", "policy_identity", "schema_identity", "state", "interpretation",
            "control_quality", "affected_cohort_identity", "control_cohort_identities", "cohorts",
            "included_evidence", "affected_independent_group_count", "control_independent_group_count",
            "included_evidence_count", "excluded_evidence", "reason_codes", "matching_dimensions", "mismatches",
            "associations", "temporal_contradictions", "limitation_codes", "coverage",
        }
        if set(value) != expected or value.get("schema") != "ephi.rca.result.v1" or value.get("interpretation") != "observational; does not establish cause":
            raise ValidationFailureError("RCA result artifact does not match its versioned observational schema")
        try:
            sequence_values = (
                "source_identities", "control_cohort_identities", "cohorts", "included_evidence",
                "excluded_evidence", "reason_codes", "matching_dimensions", "mismatches", "associations",
                "temporal_contradictions", "limitation_codes",
            )
            if any(not isinstance(value.get(key), (list, tuple)) or isinstance(value.get(key), (str, bytes)) for key in sequence_values):
                raise TypeError
            sequences = {
                key: tuple(raw)
                for key, raw in (
                    ("source_identities", value["source_identities"]),
                    ("control_cohort_identities", value["control_cohort_identities"]),
                    ("cohorts", value["cohorts"]),
                    ("included_evidence", value["included_evidence"]),
                    ("excluded_evidence", value["excluded_evidence"]),
                    ("reason_codes", value["reason_codes"]),
                    ("matching_dimensions", value["matching_dimensions"]),
                    ("mismatches", value["mismatches"]),
                    ("associations", value["associations"]),
                    ("temporal_contradictions", value["temporal_contradictions"]),
                    ("limitation_codes", value["limitation_codes"]),
                )
            }
            return cls(
                value["query_identity"], value["input_identity"], value["result_identity"], value["episode_identity"],
                value["analytical_revision_identity"], value["workflow_version"], value["active_cycle_identity"],
                _instant(value["knowledge_cutoff"], "result knowledge_cutoff"), sequences["source_identities"],
                value["policy_identity"], value["schema_identity"], RcaState(value["state"]),
                ControlQuality(value["control_quality"]), value["affected_cohort_identity"], sequences["control_cohort_identities"],
                tuple(RcaCohort.from_dict(item) for item in sequences["cohorts"]),
                tuple(RcaEvidenceTiming.from_dict(item) for item in sequences["included_evidence"]),
                value["affected_independent_group_count"], value["control_independent_group_count"], value["included_evidence_count"],
                tuple(RcaExclusion.from_dict(item) for item in sequences["excluded_evidence"]), sequences["reason_codes"],
                sequences["matching_dimensions"], sequences["mismatches"],
                tuple(ObservationalCommonality.from_dict(item) for item in sequences["associations"]),
                tuple(TemporalContradiction.from_dict(item) for item in sequences["temporal_contradictions"]),
                sequences["limitation_codes"], value["coverage"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValidationFailureError):
                raise
            raise ValidationFailureError("RCA result artifact is malformed") from exc


def _temporal_contradictions(dataset: RcaDataset, cutoff: datetime) -> tuple[TemporalContradiction, ...]:
    result: list[TemporalContradiction] = []
    affected = next(item for item in dataset.cohorts if item.cohort_identity == dataset.affected_cohort_identity)
    for fact in dataset.temporal_facts:
        if fact.available_at > cutoff:
            continue
        if fact.kind == TemporalFactKind.CANDIDATE_CHANGE and fact.event_at > dataset.onset_at:
            result.append(TemporalContradiction(fact.fact_identity, "CANDIDATE_CHANGE_AFTER_OBSERVED_ONSET"))
        elif fact.kind == TemporalFactKind.CONTROL_REGIME_START and fact.event_at > affected.interval_end:
            result.append(TemporalContradiction(fact.fact_identity, "CONTROL_REGIME_STARTS_AFTER_AFFECTED_INTERVAL"))
        elif fact.kind == TemporalFactKind.INTERVENTION_CLAIM and fact.linked_event_at is not None:
            relation_holds = fact.event_at <= fact.linked_event_at if fact.claimed_order == "PRECEDES" else fact.event_at >= fact.linked_event_at
            if not relation_holds:
                result.append(TemporalContradiction(fact.fact_identity, "INTERVENTION_ORDER_CONTRADICTS_LINKED_EVIDENCE"))
        elif fact.kind == TemporalFactKind.HISTORICAL_CLAIM and fact.linked_event_at is not None and fact.linked_event_at > cutoff:
            result.append(TemporalContradiction(fact.fact_identity, "HISTORICAL_CLAIM_MATURITY_AFTER_CUTOFF"))
    return tuple(sorted(set(result), key=lambda item: (item.fact_identity, item.reason_code)))


class RcaAnalysisService:
    """Authorize first, then compare exact current facts with qualified controls."""

    def __init__(self, current_authorization: CurrentAuthorizationAuthority):
        if not isinstance(current_authorization, CurrentAuthorizationAuthority):
            raise TypeError("current_authorization must be the current authority")
        self.current_authorization = current_authorization

    @staticmethod
    def validate_current_facts(query: RcaQuery, current: RcaCurrentFacts) -> RcaDataset:
        if not isinstance(query, RcaQuery) or not isinstance(current, RcaCurrentFacts):
            raise ValidationFailureError("RCA current facts validation requires typed query and facts")
        expected = (
            query.episode_identity, query.analytical_revision_identity, query.workflow_version,
            query.active_cycle_identity, query.knowledge_cutoff, query.source_identities,
            query.policy_identity, query.schema_identity,
        )
        actual = (
            current.episode_identity, current.analytical_revision_identity, current.workflow_version,
            current.active_cycle_identity, _instant(current.knowledge_cutoff, "current knowledge cutoff"),
            tuple(sorted(current.source_identities)), current.policy_identity, current.schema_identity,
        )
        if expected != actual:
            raise CoherentReadConflictError("RCA query no longer matches the exact current Episode revision, workflow, source or cutoff")
        dataset = current.dataset
        if dataset.knowledge_cutoff != query.knowledge_cutoff or dataset.policy_identity != query.policy_identity or dataset.schema_identity != query.schema_identity:
            raise CoherentReadConflictError("RCA profile source, policy, schema or cutoff identity changed")
        return dataset

    def analyze(
        self,
        principal: Principal,
        query: RcaQuery,
        *,
        load_current_facts: Callable[[], RcaCurrentFacts],
    ) -> RcaResult:
        return self._analyze(
            principal, query, load_current_facts=load_current_facts,
            sample_limit=MAX_RCA_SYNCHRONOUS_SAMPLE_FACTS,
            evidence_limit=MAX_RCA_SYNCHRONOUS_EVIDENCE_FACTS,
            materialized=False,
        )

    def analyze_materialized(
        self,
        principal: Principal,
        query: RcaQuery,
        *,
        load_current_facts: Callable[[], RcaCurrentFacts],
    ) -> RcaResult:
        """Run the larger, still hard-bounded contract from a durable job."""

        return self._analyze(
            principal, query, load_current_facts=load_current_facts,
            sample_limit=MAX_RCA_SAMPLE_FACTS,
            evidence_limit=MAX_RCA_EVIDENCE_FACTS,
            materialized=True,
        )

    def _analyze(
        self,
        principal: Principal,
        query: RcaQuery,
        *,
        load_current_facts: Callable[[], RcaCurrentFacts],
        sample_limit: int,
        evidence_limit: int,
        materialized: bool,
    ) -> RcaResult:
        if not isinstance(query, RcaQuery):
            raise ValidationFailureError("RCA analysis requires a typed query")
        # This must stay before the loader: an unauthorized caller cannot use
        # this API to learn whether an Episode profile or cohort exists.
        self.current_authorization.authorize(principal, query.scope, RCA_READ_CAPABILITY)
        current = load_current_facts()
        if not isinstance(current, RcaCurrentFacts):
            raise ValidationFailureError("RCA current facts loader returned an unsupported value")
        dataset = self.validate_current_facts(query, current)

        future_evidence = tuple(
            fact for fact in dataset.evidence
            if fact.event_at > query.knowledge_cutoff or fact.available_at > query.knowledge_cutoff
        )
        future_temporal_facts = tuple(fact for fact in dataset.temporal_facts if fact.available_at > query.knowledge_cutoff)
        visible_evidence = tuple(
            fact for fact in dataset.evidence
            if fact.event_at <= query.knowledge_cutoff and fact.available_at <= query.knowledge_cutoff
        )
        visible_temporal_facts = tuple(fact for fact in dataset.temporal_facts if fact.available_at <= query.knowledge_cutoff)
        visible_limitations = set(dataset.limitation_codes)
        if future_temporal_facts:
            visible_limitations.add("TEMPORAL_FACTS_EXCLUDED_AFTER_CUTOFF")
        analysis_dataset = replace(
            dataset,
            evidence=visible_evidence,
            temporal_facts=visible_temporal_facts,
            limitation_codes=tuple(sorted(visible_limitations)),
        )

        sample_count = len(analysis_dataset.evidence)
        evidence_count = sample_count + len(analysis_dataset.temporal_facts) + len(analysis_dataset.exclusions)
        control_cohorts = tuple(item for item in analysis_dataset.cohorts if item.role == CohortRole.CONTROL)
        included_cohort_ids = {analysis_dataset.affected_cohort_identity, *(item.cohort_identity for item in control_cohorts)}
        profile_sources = {item.source_identity for item in analysis_dataset.cohorts}
        profile_sources.update(item.source_identity for item in analysis_dataset.evidence)
        profile_sources.update(item.source_identity for item in analysis_dataset.temporal_facts)
        if not profile_sources.issubset(query.source_identities):
            raise CoherentReadConflictError("RCA evidence source identity is outside the bound query")
        input_identity = hashlib.sha256(canonical_json({"query": query.as_dict(), "dataset": analysis_dataset.as_dict()}).encode("utf-8")).hexdigest()
        if sample_count > sample_limit or evidence_count > evidence_limit:
            if materialized:
                raise ValidationFailureError("RCA facts exceed the durable materialization contract")
            result_identity = hashlib.sha256(canonical_json({"query_identity": query.identity, "input_identity": input_identity, "state": RcaState.MATERIALIZATION_REQUIRED.value}).encode("utf-8")).hexdigest()
            return RcaResult(
                query.identity, input_identity, result_identity, query.episode_identity,
                query.analytical_revision_identity, query.workflow_version, query.active_cycle_identity,
                query.knowledge_cutoff, query.source_identities, query.policy_identity, query.schema_identity,
                RcaState.MATERIALIZATION_REQUIRED, ControlQuality.INSUFFICIENT, analysis_dataset.affected_cohort_identity,
                tuple(item.cohort_identity for item in control_cohorts), analysis_dataset.cohorts, (), None, None, None,
                (), ("SYNCHRONOUS_BOUND_EXCEEDED",), (), (), (), (),
                tuple(sorted(
                    set(analysis_dataset.limitation_codes)
                    | {"SYNCHRONOUS_BOUND_EXCEEDED"}
                    | ({"EVIDENCE_EXCLUDED_AFTER_CUTOFF"} if future_evidence else set())
                )), analysis_dataset.coverage,
            )
        grouped_by_cohort: dict[str, dict[str, set[str]]] = {}
        all_future_exclusions: list[RcaExclusion] = []
        sample_for_group: dict[tuple[str, str], str] = {}
        for fact in dataset.evidence:
            if fact.cohort_identity not in included_cohort_ids:
                continue
            if fact.event_at > query.knowledge_cutoff or fact.available_at > query.knowledge_cutoff:
                reason = "EVIDENCE_EVENT_AFTER_CUTOFF" if fact.event_at > query.knowledge_cutoff else "EVIDENCE_AFTER_CUTOFF"
                all_future_exclusions.append(RcaExclusion(fact.evidence_identity, fact.cohort_identity, reason))
                continue
            group_key = (fact.cohort_identity, fact.dependence_identity)
            prior_sample = sample_for_group.setdefault(group_key, fact.sample_identity)
            if prior_sample != fact.sample_identity:
                raise ValidationFailureError("one dependence identity cannot span distinct sample identities")
            grouped_by_cohort.setdefault(fact.cohort_identity, {}).setdefault(fact.dependence_identity, set()).update(fact.factor_identities)
        future_exclusions = tuple(sorted(all_future_exclusions, key=lambda item: (item.cohort_identity, item.identity)))
        affected_groups = grouped_by_cohort.get(dataset.affected_cohort_identity, {})
        control_group_sets = [grouped_by_cohort.get(item.cohort_identity, {}) for item in control_cohorts]
        # A dependence group repeated across control cohorts remains one
        # independent control group; factors are unioned before counting.
        control_groups: dict[str, set[str]] = {}
        for values in control_group_sets:
            for group_id, factors in values.items():
                control_groups.setdefault(group_id, set()).update(factors)
        affected_count = len(affected_groups)
        control_count = len(control_groups)
        affected_cohort = next(item for item in analysis_dataset.cohorts if item.cohort_identity == analysis_dataset.affected_cohort_identity)
        mismatch_values = {item for cohort in control_cohorts for item in cohort.mismatches}
        for cohort in control_cohorts:
            if cohort.context_identity != affected_cohort.context_identity:
                mismatch_values.add("CONTEXT_MISMATCH")
            if cohort.characteristic_identity != affected_cohort.characteristic_identity:
                mismatch_values.add("CHARACTERISTIC_MISMATCH")
            if cohort.unit_identity != affected_cohort.unit_identity:
                mismatch_values.add("UNIT_MISMATCH")
            if not cohort.matching_dimensions or not set(cohort.matching_dimensions).issubset(cohort.matched_dimensions):
                mismatch_values.add("MATCHING_DIMENSION_UNRESOLVED")
        mismatches = tuple(sorted(mismatch_values))
        matching = tuple(sorted({item for cohort in control_cohorts for item in cohort.matched_dimensions}))
        if any(item.eligibility == CohortEligibility.INVALID for item in control_cohorts):
            state, quality, reasons = RcaState.INVALID_CONTROLS, ControlQuality.INVALID, ("CONTROL_COHORT_INVALID",)
            associations = ()
        elif any(item.eligibility != CohortEligibility.QUALIFIED for item in control_cohorts) or mismatches:
            state, quality, reasons = RcaState.UNQUALIFIED_CONTROLS, ControlQuality.UNQUALIFIED, ("CONTROL_QUALIFICATION_OR_MATCHING_FAILED",)
            associations = ()
        elif not control_cohorts or affected_count < MIN_INDEPENDENT_AFFECTED_GROUPS or control_count < MIN_INDEPENDENT_CONTROL_GROUPS:
            state, quality, reasons = RcaState.INSUFFICIENT_CONTROLS, ControlQuality.INSUFFICIENT, ("MINIMUM_INDEPENDENT_GROUPS_NOT_MET",)
            associations = ()
        else:
            state, quality, reasons = RcaState.READY, ControlQuality.QUALIFIED, ()
            affected_factors = set().union(*affected_groups.values()) if affected_groups else set()
            control_factors = set().union(*control_groups.values()) if control_groups else set()
            common_factors = tuple(sorted(affected_factors | control_factors))
            values = []
            for factor in common_factors:
                affected_n = sum(factor in factors for factors in affected_groups.values())
                control_n = sum(factor in factors for factors in control_groups.values())
                numerator = affected_n * control_count - control_n * affected_count
                denominator = affected_count * control_count
                values.append(ObservationalCommonality(
                    factor, RcaCount(affected_n, affected_count, affected_count),
                    RcaCount(control_n, control_count, control_count), numerator, denominator,
                ))
            associations = tuple(values)

        exclusions = tuple(sorted((*analysis_dataset.exclusions, *future_exclusions), key=lambda item: (item.cohort_identity, item.identity, item.reason_code)))
        included_evidence = tuple(
            RcaEvidenceTiming(item.evidence_identity, item.cohort_identity, item.source_identity, item.event_at, item.available_at)
            for item in analysis_dataset.evidence
            if item.cohort_identity in included_cohort_ids
        )
        included_evidence_count = len(included_evidence)
        temporal = _temporal_contradictions(analysis_dataset, query.knowledge_cutoff)
        limitations = tuple(sorted(set(analysis_dataset.limitation_codes) | ({"PARTIAL_COVERAGE"} if analysis_dataset.coverage != "COMPLETE" else set()) | ({"EVIDENCE_EXCLUDED_AFTER_CUTOFF"} if future_exclusions else set())))
        body = {
            "query_identity": query.identity, "input_identity": input_identity,
            "episode_identity": query.episode_identity, "analytical_revision_identity": query.analytical_revision_identity,
            "workflow_version": query.workflow_version, "active_cycle_identity": query.active_cycle_identity,
            "knowledge_cutoff": query.knowledge_cutoff, "source_identities": list(query.source_identities),
            "policy_identity": query.policy_identity, "schema_identity": query.schema_identity,
            "state": state.value,
            "control_quality": quality.value, "affected_cohort_identity": analysis_dataset.affected_cohort_identity,
            "control_cohort_identities": [item.cohort_identity for item in control_cohorts],
            "cohorts": [item.as_dict() for item in analysis_dataset.cohorts],
            "included_evidence": [item.as_dict() for item in included_evidence],
            "affected_independent_group_count": affected_count, "control_independent_group_count": control_count,
            "included_evidence_count": included_evidence_count,
            "excluded_evidence": [item.as_dict() for item in exclusions], "reason_codes": list(reasons),
            "matching_dimensions": list(matching), "mismatches": list(mismatches),
            "associations": [item.as_dict() for item in associations],
            "temporal_contradictions": [item.as_dict() for item in temporal],
            "limitation_codes": list(limitations), "coverage": analysis_dataset.coverage,
        }
        result_reasons = tuple(sorted(set(reasons) | {reason for cohort in control_cohorts for reason in cohort.reason_codes}))
        body["reason_codes"] = list(result_reasons)
        result_identity = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        return RcaResult(
            query.identity, input_identity, result_identity, query.episode_identity,
            query.analytical_revision_identity, query.workflow_version, query.active_cycle_identity,
            query.knowledge_cutoff, query.source_identities, query.policy_identity, query.schema_identity,
            state, quality, analysis_dataset.affected_cohort_identity,
            tuple(item.cohort_identity for item in control_cohorts), analysis_dataset.cohorts, included_evidence,
            affected_count, control_count,
            included_evidence_count, exclusions, result_reasons, matching, mismatches,
            associations, temporal, limitations, analysis_dataset.coverage,
        )


__all__ = [
    "ControlQuality", "CohortEligibility", "CohortRole", "MAX_RCA_EVIDENCE_FACTS",
    "MAX_RCA_SAMPLE_FACTS", "MAX_RCA_SYNCHRONOUS_EVIDENCE_FACTS", "MAX_RCA_SYNCHRONOUS_SAMPLE_FACTS",
    "ObservationalCommonality", "RCA_READ_CAPABILITY", "RCA_SCHEMA_IDENTITY", "RcaAnalysisService",
    "RcaCohort", "RcaCurrentFacts", "RcaDataset", "RcaEvidenceFact", "RcaExclusion", "RcaQuery",
    "RcaEvidenceTiming", "RcaResult", "RcaState", "RcaTemporalFact", "TemporalContradiction", "TemporalFactKind",
]
