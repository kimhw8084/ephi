"""Durable, generic O5.1 decision-loop domain contracts.

The decision loop is one versioned Episode aggregate.  This module owns no
second receipt, audit, outbox, authorization, read-snapshot, artifact, or
recovery evaluator.  Mutations are delegated to the existing O2 command
executor and recovery observations are evaluated by :mod:`ephi.recovery`.

The values here are bounded workflow facts and identities.  Scientific raw
payloads remain in immutable artifacts; this aggregate stores only references
and the structured facts needed to explain a decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
from typing import Any

from ephi.recovery import (
    IntegrityAttribution,
    ObservationOutcome,
    RecoveryEvaluator,
    RecoveryObservation,
    RecoveryPolicy,
    RecoveryValidationError,
    Severity,
)

from .context import AccessScope, CommandContext, CurrentAuthorizationAuthority, Principal, RevisionVector
from .errors import (
    AggregateNotFoundError,
    CoherentReadConflictError,
    InvalidTransitionError,
    ValidationFailureError,
)
from .hashing import canonical_json, normalize_domain_payload
from .transactions import CommandResult, VersionedAggregateCommandExecutor
from .workflow import EPISODE_WORKFLOW_AGGREGATE_TYPE


DECISION_LOOP_AGGREGATE_TYPE = EPISODE_WORKFLOW_AGGREGATE_TYPE
DECISION_LOOP_READ_CAPABILITY = "ephi.decision_loop.read"
DECISION_LOOP_CREATE_CAPABILITY = "ephi.decision_loop.create"
CHECK_REQUEST_CAPABILITY = "ephi.decision_loop.check.request"
CHECK_EXECUTE_CAPABILITY = "ephi.decision_loop.check.execute"
ACTION_RECORD_CAPABILITY = "ephi.decision_loop.action.record"
RECOVERY_PLAN_CAPABILITY = "ephi.decision_loop.recovery.plan"
RECOVERY_OBSERVATION_CAPABILITY = "ephi.decision_loop.recovery.observe"
CLOSURE_CAPABILITY = "ephi.decision_loop.closure"
REOPEN_CAPABILITY = "ephi.decision_loop.reopen"
DECISION_LOOP_SCHEMA_VERSION = "o5.1.v1"


class CheckExecutionMode(StrEnum):
    READ_EXISTING = "READ_EXISTING"
    ASYNC_COMPUTE = "ASYNC_COMPUTE"
    REQUEST_HUMAN_MEASUREMENT = "REQUEST_HUMAN_MEASUREMENT"
    REQUEST_APPROVED_EXTERNAL_WORK = "REQUEST_APPROVED_EXTERNAL_WORK"


class CheckLifecycle(StrEnum):
    REQUESTED = "REQUESTED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class CheckOutcome(StrEnum):
    SUPPORTS_A = "SUPPORTS_A"
    SUPPORTS_B = "SUPPORTS_B"
    INCONSISTENT = "INCONSISTENT"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class ExternalActionType(StrEnum):
    APPROVED_EXTERNAL_WORK = "APPROVED_EXTERNAL_WORK"
    HUMAN_MEASUREMENT = "HUMAN_MEASUREMENT"
    ENGINEERING_REVIEW = "ENGINEERING_REVIEW"
    APPROVED_WORK_REQUEST = "APPROVED_WORK_REQUEST"
    NOTIFICATION = "NOTIFICATION"


class ActionReconciliationState(StrEnum):
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RecoveryPlanState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    WAITING_FOR_DATA = "WAITING_FOR_DATA"
    MONITORING = "MONITORING"
    PASS = "PASS"
    FAIL = "FAIL"
    INVALIDATED = "INVALIDATED"
    EXCEPTION_REVIEW = "EXCEPTION_REVIEW"


class ClosureDisposition(StrEnum):
    CONFIRMED_ISSUE = "CONFIRMED_ISSUE"
    EXCEPTION = "EXCEPTION"
    UNRESOLVED = "UNRESOLVED"
    BENIGN = "BENIGN"
    DUPLICATE = "DUPLICATE"
    DATA_INTEGRITY = "DATA_INTEGRITY"


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


def _enum_value(value: object, enum_type: type[StrEnum], field: str) -> str:
    if isinstance(value, enum_type):
        return value.value
    if isinstance(value, str):
        try:
            return enum_type(value).value
        except ValueError as exc:
            raise ValidationFailureError(f"{field} is not a supported {enum_type.__name__}") from exc
    raise ValidationFailureError(f"{field} must be a {enum_type.__name__} or its value")


def _timestamp(value: datetime | None, field: str, *, default_now: bool = False) -> str | None:
    if value is None and default_now:
        value = datetime.now(timezone.utc)
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailureError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now() -> str:
    return _timestamp(None, "now", default_now=True) or ""


def _refs(value: Sequence[str] | None, field: str = "evidence_refs") -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationFailureError(f"{field} must be a sequence of strings")
    result: list[str] = []
    for item in value:
        item = _identity(item, field)
        if item not in result:
            result.append(item)
    return result


def _object(value: Mapping[str, object] | None, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationFailureError(f"{field} must be an object")
    try:
        normalized = normalize_domain_payload(value)
    except Exception as exc:
        if isinstance(exc, ValidationFailureError):
            raise
        raise ValidationFailureError(f"{field} is not a canonical object") from exc
    if not isinstance(normalized, dict):
        raise ValidationFailureError(f"{field} must be an object")
    return normalized


def _revision_payload(context: CommandContext, *, create: bool = False) -> dict[str, Any]:
    if not create and context.expected_workflow_version is None:
        raise ValidationFailureError("decision-loop commands require expected_workflow_version")
    if context.viewed_revisions is None:
        raise ValidationFailureError("decision-loop commands require viewed_revisions")
    if not create and context.viewed_revisions.workflow_version != context.expected_workflow_version:
        raise ValidationFailureError("viewed_revisions.workflow_version must equal expected_workflow_version")
    return {"viewed_revisions": context.viewed_revisions.as_dict()}


def _policy_snapshot(policy: RecoveryPolicy, policy_version: str) -> dict[str, Any]:
    if not isinstance(policy, RecoveryPolicy):
        raise ValidationFailureError("recovery plans require an explicit RecoveryPolicy")
    policy_version = _identity(policy_version, "policy_version")
    return {
        "policy_id": policy.policy_id,
        "policy_version": policy_version,
        "configuration": {
            "confidence_floor": format(policy.confidence_floor, ".17g"),
            "minimum_eligible_independent_samples": policy.minimum_eligible_independent_samples,
            "expected_context": policy.expected_context,
            "expected_characteristic": policy.expected_characteristic,
            "expected_unit": policy.expected_unit,
            "affirmative_outcome": policy.affirmative_outcome.value,
            "require_reference_valid": policy.require_reference_valid,
            "require_capability_valid": policy.require_capability_valid,
            "max_observation_age_seconds": int(policy.max_observation_age.total_seconds()),
            "max_availability_delay_seconds": int(policy.max_availability_delay.total_seconds()),
        },
    }


def _policy_hash(snapshot: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def _policy_from_snapshot(snapshot: Mapping[str, Any]) -> RecoveryPolicy:
    try:
        config = snapshot["configuration"]
        return RecoveryPolicy(
            policy_id=str(snapshot["policy_id"]),
            confidence_floor=float(config["confidence_floor"]),
            minimum_eligible_independent_samples=int(config["minimum_eligible_independent_samples"]),
            expected_context=str(config["expected_context"]),
            expected_characteristic=str(config["expected_characteristic"]),
            expected_unit=str(config["expected_unit"]),
            affirmative_outcome=ObservationOutcome(str(config["affirmative_outcome"])),
            require_reference_valid=bool(config["require_reference_valid"]),
            require_capability_valid=bool(config["require_capability_valid"]),
            max_observation_age=timedelta(seconds=int(config["max_observation_age_seconds"])),
            max_availability_delay=timedelta(seconds=int(config["max_availability_delay_seconds"])),
        )
    except (KeyError, TypeError, ValueError, RecoveryValidationError) as exc:
        raise CoherentReadConflictError("locked recovery policy is invalid") from exc


def _empty_cycle(cycle_id: str, cycle_version: int, opened_at: str) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "cycle_version": cycle_version,
        "status": "OPEN",
        "opened_at": opened_at,
        "checks": {},
        "actions": {},
        "recovery_plans": {},
        "closures": [],
    }


def _new_state(episode_id: str, cycle_id: str, viewed_revisions: Mapping[str, Any] | None) -> dict[str, Any]:
    cycle_id = _identity(cycle_id, "cycle_id")
    return {
        "decision_loop": {
            "schema_version": DECISION_LOOP_SCHEMA_VERSION,
            "episode_id": episode_id,
            "active_cycle_id": cycle_id,
            "last_viewed_revisions": dict(viewed_revisions) if viewed_revisions is not None else None,
            "cycles": [_empty_cycle(cycle_id, 1, _now())],
            "reopen_history": [],
        },
    }


def _loop_state(state: Mapping[str, Any]) -> dict[str, Any]:
    loop = state.get("decision_loop")
    if not isinstance(loop, dict):
        raise CoherentReadConflictError("Episode workflow has not initialized its O5 decision-loop extension")
    return loop


def _state_object(current: Mapping[str, Any], episode_id: str) -> dict[str, Any]:
    state = deepcopy(dict(current))
    if state.get("work_state") not in {"OPEN", "CLAIMED", "ACKNOWLEDGED", "CLOSED"}:
        raise CoherentReadConflictError("Episode workflow has an unsupported authoritative work state")
    if "owner" not in state:
        raise CoherentReadConflictError("Episode workflow has no owner field")
    loop = _loop_state(state)
    if loop.get("schema_version") != DECISION_LOOP_SCHEMA_VERSION or loop.get("episode_id") != episode_id:
        raise CoherentReadConflictError("decision-loop extension has an unsupported identity or schema")
    if not isinstance(loop.get("cycles"), list) or not loop["cycles"]:
        raise CoherentReadConflictError("decision-loop extension has no durable cycle history")
    active = _active_cycle(state)
    if state["work_state"] == "CLOSED" and active["status"] != "CLOSED":
        raise CoherentReadConflictError("closed Episode workflow has an open decision-loop cycle")
    if state["work_state"] != "CLOSED" and active["status"] != "OPEN":
        raise CoherentReadConflictError("open Episode workflow has a closed decision-loop cycle")
    return state


def _active_cycle(state: Mapping[str, Any], cycle_id: str | None = None) -> dict[str, Any]:
    loop = _loop_state(state)
    active_id = loop.get("active_cycle_id")
    if not isinstance(active_id, str):
        raise CoherentReadConflictError("decision-loop aggregate has no active cycle")
    if cycle_id is not None and cycle_id != active_id:
        raise InvalidTransitionError("command targets a closed or superseded cycle")
    for cycle in loop["cycles"]:
        if isinstance(cycle, Mapping) and cycle.get("cycle_id") == active_id:
            if cycle.get("status") not in {"OPEN", "CLOSED"}:
                raise CoherentReadConflictError("decision-loop cycle has an invalid status")
            return cycle  # type: ignore[return-value]
    raise CoherentReadConflictError("decision-loop active cycle is not in its cycle history")


def _require_open(cycle: Mapping[str, Any]) -> None:
    if cycle.get("status") != "OPEN":
        raise InvalidTransitionError("the active Episode cycle is closed; reopen it for a new work cycle")


def _action_has_observed_effect(action: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(action, Mapping)
        and action.get("reconciliation_state") == ActionReconciliationState.SUCCEEDED.value
        and isinstance(action.get("observed_at"), str)
        and action.get("observed_at")
    )


def _observation_is_post_action(observation: RecoveryObservation, evaluated_at: datetime, action: Mapping[str, Any] | None) -> bool:
    boundary = action.get("observed_at") if _action_has_observed_effect(action) else None
    if not isinstance(boundary, str):
        return False
    try:
        boundary_at = datetime.fromisoformat(boundary.replace("Z", "+00:00"))
    except ValueError:
        return False
    return observation.event_at >= boundary_at and observation.observed_at >= boundary_at and evaluated_at >= boundary_at


def _history(status: str, *, actor: str, at: str, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    item = {"status": status, "actor": actor, "at": at}
    if detail:
        item["detail"] = dict(detail)
    return item


def _copy_revision(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CoherentReadConflictError("decision-loop viewed revisions are missing")
    return dict(value)


def _post_commit_revision(value: Mapping[str, Any]) -> dict[str, Any]:
    revision = dict(value)
    revision["workflow_version"] = int(revision["workflow_version"]) + 1
    return revision


def _observation_payload(observation: RecoveryObservation | None) -> dict[str, Any] | None:
    if observation is None:
        return None
    return {
        "observation_id": observation.observation_id,
        "episode_id": observation.episode_id,
        "sampling_identity": observation.sampling_identity,
        "event_at": _timestamp(observation.event_at, "event_at"),
        "observed_at": _timestamp(observation.observed_at, "observed_at"),
        "available_at": _timestamp(observation.available_at, "available_at"),
        "context": observation.context,
        "characteristic": observation.characteristic,
        "unit": observation.unit,
        "severity": observation.severity.name,
        "outcome": observation.outcome.value,
        "confidence": format(observation.confidence, ".17g"),
        "leading_hypothesis": observation.leading_hypothesis,
        "integrity_attribution": observation.integrity_attribution.value,
        "reference_valid": observation.reference_valid,
        "capability_valid": observation.capability_valid,
    }


def _observation_from_payload(payload: Mapping[str, Any]) -> RecoveryObservation:
    def parse(value: object, field: str) -> datetime:
        if not isinstance(value, str):
            raise RecoveryValidationError(f"{field} is not a timestamp")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    try:
        return RecoveryObservation(
            observation_id=str(payload["observation_id"]),
            episode_id=str(payload["episode_id"]),
            sampling_identity=str(payload["sampling_identity"]),
            event_at=parse(payload["event_at"], "event_at"),
            observed_at=parse(payload["observed_at"], "observed_at"),
            available_at=parse(payload["available_at"], "available_at") if payload.get("available_at") is not None else None,
            context=str(payload["context"]),
            characteristic=str(payload["characteristic"]),
            unit=str(payload["unit"]),
            severity=Severity[str(payload["severity"])],
            outcome=ObservationOutcome(str(payload["outcome"])),
            confidence=float(str(payload["confidence"])),
            leading_hypothesis=str(payload["leading_hypothesis"]),
            integrity_attribution=IntegrityAttribution(str(payload["integrity_attribution"])),
            reference_valid=bool(payload["reference_valid"]),
            capability_valid=bool(payload["capability_valid"]),
        )
    except (KeyError, TypeError, ValueError, RecoveryValidationError) as exc:
        raise CoherentReadConflictError("durable recovery observation is invalid") from exc


def _eligibility_payload(eligibility: Any) -> dict[str, Any]:
    return {
        "observation_id": eligibility.observation_id,
        "eligible": eligibility.eligible,
        "qualified": eligibility.qualified,
        "contradictory": eligibility.contradictory,
        "reason_codes": [code.value for code in eligibility.reason_codes],
        "criterion_results": {
            name: {
                "passed": result.passed,
                "reason_code": result.reason_code.value if result.reason_code is not None else None,
                "detail": result.detail,
            }
            for name, result in eligibility.criterion_results.items()
        },
    }


@dataclass(frozen=True, slots=True)
class DecisionLoopSnapshot:
    """One coherent read of the O3 workflow aggregate and its O5 extension."""

    episode_id: str
    scope_key: str
    aggregate_version: int
    state: dict[str, Any]
    revision_vector: RevisionVector | None

    @property
    def active_cycle_id(self) -> str:
        return str(_loop_state(self.state)["active_cycle_id"])

    @property
    def workflow_state(self) -> dict[str, Any]:
        cycle = _active_cycle(self.state)
        return {
            "work_state": self.state["work_state"],
            "owner": self.state.get("owner"),
            "workflow_version": self.aggregate_version,
            "cycle_id": cycle["cycle_id"],
            "cycle_version": cycle["cycle_version"],
            "cycle_status": cycle["status"],
        }

    @property
    def check_state(self) -> dict[str, Any]:
        return dict(_active_cycle(self.state)["checks"])

    @property
    def action_state(self) -> dict[str, Any]:
        return dict(_active_cycle(self.state)["actions"])

    @property
    def recovery_state(self) -> dict[str, Any]:
        plans = dict(_active_cycle(self.state)["recovery_plans"])
        return {
            "plans": plans,
            "technical_state": {
                plan_id: plan.get("state") for plan_id, plan in plans.items()
            },
        }

    @property
    def closure_state(self) -> dict[str, Any]:
        cycle = _active_cycle(self.state)
        loop = _loop_state(self.state)
        return {
            "cycle_status": cycle["status"],
            "closures": list(cycle["closures"]),
            "last_closure_identity": loop.get("last_closure_identity"),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "scope_key": self.scope_key,
            "aggregate_version": self.aggregate_version,
            "active_cycle_id": self.active_cycle_id,
            "workflow_state": self.workflow_state,
            "check_state": self.check_state,
            "action_state": self.action_state,
            "recovery_state": self.recovery_state,
            "closure_state": self.closure_state,
            "state": deepcopy(self.state),
            "revision_vector": self.revision_vector.as_dict() if self.revision_vector is not None else None,
        }


class DecisionLoopCommandService:
    """Thin O5.1 facade over the existing versioned command executor."""

    def __init__(self, store: Any, current_authorization: CurrentAuthorizationAuthority):
        self.store = store
        self.current_authorization = current_authorization
        self.executor = VersionedAggregateCommandExecutor(store, current_authorization)

    def _execute(
        self,
        context: CommandContext,
        episode_id: str,
        *,
        command_type: str,
        payload: Mapping[str, object],
        capability: str,
        effect: Any,
    ) -> CommandResult:
        episode_id = _identity(episode_id, "episode_id")
        return self.executor.execute(
            context,
            command_type=command_type,
            aggregate_type=EPISODE_WORKFLOW_AGGREGATE_TYPE,
            aggregate_id=episode_id,
            payload=payload,
            required_capability=capability,
            effect=effect,
        )

    def initialize_decision_loop(
        self,
        context: CommandContext,
        episode_id: str,
        *,
        cycle_id: str = "cycle-1",
    ) -> CommandResult:
        episode_id = _identity(episode_id, "episode_id")
        cycle_id = _identity(cycle_id, "cycle_id")
        revisions = _revision_payload(context)
        payload = {"episode_id": episode_id, "cycle_id": cycle_id, **revisions}

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = deepcopy(dict(current))
            if state.get("work_state") not in {"OPEN", "CLAIMED", "ACKNOWLEDGED"}:
                raise InvalidTransitionError("O5 decision-loop initialization requires an open O3 Episode workflow")
            if "owner" not in state:
                raise CoherentReadConflictError("Episode workflow has no owner field")
            if "decision_loop" in state:
                raise InvalidTransitionError("O5 decision-loop extension is already initialized")
            extension = _new_state(episode_id, str(domain["cycle_id"]), domain["viewed_revisions"])
            extension["decision_loop"]["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            state.update(extension)
            return state

        return self._execute(
            context,
            episode_id,
            command_type="InitializeDecisionLoop",
            payload=payload,
            capability=DECISION_LOOP_CREATE_CAPABILITY,
            effect=effect,
        )

    # Kept as a vocabulary-compatible alias.  It initializes only the nested
    # O5 extension; it never creates an Episode or a second aggregate.
    initialize_episode = initialize_decision_loop
    create_episode = initialize_decision_loop

    def request_check(
        self,
        context: CommandContext,
        episode_id: str,
        check_id: str,
        *,
        template_id: str,
        template_version: str,
        execution_mode: CheckExecutionMode | str,
        required_capabilities: Sequence[str] = (),
        prerequisite_state: Mapping[str, object] | None = None,
        target_context: Mapping[str, object] | None = None,
        cycle_id: str | None = None,
    ) -> CommandResult:
        check_id = _identity(check_id, "check_id")
        template_id = _identity(template_id, "template_id")
        template_version = _identity(template_version, "template_version")
        mode = _enum_value(execution_mode, CheckExecutionMode, "execution_mode")
        capabilities = _refs(required_capabilities, "required_capabilities")
        payload = {
            "check_id": check_id,
            "template_id": template_id,
            "template_version": template_version,
            "execution_mode": mode,
            "required_capabilities": capabilities,
            "prerequisite_state": _object(prerequisite_state, "prerequisite_state"),
            "target_context": _object(target_context, "target_context"),
            "cycle_id": cycle_id,
            **_revision_payload(context),
        }

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            checks = cycle["checks"]
            if check_id in checks:
                raise InvalidTransitionError("check_id is already present in this Episode cycle")
            requested_at = _now()
            checks[check_id] = {
                "check_id": check_id,
                "episode_id": episode_id,
                "cycle_id": cycle["cycle_id"],
                "template_id": domain["template_id"],
                "template_version": domain["template_version"],
                "execution_mode": domain["execution_mode"],
                "required_capabilities": list(domain["required_capabilities"]),
                "prerequisite_state": dict(domain["prerequisite_state"]),
                "target_context": dict(domain["target_context"]),
                "status": CheckLifecycle.REQUESTED.value,
                "requested_at": requested_at,
                "started_at": None,
                "completed_at": None,
                "cancelled_at": None,
                "result": None,
                "completion_history": [],
                "history": [_history(CheckLifecycle.REQUESTED.value, actor=context.principal.subject, at=requested_at)],
            }
            _loop_state(state)["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="RequestCheck", payload=payload, capability=CHECK_REQUEST_CAPABILITY, effect=effect)

    def start_check(self, context: CommandContext, episode_id: str, check_id: str, *, cycle_id: str | None = None) -> CommandResult:
        check_id = _identity(check_id, "check_id")
        payload = {"check_id": check_id, "cycle_id": cycle_id, **_revision_payload(context)}

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            check = cycle["checks"].get(check_id)
            if not isinstance(check, dict):
                raise ValidationFailureError("check_id is not present in this Episode cycle")
            if check["status"] != CheckLifecycle.REQUESTED.value:
                raise InvalidTransitionError("only a requested check can start")
            started_at = _now()
            check["status"] = CheckLifecycle.STARTED.value
            check["started_at"] = started_at
            check["history"].append(_history(CheckLifecycle.STARTED.value, actor=context.principal.subject, at=started_at))
            _loop_state(state)["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="StartCheck", payload=payload, capability=CHECK_EXECUTE_CAPABILITY, effect=effect)

    def complete_check(
        self,
        context: CommandContext,
        episode_id: str,
        check_id: str,
        *,
        outcome: CheckOutcome | str,
        evidence_refs: Sequence[str] = (),
        known_at: datetime | None = None,
        published_at: datetime | None = None,
        result_identity: str | None = None,
        cycle_id: str | None = None,
    ) -> CommandResult:
        check_id = _identity(check_id, "check_id")
        outcome_value = _enum_value(outcome, CheckOutcome, "outcome")
        references = _refs(evidence_refs)
        if outcome_value in {CheckOutcome.SUPPORTS_A.value, CheckOutcome.SUPPORTS_B.value} and not references:
            raise ValidationFailureError("affirmative check results require evidence references")
        known_text = _timestamp(known_at, "known_at")
        published_text = _timestamp(published_at, "published_at")
        if known_text is not None and published_text is not None and published_text < known_text:
            raise ValidationFailureError("published_at cannot precede known_at")
        if result_identity is not None:
            result_identity = _identity(result_identity, "result_identity")
        payload = {
            "check_id": check_id,
            "outcome": outcome_value,
            "evidence_refs": references,
            "known_at": known_text,
            "published_at": published_text,
            "result_identity": result_identity,
            "cycle_id": cycle_id,
            **_revision_payload(context),
        }

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            check = cycle["checks"].get(check_id)
            if not isinstance(check, dict):
                raise ValidationFailureError("check_id is not present in this Episode cycle")
            if check["status"] != CheckLifecycle.STARTED.value:
                raise InvalidTransitionError("only a started check can complete")
            completed_at = _now()
            identity = domain["result_identity"] or hashlib.sha256(
                canonical_json({
                    "check_id": check_id,
                    "outcome": domain["outcome"],
                    "evidence_refs": domain["evidence_refs"],
                    "known_at": domain["known_at"],
                    "published_at": domain["published_at"],
                }).encode("utf-8")
            ).hexdigest()
            result = {
                "result_identity": identity,
                "outcome": domain["outcome"],
                "affirmative": domain["outcome"] in {CheckOutcome.SUPPORTS_A.value, CheckOutcome.SUPPORTS_B.value},
                "evidence_refs": list(domain["evidence_refs"]),
                "known_at": domain["known_at"],
                "published_at": domain["published_at"],
                "completed_at": completed_at,
            }
            check["status"] = CheckLifecycle.COMPLETED.value
            check["completed_at"] = completed_at
            check["result"] = result
            check["completion_history"].append(deepcopy(result))
            check["history"].append(_history(CheckLifecycle.COMPLETED.value, actor=context.principal.subject, at=completed_at, detail={"result_identity": identity}))
            _loop_state(state)["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="CompleteCheck", payload=payload, capability=CHECK_EXECUTE_CAPABILITY, effect=effect)

    def cancel_check(self, context: CommandContext, episode_id: str, check_id: str, *, cycle_id: str | None = None) -> CommandResult:
        check_id = _identity(check_id, "check_id")
        payload = {"check_id": check_id, "cycle_id": cycle_id, **_revision_payload(context)}

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            check = cycle["checks"].get(check_id)
            if not isinstance(check, dict):
                raise ValidationFailureError("check_id is not present in this Episode cycle")
            if check["status"] not in {CheckLifecycle.REQUESTED.value, CheckLifecycle.STARTED.value}:
                raise InvalidTransitionError("only a requested or started check can cancel")
            cancelled_at = _now()
            check["status"] = CheckLifecycle.CANCELLED.value
            check["cancelled_at"] = cancelled_at
            check["history"].append(_history(CheckLifecycle.CANCELLED.value, actor=context.principal.subject, at=cancelled_at))
            _loop_state(state)["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="CancelCheck", payload=payload, capability=CHECK_EXECUTE_CAPABILITY, effect=effect)

    def record_external_action(
        self,
        context: CommandContext,
        episode_id: str,
        action_id: str,
        *,
        action_type: ExternalActionType | str,
        external_system: str,
        work_request_id: str | None = None,
        external_reference: str | None = None,
        proposal_id: str | None = None,
        decision_revision_identity: str | None = None,
        reconciliation_state: ActionReconciliationState | str = ActionReconciliationState.UNKNOWN,
        requested_at: datetime | None = None,
        authorized_at: datetime | None = None,
        observed_at: datetime | None = None,
        evidence_refs: Sequence[str] = (),
        cycle_id: str | None = None,
    ) -> CommandResult:
        action_id = _identity(action_id, "action_id")
        action_type_value = _enum_value(action_type, ExternalActionType, "action_type")
        external_system = _identity(external_system, "external_system")
        work_request_id = _identity(work_request_id, "work_request_id") if work_request_id is not None else None
        external_reference = _identity(external_reference, "external_reference") if external_reference is not None else None
        if work_request_id is None and external_reference is None:
            raise ValidationFailureError("external action requires a work request or external reference identity")
        reconciliation_value = _enum_value(reconciliation_state, ActionReconciliationState, "reconciliation_state")
        references = _refs(evidence_refs)
        if reconciliation_value in {ActionReconciliationState.SUCCEEDED.value, ActionReconciliationState.FAILED.value} and not references:
            raise ValidationFailureError("reconciled external outcomes require evidence references")
        if reconciliation_value in {ActionReconciliationState.SUCCEEDED.value, ActionReconciliationState.FAILED.value} and observed_at is None:
            raise ValidationFailureError("reconciled external outcomes require an explicit observed_at boundary")
        payload = {
            "action_id": action_id,
            "action_type": action_type_value,
            "external_system": external_system,
            "work_request_id": work_request_id,
            "external_reference": external_reference,
            "proposal_id": _identity(proposal_id, "proposal_id") if proposal_id is not None else None,
            "decision_revision_identity": _identity(decision_revision_identity, "decision_revision_identity") if decision_revision_identity is not None else None,
            "reconciliation_state": reconciliation_value,
            "requested_at": _timestamp(requested_at, "requested_at", default_now=True),
            "authorized_at": _timestamp(authorized_at, "authorized_at"),
            "observed_at": _timestamp(observed_at, "observed_at"),
            "evidence_refs": references,
            "cycle_id": cycle_id,
            **_revision_payload(context),
        }

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            actions = cycle["actions"]
            if action_id in actions:
                raise InvalidTransitionError("action_id is already present in this Episode cycle")
            at = domain["authorized_at"] or domain["requested_at"]
            actions[action_id] = {
                "action_id": action_id,
                "episode_id": episode_id,
                "cycle_id": cycle["cycle_id"],
                "proposal_id": domain["proposal_id"],
                "authorization_subject": context.principal.subject,
                "decision_revision_identity": domain["decision_revision_identity"],
                "viewed_revisions": dict(domain["viewed_revisions"]),
                "external_system": domain["external_system"],
                "work_request_id": domain["work_request_id"],
                "external_reference": domain["external_reference"],
                "action_type": domain["action_type"],
                "requested_at": domain["requested_at"],
                "authorized_at": domain["authorized_at"],
                "observed_at": domain["observed_at"],
                "reconciliation_state": domain["reconciliation_state"],
                "evidence_refs": list(domain["evidence_refs"]),
                "effect_boundary_at": domain["observed_at"] if reconciliation_value == ActionReconciliationState.SUCCEEDED.value else None,
                "history": [_history(domain["reconciliation_state"], actor=context.principal.subject, at=at)],
            }
            _loop_state(state)["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="RecordExternalAction", payload=payload, capability=ACTION_RECORD_CAPABILITY, effect=effect)

    def reconcile_external_action(
        self,
        context: CommandContext,
        episode_id: str,
        action_id: str,
        *,
        reconciliation_state: ActionReconciliationState | str,
        external_reference: str | None = None,
        observed_at: datetime | None = None,
        evidence_refs: Sequence[str] = (),
        cycle_id: str | None = None,
    ) -> CommandResult:
        action_id = _identity(action_id, "action_id")
        target = _enum_value(reconciliation_state, ActionReconciliationState, "reconciliation_state")
        if target in {ActionReconciliationState.SUCCEEDED.value, ActionReconciliationState.FAILED.value} and not _refs(evidence_refs):
            raise ValidationFailureError("reconciled external outcomes require evidence references")
        payload = {
            "action_id": action_id,
            "reconciliation_state": target,
            "external_reference": _identity(external_reference, "external_reference") if external_reference is not None else None,
            "observed_at": _timestamp(observed_at, "observed_at", default_now=True),
            "evidence_refs": _refs(evidence_refs),
            "cycle_id": cycle_id,
            **_revision_payload(context),
        }

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            action = cycle["actions"].get(action_id)
            if not isinstance(action, dict):
                raise ValidationFailureError("action_id is not present in this Episode cycle")
            current_state = action["reconciliation_state"]
            if current_state not in {ActionReconciliationState.UNKNOWN.value, ActionReconciliationState.PENDING.value}:
                raise InvalidTransitionError("a reconciled external action cannot be changed")
            if domain["reconciliation_state"] == ActionReconciliationState.UNKNOWN.value:
                raise InvalidTransitionError("external UNKNOWN remains UNKNOWN until a new explicit reconciliation")
            action["reconciliation_state"] = domain["reconciliation_state"]
            if domain["external_reference"] is not None:
                action["external_reference"] = domain["external_reference"]
            action["observed_at"] = domain["observed_at"]
            action["evidence_refs"] = list(domain["evidence_refs"])
            action["effect_boundary_at"] = domain["observed_at"] if domain["reconciliation_state"] == ActionReconciliationState.SUCCEEDED.value else None
            action["history"].append(_history(domain["reconciliation_state"], actor=context.principal.subject, at=domain["observed_at"]))
            _loop_state(state)["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="ReconcileExternalAction", payload=payload, capability=ACTION_RECORD_CAPABILITY, effect=effect)

    def create_recovery_plan(
        self,
        context: CommandContext,
        episode_id: str,
        recovery_plan_id: str,
        *,
        policy: RecoveryPolicy,
        policy_version: str = "1",
        prior_action_id: str | None = None,
        context_identity: str,
        characteristic_identity: str,
        unit_identity: str,
        matched_reference_id: str | None = None,
        matched_cohort_id: str | None = None,
        capability_references: Sequence[str] = (),
        elapsed_duration_seconds: int | None = None,
        gap_requirements: Mapping[str, object] | None = None,
        channel_requirements: Mapping[str, object] | None = None,
        failure_reset_rules: Mapping[str, object] | None = None,
        cycle_id: str | None = None,
    ) -> CommandResult:
        recovery_plan_id = _identity(recovery_plan_id, "recovery_plan_id")
        context_identity = _identity(context_identity, "context_identity")
        characteristic_identity = _identity(characteristic_identity, "characteristic_identity")
        unit_identity = _identity(unit_identity, "unit_identity")
        prior_action_id = _identity(prior_action_id, "prior_action_id") if prior_action_id is not None else None
        matched_reference_id = _identity(matched_reference_id, "matched_reference_id") if matched_reference_id is not None else None
        matched_cohort_id = _identity(matched_cohort_id, "matched_cohort_id") if matched_cohort_id is not None else None
        capabilities = _refs(capability_references, "capability_references")
        if elapsed_duration_seconds is not None and (isinstance(elapsed_duration_seconds, bool) or not isinstance(elapsed_duration_seconds, int) or elapsed_duration_seconds <= 0):
            raise ValidationFailureError("elapsed_duration_seconds must be a positive integer when configured")
        policy_snapshot = _policy_snapshot(policy, policy_version)
        payload = {
            "recovery_plan_id": recovery_plan_id,
            "prior_action_id": prior_action_id,
            "policy_snapshot": policy_snapshot,
            "policy_hash": _policy_hash(policy_snapshot),
            "context_identity": context_identity,
            "characteristic_identity": characteristic_identity,
            "unit_identity": unit_identity,
            "matched_reference_id": matched_reference_id,
            "matched_cohort_id": matched_cohort_id,
            "capability_references": capabilities,
            "minimum_eligible_independent_evidence": policy.minimum_eligible_independent_samples,
            "elapsed_duration_seconds": elapsed_duration_seconds,
            "gap_requirements": _object(gap_requirements, "gap_requirements"),
            "channel_requirements": _object(channel_requirements, "channel_requirements"),
            "failure_reset_rules": _object(failure_reset_rules, "failure_reset_rules"),
            "cycle_id": cycle_id,
            **_revision_payload(context),
        }

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            plans = cycle["recovery_plans"]
            if recovery_plan_id in plans:
                raise InvalidTransitionError("recovery_plan_id is already present in this Episode cycle")
            bound_action = None
            if domain["prior_action_id"] is not None:
                bound_action = cycle["actions"].get(domain["prior_action_id"])
                if not isinstance(bound_action, dict):
                    raise ValidationFailureError("prior_action_id must resolve to an action in the active Episode cycle")
            plans[recovery_plan_id] = {
                "recovery_plan_id": recovery_plan_id,
                "episode_id": episode_id,
                "cycle_id": cycle["cycle_id"],
                "prior_action_id": domain["prior_action_id"],
                "bound_action_cycle_id": bound_action.get("cycle_id") if bound_action is not None else None,
                "effect_boundary_at": bound_action.get("observed_at") if _action_has_observed_effect(bound_action) else None,
                "policy_snapshot": deepcopy(domain["policy_snapshot"]),
                "policy_hash": domain["policy_hash"],
                "context_identity": domain["context_identity"],
                "characteristic_identity": domain["characteristic_identity"],
                "unit_identity": domain["unit_identity"],
                "matched_reference_id": domain["matched_reference_id"],
                "matched_cohort_id": domain["matched_cohort_id"],
                "capability_references": list(domain["capability_references"]),
                "minimum_eligible_independent_evidence": domain["minimum_eligible_independent_evidence"],
                "elapsed_duration_seconds": domain["elapsed_duration_seconds"],
                "gap_requirements": dict(domain["gap_requirements"]),
                "channel_requirements": dict(domain["channel_requirements"]),
                "failure_reset_rules": dict(domain["failure_reset_rules"]),
                "state": RecoveryPlanState.NOT_STARTED.value,
                "locked": False,
                "locked_at": None,
                "observations": [],
                "assessments": [],
                "eligible_sampling_identities": [],
                "assessment_count": 0,
                "eligible_independent_count": 0,
                "history": [_history(RecoveryPlanState.NOT_STARTED.value, actor=context.principal.subject, at=_now())],
            }
            _loop_state(state)["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="CreateRecoveryPlan", payload=payload, capability=RECOVERY_PLAN_CAPABILITY, effect=effect)

    def update_recovery_plan(
        self,
        context: CommandContext,
        episode_id: str,
        recovery_plan_id: str,
        *,
        policy: RecoveryPolicy | None = None,
        policy_version: str = "1",
        matched_reference_id: str | None = None,
        matched_cohort_id: str | None = None,
        cycle_id: str | None = None,
    ) -> CommandResult:
        recovery_plan_id = _identity(recovery_plan_id, "recovery_plan_id")
        policy_snapshot = _policy_snapshot(policy, policy_version) if policy is not None else None
        payload = {
            "recovery_plan_id": recovery_plan_id,
            "policy_snapshot": policy_snapshot,
            "policy_hash": _policy_hash(policy_snapshot) if policy_snapshot is not None else None,
            "matched_reference_id": _identity(matched_reference_id, "matched_reference_id") if matched_reference_id is not None else None,
            "matched_cohort_id": _identity(matched_cohort_id, "matched_cohort_id") if matched_cohort_id is not None else None,
            "cycle_id": cycle_id,
            **_revision_payload(context),
        }

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            plan = cycle["recovery_plans"].get(recovery_plan_id)
            if not isinstance(plan, dict):
                raise ValidationFailureError("recovery_plan_id is not present in this Episode cycle")
            if plan["locked"]:
                raise InvalidTransitionError("decision-critical recovery policy is locked")
            if domain["policy_snapshot"] is not None:
                plan["policy_snapshot"] = deepcopy(domain["policy_snapshot"])
                plan["policy_hash"] = domain["policy_hash"]
                plan["minimum_eligible_independent_evidence"] = plan["policy_snapshot"]["configuration"]["minimum_eligible_independent_samples"]
            for field in ("matched_reference_id", "matched_cohort_id"):
                if domain[field] is not None:
                    plan[field] = domain[field]
            plan["history"].append(_history("UPDATED", actor=context.principal.subject, at=_now()))
            _loop_state(state)["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="UpdateRecoveryPlan", payload=payload, capability=RECOVERY_PLAN_CAPABILITY, effect=effect)

    def lock_recovery_plan(self, context: CommandContext, episode_id: str, recovery_plan_id: str, *, cycle_id: str | None = None) -> CommandResult:
        recovery_plan_id = _identity(recovery_plan_id, "recovery_plan_id")
        payload = {"recovery_plan_id": recovery_plan_id, "cycle_id": cycle_id, **_revision_payload(context)}

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            plan = cycle["recovery_plans"].get(recovery_plan_id)
            if not isinstance(plan, dict):
                raise ValidationFailureError("recovery_plan_id is not present in this Episode cycle")
            if plan["locked"]:
                raise InvalidTransitionError("recovery plan is already locked")
            plan["locked"] = True
            plan["locked_at"] = _now()
            plan["state"] = RecoveryPlanState.WAITING_FOR_DATA.value
            plan["history"].append(_history(RecoveryPlanState.WAITING_FOR_DATA.value, actor=context.principal.subject, at=plan["locked_at"]))
            _loop_state(state)["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="LockRecoveryPlan", payload=payload, capability=RECOVERY_PLAN_CAPABILITY, effect=effect)

    def submit_recovery_observation(
        self,
        context: CommandContext,
        episode_id: str,
        recovery_plan_id: str,
        observation: RecoveryObservation | None,
        *,
        evaluated_at: datetime,
        evidence_refs: Sequence[str] = (),
        cycle_id: str | None = None,
    ) -> CommandResult:
        recovery_plan_id = _identity(recovery_plan_id, "recovery_plan_id")
        if not isinstance(evaluated_at, datetime) or evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValidationFailureError("evaluated_at must be timezone-aware")
        if observation is not None and not isinstance(observation, RecoveryObservation):
            raise ValidationFailureError("observation must be a RecoveryObservation or None")
        if observation is not None and observation.episode_id != episode_id:
            raise ValidationFailureError("observation episode_id does not match the Episode")
        payload = {
            "recovery_plan_id": recovery_plan_id,
            "observation": _observation_payload(observation),
            "evaluated_at": _timestamp(evaluated_at, "evaluated_at"),
            "evidence_refs": _refs(evidence_refs),
            "cycle_id": cycle_id,
            **_revision_payload(context),
        }

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            plan = cycle["recovery_plans"].get(recovery_plan_id)
            if not isinstance(plan, dict):
                raise ValidationFailureError("recovery_plan_id is not present in this Episode cycle")
            if plan["state"] in {RecoveryPlanState.PASS.value, RecoveryPlanState.INVALIDATED.value}:
                raise InvalidTransitionError("recovery plan cannot collect observations after terminal state")
            if not plan["locked"]:
                plan["locked"] = True
                plan["locked_at"] = _now()
            bound_action = None
            if plan.get("prior_action_id") is not None:
                bound_action = cycle["actions"].get(plan["prior_action_id"])
                if not isinstance(bound_action, dict):
                    raise CoherentReadConflictError("locked recovery plan action binding is not in the active cycle")
                if bound_action.get("cycle_id") != cycle.get("cycle_id"):
                    raise CoherentReadConflictError("recovery plan action binding crosses Episode cycles")
                if _action_has_observed_effect(bound_action):
                    plan["effect_boundary_at"] = bound_action.get("observed_at")
            policy = _policy_from_snapshot(plan["policy_snapshot"])
            evaluator = RecoveryEvaluator(policy)
            prior_observations = [_observation_from_payload(item) for item in plan["observations"]]
            submitted = _observation_from_payload(domain["observation"]) if domain["observation"] is not None else None
            evaluated_at_value = datetime.fromisoformat(str(domain["evaluated_at"]).replace("Z", "+00:00"))
            try:
                eligibility = evaluator.evaluate(
                    submitted,
                    evaluated_at=evaluated_at_value,
                    known_observation_ids={item.observation_id for item in prior_observations},
                    known_sampling_identities={item.sampling_identity for item in prior_observations},
                )
            except RecoveryValidationError as exc:
                raise ValidationFailureError("recovery observation failed deterministic validation") from exc
            post_action = bool(
                submitted is not None
                and (
                    plan.get("prior_action_id") is None
                    or _observation_is_post_action(submitted, evaluated_at_value, bound_action)
                )
            )
            assessment = {
                "observation": deepcopy(domain["observation"]),
                "evaluated_at": domain["evaluated_at"],
                "evidence_refs": list(domain["evidence_refs"]),
                "eligibility": _eligibility_payload(eligibility),
                "post_action": post_action,
            }
            plan["assessments"].append(assessment)
            plan["assessment_count"] = len(plan["assessments"])
            if submitted is not None:
                plan["observations"].append(deepcopy(domain["observation"]))
            eligible_ids = set(plan["eligible_sampling_identities"])
            if eligibility.contradictory:
                # The existing W0 integrity rule is deterministic: a qualified
                # contradiction resets progress.  It does not close the Episode.
                eligible_ids.clear()
                plan["state"] = RecoveryPlanState.FAIL.value
            else:
                if eligibility.eligible and submitted is not None and post_action:
                    eligible_ids.add(submitted.sampling_identity)
                if len(eligible_ids) >= policy.minimum_eligible_independent_samples:
                    plan["state"] = RecoveryPlanState.PASS.value
                elif eligible_ids:
                    plan["state"] = RecoveryPlanState.MONITORING.value
                else:
                    plan["state"] = RecoveryPlanState.WAITING_FOR_DATA.value
            plan["eligible_sampling_identities"] = sorted(eligible_ids)
            plan["eligible_independent_count"] = len(eligible_ids)
            plan["history"].append(_history(plan["state"], actor=context.principal.subject, at=domain["evaluated_at"], detail={"observation_id": eligibility.observation_id, "reason_codes": [code.value for code in eligibility.reason_codes]}))
            _loop_state(state)["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="SubmitRecoveryObservation", payload=payload, capability=RECOVERY_OBSERVATION_CAPABILITY, effect=effect)

    def close_episode(
        self,
        context: CommandContext,
        episode_id: str,
        *,
        disposition: ClosureDisposition | str,
        evidence_refs: Sequence[str] = (),
        reason: str | None = None,
        residual_risk_owner: str | None = None,
        obligation_id: str | None = None,
        accepted_residual_risk: bool = False,
        exception_review_id: str | None = None,
        recovery_plan_id: str | None = None,
        check_ids: Sequence[str] = (),
        cycle_id: str | None = None,
    ) -> CommandResult:
        disposition_value = _enum_value(disposition, ClosureDisposition, "disposition")
        references = _refs(evidence_refs)
        if reason is not None:
            reason = _identity(reason, "reason")
        if residual_risk_owner is not None:
            residual_risk_owner = _identity(residual_risk_owner, "residual_risk_owner")
        if obligation_id is not None:
            obligation_id = _identity(obligation_id, "obligation_id")
        if exception_review_id is not None:
            exception_review_id = _identity(exception_review_id, "exception_review_id")
        recovery_plan_id = _identity(recovery_plan_id, "recovery_plan_id") if recovery_plan_id is not None else None
        selected_check_ids = _refs(check_ids, "check_ids")
        if not isinstance(accepted_residual_risk, bool):
            raise ValidationFailureError("accepted_residual_risk must be a boolean")
        payload = {
            "disposition": disposition_value,
            "evidence_refs": references,
            "reason": reason,
            "residual_risk_owner": residual_risk_owner,
            "obligation_id": obligation_id,
            "accepted_residual_risk": accepted_residual_risk,
            "exception_review_id": exception_review_id,
            "recovery_plan_id": recovery_plan_id,
            "check_ids": selected_check_ids,
            "cycle_id": cycle_id,
            **_revision_payload(context),
        }

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            cycle = _active_cycle(state, domain.get("cycle_id"))
            _require_open(cycle)
            plans = cycle["recovery_plans"]
            selected_plan = None
            recovery_evidence_ids: list[str] = []
            if domain["disposition"] == ClosureDisposition.CONFIRMED_ISSUE.value:
                selected_id = domain["recovery_plan_id"]
                if not selected_id:
                    raise ValidationFailureError("confirmed-issue closure requires an exact recovery_plan_id")
                selected_plan = plans.get(selected_id)
                if not isinstance(selected_plan, dict):
                    raise ValidationFailureError("recovery_plan_id is not present in this Episode cycle")
                if selected_plan.get("cycle_id") != cycle.get("cycle_id"):
                    raise InvalidTransitionError("recovery plan is not in the active Episode cycle")
                if selected_plan.get("state") != RecoveryPlanState.PASS.value or not selected_plan.get("locked"):
                    raise InvalidTransitionError("confirmed-issue closure requires the exact locked PASS recovery plan")
                action_id = selected_plan.get("prior_action_id")
                if not isinstance(action_id, str) or not action_id:
                    raise InvalidTransitionError("confirmed-issue closure requires a recovery plan bound to an action")
                action = cycle["actions"].get(action_id)
                if not isinstance(action, dict) or action.get("cycle_id") != cycle.get("cycle_id"):
                    raise InvalidTransitionError("recovery plan action binding is not in the active Episode cycle")
                if not _action_has_observed_effect(action):
                    raise InvalidTransitionError("confirmed-issue closure requires a qualifying reconciled action")
                for assessment in selected_plan.get("assessments", []):
                    if not isinstance(assessment, Mapping) or not assessment.get("post_action"):
                        continue
                    eligibility = assessment.get("eligibility")
                    if isinstance(eligibility, Mapping) and eligibility.get("eligible"):
                        recovery_evidence_ids.extend(_refs(assessment.get("evidence_refs", ())))
                if not recovery_evidence_ids:
                    raise InvalidTransitionError("confirmed-issue closure requires post-action recovery evidence")
            elif domain["disposition"] == ClosureDisposition.EXCEPTION.value:
                if not domain["exception_review_id"] or not domain["residual_risk_owner"] or not domain["evidence_refs"]:
                    raise ValidationFailureError("exception closure requires review, residual-risk owner, and evidence")
            elif domain["disposition"] == ClosureDisposition.UNRESOLVED.value:
                has_obligation = bool(domain["obligation_id"] and domain["residual_risk_owner"])
                has_accepted_risk = bool(domain["accepted_residual_risk"] and domain["residual_risk_owner"])
                if not (has_obligation or has_accepted_risk):
                    raise ValidationFailureError("UNRESOLVED closure requires an obligation/owner or accepted residual risk")
            elif domain["disposition"] in {
                ClosureDisposition.BENIGN.value,
                ClosureDisposition.DUPLICATE.value,
                ClosureDisposition.DATA_INTEGRITY.value,
            } and not domain["evidence_refs"]:
                raise ValidationFailureError("this closure disposition requires evidence references")
            check_ids = domain["check_ids"] or sorted(cycle["checks"])
            if any(check_id not in cycle["checks"] for check_id in check_ids):
                raise ValidationFailureError("closure check_ids must refer to checks in the active Episode cycle")
            action_ids = sorted(cycle["actions"])
            evidence_ids = _refs(tuple(domain["evidence_refs"]) + tuple(recovery_evidence_ids), "evidence_ids")
            closure_at = _now()
            closure_identity = hashlib.sha256(canonical_json({
                "episode_id": episode_id,
                "cycle_id": cycle["cycle_id"],
                "disposition": domain["disposition"],
                "viewed_revisions": domain["viewed_revisions"],
                "evidence_refs": domain["evidence_refs"],
                "check_ids": check_ids,
                "action_ids": action_ids,
                "recovery_plan_id": domain["recovery_plan_id"],
                "evidence_ids": evidence_ids,
            }).encode("utf-8")).hexdigest()
            closure = {
                "closure_identity": closure_identity,
                "episode_id": episode_id,
                "cycle_id": cycle["cycle_id"],
                "disposition": domain["disposition"],
                "reason": domain["reason"],
                "evidence_refs": list(domain["evidence_refs"]),
                "residual_risk_owner": domain["residual_risk_owner"],
                "obligation_id": domain["obligation_id"],
                "accepted_residual_risk": domain["accepted_residual_risk"],
                "exception_review_id": domain["exception_review_id"],
                "action_id": selected_plan.get("prior_action_id") if selected_plan is not None else None,
                "recovery_plan_id": domain["recovery_plan_id"],
                "viewed_revisions": _copy_revision(domain["viewed_revisions"]),
                "workflow_version": int(context.expected_workflow_version) + 1,
                "viewed_workflow_version": context.expected_workflow_version,
                "check_ids": list(check_ids),
                "action_ids": action_ids,
                "recovery_plan_ids": sorted(cycle["recovery_plans"]),
                "evidence_ids": evidence_ids,
                "closed_at": closure_at,
                "closed_by": context.principal.subject,
            }
            cycle["closures"].append(closure)
            cycle["status"] = "CLOSED"
            state["work_state"] = "CLOSED"
            loop = _loop_state(state)
            loop["last_closure_identity"] = closure_identity
            loop["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="CloseEpisode", payload=payload, capability=CLOSURE_CAPABILITY, effect=effect)

    def reopen_episode(
        self,
        context: CommandContext,
        episode_id: str,
        *,
        reason: str,
        cycle_id: str | None = None,
    ) -> CommandResult:
        reason = _identity(reason, "reason")
        payload = {"reason": reason, "cycle_id": cycle_id, **_revision_payload(context)}

        def effect(current: Mapping[str, Any], domain: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _state_object(current, episode_id)
            previous = _active_cycle(state, domain.get("cycle_id"))
            if previous["status"] != "CLOSED" or not previous["closures"]:
                raise InvalidTransitionError("only a closed Episode cycle can be reopened")
            loop = _loop_state(state)
            cycle_version = max(int(cycle.get("cycle_version", 0)) for cycle in loop["cycles"]) + 1
            new_cycle_id = f"{episode_id}:cycle-{cycle_version}"
            while any(cycle.get("cycle_id") == new_cycle_id for cycle in loop["cycles"]):
                cycle_version += 1
                new_cycle_id = f"{episode_id}:cycle-{cycle_version}"
            reopened_at = _now()
            loop["reopen_history"].append({
                "reopen_identity": hashlib.sha256(f"{episode_id}:{previous['cycle_id']}:{new_cycle_id}:{reopened_at}".encode("utf-8")).hexdigest(),
                "from_cycle_id": previous["cycle_id"],
                "from_closure_identity": previous["closures"][-1]["closure_identity"],
                "to_cycle_id": new_cycle_id,
                "reason": domain["reason"],
                "reopened_at": reopened_at,
                "reopened_by": context.principal.subject,
                "owner_before_reset": state.get("owner"),
                "owner_after_reset": None,
                "viewed_revisions": _copy_revision(domain["viewed_revisions"]),
            })
            loop["cycles"].append(_empty_cycle(new_cycle_id, cycle_version, reopened_at))
            loop["active_cycle_id"] = new_cycle_id
            state["work_state"] = "OPEN"
            state["owner"] = None
            loop["last_viewed_revisions"] = _post_commit_revision(domain["viewed_revisions"])
            return state

        return self._execute(context, episode_id, command_type="ReopenEpisode", payload=payload, capability=REOPEN_CAPABILITY, effect=effect)

    def get_decision_loop(self, principal: Principal, scope: AccessScope, episode_id: str) -> DecisionLoopSnapshot:
        episode_id = _identity(episode_id, "episode_id")
        self.current_authorization.authorize(principal, scope, DECISION_LOOP_READ_CAPABILITY)
        aggregate = self.store.get_aggregate(scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, episode_id)
        if aggregate is None:
            raise AggregateNotFoundError("Episode workflow is not available in the requested scope")
        state = _state_object(aggregate.state, episode_id)
        cycle = _active_cycle(state)
        revisions = _loop_state(state).get("last_viewed_revisions")
        revision_vector = None
        if revisions is not None:
            try:
                revision_vector = RevisionVector(
                    revisions["analysis_revision"],
                    revisions.get("exposure_revision"),
                    revisions.get("priority_revision"),
                    aggregate.version,
                    revisions.get("plan_version"),
                    revisions["qualification_manifest_id"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise CoherentReadConflictError("decision-loop revision vector is invalid") from exc
            if revision_vector.workflow_version != aggregate.version:
                raise CoherentReadConflictError("decision-loop revision vector is not the episode_workflow version")
        return DecisionLoopSnapshot(episode_id, aggregate.scope_key, aggregate.version, state, revision_vector)

    read_decision_loop = get_decision_loop


__all__ = [
    "ACTION_RECORD_CAPABILITY",
    "ActionReconciliationState",
    "CHECK_EXECUTE_CAPABILITY",
    "CHECK_REQUEST_CAPABILITY",
    "CLOSURE_CAPABILITY",
    "CheckExecutionMode",
    "CheckLifecycle",
    "CheckOutcome",
    "ClosureDisposition",
    "DECISION_LOOP_AGGREGATE_TYPE",
    "DECISION_LOOP_CREATE_CAPABILITY",
    "DECISION_LOOP_READ_CAPABILITY",
    "DECISION_LOOP_SCHEMA_VERSION",
    "DecisionLoopCommandService",
    "DecisionLoopSnapshot",
    "ExternalActionType",
    "RECOVERY_OBSERVATION_CAPABILITY",
    "RECOVERY_PLAN_CAPABILITY",
    "REOPEN_CAPABILITY",
    "RecoveryPlanState",
]
