"""One bounded, reusable durable command transaction."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from .context import CommandContext, CurrentAuthorizationAuthority
from .errors import (
    AggregateNotFoundError,
    CommandError,
    IdempotencyConflictError,
    StorageFailureError,
    ValidationFailureError,
    VersionConflictError,
)
from .hashing import canonical_command_payload_hash, canonical_json, normalize_domain_payload
from .storage import (
    AggregateAlreadyExistsError,
    CommandEventAlreadyExistsError,
    CommandStorage,
    ReceiptAlreadyExistsError,
    StoredCommandReceipt,
)


Effect = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class CommandResult:
    status: str
    result_identity: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    state: dict[str, Any]
    payload_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "result_identity": self.result_identity,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": self.aggregate_version,
            "state": self.state,
            "payload_hash": self.payload_hash,
        }

    @classmethod
    def from_json(cls, result_json: str) -> "CommandResult":
        try:
            value = json.loads(result_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageFailureError("stored command result is not valid JSON") from exc
        if not isinstance(value, dict):
            raise StorageFailureError("stored command result is not an object")
        required = {"status", "result_identity", "aggregate_type", "aggregate_id", "aggregate_version", "state", "payload_hash"}
        if set(value) != required or not isinstance(value["state"], dict):
            raise StorageFailureError("stored command result has an invalid schema")
        if (
            not isinstance(value["status"], str)
            or not isinstance(value["result_identity"], str)
            or not isinstance(value["aggregate_type"], str)
            or not isinstance(value["aggregate_id"], str)
            or not isinstance(value["payload_hash"], str)
            or isinstance(value["aggregate_version"], bool)
            or not isinstance(value["aggregate_version"], int)
        ):
            raise StorageFailureError("stored command result has invalid field types")
        return cls(
            status=value["status"],
            result_identity=value["result_identity"],
            aggregate_type=value["aggregate_type"],
            aggregate_id=value["aggregate_id"],
            aggregate_version=value["aggregate_version"],
            state=value["state"],
            payload_hash=value["payload_hash"],
        )


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event_identity(kind: str, scope_key: str, subject: str, command_id: str) -> str:
    return hashlib.sha256(f"ephi-o2:{kind}:{scope_key}:{subject}:{command_id}".encode("utf-8")).hexdigest()


def _default_effect(current: Mapping[str, Any], payload: Mapping[str, Any], command_type: str) -> Mapping[str, Any]:
    effect_count = current.get("effect_count", 0)
    if isinstance(effect_count, bool) or not isinstance(effect_count, int) or effect_count < 0:
        raise ValidationFailureError("fixture aggregate effect_count must be a non-negative integer")
    next_state = dict(current)
    next_state["effect_count"] = effect_count + 1
    next_state["last_payload"] = dict(payload)
    next_state["last_command_type"] = command_type
    return next_state


class VersionedAggregateCommandExecutor:
    """Execute one local versioned effect with receipt/audit/outbox atomicity."""

    def __init__(self, store: CommandStorage, current_authorization: CurrentAuthorizationAuthority):
        if not isinstance(store, CommandStorage):
            raise TypeError("store must implement the durable command transaction boundary")
        if not isinstance(current_authorization, CurrentAuthorizationAuthority):
            raise TypeError("current_authorization must be a CurrentAuthorizationAuthority")
        self.store = store
        self.current_authorization = current_authorization

    def execute(
        self,
        context: CommandContext,
        *,
        command_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, object],
        required_capability: str,
        effect: Effect | None = None,
        create_if_missing: bool = False,
    ) -> CommandResult:
        if not isinstance(context, CommandContext):
            raise ValidationFailureError("context must be a CommandContext")
        command_type = _identity(command_type, "command_type")
        aggregate_type = _identity(aggregate_type, "aggregate_type")
        aggregate_id = _identity(aggregate_id, "aggregate_id")
        required_capability = _identity(required_capability, "required_capability")
        if not isinstance(payload, Mapping):
            raise ValidationFailureError("domain payload must be a mapping")
        if effect is not None and not callable(effect):
            raise ValidationFailureError("effect must be callable or None")

        # Authorization is intentionally before receipt lookup so a revoked
        # principal cannot learn or disclose a prior command result.
        self._authorize(context, required_capability)
        if context.expected_workflow_version is None and not create_if_missing:
            raise ValidationFailureError("existing-aggregate commands require expected_workflow_version")
        if create_if_missing and context.expected_workflow_version is not None:
            raise ValidationFailureError("create commands require no expected_workflow_version")
        normalized_payload = normalize_domain_payload(payload)
        if not isinstance(normalized_payload, dict):
            raise ValidationFailureError("domain payload must normalize to an object")
        target = {"aggregate_type": aggregate_type, "aggregate_id": aggregate_id}
        payload_hash = canonical_command_payload_hash(
            command_type,
            context.scope,
            context.expected_workflow_version,
            context.viewed_revisions,
            normalized_payload,
            target=target,
            reason=context.reason,
        )
        scope_key = context.scope.canonical_key
        subject = context.principal.subject
        auth_session_revision_json = canonical_json(context.principal.auth_session_revision)
        security_revision_json = canonical_json(context.principal.security_revision)

        try:
            with self.store.command_transaction() as transaction:
                # The maintained order is authorization, receipt, aggregate,
                # expected version, local effect, audit, outbox, receipt, commit.
                receipt = transaction.get_command_receipt(scope_key, subject, context.command_id)
                if receipt is not None:
                    return self._replay_or_conflict(context, required_capability, receipt, payload_hash)

                aggregate = transaction.get_aggregate(
                    scope_key,
                    aggregate_type,
                    aggregate_id,
                    for_update=not create_if_missing,
                )
                if aggregate is None:
                    if not create_if_missing:
                        raise AggregateNotFoundError("aggregate is not available in the requested scope")
                    current_version = 0
                    current_state: Mapping[str, Any] = {}
                else:
                    if create_if_missing:
                        raise AggregateAlreadyExistsError
                    current_version = aggregate.version
                    current_state = aggregate.state
                if not create_if_missing and current_version != context.expected_workflow_version:
                    # A competing first attempt may have won the aggregate lock
                    # after the initial receipt read. Reconcile its committed
                    # receipt before exposing VERSION_CONFLICT.
                    receipt = transaction.get_command_receipt(scope_key, subject, context.command_id)
                    if receipt is not None:
                        return self._replay_or_conflict(context, required_capability, receipt, payload_hash)
                    raise VersionConflictError(aggregate_id, context.expected_workflow_version, current_version)
                try:
                    next_state_raw = (
                        effect(current_state, normalized_payload)
                        if effect is not None
                        else _default_effect(current_state, normalized_payload, command_type)
                    )
                except CommandError:
                    raise
                except Exception as exc:
                    raise ValidationFailureError("local aggregate effect rejected the domain payload") from exc
                next_state = normalize_domain_payload(next_state_raw)
                if not isinstance(next_state, dict):
                    raise ValidationFailureError("local aggregate effect must return a mapping")
                next_version = current_version + 1
                state_json = canonical_json(next_state)
                if create_if_missing:
                    transaction.insert_aggregate(
                        scope_key,
                        aggregate_type,
                        aggregate_id,
                        version=next_version,
                        state_json=state_json,
                    )
                else:
                    changed = transaction.update_aggregate(
                        scope_key,
                        aggregate_type,
                        aggregate_id,
                        expected_version=current_version,
                        next_version=next_version,
                        state_json=state_json,
                    )
                    if changed != 1:
                        raise VersionConflictError(aggregate_id, context.expected_workflow_version, current_version)

                result_identity = _event_identity("result", scope_key, subject, context.command_id)
                result = CommandResult("COMMITTED", result_identity, aggregate_type, aggregate_id, next_version, next_state, payload_hash)
                result_json = canonical_json(result.as_dict())
                recorded_at = _utc_timestamp()
                audit_id = _event_identity("audit", scope_key, subject, context.command_id)
                outbox_id = _event_identity("outbox", scope_key, subject, context.command_id)
                audit_json = canonical_json({
                    "command_type": command_type,
                    "payload_hash": payload_hash,
                    "expected_workflow_version": context.expected_workflow_version,
                    "new_workflow_version": next_version,
                    "result_identity": result_identity,
                    "auth_session_revision": context.principal.auth_session_revision,
                    "security_revision": context.principal.security_revision,
                })
                outbox_json = canonical_json({
                    "event_id": outbox_id,
                    "command_type": command_type,
                    "result_identity": result_identity,
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "aggregate_version": next_version,
                })
                transaction.append_audit(
                    event_id=audit_id,
                    scope_key=scope_key,
                    subject=subject,
                    command_id=context.command_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    aggregate_version=next_version,
                    event_type=command_type,
                    event_json=audit_json,
                    auth_session_revision_json=auth_session_revision_json,
                    security_revision_json=security_revision_json,
                    recorded_at=recorded_at,
                )
                transaction.append_outbox(
                    event_id=outbox_id,
                    scope_key=scope_key,
                    subject=subject,
                    command_id=context.command_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    aggregate_version=next_version,
                    event_type=command_type,
                    payload_json=outbox_json,
                    status="PENDING",
                    created_at=recorded_at,
                )
                transaction.insert_receipt(
                    scope_key=scope_key,
                    subject=subject,
                    command_id=context.command_id,
                    payload_hash=payload_hash,
                    status=result.status,
                    result_identity=result_identity,
                    result_json=result_json,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    aggregate_version=next_version,
                    auth_session_revision_json=auth_session_revision_json,
                    security_revision_json=security_revision_json,
                    committed_at=recorded_at,
                )
                return result
        except (ReceiptAlreadyExistsError, CommandEventAlreadyExistsError, AggregateAlreadyExistsError):
            # The command winner committed independently. The losing
            # transaction has already rolled back all of its local writes.
            return self._reconcile_committed_receipt(context, required_capability, payload_hash)

    def _authorize(self, context: CommandContext, required_capability: str) -> None:
        self.current_authorization.authorize(context.principal, context.scope, required_capability)

    def _replay_or_conflict(self, context: CommandContext, required_capability: str, receipt: StoredCommandReceipt, payload_hash: str) -> CommandResult:
        if receipt.payload_hash != payload_hash:
            raise IdempotencyConflictError(context.command_id)
        self._authorize(context, required_capability)
        result = CommandResult.from_json(receipt.result_json)
        if result.result_identity != receipt.result_identity or result.payload_hash != receipt.payload_hash:
            raise StorageFailureError("stored receipt and result identity do not agree")
        return result

    def _reconcile_committed_receipt(
        self,
        context: CommandContext,
        required_capability: str,
        payload_hash: str,
    ) -> CommandResult:
        # A race/reconciliation lookup is itself a protected receipt access;
        # re-establish the same freshness boundary before opening it.
        self._authorize(context, required_capability)
        scope_key = context.scope.canonical_key
        subject = context.principal.subject
        try:
            with self.store.command_transaction() as transaction:
                receipt = transaction.get_command_receipt(scope_key, subject, context.command_id)
                if receipt is None:
                    raise StorageFailureError("receipt race could not be reconciled; retry the same command")
                return self._replay_or_conflict(context, required_capability, receipt, payload_hash)
        except ReceiptAlreadyExistsError as exc:  # pragma: no cover - reconciliation has no insert path
            raise StorageFailureError("durable receipt reconciliation failed") from exc


SQLiteCommandExecutor = VersionedAggregateCommandExecutor
