#!/usr/bin/env python3
"""Installed CHG-147/O9.1 backup, restore, and reconciliation authority."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any, Iterator

from ephi.application.operations import (  # noqa: E402
    artifact_blob_path,
    build_reconciliation_report,
    canonical_sha256,
    file_sha256,
    json_bytes,
    safe_identity_hash,
    verify_artifact_inventory,
)
from ephi.application.source_reality import redacted_connection_facts
from ephi.infrastructure.postgresql import validate_required_schema
from ephi.migration_resources import MigrationResourceError, resolve_migration_resources
from ephi.operations_status import operations_status as _package_operations_status
from ephi.release_identity import ReleaseFailure, installed_release_identity


MANIFEST_SCHEMA = "o9.1.backup.v2"
LEGACY_MANIFEST_SCHEMA = "o9.1.backup.v1"
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
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.+-]{1,80}$")


class OperationsFailure(RuntimeError):
    """A fail-closed, secret-safe operation failure."""


def _secret_safe_failure(message: str):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except OperationsFailure:
                raise
            except Exception:
                raise OperationsFailure(message) from None

        return run

    return decorate


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
        if len(prefix) != 1 or not _TOOL_NAME.fullmatch(prefix[0]) or "/" in prefix[0] or "\\" in prefix[0]:
            raise OperationsFailure("PostgreSQL native tool command is invalid")
        if shutil.which(prefix[0]) is None:
            raise OperationsFailure("PostgreSQL native tool is unavailable in PATH")
        return prefix
    executable = shutil.which(default_name)
    if executable is None:
        raise OperationsFailure(f"{default_name} is unavailable in PATH")
    return [default_name]


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
        raise OperationsFailure("PostgreSQL native tool version probe failed") from None
    output = completed.stdout or completed.stderr
    match = _VERSION_RE.search(output)
    if match is None:
        raise OperationsFailure("PostgreSQL native tool version was not parseable")
    major = int(match.group(1))
    minor = int(match.group(2) or "0")
    return f"PostgreSQL {major}.{minor}", major


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

    return _private_libpq_environment(_libpq_parameters(dsn, database=database))


def _private_libpq_environment(
    parameters: Mapping[str, object], *, base_environment: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Transform parsed libpq parameters into a private native-tool environment."""

    environment = dict(os.environ if base_environment is None else base_environment)
    for variable in _LIBPQ_ENVIRONMENT_NAMES.values():
        environment.pop(variable, None)
    for variable in ("DATABASE_URL", "EPHI_POSTGRES_DSN", "EPHI_TEST_POSTGRES_DSN"):
        environment.pop(variable, None)
    for name, value in parameters.items():
        variable = _LIBPQ_ENVIRONMENT_NAMES.get(name)
        if variable is not None and value is not None:
            environment[variable] = str(value)
    return environment


def _safe_tool_dsn(dsn: str, tool_dsn: str | None) -> str:
    if tool_dsn is not None and tool_dsn != dsn:
        raise OperationsFailure("separate native-tool connection settings are not supported")
    if not dsn.strip():
        raise OperationsFailure("PostgreSQL connection settings are missing")
    return dsn


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
    try:
        parameters = _libpq_parameters(dsn)
        if "service" in parameters:
            raise OperationsFailure("service-based native restore settings are unsupported")
        if not parameters.get("dbname"):
            raise OperationsFailure("PostgreSQL native-tool database identity is missing")
        with tempfile.TemporaryDirectory(prefix="ephi-o9-") as directory:
            service_file = Path(directory) / "pg_service.conf"
            descriptor = os.open(service_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                # Connection fields stay in the private libpq environment. The
                # INI service file carries only a fixed alias, never secrets.
                output.write("[o9_restore]\n")
            environment = _subprocess_env(dsn)
            environment["PGSERVICEFILE"] = str(service_file)
            command = [
                *prefix,
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--dbname=service=o9_restore",
            ]
            with dump_path.open("rb") as stream:
                completed = subprocess.run(
                    command,
                    check=False,
                    stdin=stream,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    timeout=300,
                )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperationsFailure("pg_restore execution failed") from None
    if completed.returncode != 0:
        raise OperationsFailure("pg_restore failed; isolated restore is not verified") from None


def _verify_dump_structure(prefix: Sequence[str], dump_path: Path) -> None:
    try:
        with dump_path.open("rb") as stream:
            completed = subprocess.run(
                [*prefix, "--list"],
                check=False,
                stdin=stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_private_libpq_environment({}),
                timeout=300,
            )
    except (OSError, subprocess.SubprocessError):
        raise OperationsFailure("logical dump structure could not be verified") from None
    if completed.returncode != 0:
        raise OperationsFailure("logical dump structure is corrupt")


def _connect(dsn: str, *, autocommit: bool = True):
    try:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)
    except ImportError:
        raise OperationsFailure("PostgreSQL operations require psycopg[binary]==3.3.6") from None
    except Exception:
        raise OperationsFailure("PostgreSQL connection failed") from None


@contextmanager
def _snapshot_connection(dsn: str) -> Iterator[tuple[Any, dict[str, object]]]:
    connection = _connect(dsn, autocommit=False)
    try:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        row = connection.execute(
            "SELECT clock_timestamp() AS server_at, txid_current() AS transaction_id, "
            "txid_current_snapshot()::text AS transaction_snapshot, pg_export_snapshot() AS exported_snapshot, "
            "current_database() AS database_name, current_schema() AS schema_name, "
            "current_setting('server_version') AS server_version"
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
            "server_version": _normalized_server_version(str(row["server_version"])),
            "database_identity_sha256": safe_identity_hash(str(row["database_name"])),
            "schema_identity_sha256": safe_identity_hash(str(row["schema_name"])),
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


def _normalized_server_version(value: str) -> str:
    match = re.match(r"^(\d+)(?:\.(\d+))?", value)
    if match is None:
        raise OperationsFailure("PostgreSQL server version was not parseable")
    return f"{int(match.group(1))}.{int(match.group(2) or '0')}"


def _migration_identity(_repo_root: Path | None = None) -> dict[str, object]:
    """Return the shared source or installed migration identity authority."""

    try:
        return resolve_migration_resources().identity
    except (MigrationResourceError, OSError, TypeError, ValueError, KeyError) as exc:
        raise OperationsFailure("packaged migration identity is unavailable") from None


def _validate_manifest_state(manifest: Mapping[str, object], *, legacy: bool = False) -> None:
    cutoff = manifest.get("backup_cutoff_high_water")
    durable = manifest.get("durable_state")
    postgresql = manifest.get("postgresql")
    if not isinstance(cutoff, Mapping) or not isinstance(durable, Mapping) or not isinstance(postgresql, Mapping):
        raise OperationsFailure("backup manifest is inconsistent")
    transaction_id = cutoff.get("transaction_id")
    transaction_snapshot = cutoff.get("transaction_snapshot")
    cutoff_at = cutoff.get("cutoff_at_server")
    if (
        isinstance(transaction_id, bool)
        or not isinstance(transaction_id, int)
        or not isinstance(transaction_snapshot, str)
        or not re.fullmatch(r"[0-9]+:[0-9]+(?::(?:[0-9]+(?:,[0-9]+)*)?)?", transaction_snapshot)
        or not isinstance(cutoff_at, str)
        or not isinstance(postgresql.get("server_version"), str)
    ):
        raise OperationsFailure("backup manifest high-water identity is inconsistent")
    try:
        parsed_cutoff = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00"))
    except ValueError:
        raise OperationsFailure("backup manifest high-water identity is inconsistent") from None
    if parsed_cutoff.tzinfo is None or parsed_cutoff.utcoffset() is None:
        raise OperationsFailure("backup manifest high-water identity is inconsistent")
    server_version = postgresql.get("server_version")
    server_major = postgresql.get("server_major")
    tooling = postgresql.get("tooling")
    pg_dump_version = tooling.get("pg_dump_version") if isinstance(tooling, Mapping) else None
    if legacy:
        server_match = _VERSION_RE.search(server_version) if isinstance(server_version, str) and len(server_version) <= 512 else None
        tool_match = _VERSION_RE.search(pg_dump_version) if isinstance(pg_dump_version, str) and len(pg_dump_version) <= 512 else None
    else:
        server_match = re.fullmatch(r"(\d+)\.(\d+)", server_version) if isinstance(server_version, str) else None
        tool_match = re.fullmatch(r"PostgreSQL (\d+)\.(\d+)", pg_dump_version) if isinstance(pg_dump_version, str) else None
    if (
        server_match is None
        or isinstance(server_major, bool)
        or not isinstance(server_major, int)
        or int(server_match.group(1)) != server_major
        or not isinstance(tooling, Mapping)
        or isinstance(tooling.get("pg_dump_major"), bool)
        or not isinstance(tooling.get("pg_dump_major"), int)
        or tooling.get("pg_dump_major") != server_major
        or tool_match is None
        or int(tool_match.group(1)) != server_major
    ):
        raise OperationsFailure("backup manifest PostgreSQL identity is inconsistent")
    tables = durable.get("tables")
    state = tables.get("_state") if isinstance(tables, Mapping) else None
    if (
        not isinstance(tables, Mapping)
        or not isinstance(state, Mapping)
        or not isinstance(state.get("content_sha256"), str)
        or not _HEX_64.fullmatch(state["content_sha256"])
        or durable.get("state_sha256") != state.get("content_sha256")
        or any(table not in tables for table in CRITICAL_TABLES)
        or isinstance(state.get("row_count"), bool)
        or not isinstance(state.get("row_count"), int)
        or state.get("row_count", -1) < 0
        or isinstance(state.get("table_count"), bool)
        or state.get("table_count") != len(CRITICAL_TABLES)
        or state.get("row_count") != sum(
            int(tables[table]["row_count"])
            for table in CRITICAL_TABLES
            if isinstance(tables.get(table), Mapping)
            and isinstance(tables[table].get("row_count"), int)
            and not isinstance(tables[table].get("row_count"), bool)
        )
    ):
        raise OperationsFailure("backup manifest durable-state identity is inconsistent")
    for table in CRITICAL_TABLES:
        fact = tables.get(table)
        if not isinstance(fact, Mapping):
            raise OperationsFailure("backup manifest durable-state identity is inconsistent")
        identities = fact.get("row_identity_hashes")
        versions = fact.get("row_versions")
        if (
            isinstance(fact.get("row_count"), bool)
            or not isinstance(fact.get("row_count"), int)
            or fact.get("row_count", -1) < 0
            or not isinstance(identities, list)
            or len(identities) != fact.get("row_count")
            or any(not isinstance(value, str) or not _HEX_64.fullmatch(value) for value in identities)
            or not isinstance(versions, list)
            or len(versions) != fact.get("row_count")
            or not isinstance(fact.get("content_sha256"), str)
            or not _HEX_64.fullmatch(fact["content_sha256"])
            or fact["content_sha256"] != canonical_sha256(versions)
            or identities != [pair.get("identity_hash") for pair in versions if isinstance(pair, Mapping)]
            or len([pair for pair in versions if isinstance(pair, Mapping)]) != len(versions)
            or any(
                not isinstance(pair, Mapping)
                or not isinstance(pair.get("identity_hash"), str)
                or not _HEX_64.fullmatch(pair["identity_hash"])
                or not isinstance(pair.get("row_hash"), str)
                or not _HEX_64.fullmatch(pair["row_hash"])
                or (
                    pair.get("version") is not None
                    and not isinstance(pair.get("version"), (int, float, bool))
                    and not (
                        isinstance(pair.get("version"), str)
                        and (
                            _HEX_64.fullmatch(pair["version"])
                            or (
                                legacy
                                and len(pair["version"]) <= 1024
                                and not any(ord(character) < 32 for character in pair["version"])
                            )
                        )
                    )
                )
                for pair in versions
            )
        ):
            raise OperationsFailure("backup manifest durable-state identity is inconsistent")
    table_fingerprints = [
        {
            "table": table,
            "row_count": tables[table]["row_count"],
            "content_sha256": tables[table]["content_sha256"],
        }
        for table in CRITICAL_TABLES
    ]
    if state["content_sha256"] != canonical_sha256(table_fingerprints):
        raise OperationsFailure("backup manifest durable-state identity is inconsistent")


def _reconciliation_projection(tables: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """Project an untrusted manifest to the hashed fields reconciliation uses."""

    projected: dict[str, dict[str, object]] = {}
    for table in CRITICAL_TABLES:
        fact = tables[table]
        if not isinstance(fact, Mapping):
            raise OperationsFailure("backup manifest durable-state identity is inconsistent")
        versions = fact["row_versions"]
        if not isinstance(versions, list):
            raise OperationsFailure("backup manifest durable-state identity is inconsistent")
        projected[table] = {
            "row_identity_hashes": list(fact["row_identity_hashes"]),
            "row_versions": [
                {"identity_hash": pair["identity_hash"], "row_hash": pair["row_hash"]}
                for pair in versions
            ],
        }
    return projected


def _table_rows(connection: Any, table: str) -> list[dict[str, object]]:
    try:
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()]
    except Exception:
        raise OperationsFailure(f"critical schema table could not be inspected: {table}") from None


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
            value = row[field]
            return safe_identity_hash(value) if isinstance(value, str) else _json_safe(value)
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
        except OSError:
            raise OperationsFailure("required immutable artifact bytes are missing") from None
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


@_secret_safe_failure("backup creation failed")
def create_backup(
    *,
    dsn: str,
    artifact_root: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    repo_root: Path | None = None,
    pg_dump_command: str | None = None,
    tool_dsn: str | None = None,
) -> dict[str, object]:
    """Create a logical dump plus a verified immutable artifact bundle."""

    # Retained as a source-checkout compatibility keyword. Package identity and
    # migrations come only from their installed authorities.
    del repo_root
    output = Path(output_dir).resolve()
    artifact_source = Path(artifact_root).resolve()
    if output == artifact_source or output in artifact_source.parents or artifact_source in output.parents:
        raise OperationsFailure("backup output directory must be distinct from the active artifact root")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise OperationsFailure("backup output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    dump_path = output / "database.dump"
    backup_artifacts = output / "artifacts"
    try:
        release = installed_release_identity()
    except (ReleaseFailure, OSError, TypeError, ValueError):
        raise OperationsFailure("packaged release identity is unavailable") from None
    migrations = _migration_identity()
    dump_prefix = _tool_prefix(pg_dump_command, "pg_dump")
    tool_version, tool_major = _tool_version(dump_prefix)
    started_monotonic = time.monotonic()
    with _snapshot_connection(dsn) as (connection, high_water):
        try:
            validate_required_schema(connection)
        except Exception:
            raise OperationsFailure("current PostgreSQL schema validation failed") from None
        # The shared current-version helper already reduced this to major.minor.
        server_major = int(str(high_water["server_version"]).split(".", 1)[0])
        if tool_major != server_major:
            raise OperationsFailure("pg_dump major version does not match the PostgreSQL server")
        tables = _table_inventory(connection)
        inventory = _artifact_inventory(connection)
        source_failures = verify_artifact_inventory(artifact_root, inventory)
        if source_failures:
            raise OperationsFailure("source immutable artifact inventory failed closed")
        _copy_artifacts(artifact_source, backup_artifacts, inventory)
        _run_dump(
            dump_prefix,
            dsn=_safe_tool_dsn(dsn, tool_dsn),
            snapshot=str(high_water["exported_snapshot"]),
            output=dump_path,
        )
        dump_sha256, dump_size = file_sha256(dump_path)
    ended_monotonic = time.monotonic()
    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA,
        "operation": "CHG-147/O9.1",
        "release_identity": release,
        "migration_schema_identity": migrations,
        "database_scope": {
            "mode": "POSTGRESQL_LOGICAL_DUMP",
            "critical_tables": list(CRITICAL_TABLES),
            "raw_telemetry_backup": "EXCLUDED_NOT_MODELED_BY_CANONICAL_SCHEMA",
        },
        "postgresql": {
            "server_version": high_water["server_version"],
            "server_major": server_major,
            "safe_database_identity": {
                "database_identity_sha256": high_water["database_identity_sha256"],
                "schema_identity_sha256": high_water["schema_identity_sha256"],
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


@_secret_safe_failure("backup verification failed")
def verify_backup(
    *,
    manifest_path: str | os.PathLike[str],
    repo_root: Path | None = None,
    backup_root: str | os.PathLike[str] | None = None,
    dsn: str | None = None,
    pg_restore_command: str | None = None,
) -> dict[str, object]:
    """Verify backup identity, artifact bytes, tooling, and optional schema."""

    path = Path(manifest_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise OperationsFailure("backup manifest is unreadable") from None
    if not isinstance(manifest, dict):
        raise OperationsFailure("backup manifest is inconsistent")
    if manifest.get("schema_version") == LEGACY_MANIFEST_SCHEMA:
        raise OperationsFailure("legacy backup manifest lacks packaged release identity")
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise OperationsFailure("backup manifest schema/version is unsupported")
    _validate_manifest_state(manifest)
    dump = manifest.get("dump")
    artifact_data = manifest.get("immutable_artifacts")
    if (
        not isinstance(dump, Mapping)
        or dump.get("path") != "database.dump"
        or not isinstance(dump.get("sha256"), str)
        or not _HEX_64.fullmatch(dump["sha256"])
        or isinstance(dump.get("byte_size"), bool)
        or not isinstance(dump.get("byte_size"), int)
        or dump["byte_size"] < 0
        or not isinstance(artifact_data, Mapping)
        or artifact_data.get("backup_root") != "artifacts"
    ):
        raise OperationsFailure("backup manifest is inconsistent")
    dump_file = path.parent / "database.dump"
    try:
        actual_sha, actual_size = file_sha256(dump_file)
    except OSError:
        raise OperationsFailure("logical dump is missing") from None
    if (actual_sha, actual_size) != (dump.get("sha256"), dump.get("byte_size")):
        raise OperationsFailure("logical dump hash or size mismatch")
    inventory = artifact_data.get("inventory")
    if (
        not isinstance(inventory, list)
        or isinstance(artifact_data.get("count"), bool)
        or artifact_data.get("count") != len(inventory)
        or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("sha256"), str)
            or not _HEX_64.fullmatch(item["sha256"])
            or isinstance(item.get("byte_size"), bool)
            or not isinstance(item.get("byte_size"), int)
            or item["byte_size"] < 0
            or not isinstance(item.get("scope_key_sha256"), str)
            or not _HEX_64.fullmatch(item["scope_key_sha256"])
            or not isinstance(item.get("metadata_sha256"), str)
            or not _HEX_64.fullmatch(item["metadata_sha256"])
            for item in inventory
        )
    ):
        raise OperationsFailure("immutable artifact inventory is inconsistent")
    if canonical_sha256(inventory) != artifact_data.get("inventory_sha256"):
        raise OperationsFailure("immutable artifact inventory hash mismatch")
    artifact_root = backup_root or path.parent / str(artifact_data.get("backup_root", "artifacts"))
    artifact_failures = verify_artifact_inventory(artifact_root, inventory)
    if artifact_failures:
        raise OperationsFailure("immutable artifact backup contains missing or corrupt bytes")
    current_migrations = _migration_identity()
    if current_migrations != manifest.get("migration_schema_identity"):
        raise OperationsFailure("migration/schema identity mismatch")
    try:
        current_release = installed_release_identity()
    except (ReleaseFailure, OSError, TypeError, ValueError):
        raise OperationsFailure("packaged release identity is unavailable") from None
    if current_release != manifest.get("release_identity"):
        raise OperationsFailure("packaged release identity mismatch")
    restore_prefix = _tool_prefix(pg_restore_command, "pg_restore")
    restore_version, restore_major = _tool_version(restore_prefix)
    postgresql = manifest.get("postgresql")
    server_major = postgresql.get("server_major", -1) if isinstance(postgresql, Mapping) else -1
    if isinstance(server_major, bool) or not isinstance(server_major, int):
        raise OperationsFailure("backup manifest PostgreSQL identity is inconsistent")
    if restore_major != server_major:
        raise OperationsFailure("pg_restore major version does not match the PostgreSQL server")
    _verify_dump_structure(restore_prefix, dump_file)
    result: dict[str, object] = {
        "schema_version": "o9.1.backup-verification.v1",
        "dump": {"sha256": actual_sha, "byte_size": actual_size},
        "immutable_artifacts": {"count": len(inventory), "verification": "VERIFIED"},
        "release_identity": current_release,
        "migration_schema_identity": current_migrations,
        "tooling": {"pg_restore": restore_version},
        "verification_state": "VERIFIED",
    }
    if dsn:
        connection = _connect(dsn)
        try:
            validate_required_schema(connection)
            tables = _table_inventory(connection)
        finally:
            connection.close()
        expected = manifest.get("durable_state", {}).get("tables", {})
        if not isinstance(expected, Mapping) or tables != expected:
            raise OperationsFailure("durable database state does not match the backup manifest")
        result["durable_state"] = {"verification": "VERIFIED", "state_sha256": tables["_state"]["content_sha256"]}
    return result


@_secret_safe_failure("restore rehearsal failed")
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
    verify_backup(manifest_path=manifest_file, pg_restore_command=pg_restore_command)
    target_database = target_database.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", target_database):
        raise OperationsFailure("isolated target database name is invalid")
    artifact_target = Path(target_artifact_root).resolve()
    if artifact_target.exists():
        raise OperationsFailure("isolated artifact target must not already exist")
    bundle_root = (manifest_file.parent / "artifacts").resolve()
    backup_root = manifest_file.parent.resolve()
    if any(root == other or root in other.parents or other in root.parents for root in (artifact_target,) for other in (bundle_root, backup_root)):
        raise OperationsFailure("isolated artifact target must be distinct from the backup bundle")
    if tool_dsn_prefix is not None and tool_dsn_prefix != source_admin_dsn:
        raise OperationsFailure("separate native-tool connection settings are not supported")
    if _native_tool_database(source_admin_dsn) == target_database:
        raise OperationsFailure("isolated target database must differ from the connected database")
    artifact_target.mkdir(parents=True, exist_ok=True)
    admin = _connect(source_admin_dsn)
    try:
        admin.execute("CREATE DATABASE \"" + target_database + "\"")
    except Exception:
        admin.close()
        raise OperationsFailure("isolated PostgreSQL database could not be created") from None
    finally:
        try:
            admin.close()
        except Exception:
            pass
    target_dsn = _dsn_with_database(source_admin_dsn, target_database)
    connection = _connect(target_dsn)
    try:
        target_version = connection.execute("SELECT current_setting('server_version') AS server_version").fetchone()
        target_major = (
            int(_normalized_server_version(str(target_version["server_version"])).split(".", 1)[0])
            if target_version is not None
            else -1
        )
        if (
            target_major != manifest["postgresql"]["server_major"]
        ):
            raise OperationsFailure("isolated target PostgreSQL major version does not match the backup")
    finally:
        connection.close()
    dump_path = manifest_file.parent / str(manifest["dump"]["path"])
    restore_prefix = _tool_prefix(pg_restore_command, "pg_restore")
    tool_target_dsn = target_dsn
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
        validate_required_schema(connection)
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


@_secret_safe_failure("reconciliation failed")
def reconcile(*, manifest_path: str | os.PathLike[str], dsn: str) -> dict[str, object]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise OperationsFailure("backup manifest is inconsistent")
    schema = manifest.get("schema_version")
    if schema not in {MANIFEST_SCHEMA, LEGACY_MANIFEST_SCHEMA}:
        raise OperationsFailure("backup manifest schema/version is unsupported")
    _validate_manifest_state(manifest, legacy=schema == LEGACY_MANIFEST_SCHEMA)
    if schema == MANIFEST_SCHEMA:
        try:
            current_release = installed_release_identity()
        except (ReleaseFailure, OSError, TypeError, ValueError):
            raise OperationsFailure("packaged release identity is unavailable") from None
        if current_release != manifest.get("release_identity"):
            raise OperationsFailure("packaged release identity mismatch")
        if _migration_identity() != manifest.get("migration_schema_identity"):
            raise OperationsFailure("migration/schema identity mismatch")
    cutoff_source = manifest["backup_cutoff_high_water"]
    cutoff = {
        "cutoff_at_server": cutoff_source["cutoff_at_server"],
        "transaction_id": cutoff_source["transaction_id"],
        "transaction_snapshot": cutoff_source["transaction_snapshot"],
    }
    connection = _connect(dsn)
    try:
        validate_required_schema(connection)
        current = _table_inventory(connection)
        observed = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    finally:
        connection.close()
    result = build_reconciliation_report(
        backup_cutoff=cutoff,
        backup_tables=_reconciliation_projection(manifest["durable_state"]["tables"]),
        current_tables=current,
        observed_at=_utc(observed),
    )
    if schema == LEGACY_MANIFEST_SCHEMA:
        result["backup_identity_state"] = "LEGACY_RELEASE_IDENTITY_UNBOUND"
    return result


def operations_status(*, dsn: str | None, artifact_root: str | os.PathLike[str] | None) -> dict[str, object]:
    """Compatibility wrapper delegated to the installed package authority."""

    return _package_operations_status(dsn=dsn, artifact_root=artifact_root)


class _UsageFailure(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _UsageFailure


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--artifact-root")
    status.add_argument("--json", action="store_true")
    create = sub.add_parser("backup-create")
    create.add_argument("--artifact-root", required=True)
    create.add_argument("--output-dir", required=True)
    create.add_argument("--pg-dump-command")
    create.add_argument("--json", action="store_true")
    verify = sub.add_parser("backup-verify")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--backup-root")
    verify.add_argument("--pg-restore-command")
    verify.add_argument("--json", action="store_true")
    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--manifest", required=True)
    reconcile_parser.add_argument("--json", action="store_true")
    rehearsal = sub.add_parser("restore-rehearsal")
    rehearsal.add_argument("--manifest", required=True)
    rehearsal.add_argument("--target-database", required=True)
    rehearsal.add_argument("--target-artifact-root", required=True)
    rehearsal.add_argument("--pg-restore-command")
    rehearsal.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _UsageFailure:
        print(json.dumps({"schema_version": "o9.1.cli.v1", "status": "VERIFY_FAILED", "reason_code": "INVALID_ARGUMENTS"}, sort_keys=True))
        return 2
    try:
        dsn = os.environ.get("EPHI_POSTGRES_DSN", "").strip()
        if args.command == "status":
            try:
                result = operations_status(dsn=dsn or None, artifact_root=args.artifact_root)
            except Exception:
                print(json.dumps(
                    {"schema_version": "o9.1.v1", "status": "ERROR", "reason_code": "STATUS_REPORT_FAILED"},
                    sort_keys=True,
                ))
                return 2
        elif args.command == "backup-create":
            if not dsn:
                raise OperationsFailure("PostgreSQL connection settings are missing")
            result = create_backup(
                dsn=dsn,
                artifact_root=args.artifact_root,
                output_dir=args.output_dir,
                pg_dump_command=args.pg_dump_command,
            )
        elif args.command == "backup-verify":
            result = verify_backup(
                manifest_path=args.manifest,
                backup_root=args.backup_root,
                dsn=dsn or None,
                pg_restore_command=args.pg_restore_command,
            )
        elif args.command == "restore-rehearsal":
            admin_dsn = os.environ.get("EPHI_POSTGRES_ADMIN_DSN", "").strip() or dsn
            if not admin_dsn:
                raise OperationsFailure("PostgreSQL connection settings are missing")
            result = restore_rehearsal(
                manifest_path=args.manifest,
                source_admin_dsn=admin_dsn,
                target_database=args.target_database,
                target_artifact_root=args.target_artifact_root,
                pg_restore_command=args.pg_restore_command,
            )
        else:
            if not dsn:
                raise OperationsFailure("PostgreSQL connection settings are missing")
            result = reconcile(manifest_path=args.manifest, dsn=dsn)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception:
        print(json.dumps({
            "schema_version": "o9.1.cli.v1",
            "status": "VERIFY_FAILED",
            "operation": args.command,
            "reason_code": "OPERATION_FAILED",
        }, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
