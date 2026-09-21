"""The EPHI Attention provider for the installed NiceGUI Base DataSource API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any

from nicegui_base import (
    And,
    Between,
    Comparison,
    ComparisonOperator,
    DataSchema,
    DataSource,
    DistinctResult,
    FilterExpression,
    In,
    IsNull,
    Query,
    QueryResult,
    QuerySort,
    SourceCapabilities,
    SourceHealth,
    SourceHealthStatus,
    SourceProvenance,
    QueryStats,
    TextMatch,
)

from ephi.application.attention import AttentionQueryService
from ephi.application.context import AccessScope, Principal
from ephi.application.errors import ValidationFailureError


_FIELDS = (
    ("episode_id", "Episode", "identifier"),
    ("attention_scent", "Issue / status", "attribute"),
    ("title", "Issue", "attribute"),
    ("asset_id", "Asset", "entity"),
    ("priority", "Priority", "attribute"),
    ("severity", "Severity", "attribute"),
    ("technical_state", "Technical state", "attribute"),
    ("source_state", "Source state", "attribute"),
    ("owner", "Owner", "attribute"),
    ("work_state", "Work state", "attribute"),
    ("workflow_version", "Workflow version", "attribute"),
    ("deadline", "Decision deadline", "timestamp"),
    ("age", "Age", "attribute"),
)
_PROJECTION_FIELDS = frozenset(field[0] for field in _FIELDS)
_SORT_FIELDS = frozenset({"priority", "deadline", "severity", "owner", "work_state", "age", "episode_id"})
_FILTER_FIELDS = frozenset({"episode_id", "title", "asset_id", "priority", "severity", "technical_state", "source_state", "owner", "work_state"})


def _schema() -> DataSchema:
    from nicegui_base import FieldRole, FieldType, SemanticField

    fields = []
    for name, label, role in _FIELDS:
        field_type = FieldType.DATETIME if role == "timestamp" else FieldType.STRING
        field_role = {
            "identifier": FieldRole.IDENTIFIER,
            "entity": FieldRole.ENTITY,
            "attribute": FieldRole.ATTRIBUTE,
            "timestamp": FieldRole.TIMESTAMP,
        }[role]
        fields.append(SemanticField(name, label, field_type, field_role, nullable=True))
    return DataSchema(tuple(fields), key="episode_id", revision="o3-attention-w1")


def _attention_scent(row: Mapping[str, Any]) -> str:
    """Compose one compact, same-row first-paint cue for narrow tables."""

    def value(key: str) -> str:
        raw = row.get(key)
        return str(raw) if raw not in (None, "") else "Unavailable"

    return f"{value('priority')} {value('source_state')}/{value('work_state')} {value('title')}"


def _merge_filter(target: dict[str, object], field: str, value: object) -> None:
    if field not in _FILTER_FIELDS:
        raise ValidationFailureError(f"unsupported EPHI Attention filter: {field}")
    if field in target and target[field] != value:
        raise ValidationFailureError(f"conflicting EPHI Attention filters for: {field}")
    target[field] = value


def _filter_mapping(expression: FilterExpression | None) -> dict[str, object]:
    if expression is None:
        return {}
    if isinstance(expression, And):
        result: dict[str, object] = {}
        for term in expression.terms:
            for field, value in _filter_mapping(term).items():
                _merge_filter(result, field, value)
        return result
    if isinstance(expression, Comparison):
        if expression.operator not in {ComparisonOperator.EQ, ComparisonOperator.NE}:
            raise ValidationFailureError("EPHI Attention only supports equality filters")
        if expression.operator is ComparisonOperator.NE:
            raise ValidationFailureError("EPHI Attention does not support negative filters")
        _merge_filter({}, expression.field, expression.value)
        return {expression.field: expression.value}
    if isinstance(expression, In):
        _merge_filter({}, expression.field, expression.values)
        return {expression.field: tuple(expression.values)}
    if isinstance(expression, TextMatch):
        if expression.field not in {"episode_id", "title", "asset_id"} or expression.negate:
            raise ValidationFailureError("unsupported EPHI Attention text filter")
        return {"search": expression.value}
    if isinstance(expression, (Between, IsNull)):
        raise ValidationFailureError("unsupported EPHI Attention filter expression")
    raise ValidationFailureError("unsupported EPHI Attention filter expression")


class EphiReadDataSource(DataSource):
    """Provider-neutral adapter with explicit EPHI filter/sort/paging bounds."""

    def __init__(
        self,
        service: AttentionQueryService,
        principal_provider: Callable[[], Principal],
        scope_provider: Callable[[], AccessScope],
        *,
        timeout_seconds: float | None = 30.0,
    ):
        super().__init__("ephi-attention", timeout_seconds=timeout_seconds)
        self.service = service
        self.principal_provider = principal_provider
        self.scope_provider = scope_provider
        self._schema = _schema()

    @property
    def provider(self) -> str:
        return "ephi.postgresql.attention"

    @property
    def schema_definition(self) -> DataSchema:
        """The registered EPHI schema used by the Base table composition."""

        return self._schema

    @property
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            filter_pushdown=True,
            search_pushdown=True,
            sort_pushdown=True,
            pagination_pushdown=True,
            projection_pushdown=True,
            cancellation=True,
        )

    async def schema(self) -> DataSchema:
        self._ensure_open()
        return self._schema

    @staticmethod
    def _order(sorts: tuple[QuerySort, ...]) -> tuple[dict[str, str], ...] | None:
        if not sorts:
            return None
        result = []
        for item in sorts:
            if item.field not in _SORT_FIELDS:
                raise ValidationFailureError(f"unsupported EPHI Attention sort: {item.field}")
            direction = getattr(item.direction, "value", item.direction)
            if direction not in {"asc", "desc"}:
                raise ValidationFailureError(f"unsupported EPHI Attention sort direction: {direction}")
            result.append({"field": item.field, "direction": direction})
        return tuple(result)

    def _query_sync(self, query: Query) -> QueryResult:
        if not isinstance(query, Query):
            raise ValidationFailureError("DataSource query must be a NiceGUI Base Query")
        limit = query.limit or 50
        if limit > 100:
            raise ValidationFailureError("EPHI Attention provider page size is bounded at 100")
        filters = _filter_mapping(query.filter)
        if query.search:
            filters["search"] = query.search
        if query.search_fields and any(field not in {"episode_id", "title", "asset_id"} for field in query.search_fields):
            raise ValidationFailureError("unsupported EPHI Attention search field")
        order = self._order(query.sorts)
        started = monotonic()
        principal = self.principal_provider()
        scope = self.scope_provider()
        page = self.service.list_attention(principal, scope, filters=filters, order=order, page_size=limit)
        remaining = query.offset
        collected: list[dict[str, Any]] = []
        while True:
            if remaining < len(page.rows):
                collected.extend(row.as_dict() for row in page.rows[remaining:])
                if len(collected) >= limit or page.next_cursor is None:
                    break
            elif page.next_cursor is None:
                break
            remaining = max(0, remaining - len(page.rows))
            if page.next_cursor is None:
                break
            # Retained-page continuations are new protected operations.  Do
            # not carry the authority snapshot from the first page forward.
            principal = self.principal_provider()
            scope = self.scope_provider()
            page = self.service.list_attention(
                principal,
                scope,
                filters=filters,
                order=order,
                page_size=limit,
                snapshot_id=page.snapshot_id,
                cursor=page.next_cursor,
            )
        rows = tuple({**row, "attention_scent": _attention_scent(row)} for row in collected[:limit])
        if query.projection:
            unknown = set(query.projection) - _PROJECTION_FIELDS
            if unknown:
                raise ValidationFailureError(f"unsupported EPHI Attention projection: {sorted(unknown)}")
            rows = tuple({"episode_id": row["episode_id"], **{field: row.get(field) for field in query.projection if field != "episode_id"}} for row in rows)
        elapsed = (monotonic() - started) * 1000
        return QueryResult(
            rows,
            page.total_count,
            page.total_count,
            SourceProvenance.now(
                self.key,
                self.provider,
                schema_revision=self._schema.revision,
                details={"query_snapshot_id": page.snapshot_id, "next_cursor": page.next_cursor},
            ),
            QueryStats(elapsed, len(rows), True, page.total_count),
        )

    async def query(self, query: Query = Query()) -> QueryResult:
        return await self._run_with_timeout(asyncio.to_thread(self._query_sync, query))

    async def aggregate(self, query):
        raise ValidationFailureError("EPHI Attention does not expose aggregate pushdown")

    async def distinct(self, field: str, query: Query = Query()) -> DistinctResult:
        raise ValidationFailureError("EPHI Attention does not expose distinct pushdown")

    async def health(self) -> SourceHealth:
        started = monotonic()
        try:
            principal = self.principal_provider()
            scope = self.scope_provider()
            self.service.check_source(principal, scope)
        except Exception as exc:
            return SourceHealth.current(
                SourceHealthStatus.UNAVAILABLE,
                message=str(exc),
                latency_ms=(monotonic() - started) * 1000,
                metadata={"authority": "ephi.postgresql", "source_check": "failed"},
            )
        return SourceHealth.current(
            SourceHealthStatus.HEALTHY,
            message="required EPHI PostgreSQL source reachable",
            latency_ms=(monotonic() - started) * 1000,
            metadata={"authority": "ephi.postgresql", "source_check": "passed"},
        )
