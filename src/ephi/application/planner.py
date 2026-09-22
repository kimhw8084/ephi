"""Deterministic O6.1 next-check planning over the existing O5 Episode authority.

Templates and policies are curated, versioned configuration. Planning is an
authorized immutable read: it does not create workflow state, request a check,
or change an existing check/action/recovery outcome.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
from typing import Any

from .context import AccessScope, Principal, RevisionVector
from .decision_loop import (
    DECISION_LOOP_READ_CAPABILITY,
    CheckExecutionMode,
    DecisionLoopCommandService,
)
from .errors import AuthorizationDeniedError, CoherentReadConflictError, ValidationFailureError
from .hashing import canonical_json


PLANNER_SCHEMA_VERSION = "o6.1.v1"
ORDINAL_UTILITY_VERSION = "o6.v1.ordinal-utility.1"
_UTILITY_WEIGHTS = {
    "D": Decimal("0.45"),
    "N": Decimal("0.20"),
    "F": Decimal("0.20"),
    "E": Decimal("0.10"),
    "R": Decimal("0.15"),
}
_MAX_TEMPLATES = 250
_MAX_FACTS = 1000
_MAX_TEXT = 240


class CapabilityState(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    UNQUALIFIED = "UNQUALIFIED"
    UNKNOWN = "UNKNOWN"


class QualificationState(StrEnum):
    QUALIFIED = "QUALIFIED"
    UNQUALIFIED = "UNQUALIFIED"
    UNKNOWN = "UNKNOWN"


class PrerequisiteState(StrEnum):
    SATISFIED = "SATISFIED"
    MISSING = "MISSING"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class EvidenceValidityState(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class DeadlineState(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNKNOWN = "UNKNOWN"


class TurnaroundState(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"
    STALE = "STALE"


class EffortBand(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class DisruptionClass(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class CapabilityRole(StrEnum):
    GENERAL = "GENERAL"
    PEER = "PEER"
    REFERENCE = "REFERENCE"


class TemplatePath(StrEnum):
    DISCRIMINATION = "DISCRIMINATION"
    PREREQUISITE_VALIDATION = "PREREQUISITE_VALIDATION"


class ExclusionReason(StrEnum):
    UNSUPPORTED_CONTEXT = "UNSUPPORTED_CONTEXT"
    UNSUPPORTED_UNIT = "UNSUPPORTED_UNIT"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    UNQUALIFIED_CAPABILITY = "UNQUALIFIED_CAPABILITY"
    UNQUALIFIED_PEER_REFERENCE = "UNQUALIFIED_PEER_REFERENCE"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    STALE_PEER_REFERENCE = "STALE_PEER_REFERENCE"
    MISSING_APPROVAL_AUTHORIZATION = "MISSING_APPROVAL_AUTHORIZATION"
    MISSING_QUALIFICATION = "MISSING_QUALIFICATION"
    MISSING_PREREQUISITE = "MISSING_PREREQUISITE"
    UNKNOWN_PREREQUISITE = "UNKNOWN_PREREQUISITE"
    STALE_PREREQUISITE = "STALE_PREREQUISITE"
    VALID_PRIOR_COMPLETION = "VALID_PRIOR_COMPLETION"
    CHECK_ALREADY_REQUESTED = "CHECK_ALREADY_REQUESTED"
    CHECK_IN_PROGRESS = "CHECK_IN_PROGRESS"
    MISSED_DECISION_WINDOW = "MISSED_DECISION_WINDOW"
    UNSUPPORTED_TURNAROUND = "UNSUPPORTED_TURNAROUND"
    LIFECYCLE_CLOSED = "LIFECYCLE_CLOSED"
    NO_VALIDATION_PATH = "NO_VALIDATION_PATH"
    REDUNDANT_WITH_SELECTED = "REDUNDANT_WITH_SELECTED"
    NOT_SELECTED_LIMIT = "NOT_SELECTED_LIMIT"


def _identity(value: object, field: str, *, max_length: int = _MAX_TEXT) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > max_length
    ):
        raise ValidationFailureError(f"{field} must be a bounded non-empty canonical string")
    return value


def _enum(value: object, kind: type[StrEnum], field: str) -> StrEnum:
    try:
        return value if isinstance(value, kind) else kind(value)
    except (TypeError, ValueError) as exc:
        raise ValidationFailureError(f"{field} is not a supported {kind.__name__}") from exc


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailureError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _seconds(value: object, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValidationFailureError(f"{field} must be a {qualifier} integer")
    return value


def _decimal(value: object, field: str, *, minimum: Decimal = Decimal("0"), maximum: Decimal = Decimal("1")) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (Decimal, int)):
        raise ValidationFailureError(f"{field} must be a Decimal or integer")
    result = Decimal(value)
    if not result.is_finite() or not minimum <= result <= maximum:
        raise ValidationFailureError(f"{field} must be between {minimum} and {maximum}")
    return result


def _ids(values: Sequence[str], field: str, *, maximum: int = _MAX_FACTS) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or len(values) > maximum:
        raise ValidationFailureError(f"{field} must be a bounded sequence of identities")
    result = tuple(_identity(value, field) for value in values)
    if len(result) != len(set(result)):
        raise ValidationFailureError(f"{field} must not contain duplicate identities")
    return tuple(sorted(result))


def _optional_id(value: object, field: str) -> str | None:
    return None if value is None else _identity(value, field)


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    capability_id: str
    max_age_seconds: int
    role: CapabilityRole = CapabilityRole.GENERAL

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_id", _identity(self.capability_id, "capability_id"))
        object.__setattr__(self, "max_age_seconds", _seconds(self.max_age_seconds, "max_age_seconds"))
        object.__setattr__(self, "role", _enum(self.role, CapabilityRole, "role"))

    def as_dict(self) -> dict[str, object]:
        return {"capability_id": self.capability_id, "max_age_seconds": self.max_age_seconds, "role": self.role.value}


@dataclass(frozen=True, slots=True)
class PrerequisiteRequirement:
    prerequisite_id: str
    max_age_seconds: int | None = None
    context_bound: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "prerequisite_id", _identity(self.prerequisite_id, "prerequisite_id"))
        if self.max_age_seconds is not None:
            object.__setattr__(self, "max_age_seconds", _seconds(self.max_age_seconds, "max_age_seconds"))
        if not isinstance(self.context_bound, bool):
            raise ValidationFailureError("context_bound must be a boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "prerequisite_id": self.prerequisite_id,
            "max_age_seconds": self.max_age_seconds,
            "context_bound": self.context_bound,
        }


@dataclass(frozen=True, slots=True)
class PairDiscrimination:
    pair_id: str
    value: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair_id", _identity(self.pair_id, "pair_id"))
        value = _decimal(self.value, "discrimination")
        if value not in {Decimal("0"), Decimal("0.5"), Decimal("1")}:
            raise ValidationFailureError("discrimination must be 0, 0.5, or 1")
        object.__setattr__(self, "value", value)

    def as_dict(self) -> dict[str, object]:
        return {"pair_id": self.pair_id, "value": self.value}


@dataclass(frozen=True, slots=True)
class CheckTemplate:
    """Curated check metadata; this contract deliberately has no setpoint or procedure field."""

    template_id: str
    version: str
    title: str
    family_ids: tuple[str, ...]
    target_kinds: tuple[str, ...]
    supported_contexts: tuple[str, ...]
    supported_units: tuple[str, ...]
    context_independent: bool
    unit_independent: bool
    required_capabilities: tuple[CapabilityRequirement, ...]
    candidate_discrimination: tuple[PairDiscrimination, ...]
    prerequisites: tuple[PrerequisiteRequirement, ...]
    redundant_with: tuple[str, ...]
    redundancy_group_ids: tuple[str, ...]
    evidence_group_ids: tuple[str, ...]
    effort_band: EffortBand
    effort_source_id: str
    turnaround_source_id: str
    disruption: DisruptionClass
    approval_capability: str | None
    execution_mode: CheckExecutionMode | str
    result_schema_identity: str
    interpretation_schema_identity: str
    evidence_quality_requirements: tuple[str, ...]
    qualification_identity: str
    path: TemplatePath = TemplatePath.DISCRIMINATION
    reuse_window_seconds: int = 86400

    def __post_init__(self) -> None:
        for field in ("template_id", "version", "effort_source_id", "turnaround_source_id", "result_schema_identity", "interpretation_schema_identity", "qualification_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        title = _identity(self.title, "title", max_length=120)
        if "\n" in title or "\r" in title:
            raise ValidationFailureError("title must be one bounded display line")
        object.__setattr__(self, "title", title)
        for field in ("family_ids", "target_kinds", "supported_contexts", "supported_units", "redundant_with", "redundancy_group_ids", "evidence_group_ids", "evidence_quality_requirements"):
            object.__setattr__(self, field, _ids(getattr(self, field), field))
        if not self.family_ids or not self.target_kinds:
            raise ValidationFailureError("templates must declare family and target-kind applicability")
        if not isinstance(self.context_independent, bool) or not isinstance(self.unit_independent, bool):
            raise ValidationFailureError("context_independent and unit_independent must be booleans")
        if not self.context_independent and not self.supported_contexts:
            raise ValidationFailureError("templates must declare supported contexts or context independence")
        if not self.unit_independent and not self.supported_units:
            raise ValidationFailureError("templates must declare supported units or unit independence")
        if not isinstance(self.required_capabilities, (tuple, list)) or any(not isinstance(item, CapabilityRequirement) for item in self.required_capabilities):
            raise ValidationFailureError("required_capabilities must contain CapabilityRequirement values")
        capabilities = tuple(sorted(self.required_capabilities, key=lambda item: item.capability_id))
        if len({item.capability_id for item in capabilities}) != len(capabilities):
            raise ValidationFailureError("required_capabilities must be unique")
        object.__setattr__(self, "required_capabilities", capabilities)
        if not isinstance(self.candidate_discrimination, (tuple, list)) or any(not isinstance(item, PairDiscrimination) for item in self.candidate_discrimination):
            raise ValidationFailureError("candidate_discrimination must contain PairDiscrimination values")
        rubric = tuple(sorted(self.candidate_discrimination, key=lambda item: item.pair_id))
        if len({item.pair_id for item in rubric}) != len(rubric):
            raise ValidationFailureError("candidate_discrimination pair identities must be unique")
        object.__setattr__(self, "candidate_discrimination", rubric)
        if not isinstance(self.prerequisites, (tuple, list)) or any(not isinstance(item, PrerequisiteRequirement) for item in self.prerequisites):
            raise ValidationFailureError("prerequisites must contain PrerequisiteRequirement values")
        prerequisites = tuple(sorted(self.prerequisites, key=lambda item: item.prerequisite_id))
        if len({item.prerequisite_id for item in prerequisites}) != len(prerequisites):
            raise ValidationFailureError("prerequisite identities must be unique")
        object.__setattr__(self, "prerequisites", prerequisites)
        object.__setattr__(self, "effort_band", _enum(self.effort_band, EffortBand, "effort_band"))
        object.__setattr__(self, "disruption", _enum(self.disruption, DisruptionClass, "disruption"))
        object.__setattr__(self, "path", _enum(self.path, TemplatePath, "path"))
        approval = _optional_id(self.approval_capability, "approval_capability")
        object.__setattr__(self, "approval_capability", approval)
        object.__setattr__(self, "execution_mode", _enum(self.execution_mode, CheckExecutionMode, "execution_mode"))
        if self.execution_mode in {CheckExecutionMode.REQUEST_HUMAN_MEASUREMENT, CheckExecutionMode.REQUEST_APPROVED_EXTERNAL_WORK} and approval is None:
            raise ValidationFailureError("human measurement and approved external work require an approval capability")
        object.__setattr__(self, "reuse_window_seconds", _seconds(self.reuse_window_seconds, "reuse_window_seconds", allow_zero=True))

    def as_dict(self) -> dict[str, object]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "title": self.title,
            "family_ids": list(self.family_ids),
            "target_kinds": list(self.target_kinds),
            "supported_contexts": list(self.supported_contexts),
            "supported_units": list(self.supported_units),
            "context_independent": self.context_independent,
            "unit_independent": self.unit_independent,
            "required_capabilities": [item.as_dict() for item in self.required_capabilities],
            "candidate_discrimination": [item.as_dict() for item in self.candidate_discrimination],
            "prerequisites": [item.as_dict() for item in self.prerequisites],
            "redundant_with": list(self.redundant_with),
            "redundancy_group_ids": list(self.redundancy_group_ids),
            "evidence_group_ids": list(self.evidence_group_ids),
            "effort_band": self.effort_band.value,
            "effort_source_id": self.effort_source_id,
            "turnaround_source_id": self.turnaround_source_id,
            "disruption": self.disruption.value,
            "approval_capability": self.approval_capability,
            "execution_mode": self.execution_mode.value,
            "result_schema_identity": self.result_schema_identity,
            "interpretation_schema_identity": self.interpretation_schema_identity,
            "evidence_quality_requirements": list(self.evidence_quality_requirements),
            "qualification_identity": self.qualification_identity,
            "path": self.path.value,
            "reuse_window_seconds": self.reuse_window_seconds,
        }


@dataclass(frozen=True, slots=True)
class CheckTemplateCatalog:
    catalog_id: str
    version: str
    templates: tuple[CheckTemplate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "catalog_id", _identity(self.catalog_id, "catalog_id"))
        object.__setattr__(self, "version", _identity(self.version, "catalog_version"))
        if not isinstance(self.templates, (tuple, list)) or len(self.templates) > _MAX_TEMPLATES or any(not isinstance(item, CheckTemplate) for item in self.templates):
            raise ValidationFailureError("templates must be a bounded sequence of CheckTemplate values")
        templates = tuple(sorted(self.templates, key=lambda item: item.template_id))
        if len({item.template_id for item in templates}) != len(templates):
            raise ValidationFailureError("catalog template identities must be unique")
        template_ids = {item.template_id for item in templates}
        for item in templates:
            if item.template_id in item.redundant_with or not set(item.redundant_with).issubset(template_ids):
                raise ValidationFailureError("redundant_with must reference other templates in this catalog")
        object.__setattr__(self, "templates", templates)

    def as_dict(self) -> dict[str, object]:
        return {"catalog_id": self.catalog_id, "version": self.version, "templates": [item.as_dict() for item in self.templates]}

    @property
    def identity(self) -> str:
        return _hash(self.as_dict())


@dataclass(frozen=True, slots=True)
class PairWeight:
    pair_id: str
    value: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair_id", _identity(self.pair_id, "pair_id"))
        value = _decimal(self.value, "pair_weight", minimum=Decimal("0"), maximum=Decimal("1"))
        if value == 0:
            raise ValidationFailureError("reviewed ordinal pair weights must be positive")
        object.__setattr__(self, "value", value)

    def as_dict(self) -> dict[str, object]:
        return {"pair_id": self.pair_id, "value": self.value}


@dataclass(frozen=True, slots=True)
class PlannerPolicy:
    policy_id: str
    version: str
    pair_weights: tuple[PairWeight, ...]
    max_recommendations: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _identity(self.policy_id, "policy_id"))
        object.__setattr__(self, "version", _identity(self.version, "policy_version"))
        weights = tuple(sorted(self.pair_weights, key=lambda item: item.pair_id))
        if any(not isinstance(item, PairWeight) for item in weights) or len({item.pair_id for item in weights}) != len(weights):
            raise ValidationFailureError("pair_weights must contain unique PairWeight values")
        object.__setattr__(self, "pair_weights", weights)
        if isinstance(self.max_recommendations, bool) or not isinstance(self.max_recommendations, int) or not 1 <= self.max_recommendations <= 3:
            raise ValidationFailureError("max_recommendations must be between one and three")

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "pair_weights": [item.as_dict() for item in self.pair_weights],
            "max_recommendations": self.max_recommendations,
            "utility_version": ORDINAL_UTILITY_VERSION,
            "utility_weights": _UTILITY_WEIGHTS,
            "ranking_kind": "DETERMINISTIC_ORDINAL_ONLY",
        }

    @property
    def identity(self) -> str:
        return _hash(self.as_dict())


@dataclass(frozen=True, slots=True)
class TargetContext:
    target_identity: str
    context_identity: str | None
    unit_identity: str | None
    characteristic_identity: str | None = None

    def __post_init__(self) -> None:
        for field in ("target_identity", "context_identity", "unit_identity", "characteristic_identity"):
            object.__setattr__(self, field, _optional_id(getattr(self, field), field))
        if self.target_identity is None:
            raise ValidationFailureError("target_identity is required")

    def as_dict(self) -> dict[str, object]:
        return {
            "target_identity": self.target_identity,
            "context_identity": self.context_identity,
            "unit_identity": self.unit_identity,
            "characteristic_identity": self.characteristic_identity,
        }


@dataclass(frozen=True, slots=True)
class UnresolvedHypothesisPair:
    pair_id: str
    hypothesis_a_id: str
    hypothesis_b_id: str
    contradiction_ids: tuple[str, ...] = ()
    dependence_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair_id", _identity(self.pair_id, "pair_id"))
        a = _identity(self.hypothesis_a_id, "hypothesis_a_id")
        b = _identity(self.hypothesis_b_id, "hypothesis_b_id")
        if a == b:
            raise ValidationFailureError("a hypothesis pair must contain two different hypotheses")
        object.__setattr__(self, "hypothesis_a_id", min(a, b))
        object.__setattr__(self, "hypothesis_b_id", max(a, b))
        object.__setattr__(self, "contradiction_ids", _ids(self.contradiction_ids, "contradiction_ids"))
        object.__setattr__(self, "dependence_group_ids", _ids(self.dependence_group_ids, "dependence_group_ids"))

    def as_dict(self) -> dict[str, object]:
        return {
            "pair_id": self.pair_id,
            "hypothesis_a_id": self.hypothesis_a_id,
            "hypothesis_b_id": self.hypothesis_b_id,
            "contradiction_ids": list(self.contradiction_ids),
            "dependence_group_ids": list(self.dependence_group_ids),
        }


@dataclass(frozen=True, slots=True)
class ContradictionFact:
    contradiction_id: str
    pair_id: str
    left_evidence_identity: str
    right_evidence_identity: str

    def __post_init__(self) -> None:
        for field in ("contradiction_id", "pair_id", "left_evidence_identity", "right_evidence_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        if self.left_evidence_identity == self.right_evidence_identity:
            raise ValidationFailureError("a contradiction must reference distinct evidence identities")

    def as_dict(self) -> dict[str, str]:
        return {
            "contradiction_id": self.contradiction_id,
            "pair_id": self.pair_id,
            "left_evidence_identity": self.left_evidence_identity,
            "right_evidence_identity": self.right_evidence_identity,
        }


@dataclass(frozen=True, slots=True)
class EvidenceDependenceGroup:
    evidence_group_id: str
    dependence_group_id: str
    pair_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_group_id", _identity(self.evidence_group_id, "evidence_group_id"))
        object.__setattr__(self, "dependence_group_id", _identity(self.dependence_group_id, "dependence_group_id"))
        object.__setattr__(self, "pair_ids", _ids(self.pair_ids, "pair_ids"))

    def as_dict(self) -> dict[str, object]:
        return {"evidence_group_id": self.evidence_group_id, "dependence_group_id": self.dependence_group_id, "pair_ids": list(self.pair_ids)}


@dataclass(frozen=True, slots=True)
class CapabilityFact:
    capability_id: str
    state: CapabilityState
    source_identity: str
    observed_at: datetime | None
    qualification_identity: str | None
    context_identity: str | None = None
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_id", _identity(self.capability_id, "capability_id"))
        object.__setattr__(self, "state", _enum(self.state, CapabilityState, "state"))
        object.__setattr__(self, "source_identity", _identity(self.source_identity, "source_identity"))
        for field in ("observed_at", "valid_until"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _aware(value, field))
        for field in ("qualification_identity", "context_identity"):
            object.__setattr__(self, field, _optional_id(getattr(self, field), field))
        if self.state == CapabilityState.AVAILABLE and (self.observed_at is None or self.qualification_identity is None):
            raise ValidationFailureError("available capabilities require observation and qualification identities")

    def as_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "state": self.state.value,
            "source_identity": self.source_identity,
            "observed_at": self.observed_at,
            "qualification_identity": self.qualification_identity,
            "context_identity": self.context_identity,
            "valid_until": self.valid_until,
        }


@dataclass(frozen=True, slots=True)
class QualificationFact:
    qualification_identity: str
    state: QualificationState
    source_identity: str
    valid_until: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "qualification_identity", _identity(self.qualification_identity, "qualification_identity"))
        object.__setattr__(self, "state", _enum(self.state, QualificationState, "state"))
        object.__setattr__(self, "source_identity", _identity(self.source_identity, "source_identity"))
        if self.valid_until is not None:
            object.__setattr__(self, "valid_until", _aware(self.valid_until, "valid_until"))

    def as_dict(self) -> dict[str, object]:
        return {"qualification_identity": self.qualification_identity, "state": self.state.value, "source_identity": self.source_identity, "valid_until": self.valid_until}


@dataclass(frozen=True, slots=True)
class PrerequisiteFact:
    prerequisite_id: str
    state: PrerequisiteState
    source_identity: str
    observed_at: datetime | None
    target_identity: str | None = None
    context_identity: str | None = None
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "prerequisite_id", _identity(self.prerequisite_id, "prerequisite_id"))
        object.__setattr__(self, "state", _enum(self.state, PrerequisiteState, "state"))
        object.__setattr__(self, "source_identity", _identity(self.source_identity, "source_identity"))
        for field in ("observed_at", "valid_until"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _aware(value, field))
        for field in ("target_identity", "context_identity"):
            object.__setattr__(self, field, _optional_id(getattr(self, field), field))

    def as_dict(self) -> dict[str, object]:
        return {
            "prerequisite_id": self.prerequisite_id,
            "state": self.state.value,
            "source_identity": self.source_identity,
            "observed_at": self.observed_at,
            "target_identity": self.target_identity,
            "context_identity": self.context_identity,
            "valid_until": self.valid_until,
        }


@dataclass(frozen=True, slots=True)
class EvidenceValidityFact:
    check_id: str
    cycle_id: str
    state: EvidenceValidityState
    valid_until: datetime | None
    target_context: TargetContext

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", _identity(self.check_id, "check_id"))
        object.__setattr__(self, "cycle_id", _identity(self.cycle_id, "cycle_id"))
        object.__setattr__(self, "state", _enum(self.state, EvidenceValidityState, "state"))
        if self.valid_until is not None:
            object.__setattr__(self, "valid_until", _aware(self.valid_until, "valid_until"))
        if not isinstance(self.target_context, TargetContext):
            raise ValidationFailureError("target_context must be a TargetContext")

    def as_dict(self) -> dict[str, object]:
        return {"check_id": self.check_id, "cycle_id": self.cycle_id, "state": self.state.value, "valid_until": self.valid_until, "target_context": self.target_context.as_dict()}


@dataclass(frozen=True, slots=True)
class DecisionDeadlineFact:
    state: DeadlineState
    source_identity: str
    deadline: datetime | None
    unknown_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _enum(self.state, DeadlineState, "state"))
        object.__setattr__(self, "source_identity", _identity(self.source_identity, "source_identity"))
        if self.deadline is not None:
            object.__setattr__(self, "deadline", _aware(self.deadline, "deadline"))
        reason = _optional_id(self.unknown_reason, "unknown_reason")
        object.__setattr__(self, "unknown_reason", reason)
        if (self.state == DeadlineState.SUPPORTED) != (self.deadline is not None):
            raise ValidationFailureError("supported deadlines require a time; unknown deadlines must omit it")
        if self.state == DeadlineState.UNKNOWN and reason is None:
            raise ValidationFailureError("unknown deadlines require an explicit reason")

    def as_dict(self) -> dict[str, object]:
        return {"state": self.state.value, "source_identity": self.source_identity, "deadline": self.deadline, "unknown_reason": self.unknown_reason}


@dataclass(frozen=True, slots=True)
class TurnaroundFact:
    source_identity: str
    state: TurnaroundState
    seconds: int | None
    valid_until: datetime | None
    unknown_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_identity", _identity(self.source_identity, "source_identity"))
        object.__setattr__(self, "state", _enum(self.state, TurnaroundState, "state"))
        if self.seconds is not None:
            object.__setattr__(self, "seconds", _seconds(self.seconds, "turnaround_seconds"))
        if self.valid_until is not None:
            object.__setattr__(self, "valid_until", _aware(self.valid_until, "valid_until"))
        reason = _optional_id(self.unknown_reason, "unknown_reason")
        object.__setattr__(self, "unknown_reason", reason)
        if self.state == TurnaroundState.SUPPORTED and self.seconds is None:
            raise ValidationFailureError("supported turnaround requires positive seconds")
        if self.state != TurnaroundState.SUPPORTED and self.seconds is not None:
            raise ValidationFailureError("non-supported turnaround cannot carry a duration")
        if self.state in {TurnaroundState.UNKNOWN, TurnaroundState.UNSUPPORTED} and reason is None:
            raise ValidationFailureError("unknown or unsupported turnaround requires an explicit reason")

    def as_dict(self) -> dict[str, object]:
        return {
            "source_identity": self.source_identity,
            "state": self.state.value,
            "seconds": self.seconds,
            "valid_until": self.valid_until,
            "unknown_reason": self.unknown_reason,
        }


@dataclass(frozen=True, slots=True)
class UnknownFact:
    fact_identity: str
    source_identity: str
    reason: str

    def __post_init__(self) -> None:
        for field in ("fact_identity", "source_identity", "reason"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))

    def as_dict(self) -> dict[str, str]:
        return {"fact_identity": self.fact_identity, "source_identity": self.source_identity, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class PlannerReadFacts:
    """Bounded current source facts pinned to the exact O5 workflow revision."""

    episode_id: str
    cycle_id: str
    workflow_version: int
    viewed_revisions: RevisionVector
    as_of: datetime
    family_id: str
    target_kind: str
    target_context: TargetContext
    unresolved_pairs: tuple[UnresolvedHypothesisPair, ...]
    contradictions: tuple[ContradictionFact, ...]
    dependence_groups: tuple[EvidenceDependenceGroup, ...]
    capability_facts: tuple[CapabilityFact, ...]
    qualification_facts: tuple[QualificationFact, ...]
    prerequisite_facts: tuple[PrerequisiteFact, ...]
    evidence_validity_facts: tuple[EvidenceValidityFact, ...]
    decision_deadline: DecisionDeadlineFact
    turnaround_facts: tuple[TurnaroundFact, ...]
    explicit_unknowns: tuple[UnknownFact, ...] = ()

    def __post_init__(self) -> None:
        for field in ("episode_id", "cycle_id", "family_id", "target_kind"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        if isinstance(self.workflow_version, bool) or not isinstance(self.workflow_version, int) or self.workflow_version < 0:
            raise ValidationFailureError("workflow_version must be a non-negative integer")
        if not isinstance(self.viewed_revisions, RevisionVector):
            raise ValidationFailureError("viewed_revisions must be a RevisionVector")
        if self.viewed_revisions.workflow_version != self.workflow_version:
            raise ValidationFailureError("viewed revision workflow_version must equal workflow_version")
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))
        if not isinstance(self.target_context, TargetContext) or not isinstance(self.decision_deadline, DecisionDeadlineFact):
            raise ValidationFailureError("target_context and decision_deadline must use their typed contracts")
        for field, key in (
            ("unresolved_pairs", "pair_id"),
            ("contradictions", "contradiction_id"),
            ("dependence_groups", "evidence_group_id"),
            ("capability_facts", "capability_id"),
            ("qualification_facts", "qualification_identity"),
            ("prerequisite_facts", "prerequisite_id"),
            ("evidence_validity_facts", "check_id"),
            ("turnaround_facts", "source_identity"),
            ("explicit_unknowns", "fact_identity"),
        ):
            values = getattr(self, field)
            expected_types = {
                "unresolved_pairs": UnresolvedHypothesisPair,
                "contradictions": ContradictionFact,
                "dependence_groups": EvidenceDependenceGroup,
                "capability_facts": CapabilityFact,
                "qualification_facts": QualificationFact,
                "prerequisite_facts": PrerequisiteFact,
                "evidence_validity_facts": EvidenceValidityFact,
                "turnaround_facts": TurnaroundFact,
                "explicit_unknowns": UnknownFact,
            }
            if not isinstance(values, (tuple, list)) or len(values) > _MAX_FACTS or any(not isinstance(item, expected_types[field]) for item in values):
                raise ValidationFailureError(f"{field} must be a bounded sequence of typed facts")
            sort_key = (lambda item: (item.cycle_id, item.check_id)) if field == "evidence_validity_facts" else (lambda item: getattr(item, key))
            values = tuple(sorted(values, key=sort_key))
            identity_key = (lambda item: (item.cycle_id, item.check_id)) if field == "evidence_validity_facts" else (lambda item: getattr(item, key))
            if len({identity_key(item) for item in values}) != len(values):
                raise ValidationFailureError(f"{field} must be bounded and have unique identities")
            object.__setattr__(self, field, values)
        pair_ids = {item.pair_id for item in self.unresolved_pairs}
        if any(item.pair_id not in pair_ids for item in self.contradictions):
            raise ValidationFailureError("contradictions must reference unresolved hypothesis pairs")
        if any(not set(group.pair_ids).issubset(pair_ids) for group in self.dependence_groups):
            raise ValidationFailureError("dependence groups must reference unresolved hypothesis pairs")
        unknown_ids = {item.fact_identity for item in self.explicit_unknowns}
        if self.target_context.context_identity is None and "context" not in unknown_ids:
            raise ValidationFailureError("unknown context must be represented by an explicit UnknownFact")
        if self.target_context.unit_identity is None and "unit" not in unknown_ids:
            raise ValidationFailureError("unknown unit must be represented by an explicit UnknownFact")

    def as_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "cycle_id": self.cycle_id,
            "workflow_version": self.workflow_version,
            "viewed_revisions": self.viewed_revisions,
            "as_of": self.as_of,
            "family_id": self.family_id,
            "target_kind": self.target_kind,
            "target_context": self.target_context.as_dict(),
            "unresolved_pairs": [item.as_dict() for item in self.unresolved_pairs],
            "contradictions": [item.as_dict() for item in self.contradictions],
            "dependence_groups": [item.as_dict() for item in self.dependence_groups],
            "capability_facts": [item.as_dict() for item in self.capability_facts],
            "qualification_facts": [item.as_dict() for item in self.qualification_facts],
            "prerequisite_facts": [item.as_dict() for item in self.prerequisite_facts],
            "evidence_validity_facts": [item.as_dict() for item in self.evidence_validity_facts],
            "decision_deadline": self.decision_deadline.as_dict(),
            "turnaround_facts": [item.as_dict() for item in self.turnaround_facts],
            "explicit_unknowns": [item.as_dict() for item in self.explicit_unknowns],
        }


@dataclass(frozen=True, slots=True)
class ScoreTerms:
    discrimination: Decimal
    independent_coverage: Decimal
    feasibility: Decimal
    effort_disruption: Decimal
    redundancy: Decimal
    utility: Decimal

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "D_ordinal_discrimination": self.discrimination,
            "N_independent_coverage": self.independent_coverage,
            "F_feasibility": self.feasibility,
            "E_effort_disruption": self.effort_disruption,
            "R_redundancy": self.redundancy,
            "U_ordinal_utility": self.utility,
        }


@dataclass(frozen=True, slots=True)
class Recommendation:
    rank: int
    template_id: str
    template_version: str
    title: str
    execution_mode: str
    score: ScoreTerms
    alternatives_discriminated: tuple[dict[str, object], ...]
    why_eligible_now: tuple[str, ...]
    capability_and_prerequisite_facts: tuple[dict[str, object], ...]
    effort_turnaround_disruption: dict[str, object]
    independent_evidence_added: tuple[str, ...]
    will_not_resolve: tuple[str, ...]
    policy_identity: str
    template_catalog_identity: str
    ranking_kind: str = "DETERMINISTIC_ORDINAL_ONLY"

    def as_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "title": self.title,
            "execution_mode": self.execution_mode,
            "score": self.score.as_dict(),
            "alternatives_discriminated": list(self.alternatives_discriminated),
            "why_eligible_now": list(self.why_eligible_now),
            "capability_and_prerequisite_facts": list(self.capability_and_prerequisite_facts),
            "effort_turnaround_disruption": self.effort_turnaround_disruption,
            "independent_evidence_added": list(self.independent_evidence_added),
            "will_not_resolve": list(self.will_not_resolve),
            "policy_identity": self.policy_identity,
            "template_catalog_identity": self.template_catalog_identity,
            "ranking_kind": self.ranking_kind,
        }


@dataclass(frozen=True, slots=True)
class ExcludedCheck:
    template_id: str
    template_version: str
    reasons: tuple[ExclusionReason, ...]
    facts: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "template_id": self.template_id,
            "template_version": self.template_version,
            "reasons": [item.value for item in self.reasons],
            "facts": list(self.facts),
        }


@dataclass(frozen=True, slots=True)
class PlannerPlan:
    plan_identity: str
    input_identity: str
    episode_id: str
    cycle_id: str
    workflow_version: int
    viewed_revisions: RevisionVector
    policy_identity: str
    template_catalog_identity: str
    recommendations: tuple[Recommendation, ...]
    excluded_checks: tuple[ExcludedCheck, ...]
    ranking_kind: str = "DETERMINISTIC_ORDINAL_ONLY"
    interpretation_limit: str = "Ordinal ranking only; not probability, causality, or scientific confidence."

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_identity": self.plan_identity,
            "input_identity": self.input_identity,
            "episode_id": self.episode_id,
            "cycle_id": self.cycle_id,
            "workflow_version": self.workflow_version,
            "viewed_revisions": self.viewed_revisions,
            "policy_identity": self.policy_identity,
            "template_catalog_identity": self.template_catalog_identity,
            "recommendations": [item.as_dict() for item in self.recommendations],
            "excluded_checks": [item.as_dict() for item in self.excluded_checks],
            "ranking_kind": self.ranking_kind,
            "interpretation_limit": self.interpretation_limit,
        }


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _context_matches(record: Mapping[str, Any], context: TargetContext) -> bool:
    target = record.get("target_context")
    if not isinstance(target, Mapping):
        return False
    expected = context.as_dict()
    return all(target.get(key) == value for key, value in expected.items())


def _workflow_checks(snapshot: Any) -> tuple[dict[str, Any], ...]:
    loop = snapshot.state.get("decision_loop", {})
    cycles = loop.get("cycles", ()) if isinstance(loop, Mapping) else ()
    checks: list[dict[str, Any]] = []
    for cycle in cycles if isinstance(cycles, (tuple, list)) else ():
        cycle_checks = cycle.get("checks", {}) if isinstance(cycle, Mapping) else {}
        if isinstance(cycle_checks, Mapping):
            for check in cycle_checks.values():
                if isinstance(check, Mapping):
                    checks.append(dict(check))
    return tuple(sorted(checks, key=lambda item: (str(item.get("template_id", "")), str(item.get("check_id", "")))))


def _workflow_actions(snapshot: Any) -> tuple[dict[str, Any], ...]:
    loop = snapshot.state.get("decision_loop", {})
    cycles = loop.get("cycles", ()) if isinstance(loop, Mapping) else ()
    actions: list[dict[str, Any]] = []
    for cycle in cycles if isinstance(cycles, (tuple, list)) else ():
        cycle_actions = cycle.get("actions", {}) if isinstance(cycle, Mapping) else {}
        if isinstance(cycle_actions, Mapping):
            for action in cycle_actions.values():
                if isinstance(action, Mapping):
                    actions.append(dict(action))
    return tuple(sorted(actions, key=lambda item: str(item.get("action_id", ""))))


_EFFORT_COST = {
    EffortBand.LOW: Decimal("0.25"),
    EffortBand.MODERATE: Decimal("0.50"),
    EffortBand.HIGH: Decimal("0.75"),
    EffortBand.UNKNOWN: Decimal("1"),
}
_DISRUPTION_COST = {
    DisruptionClass.NONE: Decimal("0"),
    DisruptionClass.LOW: Decimal("0.25"),
    DisruptionClass.MODERATE: Decimal("0.50"),
    DisruptionClass.HIGH: Decimal("0.75"),
    DisruptionClass.UNKNOWN: Decimal("1"),
}


class NextCheckPlannerService:
    """Read and rank checks against one current authorized O5 workflow version."""

    def __init__(self, decision_loop: DecisionLoopCommandService):
        if not isinstance(decision_loop, DecisionLoopCommandService):
            raise TypeError("decision_loop must be the existing DecisionLoopCommandService")
        self.decision_loop = decision_loop

    def plan(
        self,
        principal: Principal,
        scope: AccessScope,
        episode_id: str,
        *,
        expected_workflow_version: int,
        viewed_revisions: RevisionVector,
        facts: PlannerReadFacts,
        policy: PlannerPolicy,
        catalog: CheckTemplateCatalog,
    ) -> PlannerPlan:
        episode_id = _identity(episode_id, "episode_id")
        if isinstance(expected_workflow_version, bool) or not isinstance(expected_workflow_version, int) or expected_workflow_version < 0:
            raise ValidationFailureError("expected_workflow_version must be a non-negative integer")
        if not isinstance(viewed_revisions, RevisionVector):
            raise ValidationFailureError("viewed_revisions must be a RevisionVector")
        if not all((isinstance(facts, PlannerReadFacts), isinstance(policy, PlannerPolicy), isinstance(catalog, CheckTemplateCatalog))):
            raise ValidationFailureError("planner requires typed current facts, policy, and template catalog")
        if facts.episode_id != episode_id or facts.workflow_version != expected_workflow_version or facts.viewed_revisions != viewed_revisions:
            raise CoherentReadConflictError("planner facts are not bound to the requested Episode workflow view")

        snapshot = self.decision_loop.get_decision_loop(principal, scope, episode_id)
        self._require_exact_view(snapshot, expected_workflow_version, viewed_revisions, facts)
        pair_ids = {item.pair_id for item in facts.unresolved_pairs}
        weights = {item.pair_id: item.value for item in policy.pair_weights}
        if not pair_ids.issubset(weights):
            raise ValidationFailureError("planner policy has no reviewed ordinal weight for an unresolved pair")

        workflow_checks = _workflow_checks(snapshot)
        workflow_actions = _workflow_actions(snapshot)
        policy_identity = policy.identity
        catalog_identity = catalog.identity
        initial_material = {
            "schema_version": PLANNER_SCHEMA_VERSION,
            "episode_id": episode_id,
            "scope": scope,
            "cycle_id": facts.cycle_id,
            "workflow_version": expected_workflow_version,
            "viewed_revisions": viewed_revisions,
            "authorization_context": {
                "subject": principal.subject,
                "auth_session_revision": principal.auth_session_revision,
                "security_revision": principal.security_revision,
            },
            "durable_workflow_state": snapshot.state,
            "durable_checks": workflow_checks,
            "durable_actions": workflow_actions,
            "planner_facts": facts.as_dict(),
            "planner_policy": policy.as_dict(),
            "planner_policy_identity": policy_identity,
            "template_catalog": catalog.as_dict(),
            "template_catalog_identity": catalog_identity,
        }
        input_identity = _hash(initial_material)

        base_candidates: list[tuple[CheckTemplate, tuple[dict[str, object], ...], tuple[str, ...]]] = []
        excluded: dict[str, ExcludedCheck] = {}
        for template in catalog.templates:
            reasons, facts_text = self._eligibility(template, principal, scope, snapshot, facts, workflow_checks)
            if reasons:
                excluded[template.template_id] = ExcludedCheck(template.template_id, template.version, reasons, facts_text)
            else:
                alternatives = self._alternatives(template, facts)
                base_candidates.append((template, alternatives, facts_text))

        recommendations: list[Recommendation] = []
        covered_pairs: set[str] = set()
        covered_groups: set[str] = set()
        while base_candidates and len(recommendations) < policy.max_recommendations:
            scored: list[tuple[CheckTemplate, ScoreTerms, tuple[dict[str, object], ...], tuple[str, ...], int | None]] = []
            for template, alternatives, facts_text in base_candidates:
                terms, turnaround = self._score(template, facts, weights, covered_pairs, covered_groups)
                scored.append((template, terms, alternatives, facts_text, turnaround))
            front = self._pareto_front(scored)
            scored.sort(key=lambda item: self._rank_key(item, front))
            chosen = scored[0]
            template, terms, alternatives, facts_text, turnaround = chosen
            base_candidates = [item for item in base_candidates if item[0].template_id != template.template_id]
            newly_covered_groups = tuple(sorted(self._template_independent_groups(template, facts) - covered_groups))
            covered_pairs.update(item.pair_id for item in template.candidate_discrimination if item.value > 0 and item.pair_id in pair_ids)
            covered_groups.update(newly_covered_groups)
            recommendations.append(
                self._recommendation(
                    len(recommendations) + 1,
                    template,
                    terms,
                    alternatives,
                    newly_covered_groups,
                    facts,
                    policy_identity,
                    catalog_identity,
                    turnaround,
                )
            )
            if len(recommendations) < policy.max_recommendations:
                retained: list[tuple[CheckTemplate, tuple[dict[str, object], ...], tuple[str, ...]]] = []
                for candidate in base_candidates:
                    other = candidate[0]
                    common_redundancy = set(other.redundancy_group_ids) & set(template.redundancy_group_ids)
                    common_dependence = self._template_independent_groups(other, facts) & self._template_independent_groups(template, facts)
                    if template.template_id in other.redundant_with or other.template_id in template.redundant_with or common_redundancy or common_dependence:
                        excluded[other.template_id] = ExcludedCheck(
                            other.template_id,
                            other.version,
                            (ExclusionReason.REDUNDANT_WITH_SELECTED,),
                            (f"selected_template:{template.template_id}", *tuple(f"shared_redundancy_group:{item}" for item in sorted(common_redundancy)), *tuple(f"shared_dependence_group:{item}" for item in sorted(common_dependence))),
                        )
                    else:
                        retained.append(candidate)
                base_candidates = retained

        for template, _alternatives, _facts_text in base_candidates:
            excluded[template.template_id] = ExcludedCheck(
                template.template_id,
                template.version,
                (ExclusionReason.NOT_SELECTED_LIMIT,),
                (f"maximum_recommendations:{policy.max_recommendations}",),
            )

        ordered_excluded = tuple(excluded[key] for key in sorted(excluded))
        self._require_still_current(principal, scope, episode_id, expected_workflow_version, viewed_revisions, facts)
        result_material = {
            "input_identity": input_identity,
            "recommendations": [item.as_dict() for item in recommendations],
            "excluded_checks": [item.as_dict() for item in ordered_excluded],
        }
        plan_identity = _hash(result_material)
        return PlannerPlan(
            plan_identity,
            input_identity,
            episode_id,
            facts.cycle_id,
            expected_workflow_version,
            viewed_revisions,
            policy_identity,
            catalog_identity,
            tuple(recommendations),
            ordered_excluded,
        )

    @staticmethod
    def _require_exact_view(snapshot: Any, expected: int, revisions: RevisionVector, facts: PlannerReadFacts) -> None:
        if snapshot.aggregate_version != expected:
            raise CoherentReadConflictError("planner workflow version is stale")
        if snapshot.revision_vector is None or snapshot.revision_vector != revisions or revisions.workflow_version != expected:
            raise CoherentReadConflictError("planner viewed revisions do not match the current Episode view")
        if snapshot.active_cycle_id != facts.cycle_id:
            raise CoherentReadConflictError("planner cycle is not the active Episode cycle")

    def _require_still_current(
        self,
        principal: Principal,
        scope: AccessScope,
        episode_id: str,
        expected: int,
        revisions: RevisionVector,
        facts: PlannerReadFacts,
    ) -> None:
        # Re-resolve current permission and workflow truth immediately before disclosure.
        snapshot = self.decision_loop.get_decision_loop(principal, scope, episode_id)
        self._require_exact_view(snapshot, expected, revisions, facts)

    @staticmethod
    def _template_independent_groups(template: CheckTemplate, facts: PlannerReadFacts) -> set[str]:
        group_map = {item.evidence_group_id: item.dependence_group_id for item in facts.dependence_groups}
        return {group_map[group_id] for group_id in template.evidence_group_ids if group_id in group_map}

    def _eligibility(
        self,
        template: CheckTemplate,
        principal: Principal,
        scope: AccessScope,
        snapshot: Any,
        facts: PlannerReadFacts,
        checks: tuple[dict[str, Any], ...],
    ) -> tuple[tuple[ExclusionReason, ...], tuple[str, ...]]:
        reasons: set[ExclusionReason] = set()
        trace: list[str] = []
        if facts.family_id not in template.family_ids or facts.target_kind not in template.target_kinds:
            reasons.add(ExclusionReason.UNSUPPORTED_CONTEXT)
            trace.append("family_or_target_kind_not_supported")
        if facts.target_context.context_identity is None:
            if not template.context_independent:
                reasons.add(ExclusionReason.UNSUPPORTED_CONTEXT)
                trace.append("context_unknown_and_template_requires_context")
        elif not template.context_independent and facts.target_context.context_identity not in template.supported_contexts:
            reasons.add(ExclusionReason.UNSUPPORTED_CONTEXT)
            trace.append("context_not_in_template_support")
        if facts.target_context.unit_identity is None:
            if not template.unit_independent:
                reasons.add(ExclusionReason.UNSUPPORTED_UNIT)
                trace.append("unit_unknown_and_template_requires_unit")
        elif not template.unit_independent and facts.target_context.unit_identity not in template.supported_units:
            reasons.add(ExclusionReason.UNSUPPORTED_UNIT)
            trace.append("unit_not_in_template_support")

        loop = snapshot.state.get("decision_loop", {})
        cycles = loop.get("cycles", ()) if isinstance(loop, Mapping) else ()
        active_cycle = next((cycle for cycle in cycles if isinstance(cycle, Mapping) and cycle.get("cycle_id") == facts.cycle_id), {})
        if snapshot.state.get("work_state") == "CLOSED" or active_cycle.get("status") != "OPEN":
            reasons.add(ExclusionReason.LIFECYCLE_CLOSED)
            trace.append("episode_or_cycle_not_open")

        capabilities = {item.capability_id: item for item in facts.capability_facts}
        qualifications = {item.qualification_identity: item for item in facts.qualification_facts}
        for requirement in template.required_capabilities:
            fact = capabilities.get(requirement.capability_id)
            role_is_peer = requirement.role in {CapabilityRole.PEER, CapabilityRole.REFERENCE}
            if fact is None:
                reasons.add(ExclusionReason.MISSING_CAPABILITY)
                trace.append(f"missing_capability:{requirement.capability_id}")
                continue
            if role_is_peer and (
                fact.context_identity is None
                or facts.target_context.context_identity is None
                or fact.context_identity != facts.target_context.context_identity
            ):
                reasons.add(ExclusionReason.MISSING_CAPABILITY)
                trace.append(f"peer_reference_not_bound_to_current_context:{requirement.capability_id}")
                continue
            if fact.context_identity is not None and facts.target_context.context_identity != fact.context_identity:
                reasons.add(ExclusionReason.MISSING_CAPABILITY)
                trace.append(f"capability_context_mismatch:{requirement.capability_id}")
                continue
            if fact.state == CapabilityState.UNAVAILABLE or fact.state == CapabilityState.UNKNOWN:
                reasons.add(ExclusionReason.CAPABILITY_UNAVAILABLE)
                trace.append(f"capability_unavailable:{requirement.capability_id}:{fact.state.value}")
                continue
            if fact.state == CapabilityState.UNQUALIFIED:
                reasons.add(ExclusionReason.UNQUALIFIED_PEER_REFERENCE if role_is_peer else ExclusionReason.UNQUALIFIED_CAPABILITY)
                trace.append(f"capability_unqualified:{requirement.capability_id}")
                continue
            if fact.state == CapabilityState.STALE or fact.observed_at is None or facts.as_of < fact.observed_at:
                reasons.add(ExclusionReason.STALE_PEER_REFERENCE if role_is_peer else ExclusionReason.STALE_EVIDENCE)
                trace.append(f"capability_stale:{requirement.capability_id}")
                continue
            if fact.valid_until is not None and facts.as_of >= fact.valid_until:
                reasons.add(ExclusionReason.STALE_PEER_REFERENCE if role_is_peer else ExclusionReason.STALE_EVIDENCE)
                trace.append(f"capability_expired:{requirement.capability_id}")
                continue
            if (facts.as_of - fact.observed_at).total_seconds() > requirement.max_age_seconds:
                reasons.add(ExclusionReason.STALE_PEER_REFERENCE if role_is_peer else ExclusionReason.STALE_EVIDENCE)
                trace.append(f"capability_age_exceeded:{requirement.capability_id}")
                continue
            if fact.qualification_identity is None:
                reasons.add(ExclusionReason.UNQUALIFIED_PEER_REFERENCE if role_is_peer else ExclusionReason.UNQUALIFIED_CAPABILITY)
                trace.append(f"capability_missing_qualification:{requirement.capability_id}")
                continue
            qualification = qualifications.get(fact.qualification_identity)
            if qualification is None:
                reasons.add(ExclusionReason.MISSING_QUALIFICATION)
                trace.append(f"missing_qualification:{fact.qualification_identity}")
                continue
            if qualification.state != QualificationState.QUALIFIED:
                reasons.add(ExclusionReason.UNQUALIFIED_PEER_REFERENCE if role_is_peer else ExclusionReason.UNQUALIFIED_CAPABILITY)
                trace.append(f"qualification_not_valid:{fact.qualification_identity}")
                continue
            if qualification.valid_until is not None and facts.as_of >= qualification.valid_until:
                reasons.add(ExclusionReason.STALE_PEER_REFERENCE if role_is_peer else ExclusionReason.STALE_EVIDENCE)
                trace.append(f"qualification_expired:{fact.qualification_identity}")

        qualification = qualifications.get(template.qualification_identity)
        if qualification is None:
            reasons.add(ExclusionReason.MISSING_QUALIFICATION)
            trace.append(f"missing_template_qualification:{template.qualification_identity}")
        elif qualification.state != QualificationState.QUALIFIED:
            reasons.add(ExclusionReason.UNQUALIFIED_CAPABILITY)
            trace.append(f"template_qualification_not_valid:{template.qualification_identity}")
        elif qualification.valid_until is not None and facts.as_of >= qualification.valid_until:
            reasons.add(ExclusionReason.STALE_EVIDENCE)
            trace.append(f"template_qualification_expired:{template.qualification_identity}")

        prereqs = {item.prerequisite_id: item for item in facts.prerequisite_facts}
        for requirement in template.prerequisites:
            fact = prereqs.get(requirement.prerequisite_id)
            if fact is None or fact.state == PrerequisiteState.MISSING:
                reasons.add(ExclusionReason.MISSING_PREREQUISITE)
                trace.append(f"missing_prerequisite:{requirement.prerequisite_id}")
                continue
            if fact.state == PrerequisiteState.UNKNOWN:
                reasons.add(ExclusionReason.UNKNOWN_PREREQUISITE)
                trace.append(f"unknown_prerequisite:{requirement.prerequisite_id}")
                continue
            if fact.state == PrerequisiteState.STALE:
                reasons.add(ExclusionReason.STALE_PREREQUISITE)
                trace.append(f"stale_prerequisite:{requirement.prerequisite_id}")
                continue
            if requirement.context_bound and (fact.target_identity != facts.target_context.target_identity or fact.context_identity != facts.target_context.context_identity):
                reasons.add(ExclusionReason.MISSING_PREREQUISITE)
                trace.append(f"prerequisite_context_mismatch:{requirement.prerequisite_id}")
                continue
            if fact.observed_at is not None and facts.as_of < fact.observed_at:
                reasons.add(ExclusionReason.STALE_PREREQUISITE)
                trace.append(f"prerequisite_from_future:{requirement.prerequisite_id}")
            elif requirement.max_age_seconds is not None and (fact.observed_at is None or (facts.as_of - fact.observed_at).total_seconds() > requirement.max_age_seconds):
                reasons.add(ExclusionReason.STALE_PREREQUISITE)
                trace.append(f"prerequisite_age_exceeded:{requirement.prerequisite_id}")
            elif fact.valid_until is not None and facts.as_of >= fact.valid_until:
                reasons.add(ExclusionReason.STALE_PREREQUISITE)
                trace.append(f"prerequisite_expired:{requirement.prerequisite_id}")

        approval_capability = template.approval_capability
        if approval_capability is not None:
            if not principal.has_capability(approval_capability):
                reasons.add(ExclusionReason.MISSING_APPROVAL_AUTHORIZATION)
                trace.append(f"approval_capability_missing:{approval_capability}")
            else:
                try:
                    self.decision_loop.current_authorization.authorize(principal, scope, approval_capability)
                except AuthorizationDeniedError:
                    # A read revocation blocks the whole operation; a specific missing approval right is an exclusion.
                    self.decision_loop.current_authorization.authorize(principal, scope, DECISION_LOOP_READ_CAPABILITY)
                    reasons.add(ExclusionReason.MISSING_APPROVAL_AUTHORIZATION)
                    trace.append(f"approval_capability_not_current:{approval_capability}")

        pair_ids = {item.pair_id for item in facts.unresolved_pairs}
        rubric_ids = {item.pair_id for item in template.candidate_discrimination}
        if facts.unresolved_pairs and template.path == TemplatePath.DISCRIMINATION and not (pair_ids & rubric_ids):
            reasons.add(ExclusionReason.NO_VALIDATION_PATH)
            trace.append("template_has_no_rubric_for_unresolved_pairs")
        if not facts.unresolved_pairs and template.path != TemplatePath.PREREQUISITE_VALIDATION:
            reasons.add(ExclusionReason.NO_VALIDATION_PATH)
            trace.append("no_unresolved_pair_requires_validation_path")

        turnaround = next((item for item in facts.turnaround_facts if item.source_identity == template.turnaround_source_id), None)
        if turnaround is None:
            unknown = any(item.fact_identity == f"turnaround:{template.turnaround_source_id}" for item in facts.explicit_unknowns)
            if not unknown:
                reasons.add(ExclusionReason.UNSUPPORTED_TURNAROUND)
                trace.append(f"turnaround_unknown_without_explicit_fact:{template.turnaround_source_id}")
            elif facts.decision_deadline.state == DeadlineState.SUPPORTED:
                reasons.add(ExclusionReason.MISSED_DECISION_WINDOW)
                trace.append("unknown_turnaround_cannot_prove_deadline")
        elif turnaround.state == TurnaroundState.UNSUPPORTED:
            reasons.add(ExclusionReason.UNSUPPORTED_TURNAROUND)
            trace.append(f"turnaround_unsupported:{turnaround.unknown_reason}")
        elif turnaround.state == TurnaroundState.STALE or (turnaround.valid_until is not None and facts.as_of >= turnaround.valid_until):
            reasons.add(ExclusionReason.STALE_EVIDENCE)
            trace.append(f"turnaround_stale:{turnaround.source_identity}")
        elif facts.decision_deadline.state == DeadlineState.SUPPORTED:
            deadline = facts.decision_deadline.deadline
            if facts.as_of >= deadline or turnaround.state != TurnaroundState.SUPPORTED or facts.as_of + timedelta(seconds=turnaround.seconds or 0) > deadline:
                reasons.add(ExclusionReason.MISSED_DECISION_WINDOW)
                trace.append(f"not_proven_timely:{template.turnaround_source_id}")

        if turnaround is not None and turnaround.state == TurnaroundState.UNKNOWN and facts.decision_deadline.state == DeadlineState.SUPPORTED:
            reasons.add(ExclusionReason.MISSED_DECISION_WINDOW)
            trace.append(f"unknown_turnaround_cannot_prove_deadline:{turnaround.unknown_reason}")

        for check in checks:
            if check.get("template_id") != template.template_id or not _context_matches(check, facts.target_context):
                continue
            status = check.get("status")
            if status == "REQUESTED":
                reasons.add(ExclusionReason.CHECK_ALREADY_REQUESTED)
                trace.append(f"existing_check_requested:{check.get('check_id')}")
            elif status == "STARTED":
                reasons.add(ExclusionReason.CHECK_IN_PROGRESS)
                trace.append(f"existing_check_started:{check.get('check_id')}")
            elif status == "COMPLETED" and check.get("template_version") == template.version:
                result = check.get("result") if isinstance(check.get("result"), Mapping) else {}
                outcome = result.get("outcome")
                completed_at = _timestamp(check.get("completed_at"))
                validity = next(
                    (
                        item
                        for item in facts.evidence_validity_facts
                        if item.check_id == check.get("check_id") and item.cycle_id == check.get("cycle_id")
                    ),
                    None,
                )
                non_evidence_outcomes = {"FAILED", "UNAVAILABLE", "UNKNOWN", "CANCELLED"}
                if outcome not in non_evidence_outcomes and completed_at is not None and facts.as_of >= completed_at and facts.as_of <= completed_at + timedelta(seconds=template.reuse_window_seconds):
                    if (
                        validity is not None
                        and validity.state == EvidenceValidityState.VALID
                        and validity.target_context == facts.target_context
                        and (validity.valid_until is None or facts.as_of < validity.valid_until)
                    ):
                        reasons.add(ExclusionReason.VALID_PRIOR_COMPLETION)
                        trace.append(f"valid_same_context_completion:{check.get('check_id')}")

        order = {reason: index for index, reason in enumerate(ExclusionReason)}
        return tuple(sorted(reasons, key=lambda item: order[item])), tuple(sorted(set(trace)))

    @staticmethod
    def _alternatives(template: CheckTemplate, facts: PlannerReadFacts) -> tuple[dict[str, object], ...]:
        rubric = {item.pair_id: item.value for item in template.candidate_discrimination}
        return tuple(
            {
                "pair_id": pair.pair_id,
                "hypothesis_a_id": pair.hypothesis_a_id,
                "hypothesis_b_id": pair.hypothesis_b_id,
                "ordinal_discrimination": rubric.get(pair.pair_id, Decimal("0")),
                "contradiction_ids": list(pair.contradiction_ids),
                "dependence_group_ids": list(pair.dependence_group_ids),
            }
            for pair in facts.unresolved_pairs
            if rubric.get(pair.pair_id, Decimal("0")) > 0
        )

    @staticmethod
    def _score(
        template: CheckTemplate,
        facts: PlannerReadFacts,
        weights: Mapping[str, Decimal],
        covered_pairs: set[str],
        covered_groups: set[str],
    ) -> tuple[ScoreTerms, int | None]:
        unresolved = {item.pair_id: item for item in facts.unresolved_pairs}
        rubric = {item.pair_id: item.value for item in template.candidate_discrimination}
        total_weight = sum((weights[item] for item in unresolved), Decimal("0"))
        if total_weight:
            discrimination = sum((weights[pair_id] * rubric.get(pair_id, Decimal("0")) for pair_id in unresolved), Decimal("0")) / total_weight
        else:
            discrimination = Decimal("0")

        group_map = {item.evidence_group_id: item.dependence_group_id for item in facts.dependence_groups}
        independent_groups = set(group_map.values())
        candidate_groups = {group_map[group_id] for group_id in template.evidence_group_ids if group_id in group_map}
        new_groups = candidate_groups - covered_groups
        coverage = Decimal(len(new_groups)) / Decimal(len(independent_groups)) if independent_groups else Decimal("0")
        overlap = sum((weights[pair_id] * rubric.get(pair_id, Decimal("0")) for pair_id in covered_pairs if pair_id in unresolved), Decimal("0"))
        candidate_weight = sum((weights[pair_id] * rubric.get(pair_id, Decimal("0")) for pair_id in rubric if pair_id in unresolved), Decimal("0"))
        pair_redundancy = overlap / candidate_weight if candidate_weight else Decimal("0")
        shared_groups = candidate_groups & covered_groups
        group_redundancy = Decimal(len(shared_groups)) / Decimal(len(candidate_groups)) if candidate_groups else Decimal("0")
        redundancy = (pair_redundancy + group_redundancy) / Decimal("2")

        readiness: list[Decimal] = []
        capability_by_id = {item.capability_id: item for item in facts.capability_facts}
        for requirement in template.required_capabilities:
            fact = capability_by_id.get(requirement.capability_id)
            if fact is not None and fact.observed_at is not None:
                age = max(Decimal("0"), Decimal(str((facts.as_of - fact.observed_at).total_seconds())))
                readiness.append(max(Decimal("0"), min(Decimal("1"), Decimal("1") - age / Decimal(requirement.max_age_seconds))))
        prerequisite_by_id = {item.prerequisite_id: item for item in facts.prerequisite_facts}
        for requirement in template.prerequisites:
            fact = prerequisite_by_id.get(requirement.prerequisite_id)
            if requirement.max_age_seconds is not None and fact is not None and fact.observed_at is not None:
                age = max(Decimal("0"), Decimal(str((facts.as_of - fact.observed_at).total_seconds())))
                readiness.append(max(Decimal("0"), min(Decimal("1"), Decimal("1") - age / Decimal(requirement.max_age_seconds))))
            else:
                readiness.append(Decimal("1"))
        capability_readiness = sum(readiness, Decimal("0")) / Decimal(len(readiness)) if readiness else Decimal("1")

        turnaround = next((item for item in facts.turnaround_facts if item.source_identity == template.turnaround_source_id), None)
        if turnaround is not None and turnaround.state == TurnaroundState.SUPPORTED:
            turnaround_seconds: int | None = turnaround.seconds
            if facts.decision_deadline.state == DeadlineState.SUPPORTED:
                window = Decimal(str((facts.decision_deadline.deadline - facts.as_of).total_seconds()))
                slack = max(Decimal("0"), window - Decimal(turnaround.seconds))
                turnaround_feasibility = max(Decimal("0"), min(Decimal("1"), slack / window)) if window > 0 else Decimal("0")
            else:
                turnaround_feasibility = Decimal("1")
        else:
            turnaround_seconds = None
            turnaround_feasibility = Decimal("0.25")
        feasibility = (capability_readiness + turnaround_feasibility) / Decimal("2")
        effort_disruption = (_EFFORT_COST[template.effort_band] + _DISRUPTION_COST[template.disruption]) / Decimal("2")
        utility = (
            _UTILITY_WEIGHTS["D"] * discrimination
            + _UTILITY_WEIGHTS["N"] * coverage
            + _UTILITY_WEIGHTS["F"] * feasibility
            - _UTILITY_WEIGHTS["E"] * effort_disruption
            - _UTILITY_WEIGHTS["R"] * redundancy
        )
        return ScoreTerms(discrimination, coverage, feasibility, effort_disruption, redundancy, utility), turnaround_seconds

    @staticmethod
    def _pareto_front(scored: Sequence[tuple[CheckTemplate, ScoreTerms, tuple[dict[str, object], ...], tuple[str, ...], int | None]]) -> frozenset[str]:
        front: set[str] = set()
        for template, terms, _alternatives, _facts, seconds in scored:
            dominated = False
            for other, other_terms, _other_alternatives, _other_facts, other_seconds in scored:
                if template.template_id == other.template_id or terms.discrimination != other_terms.discrimination:
                    continue
                this_time = seconds if seconds is not None else 2**63 - 1
                other_time = other_seconds if other_seconds is not None else 2**63 - 1
                no_worse = (
                    other_terms.effort_disruption <= terms.effort_disruption
                    and other_time <= this_time
                    and _DISRUPTION_COST[other.disruption] <= _DISRUPTION_COST[template.disruption]
                )
                better = (
                    other_terms.effort_disruption < terms.effort_disruption
                    or other_time < this_time
                    or _DISRUPTION_COST[other.disruption] < _DISRUPTION_COST[template.disruption]
                )
                if no_worse and better:
                    dominated = True
                    break
            if not dominated:
                front.add(template.template_id)
        return frozenset(front)

    @staticmethod
    def _rank_key(item: tuple[CheckTemplate, ScoreTerms, tuple[dict[str, object], ...], tuple[str, ...], int | None], pareto_front: frozenset[str]) -> tuple[object, ...]:
        template, score, _alternatives, _facts, turnaround_seconds = item
        return (
            -score.utility,
            -score.discrimination,
            0 if template.template_id in pareto_front else 1,
            _DISRUPTION_COST[template.disruption],
            turnaround_seconds if turnaround_seconds is not None else 2**63 - 1,
            template.template_id,
        )

    @staticmethod
    def _recommendation(
        rank: int,
        template: CheckTemplate,
        score: ScoreTerms,
        alternatives: tuple[dict[str, object], ...],
        added_groups: tuple[str, ...],
        facts: PlannerReadFacts,
        policy_identity: str,
        catalog_identity: str,
        turnaround_seconds: int | None,
    ) -> Recommendation:
        rubric = {item.pair_id: item.value for item in template.candidate_discrimination}
        resolved_pairs = {item.pair_id for item in facts.unresolved_pairs if rubric.get(item.pair_id, Decimal("0")) > 0}
        not_resolved = [f"hypothesis_pair:{item.pair_id}" for item in facts.unresolved_pairs if item.pair_id not in resolved_pairs]
        group_map = {item.evidence_group_id: item.dependence_group_id for item in facts.dependence_groups}
        addressed_groups = {group_map[item] for item in template.evidence_group_ids if item in group_map}
        all_groups = set(group_map.values())
        not_resolved.extend(f"independent_evidence_group:{item}" for item in sorted(all_groups - addressed_groups))
        capability_trace = tuple(
            {"capability_id": item.capability_id, "state": item.state.value, "source_identity": item.source_identity, "qualification_identity": item.qualification_identity}
            for item in facts.capability_facts
            if item.capability_id in {req.capability_id for req in template.required_capabilities}
        )
        prerequisite_trace = tuple(
            {
                "prerequisite_id": item.prerequisite_id,
                "state": item.state.value,
                "source_identity": item.source_identity,
                "observed_at": item.observed_at,
                "valid_until": item.valid_until,
                "target_identity": item.target_identity,
                "context_identity": item.context_identity,
            }
            for item in facts.prerequisite_facts
            if item.prerequisite_id in {req.prerequisite_id for req in template.prerequisites}
        )
        qualification_ids = {template.qualification_identity} | {
            item.qualification_identity for item in facts.capability_facts
            if item.capability_id in {req.capability_id for req in template.required_capabilities}
            and item.qualification_identity is not None
        }
        qualification_trace = tuple(
            {
                "qualification_identity": item.qualification_identity,
                "state": item.state.value,
                "source_identity": item.source_identity,
                "valid_until": item.valid_until,
            }
            for item in facts.qualification_facts
            if item.qualification_identity in qualification_ids
        )
        combined_facts: tuple[dict[str, object], ...] = capability_trace + prerequisite_trace + qualification_trace
        why = ["current family and target kind match the curated template"]
        if template.context_independent:
            why.append("template is curated as context independent")
        elif facts.target_context.context_identity is None:
            why.append("template explicitly permits unknown context")
        else:
            why.append("current context is in the curated support set")
        if template.unit_independent:
            why.append("template is curated as unit independent")
        elif facts.target_context.unit_identity is None:
            why.append("template explicitly permits unknown unit")
        else:
            why.append("current unit is in the curated support set")
        if template.required_capabilities:
            why.append("required capabilities are currently available, fresh and qualified")
        if template.prerequisites:
            why.append("required inputs and prerequisites are currently satisfied")
        if facts.decision_deadline.state == DeadlineState.SUPPORTED:
            why.append("supported turnaround completes within the supported decision window")
        else:
            why.append("decision deadline is explicitly unknown")
        if template.path == TemplatePath.PREREQUISITE_VALIDATION:
            why.append("no unresolved hypothesis pair exists; this is a curated prerequisite or validation path")
        effort_value = _EFFORT_COST[template.effort_band]
        disruption_value = _DISRUPTION_COST[template.disruption]
        time_fact = next((item for item in facts.turnaround_facts if item.source_identity == template.turnaround_source_id), None)
        if turnaround_seconds is None:
            turnaround_payload: dict[str, object] = {
                "state": time_fact.state.value if time_fact is not None else "UNKNOWN",
                "seconds": None,
                "source_identity": template.turnaround_source_id,
                "unknown_reason": time_fact.unknown_reason if time_fact is not None else "fact_not_supplied",
                "ranking_treatment": "conservative_feasibility_penalty; never treated as zero time",
            }
        else:
            turnaround_payload = {
                "state": "SUPPORTED",
                "seconds": turnaround_seconds,
                "source_identity": template.turnaround_source_id,
                "unknown_reason": None,
                "ranking_treatment": "supported duration",
            }
        return Recommendation(
            rank,
            template.template_id,
            template.version,
            template.title,
            template.execution_mode.value,
            score,
            alternatives,
            tuple(why),
            combined_facts,
            {
                "effort_band": template.effort_band.value,
                "effort_source_id": template.effort_source_id,
                "effort_cost_band": effort_value,
                "turnaround": turnaround_payload,
                "disruption_class": template.disruption.value,
                "disruption_cost_band": disruption_value,
                "evidence_quality_requirements": list(template.evidence_quality_requirements),
                "qualification_identity": template.qualification_identity,
                "decision_deadline": facts.decision_deadline.as_dict(),
            },
            added_groups,
            tuple(sorted(not_resolved)),
            policy_identity,
            catalog_identity,
        )


__all__ = [
    "CapabilityFact",
    "CapabilityRequirement",
    "CapabilityRole",
    "CapabilityState",
    "CheckTemplate",
    "CheckTemplateCatalog",
    "ContradictionFact",
    "DecisionDeadlineFact",
    "DeadlineState",
    "DisruptionClass",
    "EffortBand",
    "EvidenceDependenceGroup",
    "EvidenceValidityFact",
    "EvidenceValidityState",
    "ExcludedCheck",
    "ExclusionReason",
    "NextCheckPlannerService",
    "ORDINAL_UTILITY_VERSION",
    "PairDiscrimination",
    "PairWeight",
    "PLANNER_SCHEMA_VERSION",
    "PlannerPlan",
    "PlannerPolicy",
    "PlannerReadFacts",
    "PrerequisiteFact",
    "PrerequisiteRequirement",
    "PrerequisiteState",
    "QualificationFact",
    "QualificationState",
    "Recommendation",
    "ScoreTerms",
    "TargetContext",
    "TemplatePath",
    "TurnaroundFact",
    "TurnaroundState",
    "UnknownFact",
    "UnresolvedHypothesisPair",
]
