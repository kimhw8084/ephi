"""PostgreSQL O3 product bindings over the existing O2 authorities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from datetime import datetime
from typing import Any

from ephi.application.attention import ATTENTION_READ_CAPABILITY
from ephi.application.context import AccessScope, Principal
from ephi.application.episodes import EPISODE_READ_CAPABILITY
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

    def check_attention_source(self, principal: Principal, scope: AccessScope) -> None:
        """Probe the required projection through the same current authority."""

        self._authorize(principal, scope)
        try:
            # The bounded query must touch the required O3 source table.  A
            # reachable source with no matching rows is still distinguishable
            # from a source/database failure by successful query completion.
            self.connection.execute(
                "SELECT 1 FROM o3_attention_projection WHERE scope_key = %s LIMIT 1",
                (scope.canonical_key,),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL Attention source is unavailable") from exc

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
        join_clauses = ["a.scope_key = %s", "a.aggregate_type = 'episode_workflow'", "a.aggregate_id = p.episode_id"]
        where_clauses = ["p.scope_key = %s"]
        # PostgreSQL parameters follow the textual JOIN-before-WHERE order.
        parameters: list[Any] = [scope.canonical_key, scope.canonical_key]
        for field, value in filters.items():
            if field == "search":
                search = _identity(value, "Attention search")
                where_clauses.append("(p.episode_id ILIKE %s OR coalesce(p.payload_json->>'title', '') ILIKE %s OR coalesce(p.payload_json->>'asset_id', '') ILIKE %s)")
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
                    where_clauses.append(expression + " = ANY(%s)")
                    parameters.append(list(values))
                else:
                    where_clauses.append(expression + " = %s")
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
                  ON {' AND '.join(join_clauses)}
                WHERE {' AND '.join(where_clauses)}
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

    @staticmethod
    def _authorize_assets(principal: Principal, scope: AccessScope) -> None:
        if not isinstance(principal, Principal) or not isinstance(scope, AccessScope):
            raise ValidationFailureError("Asset queries require server-derived identity and scope")
        if not principal.grants_scope(scope):
            raise ScopeDeniedError("principal is not granted the requested Asset scope")
        if not principal.has_capability(EPISODE_READ_CAPABILITY):
            raise AuthorizationDeniedError("principal is not currently granted the Asset Episode read capability")

    def fetch_asset_episode_heads(self, principal: Principal, scope: AccessScope) -> tuple[dict[str, Any], ...]:
        """Read bounded current qualified Episode heads and current O5 state."""

        self._authorize_assets(principal, scope)
        try:
            with self.adapter.read_store()._transaction(repeatable_read=True, read_only=True) as connection:
                connection.execute("SET LOCAL statement_timeout = '2000ms'")
                rows = connection.execute(
                    """
                    SELECT r.revision_id, r.entity_id, r.payload_json, r.known_at, r.published_at,
                           r.revision_vector_json, r.workflow_version AS revision_workflow_version,
                           r.workflow_state_json, a.version AS workflow_version, a.state_json AS workflow_state,
                           h.head_version
                    FROM read_head AS h
                    JOIN read_revision AS r
                      ON r.scope_key = h.scope_key AND r.entity_type = h.entity_type
                     AND r.entity_id = h.entity_id AND r.revision_id = h.revision_id
                    JOIN aggregate_state AS a
                      ON a.scope_key = r.scope_key
                     AND a.aggregate_type = r.workflow_aggregate_type
                     AND a.aggregate_id = r.workflow_aggregate_id
                    WHERE h.scope_key = %s
                      AND h.entity_type = 'episode'
                      AND r.known_at <= clock_timestamp()
                      AND r.published_at <= clock_timestamp()
                      AND r.payload_json ? 'investigation_profile'
                    ORDER BY r.known_at ASC, r.published_at ASC, r.entity_id ASC, r.revision_id ASC
                    LIMIT %s
                    """,
                    (scope.canonical_key, 1001),
                ).fetchall()
        except CommandError:
            raise
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL Asset Episode head query failed") from exc
        if len(rows) >= 1001:
            raise QueryTooBroadError("qualified Episode head projection exceeds the bounded Asset scan", limit=1000)
        result = []
        for row in rows:
            result.append({
                "revision_id": row["revision_id"],
                "entity_id": row["entity_id"],
                "payload": _json_object(row["payload_json"], "Asset Episode payload"),
                "known_at": row["known_at"],
                "published_at": row["published_at"],
                "revision_vector": _json_object(row["revision_vector_json"], "Asset Episode revision vector"),
                "revision_workflow_version": int(row["revision_workflow_version"]),
                "workflow_version": int(row["workflow_version"]),
                "workflow_state": _json_object(row["workflow_state"], "Asset current workflow state"),
                "historical_workflow_state": _json_object(row["workflow_state_json"], "Asset historical workflow state"),
                "head_version": int(row["head_version"]),
            })
        return tuple(result)

    def fetch_asset_episode_history(
        self,
        principal: Principal,
        scope: AccessScope,
        asset_id: str,
        knowledge_cutoff: datetime,
        *,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        """Read immutable matching Episode revisions strictly as known by cutoff."""

        self._authorize_assets(principal, scope)
        asset_id = _identity(asset_id, "asset_id")
        if not isinstance(knowledge_cutoff, datetime) or knowledge_cutoff.tzinfo is None or knowledge_cutoff.utcoffset() is None:
            raise ValidationFailureError("Asset history cutoff must be timezone-aware")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 201:
            raise QueryTooBroadError("Asset Episode history exceeds the bounded query contract", limit=201)
        try:
            with self.adapter.read_store()._transaction(repeatable_read=True, read_only=True) as connection:
                connection.execute("SET LOCAL statement_timeout = '2000ms'")
                rows = connection.execute(
                    """
                    SELECT r.revision_id, r.entity_id, r.payload_json, r.known_at, r.published_at,
                           r.revision_vector_json, r.workflow_version, r.workflow_state_json,
                           (h.revision_id = r.revision_id) AS is_current_head
                    FROM read_revision AS r
                    LEFT JOIN read_head AS h
                      ON h.scope_key = r.scope_key AND h.entity_type = r.entity_type
                     AND h.entity_id = r.entity_id
                    WHERE r.scope_key = %s AND r.entity_type = 'episode'
                      AND r.payload_json #>> '{investigation_profile,target,asset_identity}' = %s
                      AND r.known_at <= %s AND r.published_at <= %s
                    ORDER BY r.known_at ASC, r.published_at ASC, r.entity_id ASC, r.revision_id ASC
                    LIMIT %s
                    """,
                    (scope.canonical_key, asset_id, knowledge_cutoff, knowledge_cutoff, limit),
                ).fetchall()
        except CommandError:
            raise
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL Asset Episode history query failed") from exc
        result = []
        for row in rows:
            result.append({
                "revision_id": row["revision_id"],
                "entity_id": row["entity_id"],
                "payload": _json_object(row["payload_json"], "Asset Episode history payload"),
                "known_at": row["known_at"],
                "published_at": row["published_at"],
                "revision_vector": _json_object(row["revision_vector_json"], "Asset Episode history revision vector"),
                "workflow_version": int(row["workflow_version"]),
                "workflow_state": _json_object(row["workflow_state_json"], "Asset historical workflow state"),
                "is_current_head": bool(row["is_current_head"]),
            })
        return tuple(result)

    def fetch_asset_workflow_versions(
        self,
        principal: Principal,
        scope: AccessScope,
        episode_ids: tuple[str, ...],
        knowledge_cutoff: datetime,
        *,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        """Read immutable O2 command-result snapshots for existing O5 versions."""

        self._authorize_assets(principal, scope)
        if not isinstance(episode_ids, tuple) or any(not isinstance(item, str) or not item for item in episode_ids):
            raise ValidationFailureError("Asset workflow identities must be a canonical tuple")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5001:
            raise QueryTooBroadError("Asset workflow history exceeds the bounded query contract", limit=5000)
        if not episode_ids:
            return ()
        try:
            with self.adapter.read_store()._transaction(repeatable_read=True, read_only=True) as connection:
                connection.execute("SET LOCAL statement_timeout = '2000ms'")
                rows = connection.execute(
                    """
                    SELECT aggregate_id, aggregate_version, committed_at, result_json
                    FROM command_receipt
                    WHERE scope_key = %s AND aggregate_type = 'episode_workflow'
                      AND aggregate_id = ANY(%s)
                      AND committed_at::timestamptz <= %s
                    ORDER BY aggregate_id ASC, aggregate_version ASC, command_id ASC
                    LIMIT %s
                    """,
                    (scope.canonical_key, list(episode_ids), knowledge_cutoff, limit),
                ).fetchall()
        except CommandError:
            raise
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL Asset O5 version query failed") from exc
        result = []
        for row in rows:
            result_json = _json_object(row["result_json"], "Asset workflow command result")
            result.append({
                "episode_id": row["aggregate_id"],
                "workflow_version": int(row["aggregate_version"]),
                "committed_at": datetime.fromisoformat(str(row["committed_at"]).replace("Z", "+00:00")),
                "state": result_json.get("state", {}),
            })
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
        connection = self.connection
        try:
            connection.execute("BEGIN")
            connection.execute(
                """
                INSERT INTO o3_attention_projection(scope_key, episode_id, row_version, payload_json)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (scope_key, episode_id) DO UPDATE
                SET row_version = EXCLUDED.row_version, payload_json = EXCLUDED.payload_json
                """,
                (scope.canonical_key, episode_id, str(row_version), canonical_json(normalized)),
            )
            connection.commit()
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                pass
            raise StorageFailureError("durable PostgreSQL Attention projection seed failed") from exc
