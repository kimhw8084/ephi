"""File-backed SQLite reference persistence for O2 transaction evidence.

This module intentionally provides durability and transaction semantics for
offline development/integration tests only.  It is not a PostgreSQL adapter or
G05 production qualification.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from ephi.application.context import AccessScope
from ephi.application.errors import StorageFailureError, ValidationFailureError
from ephi.application.hashing import canonical_json, normalize_domain_payload
from ephi.application.storage import (
    AggregateSnapshot,
    CommandUnitOfWork,
    ReceiptAlreadyExistsError,
    StoredCommandReceipt,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS aggregate_state (
    scope_key TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0),
    state_json TEXT NOT NULL,
    PRIMARY KEY (scope_key, aggregate_type, aggregate_id)
);

CREATE TABLE IF NOT EXISTS command_receipt (
    scope_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    command_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    result_identity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
    auth_session_revision_json TEXT NOT NULL,
    security_revision_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (scope_key, subject, command_id)
);

CREATE TABLE IF NOT EXISTS audit_event (
    event_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    command_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    auth_session_revision_json TEXT NOT NULL,
    security_revision_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (scope_key, subject, command_id)
);

CREATE TABLE IF NOT EXISTS outbox_event (
    event_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    command_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (scope_key, subject, command_id)
);

CREATE INDEX IF NOT EXISTS idx_receipt_scope_subject ON command_receipt(scope_key, subject);
CREATE INDEX IF NOT EXISTS idx_audit_scope_aggregate ON audit_event(scope_key, aggregate_type, aggregate_id, aggregate_version);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_event(status, created_at);
"""


def _validated_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


class _SQLiteCommandTransaction:
    """One SQLite transaction implementing the application UoW contract."""

    def __init__(self, adapter: "SQLiteReferenceTransactionAdapter") -> None:
        self.adapter = adapter
        self.connection = adapter.connection

    def __enter__(self) -> "_SQLiteCommandTransaction":
        self.adapter.transaction_lock.acquire()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self.adapter.transaction_lock.release()
            raise StorageFailureError("durable SQLite command transaction could not begin") from exc
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> bool:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        except sqlite3.Error as exc:
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass
            raise StorageFailureError("durable SQLite command transaction failed") from exc
        finally:
            self.adapter.transaction_lock.release()
        return False

    def get_command_receipt(self, scope_key: str, subject: str, command_id: str) -> StoredCommandReceipt | None:
        try:
            row = self.connection.execute(
                "SELECT scope_key, subject, command_id, payload_hash, status, result_identity, result_json, aggregate_type, aggregate_id, aggregate_version, auth_session_revision_json, security_revision_json, committed_at FROM command_receipt WHERE scope_key = ? AND subject = ? AND command_id = ?",
                (scope_key, subject, command_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageFailureError("durable SQLite storage failed while reading a command receipt") from exc
        return self.adapter._receipt_from_row(row) if row is not None else None

    def get_aggregate(
        self,
        scope_key: str,
        aggregate_type: str,
        aggregate_id: str,
        *,
        for_update: bool = False,
    ) -> AggregateSnapshot | None:
        del for_update
        try:
            row = self.connection.execute(
                "SELECT scope_key, aggregate_type, aggregate_id, version, state_json FROM aggregate_state WHERE scope_key = ? AND aggregate_type = ? AND aggregate_id = ?",
                (scope_key, aggregate_type, aggregate_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageFailureError("durable SQLite storage failed while reading an aggregate") from exc
        if row is None:
            return None
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageFailureError("durable aggregate state is not valid JSON") from exc
        if not isinstance(state, dict):
            raise StorageFailureError("durable aggregate state is not a mapping")
        return AggregateSnapshot(row["scope_key"], row["aggregate_type"], row["aggregate_id"], row["version"], state)

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
                "UPDATE aggregate_state SET version = ?, state_json = ? WHERE scope_key = ? AND aggregate_type = ? AND aggregate_id = ? AND version = ?",
                (next_version, state_json, scope_key, aggregate_type, aggregate_id, expected_version),
            ).rowcount)
        except sqlite3.Error as exc:
            raise StorageFailureError("durable SQLite storage failed while updating an aggregate") from exc

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
                "INSERT INTO audit_event(event_id, scope_key, subject, command_id, aggregate_type, aggregate_id, aggregate_version, event_type, event_json, auth_session_revision_json, security_revision_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id, scope_key, subject, command_id, aggregate_type, aggregate_id,
                    aggregate_version, event_type, event_json, auth_session_revision_json,
                    security_revision_json, recorded_at,
                ),
            )
        except sqlite3.Error as exc:
            raise StorageFailureError("durable SQLite storage failed while appending audit") from exc

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
                "INSERT INTO outbox_event(event_id, scope_key, subject, command_id, aggregate_type, aggregate_id, aggregate_version, event_type, payload_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id, scope_key, subject, command_id, aggregate_type, aggregate_id,
                    aggregate_version, event_type, payload_json, status, created_at,
                ),
            )
        except sqlite3.Error as exc:
            raise StorageFailureError("durable SQLite storage failed while appending outbox") from exc

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
                "INSERT INTO command_receipt(scope_key, subject, command_id, payload_hash, status, result_identity, result_json, aggregate_type, aggregate_id, aggregate_version, auth_session_revision_json, security_revision_json, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope_key, subject, command_id, payload_hash, status, result_identity,
                    result_json, aggregate_type, aggregate_id, aggregate_version,
                    auth_session_revision_json, security_revision_json, committed_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "command_receipt" in str(exc) and ("UNIQUE" in str(exc).upper() or "PRIMARY KEY" in str(exc).upper()):
                raise ReceiptAlreadyExistsError from exc
            raise StorageFailureError("durable SQLite storage failed while inserting receipt") from exc
        except sqlite3.Error as exc:
            raise StorageFailureError("durable SQLite storage failed while inserting receipt") from exc


class SQLiteReferenceTransactionAdapter:
    """A real file-backed SQLite store with no memory/demo fallback."""

    def __init__(self, path: str | Path):
        if not isinstance(path, (str, Path)):
            raise ValidationFailureError("SQLite path must be a filesystem path")
        raw_path = str(path)
        lowered = raw_path.strip().lower()
        if not lowered or lowered == ":memory:" or lowered.startswith("file:") or "mode=memory" in lowered:
            raise ValidationFailureError("SQLite reference adapter requires a file-backed path")
        self.path = Path(path)
        if self.path.exists() and self.path.is_dir():
            raise ValidationFailureError("SQLite path must identify a file, not a directory")
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=10.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 10000")
            journal_mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if journal_mode.lower() != "wal":
                raise sqlite3.DatabaseError("SQLite WAL mode was not enabled")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.executescript(SCHEMA)
            audit_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(audit_event)").fetchall()}
            for column in ("auth_session_revision_json", "security_revision_json"):
                if column not in audit_columns:
                    self._connection.execute(
                        f"ALTER TABLE audit_event ADD COLUMN {column} TEXT NOT NULL DEFAULT 'null'"
                    )
        except (OSError, sqlite3.Error) as exc:
            try:
                self._connection.close()
            except Exception:
                pass
            raise StorageFailureError("durable SQLite storage could not be opened") from exc

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageFailureError("durable SQLite storage is closed")
        return self._connection

    @property
    def transaction_lock(self) -> threading.RLock:
        return self._lock

    def command_transaction(self) -> CommandUnitOfWork:
        """Open the storage-neutral bounded command transaction."""

        return _SQLiteCommandTransaction(self)

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            with self._lock:
                connection.close()
                self._connection = None

    def __enter__(self) -> "SQLiteReferenceTransactionAdapter":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

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
        with self._lock:
            connection = self.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO aggregate_state(scope_key, aggregate_type, aggregate_id, version, state_json) VALUES (?, ?, ?, ?, ?)",
                    (scope.canonical_key, aggregate_type, aggregate_id, version, state_json),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ValidationFailureError("aggregate already exists") from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise StorageFailureError("durable SQLite storage failed while seeding an aggregate") from exc
        return AggregateSnapshot(scope.canonical_key, aggregate_type, aggregate_id, version, normalized)

    def get_aggregate(self, scope: AccessScope, aggregate_type: str, aggregate_id: str) -> AggregateSnapshot | None:
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        aggregate_type = _validated_identity(aggregate_type, "aggregate_type")
        aggregate_id = _validated_identity(aggregate_id, "aggregate_id")
        try:
            row = self.connection.execute(
                "SELECT scope_key, aggregate_type, aggregate_id, version, state_json FROM aggregate_state WHERE scope_key = ? AND aggregate_type = ? AND aggregate_id = ?",
                (scope.canonical_key, aggregate_type, aggregate_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageFailureError("durable SQLite storage failed while reading an aggregate") from exc
        if row is None:
            return None
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageFailureError("durable aggregate state is not valid JSON") from exc
        if not isinstance(state, dict):
            raise StorageFailureError("durable aggregate state is not a mapping")
        return AggregateSnapshot(row["scope_key"], row["aggregate_type"], row["aggregate_id"], row["version"], state)

    def get_command_receipt(self, scope_key: str, subject: str, command_id: str) -> StoredCommandReceipt | None:
        scope_key = _validated_identity(scope_key, "scope_key")
        subject = _validated_identity(subject, "subject")
        command_id = _validated_identity(command_id, "command_id")
        try:
            row = self.connection.execute(
                "SELECT scope_key, subject, command_id, payload_hash, status, result_identity, result_json, aggregate_type, aggregate_id, aggregate_version, auth_session_revision_json, security_revision_json, committed_at FROM command_receipt WHERE scope_key = ? AND subject = ? AND command_id = ?",
                (scope_key, subject, command_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageFailureError("durable SQLite storage failed while reading a command receipt") from exc
        return self._receipt_from_row(row) if row is not None else None

    def count_rows(self) -> dict[str, int]:
        try:
            return {
                table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("aggregate_state", "command_receipt", "audit_event", "outbox_event")
            }
        except sqlite3.Error as exc:
            raise StorageFailureError("durable SQLite storage failed while counting rows") from exc

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
        except sqlite3.Error as exc:
            raise StorageFailureError("durable SQLite storage failed while reading events") from exc
        events = []
        for row in rows:
            item = dict(row)
            payload_key = json_column.removesuffix("_json")
            try:
                item[payload_key] = json.loads(item.pop(json_column))
            except (TypeError, json.JSONDecodeError) as exc:
                raise StorageFailureError("durable event payload is not valid JSON") from exc
            events.append(item)
        return tuple(events)

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> StoredCommandReceipt:
        return StoredCommandReceipt(
            scope_key=row["scope_key"],
            subject=row["subject"],
            command_id=row["command_id"],
            payload_hash=row["payload_hash"],
            status=row["status"],
            result_identity=row["result_identity"],
            result_json=row["result_json"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            aggregate_version=row["aggregate_version"],
            auth_session_revision_json=row["auth_session_revision_json"],
            security_revision_json=row["security_revision_json"],
            committed_at=row["committed_at"],
        )


SQLiteReferenceStore = SQLiteReferenceTransactionAdapter
