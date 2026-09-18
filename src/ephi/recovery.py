"""Bounded, in-memory qualified recovery evaluation for the W0 F05 slice."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import IntEnum, StrEnum
import math
from typing import Any


class RecoveryValidationError(ValueError):
    """An observation, policy or episode violated the recovery contract."""


class RecoveryState(StrEnum):
    """Technical recovery state derived by :class:`RecoveryService`."""

    ACTIVE = "ACTIVE"
    RECOVERING = "RECOVERING"
    RESOLVED = "RESOLVED"


class Severity(IntEnum):
    """Small W0 severity vocabulary; severity is never recovery evidence alone."""

    NORMAL = 0
    OBSERVE = 1
    ALERT = 2
    CRITICAL = 3


class ObservationOutcome(StrEnum):
    """Outcome of the observed characteristic."""

    NORMAL = "NORMAL"
    HEALTHY = "NORMAL"
    ABNORMAL = "ABNORMAL"
    UNKNOWN = "UNKNOWN"


Outcome = ObservationOutcome


class IntegrityAttribution(StrEnum):
    """Known data-quality attribution that cannot support recovery."""

    NONE = "NONE"
    DATA_PIPELINE_OR_SCHEMA_CHANGE = "DATA_PIPELINE_OR_SCHEMA_CHANGE"
    MEASUREMENT_INTEGRITY = "MEASUREMENT_INTEGRITY"


class RecoveryReasonCode(StrEnum):
    """Stable reason codes emitted by the recovery evaluator."""

    ELIGIBLE = "ELIGIBLE"
    MISSING_OBSERVATION = "MISSING_OBSERVATION"
    OBSERVATION_UNAVAILABLE = "OBSERVATION_UNAVAILABLE"
    EVENT_AFTER_CUTOFF = "EVENT_AFTER_CUTOFF"
    AVAILABLE_AFTER_CUTOFF = "AVAILABLE_AFTER_CUTOFF"
    INVALID_TIMING = "INVALID_TIMING"
    AVAILABILITY_DELAY_EXCEEDED = "AVAILABILITY_DELAY_EXCEEDED"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
    CHARACTERISTIC_MISMATCH = "CHARACTERISTIC_MISMATCH"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    INVALID_CAPABILITY = "INVALID_CAPABILITY"
    PIPELINE_OR_SCHEMA_SUSPECT = "PIPELINE_OR_SCHEMA_SUSPECT"
    DUPLICATE_OBSERVATION_ID = "DUPLICATE_OBSERVATION_ID"
    DUPLICATE_SAMPLING_IDENTITY = "DUPLICATE_SAMPLING_IDENTITY"
    NON_AFFIRMATIVE_OUTCOME = "NON_AFFIRMATIVE_OUTCOME"
    CONTRADICTORY_OUTCOME = "CONTRADICTORY_OUTCOME"


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecoveryValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RecoveryValidationError(f"{field} must be timezone-aware")
    return value


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise RecoveryValidationError("confidence must be a finite number in [0, 1]")
    converted = float(value)
    if not math.isfinite(converted) or not 0.0 <= converted <= 1.0:
        raise RecoveryValidationError("confidence must be a finite number in [0, 1]")
    return converted


def _duration(value: object, field: str) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise RecoveryValidationError(f"{field} must be a positive timedelta")
    return value


def _enum(value: object, enum_type: type[Any], field: str) -> Any:
    if not isinstance(value, enum_type):
        raise RecoveryValidationError(f"{field} must be a {enum_type.__name__}")
    return value


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """Deterministic W0 regression policy, not family production qualification."""

    policy_id: str
    confidence_floor: float
    minimum_eligible_independent_samples: int
    expected_context: str
    expected_characteristic: str
    expected_unit: str
    affirmative_outcome: ObservationOutcome = ObservationOutcome.NORMAL
    require_reference_valid: bool = True
    require_capability_valid: bool = True
    max_observation_age: timedelta = timedelta(hours=24)
    max_availability_delay: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _identity(self.policy_id, "policy_id"))
        object.__setattr__(self, "confidence_floor", _confidence(self.confidence_floor))
        if self.confidence_floor <= 0.0:
            raise RecoveryValidationError("confidence_floor must be greater than zero")
        if (
            isinstance(self.minimum_eligible_independent_samples, bool)
            or not isinstance(self.minimum_eligible_independent_samples, int)
            or self.minimum_eligible_independent_samples < 1
        ):
            raise RecoveryValidationError("minimum_eligible_independent_samples must be a positive integer")
        for field in ("expected_context", "expected_characteristic", "expected_unit"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        _enum(self.affirmative_outcome, ObservationOutcome, "affirmative_outcome")
        if self.affirmative_outcome is ObservationOutcome.UNKNOWN:
            raise RecoveryValidationError("affirmative_outcome cannot be UNKNOWN")
        for field in ("require_reference_valid", "require_capability_valid"):
            if not isinstance(getattr(self, field), bool):
                raise RecoveryValidationError(f"{field} must be a boolean")
        object.__setattr__(self, "max_observation_age", _duration(self.max_observation_age, "max_observation_age"))
        object.__setattr__(self, "max_availability_delay", _duration(self.max_availability_delay, "max_availability_delay"))

    @classmethod
    def deterministic_w0_regression(cls) -> "RecoveryPolicy":
        """Return the bounded policy used by offline F05 regression scenarios."""

        return cls(
            policy_id="W0_DETERMINISTIC_REGRESSION",
            confidence_floor=0.8,
            minimum_eligible_independent_samples=3,
            expected_context="W0_CONTEXT",
            expected_characteristic="W0_CHARACTERISTIC",
            expected_unit="W0_UNIT",
        )

    @property
    def minimum_eligible_independent_sample_count(self) -> int:
        """Readable alias for the policy's required independent sample count."""

        return self.minimum_eligible_independent_samples


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    """One immutable observation candidate considered for episode recovery."""

    observation_id: str
    episode_id: str
    sampling_identity: str
    event_at: datetime
    observed_at: datetime
    available_at: datetime | None
    context: str
    characteristic: str
    unit: str
    severity: Severity
    outcome: ObservationOutcome
    confidence: float
    leading_hypothesis: str
    integrity_attribution: IntegrityAttribution = IntegrityAttribution.NONE
    reference_valid: bool = True
    capability_valid: bool = True

    def __post_init__(self) -> None:
        for field in ("observation_id", "episode_id", "sampling_identity", "context", "characteristic", "unit", "leading_hypothesis"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        for field in ("event_at", "observed_at"):
            object.__setattr__(self, field, _timestamp(getattr(self, field), field))
        if self.available_at is not None:
            object.__setattr__(self, "available_at", _timestamp(self.available_at, "available_at"))
        _enum(self.severity, Severity, "severity")
        _enum(self.outcome, ObservationOutcome, "outcome")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        _enum(self.integrity_attribution, IntegrityAttribution, "integrity_attribution")
        for field in ("reference_valid", "capability_valid"):
            if not isinstance(getattr(self, field), bool):
                raise RecoveryValidationError(f"{field} must be a boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "episode_id": self.episode_id,
            "sampling_identity": self.sampling_identity,
            "event_at": self.event_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat() if self.available_at is not None else None,
            "context": self.context,
            "characteristic": self.characteristic,
            "unit": self.unit,
            "severity": self.severity.name,
            "outcome": self.outcome.value,
            "confidence": self.confidence,
            "leading_hypothesis": self.leading_hypothesis,
            "integrity_attribution": self.integrity_attribution.value,
            "reference_valid": self.reference_valid,
            "capability_valid": self.capability_valid,
        }


@dataclass(frozen=True, slots=True)
class CriterionResult:
    """One named deterministic recovery criterion result."""

    passed: bool
    reason_code: RecoveryReasonCode | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise RecoveryValidationError("criterion passed must be a boolean")
        if self.reason_code is not None:
            _enum(self.reason_code, RecoveryReasonCode, "criterion reason_code")
        if not isinstance(self.detail, str):
            raise RecoveryValidationError("criterion detail must be a string")

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "reason_code": self.reason_code.value if self.reason_code is not None else None,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RecoveryEligibility:
    """Evaluation result with explicit reason codes and all criterion results."""

    observation_id: str | None
    eligible: bool
    reason_codes: tuple[RecoveryReasonCode, ...]
    criterion_results: Mapping[str, CriterionResult]
    qualified: bool = False
    contradictory: bool = False

    def __post_init__(self) -> None:
        if self.observation_id is not None:
            object.__setattr__(self, "observation_id", _identity(self.observation_id, "observation_id"))
        if not isinstance(self.eligible, bool) or not isinstance(self.qualified, bool) or not isinstance(self.contradictory, bool):
            raise RecoveryValidationError("eligibility flags must be booleans")
        for code in self.reason_codes:
            _enum(code, RecoveryReasonCode, "reason code")
        if not isinstance(self.criterion_results, Mapping):
            raise RecoveryValidationError("criterion_results must be a mapping")

    def as_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "eligible": self.eligible,
            "qualified": self.qualified,
            "contradictory": self.contradictory,
            "reason_codes": [code.value for code in self.reason_codes],
            "criterion_results": {name: result.as_dict() for name, result in self.criterion_results.items()},
        }


@dataclass(frozen=True, slots=True)
class RecoveryEpisode:
    """Explicit episode recovery state derived by the recovery service."""

    episode_id: str
    state: RecoveryState = RecoveryState.ACTIVE
    assessment_count: int = 0
    eligible_independent_count: int = 0
    policy_id: str = "W0_DETERMINISTIC_REGRESSION"

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _identity(self.episode_id, "episode_id"))
        _enum(self.state, RecoveryState, "state")
        for field in ("assessment_count", "eligible_independent_count"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RecoveryValidationError(f"{field} must be a non-negative integer")
        if self.eligible_independent_count > self.assessment_count:
            raise RecoveryValidationError("eligible_independent_count cannot exceed assessment_count")
        object.__setattr__(self, "policy_id", _identity(self.policy_id, "policy_id"))

    @property
    def episode_state(self) -> RecoveryState:
        """Named alias for callers that distinguish episode identity from state."""

        return self.state

    def as_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "state": self.state.value,
            "episode_state": self.state.value,
            "assessment_count": self.assessment_count,
            "eligible_independent_count": self.eligible_independent_count,
            "policy_id": self.policy_id,
        }


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    """The evaluator result and derived episode snapshot for one submission."""

    observation: RecoveryObservation
    eligibility: RecoveryEligibility
    episode: RecoveryEpisode


class RecoveryEvaluator:
    """Evaluate one observation against a bounded recovery policy."""

    def __init__(self, policy: RecoveryPolicy):
        if not isinstance(policy, RecoveryPolicy):
            raise RecoveryValidationError("policy must be a RecoveryPolicy")
        self.policy = policy

    def evaluate(
        self,
        observation: RecoveryObservation | None,
        *,
        evaluated_at: datetime,
        known_observation_ids: Iterable[str] = (),
        known_sampling_identities: Iterable[str] = (),
    ) -> RecoveryEligibility:
        """Return fail-closed eligibility at a caller-supplied availability cutoff."""

        evaluated_at = _timestamp(evaluated_at, "evaluated_at")
        if observation is None:
            return RecoveryEligibility(
                observation_id=None,
                eligible=False,
                reason_codes=(RecoveryReasonCode.MISSING_OBSERVATION,),
                criterion_results={
                    "observation_present": CriterionResult(False, RecoveryReasonCode.MISSING_OBSERVATION, "observation is required"),
                },
            )
        if not isinstance(observation, RecoveryObservation):
            raise RecoveryValidationError("observation must be a RecoveryObservation")

        known_observation_ids = set(known_observation_ids)
        known_sampling_identities = set(known_sampling_identities)
        results: dict[str, CriterionResult] = {}

        def add(name: str, passed: bool, reason: RecoveryReasonCode | None, detail: str) -> None:
            results[name] = CriterionResult(passed, None if passed else reason, detail)

        add(
            "observation_identity",
            observation.observation_id not in known_observation_ids,
            RecoveryReasonCode.DUPLICATE_OBSERVATION_ID,
            "observation identity is new" if observation.observation_id not in known_observation_ids else "observation identity was already submitted",
        )
        add(
            "sampling_identity",
            observation.sampling_identity not in known_sampling_identities,
            RecoveryReasonCode.DUPLICATE_SAMPLING_IDENTITY,
            "sampling identity is independent" if observation.sampling_identity not in known_sampling_identities else "sampling identity was already submitted",
        )
        add(
            "event_cutoff",
            observation.event_at <= evaluated_at,
            RecoveryReasonCode.EVENT_AFTER_CUTOFF,
            "event is at or before the evaluation cutoff" if observation.event_at <= evaluated_at else "event is after the evaluation cutoff",
        )
        available = observation.available_at
        availability_ok = available is not None and available <= evaluated_at
        availability_reason = (
            None
            if availability_ok
            else RecoveryReasonCode.OBSERVATION_UNAVAILABLE if available is None else RecoveryReasonCode.AVAILABLE_AFTER_CUTOFF
        )
        add(
            "availability_cutoff",
            availability_ok,
            availability_reason,
            "observation is available at the evaluation cutoff" if availability_ok else "observation is unavailable at the evaluation cutoff",
        )
        timing_ok = observation.event_at <= observation.observed_at and (
            available is None or observation.observed_at <= available
        )
        add(
            "timing_order",
            timing_ok,
            RecoveryReasonCode.INVALID_TIMING,
            "event_at precedes observed_at and observed_at precedes available_at" if timing_ok else "event/observed/available times are not ordered",
        )
        availability_delay_ok = available is None or timedelta(0) <= available - observation.observed_at <= self.policy.max_availability_delay
        add(
            "availability_delay",
            availability_delay_ok,
            RecoveryReasonCode.AVAILABILITY_DELAY_EXCEEDED,
            "availability delay is within the W0 window" if availability_delay_ok else "availability delay exceeds the W0 window",
        )
        age = evaluated_at - observation.observed_at
        fresh = timedelta(0) <= age <= self.policy.max_observation_age
        add(
            "freshness",
            fresh,
            RecoveryReasonCode.STALE_OBSERVATION,
            "observation is within the W0 freshness window" if fresh else "observation is stale or from the future",
        )
        add("context", observation.context == self.policy.expected_context, RecoveryReasonCode.CONTEXT_MISMATCH, "context matches policy" if observation.context == self.policy.expected_context else "context does not match policy")
        add("characteristic", observation.characteristic == self.policy.expected_characteristic, RecoveryReasonCode.CHARACTERISTIC_MISMATCH, "characteristic matches policy" if observation.characteristic == self.policy.expected_characteristic else "characteristic does not match policy")
        add("unit", observation.unit == self.policy.expected_unit, RecoveryReasonCode.UNIT_MISMATCH, "unit matches policy" if observation.unit == self.policy.expected_unit else "unit does not match policy")
        add(
            "confidence",
            observation.confidence >= self.policy.confidence_floor,
            RecoveryReasonCode.LOW_CONFIDENCE,
            "confidence meets the W0 floor" if observation.confidence >= self.policy.confidence_floor else "confidence is below the W0 floor",
        )
        reference_ok = not self.policy.require_reference_valid or observation.reference_valid
        capability_ok = not self.policy.require_capability_valid or observation.capability_valid
        add("reference", reference_ok, RecoveryReasonCode.INVALID_REFERENCE, "reference is valid" if reference_ok else "reference is invalid or unqualified")
        add("capability", capability_ok, RecoveryReasonCode.INVALID_CAPABILITY, "capability is valid" if capability_ok else "capability is invalid or unqualified")
        pipeline_suspect = observation.integrity_attribution is not IntegrityAttribution.NONE or any(
            token in observation.leading_hypothesis.upper().replace("-", "_").replace(" ", "_")
            for token in ("PIPELINE", "SCHEMA", "MEASUREMENT_INTEGRITY")
        )
        add(
            "integrity_attribution",
            not pipeline_suspect,
            RecoveryReasonCode.PIPELINE_OR_SCHEMA_SUSPECT,
            "no pipeline/schema/measurement-integrity attribution" if not pipeline_suspect else "pipeline/schema/measurement-integrity attribution cannot support recovery",
        )
        outcome_ok = observation.outcome is self.policy.affirmative_outcome
        outcome_reason = None if outcome_ok else RecoveryReasonCode.CONTRADICTORY_OUTCOME if observation.outcome is ObservationOutcome.ABNORMAL else RecoveryReasonCode.NON_AFFIRMATIVE_OUTCOME
        add(
            "affirmative_outcome",
            outcome_ok,
            outcome_reason,
            "qualified affirmative healthy/normal outcome" if outcome_ok else "outcome is not the policy's affirmative condition",
        )

        reason_codes = tuple(
            result.reason_code
            for result in results.values()
            if result.reason_code is not None
        )
        qualified = all(
            result.passed
            for name, result in results.items()
            if name != "affirmative_outcome"
        )
        contradictory = qualified and observation.outcome is ObservationOutcome.ABNORMAL
        eligible = not reason_codes
        if eligible:
            reason_codes = (RecoveryReasonCode.ELIGIBLE,)
        return RecoveryEligibility(
            observation_id=observation.observation_id,
            eligible=eligible,
            reason_codes=reason_codes,
            criterion_results=results,
            qualified=qualified,
            contradictory=contradictory,
        )


class RecoveryService:
    """Bounded in-memory recovery service; no persistence or worker authority."""

    def __init__(self, policy: RecoveryPolicy | None = None):
        self.policy = policy or RecoveryPolicy.deterministic_w0_regression()
        self.evaluator = RecoveryEvaluator(self.policy)
        self._episodes: dict[str, RecoveryEpisode] = {}
        self._observations: dict[str, list[RecoveryObservation]] = {}
        self._eligible_sampling_identities: dict[str, set[str]] = {}

    def create_episode(self, episode_id: str) -> RecoveryEpisode:
        episode_id = _identity(episode_id, "episode_id")
        if episode_id in self._episodes:
            raise RecoveryValidationError(f"episode already exists: {episode_id}")
        episode = RecoveryEpisode(episode_id=episode_id, policy_id=self.policy.policy_id)
        self._episodes[episode_id] = episode
        self._observations[episode_id] = []
        self._eligible_sampling_identities[episode_id] = set()
        return episode

    def get_episode(self, episode_id: str) -> RecoveryEpisode:
        episode_id = _identity(episode_id, "episode_id")
        try:
            return self._episodes[episode_id]
        except KeyError as exc:
            raise RecoveryValidationError(f"unknown episode: {episode_id}") from exc

    def submit(
        self,
        observation: RecoveryObservation,
        *,
        evaluated_at: datetime,
    ) -> RecoveryAssessment:
        if not isinstance(observation, RecoveryObservation):
            raise RecoveryValidationError("observation must be a RecoveryObservation")
        episode = self.get_episode(observation.episode_id)
        observations = self._observations[episode.episode_id]
        observation_ids = {item.observation_id for item in observations}
        sampling_ids = {item.sampling_identity for item in observations}
        eligibility = self.evaluator.evaluate(
            observation,
            evaluated_at=evaluated_at,
            known_observation_ids=observation_ids,
            known_sampling_identities=sampling_ids,
        )
        observations.append(observation)
        eligible_sampling_ids = self._eligible_sampling_identities[episode.episode_id]
        if eligibility.contradictory:
            # Deterministic W0 rule: qualified contradictory evidence resets all
            # recovery progress and returns the episode to ACTIVE.
            eligible_sampling_ids.clear()
            state = RecoveryState.ACTIVE
        else:
            if eligibility.eligible:
                eligible_sampling_ids.add(observation.sampling_identity)
            eligible_count = len(eligible_sampling_ids)
            if eligible_count >= self.policy.minimum_eligible_independent_samples:
                state = RecoveryState.RESOLVED
            elif eligible_count:
                state = RecoveryState.RECOVERING
            else:
                state = episode.state
        next_episode = RecoveryEpisode(
            episode_id=episode.episode_id,
            state=state,
            assessment_count=len(observations),
            eligible_independent_count=len(eligible_sampling_ids),
            policy_id=episode.policy_id,
        )
        self._episodes[episode.episode_id] = next_episode
        return RecoveryAssessment(observation=observation, eligibility=eligibility, episode=next_episode)

    def submit_observation(self, observation: RecoveryObservation, *, evaluated_at: datetime) -> RecoveryAssessment:
        """Explicit alias for the observation command."""

        return self.submit(observation, evaluated_at=evaluated_at)

    def observations(self, episode_id: str) -> tuple[RecoveryObservation, ...]:
        episode_id = _identity(episode_id, "episode_id")
        self.get_episode(episode_id)
        return tuple(self._observations[episode_id])
