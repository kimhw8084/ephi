"""Real PostgreSQL source-snapshot and capability projection adapter."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from typing import Any, Iterator

from ephi.application.context import AccessScope, Principal
from ephi.application.errors import (
    AuthorizationDeniedError,
    ScopeDeniedError,
    SourceSnapshotConflictError,
    SourceSnapshotNotFoundError,
    StorageFailureError,
    ValidationFailureError,
)
from ephi.application.source_ingress import (
    SOURCE_READ_CAPABILITY,
    MetrologySourceBinding,
    SourceCapabilityRecord,
    SourceCapabilityState,
    SourceSnapshotDraft,
    SourceSnapshotRecord,
    SourceSnapshotStatus,
    SOURCE_SNAPSHOT_SCHEMA_VERSION,
    _timestamp,
)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _db_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StorageFailureError(f"durable PostgreSQL {field} is not a timezone-aware timestamp")
    return value.astimezone(timezone.utc)


def _scope(value: object) -> AccessScope:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        if not isinstance(decoded, Mapping):
            raise ValueError
        return AccessScope(
            decoded["scope_id"],
            decoded.get("site_id"),
            decoded.get("area_id"),
            decoded.get("family_id"),
            tuple(decoded.get("project_ids", ())),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StorageFailureError("durable PostgreSQL source scope identity is invalid") from exc


def _row_binding(row: Mapping[str, Any]) -> MetrologySourceBinding:
    try:
        return MetrologySourceBinding(
            _scope(row["scope_key"]),
            row["source_id"],
            row["provider_id"],
            row["family_id"],
            row["capability_id"],
            row["adapter_id"],
            row["schema_id"],
            row["mapping_version"],
            row["mapping_hash"],
            row["unit"],
            row["reference_population_id"],
            row["comparable_population_id"],
            tuple(json.loads(row["required_identifiers_json"]) if isinstance(row["required_identifiers_json"], str) else row["required_identifiers_json"]),
        )
    except Exception as exc:
        raise StorageFailureError("durable PostgreSQL source binding identity is invalid") from exc


def _source_record(row: Mapping[str, Any]) -> SourceSnapshotRecord:
    try:
        binding = _row_binding(row)
        from ephi.application.artifacts import ArtifactContentIdentity, ScopedArtifactReference

        artifact = ScopedArtifactReference(
            binding.scope,
            ArtifactContentIdentity(row["manifest_artifact_sha256"], int(row["manifest_artifact_byte_size"])),
        )
        status = SourceSnapshotStatus(row["status"])
        return SourceSnapshotRecord(
            row["snapshot_id"],
            binding,
            row["source_partition"],
            row["source_revision"],
            _db_time(row["event_start"], "event_start"),
            _db_time(row["event_end"], "event_end"),
            _db_time(row["available_cutoff"], "available_cutoff"),
            artifact,
            int(row["row_count"]),
            status,
            row["manifest_hash"],
            _db_time(row["ingested_at"], "ingested_at"),
            _db_time(row["published_at"], "published_at"),
            _db_time(row["created_at"], "created_at"),
            row["schema_version"],
            None if row.get("freshness_age_seconds") is None else int(row["freshness_age_seconds"]),
        )
    except SourceSnapshotConflictError:
        raise
    except Exception as exc:
        raise StorageFailureError("durable PostgreSQL source snapshot row is invalid") from exc


def _capability_record(row: Mapping[str, Any]) -> SourceCapabilityRecord:
    binding = _row_binding(row)
    try:
        return SourceCapabilityRecord(
            binding,
            SourceCapabilityState(row["state"]),
            row["latest_snapshot_id"],
            None if row["latest_event_at"] is None else _db_time(row["latest_event_at"], "latest_event_at"),
            None if row["latest_available_at"] is None else _db_time(row["latest_available_at"], "latest_available_at"),
            _db_time(row["checked_at"], "checked_at"),
            int(row["freshness_age_seconds"]),
            row["reason"],
            row["latest_source_partition"],
            row["latest_source_revision"],
            row["schema_version"],
        )
    except Exception as exc:
        raise StorageFailureError("durable PostgreSQL source capability row is invalid") from exc


_SOURCE_COLUMNS = (
    "snapshot_id, schema_version, scope_key, source_id, provider_id, family_id, capability_id, adapter_id, schema_id, "
    "mapping_version, mapping_hash, unit, reference_population_id, comparable_population_id, "
    "required_identifiers_json, source_partition, source_revision, event_start, event_end, available_cutoff, "
    "manifest_artifact_sha256, manifest_artifact_byte_size, manifest_artifact_object_key, row_count, status, "
    "manifest_hash, ingested_at, published_at, created_at, freshness_age_seconds"
)

_CAPABILITY_COLUMNS = (
    "scope_key, schema_version, source_id, provider_id, family_id, capability_id, adapter_id, schema_id, mapping_version, "
    "mapping_hash, unit, reference_population_id, comparable_population_id, required_identifiers_json, state, "
    "latest_snapshot_id, latest_event_at, latest_available_at, checked_at, freshness_age_seconds, reason, "
    "latest_source_partition, latest_source_revision"
)


class PostgreSQLSourceSnapshotStore:
    """The only production source-manifest store; it requires the O2 adapter."""

    def __init__(self, adapter: Any):
        if not hasattr(adapter, "connection"):
            raise ValidationFailureError("source snapshot storage requires the existing PostgreSQL adapter")
        self.adapter = adapter

    @property
    def connection(self) -> Any:
        return self.adapter.connection

    @staticmethod
    def _authorize(principal: Principal, scope: AccessScope) -> None:
        if not isinstance(principal, Principal):
            raise AuthorizationDeniedError("a current server-derived Principal is required")
        if not principal.grants_scope(scope):
            raise ScopeDeniedError("principal is not granted the requested source scope")
        if not principal.has_capability(SOURCE_READ_CAPABILITY):
            raise AuthorizationDeniedError("principal is not currently granted source-read capability")

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self.connection
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except SourceSnapshotConflictError:
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
            if isinstance(exc, (StorageFailureError, ValidationFailureError)):
                raise
            raise StorageFailureError("PostgreSQL source snapshot transaction failed") from exc

    @staticmethod
    def _snapshot_values(draft: SourceSnapshotDraft, ingested_at: datetime) -> tuple[object, ...]:
        binding = draft.binding
        return (
            draft.snapshot_id,
            SOURCE_SNAPSHOT_SCHEMA_VERSION,
            binding.scope_key,
            binding.source_id,
            binding.provider_id,
            binding.family_id,
            binding.capability_id,
            binding.adapter_id,
            binding.schema_id,
            binding.mapping_version,
            binding.mapping_hash,
            binding.unit,
            binding.reference_population_id,
            binding.comparable_population_id,
            _json(binding.required_identifiers),
            draft.source_partition,
            draft.source_revision,
            draft.event_start,
            draft.event_end,
            draft.available_cutoff,
            draft.artifact_reference.content.sha256,
            draft.artifact_reference.content.byte_size,
            f"sha256/{draft.artifact_reference.content.sha256}",
            draft.row_count,
            draft.status.value,
            draft.manifest_hash,
            _timestamp(ingested_at, "ingested_at"),
            draft.freshness_age_seconds,
        )

    @staticmethod
    def _ensure_same(existing: SourceSnapshotRecord, draft: SourceSnapshotDraft) -> None:
        existing_identity = existing.immutable_identity
        draft_identity = draft.immutable_identity()
        if existing.freshness_age_seconds is None:
            # Pre-011 immutable rows have no declared freshness policy. An
            # idempotent replay must not rewrite or conflict with that history.
            existing_identity = existing_identity[:-1]
            draft_identity = draft_identity[:-1]
        if existing_identity != draft_identity:
            raise SourceSnapshotConflictError(draft.snapshot_id)

    @staticmethod
    def _upsert_capability(connection: Any, capability: SourceCapabilityRecord) -> None:
        binding = capability.binding
        connection.execute(
            """
            INSERT INTO source_capability(
                scope_key, schema_version, source_id, provider_id, family_id, capability_id, adapter_id, schema_id,
                mapping_version, mapping_hash, unit, reference_population_id, comparable_population_id,
                required_identifiers_json, state, latest_snapshot_id, latest_event_at, latest_available_at,
                checked_at, freshness_age_seconds, reason, latest_source_partition, latest_source_revision
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (scope_key, source_id, family_id, capability_id) DO UPDATE SET
                provider_id = EXCLUDED.provider_id,
                adapter_id = EXCLUDED.adapter_id,
                schema_id = EXCLUDED.schema_id,
                mapping_version = EXCLUDED.mapping_version,
                mapping_hash = EXCLUDED.mapping_hash,
                unit = EXCLUDED.unit,
                reference_population_id = EXCLUDED.reference_population_id,
                comparable_population_id = EXCLUDED.comparable_population_id,
                required_identifiers_json = EXCLUDED.required_identifiers_json,
                state = EXCLUDED.state,
                latest_snapshot_id = EXCLUDED.latest_snapshot_id,
                latest_event_at = EXCLUDED.latest_event_at,
                latest_available_at = EXCLUDED.latest_available_at,
                checked_at = EXCLUDED.checked_at,
                freshness_age_seconds = EXCLUDED.freshness_age_seconds,
                reason = EXCLUDED.reason,
                latest_source_partition = EXCLUDED.latest_source_partition,
                latest_source_revision = EXCLUDED.latest_source_revision
            WHERE source_capability.latest_available_at IS NULL
               OR EXCLUDED.latest_available_at > source_capability.latest_available_at
               OR (EXCLUDED.latest_available_at = source_capability.latest_available_at
                   AND COALESCE(EXCLUDED.latest_event_at, '-infinity'::timestamptz)
                       >= COALESCE(source_capability.latest_event_at, '-infinity'::timestamptz))
            """,
            (
                binding.scope_key,
                SOURCE_SNAPSHOT_SCHEMA_VERSION,
                binding.source_id,
                binding.provider_id,
                binding.family_id,
                binding.capability_id,
                binding.adapter_id,
                binding.schema_id,
                binding.mapping_version,
                binding.mapping_hash,
                binding.unit,
                binding.reference_population_id,
                binding.comparable_population_id,
                _json(binding.required_identifiers),
                capability.state.value,
                capability.latest_snapshot_id,
                capability.latest_event_at,
                capability.latest_available_at,
                capability.checked_at,
                capability.freshness_age_seconds,
                capability.reason,
                capability.latest_source_partition,
                capability.latest_source_revision,
            ),
        )

    @staticmethod
    def _read_capability(connection: Any, binding: MetrologySourceBinding) -> SourceCapabilityRecord:
        row = connection.execute(
            f"SELECT {_CAPABILITY_COLUMNS} FROM source_capability WHERE scope_key = %s AND source_id = %s AND family_id = %s AND capability_id = %s",
            (binding.scope_key, binding.source_id, binding.family_id, binding.capability_id),
        ).fetchone()
        if row is None:
            raise SourceSnapshotNotFoundError("source capability record is unavailable")
        return _capability_record(row)

    def publish_snapshot(
        self,
        draft: SourceSnapshotDraft,
        ingested_at: datetime,
        capability: SourceCapabilityRecord,
    ) -> tuple[SourceSnapshotRecord, SourceCapabilityRecord]:
        if not isinstance(draft, SourceSnapshotDraft) or not isinstance(capability, SourceCapabilityRecord):
            raise ValidationFailureError("source publication requires a typed draft and capability record")
        if capability.binding != draft.binding or capability.latest_snapshot_id != draft.snapshot_id:
            raise ValidationFailureError("source capability publication does not match the source snapshot")
        values = self._snapshot_values(draft, ingested_at)
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    f"SELECT {_SOURCE_COLUMNS} FROM source_snapshot WHERE snapshot_id = %s FOR UPDATE",
                    (draft.snapshot_id,),
                ).fetchone()
                if row is not None:
                    existing = _source_record(row)
                    self._ensure_same(existing, draft)
                    self._upsert_capability(connection, capability)
                    current_capability = self._read_capability(connection, draft.binding)
                    return existing, current_capability
                row = connection.execute(
                    f"""
                    INSERT INTO source_snapshot(
                        snapshot_id, schema_version, scope_key, source_id, provider_id, family_id, capability_id, adapter_id, schema_id,
                        mapping_version, mapping_hash, unit, reference_population_id, comparable_population_id,
                        required_identifiers_json, source_partition, source_revision, event_start, event_end,
                        available_cutoff, manifest_artifact_sha256, manifest_artifact_byte_size,
                        manifest_artifact_object_key, row_count, status, manifest_hash, ingested_at,
                        freshness_age_seconds
                    ) VALUES ({', '.join(['%s'] * len(values))})
                    RETURNING {_SOURCE_COLUMNS}
                    """,
                    values,
                ).fetchone()
                if row is None:  # pragma: no cover - PostgreSQL RETURNING contract
                    raise StorageFailureError("source snapshot insert returned no row")
                self._upsert_capability(connection, capability)
                current_capability = self._read_capability(connection, draft.binding)
                return _source_record(row), current_capability
        except SourceSnapshotConflictError:
            raise
        except Exception as exc:
            database_error = exc.__cause__ if isinstance(exc, StorageFailureError) else exc
            if getattr(database_error, "sqlstate", None) == "23505":
                # A concurrent publisher may have inserted the same logical
                # identity after the SELECT.  Reconcile without rewriting it.
                row = self.connection.execute(
                    f"SELECT {_SOURCE_COLUMNS} FROM source_snapshot WHERE snapshot_id = %s",
                    (draft.snapshot_id,),
                ).fetchone()
                if row is not None:
                    existing = _source_record(row)
                    self._ensure_same(existing, draft)
                    with self._transaction() as connection:
                        self._upsert_capability(connection, capability)
                        current_capability = self._read_capability(connection, draft.binding)
                    return existing, current_capability
            if isinstance(exc, (StorageFailureError, ValidationFailureError)):
                raise
            raise StorageFailureError("PostgreSQL source snapshot publication failed") from exc

    def get_snapshot(self, principal: Principal, scope: AccessScope, snapshot_id: str) -> SourceSnapshotRecord:
        self._authorize(principal, scope)
        try:
            row = self.connection.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM source_snapshot WHERE scope_key = %s AND snapshot_id = %s",
                (scope.canonical_key, snapshot_id),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("PostgreSQL source snapshot lookup failed") from exc
        if row is None:
            raise SourceSnapshotNotFoundError("source snapshot is unavailable")
        return _source_record(row)

    def get_latest_snapshot_as_of(
        self,
        principal: Principal,
        binding: MetrologySourceBinding,
        knowledge_cutoff: datetime,
    ) -> SourceSnapshotRecord | None:
        """Resolve the exact binding's latest immutable snapshot known by cutoff."""

        self._authorize(principal, binding.scope)
        cutoff = _timestamp(knowledge_cutoff, "knowledge_cutoff")
        try:
            row = self.connection.execute(
                f"""
                SELECT {_SOURCE_COLUMNS}
                FROM source_snapshot
                WHERE scope_key = %s AND source_id = %s AND provider_id = %s
                  AND family_id = %s AND capability_id = %s AND adapter_id = %s
                  AND schema_id = %s AND mapping_version = %s AND mapping_hash = %s
                  AND unit = %s
                  AND reference_population_id IS NOT DISTINCT FROM %s
                  AND comparable_population_id IS NOT DISTINCT FROM %s
                  AND required_identifiers_json = %s::jsonb
                  AND published_at <= %s AND available_cutoff <= %s
                ORDER BY published_at DESC, available_cutoff DESC, event_end DESC,
                         created_at DESC, snapshot_id DESC
                LIMIT 1
                """,
                (
                    binding.scope_key,
                    binding.source_id,
                    binding.provider_id,
                    binding.family_id,
                    binding.capability_id,
                    binding.adapter_id,
                    binding.schema_id,
                    binding.mapping_version,
                    binding.mapping_hash,
                    binding.unit,
                    binding.reference_population_id,
                    binding.comparable_population_id,
                    _json(binding.required_identifiers),
                    cutoff,
                    cutoff,
                ),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("PostgreSQL as-of source snapshot lookup failed") from exc
        if row is None:
            return None
        record = _source_record(row)
        if record.binding != binding:
            raise StorageFailureError("PostgreSQL as-of source snapshot binding identity is invalid")
        return record

    def get_capability(self, principal: Principal, binding: MetrologySourceBinding) -> SourceCapabilityRecord:
        self._authorize(principal, binding.scope)
        try:
            row = self.connection.execute(
                f"SELECT {_CAPABILITY_COLUMNS} FROM source_capability WHERE scope_key = %s AND source_id = %s AND family_id = %s AND capability_id = %s",
                (binding.scope_key, binding.source_id, binding.family_id, binding.capability_id),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("PostgreSQL source capability lookup failed") from exc
        if row is None:
            # Absence is a truthful UNAVAILABLE capability, never READY/zero.
            now = _db_time(self.connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"], "checked_at")
            return SourceCapabilityRecord(
                binding,
                SourceCapabilityState.UNAVAILABLE,
                None,
                None,
                None,
                now,
                1,
                "CAPABILITY_RECORD_MISSING",
            )
        return _capability_record(row)


__all__ = ["PostgreSQLSourceSnapshotStore"]
