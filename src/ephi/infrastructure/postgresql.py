"""PostgreSQL reference transaction evidence for the O2 command core.

The adapter is deliberately optional: importing :mod:`ephi` does not import
psycopg, and constructing this adapter always requires an explicit DSN.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Iterator
import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Any

from ephi.application.context import AccessScope
from ephi.application.errors import StorageFailureError, ValidationFailureError
from ephi.application.hashing import canonical_json, normalize_domain_payload
from ephi.application.worker import WorkerLeaseConfig
from ephi.application.storage import (
    AggregateAlreadyExistsError,
    AggregateSnapshot,
    CommandEventAlreadyExistsError,
    CommandUnitOfWork,
    ReceiptAlreadyExistsError,
    StoredCommandReceipt,
)


MIGRATION_DIR = Path(__file__).resolve().parents[3] / "migrations"
MIGRATION_PATHS = tuple(sorted(MIGRATION_DIR.glob("*.sql")))
# Kept as a compatibility name for callers that identify the command-core
# migration specifically.  ``apply_migrations`` applies every numbered file.
MIGRATION_PATH = MIGRATION_DIR / "001_o2_command_core.sql"
_TABLES = ("aggregate_state", "command_receipt", "audit_event", "outbox_event")
_REQUIRED_SCHEMA_TABLES = (
    "aggregate_state",
    "command_receipt",
    "audit_event",
    "outbox_event",
    "job",
    "applied_effect",
    "read_revision",
    "read_head",
    "query_snapshot",
    "query_snapshot_row",
    "artifact_catalog",
    "o3_attention_projection",
    "source_snapshot",
    "source_capability",
)


def _sql_statements(script: str) -> Iterator[str]:
    """Split numbered migrations without splitting dollar-quoted functions."""

    start = 0
    index = 0
    quote: str | None = None
    dollar_tag: str | None = None
    line_comment = False
    block_comment = False
    while index < len(script):
        character = script[index]
        following = script[index + 1] if index + 1 < len(script) else ""
        if line_comment:
            if character == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if character == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if dollar_tag is not None:
            if script.startswith(dollar_tag, index):
                index += len(dollar_tag)
                dollar_tag = None
            else:
                index += 1
            continue
        if quote is not None:
            if character == quote:
                if following == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character == "-" and following == "-":
            line_comment = True
            index += 2
            continue
        if character == "/" and following == "*":
            block_comment = True
            index += 2
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "$":
            end = script.find("$", index + 1)
            if end != -1:
                candidate = script[index:end + 1]
                if candidate[1:-1] == "" or all(part.isalnum() or part == "_" for part in candidate[1:-1]):
                    dollar_tag = candidate
                    index = end + 1
                    continue
        if character == ";":
            statement = script[start:index].strip()
            if statement:
                yield statement
            start = index + 1
        index += 1
    statement = script[start:].strip()
    if statement:
        yield statement


def _validated_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


def _json_object(value: object, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageFailureError(f"durable PostgreSQL {field} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise StorageFailureError(f"durable PostgreSQL {field} is not an object")
    return parsed


def _json_text(value: object, field: str) -> str:
    try:
        return value if isinstance(value, str) else canonical_json(value)
    except StorageFailureError:
        raise
    except ValidationFailureError as exc:
        raise StorageFailureError(f"durable PostgreSQL {field} is not valid JSON") from exc
    except Exception as exc:
        raise StorageFailureError(f"durable PostgreSQL {field} is not valid JSON") from exc


def _deterministic_event_id(kind: str, scope_key: str, subject: str, command_id: str) -> str:
    return hashlib.sha256(f"ephi-o2:{kind}:{scope_key}:{subject}:{command_id}".encode("utf-8")).hexdigest()


def _is_command_event_conflict(
    exc: Exception,
    *,
    table: str,
    event_id: str,
    scope_key: str,
    subject: str,
    command_id: str,
) -> bool:
    if getattr(exc, "sqlstate", None) != "23505":
        return False
    constraint_name = getattr(getattr(exc, "diag", None), "constraint_name", None)
    if constraint_name == f"{table}_command_unique":
        return True
    if constraint_name != f"{table}_pkey":
        return False
    kind = table.removesuffix("_event")
    return event_id == _deterministic_event_id(kind, scope_key, subject, command_id)


class _PostgreSQLCommandTransaction:
    """One explicit PostgreSQL transaction implementing the command UoW."""

    def __init__(self, adapter: "PostgreSQLReferenceTransactionAdapter") -> None:
        self.adapter = adapter
        self.connection = adapter.connection

    def __enter__(self) -> "_PostgreSQLCommandTransaction":
        try:
            self.connection.execute("BEGIN")
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL command transaction could not begin") from exc
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> bool:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        except Exception as exc:
            try:
                self.connection.rollback()
            except Exception:
                pass
            raise StorageFailureError("durable PostgreSQL command transaction failed") from exc
        return False

    def get_command_receipt(self, scope_key: str, subject: str, command_id: str) -> StoredCommandReceipt | None:
        try:
            row = self.connection.execute(
                "SELECT scope_key, subject, command_id, payload_hash, status, result_identity, result_json, aggregate_type, aggregate_id, aggregate_version, auth_session_revision_json, security_revision_json, committed_at FROM command_receipt WHERE scope_key = %s AND subject = %s AND command_id = %s",
                (scope_key, subject, command_id),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while reading a command receipt") from exc
        return self.adapter._receipt_from_row(row) if row is not None else None

    def get_aggregate(
        self,
        scope_key: str,
        aggregate_type: str,
        aggregate_id: str,
        *,
        for_update: bool = False,
    ) -> AggregateSnapshot | None:
        lock = " FOR UPDATE" if for_update else ""
        try:
            row = self.connection.execute(
                "SELECT scope_key, aggregate_type, aggregate_id, version, state_json FROM aggregate_state WHERE scope_key = %s AND aggregate_type = %s AND aggregate_id = %s" + lock,
                (scope_key, aggregate_type, aggregate_id),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while reading an aggregate") from exc
        if row is None:
            return None
        return AggregateSnapshot(
            row["scope_key"],
            row["aggregate_type"],
            row["aggregate_id"],
            row["version"],
            _json_object(row["state_json"], "aggregate state"),
        )

    def update_aggregate(
        self,
        scope_key: str,
        aggregate_type: str,
        aggregate_id: str,
        *,
        expected_version: int,
        next_version: int,
        state_json: str,
    ) -> int:
        try:
            return int(self.connection.execute(
                "UPDATE aggregate_state SET version = %s, state_json = %s::jsonb WHERE scope_key = %s AND aggregate_type = %s AND aggregate_id = %s AND version = %s",
                (next_version, state_json, scope_key, aggregate_type, aggregate_id, expected_version),
            ).rowcount)
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while updating an aggregate") from exc

    def insert_aggregate(
        self,
        scope_key: str,
        aggregate_type: str,
        aggregate_id: str,
        *,
        version: int,
        state_json: str,
    ) -> None:
        try:
            self.connection.execute(
                "INSERT INTO aggregate_state(scope_key, aggregate_type, aggregate_id, version, state_json) VALUES (%s, %s, %s, %s, %s::jsonb)",
                (scope_key, aggregate_type, aggregate_id, version, state_json),
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise AggregateAlreadyExistsError from exc
            raise StorageFailureError("durable PostgreSQL storage failed while creating an aggregate") from exc

    def append_audit(
        self,
        *,
        event_id: str,
        scope_key: str,
        subject: str,
        command_id: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        event_json: str,
        auth_session_revision_json: str,
        security_revision_json: str,
        recorded_at: str,
    ) -> None:
        try:
            self.connection.execute(
                "INSERT INTO audit_event(event_id, scope_key, subject, command_id, aggregate_type, aggregate_id, aggregate_version, event_type, event_json, auth_session_revision_json, security_revision_json, recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)",
                (
                    event_id, scope_key, subject, command_id, aggregate_type, aggregate_id,
                    aggregate_version, event_type, event_json, auth_session_revision_json,
                    security_revision_json, recorded_at,
                ),
            )
        except Exception as exc:
            if _is_command_event_conflict(
                exc,
                table="audit_event",
                event_id=event_id,
                scope_key=scope_key,
                subject=subject,
                command_id=command_id,
            ):
                raise CommandEventAlreadyExistsError from exc
            raise StorageFailureError("durable PostgreSQL storage failed while appending audit") from exc

    def append_outbox(
        self,
        *,
        event_id: str,
        scope_key: str,
        subject: str,
        command_id: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        payload_json: str,
        status: str,
        created_at: str,
    ) -> None:
        try:
            self.connection.execute(
                "INSERT INTO outbox_event(event_id, scope_key, subject, command_id, aggregate_type, aggregate_id, aggregate_version, event_type, payload_json, status, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (
                    event_id, scope_key, subject, command_id, aggregate_type, aggregate_id,
                    aggregate_version, event_type, payload_json, status, created_at,
                ),
            )
        except Exception as exc:
            if _is_command_event_conflict(
                exc,
                table="outbox_event",
                event_id=event_id,
                scope_key=scope_key,
                subject=subject,
                command_id=command_id,
            ):
                raise CommandEventAlreadyExistsError from exc
            raise StorageFailureError("durable PostgreSQL storage failed while appending outbox") from exc

    def insert_receipt(
        self,
        *,
        scope_key: str,
        subject: str,
        command_id: str,
        payload_hash: str,
        status: str,
        result_identity: str,
        result_json: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        auth_session_revision_json: str,
        security_revision_json: str,
        committed_at: str,
    ) -> None:
        try:
            self.connection.execute(
                "INSERT INTO command_receipt(scope_key, subject, command_id, payload_hash, status, result_identity, result_json, aggregate_type, aggregate_id, aggregate_version, auth_session_revision_json, security_revision_json, committed_at) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s::jsonb, %s)",
                (
                    scope_key, subject, command_id, payload_hash, status, result_identity,
                    result_json, aggregate_type, aggregate_id, aggregate_version,
                    auth_session_revision_json, security_revision_json, committed_at,
                ),
            )
        except Exception as exc:
            if (
                getattr(exc, "sqlstate", None) == "23505"
                and getattr(getattr(exc, "diag", None), "constraint_name", None) == "command_receipt_pkey"
            ):
                raise ReceiptAlreadyExistsError from exc
            raise StorageFailureError("durable PostgreSQL storage failed while inserting receipt") from exc


class PostgreSQLReferenceTransactionAdapter:
    """A real PostgreSQL reference adapter with no SQLite or memory fallback."""

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValidationFailureError("PostgreSQL adapter requires an explicit DSN")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise StorageFailureError("PostgreSQL support requires psycopg[binary]==3.3.6") from exc
        self.dsn = dsn
        self._psycopg = psycopg
        self._row_factory = dict_row
        self._connection_lock = Lock()
        self._explicitly_closed = False
        self._connection: Any = None
        try:
            self._connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
            self.apply_migrations()
        except Exception as exc:
            if self._connection is not None:
                try:
                    self._connection.close()
                except Exception:
                    pass
                self._connection = None
            if isinstance(exc, (StorageFailureError, ValidationFailureError)):
                raise
            raise StorageFailureError("durable PostgreSQL storage could not be opened") from exc

    @property
    def connection(self) -> Any:
        with self._connection_lock:
            if self._explicitly_closed or self._connection is None:
                raise StorageFailureError("durable PostgreSQL storage is closed")
            connection = self._connection
            if not self._connection_is_broken(connection):
                return connection
            replacement: Any | None = None
            try:
                replacement = self._psycopg.connect(
                    self.dsn,
                    autocommit=True,
                    row_factory=self._row_factory,
                )
                self._validate_existing_schema(replacement)
            except Exception as exc:
                if replacement is not None:
                    try:
                        replacement.close()
                    except Exception:
                        pass
                raise StorageFailureError("durable PostgreSQL storage could not reconnect") from exc
            self._connection = replacement
            try:
                connection.close()
            except Exception:
                pass
            return replacement

    @staticmethod
    def _connection_is_broken(connection: Any) -> bool:
        try:
            return bool(connection.closed) or bool(connection.broken)
        except Exception:
            return True

    @staticmethod
    def _validate_existing_schema(connection: Any) -> None:
        placeholders = ", ".join("%s" for _ in _REQUIRED_SCHEMA_TABLES)
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name IN (" + placeholders + ")",
            _REQUIRED_SCHEMA_TABLES,
        ).fetchall()
        present = {row["table_name"] for row in rows}
        missing = sorted(set(_REQUIRED_SCHEMA_TABLES) - present)
        if missing:
            raise StorageFailureError(
                "durable PostgreSQL schema validation failed; missing tables: " + ", ".join(missing)
            )

    def apply_migrations(self) -> None:
        with self._connection_lock:
            if self._explicitly_closed or self._connection is None:
                raise StorageFailureError("durable PostgreSQL storage is closed")
            connection = self._connection
            try:
                for migration_path in MIGRATION_PATHS:
                    migration = migration_path.read_text(encoding="utf-8")
                    for statement in _sql_statements(migration):
                        connection.execute(statement)
            except (OSError, StorageFailureError):
                raise
            except Exception as exc:
                raise StorageFailureError("durable PostgreSQL migration could not be applied") from exc

    def worker_store(self, *, config: WorkerLeaseConfig | None = None):
        """Return the generic durable worker adapter on this PostgreSQL connection."""

        from .postgresql_worker import PostgreSQLWorkerStore

        return PostgreSQLWorkerStore(self, config=config)

    def read_store(self):
        """Return the generic CHG-129 read adapter on this same connection."""

        from .postgresql_reads import PostgreSQLReadSnapshotStore

        return PostgreSQLReadSnapshotStore(self)

    def o3_store(self):
        """Return the narrow CHG-134 product projection adapter on this connection."""

        from .postgresql_o3 import PostgreSQLO3ProductStore

        return PostgreSQLO3ProductStore(self)

    def seed_attention_projection(self, *args, **kwargs):
        return self.o3_store().seed_attention_projection(*args, **kwargs)

    def artifact_catalog(self):
        """Return the separate CHG-133 scoped artifact catalog adapter."""

        from .artifacts import PostgreSQLArtifactCatalog

        return PostgreSQLArtifactCatalog(self)

    def source_store(self):
        """Return the O4 bounded source-manifest/capability adapter."""

        from .postgresql_source import PostgreSQLSourceSnapshotStore

        return PostgreSQLSourceSnapshotStore(self)

    def publish_read_revision(self, *args, **kwargs):
        return self.read_store().publish_read_revision(*args, **kwargs)

    def publish_current_revision(self, *args, **kwargs):
        return self.read_store().publish_current_revision(*args, **kwargs)

    def publish_current_revision_in_transaction(self, *args, **kwargs):
        return self.read_store().publish_current_revision_in_transaction(*args, **kwargs)

    def get_current_head(self, *args, **kwargs):
        return self.read_store().get_current_head(*args, **kwargs)

    def get_read_revision(self, *args, **kwargs):
        return self.read_store().get_read_revision(*args, **kwargs)

    def read_current_bundle(self, *args, **kwargs):
        return self.read_store().read_current_bundle(*args, **kwargs)

    def read_historical_bundle(self, *args, **kwargs):
        return self.read_store().read_historical_bundle(*args, **kwargs)

    def read_historical_revision(self, *args, **kwargs):
        return self.read_store().read_historical_revision(*args, **kwargs)

    def create_query_snapshot(self, *args, **kwargs):
        return self.read_store().create_query_snapshot(*args, **kwargs)

    def create_retained_query_snapshot(self, *args, **kwargs):
        return self.read_store().create_retained_query_snapshot(*args, **kwargs)

    def read_query_snapshot_page(self, *args, **kwargs):
        return self.read_store().read_query_snapshot_page(*args, **kwargs)

    def read_retained_query_snapshot_page(self, *args, **kwargs):
        return self.read_store().read_retained_query_snapshot_page(*args, **kwargs)

    def command_transaction(self) -> CommandUnitOfWork:
        return _PostgreSQLCommandTransaction(self)

    def close(self) -> None:
        with self._connection_lock:
            self._explicitly_closed = True
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()

    def __enter__(self) -> "PostgreSQLReferenceTransactionAdapter":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def server_version(self) -> str:
        try:
            return str(self.connection.execute("SHOW server_version").fetchone()["server_version"])
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while reading server version") from exc

    def seed_aggregate(
        self,
        scope: AccessScope,
        aggregate_type: str,
        aggregate_id: str,
        state: Mapping[str, object],
        *,
        version: int = 0,
    ) -> AggregateSnapshot:
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        aggregate_type = _validated_identity(aggregate_type, "aggregate_type")
        aggregate_id = _validated_identity(aggregate_id, "aggregate_id")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValidationFailureError("aggregate version must be a non-negative integer")
        normalized = normalize_domain_payload(state)
        if not isinstance(normalized, dict):
            raise ValidationFailureError("aggregate state must be a mapping")
        state_json = canonical_json(normalized)
        connection = self.connection
        try:
            connection.execute("BEGIN")
            connection.execute(
                "INSERT INTO aggregate_state(scope_key, aggregate_type, aggregate_id, version, state_json) VALUES (%s, %s, %s, %s, %s::jsonb)",
                (scope.canonical_key, aggregate_type, aggregate_id, version, state_json),
            )
            connection.commit()
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                pass
            if getattr(exc, "sqlstate", None) == "23505":
                raise ValidationFailureError("aggregate already exists") from exc
            raise StorageFailureError("durable PostgreSQL storage failed while seeding an aggregate") from exc
        return AggregateSnapshot(scope.canonical_key, aggregate_type, aggregate_id, version, normalized)

    def get_aggregate(self, scope: AccessScope, aggregate_type: str, aggregate_id: str) -> AggregateSnapshot | None:
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        aggregate_type = _validated_identity(aggregate_type, "aggregate_type")
        aggregate_id = _validated_identity(aggregate_id, "aggregate_id")
        try:
            row = self.connection.execute(
                "SELECT scope_key, aggregate_type, aggregate_id, version, state_json FROM aggregate_state WHERE scope_key = %s AND aggregate_type = %s AND aggregate_id = %s",
                (scope.canonical_key, aggregate_type, aggregate_id),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while reading an aggregate") from exc
        if row is None:
            return None
        return AggregateSnapshot(row["scope_key"], row["aggregate_type"], row["aggregate_id"], row["version"], _json_object(row["state_json"], "aggregate state"))

    def get_command_receipt(self, scope_key: str, subject: str, command_id: str) -> StoredCommandReceipt | None:
        scope_key = _validated_identity(scope_key, "scope_key")
        subject = _validated_identity(subject, "subject")
        command_id = _validated_identity(command_id, "command_id")
        try:
            row = self.connection.execute(
                "SELECT scope_key, subject, command_id, payload_hash, status, result_identity, result_json, aggregate_type, aggregate_id, aggregate_version, auth_session_revision_json, security_revision_json, committed_at FROM command_receipt WHERE scope_key = %s AND subject = %s AND command_id = %s",
                (scope_key, subject, command_id),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while reading a command receipt") from exc
        return self._receipt_from_row(row) if row is not None else None

    def count_rows(self) -> dict[str, int]:
        try:
            return {table: int(self.connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]) for table in _TABLES}
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while counting rows") from exc

    def list_audit_events(self) -> tuple[dict[str, Any], ...]:
        return self._list_event_rows("audit_event", "event_json", "recorded_at")

    def list_outbox_events(self) -> tuple[dict[str, Any], ...]:
        return self._list_event_rows("outbox_event", "payload_json", "created_at")

    def _list_event_rows(self, table: str, json_column: str, timestamp_column: str) -> tuple[dict[str, Any], ...]:
        try:
            status_column = ", status" if table == "outbox_event" else ""
            audit_metadata = ", auth_session_revision_json, security_revision_json" if table == "audit_event" else ""
            rows = self.connection.execute(
                f"SELECT event_id, scope_key, subject, command_id, aggregate_type, aggregate_id, aggregate_version, event_type, {json_column}{audit_metadata}{status_column}, {timestamp_column} FROM {table} ORDER BY event_id"
            ).fetchall()
        except Exception as exc:
            raise StorageFailureError("durable PostgreSQL storage failed while reading events") from exc
        events = []
        for row in rows:
            item = dict(row)
            payload_key = json_column.removesuffix("_json")
            item[payload_key] = _json_object(item.pop(json_column), "event payload")
            if table == "audit_event":
                item["auth_session_revision_json"] = _json_text(item["auth_session_revision_json"], "auth session revision")
                item["security_revision_json"] = _json_text(item["security_revision_json"], "security revision")
            events.append(item)
        return tuple(events)

    @staticmethod
    def _receipt_from_row(row: Mapping[str, Any]) -> StoredCommandReceipt:
        return StoredCommandReceipt(
            scope_key=row["scope_key"],
            subject=row["subject"],
            command_id=row["command_id"],
            payload_hash=row["payload_hash"],
            status=row["status"],
            result_identity=row["result_identity"],
            result_json=_json_text(row["result_json"], "command result"),
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            aggregate_version=row["aggregate_version"],
            auth_session_revision_json=_json_text(row["auth_session_revision_json"], "auth session revision"),
            security_revision_json=_json_text(row["security_revision_json"], "security revision"),
            committed_at=row["committed_at"],
        )


PostgresReferenceTransactionAdapter = PostgreSQLReferenceTransactionAdapter
