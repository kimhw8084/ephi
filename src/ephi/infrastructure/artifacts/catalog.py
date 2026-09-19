"""Scoped immutable artifact catalogs for reference and PostgreSQL evidence."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator

from ephi.application.artifacts import (
    ArtifactCatalogRegistration,
    ArtifactContentIdentity,
    ArtifactMetadata,
    ScopedArtifactReference,
    internal_artifact_object_key,
)
from ephi.application.errors import (
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactMetadataConflictError,
    ArtifactNotFoundError,
    ArtifactStorageConfigurationError,
    StorageFailureError,
    ValidationFailureError,
)


def _validate_metadata(metadata: ArtifactMetadata) -> None:
    if not isinstance(metadata, ArtifactMetadata):
        raise ValidationFailureError("metadata must be an ArtifactMetadata")


def _validate_object_key(metadata: ArtifactMetadata, object_key: str) -> None:
    if object_key != internal_artifact_object_key(metadata.content):
        raise ArtifactStorageConfigurationError(
            "artifact object key must be derived from the validated SHA-256 only"
        )


def _metadata_conflict(metadata: ArtifactMetadata) -> ArtifactMetadataConflictError:
    return ArtifactMetadataConflictError(
        "scoped artifact identity is already registered with conflicting immutable metadata",
        details={
            "scope_key": metadata.reference.scope_key,
            "sha256": metadata.content.sha256,
        },
    )


def _ensure_same_or_conflict(existing: ArtifactMetadata, requested: ArtifactMetadata) -> None:
    if existing.immutable_metadata_key() != requested.immutable_metadata_key():
        raise _metadata_conflict(requested)


def _ensure_catalog_record_integrity(metadata: ArtifactMetadata, object_key: str) -> None:
    if object_key != internal_artifact_object_key(metadata.content):
        raise ArtifactIntegrityError("catalog object key is not derived from its content identity")


def _parse_sqlite_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise StorageFailureError("SQLite artifact catalog has an invalid server timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class PostgreSQLArtifactCatalog:
    """PostgreSQL scoped catalog; it never reads or writes blob bytes."""

    def __init__(self, adapter: Any):
        if not hasattr(adapter, "connection"):
            raise ArtifactStorageConfigurationError(
                "PostgreSQL artifact catalog requires an explicit PostgreSQL adapter"
            )
        self.adapter = adapter

    @property
    def connection(self) -> Any:
        try:
            return self.adapter.connection
        except Exception as exc:
            raise StorageFailureError("PostgreSQL artifact catalog connection is unavailable") from exc

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self.connection
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except ArtifactMetadataConflictError:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        except ArtifactError:
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
            raise StorageFailureError("PostgreSQL artifact catalog transaction failed") from exc

    @staticmethod
    def _from_row(row: Any) -> ArtifactMetadata:
        try:
            identity = ArtifactContentIdentity(row["sha256"], int(row["byte_size"]))
            reference = ScopedArtifactReference(
                _scope_from_key(row["scope_key"]),
                identity,
            )
            created_at = row["created_at"]
            if not isinstance(created_at, datetime) or created_at.tzinfo is None:
                raise ValueError("created_at is not a timezone-aware PostgreSQL timestamp")
            metadata = ArtifactMetadata(
                reference,
                row["media_type"],
                row["logical_purpose"],
                row["producing_job_id"],
                row["revision_id"],
                created_at,
            )
            _ensure_catalog_record_integrity(metadata, row["object_key"])
            return metadata
        except ArtifactIntegrityError:
            raise
        except Exception as exc:
            raise StorageFailureError("PostgreSQL artifact catalog row is invalid") from exc

    def register(self, metadata: ArtifactMetadata, *, object_key: str) -> ArtifactCatalogRegistration:
        _validate_metadata(metadata)
        _validate_object_key(metadata, object_key)
        scope_key = metadata.reference.scope_key
        sha256 = metadata.content.sha256
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT scope_key, sha256, byte_size, media_type, logical_purpose, object_key, producing_job_id, revision_id, created_at FROM artifact_catalog WHERE scope_key = %s AND sha256 = %s FOR UPDATE",
                    (scope_key, sha256),
                ).fetchone()
                if row is not None:
                    existing = self._from_row(row)
                    _ensure_same_or_conflict(existing, metadata)
                    return ArtifactCatalogRegistration(existing, False)
                row = connection.execute(
                    "INSERT INTO artifact_catalog(scope_key, sha256, byte_size, media_type, logical_purpose, object_key, producing_job_id, revision_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING scope_key, sha256, byte_size, media_type, logical_purpose, object_key, producing_job_id, revision_id, created_at",
                    (
                        scope_key,
                        sha256,
                        metadata.content.byte_size,
                        metadata.media_type,
                        metadata.logical_purpose,
                        object_key,
                        metadata.producing_job_id,
                        metadata.revision_id,
                    ),
                ).fetchone()
                if row is None:  # pragma: no cover - PostgreSQL RETURNING contract
                    raise StorageFailureError("PostgreSQL artifact catalog insert returned no record")
                return ArtifactCatalogRegistration(self._from_row(row), True)
        except ArtifactMetadataConflictError:
            raise
        except Exception as exc:
            database_error = exc.__cause__ if isinstance(exc, StorageFailureError) else exc
            if getattr(database_error, "sqlstate", None) == "23505":
                # A concurrent first registration may have won after the
                # SELECT.  Re-read the committed row and apply the same exact
                # metadata/idempotency decision without rewriting it.
                existing = self.get(metadata.reference)
                if existing is None:
                    raise StorageFailureError("artifact catalog registration race could not be reconciled") from exc
                _ensure_same_or_conflict(existing, metadata)
                return ArtifactCatalogRegistration(existing, False)
            if isinstance(exc, (StorageFailureError, ArtifactIntegrityError, ArtifactStorageConfigurationError)):
                raise
            raise StorageFailureError("PostgreSQL artifact catalog registration failed") from exc

    def get(self, reference: ScopedArtifactReference) -> ArtifactMetadata | None:
        if not isinstance(reference, ScopedArtifactReference):
            raise ValidationFailureError("reference must be a ScopedArtifactReference")
        try:
            row = self.connection.execute(
                "SELECT scope_key, sha256, byte_size, media_type, logical_purpose, object_key, producing_job_id, revision_id, created_at FROM artifact_catalog WHERE scope_key = %s AND sha256 = %s",
                (reference.scope_key, reference.content.sha256),
            ).fetchone()
        except Exception as exc:
            raise StorageFailureError("PostgreSQL artifact catalog lookup failed") from exc
        if row is None:
            return None
        metadata = self._from_row(row)
        if metadata.reference != reference:
            raise ArtifactIntegrityError("catalog content identity does not match its scoped lookup key")
        return metadata

    def count(self) -> int:
        try:
            return int(self.connection.execute("SELECT COUNT(*) AS count FROM artifact_catalog").fetchone()["count"])
        except Exception as exc:
            raise StorageFailureError("PostgreSQL artifact catalog count failed") from exc


class SQLiteArtifactCatalog:
    """Durable offline catalog used by filesystem/reference tests only."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS artifact_catalog (
        scope_key TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK (sha256 GLOB '[0-9a-f]*' AND length(sha256) = 64),
        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
        media_type TEXT NOT NULL,
        logical_purpose TEXT NOT NULL,
        object_key TEXT NOT NULL,
        producing_job_id TEXT,
        revision_id TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, sha256)
    );
    CREATE INDEX IF NOT EXISTS idx_artifact_catalog_scope ON artifact_catalog(scope_key);
    CREATE TRIGGER IF NOT EXISTS artifact_catalog_immutable_update
    BEFORE UPDATE ON artifact_catalog
    BEGIN SELECT RAISE(ABORT, 'artifact_catalog rows are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS artifact_catalog_immutable_delete
    BEFORE DELETE ON artifact_catalog
    BEGIN SELECT RAISE(ABORT, 'artifact_catalog rows are immutable'); END;
    """

    def __init__(self, path: str | os.PathLike[str]):
        raw_path = os.fspath(path) if isinstance(path, (str, os.PathLike)) else ""
        lowered = raw_path.strip().lower() if isinstance(raw_path, str) else ""
        if not lowered or lowered == ":memory:" or lowered.startswith("file:") or "mode=memory" in lowered:
            raise ArtifactStorageConfigurationError(
                "SQLite artifact catalog requires an explicit file-backed path"
            )
        self.path = Path(raw_path)
        if self.path.exists() and self.path.is_dir():
            raise ArtifactStorageConfigurationError("SQLite artifact catalog path must identify a file")
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=10,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 10000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.executescript(self._SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            try:
                self._connection.close()
            except Exception:
                pass
            raise StorageFailureError("SQLite artifact catalog could not be opened") from exc

    @property
    def connection(self) -> sqlite3.Connection:
        connection = getattr(self, "_connection", None)
        if connection is None:
            raise StorageFailureError("SQLite artifact catalog is closed")
        return connection

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ArtifactMetadata:
        try:
            identity = ArtifactContentIdentity(row["sha256"], int(row["byte_size"]))
            metadata = ArtifactMetadata(
                ScopedArtifactReference(_scope_from_key(row["scope_key"]), identity),
                row["media_type"],
                row["logical_purpose"],
                row["producing_job_id"],
                row["revision_id"],
                _parse_sqlite_time(row["created_at"]),
            )
            _ensure_catalog_record_integrity(metadata, row["object_key"])
            return metadata
        except ArtifactIntegrityError:
            raise
        except Exception as exc:
            raise StorageFailureError("SQLite artifact catalog row is invalid") from exc

    def register(self, metadata: ArtifactMetadata, *, object_key: str) -> ArtifactCatalogRegistration:
        _validate_metadata(metadata)
        _validate_object_key(metadata, object_key)
        with self._lock:
            connection = self.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT scope_key, sha256, byte_size, media_type, logical_purpose, object_key, producing_job_id, revision_id, created_at FROM artifact_catalog WHERE scope_key = ? AND sha256 = ?",
                    (metadata.reference.scope_key, metadata.content.sha256),
                ).fetchone()
                if row is not None:
                    existing = self._from_row(row)
                    _ensure_same_or_conflict(existing, metadata)
                    connection.commit()
                    return ArtifactCatalogRegistration(existing, False)
                row = connection.execute(
                    "INSERT INTO artifact_catalog(scope_key, sha256, byte_size, media_type, logical_purpose, object_key, producing_job_id, revision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING scope_key, sha256, byte_size, media_type, logical_purpose, object_key, producing_job_id, revision_id, created_at",
                    (
                        metadata.reference.scope_key,
                        metadata.content.sha256,
                        metadata.content.byte_size,
                        metadata.media_type,
                        metadata.logical_purpose,
                        object_key,
                        metadata.producing_job_id,
                        metadata.revision_id,
                    ),
                ).fetchone()
                connection.commit()
                if row is None:  # pragma: no cover - SQLite RETURNING contract
                    raise StorageFailureError("SQLite artifact catalog insert returned no record")
                return ArtifactCatalogRegistration(self._from_row(row), True)
            except ArtifactMetadataConflictError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise StorageFailureError("SQLite artifact catalog registration failed") from exc
            except (ArtifactIntegrityError, ArtifactStorageConfigurationError, StorageFailureError):
                connection.rollback()
                raise
            except Exception as exc:
                connection.rollback()
                raise StorageFailureError("SQLite artifact catalog registration failed") from exc

    def get(self, reference: ScopedArtifactReference) -> ArtifactMetadata | None:
        if not isinstance(reference, ScopedArtifactReference):
            raise ValidationFailureError("reference must be a ScopedArtifactReference")
        try:
            row = self.connection.execute(
                "SELECT scope_key, sha256, byte_size, media_type, logical_purpose, object_key, producing_job_id, revision_id, created_at FROM artifact_catalog WHERE scope_key = ? AND sha256 = ?",
                (reference.scope_key, reference.content.sha256),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageFailureError("SQLite artifact catalog lookup failed") from exc
        if row is None:
            return None
        metadata = self._from_row(row)
        if metadata.reference != reference:
            raise ArtifactIntegrityError("catalog content identity does not match its scoped lookup key")
        return metadata

    def count(self) -> int:
        try:
            return int(self.connection.execute("SELECT COUNT(*) FROM artifact_catalog").fetchone()[0])
        except sqlite3.Error as exc:
            raise StorageFailureError("SQLite artifact catalog count failed") from exc

    def close(self) -> None:
        with self._lock:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
                self._connection = None

    def __enter__(self) -> "SQLiteArtifactCatalog":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


def _scope_from_key(scope_key: object):
    """Reconstruct an AccessScope only from its canonical durable identity."""

    import json

    from ephi.application.context import AccessScope

    try:
        value = json.loads(scope_key)
        if not isinstance(value, dict):
            raise ValueError
        return AccessScope(
            value["scope_id"],
            value.get("site_id"),
            value.get("area_id"),
            value.get("family_id"),
            tuple(value.get("project_ids", ())),
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise StorageFailureError("artifact catalog scope identity is invalid") from exc


__all__ = ["PostgreSQLArtifactCatalog", "SQLiteArtifactCatalog"]
