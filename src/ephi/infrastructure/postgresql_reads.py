"""PostgreSQL implementation of the generic CHG-129 read substrate.

The adapter uses the existing PostgreSQL connection authority.  Every public
operation is bounded by its own transaction; no cursor or browser request
keeps a transaction open between calls.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from dataclasses import dataclass
import json
import secrets
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from ephi.application.context import AccessScope, Principal, RevisionIdentity, RevisionVector
from ephi.application.errors import (
    AggregateNotFoundError,
    AuthorizationDeniedError,
    CoherentReadConflictError,
    CommandError,
    QueryCursorValidationError,
    QueryIdentityMismatchError,
    QuerySnapshotExpiredError,
    QueryTooBroadError,
    ReadRevisionConflictError,
    ReadRevisionNotFoundError,
    ScopeDeniedError,
    StorageFailureError,
    ValidationFailureError,
)
from ephi.application.hashing import canonical_json, normalize_domain_payload
from ephi.application.read import (
    DEFAULT_SNAPSHOT_TTL_SECONDS,
    MAX_PAGE_SIZE,
    MAX_RETAINED_ROW_COUNT,
    MAX_SNAPSHOT_TTL_SECONDS,
    CurrentReadBundle,
    CurrentReadHead,
    CursorPageToken,
    HistoricalReadBundle,
    PageResult,
    ReadRevision,
    ReadRevisionDraft,
    ReadRevisionIdentity,
    RetainedQuerySnapshot,
    RetainedSnapshotRow,
    VersionedReadRow,
    canonical_query_identity,
)
from ephi.application.storage import AggregateSnapshot

if TYPE_CHECKING:
    from .postgresql import PostgreSQLReferenceTransactionAdapter


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


def _revision_identity(value: object, field: str) -> RevisionIdentity:
    if isinstance(value, bool):
        raise StorageFailureError(f"stored {field} is invalid")
    if isinstance(value, int):
        if value < 0:
            raise StorageFailureError(f"stored {field} is invalid")
        return value
    if isinstance(value, str):
        return _identity(value, field)
    raise StorageFailureError(f"stored {field} is invalid")


def _json_value(value: object, field: str) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageFailureError(f"durable PostgreSQL {field} is not valid JSON") from exc
    try:
        return normalize_domain_payload(parsed)
    except ValidationFailureError as exc:
        raise StorageFailureError(f"durable PostgreSQL {field} is not canonical JSON") from exc


def _json_object(value: object, field: str) -> dict[str, Any]:
    parsed = _json_value(value, field)
    if not isinstance(parsed, dict):
        raise StorageFailureError(f"durable PostgreSQL {field} is not an object")
    return parsed


def _scope_from_key(scope_key: object) -> AccessScope:
    if not isinstance(scope_key, str):
        raise StorageFailureError("stored scope key is not a string")
    try:
        value = json.loads(scope_key)
    except json.JSONDecodeError as exc:
        raise StorageFailureError("stored scope key is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"scope_id", "site_id", "area_id", "family_id", "project_ids"}:
        raise StorageFailureError("stored scope key has an invalid schema")
    try:
        return AccessScope(
            value["scope_id"],
            site_id=value["site_id"],
            area_id=value["area_id"],
            family_id=value["family_id"],
            project_ids=tuple(value["project_ids"]),
        )
    except (TypeError, ValueError) as exc:
        raise StorageFailureError("stored scope key is not a valid AccessScope") from exc


def _revision_vector(value: object) -> RevisionVector:
    parsed = _json_object(value, "revision vector")
    required = {
        "analysis_revision",
        "exposure_revision",
        "priority_revision",
        "workflow_version",
        "plan_version",
        "qualification_manifest_id",
    }
    if set(parsed) != required:
        raise StorageFailureError("stored revision vector omits or adds a field")
    try:
        return RevisionVector(
            parsed["analysis_revision"],
            parsed["exposure_revision"],
            parsed["priority_revision"],
            parsed["workflow_version"],
            parsed["plan_version"],
            parsed["qualification_manifest_id"],
        )
    except (TypeError, ValueError) as exc:
        raise StorageFailureError("stored revision vector failed validation") from exc


def _stored_aggregate(row: Mapping[str, Any], *, prefix: str = "") -> AggregateSnapshot:
    state_column = f"{prefix}workflow_state_json" if prefix else "workflow_state_json"
    type_column = f"{prefix}workflow_aggregate_type" if prefix else "workflow_aggregate_type"
    id_column = f"{prefix}workflow_aggregate_id" if prefix else "workflow_aggregate_id"
    version_column = f"{prefix}workflow_version" if prefix else "workflow_version"
    return AggregateSnapshot(
        row["scope_key"],
        row[type_column],
        row[id_column],
        int(row[version_column]),
        _json_object(row[state_column], "historical workflow state"),
    )


def _current_aggregate(row: Mapping[str, Any]) -> AggregateSnapshot:
    return AggregateSnapshot(
        row["scope_key"],
        row["aggregate_type"],
        row["aggregate_id"],
        int(row["version"]),
        _json_object(row["state_json"], "workflow aggregate state"),
    )


@dataclass(frozen=True, slots=True)
class _StoredQuerySnapshot:
    """Public snapshot metadata plus server-only cursor verification material."""

    public: RetainedQuerySnapshot
    server_binding: str


class PostgreSQLReadSnapshotStore:
    """Durable coherent-read and retained-query implementation."""

    def __init__(self, adapter: PostgreSQLReferenceTransactionAdapter):
        if not hasattr(adapter, "connection"):
            raise TypeError("adapter must provide the existing PostgreSQL connection authority")
        self.adapter = adapter

    @property
    def connection(self) -> Any:
        return self.adapter.connection

    @contextmanager
    def _transaction(self, *, repeatable_read: bool = False, read_only: bool = False):
        begin = "BEGIN"
        if repeatable_read:
            begin = "BEGIN ISOLATION LEVEL REPEATABLE READ"
        if read_only:
            begin += " READ ONLY"
        connection = self.connection
        connection.execute(begin)
        try:
            yield connection
        except BaseException:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        else:
            try:
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise

    @staticmethod
    def _authorize(principal: Principal, scope: AccessScope, required_capability: str) -> None:
        if not isinstance(principal, Principal):
            raise ValidationFailureError("principal must be a server-derived Principal")
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        required_capability = _identity(required_capability, "required_read_capability")
        if not principal.grants_scope(scope):
            raise ScopeDeniedError("principal is not granted the requested product scope")
        if not principal.has_capability(required_capability):
            raise AuthorizationDeniedError("principal is not currently granted the required read capability")

    @staticmethod
    def _validate_expected_head(expected_head_version: int | None, expected_revision_id: str | None) -> int | None:
        if expected_head_version is not None and (
            isinstance(expected_head_version, bool) or not isinstance(expected_head_version, int) or expected_head_version < 0
        ):
            raise ValidationFailureError("expected_head_version must be a non-negative integer or None")
        if expected_revision_id is not None:
            _identity(expected_revision_id, "expected_revision_id")
            if expected_head_version is None or expected_head_version == 0:
                raise ValidationFailureError("expected_revision_id requires an existing expected head version")
        return expected_head_version

    def _insert_and_publish(
        self,
        connection: Any,
        revision: ReadRevisionDraft,
        *,
        expected_head_version: int | None,
        expected_revision_id: str | None,
    ) -> CurrentReadHead:
        scope_key = revision.identity.scope.canonical_key
        vector_json = canonical_json(revision.revision_vector.as_dict())
        payload_json = canonical_json(revision.payload)
        workflow = revision.workflow_aggregate
        try:
            connection.execute(
                """
                INSERT INTO read_revision(
                    revision_id, scope_key, entity_type, entity_id,
                    revision_vector_json, payload_json, known_at, published_at,
                    workflow_aggregate_type, workflow_aggregate_id,
                    workflow_version, workflow_state_json
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb,
                          clock_timestamp(), clock_timestamp(), %s, %s, %s, %s::jsonb)
                """,
                (
                    revision.identity.revision_id,
                    scope_key,
                    revision.identity.entity_type,
                    revision.identity.entity_id,
                    vector_json,
                    payload_json,
                    workflow.aggregate_type,
                    workflow.aggregate_id,
                    workflow.version,
                    canonical_json(workflow.state),
                ),
            )
        except Exception as exc:
            constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
            if getattr(exc, "sqlstate", None) == "23505" and constraint in {
                "read_revision_pkey",
                "read_revision_entity_identity_unique",
            }:
                raise ReadRevisionConflictError(
                    "immutable read revision identity is already stored and cannot be overwritten"
                ) from exc
            raise

        if expected_head_version is None or expected_head_version == 0:
            try:
                row = connection.execute(
                    """
                    INSERT INTO read_head(scope_key, entity_type, entity_id, revision_id, head_version, published_at)
                    VALUES (%s, %s, %s, %s, 1, clock_timestamp())
                    RETURNING scope_key, entity_type, entity_id, revision_id, head_version, published_at
                    """,
                    (
                        scope_key,
                        revision.identity.entity_type,
                        revision.identity.entity_id,
                        revision.identity.revision_id,
                    ),
                ).fetchone()
            except Exception as exc:
                constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
                if getattr(exc, "sqlstate", None) == "23505" and constraint == "read_head_pkey":
                    raise ReadRevisionConflictError("current read head was advanced by another publisher") from exc
                raise
        else:
            row = connection.execute(
                """
                UPDATE read_head
                SET revision_id = %s, head_version = head_version + 1, published_at = clock_timestamp()
                WHERE scope_key = %s AND entity_type = %s AND entity_id = %s
                  AND head_version = %s
                  AND (%s::text IS NULL OR revision_id = %s)
                RETURNING scope_key, entity_type, entity_id, revision_id, head_version, published_at
                """,
                (
                    revision.identity.revision_id,
                    scope_key,
                    revision.identity.entity_type,
                    revision.identity.entity_id,
                    expected_head_version,
                    expected_revision_id,
                    expected_revision_id,
                ),
            ).fetchone()
            if row is None:
                raise ReadRevisionConflictError("current read head is stale; recompute and retry publication")
        if row is None:  # pragma: no cover - PostgreSQL RETURNING contract
            raise StorageFailureError("PostgreSQL read-head publication returned no head")
        return CurrentReadHead(
            _scope_from_key(row["scope_key"]),
            row["entity_type"],
            row["entity_id"],
            row["revision_id"],
            int(row["head_version"]),
            row["published_at"],
        )

    def publish_read_revision(
        self,
        revision: ReadRevisionDraft,
        *,
        expected_head_version: int | None = None,
        expected_revision_id: str | None = None,
    ) -> CurrentReadHead:
        if not isinstance(revision, ReadRevisionDraft):
            raise ValidationFailureError("revision must be a ReadRevisionDraft")
        expected_head_version = self._validate_expected_head(expected_head_version, expected_revision_id)
        try:
            with self._transaction() as connection:
                return self._insert_and_publish(
                    connection,
                    revision,
                    expected_head_version=expected_head_version,
                    expected_revision_id=expected_revision_id,
                )
        except CommandError:
            raise
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL read-revision publication failed") from exc

    def publish_current_revision(
        self,
        scope: AccessScope,
        entity_type: str,
        entity_id: str,
        revision_id: str,
        revision_vector: RevisionVector,
        payload: Mapping[str, object],
        workflow_aggregate: AggregateSnapshot,
        *,
        expected_head_version: int | None = None,
        expected_revision_id: str | None = None,
    ) -> CurrentReadHead:
        draft = ReadRevisionDraft(
            ReadRevisionIdentity(revision_id, scope, entity_type, entity_id),
            revision_vector,
            dict(payload),
            workflow_aggregate,
        )
        return self.publish_read_revision(
            draft,
            expected_head_version=expected_head_version,
            expected_revision_id=expected_revision_id,
        )

    def publish_current_revision_in_transaction(
        self,
        connection: Any,
        revision: ReadRevisionDraft,
        *,
        expected_head_version: int | None = None,
        expected_revision_id: str | None = None,
    ) -> CurrentReadHead:
        """Publish into a caller-owned short transaction.

        This is the composition point for an existing durable workflow
        command transaction.  It only inserts the immutable revision and
        advances the read head; it never updates ``aggregate_state``.
        """

        if connection is not self.connection:
            raise ValidationFailureError("read publication must use the existing adapter connection")
        if not isinstance(revision, ReadRevisionDraft):
            raise ValidationFailureError("revision must be a ReadRevisionDraft")
        expected_head_version = self._validate_expected_head(expected_head_version, expected_revision_id)
        return self._insert_and_publish(
            connection,
            revision,
            expected_head_version=expected_head_version,
            expected_revision_id=expected_revision_id,
        )

    def get_current_head(self, scope: AccessScope, entity_type: str, entity_id: str) -> CurrentReadHead | None:
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        entity_type = _identity(entity_type, "entity_type")
        entity_id = _identity(entity_id, "entity_id")
        try:
            row = self.connection.execute(
                "SELECT scope_key, entity_type, entity_id, revision_id, head_version, published_at FROM read_head WHERE scope_key = %s AND entity_type = %s AND entity_id = %s",
                (scope.canonical_key, entity_type, entity_id),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while reading the current head") from exc
        if row is None:
            return None
        return CurrentReadHead(
            scope,
            row["entity_type"],
            row["entity_id"],
            row["revision_id"],
            int(row["head_version"]),
            row["published_at"],
        )

    def _read_revision_from_row(self, row: Mapping[str, Any]) -> ReadRevision:
        scope = _scope_from_key(row["scope_key"])
        identity = ReadRevisionIdentity(row["revision_id"], scope, row["entity_type"], row["entity_id"])
        vector = _revision_vector(row["revision_vector_json"])
        workflow = _stored_aggregate(row)
        try:
            return ReadRevision(
                identity,
                vector,
                _json_object(row["payload_json"], "read revision payload"),
                row["known_at"],
                row["published_at"],
                workflow,
            )
        except ValidationFailureError as exc:
            raise StorageFailureError("stored read revision failed validation") from exc

    def read_current_bundle(
        self,
        principal: Principal,
        scope: AccessScope,
        entity_type: str,
        entity_id: str,
        required_read_capability: str,
        *,
        _after_revision_read: Callable[[], None] | None = None,
    ) -> CurrentReadBundle:
        self._authorize(principal, scope, required_read_capability)
        entity_type = _identity(entity_type, "entity_type")
        entity_id = _identity(entity_id, "entity_id")
        try:
            with self._transaction(repeatable_read=True, read_only=True) as connection:
                row = connection.execute(
                    """
                    SELECT r.revision_id, r.scope_key, r.entity_type, r.entity_id,
                           r.revision_vector_json, r.payload_json, r.known_at, r.published_at,
                           r.workflow_aggregate_type, r.workflow_aggregate_id,
                           r.workflow_version, r.workflow_state_json
                    FROM read_head AS h
                    JOIN read_revision AS r ON r.revision_id = h.revision_id
                    WHERE h.scope_key = %s AND h.entity_type = %s AND h.entity_id = %s
                    """,
                    (scope.canonical_key, entity_type, entity_id),
                ).fetchone()
                if row is None:
                    raise ReadRevisionNotFoundError("current read head is not available")
                revision = self._read_revision_from_row(row)
                if revision.scope.canonical_key != scope.canonical_key:
                    raise ScopeDeniedError("current read head is outside the requested scope")
                if revision.entity_type != entity_type or revision.entity_id != entity_id:
                    raise CoherentReadConflictError("current read head points to a different entity revision")
                if _after_revision_read is not None:
                    _after_revision_read()
                aggregate_row = connection.execute(
                    "SELECT scope_key, aggregate_type, aggregate_id, version, state_json FROM aggregate_state WHERE scope_key = %s AND aggregate_type = %s AND aggregate_id = %s",
                    (
                        scope.canonical_key,
                        revision.workflow_aggregate.aggregate_type,
                        revision.workflow_aggregate.aggregate_id,
                    ),
                ).fetchone()
                if aggregate_row is None:
                    raise AggregateNotFoundError("current workflow aggregate is not available")
                aggregate = _current_aggregate(aggregate_row)
                if (
                    aggregate.scope_key != revision.scope.canonical_key
                    or aggregate.aggregate_type != revision.workflow_aggregate.aggregate_type
                    or aggregate.aggregate_id != revision.workflow_aggregate.aggregate_id
                ):
                    raise CoherentReadConflictError(
                        "current read head and workflow aggregate identities are inconsistent"
                    )
                stored_vector = revision.revision_vector
                effective_vector = RevisionVector(
                    stored_vector.analysis_revision,
                    stored_vector.exposure_revision,
                    stored_vector.priority_revision,
                    aggregate.version,
                    stored_vector.plan_version,
                    stored_vector.qualification_manifest_id,
                )
                return CurrentReadBundle(revision, aggregate, effective_vector)
        except CommandError:
            raise
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL coherent current read failed") from exc

    def get_read_revision(self, revision_id: str) -> ReadRevision | None:
        revision_id = _identity(revision_id, "revision_id")
        try:
            row = self.connection.execute(
                "SELECT revision_id, scope_key, entity_type, entity_id, revision_vector_json, payload_json, known_at, published_at, workflow_aggregate_type, workflow_aggregate_id, workflow_version, workflow_state_json FROM read_revision WHERE revision_id = %s",
                (revision_id,),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while reading a revision") from exc
        return self._read_revision_from_row(row) if row is not None else None

    def read_historical_bundle(
        self,
        principal: Principal,
        scope: AccessScope,
        revision_id: str,
        required_read_capability: str,
    ) -> HistoricalReadBundle:
        self._authorize(principal, scope, required_read_capability)
        revision_id = _identity(revision_id, "revision_id")
        try:
            with self._transaction(repeatable_read=True, read_only=True) as connection:
                row = connection.execute(
                    "SELECT revision_id, scope_key, entity_type, entity_id, revision_vector_json, payload_json, known_at, published_at, workflow_aggregate_type, workflow_aggregate_id, workflow_version, workflow_state_json FROM read_revision WHERE revision_id = %s",
                    (revision_id,),
                ).fetchone()
                if row is None:
                    raise ReadRevisionNotFoundError("historical read revision is not available")
                revision = self._read_revision_from_row(row)
                if revision.scope.canonical_key != scope.canonical_key:
                    raise ScopeDeniedError("historical read revision is outside the requested scope")
                return HistoricalReadBundle(revision, revision.workflow_aggregate, revision.revision_vector)
        except CommandError:
            raise
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL historical read failed") from exc

    @staticmethod
    def _validate_ttl(ttl_seconds: int) -> int:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValidationFailureError("ttl_seconds must be a positive integer")
        if ttl_seconds > MAX_SNAPSHOT_TTL_SECONDS:
            raise ValidationFailureError("ttl_seconds exceeds the bounded snapshot lifetime")
        return ttl_seconds

    def _snapshot_from_row(self, row: Mapping[str, Any], scope: AccessScope) -> _StoredQuerySnapshot:
        if row["scope_key"] != scope.canonical_key:
            raise ScopeDeniedError("retained query snapshot is outside the requested scope")
        security_revision_value = _json_value(row["security_revision_json"], "snapshot security revision")
        security_revision = _revision_identity(security_revision_value, "security_revision")
        try:
            public = RetainedQuerySnapshot(
                row["snapshot_id"],
                row["query_identity_hash"],
                scope,
                row["subject"],
                security_revision,
                row["required_read_capability"],
                row["created_at"],
                row["expires_at"],
                int(row["total_row_count"]),
            )
            server_binding = row["token_binding"]
            if not isinstance(server_binding, str) or len(server_binding) != 64 or any(
                character not in "0123456789abcdef" for character in server_binding
            ):
                raise StorageFailureError("stored query snapshot cursor binding failed validation")
            return _StoredQuerySnapshot(public, server_binding)
        except ValidationFailureError as exc:
            raise StorageFailureError("stored query snapshot failed validation") from exc

    def create_query_snapshot(
        self,
        principal: Principal,
        scope: AccessScope,
        query_identity: Mapping[str, object],
        required_read_capability: str,
        rows: Sequence[VersionedReadRow],
        *,
        ttl_seconds: int = DEFAULT_SNAPSHOT_TTL_SECONDS,
    ) -> RetainedQuerySnapshot:
        self._authorize(principal, scope, required_read_capability)
        required_read_capability = _identity(required_read_capability, "required_read_capability")
        _, query_hash = canonical_query_identity(query_identity)
        ttl_seconds = self._validate_ttl(ttl_seconds)
        if not isinstance(rows, (tuple, list)):
            raise ValidationFailureError("rows must be a bounded tuple or list of VersionedReadRow values")
        rows = tuple(rows)
        if len(rows) > MAX_RETAINED_ROW_COUNT:
            raise QueryTooBroadError(limit=MAX_RETAINED_ROW_COUNT)
        if any(not isinstance(row, VersionedReadRow) for row in rows):
            raise ValidationFailureError("rows must contain only VersionedReadRow values")
        if len({row.row_id for row in rows}) != len(rows):
            raise ValidationFailureError("retained snapshot row identities must be unique")

        snapshot_id = uuid4().hex
        token_binding = secrets.token_hex(32)
        security_json = canonical_json(principal.security_revision)
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    INSERT INTO query_snapshot(
                        snapshot_id, query_identity_hash, scope_key, subject,
                        security_revision_json, required_read_capability, token_binding,
                        created_at, expires_at, total_row_count
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s,
                              clock_timestamp(), clock_timestamp() + (%s * INTERVAL '1 second'), %s)
                    RETURNING snapshot_id, query_identity_hash, scope_key, subject,
                              security_revision_json, required_read_capability, token_binding,
                              created_at, expires_at, total_row_count
                    """,
                    (
                        snapshot_id,
                        query_hash,
                        scope.canonical_key,
                        principal.subject,
                        security_json,
                        required_read_capability,
                        token_binding,
                        ttl_seconds,
                        len(rows),
                    ),
                ).fetchone()
                if row is None:  # pragma: no cover - PostgreSQL RETURNING contract
                    raise StorageFailureError("PostgreSQL query snapshot creation returned no metadata")
                with connection.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO query_snapshot_row(snapshot_id, ordinal, row_id, row_version_json, payload_json) VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)",
                        (
                            (
                                snapshot_id,
                                ordinal,
                                retained.row_id,
                                canonical_json(retained.row_version),
                                canonical_json(retained.payload),
                            )
                            for ordinal, retained in enumerate(rows, start=1)
                        ),
                    )
                return self._snapshot_from_row(row, scope).public
        except CommandError:
            raise
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL query snapshot creation failed") from exc

    @staticmethod
    def _authorize_snapshot(
        principal: Principal,
        scope: AccessScope,
        required_read_capability: str,
        snapshot: RetainedQuerySnapshot,
    ) -> None:
        if snapshot.subject != principal.subject:
            raise AuthorizationDeniedError("current principal is not the retained snapshot subject")
        if snapshot.scope.canonical_key != scope.canonical_key or not principal.grants_scope(scope):
            raise ScopeDeniedError("current principal is not granted the retained snapshot scope")
        if required_read_capability != snapshot.required_read_capability:
            raise QueryIdentityMismatchError("required read capability does not match the retained snapshot")
        if not principal.has_capability(snapshot.required_read_capability):
            raise AuthorizationDeniedError("current principal no longer has the retained read capability")
        if principal.security_revision != snapshot.security_revision:
            raise QuerySnapshotExpiredError(reason="effective_security_revision_changed")

    def read_query_snapshot_page(
        self,
        principal: Principal,
        scope: AccessScope,
        snapshot_id: str,
        query_identity: Mapping[str, object],
        required_read_capability: str,
        *,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> PageResult:
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        snapshot_id = _identity(snapshot_id, "snapshot_id")
        required_read_capability = _identity(required_read_capability, "required_read_capability")
        _, query_hash = canonical_query_identity(query_identity)
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
            raise ValidationFailureError("page_size must be a positive integer")
        if page_size > MAX_PAGE_SIZE:
            raise QueryTooBroadError("page_size exceeds the bounded page size", limit=MAX_PAGE_SIZE)
        decoded_cursor = CursorPageToken.decode(cursor) if cursor is not None else None
        try:
            with self._transaction(repeatable_read=True, read_only=True) as connection:
                row = connection.execute(
                    "SELECT snapshot_id, query_identity_hash, scope_key, subject, security_revision_json, required_read_capability, token_binding, created_at, expires_at, total_row_count, clock_timestamp() AS database_now FROM query_snapshot WHERE snapshot_id = %s",
                    (snapshot_id,),
                ).fetchone()
                if row is None:
                    raise QuerySnapshotExpiredError(reason="retained_snapshot_missing")
                stored_snapshot = self._snapshot_from_row(row, scope)
                snapshot = stored_snapshot.public
                database_now = row["database_now"]
                if not isinstance(database_now, datetime) or snapshot.expires_at <= database_now:
                    raise QuerySnapshotExpiredError(reason="retained_snapshot_expired")
                self._authorize_snapshot(principal, scope, required_read_capability, snapshot)
                if query_hash != snapshot.query_identity_hash:
                    raise QueryIdentityMismatchError("query/filter/sort identity does not match the retained snapshot")
                if decoded_cursor is not None:
                    decoded_cursor.verify(snapshot, server_binding=stored_snapshot.server_binding)
                    start_ordinal = decoded_cursor.next_ordinal
                else:
                    if snapshot.total_row_count == 0:
                        return PageResult(snapshot, tuple(), None)
                    start_ordinal = 1
                if start_ordinal <= 0 or start_ordinal > snapshot.total_row_count:
                    raise QueryCursorValidationError("cursor ordinal is outside the retained snapshot")
                expected_count = min(page_size, snapshot.total_row_count - start_ordinal + 1)
                rows = connection.execute(
                    "SELECT snapshot_id, ordinal, row_id, row_version_json, payload_json FROM query_snapshot_row WHERE snapshot_id = %s AND ordinal >= %s AND ordinal < %s ORDER BY ordinal ASC",
                    (snapshot_id, start_ordinal, start_ordinal + expected_count),
                ).fetchall()
                if len(rows) != expected_count:
                    raise QuerySnapshotExpiredError(reason="retained_snapshot_member_missing")
                retained_rows = []
                for expected_ordinal, stored in enumerate(rows, start=start_ordinal):
                    if int(stored["ordinal"]) != expected_ordinal:
                        raise QuerySnapshotExpiredError(reason="retained_snapshot_order_missing")
                    try:
                        retained_rows.append(
                            RetainedSnapshotRow(
                                stored["snapshot_id"],
                                int(stored["ordinal"]),
                                stored["row_id"],
                                _revision_identity(_json_value(stored["row_version_json"], "row version"), "row_version"),
                                _json_object(stored["payload_json"], "retained row payload"),
                            )
                        )
                    except ValidationFailureError as exc:
                        raise QuerySnapshotExpiredError(reason="retained_snapshot_member_invalid") from exc
                next_cursor = None
                next_ordinal = start_ordinal + len(retained_rows)
                if next_ordinal <= snapshot.total_row_count:
                    next_cursor = CursorPageToken.create(
                        snapshot,
                        next_ordinal,
                        server_binding=stored_snapshot.server_binding,
                    ).encode()
                return PageResult(snapshot, tuple(retained_rows), next_cursor)
        except CommandError:
            raise
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL retained query page read failed") from exc

    # Names used by the design vocabulary and by future O3 adapters.
    create_retained_query_snapshot = create_query_snapshot
    read_retained_query_snapshot_page = read_query_snapshot_page
    read_historical_revision = read_historical_bundle
