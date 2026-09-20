#!/usr/bin/env python3
"""CHG-147/O9.1 operations status, PostgreSQL backup, and restore evidence.

The default PostgreSQL-native tools are discovered from PATH. An explicit
command prefix is supported for isolated environments such as a PostgreSQL 18
container; product logic never assumes a developer Homebrew path.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ephi.application.operations import (  # noqa: E402
    OperationalState,
    OperationsAxis,
    artifact_blob_path,
    build_reconciliation_report,
    canonical_sha256,
    file_sha256,
    json_bytes,
    operations_health_snapshot,
    safe_identity_hash,
    verify_artifact_inventory,
)
from ephi.application.source_reality import preflight_source_reality, redacted_connection_facts  # noqa: E402


MANIFEST_SCHEMA = "o9.1.backup.v1"
CRITICAL_TABLES = (
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
_VERSION_RE = re.compile(r"PostgreSQL\)?\s+(\d+)(?:\.(\d+))?")


class OperationsFailure(RuntimeError):
    """A fail-closed, secret-safe operation failure."""


def _utc(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise OperationsFailure("database timestamp was not timezone-aware")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, str):
        return value
    raise OperationsFailure("database timestamp was not serializable")


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _tool_prefix(command: str | None, default_name: str) -> list[str]:
    if command:
        prefix = shlex.split(command)
        if not prefix:
            raise OperationsFailure(f"{default_name} command prefix is empty")
        return prefix
    executable = shutil.which(default_name)
    if executable is None:
        raise OperationsFailure(f"{default_name} is unavailable in PATH")
    return [executable]


def _tool_version(prefix: Sequence[str]) -> tuple[str, int]:
    try:
        completed = subprocess.run(
            [*prefix, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperationsFailure("PostgreSQL native tool version probe failed") from exc
    output = (completed.stdout or completed.stderr).strip()
    match = _VERSION_RE.search(output)
    if match is None:
        raise OperationsFailure("PostgreSQL native tool version was not parseable")
    return output, int(match.group(1))


def _dsn_with_database(dsn: str, database: str) -> str:
    """Replace only the database component using libpq's parser."""

    try:
        from psycopg.conninfo import make_conninfo

        return make_conninfo(dsn, dbname=database)
    except ImportError:
        raise OperationsFailure("PostgreSQL connection settings could not be prepared") from None
    except Exception:
        raise OperationsFailure("PostgreSQL connection settings could not be prepared") from None


_LIBPQ_ENVIRONMENT_NAMES = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "user": "PGUSER",
    "dbname": "PGDATABASE",
    "service": "PGSERVICE",
    "password": "PGPASSWORD",
    "passfile": "PGPASSFILE",
    "application_name": "PGAPPNAME",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "client_encoding": "PGCLIENTENCODING",
    "options": "PGOPTIONS",
    "sslmode": "PGSSLMODE",
    "sslcompression": "PGSSLCOMPRESSION",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR",
    "sslpassword": "PGSSLPASSWORD",
    "gssencmode": "PGGSSENCMODE",
    "krbsrvname": "PGKRBSRVNAME",
    "gsslib": "PGGSSLIB",
    "replication": "PGREPLICATION",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "load_balance_hosts": "PGLOADBALANCEHOSTS",
    "channel_binding": "PGCHANNELBINDING",
    "keepalives": "PGKEEPALIVES",
    "keepalives_idle": "PGKEEPALIVES_IDLE",
    "keepalives_interval": "PGKEEPALIVES_INTERVAL",
    "keepalives_count": "PGKEEPALIVES_COUNT",
}


def _libpq_parameters(dsn: str, *, database: str | None = None) -> dict[str, str]:
    try:
        from psycopg.conninfo import conninfo_to_dict

        parameters = dict(conninfo_to_dict(dsn))
    except ImportError:
        raise OperationsFailure("PostgreSQL native-tool connection settings are unavailable") from None
    except Exception:
        raise OperationsFailure("PostgreSQL native-tool connection settings could not be parsed") from None
    if database is not None:
        parameters["dbname"] = database
    return {str(key): str(value) for key, value in parameters.items() if value is not None}


def _native_tool_database(dsn: str) -> str:
    database = _libpq_parameters(dsn).get("dbname", "").strip()
    if not database:
        raise OperationsFailure("PostgreSQL native-tool database identity is missing")
    return database


def _subprocess_env(dsn: str, *, database: str | None = None) -> dict[str, str]:
    """Build a private libpq environment; never put conninfo in native-tool argv."""

    parameters = _libpq_parameters(dsn, database=database)
    environment = os.environ.copy()
    for variable in _LIBPQ_ENVIRONMENT_NAMES.values():
        environment.pop(variable, None)
    for variable in ("DATABASE_URL", "EPHI_POSTGRES_DSN", "EPHI_TEST_POSTGRES_DSN"):
        environment.pop(variable, None)
    for name, value in parameters.items():
        variable = _LIBPQ_ENVIRONMENT_NAMES.get(name)
        if variable is not None:
            environment[variable] = value
    return environment


def _safe_tool_dsn(dsn: str, tool_dsn: str | None) -> str:
    selected = (tool_dsn or dsn).strip()
    if not selected:
        raise OperationsFailure("PostgreSQL tool DSN is empty")
    return selected


def _run_dump(prefix: Sequence[str], *, dsn: str, snapshot: str, output: Path) -> None:
    command = [
        *prefix,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--snapshot=" + snapshot,
    ]
    try:
        with output.open("wb") as stream:
            completed = subprocess.run(
                command,
                check=False,
                stdout=stream,
                stderr=subprocess.PIPE,
                env=_subprocess_env(dsn),
                timeout=300,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperationsFailure("pg_dump execution failed") from None
    if completed.returncode != 0:
        raise OperationsFailure("pg_dump failed; PostgreSQL backup is not verified") from None


def _run_restore(prefix: Sequence[str], *, dsn: str, dump_path: Path) -> None:
    command = [
        *prefix,
        "--exit-on-error",
        "--no-owner",
        "--no-privileges",
        "--dbname=" + _native_tool_database(dsn),
    ]
    try:
        with dump_path.open("rb") as stream:
            completed = subprocess.run(
                command,
                check=False,
                stdin=stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_subprocess_env(dsn),
                timeout=300,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperationsFailure("pg_restore execution failed") from None
    if completed.returncode != 0:
        raise OperationsFailure("pg_restore failed; isolated restore is not verified") from None


def _connect(dsn: str, *, autocommit: bool = True):
    try:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)
    except ImportError as exc:
        raise OperationsFailure("PostgreSQL operations require psycopg[binary]==3.3.6") from exc
    except Exception as exc:
        raise OperationsFailure("PostgreSQL connection failed") from exc


@contextmanager
def _snapshot_connection(dsn: str) -> Iterator[tuple[Any, dict[str, object]]]:
    connection = _connect(dsn, autocommit=False)
    try:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        row = connection.execute(
            "SELECT clock_timestamp() AS server_at, txid_current() AS transaction_id, "
            "txid_current_snapshot()::text AS transaction_snapshot, pg_export_snapshot() AS exported_snapshot, "
            "current_database() AS database_name, current_schema() AS schema_name, version() AS server_version"
        ).fetchone()
        if row is None:
            raise OperationsFailure("PostgreSQL high-water query returned no row")
        facts = {
            "server_at": _utc(row["server_at"]),
            "transaction_id": int(row["transaction_id"]),
            "transaction_snapshot": str(row["transaction_snapshot"]),
            "exported_snapshot": str(row["exported_snapshot"]),
            "database_name": str(row["database_name"]),
            "schema_name": str(row["schema_name"]),
            "server_version": str(row["server_version"]),
        }
        yield connection, facts
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    finally:
        connection.close()


def _migration_identity(repo_root: Path = ROOT) -> dict[str, object]:
    migrations = []
    for path in sorted((repo_root / "migrations").glob("*.sql")):
        digest, size = file_sha256(path)
        migrations.append({"path": path.relative_to(repo_root).as_posix(), "sha256": digest, "byte_size": size})
    if not migrations:
        raise OperationsFailure("no numbered migrations were found")
    return {"migration_count": len(migrations), "files": migrations, "identity_sha256": canonical_sha256(migrations)}


def _git_identity(repo_root: Path = ROOT) -> dict[str, object]:
    def git(*args: str) -> str:
        try:
            return subprocess.check_output(["git", *args], cwd=repo_root, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise OperationsFailure("Git identity could not be captured") from exc

    return {
        "source_sha": git("rev-parse", "HEAD"),
        "source_tree": git("rev-parse", "HEAD^{tree}"),
        "base_sha": "4e927cc6436a302134249a50e7ccdbb1c29dc89f",
        "candidate_branch": git("branch", "--show-current"),
    }


def _table_rows(connection: Any, table: str) -> list[dict[str, object]]:
    try:
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()]
    except Exception as exc:
        raise OperationsFailure(f"critical schema table could not be inspected: {table}") from exc


_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "aggregate_state": ("scope_key", "aggregate_type", "aggregate_id"),
    "command_receipt": ("scope_key", "subject", "command_id"),
    "audit_event": ("event_id",),
    "outbox_event": ("event_id",),
    "job": ("job_id",),
    "applied_effect": ("job_id", "effect_key"),
    "read_revision": ("revision_id",),
    "read_head": ("scope_key", "entity_type", "entity_id"),
    "query_snapshot": ("snapshot_id",),
    "query_snapshot_row": ("snapshot_id", "ordinal"),
    "artifact_catalog": ("scope_key", "sha256"),
    "o3_attention_projection": ("scope_key", "episode_id"),
    "source_snapshot": ("snapshot_id",),
    "source_capability": ("scope_key", "source_id", "family_id", "capability_id"),
}


def _row_version(table: str, row: Mapping[str, object]) -> object | None:
    for field in ("version", "aggregate_version", "row_version", "head_version", "lease_epoch", "ordinal"):
        if field in row:
            return row[field]
    return None


def _table_inventory(connection: Any, *, tables: Sequence[str] = CRITICAL_TABLES) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    table_fingerprints: list[dict[str, object]] = []
    for table in tables:
        rows = _table_rows(connection, table)
        identities: list[str] = []
        row_pairs: list[dict[str, object]] = []
        fields = _IDENTITY_FIELDS[table]
        for row in rows:
            identity = {field: _json_safe(row.get(field)) for field in fields}
            identity_hash = safe_identity_hash(identity)
            row_hash = canonical_sha256({key: _json_safe(value) for key, value in sorted(row.items())})
            identities.append(identity_hash)
            row_pairs.append({"identity_hash": identity_hash, "row_hash": row_hash, "version": _json_safe(_row_version(table, row))})
        row_pairs.sort(key=lambda item: str(item["identity_hash"]))
        identities.sort()
        result[table] = {
            "row_count": len(rows),
            "row_identity_hashes": identities,
            "content_sha256": canonical_sha256(row_pairs),
            "row_versions": row_pairs,
        }
        table_fingerprints.append({"table": table, "row_count": len(rows), "content_sha256": result[table]["content_sha256"]})
    result["_state"] = {
        "row_count": sum(int(item["row_count"]) for item in table_fingerprints),
        "content_sha256": canonical_sha256(table_fingerprints),
        "table_count": len(table_fingerprints),
    }
    return result


def _artifact_inventory(connection: Any) -> list[dict[str, object]]:
    inventory = []
    for row in _table_rows(connection, "artifact_catalog"):
        inventory.append(
            {
                "scope_key_sha256": safe_identity_hash(row["scope_key"]),
                "sha256": row["sha256"],
                "byte_size": int(row["byte_size"]),
                "metadata_sha256": canonical_sha256(
                    {
                        "media_type": row["media_type"],
                        "logical_purpose": row["logical_purpose"],
                        "producing_job_id": row["producing_job_id"],
                        "revision_id": row["revision_id"],
                    }
                ),
            }
        )
    return sorted(inventory, key=lambda item: (str(item["scope_key_sha256"]), str(item["sha256"])))


def _durable_worker_health(connection: Any) -> OperationsAxis:
    """Classify the existing durable job table using PostgreSQL time."""

    try:
        row = connection.execute(
            """
            WITH authoritative_clock AS MATERIALIZED (
                SELECT clock_timestamp() AS now
            )
            SELECT
                COUNT(*)::int AS job_count,
                COUNT(*) FILTER (WHERE status = 'FAILED')::int AS failed_count,
                COUNT(*) FILTER (WHERE status = 'DEAD_LETTER')::int AS dead_letter_count,
                COUNT(*) FILTER (WHERE status = 'RUNNING')::int AS running_count,
                COUNT(*) FILTER (
                    WHERE status = 'RUNNING'
                      AND (lease_expires_at IS NULL OR lease_expires_at <= authoritative_clock.now)
                )::int AS expired_running_count
            FROM job
            CROSS JOIN authoritative_clock
            """
        ).fetchone()
    except Exception:
        raise OperationsFailure("durable worker state could not be classified") from None
    if row is None:
        raise OperationsFailure("durable worker state classification returned no result")
    facts = {
        "job_count": int(row["job_count"]),
        "failed_count": int(row["failed_count"]),
        "dead_letter_count": int(row["dead_letter_count"]),
        "running_count": int(row["running_count"]),
        "expired_running_count": int(row["expired_running_count"]),
        "authoritative_clock_used": True,
    }
    terminal_failure_count = facts["failed_count"] + facts["dead_letter_count"]
    if terminal_failure_count:
        return OperationsAxis.create("ERROR", "DURABLE_WORKER_TERMINAL_FAILURE", facts)
    if facts["expired_running_count"]:
        return OperationsAxis.create("STALE", "DURABLE_WORKER_EXPIRED_LEASE", facts)
    return OperationsAxis.create("READY", "DURABLE_WORKER_STATE_REACHABLE", facts)


def _copy_artifacts(source_root: Path, target_root: Path, inventory: Sequence[Mapping[str, object]]) -> None:
    target_root.mkdir(parents=True, exist_ok=True)
    for item in inventory:
        sha256 = item.get("sha256")
        byte_size = item.get("byte_size")
        if not isinstance(sha256, str) or not isinstance(byte_size, int):
            raise OperationsFailure("artifact manifest is inconsistent")
        source = artifact_blob_path(source_root, sha256)
        target = artifact_blob_path(target_root, sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            actual_sha, actual_size = file_sha256(source)
        except OSError as exc:
            raise OperationsFailure("required immutable artifact bytes are missing") from exc
        if actual_sha != sha256 or actual_size != byte_size:
            raise OperationsFailure("required immutable artifact bytes are corrupt")
        if target.exists():
            target_sha, target_size = file_sha256(target)
            if (target_sha, target_size) != (sha256, byte_size):
                raise OperationsFailure("isolated artifact target already contains corrupt bytes")
            continue
        shutil.copyfile(source, target)
        copied_sha, copied_size = file_sha256(target)
        if (copied_sha, copied_size) != (sha256, byte_size):
            raise OperationsFailure("isolated artifact copy failed verification")


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(json_bytes(value))
    os.replace(temporary, path)


def create_backup(
    *,
    dsn: str,
    artifact_root: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    repo_root: Path = ROOT,
    pg_dump_command: str | None = None,
    tool_dsn: str | None = None,
) -> dict[str, object]:
    """Create a logical dump plus a verified immutable artifact bundle."""

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    dump_path = output / "database.dump"
    backup_artifacts = output / "artifacts"
    if dump_path.exists():
        raise OperationsFailure("backup output already contains database.dump; use a new isolated directory")
    dump_prefix = _tool_prefix(pg_dump_command, "pg_dump")
    tool_version, tool_major = _tool_version(dump_prefix)
    started_monotonic = time.monotonic()
    with _snapshot_connection(dsn) as (connection, high_water):
        server_match = _VERSION_RE.search(str(high_water["server_version"]))
        if server_match is None:
            raise OperationsFailure("PostgreSQL server version was not parseable")
        server_major = int(server_match.group(1))
        if tool_major != server_major:
            raise OperationsFailure("pg_dump major version does not match the PostgreSQL server")
        tables = _table_inventory(connection)
        inventory = _artifact_inventory(connection)
        source_failures = verify_artifact_inventory(artifact_root, inventory)
        if source_failures:
            raise OperationsFailure("source immutable artifact inventory failed closed")
        _copy_artifacts(Path(artifact_root), backup_artifacts, inventory)
        _run_dump(
            dump_prefix,
            dsn=_safe_tool_dsn(dsn, tool_dsn),
            snapshot=str(high_water["exported_snapshot"]),
            output=dump_path,
        )
        dump_sha256, dump_size = file_sha256(dump_path)
    ended_monotonic = time.monotonic()
    git = _git_identity(repo_root)
    migrations = _migration_identity(repo_root)
    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA,
        "operation": "CHG-147/O9.1",
        "repository": git,
        "migration_schema_identity": migrations,
        "database_scope": {
            "mode": "POSTGRESQL_LOGICAL_DUMP",
            "critical_tables": list(CRITICAL_TABLES),
            "raw_telemetry_backup": "EXCLUDED_NOT_MODELED_BY_CANONICAL_SCHEMA",
        },
        "postgresql": {
            "server_version": high_water["server_version"],
            "server_major": int(server_match.group(1)),
            "safe_database_identity": {
                "database_name": high_water["database_name"],
                "schema_name": high_water["schema_name"],
                "connection": redacted_connection_facts(dsn),
            },
            "tooling": {"pg_dump_version": tool_version, "pg_dump_major": tool_major},
        },
        "backup_cutoff_high_water": {
            "cutoff_at_server": high_water["server_at"],
            "transaction_id": high_water["transaction_id"],
            "transaction_snapshot": high_water["transaction_snapshot"],
            "snapshot_exported_for_pg_dump": True,
        },
        "dump": {"path": "database.dump", "sha256": dump_sha256, "byte_size": dump_size},
        "immutable_artifacts": {
            "backup_root": "artifacts",
            "count": len(inventory),
            "inventory_sha256": canonical_sha256(inventory),
            "inventory": inventory,
        },
        "durable_state": {"tables": tables, "state_sha256": tables["_state"]["content_sha256"]},
        "verification": {
            "required_schema_present_at_capture": True,
            "artifact_bytes_verified_at_capture": True,
            "manifest_consistent": True,
            "overall_verification_state": "VERIFIED",
        },
        "timing": {
            "scope": "LOCAL_RESTORE_REHEARSAL",
            "backup_duration_seconds": round(ended_monotonic - started_monotonic, 6),
            "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
        },
    }
    _write_json(output / "backup_manifest.json", manifest)
    return manifest


def verify_backup(
    *,
    manifest_path: str | os.PathLike[str],
    repo_root: Path = ROOT,
    backup_root: str | os.PathLike[str] | None = None,
    dsn: str | None = None,
    pg_restore_command: str | None = None,
) -> dict[str, object]:
    """Verify backup identity, artifact bytes, tooling, and optional schema."""

    path = Path(manifest_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationsFailure("backup manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise OperationsFailure("backup manifest schema/version is unsupported")
    dump = manifest.get("dump")
    artifact_data = manifest.get("immutable_artifacts")
    if not isinstance(dump, Mapping) or not isinstance(artifact_data, Mapping):
        raise OperationsFailure("backup manifest is inconsistent")
    dump_file = path.parent / str(dump.get("path"))
    try:
        actual_sha, actual_size = file_sha256(dump_file)
    except OSError as exc:
        raise OperationsFailure("logical dump is missing") from exc
    if (actual_sha, actual_size) != (dump.get("sha256"), dump.get("byte_size")):
        raise OperationsFailure("logical dump hash or size mismatch")
    inventory = artifact_data.get("inventory")
    if not isinstance(inventory, list):
        raise OperationsFailure("immutable artifact inventory is inconsistent")
    if canonical_sha256(inventory) != artifact_data.get("inventory_sha256"):
        raise OperationsFailure("immutable artifact inventory hash mismatch")
    artifact_root = backup_root or path.parent / str(artifact_data.get("backup_root", "artifacts"))
    artifact_failures = verify_artifact_inventory(artifact_root, inventory)
    if artifact_failures:
        raise OperationsFailure("immutable artifact backup contains missing or corrupt bytes")
    current_migrations = _migration_identity(repo_root)
    if current_migrations != manifest.get("migration_schema_identity"):
        raise OperationsFailure("migration/schema identity mismatch")
    restore_prefix = _tool_prefix(pg_restore_command, "pg_restore")
    restore_version, restore_major = _tool_version(restore_prefix)
    postgresql = manifest.get("postgresql")
    server_major = int(postgresql.get("server_major", -1)) if isinstance(postgresql, Mapping) else -1
    if restore_major != server_major:
        raise OperationsFailure("pg_restore major version does not match the PostgreSQL server")
    result: dict[str, object] = {
        "schema_version": "o9.1.backup-verification.v1",
        "manifest": path.name,
        "dump": {"sha256": actual_sha, "byte_size": actual_size},
        "immutable_artifacts": {"count": len(inventory), "verification": "VERIFIED"},
        "migration_schema_identity": manifest["migration_schema_identity"],
        "tooling": {"pg_restore": restore_version},
        "verification_state": "VERIFIED",
    }
    if dsn:
        connection = _connect(dsn)
        try:
            tables = _table_inventory(connection)
        finally:
            connection.close()
        expected = manifest.get("durable_state", {}).get("tables", {})
        if not isinstance(expected, Mapping) or tables != expected:
            raise OperationsFailure("durable database state does not match the backup manifest")
        result["durable_state"] = {"verification": "VERIFIED", "state_sha256": tables["_state"]["content_sha256"]}
    return result


def restore_rehearsal(
    *,
    manifest_path: str | os.PathLike[str],
    source_admin_dsn: str,
    target_database: str,
    target_artifact_root: str | os.PathLike[str],
    pg_restore_command: str | None = None,
    tool_dsn_prefix: str | None = None,
) -> dict[str, object]:
    """Restore to a newly created database and distinct artifact root."""

    manifest_file = Path(manifest_path).resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise OperationsFailure("backup manifest schema/version is unsupported")
    verify_backup(manifest_path=manifest_file, repo_root=ROOT, pg_restore_command=pg_restore_command)
    target_database = target_database.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", target_database):
        raise OperationsFailure("isolated target database name is invalid")
    artifact_target = Path(target_artifact_root).resolve()
    if artifact_target.exists() and any(artifact_target.iterdir()):
        raise OperationsFailure("isolated artifact target must be new and empty")
    artifact_target.mkdir(parents=True, exist_ok=True)
    admin = _connect(source_admin_dsn)
    try:
        admin.execute("CREATE DATABASE \"" + target_database + "\"")
    except Exception as exc:
        admin.close()
        raise OperationsFailure("isolated PostgreSQL database could not be created") from exc
    finally:
        try:
            admin.close()
        except Exception:
            pass
    target_dsn = _dsn_with_database(source_admin_dsn, target_database)
    connection = _connect(target_dsn)
    connection.close()
    dump_path = manifest_file.parent / str(manifest["dump"]["path"])
    restore_prefix = _tool_prefix(pg_restore_command, "pg_restore")
    _tool_version(restore_prefix)
    tool_target_dsn = _dsn_with_database(tool_dsn_prefix or source_admin_dsn, target_database)
    started = time.monotonic()
    _run_restore(restore_prefix, dsn=tool_target_dsn, dump_path=dump_path)
    restore_duration = time.monotonic() - started
    source_artifacts = manifest["immutable_artifacts"]["inventory"]
    _copy_artifacts(
        manifest_file.parent / str(manifest["immutable_artifacts"]["backup_root"]),
        artifact_target,
        source_artifacts,
    )
    verification_started = time.monotonic()
    connection = _connect(target_dsn, autocommit=True)
    try:
        tables = _table_inventory(connection)
        expected = manifest["durable_state"]["tables"]
        if tables != expected:
            raise OperationsFailure("restored critical durable state does not match backup state")
        artifact_failures = verify_artifact_inventory(artifact_target, source_artifacts)
        if artifact_failures:
            raise OperationsFailure("restored immutable artifact inventory failed")
        missing = {
            row["table_name"]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            ).fetchall()
        }
        if set(CRITICAL_TABLES) - missing:
            raise OperationsFailure("restored critical schema is incomplete")
    finally:
        connection.close()
    verification_duration = time.monotonic() - verification_started
    return {
        "schema_version": "o9.1.restore-rehearsal.v1",
        "restore_target": {"isolated": True, "traffic_switched": False, "database_identity": safe_identity_hash(target_database)},
        "durable_state": {"verification": "VERIFIED", "state_sha256": tables["_state"]["content_sha256"]},
        "immutable_artifacts": {"verification": "VERIFIED", "count": len(source_artifacts)},
        "critical_authorities": {
            "command_receipt_audit_outbox": "VERIFIED",
            "workflow_o3": "VERIFIED",
            "retained_read_snapshot": "VERIFIED",
            "worker_effect_fencing_identity": "VERIFIED",
            "artifact_catalog_references": "VERIFIED",
            "o4_source_snapshot_capability": "VERIFIED" if tables["source_snapshot"]["row_count"] and tables["source_capability"]["row_count"] else "VERIFIED_OR_EMPTY",
        },
        "timing": {
            "scope": "LOCAL_RESTORE_REHEARSAL",
            "restore_duration_seconds": round(restore_duration, 6),
            "verification_duration_seconds": round(verification_duration, 6),
        },
        "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
        "verification_state": "VERIFIED",
    }


def reconcile(*, manifest_path: str | os.PathLike[str], dsn: str) -> dict[str, object]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    connection = _connect(dsn)
    try:
        current = _table_inventory(connection)
        observed = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    finally:
        connection.close()
    return build_reconciliation_report(
        backup_cutoff=manifest["backup_cutoff_high_water"],
        backup_tables=manifest["durable_state"]["tables"],
        current_tables=current,
        observed_at=_utc(observed),
    )


def operations_status(*, dsn: str | None, artifact_root: str | os.PathLike[str] | None) -> dict[str, object]:
    process = OperationsAxis.create("READY", "PROCESS_ENTRYPOINT_RESPONDED", {"pid_present": True})
    source = preflight_source_reality().get("capability", {})
    source_state = str(source.get("state", "UNAVAILABLE"))
    source_state = {"PARTIAL": "DEGRADED", "INSUFFICIENT": "NOT_QUALIFIED"}.get(source_state, source_state)
    if source_state not in {item.value for item in OperationalState}:
        source_state = "UNAVAILABLE"
    source_axis = OperationsAxis.create(
        source_state,
        "BLOCKED_REAL_SOURCE" if source_state == "UNAVAILABLE" else str(source.get("reason", "SOURCE_STATE")),
        {"freshness_known": source.get("checked_at") is not None},
    )
    if not dsn:
        postgres = OperationsAxis.create("UNAVAILABLE", "POSTGRES_DSN_NOT_CONFIGURED", {"configured": False})
        artifacts = OperationsAxis.create("UNAVAILABLE", "POSTGRES_REQUIRED_FOR_CATALOG_INTEGRITY", {"configured": False})
        workers = OperationsAxis.create("UNAVAILABLE", "POSTGRES_REQUIRED_FOR_DURABLE_WORKER_STATE", {"configured": False})
    else:
        try:
            connection = _connect(dsn)
            try:
                present = {
                    row["table_name"]
                    for row in connection.execute(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
                    ).fetchall()
                }
                missing = sorted(set(CRITICAL_TABLES) - present)
                if missing:
                    postgres = OperationsAxis.create("ERROR", "CRITICAL_SCHEMA_MISSING", {"missing_table_count": len(missing)})
                    artifacts = OperationsAxis.create("ERROR", "CRITICAL_SCHEMA_MISSING", {"missing_table_count": len(missing)})
                    workers = OperationsAxis.create("ERROR", "CRITICAL_SCHEMA_MISSING", {"missing_table_count": len(missing)})
                else:
                    postgres = OperationsAxis.create("READY", "POSTGRES_REACHABLE_AND_SCHEMA_PRESENT", {"critical_table_count": len(CRITICAL_TABLES)})
                    if artifact_root is None:
                        artifacts = OperationsAxis.create("UNAVAILABLE", "IMMUTABLE_ARTIFACT_ROOT_NOT_CONFIGURED", {"configured": False})
                    else:
                        artifact_inventory = _artifact_inventory(connection)
                        failures = verify_artifact_inventory(artifact_root, artifact_inventory)
                        artifacts = OperationsAxis.create(
                            "ERROR" if failures else "READY",
                            "IMMUTABLE_ARTIFACT_BYTES_FAILED" if failures else "IMMUTABLE_ARTIFACT_INVENTORY_VERIFIED",
                            {"artifact_count": len(artifact_inventory), "inventory_known": True},
                        )
                    workers = _durable_worker_health(connection)
            finally:
                connection.close()
        except OperationsFailure as exc:
            postgres = OperationsAxis.create("UNAVAILABLE", "POSTGRES_UNAVAILABLE", {"configured": True})
            artifacts = OperationsAxis.create("UNAVAILABLE", "POSTGRES_UNAVAILABLE", {"configured": True})
            workers = OperationsAxis.create("UNAVAILABLE", str(exc), {"configured": True})
    evidence = OperationsAxis.create("NOT_QUALIFIED", "QUALIFICATION_AUTHORITY_NOT_BOUND", {"freshness_known": False})
    snapshot = operations_health_snapshot(
        process_transport=process,
        postgres=postgres,
        immutable_artifacts=artifacts,
        source_capability=source_axis,
        durable_worker_jobs=workers,
        evidence_qualification=evidence,
    )
    return snapshot.as_dict()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--dsn", default=os.environ.get("EPHI_POSTGRES_DSN", ""))
    status.add_argument("--artifact-root")
    status.add_argument("--json", action="store_true")
    create = sub.add_parser("backup-create")
    create.add_argument("--dsn", required=True)
    create.add_argument("--artifact-root", required=True)
    create.add_argument("--output-dir", required=True)
    create.add_argument("--pg-dump-command")
    create.add_argument("--tool-dsn")
    create.add_argument("--json", action="store_true")
    verify = sub.add_parser("backup-verify")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--repo-root", default=str(ROOT))
    verify.add_argument("--backup-root")
    verify.add_argument("--dsn")
    verify.add_argument("--pg-restore-command")
    verify.add_argument("--json", action="store_true")
    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--manifest", required=True)
    reconcile_parser.add_argument("--dsn", required=True)
    reconcile_parser.add_argument("--json", action="store_true")
    rehearsal = sub.add_parser("restore-rehearsal")
    rehearsal.add_argument("--manifest", required=True)
    rehearsal.add_argument("--source-admin-dsn", required=True)
    rehearsal.add_argument("--target-database", required=True)
    rehearsal.add_argument("--target-artifact-root", required=True)
    rehearsal.add_argument("--pg-restore-command")
    rehearsal.add_argument("--tool-dsn-prefix")
    rehearsal.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            result = operations_status(dsn=args.dsn.strip() or None, artifact_root=args.artifact_root)
        elif args.command == "backup-create":
            result = create_backup(
                dsn=args.dsn,
                artifact_root=args.artifact_root,
                output_dir=args.output_dir,
                pg_dump_command=args.pg_dump_command,
                tool_dsn=args.tool_dsn,
            )
        elif args.command == "backup-verify":
            result = verify_backup(
                manifest_path=args.manifest,
                repo_root=Path(args.repo_root),
                backup_root=args.backup_root,
                dsn=args.dsn,
                pg_restore_command=args.pg_restore_command,
            )
        elif args.command == "restore-rehearsal":
            result = restore_rehearsal(
                manifest_path=args.manifest,
                source_admin_dsn=args.source_admin_dsn,
                target_database=args.target_database,
                target_artifact_root=args.target_artifact_root,
                pg_restore_command=args.pg_restore_command,
                tool_dsn_prefix=args.tool_dsn_prefix,
            )
        else:
            result = reconcile(manifest_path=args.manifest, dsn=args.dsn)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (OperationsFailure, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "VERIFY_FAILED", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
