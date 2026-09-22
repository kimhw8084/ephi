"""O5.2 durable decision snapshots and authorized handoff delivery.

This module contains the renderer-independent contracts.  The Episode
workflow remains the only workflow authority; snapshots and handoffs are
bounded, append-oriented records derived from that authority and from the
committed O2 outbox.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Protocol, runtime_checkable

from .context import AccessScope, CommandContext, CurrentAuthorizationAuthority, Principal, RevisionVector
from .errors import (
    AuthorizationDeniedError,
    InvalidTransitionError,
    StorageFailureError,
    ValidationFailureError,
    VersionConflictError,
)
from .hashing import canonical_json, normalize_domain_payload
from .storage import AggregateSnapshot
from .worker import AppliedEffectReceipt, JobRecord, WorkerJobPort, WorkerLease


DECISION_SNAPSHOT_CREATE_CAPABILITY = "decision_snapshot.create"
DECISION_SNAPSHOT_READ_CAPABILITY = "decision_snapshot.read"
HANDOFF_CREATE_CAPABILITY = "handoff.create"
HANDOFF_READ_CAPABILITY = "handoff.read"
DELIVERY_DISPATCH_CAPABILITY = "handoff.delivery.dispatch"
DELIVERY_RECONCILE_CAPABILITY = "handoff.delivery.reconcile"

HANDOFF_DELIVERY_JOB_TYPE = "HANDOFF_DELIVERY"
DELIVERY_POLICY_VERSION = "o5.2.v1"

ASSIGNMENT_HANDOFF = "ASSIGNMENT_HANDOFF"
CHECK_RESULT_REQUIRES_ACTION = "CHECK_RESULT_REQUIRES_ACTION"
ACTION_RECORDED = "ACTION_RECORDED"
ACTION_OUTCOME_UNKNOWN = "ACTION_OUTCOME_UNKNOWN"
ACTION_FAILED = "ACTION_FAILED"
RECOVERY_FAILED = "RECOVERY_FAILED"
RECOVERY_PASSED_REVIEW_REQUIRED = "RECOVERY_PASSED_REVIEW_REQUIRED"
CLOSURE = "CLOSURE"
REOPEN = "REOPEN"
EVENT_KINDS = frozenset(
    {
        ASSIGNMENT_HANDOFF,
        CHECK_RESULT_REQUIRES_ACTION,
        ACTION_RECORDED,
        ACTION_OUTCOME_UNKNOWN,
        ACTION_FAILED,
        RECOVERY_FAILED,
        RECOVERY_PASSED_REVIEW_REQUIRED,
        CLOSURE,
        REOPEN,
    }
)

# O5.2 is a projector, not a second event authority.  Keep this policy
# deliberately narrow: these O2 command types already carry the complete
# workflow meaning needed by the handoff vocabulary.  Commands not listed
# here are valid O2/O3/O5 commands, but are not notification triggers.
_EVENT_KIND_BY_COMMAND = {
    "ClaimEpisode": ASSIGNMENT_HANDOFF,
    "RecordExternalAction": ACTION_RECORDED,
    "CloseEpisode": CLOSURE,
    "ReopenEpisode": REOPEN,
}

PENDING = "PENDING"
DISPATCHING = "DISPATCHING"
DELIVERED = "DELIVERED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
CANCELED = "CANCELED"
SUPERSEDED = "SUPERSEDED"
DELIVERY_STATES = frozenset({PENDING, DISPATCHING, DELIVERED, FAILED, UNKNOWN, CANCELED, SUPERSEDED})

_MAX_TEXT = 512
_MAX_LIST = 64
_MAX_JSON_BYTES = 64 * 1024
_FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "raw_measurements",
        "measurements",
        "raw_materials",
        "materials",
        "wip",
        "raw_wip",
        "secret",
        "token",
        "credential",
        "authorization",
        "signed_url",
        "object_store_url",
        "artifact_url",
        "download_url",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    if len(value) > _MAX_TEXT:
        raise ValidationFailureError(f"{field} exceeds the bounded length")
    return value


def _bounded(value: object, field: str = "content", *, depth: int = 0) -> object:
    if depth > 6:
        raise ValidationFailureError(f"{field} is too deeply nested")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TEXT:
            raise ValidationFailureError(f"{field} contains an overlong string")
        lowered = value.lower()
        if any(marker in lowered for marker in ("http://", "https://", "s3://", "gs://", "az://")):
            raise ValidationFailureError(f"{field} contains an unrestricted URL")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_LIST:
            raise ValidationFailureError(f"{field} contains too many fields")
        result: dict[str, object] = {}
        for key, item in value.items():
            key_text = _identity(key, f"{field} key")
            if key_text.lower() in _FORBIDDEN_CONTENT_KEYS or any(
                marker in key_text.lower() for marker in ("raw_", "_token", "_secret", "password")
            ):
                raise ValidationFailureError(f"{field} contains a protected or unrestricted field")
            result[key_text] = _bounded(item, f"{field}.{key_text}", depth=depth + 1)
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, (tuple, list, frozenset)):
        if len(value) > _MAX_LIST:
            raise ValidationFailureError(f"{field} contains too many items")
        return [_bounded(item, field, depth=depth + 1) for item in value]
    raise ValidationFailureError(f"{field} contains an unsupported value")


def _json_object(value: object, field: str) -> dict[str, Any]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise StorageFailureError(f"stored {field} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise StorageFailureError(f"stored {field} is not an object")
    return parsed


def _workflow_cycle(state: Mapping[str, Any], cycle_id: str | None) -> Mapping[str, Any]:
    loop = state.get("decision_loop")
    if not isinstance(loop, Mapping):
        raise ValidationFailureError("Episode has no initialized decision loop")
    cycles = loop.get("cycles")
    if not isinstance(cycles, Sequence) or isinstance(cycles, (str, bytes)):
        raise StorageFailureError("Episode decision loop cycles are invalid")
    selected = cycle_id or loop.get("active_cycle_id")
    if not isinstance(selected, str) or not selected:
        open_cycles = [item for item in cycles if isinstance(item, Mapping) and item.get("status") != "CLOSED"]
        if len(open_cycles) != 1:
            raise ValidationFailureError("active Episode cycle is not unambiguous")
        selected = open_cycles[0].get("cycle_id")
    for cycle in cycles:
        if isinstance(cycle, Mapping) and cycle.get("cycle_id") == selected:
            return cycle
    raise ValidationFailureError("cycle_id is not present in the Episode")


def _revision_dict(revisions: RevisionVector) -> dict[str, object]:
    return revisions.as_dict()


def _safe_cycle_context(state: Mapping[str, Any], cycle: Mapping[str, Any]) -> dict[str, object]:
    checks = []
    for check_id, check in sorted((cycle.get("checks") or {}).items()):
        if not isinstance(check, Mapping):
            continue
        result = check.get("result")
        checks.append(
            {
                "check_id": check_id,
                "status": check.get("status"),
                "result_identity": result.get("result_identity") if isinstance(result, Mapping) else None,
                "outcome": result.get("outcome") if isinstance(result, Mapping) else None,
            }
        )
    actions = []
    for action_id, action in sorted((cycle.get("actions") or {}).items()):
        if not isinstance(action, Mapping):
            continue
        actions.append(
            {
                "action_id": action_id,
                "action_type": action.get("action_type"),
                "reconciliation_state": action.get("reconciliation_state"),
                "external_reference_identity": action.get("external_reference"),
            }
        )
    recovery = []
    for plan_id, plan in sorted((cycle.get("recovery_plans") or {}).items()):
        if not isinstance(plan, Mapping):
            continue
        evidence_ids: list[object] = []
        for assessment in plan.get("assessments", ()):
            if isinstance(assessment, Mapping):
                refs = assessment.get("evidence_refs", ())
                if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
                    evidence_ids.extend(refs)
        recovery.append(
            {
                "recovery_plan_id": plan_id,
                "state": plan.get("state"),
                "policy_hash": plan.get("policy_hash"),
                "evidence_ids": sorted(set(str(item) for item in evidence_ids)),
            }
        )
    closures = []
    for closure in cycle.get("closures", ()):
        if isinstance(closure, Mapping):
            closures.append(
                {
                    "closure_identity": closure.get("closure_identity"),
                    "disposition": closure.get("disposition"),
                    "evidence_ids": closure.get("evidence_ids", []),
                }
            )
    return {
        "work_state": state.get("work_state"),
        "owner": state.get("owner"),
        "cycle_status": cycle.get("status"),
        "checks": checks[:_MAX_LIST],
        "actions": actions[:_MAX_LIST],
        "recovery_plans": recovery[:_MAX_LIST],
        "closures": closures[-_MAX_LIST:],
        "reopen_identity": (state.get("decision_loop") or {}).get("last_reopen_identity"),
    }


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    snapshot_id: str
    content_hash: str
    scope_key: str
    episode_id: str
    cycle_id: str
    workflow_version: int
    viewed_revisions: dict[str, object]
    content: dict[str, object]
    created_by: str
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "content_hash": self.content_hash,
            "scope_key": self.scope_key,
            "episode_id": self.episode_id,
            "cycle_id": self.cycle_id,
            "workflow_version": self.workflow_version,
            "viewed_revisions": self.viewed_revisions,
            "content": self.content,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RecipientResolution:
    selector: str
    recipient: str
    channel: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    state: str
    external_reference: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.state not in {DELIVERED, FAILED, UNKNOWN}:
            raise ValidationFailureError("delivery result state is invalid")


@runtime_checkable
class RecipientResolver(Protocol):
    def resolve(self, scope: AccessScope, selector: str) -> RecipientResolution: ...


@runtime_checkable
class DeliveryChannelAdapter(Protocol):
    def send(self, resolution: RecipientResolution, payload: Mapping[str, object], idempotency_key: str) -> DeliveryResult: ...

    def reconcile(self, resolution: RecipientResolution, idempotency_key: str, external_reference: str | None) -> DeliveryResult: ...


@runtime_checkable
class HandoffStorage(Protocol):
    def aggregate_for_snapshot(self, scope: AccessScope, episode_id: str) -> AggregateSnapshot | None: ...

    def insert_decision_snapshot(self, **kwargs: object) -> DecisionSnapshot: ...

    def get_decision_snapshot(self, scope: AccessScope, snapshot_id: str) -> DecisionSnapshot | None: ...

    def get_outbox_event(self, scope: AccessScope, event_id: str) -> dict[str, object] | None: ...

    def insert_handoff_intent(self, **kwargs: object) -> dict[str, object]: ...

    def bind_handoff_job(self, scope: AccessScope, intent_id: str, job_id: str) -> None: ...

    def get_handoff_status(self, scope: AccessScope, intent_id: str) -> dict[str, object] | None: ...

    def list_handoff_status(self, scope: AccessScope, *, limit: int = 50) -> tuple[dict[str, object], ...]: ...

    def commit_delivery_effect(self, lease: WorkerLease, effect_key: str, input_payload: Mapping[str, object], **kwargs: object) -> AppliedEffectReceipt: ...

    def reconcile_unknown(self, scope: AccessScope, intent_id: str, result: DeliveryResult) -> dict[str, object]: ...

    def get_intent_for_job(self, scope: AccessScope, job_id: str) -> dict[str, object] | None: ...

    def mark_expired_deliveries_unknown(self, scope: AccessScope) -> int: ...

    def mark_delivery_dispatching(self, scope: AccessScope, intent_id: str, lease: WorkerLease) -> None: ...


@dataclass(frozen=True, slots=True)
class _DerivedHandoff:
    event_kind: str
    material_change_signature: str


def _authoritative_outbox_facts(event: Mapping[str, object]) -> tuple[str, dict[str, object]]:
    """Validate and return the committed O2 event envelope.

    The O2 outbox payload is intentionally an envelope containing the command
    type, result identity, and aggregate identity.  O5.2 may classify only
    after those facts agree with the row columns.  In particular, a payload
    command type that disagrees with ``event_type`` is not a second opinion;
    it is a malformed event and must fail closed.
    """

    if not isinstance(event, Mapping):
        raise StorageFailureError("stored outbox event is not an object")
    payload = event.get("payload_json")
    if not isinstance(payload, Mapping):
        raise StorageFailureError("stored outbox payload is not an object")
    required = ("event_id", "command_type", "result_identity", "aggregate_type", "aggregate_id", "aggregate_version")
    if any(field not in payload for field in required):
        raise StorageFailureError("stored outbox payload has an invalid typed envelope")
    for field in ("event_id", "command_type", "result_identity", "aggregate_type", "aggregate_id"):
        _identity(payload[field], f"outbox payload {field}")
    version = payload["aggregate_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise StorageFailureError("stored outbox payload aggregate_version is invalid")
    for field in ("event_id", "event_type", "aggregate_type", "aggregate_id"):
        _identity(event.get(field), f"outbox {field}")
    row_version = event.get("aggregate_version")
    if isinstance(row_version, bool) or not isinstance(row_version, int) or row_version < 0:
        raise StorageFailureError("stored outbox aggregate_version is invalid")
    if payload["event_id"] != event["event_id"]:
        raise StorageFailureError("outbox payload event identity does not match its row")
    if payload["command_type"] != event["event_type"]:
        raise ValidationFailureError("outbox command type does not match event_type")
    if payload["aggregate_type"] != event["aggregate_type"] or payload["aggregate_id"] != event["aggregate_id"]:
        raise ValidationFailureError("outbox aggregate identity does not match its row")
    if payload["aggregate_version"] != row_version:
        raise ValidationFailureError("outbox aggregate version does not match its row")
    return str(payload["command_type"]), dict(payload)


def _derive_handoff(event: Mapping[str, object]) -> _DerivedHandoff:
    """Derive notification identity solely from one committed O2 event."""

    command_type, payload = _authoritative_outbox_facts(event)
    event_kind = _EVENT_KIND_BY_COMMAND.get(command_type)
    if event_kind is None:
        raise ValidationFailureError("outbox event type is not an eligible O5 handoff trigger")

    # Include the complete validated, typed envelope and the immutable O2 row
    # identity.  This makes the signature stable for replay/concurrency while
    # keeping arbitrary caller text outside the logical intent identity.
    material_facts = {
        "scope_key": event["scope_key"],
        "subject": event["subject"],
        "command_id": event["command_id"],
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "aggregate_type": event["aggregate_type"],
        "aggregate_id": event["aggregate_id"],
        "aggregate_version": event["aggregate_version"],
        "payload": payload,
    }
    signature = sha256(canonical_json(material_facts).encode("utf-8")).hexdigest()
    return _DerivedHandoff(event_kind, signature)


class DeterministicRecipientDirectory:
    """Qualification-only recipient directory; no company identity binding."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], RecipientResolution] = {}

    def register(self, scope: AccessScope, selector: str, recipient: str, *, channel: str = "IN_APP", policy_version: str = DELIVERY_POLICY_VERSION) -> None:
        self._records[(scope.canonical_key, _identity(selector, "recipient_selector"))] = RecipientResolution(
            _identity(selector, "recipient_selector"), _identity(recipient, "recipient"), _identity(channel, "channel"), _identity(policy_version, "policy_version")
        )

    def revoke(self, scope: AccessScope, selector: str) -> None:
        self._records.pop((scope.canonical_key, _identity(selector, "recipient_selector")), None)

    def resolve(self, scope: AccessScope, selector: str) -> RecipientResolution:
        resolution = self._records.get((scope.canonical_key, _identity(selector, "recipient_selector")))
        if resolution is None:
            raise AuthorizationDeniedError("recipient is not currently authorized in this scope")
        return resolution


class DeterministicInAppChannel:
    """Qualification-only channel with explicit idempotency and reconciliation."""

    def __init__(self) -> None:
        self.sent: dict[str, dict[str, object]] = {}
        self.next_result: DeliveryResult | None = None

    def send(self, resolution: RecipientResolution, payload: Mapping[str, object], idempotency_key: str) -> DeliveryResult:
        if idempotency_key in self.sent:
            return DeliveryResult(DELIVERED, str(self.sent[idempotency_key]["external_reference"]))
        result = self.next_result or DeliveryResult(DELIVERED, f"inapp:{sha256(idempotency_key.encode()).hexdigest()[:16]}")
        self.next_result = None
        if result.state == DELIVERED:
            self.sent[idempotency_key] = {"recipient": resolution.recipient, "payload": dict(payload), "external_reference": result.external_reference}
        return result

    def reconcile(self, resolution: RecipientResolution, idempotency_key: str, external_reference: str | None) -> DeliveryResult:
        existing = self.sent.get(idempotency_key)
        if existing is not None and (external_reference is None or existing["external_reference"] == external_reference):
            return DeliveryResult(DELIVERED, str(existing["external_reference"]))
        return DeliveryResult(UNKNOWN, external_reference, "RECONCILIATION_NOT_FOUND", "external outcome is still ambiguous")


class DecisionSnapshotHandoffService:
    """Create/read snapshots, project committed events, and run deliveries."""

    def __init__(
        self,
        storage: HandoffStorage,
        current_authorization: CurrentAuthorizationAuthority,
        *,
        worker: WorkerJobPort | None = None,
        recipients: RecipientResolver | None = None,
    ) -> None:
        if hasattr(storage, "handoff_store"):
            storage = storage.handoff_store()
        if not isinstance(current_authorization, CurrentAuthorizationAuthority):
            raise TypeError("current_authorization must be a CurrentAuthorizationAuthority")
        if not isinstance(storage, HandoffStorage):
            raise TypeError("storage must implement HandoffStorage")
        if worker is not None and not isinstance(worker, WorkerJobPort):
            raise TypeError("worker must implement WorkerJobPort")
        if recipients is not None and not isinstance(recipients, RecipientResolver):
            raise TypeError("recipients must implement RecipientResolver")
        self.storage = storage
        self.current_authorization = current_authorization
        self.worker = worker
        self.recipients = recipients

    def _authorize(self, principal: Principal, scope: AccessScope, capability: str) -> None:
        self.current_authorization.authorize(principal, scope, capability)

    def create_decision_snapshot(
        self,
        context: CommandContext,
        episode_id: str,
        *,
        cycle_id: str | None = None,
        analysis_identity: str | None = None,
        exposure_identity: str | None = None,
        priority_identity: str | None = None,
        plan_identity: str | None = None,
        source_knowledge_cutoff: str | None = None,
        source_capability_facts: Mapping[str, object] | None = None,
        what_changed: str,
        why_it_matters: str,
        key_limitation: str,
        next_authorized_action: str,
        qualification_identity: str | None = None,
    ) -> DecisionSnapshot:
        if not isinstance(context, CommandContext):
            raise ValidationFailureError("context must be a CommandContext")
        episode_id = _identity(episode_id, "episode_id")
        self._authorize(context.principal, context.scope, DECISION_SNAPSHOT_CREATE_CAPABILITY)
        expected = context.expected_workflow_version
        revisions = context.viewed_revisions
        if expected is None or revisions is None:
            raise ValidationFailureError("snapshot creation requires expected workflow version and viewed revisions")
        if revisions.workflow_version != expected:
            raise VersionConflictError(episode_id, expected, revisions.workflow_version)

        # The adapter validates this version again while holding its writer
        # transaction, so a workflow mutation cannot slip between read and
        # immutable snapshot commit.
        aggregate = self.storage.aggregate_for_snapshot(context.scope, episode_id)
        if aggregate is None:
            raise ValidationFailureError("Episode workflow aggregate is not available")
        if aggregate.version != expected:
            raise VersionConflictError(episode_id, expected, aggregate.version)
        cycle = _workflow_cycle(aggregate.state, cycle_id)
        selected_cycle_id = _identity(str(cycle.get("cycle_id")), "cycle_id")
        content = {
            "canonical_scope": context.scope.as_dict(),
            "episode_id": episode_id,
            "cycle_id": selected_cycle_id,
            "workflow_version": expected,
            "viewed_revisions": _revision_dict(revisions),
            "analysis_identity": analysis_identity,
            "exposure_identity": exposure_identity,
            "priority_identity": priority_identity,
            "plan_identity": plan_identity or revisions.plan_version,
            "qualification_identity": qualification_identity or revisions.qualification_manifest_id,
            "source": {
                "knowledge_cutoff": source_knowledge_cutoff,
                "capability_facts": dict(source_capability_facts or {}),
            },
            "workflow": _safe_cycle_context(aggregate.state, cycle),
            "what_changed": what_changed,
            "why_it_matters": why_it_matters,
            "key_limitation": key_limitation,
            "next_authorized_action": next_authorized_action,
        }
        safe_content = _bounded(content)
        if not isinstance(safe_content, dict):  # pragma: no cover - bounded mapping invariant
            raise ValidationFailureError("snapshot content must be an object")
        content_hash = sha256(canonical_json(safe_content).encode("utf-8")).hexdigest()
        snapshot_id = sha256(
            canonical_json(
                {
                    "scope": context.scope.canonical_key,
                    "episode_id": episode_id,
                    "cycle_id": selected_cycle_id,
                    "workflow_version": expected,
                    "content_hash": content_hash,
                }
            ).encode("utf-8")
        ).hexdigest()
        if len(canonical_json(safe_content).encode("utf-8")) > _MAX_JSON_BYTES:
            raise ValidationFailureError("decision snapshot exceeds the bounded size")
        return self.storage.insert_decision_snapshot(
            scope=context.scope,
            snapshot_id=snapshot_id,
            content_hash=content_hash,
            episode_id=episode_id,
            cycle_id=selected_cycle_id,
            workflow_version=expected,
            viewed_revisions=revisions.as_dict(),
            content=safe_content,
            created_by=context.principal.subject,
        )

    def read_decision_snapshot(self, principal: Principal, scope: AccessScope, snapshot_id: str) -> DecisionSnapshot:
        self._authorize(principal, scope, DECISION_SNAPSHOT_READ_CAPABILITY)
        snapshot = self.storage.get_decision_snapshot(scope, _identity(snapshot_id, "snapshot_id"))
        if snapshot is None:
            raise ValidationFailureError("decision snapshot is not available in the requested scope")
        return snapshot

    def create_handoff(
        self,
        context: CommandContext,
        *,
        event_id: str,
        snapshot_id: str,
        event_kind: str | None = None,
        material_change_signature: str | None = None,
        recipient_selector: str,
        channel: str | None = None,
        policy_version: str = DELIVERY_POLICY_VERSION,
    ) -> dict[str, object]:
        if self.worker is None or self.recipients is None:
            raise ValidationFailureError("handoff creation requires a durable worker and recipient resolver")
        self._authorize(context.principal, context.scope, HANDOFF_CREATE_CAPABILITY)
        if context.expected_workflow_version is None or context.viewed_revisions is None:
            raise ValidationFailureError("handoff creation requires expected workflow version and viewed revisions")
        event_id = _identity(event_id, "event_id")
        snapshot = self.read_decision_snapshot(context.principal, context.scope, snapshot_id)
        if snapshot.workflow_version != context.expected_workflow_version or snapshot.viewed_revisions != context.viewed_revisions.as_dict():
            raise VersionConflictError(snapshot.episode_id, context.expected_workflow_version, snapshot.workflow_version)
        event = self.storage.get_outbox_event(context.scope, event_id)
        if event is None:
            raise ValidationFailureError("handoff must be derived from a committed outbox event")
        if event.get("aggregate_id") != snapshot.episode_id or event.get("aggregate_version") != snapshot.workflow_version:
            raise ValidationFailureError("outbox event does not bind the requested decision snapshot")
        if event.get("aggregate_type") != "episode_workflow":
            raise ValidationFailureError("handoff event is not an Episode workflow event")
        derived = _derive_handoff(event)
        if event_kind is not None and _identity(event_kind, "event_kind") != derived.event_kind:
            raise ValidationFailureError("caller event_kind does not match the committed workflow event")
        if material_change_signature is not None and _identity(material_change_signature, "material_change_signature") != derived.material_change_signature:
            raise ValidationFailureError("caller material_change_signature does not match the committed workflow event")
        event_kind = derived.event_kind
        material_change_signature = derived.material_change_signature
        resolution = self.recipients.resolve(context.scope, _identity(recipient_selector, "recipient_selector"))
        selected_channel = _identity(channel or resolution.channel, "channel")
        selected_policy = _identity(policy_version or resolution.policy_version, "policy_version")
        if selected_channel != resolution.channel or selected_policy != resolution.policy_version:
            raise AuthorizationDeniedError("recipient channel policy is not currently authorized")
        signature = material_change_signature
        dedup_key = sha256(
            canonical_json(
                {
                    "episode_id": snapshot.episode_id,
                    "cycle_id": snapshot.cycle_id,
                    "event_kind": event_kind,
                    "material_change_signature": signature,
                    "recipient": resolution.recipient,
                    "channel": selected_channel,
                    "policy_version": selected_policy,
                }
            ).encode("utf-8")
        ).hexdigest()
        safe_content = snapshot.content
        safe_payload = {
            "episode_id": snapshot.episode_id,
            "cycle_id": snapshot.cycle_id,
            "snapshot_id": snapshot.snapshot_id,
            "what_changed": safe_content["what_changed"],
            "why_it_matters": safe_content["why_it_matters"],
            "source_knowledge_cutoff": (safe_content.get("source") or {}).get("knowledge_cutoff"),
            "key_limitation": safe_content["key_limitation"],
            "next_authorized_action": safe_content["next_authorized_action"],
            "deep_link": {
                "route": "episode",
                "episode_id": snapshot.episode_id,
                "cycle_id": snapshot.cycle_id,
                "snapshot_id": snapshot.snapshot_id,
            },
        }
        safe_payload = _bounded(safe_payload, "notification payload")
        intent = self.storage.insert_handoff_intent(
            scope=context.scope,
            triggering_event_id=event_id,
            episode_id=snapshot.episode_id,
            cycle_id=snapshot.cycle_id,
            decision_snapshot_id=snapshot.snapshot_id,
            event_kind=event_kind,
            material_change_signature=signature,
            recipient_selector=resolution.selector,
            resolved_recipient=resolution.recipient,
            channel=selected_channel,
            policy_version=selected_policy,
            dedup_key=dedup_key,
            deep_link=safe_payload["deep_link"],
            safe_payload=safe_payload,
        )
        job = self.worker.enqueue(
            context.scope,
            HANDOFF_DELIVERY_JOB_TYPE,
            f"handoff:{dedup_key}",
            {
                "intent_id": intent["intent_id"],
                "dedup_key": dedup_key,
                "recipient_selector": resolution.selector,
                "channel": selected_channel,
                "policy_version": selected_policy,
                "idempotency_key": f"ephi-o5.2:{dedup_key}",
            },
        )
        self.storage.bind_handoff_job(context.scope, str(intent["intent_id"]), job.job_id)
        return {**intent, "job_id": job.job_id}

    def project_outbox_event(self, context: CommandContext, **kwargs: object) -> dict[str, object]:
        """Idempotently project one already committed O2 outbox event."""

        return self.create_handoff(context, **kwargs)  # type: ignore[arg-type]

    def read_handoff_status(self, principal: Principal, scope: AccessScope, intent_id: str) -> dict[str, object]:
        self._authorize(principal, scope, HANDOFF_READ_CAPABILITY)
        status = self.storage.get_handoff_status(scope, _identity(intent_id, "intent_id"))
        if status is None:
            raise ValidationFailureError("handoff status is not available in the requested scope")
        return status

    def list_handoff_status(self, principal: Principal, scope: AccessScope, *, limit: int = 50) -> tuple[dict[str, object], ...]:
        self._authorize(principal, scope, HANDOFF_READ_CAPABILITY)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValidationFailureError("limit must be between 1 and 100")
        return self.storage.list_handoff_status(scope, limit=limit)

    def resolve_deep_link(self, principal: Principal, scope: AccessScope, descriptor: Mapping[str, object]) -> DecisionSnapshot:
        self._authorize(principal, scope, DECISION_SNAPSHOT_READ_CAPABILITY)
        if not isinstance(descriptor, Mapping) or descriptor.get("route") != "episode":
            raise ValidationFailureError("deep link descriptor is invalid")
        return self.read_decision_snapshot(principal, scope, _identity(descriptor.get("snapshot_id"), "snapshot_id"))

    def dispatch_once(
        self,
        principal: Principal,
        scope: AccessScope,
        *,
        worker_id: str,
        channel_adapter: DeliveryChannelAdapter,
    ) -> JobRecord | None:
        if self.worker is None or self.recipients is None:
            raise ValidationFailureError("delivery dispatch requires a durable worker and recipient resolver")
        self._authorize(principal, scope, DELIVERY_DISPATCH_CAPABILITY)
        # A worker crash after an external call but before the local effect
        # receipt is ambiguous.  Mark the expired delivery UNKNOWN before a
        # new lease can be claimed; UNKNOWN is never blindly resent.
        self.storage.mark_expired_deliveries_unknown(scope)
        job = self.worker.claim(scope, _identity(worker_id, "worker_id"), job_type=HANDOFF_DELIVERY_JOB_TYPE)
        if job is None:
            return None
        intent = self.storage.get_intent_for_job(scope, job.job_id)
        if intent is None:
            self.worker.fail(job.lease, retryable=False, error_code="INTENT_NOT_FOUND", error_message="delivery intent is missing")
            return job
        if intent.get("delivery_state") == UNKNOWN:
            self.worker.cancel(scope, job.job_id, lease=job.lease)
            return job
        self.storage.mark_delivery_dispatching(scope, str(intent["intent_id"]), job.lease)
        selector = _identity(intent["recipient_selector"], "recipient_selector")
        try:
            current = self.recipients.resolve(scope, selector)
            if current.recipient != intent["resolved_recipient"]:
                raise AuthorizationDeniedError("recipient authorization changed after intent creation")
            result = channel_adapter.send(current, intent["safe_payload"], str(intent["idempotency_key"]))
        except AuthorizationDeniedError:
            result = DeliveryResult(FAILED, error_code="RECIPIENT_REVOKED", error_message="recipient is no longer authorized")
        except Exception:
            result = DeliveryResult(UNKNOWN, error_code="DELIVERY_OUTCOME_UNKNOWN", error_message="channel outcome is ambiguous")
        effect_key = f"delivery:{intent['intent_id']}:{job.attempts}"
        self.storage.commit_delivery_effect(
            job.lease,
            effect_key,
            {"intent_id": intent["intent_id"], "state": result.state, "external_reference": result.external_reference},
            intent_id=intent["intent_id"],
            state=result.state,
            external_reference=result.external_reference,
            error_code=result.error_code,
            error_message=result.error_message,
            retryable=result.retryable,
        )
        return job

    def reconcile_unknown(
        self,
        principal: Principal,
        scope: AccessScope,
        intent_id: str,
        channel_adapter: DeliveryChannelAdapter,
    ) -> dict[str, object]:
        self._authorize(principal, scope, DELIVERY_RECONCILE_CAPABILITY)
        status = self.read_handoff_status(principal, scope, intent_id)
        if status.get("delivery_state") != UNKNOWN:
            return status
        if self.recipients is None:
            raise ValidationFailureError("recipient resolver is not configured")
        current = self.recipients.resolve(scope, _identity(status["recipient_selector"], "recipient_selector"))
        if current.recipient != status["resolved_recipient"]:
            raise AuthorizationDeniedError("recipient authorization changed before reconciliation")
        result = channel_adapter.reconcile(current, str(status["idempotency_key"]), status.get("external_reference"))
        return self.storage.reconcile_unknown(scope, intent_id, result)


__all__ = [
    "ACTION_FAILED",
    "ACTION_OUTCOME_UNKNOWN",
    "ACTION_RECORDED",
    "ASSIGNMENT_HANDOFF",
    "CHECK_RESULT_REQUIRES_ACTION",
    "CANCELED",
    "CLOSURE",
    "DELIVERED",
    "DELIVERY_DISPATCH_CAPABILITY",
    "DELIVERY_POLICY_VERSION",
    "DELIVERY_RECONCILE_CAPABILITY",
    "DELIVERY_STATES",
    "DISPATCHING",
    "EVENT_KINDS",
    "FAILED",
    "HANDOFF_CREATE_CAPABILITY",
    "HANDOFF_DELIVERY_JOB_TYPE",
    "HANDOFF_READ_CAPABILITY",
    "PENDING",
    "RECOVERY_FAILED",
    "RECOVERY_PASSED_REVIEW_REQUIRED",
    "REOPEN",
    "SUPERSEDED",
    "UNKNOWN",
    "DECISION_SNAPSHOT_CREATE_CAPABILITY",
    "DECISION_SNAPSHOT_READ_CAPABILITY",
    "DecisionSnapshot",
    "DecisionSnapshotHandoffService",
    "DeliveryChannelAdapter",
    "DeliveryResult",
    "DeterministicInAppChannel",
    "DeterministicRecipientDirectory",
    "HandoffStorage",
    "RecipientResolution",
    "RecipientResolver",
]
