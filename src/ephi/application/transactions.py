"""One bounded, reusable durable command transaction."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any

from ephi.infrastructure.sqlite import SQLiteReferenceTransactionAdapter

from .context import CommandContext
from .errors import (
    AggregateNotFoundError,
    AuthorizationDeniedError,
    CommandError,
    IdempotencyConflictError,
    ScopeDeniedError,
    StorageFailureError,
    ValidationFailureError,
    VersionConflictError,
)
from .hashing import canonical_command_payload_hash, canonical_json, normalize_domain_payload


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

    def __init__(self, store: SQLiteReferenceTransactionAdapter):
        if not isinstance(store, SQLiteReferenceTransactionAdapter):
            raise TypeError("store must be a SQLiteReferenceTransactionAdapter")
        self.store = store

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
        if context.expected_workflow_version is None:
            raise ValidationFailureError("existing-aggregate commands require expected_workflow_version")
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

        with self.store.transaction_lock:
            connection = self.store.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                receipt_row = connection.execute(
                    "SELECT scope_key, subject, command_id, payload_hash, status, result_identity, result_json, aggregate_type, aggregate_id, aggregate_version, auth_session_revision_json, security_revision_json, committed_at FROM command_receipt WHERE scope_key = ? AND subject = ? AND command_id = ?",
                    (context.scope.canonical_key, context.principal.subject, context.command_id),
                ).fetchone()
                receipt = self.store._receipt_from_row(receipt_row) if receipt_row is not None else None
                if receipt is not None:
                    result = self._replay_or_conflict(context, required_capability, receipt, payload_hash)
                    connection.commit()
                    return result

                aggregate_row = connection.execute(
                    "SELECT scope_key, aggregate_type, aggregate_id, version, state_json FROM aggregate_state WHERE scope_key = ? AND aggregate_type = ? AND aggregate_id = ?",
                    (context.scope.canonical_key, aggregate_type, aggregate_id),
                ).fetchone()
                if aggregate_row is None:
                    raise AggregateNotFoundError("aggregate is not available in the requested scope")
                current_version = aggregate_row["version"]
                if current_version != context.expected_workflow_version:
                    raise VersionConflictError(aggregate_id, context.expected_workflow_version, current_version)
                try:
                    current_state = json.loads(aggregate_row["state_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise StorageFailureError("durable aggregate state is not valid JSON") from exc
                if not isinstance(current_state, dict):
                    raise StorageFailureError("durable aggregate state is not an object")
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
                changed = connection.execute(
                    "UPDATE aggregate_state SET version = ?, state_json = ? WHERE scope_key = ? AND aggregate_type = ? AND aggregate_id = ? AND version = ?",
                    (next_version, state_json, context.scope.canonical_key, aggregate_type, aggregate_id, current_version),
                ).rowcount
                if changed != 1:
                    raise VersionConflictError(aggregate_id, context.expected_workflow_version, current_version)

                result_identity = _event_identity("result", context.scope.canonical_key, context.principal.subject, context.command_id)
                result = CommandResult("COMMITTED", result_identity, aggregate_type, aggregate_id, next_version, next_state, payload_hash)
                result_json = canonical_json(result.as_dict())
                recorded_at = _utc_timestamp()
                audit_id = _event_identity("audit", context.scope.canonical_key, context.principal.subject, context.command_id)
                outbox_id = _event_identity("outbox", context.scope.canonical_key, context.principal.subject, context.command_id)
                audit_json = canonical_json({
                    "command_type": command_type,
                    "payload_hash": payload_hash,
                    "expected_workflow_version": context.expected_workflow_version,
                    "new_workflow_version": next_version,
                    "result_identity": result_identity,
                })
                outbox_json = canonical_json({
                    "event_id": outbox_id,
                    "command_type": command_type,
                    "result_identity": result_identity,
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "aggregate_version": next_version,
                })
                connection.execute(
                    "INSERT INTO audit_event(event_id, scope_key, subject, command_id, aggregate_type, aggregate_id, aggregate_version, event_type, event_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (audit_id, context.scope.canonical_key, context.principal.subject, context.command_id, aggregate_type, aggregate_id, next_version, command_type, audit_json, recorded_at),
                )
                connection.execute(
                    "INSERT INTO outbox_event(event_id, scope_key, subject, command_id, aggregate_type, aggregate_id, aggregate_version, event_type, payload_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (outbox_id, context.scope.canonical_key, context.principal.subject, context.command_id, aggregate_type, aggregate_id, next_version, command_type, outbox_json, "PENDING", recorded_at),
                )
                try:
                    connection.execute(
                        "INSERT INTO command_receipt(scope_key, subject, command_id, payload_hash, status, result_identity, result_json, aggregate_type, aggregate_id, aggregate_version, auth_session_revision_json, security_revision_json, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            context.scope.canonical_key,
                            context.principal.subject,
                            context.command_id,
                            payload_hash,
                            result.status,
                            result_identity,
                            result_json,
                            aggregate_type,
                            aggregate_id,
                            next_version,
                            canonical_json(context.principal.auth_session_revision),
                            canonical_json(context.principal.security_revision),
                            recorded_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    # A backend that permits two first attempts to race may
                    # report the unique receipt winner here.  Roll back the
                    # entire losing effect, then reconcile in a fresh read.
                    connection.rollback()
                    return self._reconcile_committed_receipt(context, required_capability, payload_hash, exc)
                connection.commit()
                return result
            except CommandError:
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise StorageFailureError("durable command transaction failed") from exc

    @staticmethod
    def _authorize(context: CommandContext, required_capability: str) -> None:
        principal = context.principal
        if not principal.grants_scope(context.scope):
            raise ScopeDeniedError("principal is not granted the requested product scope")
        if not principal.has_capability(required_capability):
            raise AuthorizationDeniedError("principal is not currently granted the required capability")

    def _replay_or_conflict(self, context: CommandContext, required_capability: str, receipt: Any, payload_hash: str) -> CommandResult:
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
        cause: sqlite3.IntegrityError,
    ) -> CommandResult:
        del cause
        connection = self.store.connection
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT scope_key, subject, command_id, payload_hash, status, result_identity, result_json, aggregate_type, aggregate_id, aggregate_version, auth_session_revision_json, security_revision_json, committed_at FROM command_receipt WHERE scope_key = ? AND subject = ? AND command_id = ?",
                (context.scope.canonical_key, context.principal.subject, context.command_id),
            ).fetchone()
            if row is None:
                raise StorageFailureError("receipt race could not be reconciled; retry the same command")
            receipt = self.store._receipt_from_row(row)
            result = self._replay_or_conflict(context, required_capability, receipt, payload_hash)
            connection.commit()
            return result
        except CommandError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise StorageFailureError("durable receipt reconciliation failed") from exc


SQLiteCommandExecutor = VersionedAggregateCommandExecutor
