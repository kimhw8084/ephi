"""The bounded O3 Attention query application contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .context import AccessScope, CurrentAuthorizationAuthority, Principal
from .errors import (
    QueryCursorValidationError,
    QueryIdentityMismatchError,
    QueryTooBroadError,
    StorageFailureError,
    ValidationFailureError,
)
from .read import (
    DEFAULT_SNAPSHOT_TTL_SECONDS,
    MAX_PAGE_SIZE,
    PageResult,
    ReadSnapshotStore,
    RetainedSnapshotRow,
    VersionedReadRow,
)


ATTENTION_READ_CAPABILITY = "ephi.attention.read"
MAX_ATTENTION_ROWS = 1000

_FILTER_FIELDS = frozenset({"search", "work_state", "owner", "priority", "technical_state", "source_state"})
_SORT_FIELDS = frozenset({
    "priority",
    "deadline",
    "severity",
    "owner",
    "work_state",
    "age",
    "episode_id",
})
_SORT_DIRECTIONS = frozenset({"asc", "desc"})
_DEFAULT_ORDER = (
    {"field": "priority", "direction": "desc"},
    {"field": "deadline", "direction": "asc"},
    {"field": "severity", "direction": "desc"},
    {"field": "age", "direction": "desc"},
    {"field": "episode_id", "direction": "asc"},
)


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


def _normalize_filters(filters: Mapping[str, object] | None) -> dict[str, Any]:
    if filters is None:
        return {}
    if not isinstance(filters, Mapping):
        raise ValidationFailureError("attention filters must be a mapping")
    normalized: dict[str, Any] = {}
    for field, value in filters.items():
        field = _identity(field, "attention filter")
        if field not in _FILTER_FIELDS:
            raise ValidationFailureError(f"unsupported attention filter: {field}")
        if field == "search":
            if not isinstance(value, str):
                raise ValidationFailureError("attention search must be a string")
            value = value.strip()
            if not value:
                continue
        elif isinstance(value, (list, tuple, frozenset)):
            value = tuple(_identity(item, f"attention filter {field} value") for item in value)
            if not value:
                raise ValidationFailureError(f"attention filter {field} must not be empty")
        else:
            value = _identity(value, f"attention filter {field} value")
        normalized[field] = value
    return dict(sorted(normalized.items()))


def _normalize_order(order: Sequence[object] | None) -> tuple[dict[str, str], ...]:
    if order is None:
        raw: Sequence[object] = _DEFAULT_ORDER
    else:
        raw = order
    if not isinstance(raw, (tuple, list)) or not raw:
        raise ValidationFailureError("attention order must contain at least one sort")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            if "_" not in item:
                raise ValidationFailureError("attention sort strings must use field_direction form")
            field, direction = item.rsplit("_", 1)
        elif isinstance(item, Mapping):
            if set(item) != {"field", "direction"}:
                raise ValidationFailureError("attention sort objects must contain field and direction")
            field, direction = item["field"], item["direction"]
        else:
            raise ValidationFailureError("attention order contains an invalid sort")
        field = _identity(field, "attention sort field")
        direction = _identity(direction, "attention sort direction").lower()
        if field not in _SORT_FIELDS or direction not in _SORT_DIRECTIONS:
            raise ValidationFailureError(f"unsupported attention sort: {field}:{direction}")
        if field in seen:
            raise ValidationFailureError(f"attention sort field is repeated: {field}")
        seen.add(field)
        normalized.append({"field": field, "direction": direction})
    if "episode_id" not in seen:
        normalized.append({"field": "episode_id", "direction": "asc"})
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class AttentionRow:
    """One stable, already-authorized Attention row."""

    episode_id: str
    payload: dict[str, Any]
    row_version: int | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _identity(self.episode_id, "episode_id"))
        if not isinstance(self.payload, dict):
            raise ValidationFailureError("attention row payload must be an object")
        if isinstance(self.row_version, bool) or not isinstance(self.row_version, (int, str)):
            raise ValidationFailureError("attention row version must be an integer or canonical string")

    def as_dict(self) -> dict[str, Any]:
        return {"episode_id": self.episode_id, **self.payload}


@dataclass(frozen=True, slots=True)
class AttentionPage:
    rows: tuple[AttentionRow, ...]
    total_count: int
    snapshot_id: str
    next_cursor: str | None
    filters: dict[str, Any]
    order: tuple[dict[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rows, tuple) or any(not isinstance(row, AttentionRow) for row in self.rows):
            raise ValidationFailureError("attention rows must be a tuple of AttentionRow values")
        if isinstance(self.total_count, bool) or not isinstance(self.total_count, int) or self.total_count < 0:
            raise ValidationFailureError("attention total_count must be a non-negative integer")
        _identity(self.snapshot_id, "snapshot_id")
        if self.next_cursor is not None:
            _identity(self.next_cursor, "next_cursor")

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.as_dict() for row in self.rows],
            "total_count": self.total_count,
            "query_snapshot_id": self.snapshot_id,
            "next_cursor": self.next_cursor,
            "filters": self.filters,
            "order": self.order,
        }


class AttentionRowSource(Protocol):
    def check_attention_source(self, principal: Principal, scope: AccessScope) -> None: ...

    def fetch_attention_rows(
        self,
        principal: Principal,
        scope: AccessScope,
        filters: Mapping[str, object],
        order: Sequence[Mapping[str, str]],
    ) -> Sequence[VersionedReadRow]: ...


def _row_from_retained(row: RetainedSnapshotRow) -> AttentionRow:
    payload = dict(row.payload)
    episode_id = payload.pop("episode_id", row.row_id)
    if episode_id != row.row_id:
        raise QueryIdentityMismatchError("retained Attention row identity is inconsistent")
    return AttentionRow(episode_id, payload, row.row_version)


class AttentionQueryService:
    """Bind the product query to the existing retained-snapshot authority."""

    def __init__(
        self,
        row_source: AttentionRowSource,
        read_store: ReadSnapshotStore,
        current_authorization: CurrentAuthorizationAuthority,
    ):
        if not hasattr(row_source, "fetch_attention_rows"):
            raise TypeError("row_source must provide the PostgreSQL Attention projection query")
        if not isinstance(read_store, ReadSnapshotStore):
            raise TypeError("read_store must implement the durable read snapshot boundary")
        if not isinstance(current_authorization, CurrentAuthorizationAuthority):
            raise TypeError("current_authorization must be a CurrentAuthorizationAuthority")
        self.row_source = row_source
        self.read_store = read_store
        self.current_authorization = current_authorization

    def check_source(self, principal: Principal, scope: AccessScope) -> None:
        """Run the bounded, authorized health probe for the required source."""

        self.current_authorization.authorize(principal, scope, ATTENTION_READ_CAPABILITY)
        checker = getattr(self.row_source, "check_attention_source", None)
        if not callable(checker):
            raise StorageFailureError("Attention source health authority is unavailable")
        checker(principal, scope)

    def list_attention(
        self,
        principal: Principal,
        scope: AccessScope,
        *,
        filters: Mapping[str, object] | None = None,
        order: Sequence[object] | None = None,
        page_size: int = 50,
        snapshot_id: str | None = None,
        cursor: str | None = None,
        snapshot_ttl_seconds: int = DEFAULT_SNAPSHOT_TTL_SECONDS,
    ) -> AttentionPage:
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
            raise ValidationFailureError("attention page_size must be a positive integer")
        if page_size > MAX_PAGE_SIZE:
            raise QueryTooBroadError("attention page_size exceeds the bounded page size", limit=MAX_PAGE_SIZE)
        normalized_filters = _normalize_filters(filters)
        normalized_order = _normalize_order(order)
        # This is the application disclosure boundary.  It runs before the
        # projection, count, snapshot lookup or cursor continuation.
        self.current_authorization.authorize(principal, scope, ATTENTION_READ_CAPABILITY)
        query_identity = {
            "query": "attention",
            "filters": normalized_filters,
            "order": list(normalized_order),
        }
        if snapshot_id is None:
            if cursor is not None:
                raise QueryCursorValidationError("a cursor requires its retained query snapshot")
            source_rows = tuple(
                self.row_source.fetch_attention_rows(principal, scope, normalized_filters, normalized_order)
            )
            if len(source_rows) > MAX_ATTENTION_ROWS:
                raise QueryTooBroadError(limit=MAX_ATTENTION_ROWS)
            self.current_authorization.authorize(principal, scope, ATTENTION_READ_CAPABILITY)
            snapshot = self.read_store.create_query_snapshot(
                principal,
                scope,
                query_identity,
                ATTENTION_READ_CAPABILITY,
                source_rows,
                ttl_seconds=snapshot_ttl_seconds,
            )
            snapshot_id = snapshot.snapshot_id
        self.current_authorization.authorize(principal, scope, ATTENTION_READ_CAPABILITY)
        page: PageResult = self.read_store.read_query_snapshot_page(
            principal,
            scope,
            snapshot_id,
            query_identity,
            ATTENTION_READ_CAPABILITY,
            page_size=page_size,
            cursor=cursor,
        )
        return AttentionPage(
            tuple(_row_from_retained(row) for row in page.rows),
            page.total_row_count,
            page.snapshot_id,
            page.next_cursor,
            normalized_filters,
            normalized_order,
        )


# Concise vocabulary aliases used by API/UI adapters.
ListAttention = AttentionQueryService
