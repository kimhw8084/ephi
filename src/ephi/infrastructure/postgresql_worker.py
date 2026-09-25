"""Real PostgreSQL implementation of the generic durable worker port.

This module contains queue mechanics only.  It does not know how a scientific
job is computed, delivered, or presented in a UI.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from ephi.application.context import AccessScope
from ephi.application.errors import (
    AggregateNotFoundError,
    CommandError,
    EffectIdempotencyConflictError,
    InvalidTransitionError,
    JobNotFoundError,
    JobSemanticConflictError,
    NoEligibleJobError,
    StorageFailureError,
    StaleLeaseError,
    ValidationFailureError,
)
from ephi.application.hashing import canonical_json, normalize_domain_payload
from ephi.application.worker import (
    AppliedEffectReceipt,
    JobRecord,
    LocalEffect,
    WorkerLease,
    WorkerLeaseConfig,
)
from ephi.application.operations import WorkerHealthFacts

if TYPE_CHECKING:
    from .postgresql import PostgreSQLReferenceTransactionAdapter


_TERMINAL_STATUSES = ("SUCCEEDED", "FAILED", "DEAD_LETTER", "CANCELED")
_DEFAULT_RETRY_CAP_SECONDS = 3600
_ATTEMPT_EXHAUSTED_CODE = "ATTEMPT_BUDGET_EXHAUSTED"
_ATTEMPT_EXHAUSTED_MESSAGE = "attempt budget exhausted after lease expiry or due retry"


def _validated_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


def _validated_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailureError(f"{field} must be timezone-aware")
    return value


def _validated_positive_duration(value: timedelta, field: str) -> timedelta:
    if not isinstance(value, timedelta) or value.total_seconds() <= 0:
        raise ValidationFailureError(f"{field} must be positive")
    return value


def _json_object(value: object, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageFailureError(f"durable PostgreSQL {field} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise StorageFailureError(f"durable PostgreSQL {field} is not an object")
    return parsed


def _input_hash(value: Mapping[str, object]) -> tuple[dict[str, Any], str]:
    normalized = normalize_domain_payload(value)
    if not isinstance(normalized, dict):
        raise ValidationFailureError("local-effect input must normalize to an object")
    digest = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    return normalized, digest


def _result_identity(job_id: str, effect_key: str, input_hash: str) -> str:
    return hashlib.sha256(f"ephi-o2:local-effect:{job_id}:{effect_key}:{input_hash}".encode("utf-8")).hexdigest()


def _default_mutation(current: Mapping[str, Any], payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """A deliberately small fixture mutation for adapter/integration tests."""

    effect_count = current.get("effect_count", 0)
    if isinstance(effect_count, bool) or not isinstance(effect_count, int) or effect_count < 0:
        raise ValidationFailureError("fixture aggregate effect_count must be a non-negative integer")
    next_state = dict(current)
    next_state["effect_count"] = effect_count + 1
    next_state["last_effect_input"] = dict(payload)
    return next_state


class PostgreSQLWorkerStore:
    """Durable worker substrate backed by a real PostgreSQL connection."""

    def __init__(
        self,
        adapter: PostgreSQLReferenceTransactionAdapter,
        *,
        config: WorkerLeaseConfig | None = None,
    ) -> None:
        if not hasattr(adapter, "connection"):
            raise TypeError("adapter must provide the PostgreSQL reference connection")
        self.adapter = adapter
        self.config = config or WorkerLeaseConfig()
        self._lease_duration = _validated_positive_duration(self.config.lease_duration, "lease_duration")
        self._heartbeat_interval = _validated_positive_duration(self.config.heartbeat_interval, "heartbeat_interval")

    @property
    def lease_duration(self) -> timedelta:
        return self._lease_duration

    @property
    def heartbeat_interval(self) -> timedelta:
        return self._heartbeat_interval

    @contextmanager
    def _transaction(self):
        connection = self.adapter.connection
        try:
            connection.execute("BEGIN")
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL worker transaction could not begin") from exc
        try:
            yield connection
            connection.commit()
        except CommandError:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                pass
            raise StorageFailureError("durable PostgreSQL worker transaction failed") from exc

    def enqueue(
        self,
        scope: AccessScope,
        job_type: str,
        semantic_key: str,
        payload: Mapping[str, object],
        *,
        available_at: datetime | None = None,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> JobRecord:
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        job_type = _validated_identity(job_type, "job_type")
        semantic_key = _validated_identity(semantic_key, "semantic_key")
        if not isinstance(payload, Mapping):
            raise ValidationFailureError("job payload must be a mapping")
        normalized_payload, payload_hash = _input_hash(payload)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValidationFailureError("priority must be an integer")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
            raise ValidationFailureError("max_attempts must be a positive integer")
        if available_at is not None:
            available_at = _validated_datetime(available_at, "available_at")

        scope_key = scope.canonical_key
        job_id = uuid4().hex
        connection = self.adapter.connection
        try:
            connection.execute("BEGIN")
            existing = connection.execute(
                "SELECT * FROM job WHERE scope_key = %s AND semantic_key = %s FOR UPDATE",
                (scope_key, semantic_key),
            ).fetchone()
            if existing is not None:
                result = self._resolve_enqueue(existing, scope_key, semantic_key, job_type, payload_hash)
                connection.commit()
                return result
            row = connection.execute(
                """
                INSERT INTO job(
                    job_id, scope_key, job_type, semantic_key, payload_hash, payload_json,
                    status, priority, available_at, attempts, max_attempts
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'QUEUED', %s,
                          COALESCE(%s::timestamptz, clock_timestamp()), 0, %s)
                RETURNING *
                """,
                (
                    job_id, scope_key, job_type, semantic_key, payload_hash,
                    canonical_json(normalized_payload), priority, available_at, max_attempts,
                ),
            ).fetchone()
            connection.commit()
            if row is None:  # pragma: no cover - PostgreSQL RETURNING contract
                raise StorageFailureError("durable PostgreSQL enqueue did not return its job")
            return self._job_from_row(row)
        except CommandError:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                pass
            if self._is_semantic_unique_conflict(exc):
                return self._reconcile_enqueue(scope_key, semantic_key, job_type, payload_hash)
            raise StorageFailureError("durable PostgreSQL storage failed while enqueueing a job") from exc

    def _resolve_enqueue(
        self,
        row: Mapping[str, Any],
        scope_key: str,
        semantic_key: str,
        job_type: str,
        payload_hash: str,
    ) -> JobRecord:
        if row["job_type"] != job_type or row["payload_hash"] != payload_hash:
            raise JobSemanticConflictError(scope_key, semantic_key)
        return self._job_from_row(row)

    def _reconcile_enqueue(self, scope_key: str, semantic_key: str, job_type: str, payload_hash: str) -> JobRecord:
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM job WHERE scope_key = %s AND semantic_key = %s",
                    (scope_key, semantic_key),
                ).fetchone()
                if row is None:
                    raise StorageFailureError("job enqueue race could not be reconciled; retry the same semantic key")
                return self._resolve_enqueue(row, scope_key, semantic_key, job_type, payload_hash)
        except JobSemanticConflictError:
            raise
        except StorageFailureError:
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise StorageFailureError("durable PostgreSQL enqueue reconciliation failed") from exc

    @staticmethod
    def _is_semantic_unique_conflict(exc: Exception) -> bool:
        return (
            getattr(exc, "sqlstate", None) == "23505"
            and getattr(getattr(exc, "diag", None), "constraint_name", None) == "job_scope_semantic_unique"
        )

    def claim(self, scope: AccessScope, owner: str, *, raise_if_none: bool = False, job_type: str | None = None) -> JobRecord | None:
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        owner = _validated_identity(owner, "owner")
        if job_type is not None:
            job_type = _validated_identity(job_type, "job_type")
        seconds = self._lease_duration.total_seconds()
        with self._transaction() as connection:
            row = connection.execute(
                """
                WITH db_now AS MATERIALIZED (
                    SELECT clock_timestamp() AS now
                ), exhausted AS MATERIALIZED (
                    SELECT j.job_id
                    FROM job AS j
                    CROSS JOIN db_now
                    WHERE j.scope_key = %s
                      AND (%s::text IS NULL OR j.job_type = %s)
                      AND j.attempts >= j.max_attempts
                      AND (
                          (j.status = 'RUNNING' AND j.lease_expires_at <= db_now.now)
                          OR (j.status IN ('QUEUED', 'DEFERRED') AND j.available_at <= db_now.now)
                      )
                    ORDER BY j.priority DESC, j.available_at ASC, j.created_at ASC, j.job_id ASC
                    FOR UPDATE SKIP LOCKED
                ), dead_lettered AS (
                    UPDATE job AS j
                    SET status = 'DEAD_LETTER',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        last_failure_code = %s,
                        last_failure_message = %s,
                        last_failure_at = db_now.now,
                        updated_at = db_now.now
                    FROM exhausted, db_now
                    WHERE j.job_id = exhausted.job_id
                    RETURNING j.job_id
                ), candidate AS MATERIALIZED (
                    SELECT j.job_id
                    FROM job AS j
                    CROSS JOIN db_now
                    WHERE j.scope_key = %s
                      AND (%s::text IS NULL OR j.job_type = %s)
                      AND j.status IN ('QUEUED', 'DEFERRED', 'RUNNING')
                      AND j.attempts < j.max_attempts
                      AND j.available_at <= db_now.now
                      AND (j.lease_expires_at IS NULL OR j.lease_expires_at <= db_now.now)
                    ORDER BY j.priority DESC, j.available_at ASC, j.created_at ASC, j.job_id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE job AS j
                SET status = 'RUNNING',
                    attempts = j.attempts + 1,
                    lease_owner = %s,
                    lease_epoch = j.lease_epoch + 1,
                    lease_expires_at = db_now.now + (%s * INTERVAL '1 second'),
                    updated_at = db_now.now
                FROM candidate, db_now
                WHERE j.job_id = candidate.job_id
                RETURNING j.*
                """,
                (
                    scope.canonical_key,
                    job_type, job_type,
                    _ATTEMPT_EXHAUSTED_CODE,
                    _ATTEMPT_EXHAUSTED_MESSAGE,
                    scope.canonical_key,
                    job_type, job_type,
                    owner,
                    seconds,
                ),
            ).fetchone()
            if row is None:
                if raise_if_none:
                    raise NoEligibleJobError("no eligible durable job is available")
                return None
            return self._job_from_row(row)

    def heartbeat(self, lease: WorkerLease) -> JobRecord:
        self._validate_lease(lease)
        seconds = self._lease_duration.total_seconds()
        with self._transaction() as connection:
            row = connection.execute(
                """
                UPDATE job
                SET lease_expires_at = clock_timestamp() + (%s * INTERVAL '1 second'),
                    updated_at = clock_timestamp()
                WHERE job_id = %s AND status = 'RUNNING'
                  AND lease_owner = %s AND lease_epoch = %s
                  AND lease_expires_at > clock_timestamp()
                RETURNING *
                """,
                (seconds, lease.job_id, lease.owner, lease.epoch),
            ).fetchone()
            if row is None:
                self._raise_lease_failure(connection, lease)
            return self._job_from_row(row)

    def complete(self, lease: WorkerLease) -> JobRecord:
        self._validate_lease(lease)
        with self._transaction() as connection:
            row = connection.execute(
                """
                UPDATE job
                SET status = 'SUCCEEDED', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE job_id = %s AND status = 'RUNNING'
                  AND lease_owner = %s AND lease_epoch = %s
                  AND lease_expires_at > clock_timestamp()
                RETURNING *
                """,
                (lease.job_id, lease.owner, lease.epoch),
            ).fetchone()
            if row is None:
                self._raise_lease_failure(connection, lease)
            return self._job_from_row(row)

    def fail(
        self,
        lease: WorkerLease,
        *,
        retryable: bool,
        error_code: str,
        error_message: str,
        next_available_at: datetime | None = None,
    ) -> JobRecord:
        self._validate_lease(lease)
        if not isinstance(retryable, bool):
            raise ValidationFailureError("retryable must be a boolean")
        error_code = _validated_identity(error_code, "error_code")
        error_message = _validated_identity(error_message, "error_message")
        if next_available_at is not None:
            next_available_at = _validated_datetime(next_available_at, "next_available_at")
        with self._transaction() as connection:
            current = self._locked_lease(connection, lease)
            terminal_status = "FAILED"
            if retryable:
                terminal_status = "QUEUED" if current["attempts"] < current["max_attempts"] else "DEAD_LETTER"
            backoff_seconds = min(2 ** max(int(current["attempts"]) - 1, 0), _DEFAULT_RETRY_CAP_SECONDS)
            row = connection.execute(
                """
                UPDATE job
                SET status = %s,
                    available_at = CASE WHEN %s = 'QUEUED'
                        THEN COALESCE(%s::timestamptz, clock_timestamp() + (%s * INTERVAL '1 second'))
                        ELSE available_at END,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_failure_code = %s,
                    last_failure_message = %s,
                    last_failure_at = clock_timestamp(),
                    updated_at = clock_timestamp()
                WHERE job_id = %s AND status = 'RUNNING'
                  AND lease_owner = %s AND lease_epoch = %s
                  AND lease_expires_at > clock_timestamp()
                RETURNING *
                """,
                (
                    terminal_status, terminal_status, next_available_at, backoff_seconds,
                    error_code, error_message, lease.job_id, lease.owner, lease.epoch,
                ),
            ).fetchone()
            if row is None:
                self._raise_lease_failure(connection, lease)
            return self._job_from_row(row)

    def defer(self, lease: WorkerLease, available_at: datetime) -> JobRecord:
        self._validate_lease(lease)
        available_at = _validated_datetime(available_at, "available_at")
        with self._transaction() as connection:
            self._locked_lease(connection, lease)
            future = connection.execute("SELECT %s::timestamptz > clock_timestamp() AS is_future", (available_at,)).fetchone()
            if not future["is_future"]:
                raise ValidationFailureError("deferred available_at must be in the future by PostgreSQL time")
            row = connection.execute(
                """
                UPDATE job
                SET status = 'DEFERRED', available_at = %s, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = clock_timestamp()
                WHERE job_id = %s AND status = 'RUNNING'
                  AND lease_owner = %s AND lease_epoch = %s
                  AND lease_expires_at > clock_timestamp()
                RETURNING *
                """,
                (available_at, lease.job_id, lease.owner, lease.epoch),
            ).fetchone()
            if row is None:
                self._raise_lease_failure(connection, lease)
            return self._job_from_row(row)

    def cancel(self, scope: AccessScope, job_id: str, *, lease: WorkerLease | None = None) -> JobRecord:
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        job_id = _validated_identity(job_id, "job_id")
        if lease is not None:
            self._validate_lease(lease)
            if lease.job_id != job_id:
                raise StaleLeaseError(job_id)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT *, lease_expires_at > clock_timestamp() AS lease_is_current FROM job WHERE job_id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is None or row["scope_key"] != scope.canonical_key:
                raise JobNotFoundError("job is not available in the requested scope")
            if row["status"] in _TERMINAL_STATUSES:
                raise InvalidTransitionError("terminal jobs cannot be canceled")
            effect = connection.execute("SELECT 1 FROM applied_effect WHERE job_id = %s LIMIT 1", (job_id,)).fetchone()
            if effect is not None:
                raise InvalidTransitionError("a job with a committed local effect cannot be silently canceled")
            if lease is not None:
                self._assert_locked_lease(row, lease, connection)
            elif row["status"] == "RUNNING":
                valid = connection.execute(
                    "SELECT %s::timestamptz <= clock_timestamp() AS expired",
                    (row["lease_expires_at"],),
                ).fetchone()
                # The row is locked, so an unexpired running lease must be
                # canceled by its exact owner/epoch instead.
                if not valid["expired"]:
                    raise StaleLeaseError(job_id)
            if lease is not None:
                update_sql = """
                    UPDATE job SET status = 'CANCELED', lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = clock_timestamp()
                    WHERE job_id = %s AND scope_key = %s AND status = 'RUNNING'
                      AND lease_owner = %s AND lease_epoch = %s
                      AND lease_expires_at > clock_timestamp()
                    RETURNING *
                """
                update_params = (job_id, scope.canonical_key, lease.owner, lease.epoch)
            else:
                update_sql = """
                    UPDATE job SET status = 'CANCELED', lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = clock_timestamp()
                    WHERE job_id = %s AND scope_key = %s
                      AND (status IN ('QUEUED', 'DEFERRED')
                           OR (status = 'RUNNING' AND lease_expires_at <= clock_timestamp()))
                    RETURNING *
                """
                update_params = (job_id, scope.canonical_key)
            row = connection.execute(update_sql, update_params).fetchone()
            if row is None:  # pragma: no cover - row is held by this transaction
                raise StaleLeaseError(job_id)
            return self._job_from_row(row)

    def inspect(
        self,
        scope: AccessScope,
        *,
        job_id: str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 100,
    ) -> tuple[JobRecord, ...]:
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        if job_id is not None:
            job_id = _validated_identity(job_id, "job_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationFailureError("inspect limit must be between 1 and 1000")
        status_values = None
        if statuses is not None:
            status_values = [_validated_identity(status, "status") for status in statuses]
            if not status_values:
                raise ValidationFailureError("statuses must not be empty")
        clauses = ["scope_key = %s"]
        params: list[Any] = [scope.canonical_key]
        if job_id is not None:
            clauses.append("job_id = %s")
            params.append(job_id)
        if status_values is not None:
            clauses.append("status = ANY(%s)")
            params.append(status_values)
        params.append(limit)
        try:
            rows = self.adapter.connection.execute(
                f"SELECT * FROM job WHERE {' AND '.join(clauses)} ORDER BY priority DESC, available_at ASC, created_at ASC, job_id ASC LIMIT %s",
                params,
            ).fetchall()
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while inspecting jobs") from exc
        return tuple(self._job_from_row(row) for row in rows)

    def operations_health_facts(self, scope: AccessScope) -> WorkerHealthFacts:
        """Return scoped queue counts and lease classification at PostgreSQL time."""

        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        try:
            row = self.adapter.connection.execute(
                """
                WITH authoritative_clock AS MATERIALIZED (
                    SELECT clock_timestamp() AS now
                )
                SELECT
                    authoritative_clock.now AS observed_at,
                    COUNT(job.job_id)::int AS job_count,
                    COUNT(job.job_id) FILTER (WHERE job.status = 'FAILED')::int AS failed_count,
                    COUNT(job.job_id) FILTER (WHERE job.status = 'DEAD_LETTER')::int AS dead_letter_count,
                    COUNT(job.job_id) FILTER (WHERE job.status = 'RUNNING')::int AS running_count,
                    COUNT(job.job_id) FILTER (
                        WHERE job.status = 'RUNNING'
                          AND (job.lease_expires_at IS NULL OR job.lease_expires_at <= authoritative_clock.now)
                    )::int AS expired_running_count
                FROM authoritative_clock
                LEFT JOIN job ON job.scope_key = %s
                GROUP BY authoritative_clock.now
                """,
                (scope.canonical_key,),
            ).fetchone()
            if row is None:
                raise ValueError("worker health query returned no row")
            return WorkerHealthFacts(
                int(row["job_count"]),
                int(row["failed_count"]),
                int(row["dead_letter_count"]),
                int(row["running_count"]),
                int(row["expired_running_count"]),
                row["observed_at"],
            )
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL worker health facts are unavailable") from exc

    def has_committed_local_effect(self, scope: AccessScope, job_id: str) -> bool:
        """Prove only receipt existence for one already-inspected scoped job."""

        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        job_id = _validated_identity(job_id, "job_id")
        try:
            row = self.adapter.connection.execute(
                "SELECT EXISTS (SELECT 1 FROM job j JOIN applied_effect e ON e.job_id = j.job_id "
                "WHERE j.scope_key = %s AND j.job_id = %s) AS receipt_exists",
                (scope.canonical_key, job_id),
            ).fetchone()
            if row is None:
                raise ValueError("effect receipt query returned no row")
            return bool(row["receipt_exists"])
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL local-effect receipt check is unavailable") from exc

    def commit_local_effect(
        self,
        lease: WorkerLease,
        effect_key: str,
        input_payload: Mapping[str, object],
        *,
        aggregate_type: str,
        aggregate_id: str,
        mutation: LocalEffect | None = None,
    ) -> AppliedEffectReceipt:
        self._validate_lease(lease)
        effect_key = _validated_identity(effect_key, "effect_key")
        aggregate_type = _validated_identity(aggregate_type, "aggregate_type")
        aggregate_id = _validated_identity(aggregate_id, "aggregate_id")
        if not isinstance(input_payload, Mapping):
            raise ValidationFailureError("local-effect input must be a mapping")
        if mutation is not None and not callable(mutation):
            raise ValidationFailureError("mutation must be callable or None")
        normalized_input, input_hash = _input_hash(input_payload)
        with self._transaction() as connection:
            job = self._locked_lease(connection, lease)
            existing = connection.execute(
                "SELECT * FROM applied_effect WHERE job_id = %s AND effect_key = %s",
                (lease.job_id, effect_key),
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != input_hash:
                    raise EffectIdempotencyConflictError(lease.job_id, effect_key)
                self._require_current_lease(connection, lease)
                return self._effect_from_row(existing)

            aggregate = connection.execute(
                "SELECT scope_key, aggregate_type, aggregate_id, version, state_json FROM aggregate_state WHERE scope_key = %s AND aggregate_type = %s AND aggregate_id = %s FOR UPDATE",
                (job["scope_key"], aggregate_type, aggregate_id),
            ).fetchone()
            if aggregate is None:
                raise AggregateNotFoundError("local-effect target is not available in the job scope")
            current_state = _json_object(aggregate["state_json"], "aggregate state")
            try:
                next_state_raw = (mutation or _default_mutation)(current_state, normalized_input)
            except CommandError:
                raise
            except Exception as exc:
                raise ValidationFailureError("local-effect mutation rejected the input") from exc
            next_state = normalize_domain_payload(next_state_raw)
            if not isinstance(next_state, dict):
                raise ValidationFailureError("local-effect mutation must return a mapping")
            next_revision = int(aggregate["version"]) + 1
            result_identity = _result_identity(lease.job_id, effect_key, input_hash)
            result_json = canonical_json(next_state)
            changed = connection.execute(
                """
                UPDATE aggregate_state AS aggregate
                SET version = %s, state_json = %s::jsonb
                WHERE aggregate.scope_key = %s AND aggregate.aggregate_type = %s
                  AND aggregate.aggregate_id = %s AND aggregate.version = %s
                  AND EXISTS (
                      SELECT 1 FROM job
                      WHERE job_id = %s AND status = 'RUNNING'
                        AND lease_owner = %s AND lease_epoch = %s
                        AND lease_expires_at > clock_timestamp()
                  )
                """,
                (
                    next_revision, result_json, job["scope_key"], aggregate_type,
                    aggregate_id, aggregate["version"], lease.job_id, lease.owner, lease.epoch,
                ),
            ).rowcount
            if changed != 1:
                self._raise_lease_failure(connection, lease)

            receipt = connection.execute(
                """
                INSERT INTO applied_effect(
                    job_id, effect_key, input_hash, committed_revision, result_identity, result_json, committed_at
                )
                SELECT %s, %s, %s, %s, %s, %s::jsonb, clock_timestamp()
                WHERE EXISTS (
                    SELECT 1 FROM job
                    WHERE job_id = %s AND status = 'RUNNING'
                      AND lease_owner = %s AND lease_epoch = %s
                      AND lease_expires_at > clock_timestamp()
                )
                RETURNING *
                """,
                (
                    lease.job_id, effect_key, input_hash, next_revision, result_identity,
                    result_json, lease.job_id, lease.owner, lease.epoch,
                ),
            ).fetchone()
            if receipt is None:
                self._raise_lease_failure(connection, lease)
            return self._effect_from_row(receipt)

    def _locked_lease(self, connection: Any, lease: WorkerLease) -> Mapping[str, Any]:
        row = connection.execute(
            """
            SELECT *, lease_expires_at > clock_timestamp() AS lease_is_current
            FROM job WHERE job_id = %s FOR UPDATE
            """,
            (lease.job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFoundError("job does not exist")
        self._assert_locked_lease(row, lease, connection)
        return row

    def _require_current_lease(self, connection: Any, lease: WorkerLease) -> None:
        row = connection.execute(
            """
            SELECT status, lease_owner, lease_epoch,
                   lease_expires_at > clock_timestamp() AS lease_is_current
            FROM job WHERE job_id = %s FOR UPDATE
            """,
            (lease.job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFoundError("job does not exist")
        self._assert_locked_lease(row, lease, connection)

    def _assert_locked_lease(self, row: Mapping[str, Any], lease: WorkerLease, connection: Any) -> None:
        if row["status"] in _TERMINAL_STATUSES:
            raise InvalidTransitionError("terminal jobs cannot be mutated")
        if (
            row["status"] != "RUNNING"
            or row["lease_owner"] != lease.owner
            or int(row["lease_epoch"]) != lease.epoch
            or not row.get("lease_is_current", False)
        ):
            raise StaleLeaseError(lease.job_id)

    def _raise_lease_failure(self, connection: Any, lease: WorkerLease) -> None:
        row = connection.execute(
            "SELECT *, lease_expires_at > clock_timestamp() AS lease_is_current FROM job WHERE job_id = %s FOR UPDATE",
            (lease.job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFoundError("job does not exist")
        self._assert_locked_lease(row, lease, connection)
        raise StaleLeaseError(lease.job_id)

    @staticmethod
    def _validate_lease(lease: WorkerLease) -> None:
        if not isinstance(lease, WorkerLease):
            raise ValidationFailureError("lease must be a WorkerLease")
        _validated_identity(lease.job_id, "lease.job_id")
        _validated_identity(lease.owner, "lease.owner")
        if isinstance(lease.epoch, bool) or not isinstance(lease.epoch, int) or lease.epoch <= 0:
            raise ValidationFailureError("lease.epoch must be a positive integer")

    @staticmethod
    def _job_from_row(row: Mapping[str, Any]) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"],
            scope_key=row["scope_key"],
            job_type=row["job_type"],
            semantic_key=row["semantic_key"],
            payload_hash=row["payload_hash"],
            payload=_json_object(row["payload_json"], "job payload"),
            status=row["status"],
            priority=int(row["priority"]),
            available_at=row["available_at"],
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            lease_owner=row["lease_owner"],
            lease_epoch=int(row["lease_epoch"]),
            lease_expires_at=row["lease_expires_at"],
            last_failure_code=row["last_failure_code"],
            last_failure_message=row["last_failure_message"],
            last_failure_at=row["last_failure_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _effect_from_row(row: Mapping[str, Any]) -> AppliedEffectReceipt:
        return AppliedEffectReceipt(
            job_id=row["job_id"],
            effect_key=row["effect_key"],
            input_hash=row["input_hash"],
            committed_revision=int(row["committed_revision"]),
            result_identity=row["result_identity"],
            result=_json_object(row["result_json"], "applied effect result"),
            committed_at=row["committed_at"],
        )
