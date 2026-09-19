"""PostgreSQL O3 product bindings over the existing O2 authorities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

from ephi.application.attention import ATTENTION_READ_CAPABILITY
from ephi.application.context import AccessScope, Principal
from ephi.application.errors import (
    AuthorizationDeniedError,
    CommandError,
    QueryTooBroadError,
    ScopeDeniedError,
    StorageFailureError,
    ValidationFailureError,
)
from ephi.application.hashing import canonical_json, normalize_domain_payload
from ephi.application.read import VersionedReadRow


_MAX_SOURCE_ROWS = 1001


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


def _json_object(value: object, field: str) -> dict[str, Any]:
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageFailureError(f"durable PostgreSQL {field} is not valid JSON") from exc
    try:
        value = normalize_domain_payload(value)
    except ValidationFailureError as exc:
        raise StorageFailureError(f"durable PostgreSQL {field} is not canonical JSON") from exc
    if not isinstance(value, dict):
        raise StorageFailureError(f"durable PostgreSQL {field} is not an object")
    return value


class PostgreSQLO3ProductStore:
    """Indexed O3 projection queries using the already-open PostgreSQL adapter."""

    def __init__(self, adapter):
        if not hasattr(adapter, "connection"):
            raise TypeError("adapter must provide the existing PostgreSQL connection authority")
        self.adapter = adapter

    @property
    def connection(self):
        return self.adapter.connection

    @staticmethod
    def _authorize(principal: Principal, scope: AccessScope) -> None:
        if not isinstance(principal, Principal):
            raise ValidationFailureError("principal must be a server-derived Principal")
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        if not principal.grants_scope(scope):
            raise ScopeDeniedError("principal is not granted the requested product scope")
        if not principal.has_capability(ATTENTION_READ_CAPABILITY):
            raise AuthorizationDeniedError("principal is not currently granted the Attention read capability")

    @staticmethod
    def _sort_sql(order: Sequence[Mapping[str, str]]) -> str:
        expressions = {
            "priority": "CASE p.payload_json->>'priority' WHEN 'P1' THEN 3 WHEN 'P2' THEN 2 WHEN 'P3' THEN 1 ELSE 0 END",
            "deadline": "p.payload_json->>'deadline'",
            "severity": "CASE p.payload_json->>'severity' WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 1 ELSE 0 END",
            "owner": "a.state_json->>'owner'",
            "work_state": "a.state_json->>'work_state'",
            "age": "p.payload_json->>'age'",
            "episode_id": "p.episode_id",
        }
        parts: list[str] = []
        for item in order:
            field = item.get("field")
            direction = item.get("direction")
            if field not in expressions or direction not in {"asc", "desc"}:
                raise ValidationFailureError("unsupported Attention sort reached the PostgreSQL adapter")
            parts.append(f"{expressions[field]} {direction.upper()} NULLS LAST")
        return ", ".join(parts)

    def fetch_attention_rows(
        self,
        principal: Principal,
        scope: AccessScope,
        filters: Mapping[str, object],
        order: Sequence[Mapping[str, str]],
    ) -> tuple[VersionedReadRow, ...]:
        self._authorize(principal, scope)
        if not isinstance(filters, Mapping):
            raise ValidationFailureError("Attention filters must be a mapping")
        clauses = ["p.scope_key = %s", "a.scope_key = %s", "a.aggregate_type = 'episode_workflow'", "a.aggregate_id = p.episode_id"]
        # PostgreSQL parameters follow the textual JOIN-before-WHERE order.
        parameters: list[Any] = [scope.canonical_key, scope.canonical_key]
        for field, value in filters.items():
            if field == "search":
                search = _identity(value, "Attention search")
                clauses.append("(p.episode_id ILIKE %s OR coalesce(p.payload_json->>'title', '') ILIKE %s OR coalesce(p.payload_json->>'asset_id', '') ILIKE %s)")
                pattern = f"%{search}%"
                parameters.extend((pattern, pattern, pattern))
            elif field in {"work_state", "owner", "priority", "technical_state", "source_state"}:
                expression = {
                    "work_state": "a.state_json->>'work_state'",
                    "owner": "a.state_json->>'owner'",
                    "priority": "p.payload_json->>'priority'",
                    "technical_state": "p.payload_json->>'technical_state'",
                    "source_state": "p.payload_json->>'source_state'",
                }[field]
                if isinstance(value, (tuple, list, frozenset)):
                    values = tuple(_identity(item, f"{field} value") for item in value)
                    clauses.append(expression + " = ANY(%s)")
                    parameters.append(list(values))
                else:
                    clauses.append(expression + " = %s")
                    parameters.append(_identity(value, field))
            else:
                raise ValidationFailureError(f"unsupported Attention filter: {field}")
        order_sql = self._sort_sql(order)
        try:
            rows = self.connection.execute(
                f"""
                SELECT p.episode_id, p.row_version,
                       p.payload_json || jsonb_build_object(
                           'episode_id', p.episode_id,
                           'workflow_version', a.version,
                           'owner', a.state_json->'owner',
                           'work_state', a.state_json->'work_state',
                           'acknowledged_at', a.state_json->'acknowledged_at'
                       ) AS payload_json
                FROM o3_attention_projection AS p
                JOIN aggregate_state AS a
                  ON {clauses[1]} AND {clauses[2]} AND {clauses[3]}
                WHERE {clauses[0]}
                ORDER BY {order_sql}
                LIMIT %s
                """,
                (*parameters, _MAX_SOURCE_ROWS),
            ).fetchall()
        except CommandError:
            raise
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL Attention projection query failed") from exc
        if len(rows) >= _MAX_SOURCE_ROWS:
            raise QueryTooBroadError("Attention projection exceeds the bounded retained result", limit=_MAX_SOURCE_ROWS - 1)
        result: list[VersionedReadRow] = []
        for row in rows:
            payload = _json_object(row["payload_json"], "Attention row payload")
            result.append(VersionedReadRow(row["episode_id"], row["row_version"], payload))
        return tuple(result)

    def seed_attention_projection(
        self,
        scope: AccessScope,
        episode_id: str,
        payload: Mapping[str, object],
        *,
        row_version: int | str = 1,
    ) -> None:
        """Explicit reference/integration fixture hook; never used as runtime fallback."""

        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        episode_id = _identity(episode_id, "episode_id")
        if isinstance(row_version, bool) or not isinstance(row_version, (int, str)):
            raise ValidationFailureError("row_version must be an integer or canonical string")
        if isinstance(row_version, str):
            row_version = _identity(row_version, "row_version")
        normalized = normalize_domain_payload(payload)
        if not isinstance(normalized, dict):
            raise ValidationFailureError("Attention payload must be an object")
        try:
            self.connection.execute("BEGIN")
            self.connection.execute(
                """
                INSERT INTO o3_attention_projection(scope_key, episode_id, row_version, payload_json)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (scope_key, episode_id) DO UPDATE
                SET row_version = EXCLUDED.row_version, payload_json = EXCLUDED.payload_json
                """,
                (scope.canonical_key, episode_id, str(row_version), canonical_json(normalized)),
            )
            self.connection.commit()
        except Exception as exc:
            try:
                self.connection.rollback()
            except Exception:
                pass
            raise StorageFailureError("durable PostgreSQL Attention projection seed failed") from exc
