"""Storage-neutral contracts for coherent reads and retained query pages.

This module contains no product query, Attention, Episode, UI, or company
identity behavior.  It defines the small immutable values exchanged by a
future read service and a durable storage adapter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import base64
import binascii
import hashlib
from hmac import compare_digest
import json
from typing import Any, Protocol, runtime_checkable

from .context import AccessScope, Principal, RevisionIdentity, RevisionVector
from .errors import QueryCursorValidationError, ValidationFailureError
from .hashing import canonical_json, normalize_domain_payload
from .storage import AggregateSnapshot


MAX_PAGE_SIZE = 100
MAX_RETAINED_ROW_COUNT = 1000
DEFAULT_SNAPSHOT_TTL_SECONDS = 300
MAX_SNAPSHOT_TTL_SECONDS = 3600


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


def _revision_identity(value: object, field: str) -> RevisionIdentity:
    if isinstance(value, bool):
        raise ValidationFailureError(f"{field} must be a non-negative integer or canonical string")
    if isinstance(value, int):
        if value < 0:
            raise ValidationFailureError(f"{field} must be non-negative")
        return value
    if isinstance(value, str):
        return _identity(value, field)
    raise ValidationFailureError(f"{field} must be a non-negative integer or canonical string")


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailureError(f"{field} must be timezone-aware")
    return value


def _mapping_payload(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationFailureError(f"{field} must be a mapping")
    normalized = normalize_domain_payload(value)
    if not isinstance(normalized, dict):  # pragma: no cover - guarded by Mapping
        raise ValidationFailureError(f"{field} must normalize to an object")
    return normalized


@dataclass(frozen=True, slots=True)
class ReadRevisionIdentity:
    """The immutable identity of one versioned entity read."""

    revision_id: str
    scope: AccessScope
    entity_type: str
    entity_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision_id", _identity(self.revision_id, "revision_id"))
        if not isinstance(self.scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        object.__setattr__(self, "entity_type", _identity(self.entity_type, "entity_type"))
        object.__setattr__(self, "entity_id", _identity(self.entity_id, "entity_id"))


@dataclass(frozen=True, slots=True)
class ReadRevisionDraft:
    """Validated revision data before PostgreSQL assigns server timestamps."""

    identity: ReadRevisionIdentity
    revision_vector: RevisionVector
    payload: dict[str, Any]
    workflow_aggregate: AggregateSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ReadRevisionIdentity):
            raise ValidationFailureError("identity must be a ReadRevisionIdentity")
        if not isinstance(self.revision_vector, RevisionVector):
            raise ValidationFailureError("revision_vector must be a RevisionVector")
        object.__setattr__(self, "payload", _mapping_payload(self.payload, "payload"))
        if not isinstance(self.workflow_aggregate, AggregateSnapshot):
            raise ValidationFailureError("workflow_aggregate must be an AggregateSnapshot")
        if self.workflow_aggregate.scope_key != self.identity.scope.canonical_key:
            raise ValidationFailureError("workflow aggregate scope does not match read revision scope")
        if self.workflow_aggregate.version != self.revision_vector.workflow_version:
            raise ValidationFailureError("workflow aggregate version must match revision_vector.workflow_version")


@dataclass(frozen=True, slots=True)
class ReadRevision:
    """An immutable stored read revision with its historical workflow state."""

    identity: ReadRevisionIdentity
    revision_vector: RevisionVector
    payload: dict[str, Any]
    known_at: datetime
    published_at: datetime
    workflow_aggregate: AggregateSnapshot

    def __post_init__(self) -> None:
        draft = ReadRevisionDraft(self.identity, self.revision_vector, self.payload, self.workflow_aggregate)
        object.__setattr__(self, "payload", draft.payload)
        object.__setattr__(self, "known_at", _aware_datetime(self.known_at, "known_at"))
        object.__setattr__(self, "published_at", _aware_datetime(self.published_at, "published_at"))

    @property
    def revision_id(self) -> str:
        return self.identity.revision_id

    @property
    def scope(self) -> AccessScope:
        return self.identity.scope

    @property
    def entity_type(self) -> str:
        return self.identity.entity_type

    @property
    def entity_id(self) -> str:
        return self.identity.entity_id

    @property
    def workflow_snapshot(self) -> AggregateSnapshot:
        return self.workflow_aggregate


@dataclass(frozen=True, slots=True)
class CurrentReadHead:
    scope: AccessScope
    entity_type: str
    entity_id: str
    revision_id: str
    head_version: int
    published_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        object.__setattr__(self, "entity_type", _identity(self.entity_type, "entity_type"))
        object.__setattr__(self, "entity_id", _identity(self.entity_id, "entity_id"))
        object.__setattr__(self, "revision_id", _identity(self.revision_id, "revision_id"))
        if isinstance(self.head_version, bool) or not isinstance(self.head_version, int) or self.head_version <= 0:
            raise ValidationFailureError("head_version must be a positive integer")
        object.__setattr__(self, "published_at", _aware_datetime(self.published_at, "published_at"))


@dataclass(frozen=True, slots=True)
class CurrentReadBundle:
    read_revision: ReadRevision
    workflow_aggregate: AggregateSnapshot
    revision_vector: RevisionVector

    def __post_init__(self) -> None:
        if not isinstance(self.read_revision, ReadRevision):
            raise ValidationFailureError("read_revision must be a ReadRevision")
        if not isinstance(self.workflow_aggregate, AggregateSnapshot):
            raise ValidationFailureError("workflow_aggregate must be an AggregateSnapshot")
        if not isinstance(self.revision_vector, RevisionVector):
            raise ValidationFailureError("revision_vector must be a RevisionVector")
        if self.workflow_aggregate.scope_key != self.read_revision.scope.canonical_key:
            raise ValidationFailureError("bundle workflow aggregate scope must match read_revision scope")
        if (
            self.workflow_aggregate.aggregate_type != self.read_revision.workflow_aggregate.aggregate_type
            or self.workflow_aggregate.aggregate_id != self.read_revision.workflow_aggregate.aggregate_id
        ):
            raise ValidationFailureError("bundle workflow aggregate identity must match read_revision")
        stored_vector = self.read_revision.revision_vector
        effective_vector = RevisionVector(
            stored_vector.analysis_revision,
            stored_vector.exposure_revision,
            stored_vector.priority_revision,
            self.workflow_aggregate.version,
            stored_vector.plan_version,
            stored_vector.qualification_manifest_id,
        )
        if self.revision_vector != effective_vector:
            raise ValidationFailureError("bundle revision_vector is inconsistent with the current workflow aggregate")


@dataclass(frozen=True, slots=True)
class HistoricalReadBundle:
    read_revision: ReadRevision
    workflow_aggregate: AggregateSnapshot
    revision_vector: RevisionVector

    def __post_init__(self) -> None:
        CurrentReadBundle(self.read_revision, self.workflow_aggregate, self.revision_vector)
        if self.workflow_aggregate != self.read_revision.workflow_aggregate:
            raise ValidationFailureError("historical bundle workflow aggregate must match read_revision")
        if self.revision_vector != self.read_revision.revision_vector:
            raise ValidationFailureError("historical bundle revision_vector must match read_revision")


@dataclass(frozen=True, slots=True)
class VersionedReadRow:
    """One already-authorized ordered source row supplied to snapshot creation."""

    row_id: str
    row_version: RevisionIdentity
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "row_id", _identity(self.row_id, "row_id"))
        object.__setattr__(self, "row_version", _revision_identity(self.row_version, "row_version"))
        object.__setattr__(self, "payload", _mapping_payload(self.payload, "row payload"))


@dataclass(frozen=True, slots=True)
class RetainedSnapshotRow:
    snapshot_id: str
    ordinal: int
    row_id: str
    row_version: RevisionIdentity
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", _identity(self.snapshot_id, "snapshot_id"))
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal <= 0:
            raise ValidationFailureError("ordinal must be a positive integer")
        object.__setattr__(self, "row_id", _identity(self.row_id, "row_id"))
        object.__setattr__(self, "row_version", _revision_identity(self.row_version, "row_version"))
        object.__setattr__(self, "payload", _mapping_payload(self.payload, "row payload"))


@dataclass(frozen=True, slots=True)
class RetainedQuerySnapshot:
    snapshot_id: str
    query_identity_hash: str
    scope: AccessScope
    subject: str
    security_revision: RevisionIdentity
    required_read_capability: str
    created_at: datetime
    expires_at: datetime
    total_row_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", _identity(self.snapshot_id, "snapshot_id"))
        if not isinstance(self.query_identity_hash, str) or len(self.query_identity_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.query_identity_hash
        ):
            raise ValidationFailureError("query_identity_hash must be a lowercase SHA-256 digest")
        if not isinstance(self.scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        object.__setattr__(self, "subject", _identity(self.subject, "subject"))
        object.__setattr__(self, "security_revision", _revision_identity(self.security_revision, "security_revision"))
        object.__setattr__(self, "required_read_capability", _identity(self.required_read_capability, "required_read_capability"))
        object.__setattr__(self, "created_at", _aware_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "expires_at", _aware_datetime(self.expires_at, "expires_at"))
        if isinstance(self.total_row_count, bool) or not isinstance(self.total_row_count, int) or self.total_row_count < 0:
            raise ValidationFailureError("total_row_count must be a non-negative integer")
        if self.total_row_count > MAX_RETAINED_ROW_COUNT:
            raise ValidationFailureError("total_row_count exceeds MAX_RETAINED_ROW_COUNT")


def canonical_query_identity(query_identity: Mapping[str, object]) -> tuple[dict[str, Any], str]:
    """Normalize and hash the complete query/filter/sort identity."""

    normalized = _mapping_payload(query_identity, "query_identity")
    return normalized, hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def _cursor_digest(snapshot_id: str, query_identity_hash: str, next_ordinal: int, binding: str) -> str:
    envelope = {
        "snapshot_id": snapshot_id,
        "query_identity_hash": query_identity_hash,
        "next_ordinal": next_ordinal,
        "snapshot_binding": binding,
    }
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CursorPageToken:
    snapshot_id: str
    query_identity_hash: str
    next_ordinal: int
    integrity: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", _identity(self.snapshot_id, "snapshot_id"))
        if not isinstance(self.query_identity_hash, str) or len(self.query_identity_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.query_identity_hash
        ):
            raise QueryCursorValidationError("cursor query identity is malformed")
        if isinstance(self.next_ordinal, bool) or not isinstance(self.next_ordinal, int) or self.next_ordinal <= 0:
            raise QueryCursorValidationError("cursor ordinal is invalid")
        if not isinstance(self.integrity, str) or len(self.integrity) != 64 or any(
            character not in "0123456789abcdef" for character in self.integrity
        ):
            raise QueryCursorValidationError("cursor integrity is malformed")

    @classmethod
    def create(
        cls,
        snapshot: RetainedQuerySnapshot,
        next_ordinal: int,
        *,
        server_binding: str,
    ) -> "CursorPageToken":
        if isinstance(next_ordinal, bool) or not isinstance(next_ordinal, int) or next_ordinal <= 0:
            raise QueryCursorValidationError("cursor ordinal is invalid")
        if not isinstance(server_binding, str) or len(server_binding) != 64 or any(
            character not in "0123456789abcdef" for character in server_binding
        ):
            raise QueryCursorValidationError("cursor server binding is invalid")
        return cls(
            snapshot.snapshot_id,
            snapshot.query_identity_hash,
            next_ordinal,
            _cursor_digest(snapshot.snapshot_id, snapshot.query_identity_hash, next_ordinal, server_binding),
        )

    def verify(self, snapshot: RetainedQuerySnapshot, *, server_binding: str) -> None:
        if self.snapshot_id != snapshot.snapshot_id or self.query_identity_hash != snapshot.query_identity_hash:
            raise QueryCursorValidationError("cursor does not belong to the requested query snapshot")
        if not isinstance(server_binding, str) or len(server_binding) != 64 or any(
            character not in "0123456789abcdef" for character in server_binding
        ):
            raise QueryCursorValidationError("cursor server binding is invalid")
        expected = _cursor_digest(self.snapshot_id, self.query_identity_hash, self.next_ordinal, server_binding)
        if not compare_digest(self.integrity, expected):
            raise QueryCursorValidationError("cursor integrity validation failed")

    def encode(self) -> str:
        raw = canonical_json(
            {
                "integrity": self.integrity,
                "next_ordinal": self.next_ordinal,
                "query_identity_hash": self.query_identity_hash,
                "snapshot_id": self.snapshot_id,
            }
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> "CursorPageToken":
        if not isinstance(token, str) or not token or token != token.strip():
            raise QueryCursorValidationError("cursor token is malformed")
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise QueryCursorValidationError("cursor token is malformed") from exc
        if not isinstance(value, dict) or set(value) != {"integrity", "next_ordinal", "query_identity_hash", "snapshot_id"}:
            raise QueryCursorValidationError("cursor token fields are invalid")
        return cls(
            value["snapshot_id"],
            value["query_identity_hash"],
            value["next_ordinal"],
            value["integrity"],
        )


@dataclass(frozen=True, slots=True)
class PageResult:
    snapshot: RetainedQuerySnapshot
    rows: tuple[RetainedSnapshotRow, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, RetainedQuerySnapshot):
            raise ValidationFailureError("snapshot must be a RetainedQuerySnapshot")
        if not isinstance(self.rows, tuple) or any(not isinstance(row, RetainedSnapshotRow) for row in self.rows):
            raise ValidationFailureError("rows must be a tuple of RetainedSnapshotRow values")
        if self.next_cursor is not None and (not isinstance(self.next_cursor, str) or not self.next_cursor):
            raise ValidationFailureError("next_cursor must be a non-empty token or None")

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id

    @property
    def total_row_count(self) -> int:
        return self.snapshot.total_row_count


@runtime_checkable
class ReadSnapshotStore(Protocol):
    """Generic coherent-read and retained-snapshot storage boundary."""

    def publish_read_revision(
        self,
        revision: ReadRevisionDraft,
        *,
        expected_head_version: int | None = None,
        expected_revision_id: str | None = None,
    ) -> CurrentReadHead: ...

    def read_current_bundle(
        self,
        principal: Principal,
        scope: AccessScope,
        entity_type: str,
        entity_id: str,
        required_read_capability: str,
    ) -> CurrentReadBundle: ...

    def read_historical_bundle(
        self,
        principal: Principal,
        scope: AccessScope,
        revision_id: str,
        required_read_capability: str,
    ) -> HistoricalReadBundle: ...

    def create_query_snapshot(
        self,
        principal: Principal,
        scope: AccessScope,
        query_identity: Mapping[str, object],
        required_read_capability: str,
        rows: Sequence[VersionedReadRow],
        *,
        ttl_seconds: int = DEFAULT_SNAPSHOT_TTL_SECONDS,
    ) -> RetainedQuerySnapshot: ...

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
    ) -> PageResult: ...


# Explicit compatibility names for callers that use the terminology from the
# design chapters rather than the shorter class names.
ImmutableReadRevision = ReadRevision
RetainedQuerySnapshotRow = RetainedSnapshotRow
CursorData = CursorPageToken
