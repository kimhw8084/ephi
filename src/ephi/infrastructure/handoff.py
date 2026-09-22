"""Reference SQLite/PostgreSQL persistence for the O5.2 contracts."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Iterator

from ephi.application.context import AccessScope
from ephi.application.errors import (
    EffectIdempotencyConflictError,
    StorageFailureError,
    StaleLeaseError,
    ValidationFailureError,
    VersionConflictError,
)
from ephi.application.hashing import canonical_json
from ephi.application.handoff import (
    DecisionSnapshot,
    DeliveryResult,
    DELIVERED,
    FAILED,
    PENDING,
    UNKNOWN,
)
from ephi.application.storage import AggregateSnapshot
from ephi.application.worker import AppliedEffectReceipt, WorkerLease


def _validated(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


def _json(value: object, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise StorageFailureError(f"stored {field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise StorageFailureError(f"stored {field} is not an object")
    return value


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return str(value)


class ReferenceHandoffStore:
    """One narrow durable adapter over an existing reference transaction store."""

    def __init__(self, adapter: Any):
        self.adapter = adapter

    @property
    def connection(self) -> Any:
        return self.adapter.connection

    @property
    def _is_sqlite(self) -> bool:
        return isinstance(self.connection, sqlite3.Connection)

    def _sql(self, statement: str) -> str:
        return statement.replace("?", "%s") if not self._is_sqlite else statement

    def _execute(self, statement: str, params: tuple[Any, ...] = ()) -> Any:
        return self.connection.execute(self._sql(statement), params)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        lock = getattr(self.adapter, "transaction_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            try:
                self.connection.execute("BEGIN IMMEDIATE" if self._is_sqlite else "BEGIN")
            except Exception as exc:
                raise StorageFailureError("O5.2 durable transaction could not begin") from exc
            try:
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        finally:
            if lock is not None:
                lock.release()

    def aggregate_for_snapshot(self, scope: AccessScope, episode_id: str) -> AggregateSnapshot | None:
        episode_id = _validated(episode_id, "episode_id")
        row = self._execute(
            "SELECT scope_key, aggregate_type, aggregate_id, version, state_json FROM aggregate_state WHERE scope_key = ? AND aggregate_type = 'episode_workflow' AND aggregate_id = ?",
            (scope.canonical_key, episode_id),
        ).fetchone()
        if row is None:
            return None
        return AggregateSnapshot(
            row["scope_key"], row["aggregate_type"], row["aggregate_id"], int(row["version"]), _json(row["state_json"], "aggregate state")
        )

    @staticmethod
    def _snapshot_from_row(row: Mapping[str, Any]) -> DecisionSnapshot:
        return DecisionSnapshot(
            snapshot_id=row["snapshot_id"],
            content_hash=row["content_hash"],
            scope_key=row["scope_key"],
            episode_id=row["episode_id"],
            cycle_id=row["cycle_id"],
            workflow_version=int(row["workflow_version"]),
            viewed_revisions=_json(row["viewed_revisions_json"], "snapshot revisions"),
            content=_json(row["content_json"], "snapshot content"),
            created_by=row["created_by"],
            created_at=_timestamp(row["created_at"]),
        )

    def insert_decision_snapshot(self, **kwargs: object) -> DecisionSnapshot:
        scope = kwargs["scope"]
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        snapshot_id = _validated(kwargs["snapshot_id"], "snapshot_id")
        episode_id = _validated(kwargs["episode_id"], "episode_id")
        cycle_id = _validated(kwargs["cycle_id"], "cycle_id")
        created_by = _validated(kwargs["created_by"], "created_by")
        workflow_version = kwargs["workflow_version"]
        if isinstance(workflow_version, bool) or not isinstance(workflow_version, int) or workflow_version < 0:
            raise ValidationFailureError("workflow_version must be non-negative")
        revisions = kwargs["viewed_revisions"]
        content = kwargs["content"]
        if not isinstance(revisions, Mapping) or not isinstance(content, Mapping):
            raise ValidationFailureError("snapshot JSON values must be mappings")
        with self._transaction():
            lock = "" if self._is_sqlite else " FOR UPDATE"
            aggregate = self._execute(
                "SELECT version FROM aggregate_state WHERE scope_key = ? AND aggregate_type = 'episode_workflow' AND aggregate_id = ?" + lock,
                (scope.canonical_key, episode_id),
            ).fetchone()
            if aggregate is None:
                raise ValidationFailureError("Episode workflow aggregate is not available")
            current_version = int(aggregate["version"])
            if current_version != workflow_version:
                raise VersionConflictError(episode_id, workflow_version, current_version)
            existing = self._execute(
                "SELECT snapshot_id, scope_key, content_hash, episode_id, cycle_id, workflow_version, viewed_revisions_json, content_json, created_by, created_at FROM decision_snapshot WHERE scope_key = ? AND snapshot_id = ?",
                (scope.canonical_key, snapshot_id),
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] != kwargs["content_hash"] or _json(existing["content_json"], "snapshot content") != dict(content):
                    raise StorageFailureError("immutable decision snapshot identity conflicts with stored content")
                return self._snapshot_from_row(existing)
            self._execute(
                "INSERT INTO decision_snapshot(snapshot_id, scope_key, content_hash, episode_id, cycle_id, workflow_version, viewed_revisions_json, content_json, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    scope.canonical_key,
                    _validated(kwargs["content_hash"], "content_hash"),
                    episode_id,
                    cycle_id,
                    workflow_version,
                    canonical_json(dict(revisions)),
                    canonical_json(dict(content)),
                    created_by,
                ),
            )
            stored = self._execute(
                "SELECT snapshot_id, scope_key, content_hash, episode_id, cycle_id, workflow_version, viewed_revisions_json, content_json, created_by, created_at FROM decision_snapshot WHERE scope_key = ? AND snapshot_id = ?",
                (scope.canonical_key, snapshot_id),
            ).fetchone()
            if stored is None:  # pragma: no cover - database failure
                raise StorageFailureError("decision snapshot insert was not durable")
            return self._snapshot_from_row(stored)

    def get_decision_snapshot(self, scope: AccessScope, snapshot_id: str) -> DecisionSnapshot | None:
        row = self._execute(
            "SELECT snapshot_id, scope_key, content_hash, episode_id, cycle_id, workflow_version, viewed_revisions_json, content_json, created_by, created_at FROM decision_snapshot WHERE scope_key = ? AND snapshot_id = ?",
            (scope.canonical_key, _validated(snapshot_id, "snapshot_id")),
        ).fetchone()
        return self._snapshot_from_row(row) if row is not None else None

    def get_outbox_event(self, scope: AccessScope, event_id: str) -> dict[str, object] | None:
        row = self._execute(
            "SELECT event_id, scope_key, subject, command_id, aggregate_type, aggregate_id, aggregate_version, event_type, payload_json, status, created_at FROM outbox_event WHERE scope_key = ? AND event_id = ?",
            (scope.canonical_key, _validated(event_id, "event_id")),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload_json"] = _json(item["payload_json"], "outbox payload")
        return item

    def insert_handoff_intent(self, **kwargs: object) -> dict[str, object]:
        scope = kwargs["scope"]
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        dedup_key = _validated(kwargs["dedup_key"], "dedup_key")
        intent_id = hashlib.sha256(f"ephi-o5.2:intent:{scope.canonical_key}:{dedup_key}".encode()).hexdigest()
        values = (
            intent_id,
            scope.canonical_key,
            _validated(kwargs["triggering_event_id"], "triggering_event_id"),
            _validated(kwargs["episode_id"], "episode_id"),
            _validated(kwargs["cycle_id"], "cycle_id"),
            _validated(kwargs["decision_snapshot_id"], "decision_snapshot_id"),
            _validated(kwargs["event_kind"], "event_kind"),
            _validated(kwargs["material_change_signature"], "material_change_signature"),
            _validated(kwargs["recipient_selector"], "recipient_selector"),
            _validated(kwargs["resolved_recipient"], "resolved_recipient"),
            _validated(kwargs["channel"], "channel"),
            _validated(kwargs["policy_version"], "policy_version"),
            dedup_key,
            canonical_json(dict(kwargs["deep_link"])),
            canonical_json(dict(kwargs["safe_payload"])),
        )
        with self._transaction():
            self._execute(
                "INSERT OR IGNORE INTO handoff_intent(intent_id, scope_key, triggering_event_id, episode_id, cycle_id, decision_snapshot_id, event_kind, material_change_signature, recipient_selector, resolved_recipient, channel, policy_version, dedup_key, deep_link_json, safe_payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            ) if self._is_sqlite else self._execute(
                "INSERT INTO handoff_intent(intent_id, scope_key, triggering_event_id, episode_id, cycle_id, decision_snapshot_id, event_kind, material_change_signature, recipient_selector, resolved_recipient, channel, policy_version, dedup_key, deep_link_json, safe_payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                values,
            )
            row = self._execute("SELECT * FROM handoff_intent WHERE scope_key = ? AND dedup_key = ?", (scope.canonical_key, dedup_key)).fetchone()
            if row is None:
                raise StorageFailureError("handoff intent insert was not durable")
            item = dict(row)
            item["deep_link"] = _json(item.pop("deep_link_json"), "deep link")
            item["safe_payload"] = _json(item.pop("safe_payload_json"), "notification payload")
            item["intent_id"] = item["intent_id"]
            status = self._execute("SELECT * FROM handoff_delivery_status WHERE intent_id = ?", (item["intent_id"],)).fetchone()
            if status is None:
                idempotency_key = f"ephi-o5.2:{dedup_key}"
                self._execute(
                    "INSERT INTO handoff_delivery_status(intent_id, scope_key, delivery_state, idempotency_key) VALUES (?, ?, ?, ?)",
                    (item["intent_id"], scope.canonical_key, PENDING, idempotency_key),
                )
            return item

    def bind_handoff_job(self, scope: AccessScope, intent_id: str, job_id: str) -> None:
        with self._transaction():
            self._execute(
                "UPDATE handoff_intent SET job_id = ?, updated_at = " + ("CURRENT_TIMESTAMP" if self._is_sqlite else "clock_timestamp()") + " WHERE scope_key = ? AND intent_id = ? AND (job_id IS NULL OR job_id = ?)",
                (job_id, scope.canonical_key, _validated(intent_id, "intent_id"), job_id),
            )
            self._execute(
                "UPDATE handoff_delivery_status SET job_id = ?, updated_at = " + ("CURRENT_TIMESTAMP" if self._is_sqlite else "clock_timestamp()") + " WHERE scope_key = ? AND intent_id = ? AND (job_id IS NULL OR job_id = ?)",
                (job_id, scope.canonical_key, _validated(intent_id, "intent_id"), job_id),
            )

    @staticmethod
    def _status_from_row(row: Mapping[str, Any]) -> dict[str, object]:
        item = dict(row)
        for source, target in (("deep_link_json", "deep_link"), ("safe_payload_json", "safe_payload")):
            if source in item:
                item[target] = _json(item.pop(source), target)
        item["delivery_state"] = item.pop("delivery_state")
        for key in ("created_at", "updated_at", "last_failure_at"):
            if item.get(key) is not None:
                item[key] = _timestamp(item[key])
        return item

    def get_handoff_status(self, scope: AccessScope, intent_id: str) -> dict[str, object] | None:
        row = self._execute(
            "SELECT i.intent_id, i.scope_key, i.episode_id, i.cycle_id, i.decision_snapshot_id, i.event_kind, i.recipient_selector, i.resolved_recipient, i.channel, i.policy_version, i.dedup_key, i.deep_link_json, i.created_at, i.updated_at, d.job_id, d.delivery_state, d.attempt_count, d.last_failure_code, d.last_failure_message, d.last_failure_at, d.ambiguity_warning, d.external_reference, d.idempotency_key FROM handoff_intent i JOIN handoff_delivery_status d ON d.intent_id = i.intent_id WHERE i.scope_key = ? AND i.intent_id = ?",
            (scope.canonical_key, _validated(intent_id, "intent_id")),
        ).fetchone()
        return self._status_from_row(row) if row is not None else None

    def list_handoff_status(self, scope: AccessScope, *, limit: int = 50) -> tuple[dict[str, object], ...]:
        rows = self._execute(
            "SELECT i.intent_id, i.scope_key, i.episode_id, i.cycle_id, i.decision_snapshot_id, i.event_kind, i.recipient_selector, i.resolved_recipient, i.channel, i.policy_version, i.dedup_key, i.deep_link_json, i.created_at, i.updated_at, d.job_id, d.delivery_state, d.attempt_count, d.last_failure_code, d.last_failure_message, d.last_failure_at, d.ambiguity_warning, d.external_reference, d.idempotency_key FROM handoff_intent i JOIN handoff_delivery_status d ON d.intent_id = i.intent_id WHERE i.scope_key = ? ORDER BY i.created_at DESC, i.intent_id DESC LIMIT ?",
            (scope.canonical_key, limit),
        ).fetchall()
        return tuple(self._status_from_row(row) for row in rows)

    def get_intent_for_job(self, scope: AccessScope, job_id: str) -> dict[str, object] | None:
        row = self._execute(
            "SELECT i.*, d.delivery_state, d.attempt_count, d.idempotency_key FROM handoff_intent i JOIN handoff_delivery_status d ON d.intent_id = i.intent_id WHERE i.scope_key = ? AND d.job_id = ?",
            (scope.canonical_key, _validated(job_id, "job_id")),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["deep_link"] = _json(item.pop("deep_link_json"), "deep link")
        item["safe_payload"] = _json(item.pop("safe_payload_json"), "notification payload")
        return item

    def mark_expired_deliveries_unknown(self, scope: AccessScope) -> int:
        with self._transaction():
            now = "CURRENT_TIMESTAMP" if self._is_sqlite else "clock_timestamp()"
            rows = self._execute(
                "SELECT d.intent_id, d.attempt_count FROM handoff_delivery_status d JOIN job j ON j.job_id = d.job_id WHERE d.scope_key = ? AND j.job_type = 'HANDOFF_DELIVERY' AND j.status = 'RUNNING' AND j.lease_expires_at <= " + now + " FOR UPDATE" if not self._is_sqlite else "SELECT d.intent_id, d.attempt_count FROM handoff_delivery_status d JOIN job j ON j.job_id = d.job_id WHERE d.scope_key = ? AND j.job_type = 'HANDOFF_DELIVERY' AND j.status = 'RUNNING' AND j.lease_expires_at <= " + now,
                (scope.canonical_key,),
            ).fetchall()
            for row in rows:
                self._execute(
                    "UPDATE handoff_delivery_status SET delivery_state = 'UNKNOWN', last_failure_code = 'LEASE_EXPIRED_OUTCOME_UNKNOWN', last_failure_message = 'delivery worker lease expired before durable completion', ambiguity_warning = 'external outcome is ambiguous; explicit reconciliation required', last_failure_at = " + now + ", updated_at = " + now + " WHERE scope_key = ? AND intent_id = ? AND delivery_state IN ('PENDING', 'DISPATCHING')",
                    (scope.canonical_key, row["intent_id"]),
                )
            return len(rows)

    def mark_delivery_dispatching(self, scope: AccessScope, intent_id: str, lease: WorkerLease) -> None:
        with self._transaction():
            lease_now = "CURRENT_TIMESTAMP" if self._is_sqlite else "clock_timestamp()"
            job = self._execute(
                "SELECT status, lease_owner, lease_epoch, lease_expires_at > " + lease_now + " AS lease_is_current FROM job WHERE job_id = ?",
                (lease.job_id,),
            ).fetchone()
            if job is None or job["status"] != "RUNNING" or job["lease_owner"] != lease.owner or int(job["lease_epoch"]) != lease.epoch or not bool(job["lease_is_current"]):
                raise StaleLeaseError(lease.job_id)
            changed = self._execute(
                "UPDATE handoff_delivery_status SET delivery_state = 'DISPATCHING', attempt_count = CASE WHEN attempt_count < (SELECT attempts FROM job WHERE job_id = ?) THEN (SELECT attempts FROM job WHERE job_id = ?) ELSE attempt_count END, updated_at = " + lease_now + " WHERE scope_key = ? AND intent_id = ? AND delivery_state = 'PENDING'",
                (lease.job_id, lease.job_id, scope.canonical_key, _validated(intent_id, "intent_id")),
            ).rowcount
            if changed != 1:
                current = self._execute("SELECT delivery_state FROM handoff_delivery_status WHERE scope_key = ? AND intent_id = ?", (scope.canonical_key, intent_id)).fetchone()
                if current is None or current["delivery_state"] not in {"DISPATCHING", DELIVERED, FAILED, UNKNOWN}:
                    raise StorageFailureError("delivery status could not enter dispatching state")

    def commit_delivery_effect(self, lease: WorkerLease, effect_key: str, input_payload: Mapping[str, object], **kwargs: object) -> AppliedEffectReceipt:
        if not isinstance(lease, WorkerLease):
            raise ValidationFailureError("lease must be a WorkerLease")
        intent_id = _validated(kwargs["intent_id"], "intent_id")
        state = _validated(kwargs["state"], "state")
        if state not in {DELIVERED, FAILED, UNKNOWN}:
            raise ValidationFailureError("delivery state must be terminal or ambiguous")
        input_hash = hashlib.sha256(canonical_json(dict(input_payload)).encode()).hexdigest()
        retryable = bool(kwargs.get("retryable", False))
        error_code = kwargs.get("error_code")
        error_message = kwargs.get("error_message")
        external_reference = kwargs.get("external_reference")
        with self._transaction():
            job = self._execute(
                "SELECT *, lease_expires_at > " + ("CURRENT_TIMESTAMP" if self._is_sqlite else "clock_timestamp()") + " AS lease_is_current FROM job WHERE job_id = ?",
                (lease.job_id,),
            ).fetchone()
            if job is None:
                raise StaleLeaseError(lease.job_id)
            lease_is_current = bool(job["lease_is_current"])
            if job["status"] != "RUNNING" or job["lease_owner"] != lease.owner or int(job["lease_epoch"]) != lease.epoch or not lease_is_current:
                raise StaleLeaseError(lease.job_id)
            existing = self._execute("SELECT * FROM applied_effect WHERE job_id = ? AND effect_key = ?", (lease.job_id, effect_key)).fetchone()
            if existing is not None:
                if existing["input_hash"] != input_hash:
                    raise EffectIdempotencyConflictError(lease.job_id, effect_key)
                return self._effect_from_row(existing)
            delivery = self._execute("SELECT * FROM handoff_delivery_status WHERE intent_id = ? FOR UPDATE" if not self._is_sqlite else "SELECT * FROM handoff_delivery_status WHERE intent_id = ?", (intent_id,)).fetchone()
            if delivery is None:
                raise ValidationFailureError("delivery status is not available")
            attempt_no = max(int(delivery["attempt_count"]) + 1, int(job["attempts"]))
            exhausted = state == FAILED and retryable and int(job["attempts"]) >= int(job["max_attempts"])
            next_state = FAILED if exhausted else (PENDING if state == FAILED and retryable else state)
            warning = "external outcome is ambiguous; explicit reconciliation required" if state == UNKNOWN else None
            last_failure_at = "CURRENT_TIMESTAMP" if self._is_sqlite else "clock_timestamp()"
            self._execute(
                "UPDATE handoff_delivery_status SET delivery_state = ?, attempt_count = ?, last_failure_code = ?, last_failure_message = ?, last_failure_at = " + last_failure_at + ", ambiguity_warning = ?, external_reference = ?, updated_at = " + last_failure_at + " WHERE intent_id = ? AND scope_key = ?",
                (next_state, attempt_no, error_code, error_message, warning, external_reference, intent_id, job["scope_key"]),
            )
            self._execute(
                "INSERT INTO handoff_delivery_attempt(intent_id, scope_key, attempt_no, delivery_state, error_code, external_reference) VALUES (?, ?, ?, ?, ?, ?)",
                (intent_id, job["scope_key"], attempt_no, state, error_code, external_reference),
            )
            result = {"intent_id": intent_id, "state": state, "external_reference": external_reference, "error_code": error_code}
            result_json = canonical_json(result)
            committed_at_expr = "CURRENT_TIMESTAMP" if self._is_sqlite else "clock_timestamp()"
            receipt_row = self._execute(
                "INSERT INTO applied_effect(job_id, effect_key, input_hash, committed_revision, result_identity, result_json, committed_at) VALUES (?, ?, ?, ?, ?, ?, " + committed_at_expr + ") RETURNING *",
                (lease.job_id, effect_key, input_hash, attempt_no, hashlib.sha256(f"{lease.job_id}:{effect_key}:{input_hash}".encode()).hexdigest(), result_json),
            ).fetchone()
            if receipt_row is None:  # pragma: no cover - database failure
                raise StorageFailureError("delivery effect receipt was not durable")
            if next_state == DELIVERED:
                job_status = "SUCCEEDED"
            elif next_state == FAILED:
                job_status = "FAILED"
            elif state == UNKNOWN:
                job_status = "FAILED"
            else:
                job_status = "QUEUED"
            if job_status == "QUEUED":
                self._execute(
                    "UPDATE job SET status = 'QUEUED', lease_owner = NULL, lease_expires_at = NULL, available_at = " + ("datetime('now', '+1 second')" if self._is_sqlite else "clock_timestamp() + interval '1 second'") + ", updated_at = " + committed_at_expr + " WHERE job_id = ? AND status = 'RUNNING' AND lease_owner = ? AND lease_epoch = ?",
                    (lease.job_id, lease.owner, lease.epoch),
                )
            else:
                self._execute(
                    "UPDATE job SET status = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = " + committed_at_expr + " WHERE job_id = ? AND status = 'RUNNING' AND lease_owner = ? AND lease_epoch = ?",
                    (job_status, lease.job_id, lease.owner, lease.epoch),
                )
            return self._effect_from_row(receipt_row)

    @staticmethod
    def _effect_from_row(row: Mapping[str, Any]) -> AppliedEffectReceipt:
        return AppliedEffectReceipt(
            job_id=row["job_id"],
            effect_key=row["effect_key"],
            input_hash=row["input_hash"],
            committed_revision=int(row["committed_revision"]),
            result_identity=row["result_identity"],
            result=_json(row["result_json"], "applied effect result"),
            committed_at=row["committed_at"],
        )

    def reconcile_unknown(self, scope: AccessScope, intent_id: str, result: DeliveryResult) -> dict[str, object]:
        if not isinstance(result, DeliveryResult):
            raise ValidationFailureError("reconciliation result must be a DeliveryResult")
        with self._transaction():
            row = self._execute(
                "SELECT * FROM handoff_delivery_status WHERE scope_key = ? AND intent_id = ?",
                (scope.canonical_key, _validated(intent_id, "intent_id")),
            ).fetchone()
            if row is None:
                raise ValidationFailureError("handoff status is not available")
            if row["delivery_state"] != UNKNOWN:
                status = self.get_handoff_status(scope, intent_id)
                if status is None:
                    raise StorageFailureError("handoff status disappeared")
                return status
            next_state = result.state
            if next_state == UNKNOWN:
                next_state = UNKNOWN
            warning = "external outcome is ambiguous; explicit reconciliation required" if next_state == UNKNOWN else None
            attempt = int(row["attempt_count"]) + 1
            now_expr = "CURRENT_TIMESTAMP" if self._is_sqlite else "clock_timestamp()"
            self._execute(
                "UPDATE handoff_delivery_status SET delivery_state = ?, attempt_count = ?, last_failure_code = ?, last_failure_message = ?, last_failure_at = " + now_expr + ", ambiguity_warning = ?, external_reference = COALESCE(?, external_reference), updated_at = " + now_expr + " WHERE scope_key = ? AND intent_id = ? AND delivery_state = 'UNKNOWN'",
                (next_state, attempt, result.error_code, result.error_message, warning, result.external_reference, scope.canonical_key, intent_id),
            )
            self._execute(
                "INSERT INTO handoff_delivery_attempt(intent_id, scope_key, attempt_no, delivery_state, error_code, external_reference) VALUES (?, ?, ?, ?, ?, ?)",
                (intent_id, scope.canonical_key, attempt, next_state, result.error_code, result.external_reference),
            )
            status = self.get_handoff_status(scope, intent_id)
            if status is None:
                raise StorageFailureError("reconciled handoff status is not available")
            return status
