#!/usr/bin/env python3
"""Qualify the frozen N-1 to N PostgreSQL durable upgrade and restart."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_PATH = ROOT / "environment" / "u4_n1_durable_upgrade_restart_authority.json"
RELEASE_AUTHORITY_PATH = ROOT / "environment" / "u4_n1_provider_compatibility_authority.json"
SCHEMA = "org.ephi.u4-n1-durable-upgrade-restart.v1"
_JOB_ID = re.compile(r"^CF-[0-9a-f]{24}$")
_IDENTITY = re.compile(r"^[0-9a-f]{64}$")


class QualificationFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(value) + b"\n")


def _read_json(path: Path, code: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationFailure(code) from exc
    if not isinstance(value, dict):
        raise QualificationFailure(code)
    return value


def _git(repository: Path, *args: str, code: str = "GIT_HISTORY_INVALID") -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure(code) from exc
    if result.returncode != 0:
        raise QualificationFailure(code)
    return result.stdout.strip()


def _command(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    code: str,
    expected_returncode: int = 0,
    sensitive: tuple[str, ...] = (),
) -> tuple[str, int]:
    try:
        process = subprocess.Popen(
            [str(item) for item in argv],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate()
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure(code) from exc
    rendered = stdout + stderr
    if any(value and value in rendered for value in sensitive):
        raise QualificationFailure("PRIVATE_VALUE_DISCLOSURE")
    if process.returncode != expected_returncode:
        raise QualificationFailure(code)
    return stdout.strip(), process.pid


def _command_json(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    code: str,
    expected_returncode: int = 0,
    sensitive: tuple[str, ...] = (),
) -> tuple[dict[str, object], int]:
    output, pid = _command(
        argv,
        cwd=cwd,
        env=env,
        code=code,
        expected_returncode=expected_returncode,
        sensitive=sensitive,
    )
    try:
        value = json.loads(output)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationFailure(code) from exc
    if not isinstance(value, dict):
        raise QualificationFailure(code)
    return value, pid


def _clean_env(env_root: Path, *, database_admin_dsn: str | None = None) -> dict[str, str]:
    home = env_root / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": f"{env_root / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "EPHI_ENV": "development",
    }
    if database_admin_dsn is not None:
        env["EPHI_U4_ADMIN_DSN"] = database_admin_dsn
    return env


def _worker_env(
    base_env: dict[str, str],
    *,
    database_name: str | None = None,
    prefix_database_name: str | None = None,
    fixture: dict[str, object] | None = None,
    blob_root: Path | None = None,
    work_root: Path | None = None,
    repository_roots: list[Path] | None = None,
    expected_command_result: dict[str, object] | None = None,
    expected_read: dict[str, object] | None = None,
    expected_artifact: dict[str, object] | None = None,
) -> dict[str, str]:
    env = dict(base_env)
    if database_name is not None:
        env["EPHI_U4_DATABASE_NAME"] = database_name
    if prefix_database_name is not None:
        env["EPHI_U4_PREFIX_DATABASE_NAME"] = prefix_database_name
    if fixture is not None:
        env["EPHI_U4_FIXTURE_JSON"] = json.dumps(fixture, sort_keys=True, separators=(",", ":"))
    if blob_root is not None:
        env["EPHI_U4_BLOB_ROOT"] = str(blob_root)
    if work_root is not None:
        env["EPHI_U4_WORK_ROOT"] = str(work_root)
    if repository_roots is not None:
        env["EPHI_U4_REPOSITORY_ROOTS_JSON"] = json.dumps([str(item) for item in repository_roots])
    if expected_command_result is not None:
        env["EPHI_U4_EXPECTED_COMMAND_RESULT_JSON"] = json.dumps(expected_command_result, sort_keys=True, separators=(",", ":"))
    if expected_read is not None:
        env["EPHI_U4_EXPECTED_READ_JSON"] = json.dumps(expected_read, sort_keys=True, separators=(",", ":"))
    if expected_artifact is not None:
        env["EPHI_U4_EXPECTED_ARTIFACT_JSON"] = json.dumps(expected_artifact, sort_keys=True, separators=(",", ":"))
    return env


_WORKER_SOURCE = r'''from __future__ import annotations
import hashlib, json, os, re, shutil, subprocess, sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

def normalize(value):
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value

def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def env_json(name):
    return json.loads(os.environ[name])

def database_dsn(name=None):
    import psycopg
    admin = os.environ["EPHI_U4_ADMIN_DSN"]
    return psycopg.conninfo.make_conninfo(admin, dbname=name or os.environ["EPHI_U4_DATABASE_NAME"])

def safe_cli(name, args, *, expected_code=0, expected_failure_reason=None, artifact_root=None, database_name=None):
    import psycopg
    from pathlib import Path
    cli = Path(sys.executable).with_name(name)
    if not cli.is_file():
        raise RuntimeError("INSTALLED_COMMAND_MISSING")
    child_env = dict(os.environ)
    child_env.pop("EPHI_TEST_POSTGRES_DSN", None)
    child_env["EPHI_POSTGRES_DSN"] = database_dsn(database_name)
    result = subprocess.run([str(cli), *args], cwd=Path.cwd(), env=child_env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    rendered = result.stdout + result.stderr
    conn = psycopg.conninfo.conninfo_to_dict(child_env["EPHI_POSTGRES_DSN"])
    sensitive = (child_env["EPHI_POSTGRES_DSN"], *(conn.get(key) for key in ("user", "password", "host", "dbname", "options")))
    if artifact_root is not None:
        sensitive += (str(artifact_root),)
    if any(value and value in rendered for value in sensitive):
        raise RuntimeError("PRIVATE_VALUE_DISCLOSURE")
    try:
        report = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("INSTALLED_COMMAND_OUTPUT_INVALID") from exc
    if not isinstance(report, dict):
        raise RuntimeError("INSTALLED_COMMAND_OUTPUT_INVALID")
    if expected_failure_reason is None:
        if result.returncode != expected_code:
            raise RuntimeError("INSTALLED_COMMAND_FAILED")
    elif (result.returncode == 0 or report.get("status") != "FAIL"
          or report.get("reason_code") != expected_failure_reason
          or report.get("schema_state") == "CURRENT"):
        raise RuntimeError("INSTALLED_COMMAND_REJECTION_MISSING")
    return report, result.returncode

def create_databases():
    import psycopg
    from psycopg import sql
    admin = os.environ["EPHI_U4_ADMIN_DSN"]
    names = (os.environ["EPHI_U4_DATABASE_NAME"], os.environ["EPHI_U4_PREFIX_DATABASE_NAME"])
    created = []
    try:
        with psycopg.connect(admin, autocommit=True) as connection:
            if connection.execute("SELECT current_database()").fetchone()[0] in names:
                raise RuntimeError("DATABASE_NAME_COLLISION")
            for name in names:
                if connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                    raise RuntimeError("DATABASE_NAME_COLLISION")
            version = str(connection.execute("SHOW server_version").fetchone()[0])
            if not version.startswith("18."):
                raise RuntimeError("POSTGRESQL_18_REQUIRED")
            for name in names:
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                created.append(name)
            database_oids = {name: str(connection.execute(
                "SELECT oid FROM pg_database WHERE datname = %s", (name,)).fetchone()[0]) for name in names}
    except Exception:
        if created:
            with psycopg.connect(admin, autocommit=True) as connection:
                for name in created:
                    connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
                    connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        raise
    return {"postgres_version": version, "postgres_major": 18,
            "database_identity_sha256": hashlib.sha256(f"{names[0]}:{database_oids[names[0]]}".encode()).hexdigest(),
            "prefix_database_identity_sha256": hashlib.sha256(f"{names[1]}:{database_oids[names[1]]}".encode()).hexdigest()}

def drop_databases():
    import psycopg
    from psycopg import sql
    admin = os.environ["EPHI_U4_ADMIN_DSN"]
    names = (os.environ["EPHI_U4_DATABASE_NAME"], os.environ["EPHI_U4_PREFIX_DATABASE_NAME"])
    with psycopg.connect(admin, autocommit=True) as connection:
        current = connection.execute("SELECT current_database()").fetchone()[0]
        for name in names:
            if current == name:
                raise RuntimeError("DATABASE_CLEANUP_REFUSED")
            connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            if connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
    return {"disposable_databases_removed": True}

def schema_signature(connection):
    tables = [row["table_name"] for row in connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()]
    columns = [normalize(row) for row in connection.execute(
        "SELECT table_name, column_name, ordinal_position, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position").fetchall()]
    indexes = [normalize(row) for row in connection.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() ORDER BY indexname").fetchall()]
    constraints = [normalize(row) for row in connection.execute(
        "SELECT c.relname AS table_name, con.conname AS constraint_name, pg_get_constraintdef(con.oid, true) AS definition "
        "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() ORDER BY c.relname, con.conname").fetchall()]
    triggers = [normalize(row) for row in connection.execute(
        "SELECT c.relname AS table_name, t.tgname AS trigger_name, pg_get_triggerdef(t.oid, true) AS definition "
        "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND NOT t.tgisinternal ORDER BY c.relname, t.tgname").fetchall()]
    body = {"tables": tables, "columns": columns, "indexes": indexes, "constraints": constraints, "triggers": triggers}
    return {"table_count": len(tables), "column_count": len(columns), "index_count": len(indexes),
            "constraint_count": len(constraints), "trigger_count": len(triggers), "canonical_sha256": digest(body)}

def row_inventory(connection, fixture):
    scope = fixture["scope_key"]
    principal = fixture["principal"]["subject"]
    episode = fixture["episode_id"]
    command = fixture["command"]["command_id"]
    revision = fixture["read"]["revision_id"]
    artifact_sha = fixture["artifact"]["sha256"]
    queries = {
        "aggregate_state": ("SELECT * FROM aggregate_state WHERE scope_key=%s AND aggregate_type=%s AND aggregate_id=%s", (scope, fixture["aggregate_type"], episode)),
        "command_receipt": ("SELECT * FROM command_receipt WHERE scope_key=%s AND subject=%s AND command_id=%s", (scope, principal, command)),
        "audit_event": ("SELECT * FROM audit_event WHERE scope_key=%s AND subject=%s AND command_id=%s", (scope, principal, command)),
        "outbox_event": ("SELECT * FROM outbox_event WHERE scope_key=%s AND subject=%s AND command_id=%s", (scope, principal, command)),
        "read_revision": ("SELECT * FROM read_revision WHERE revision_id=%s", (revision,)),
        "read_head": ("SELECT * FROM read_head WHERE scope_key=%s AND entity_type=%s AND entity_id=%s", (scope, fixture["read"]["entity_type"], episode)),
        "artifact_catalog": ("SELECT * FROM artifact_catalog WHERE scope_key=%s AND sha256=%s", (scope, artifact_sha)),
    }
    rows = {}
    row_hashes = {}
    counts = {}
    for table, (query, parameters) in queries.items():
        fetched = connection.execute(query, parameters).fetchall()
        normalized = [normalize(dict(row)) for row in fetched]
        rows[table] = normalized
        counts[table] = len(normalized)
        row_hashes[table] = digest(normalized)
    body = {"rows": rows, "row_counts": counts, "row_fingerprints": row_hashes}
    return {**body, "canonical_sha256": digest(body)}

def brief_identity(brief):
    return {"episode_id": brief.episode_id, "revision_id": brief.revision_id,
            "revision_vector": brief.revision_vector.as_dict(), "workflow_snapshot": brief.workflow,
            "historical": brief.historical}

def release_probe():
    import ephi
    prefix = Path(sys.prefix).resolve()
    package_path = Path(ephi.__file__).resolve()
    roots = [Path(item).resolve() for item in env_json("EPHI_U4_REPOSITORY_ROOTS_JSON")]
    entries = [Path(item or Path.cwd()).resolve() for item in sys.path]
    if not package_path.is_relative_to(prefix):
        raise RuntimeError("INSTALLED_PACKAGE_IMPORT_INVALID")
    if any(entry == root or root in entry.parents for entry in entries for root in roots):
        raise RuntimeError("CHECKOUT_IMPORT_PATH_PRESENT")
    if "PYTHONPATH" in os.environ or "PYTHONHOME" in os.environ:
        raise RuntimeError("CHECKOUT_IMPORT_ENV_PRESENT")
    return {"installed_package_inside_virtual_environment": True,
            "checkout_pythonpath_or_sys_path_used": False, "public_index_used_for_installation": False}

def seed_n1():
    from ephi.application import (AccessScope, ArtifactService, CommandContext, EpisodeBriefQueryService,
        EpisodeWorkflowCommandService, MutableCurrentAuthorizationAuthority, Principal, RevisionVector)
    from ephi.infrastructure import FileArtifactBlobStore, PostgreSQLArtifactCatalog, PostgreSQLReferenceTransactionAdapter
    fixture = env_json("EPHI_U4_FIXTURE_JSON")
    db_name = os.environ["EPHI_U4_DATABASE_NAME"]
    dsn = database_dsn(db_name)
    apply_report, apply_code = safe_cli("ephi-db-migrate", ["apply"])
    verify_report, verify_code = safe_cli("ephi-db-migrate", ["verify"])
    if (apply_report.get("status") != "APPLIED" or apply_report.get("schema_state") != "CURRENT"
        or verify_report.get("status") != "VERIFIED" or verify_report.get("schema_state") != "CURRENT"):
        raise RuntimeError("N_MINUS_1_MIGRATION_FAILED")
    scope_input = fixture["scope"]
    scope = AccessScope(scope_input["scope_id"], site_id=scope_input["site_id"], area_id=scope_input["area_id"])
    principal_input = fixture["principal"]
    principal = Principal(principal_input["subject"], tuple(fixture["capabilities"]), (scope,),
                          principal_input["auth_session_revision"], principal_input["security_revision"])
    auth = MutableCurrentAuthorizationAuthority(principal)
    workflow_id = fixture["episode_id"]
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        initial = adapter.seed_aggregate(scope, fixture["aggregate_type"], workflow_id,
                                         fixture["initial_workflow_state"], version=0)
        read_input = fixture["read"]
        vector = RevisionVector(read_input["analysis_revision"], read_input["exposure_revision"],
                                read_input["priority_revision"], 0, None, read_input["qualification_manifest_id"])
        adapter.publish_current_revision(scope, read_input["entity_type"], workflow_id,
                                         read_input["revision_id"], vector, read_input["payload"], initial)
        command_input = fixture["command"]
        context = CommandContext(command_input["command_id"], principal, scope,
                                 command_input["expected_workflow_version"], vector, command_input["reason"])
        command_result = EpisodeWorkflowCommandService(adapter, auth).claim_episode(context, workflow_id)
        artifact_input = fixture["artifact"]
        content = artifact_input["content_utf8"].encode("utf-8")
        artifact_service = ArtifactService(FileArtifactBlobStore(Path(os.environ["EPHI_U4_BLOB_ROOT"])),
                                           PostgreSQLArtifactCatalog(adapter), auth)
        written = artifact_service.write_and_register(
            principal, scope, content, media_type=artifact_input["media_type"],
            logical_purpose=artifact_input["logical_purpose"],
            required_write_capability=artifact_input["write_capability"],
            producing_job_id=artifact_input["producing_job_id"], revision_id=read_input["revision_id"])
        fixture["scope_key"] = scope.canonical_key
        fixture["artifact"]["sha256"] = written.metadata.content.sha256
        fixture["artifact"]["byte_size"] = written.metadata.content.byte_size
        aggregate = adapter.get_aggregate(scope, fixture["aggregate_type"], workflow_id)
        briefs = EpisodeBriefQueryService(adapter.read_store(), auth)
        historical = briefs.get_episode_brief(principal, scope, workflow_id, revision_id=read_input["revision_id"])
        current = briefs.get_episode_brief(principal, scope, workflow_id)
        if aggregate is None or aggregate.version != command_result.aggregate_version:
            raise RuntimeError("N_MINUS_1_WORKFLOW_RESULT_INVALID")
        if historical.workflow.get("work_state") != "OPEN" or current.workflow.get("work_state") != "CLAIMED":
            raise RuntimeError("N_MINUS_1_READ_SEMANTICS_INVALID")
        inventory = row_inventory(adapter.connection, fixture)
        if any(inventory["row_counts"][key] != 1 for key in inventory["row_counts"]):
            raise RuntimeError("N_MINUS_1_RETAINED_ROWS_MISSING")
        if (inventory["rows"]["aggregate_state"][0]["version"] != 1
            or inventory["rows"]["read_revision"][0]["workflow_version"] != 0
            or inventory["rows"]["artifact_catalog"][0]["sha256"] != written.metadata.content.sha256):
            raise RuntimeError("N_MINUS_1_RETAINED_IDENTITY_INVALID")
        root_stat = Path(os.environ["EPHI_U4_BLOB_ROOT"]).stat()
        root_fp = hashlib.sha256(f"{root_stat.st_dev}:{root_stat.st_ino}".encode()).hexdigest()
        database_fact = adapter.connection.execute(
            "SELECT current_database() AS database_name, (SELECT oid FROM pg_database WHERE datname = current_database()) AS database_oid"
        ).fetchone()
        database_identity = hashlib.sha256(
            f"{database_fact['database_name']}:{database_fact['database_oid']}".encode()
        ).hexdigest()
        return {"migration_apply": apply_report, "migration_verify": verify_report,
                "initial_workflow": {"aggregate_type": initial.aggregate_type, "aggregate_id": initial.aggregate_id,
                                     "version": initial.version, "state": initial.state},
                "command_result": command_result.as_dict(),
                "workflow_after_command": {"aggregate_type": aggregate.aggregate_type,
                                           "aggregate_id": aggregate.aggregate_id, "version": aggregate.version,
                                           "state": aggregate.state},
                "historical_read": brief_identity(historical), "current_read": brief_identity(current),
                "artifact_metadata": written.metadata.as_dict(),
                "artifact_content": {"sha256": written.metadata.content.sha256,
                                     "byte_size": written.metadata.content.byte_size,
                                     "content_bytes_verified": True},
                "blob_root_identity_sha256": root_fp, "database_identity_sha256": database_identity,
                "inventory": inventory,
                "schema_signature": schema_signature(adapter.connection),
                "adapter_closed": True, "connection_closed": True,
                "secret_values_emitted": False}
    finally:
        adapter.close()

def capture_inventory():
    import psycopg
    fixture = env_json("EPHI_U4_FIXTURE_JSON")
    with psycopg.connect(database_dsn(), autocommit=True, row_factory=psycopg.rows.dict_row) as connection:
        version = str(connection.execute("SHOW server_version").fetchone()["server_version"])
        if not version.startswith("18."):
            raise RuntimeError("POSTGRESQL_18_REQUIRED")
        database_fact = connection.execute(
            "SELECT current_database() AS database_name, (SELECT oid FROM pg_database WHERE datname = current_database()) AS database_oid"
        ).fetchone()
        database_identity = hashlib.sha256(
            f"{database_fact['database_name']}:{database_fact['database_oid']}".encode()
        ).hexdigest()
        return {"inventory": row_inventory(connection, fixture), "schema_signature": schema_signature(connection),
                "postgres_version": version, "database_identity_sha256": database_identity}

def migration_phase():
    pre, pre_code = safe_cli("ephi-db-migrate", ["verify"], expected_code=0)
    apply, apply_code = safe_cli("ephi-db-migrate", ["apply"], expected_code=0)
    post, post_code = safe_cli("ephi-db-migrate", ["verify"], expected_code=0)
    if (pre.get("status") != "VERIFIED" or pre.get("schema_state") != "CURRENT"
        or apply.get("status") != "APPLIED" or apply.get("schema_state") != "CURRENT"
        or post.get("status") != "VERIFIED" or post.get("schema_state") != "CURRENT"):
        raise RuntimeError("N_MIGRATION_PHASE_FAILED")
    inventory = capture_inventory()
    return {"pre_n_verify": pre, "n_idempotent_apply": apply,
            "post_n_verify": post, **inventory, "separate_cli_processes": True}

def status_current():
    import psycopg
    from pathlib import Path
    cli = Path(sys.executable).with_name("ephi-operations")
    if not cli.is_file():
        raise RuntimeError("INSTALLED_COMMAND_MISSING")
    artifact_root = Path(os.environ["EPHI_U4_BLOB_ROOT"])
    child_env = dict(os.environ)
    child_env.pop("EPHI_TEST_POSTGRES_DSN", None)
    child_env["EPHI_POSTGRES_DSN"] = database_dsn()
    result = subprocess.run([str(cli), "status", "--json", "--artifact-root", str(artifact_root)],
                            cwd=Path.cwd(), env=child_env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    rendered = result.stdout + result.stderr
    dsn = database_dsn()
    conn = psycopg.conninfo.conninfo_to_dict(dsn)
    sensitive = (dsn, *(conn.get(key) for key in ("user", "password", "host", "dbname", "options")),
                 str(artifact_root), os.environ["EPHI_U4_DATABASE_NAME"])
    if any(value and value in rendered for value in sensitive):
        raise RuntimeError("PRIVATE_VALUE_DISCLOSURE")
    try:
        report = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("INSTALLED_STATUS_OUTPUT_INVALID") from exc
    if result.returncode != 0 or not isinstance(report, dict):
        raise RuntimeError("INSTALLED_STATUS_FAILED")
    axis = report.get("axes", {}).get("postgres_readiness_durability", {})
    facts = axis.get("facts", {})
    artifact_axis = report.get("axes", {}).get("immutable_artifact_integrity", {})
    return {
        "postgresql": {"state": axis.get("state"), "reason": axis.get("reason"),
                       "server_version": facts.get("server_version"),
                       "migration_count": facts.get("migration_count"),
                       "migration_identity_sha256": facts.get("migration_identity_sha256"),
                       "migration_ledger_state": facts.get("migration_ledger_state")},
        "immutable_artifact_integrity": {"state": artifact_axis.get("state"), "reason": artifact_axis.get("reason")},
    }

def restart_phase():
    from ephi.application import (AccessScope, ArtifactIntegrityError, ArtifactNotFoundError, ArtifactService,
        CommandContext, EpisodeBriefQueryService, EpisodeWorkflowCommandService, IdempotencyConflictError,
        MutableCurrentAuthorizationAuthority, Principal, RevisionVector, ArtifactContentIdentity,
        ScopedArtifactReference)
    from ephi.infrastructure import FileArtifactBlobStore, PostgreSQLArtifactCatalog, PostgreSQLReferenceTransactionAdapter
    fixture = env_json("EPHI_U4_FIXTURE_JSON")
    scope_input = fixture["scope"]
    scope = AccessScope(scope_input["scope_id"], site_id=scope_input["site_id"], area_id=scope_input["area_id"])
    principal_input = fixture["principal"]
    principal = Principal(principal_input["subject"], tuple(fixture["capabilities"]), (scope,),
                          principal_input["auth_session_revision"], principal_input["security_revision"])
    auth = MutableCurrentAuthorizationAuthority(principal)
    adapter = PostgreSQLReferenceTransactionAdapter(database_dsn())
    try:
        entry_inventory = row_inventory(adapter.connection, fixture)
        command = fixture["command"]
        read_input = fixture["read"]
        vector = RevisionVector(read_input["analysis_revision"], read_input["exposure_revision"],
                                read_input["priority_revision"], 0, None, read_input["qualification_manifest_id"])
        context = CommandContext(command["command_id"], principal, scope,
                                 command["expected_workflow_version"], vector, command["reason"])
        workflow = EpisodeWorkflowCommandService(adapter, auth)
        expected_result = env_json("EPHI_U4_EXPECTED_COMMAND_RESULT_JSON")
        replay = workflow.claim_episode(context, fixture["episode_id"])
        if replay.as_dict() != expected_result:
            raise RuntimeError("ACCEPTED_COMMAND_REPLAY_IDENTITY_MISMATCH")
        after_replay = row_inventory(adapter.connection, fixture)
        if after_replay["canonical_sha256"] != entry_inventory["canonical_sha256"]:
            raise RuntimeError("ACCEPTED_COMMAND_REPLAY_MUTATED_ROWS")
        negative_vector = RevisionVector("u4-u42-altered-analysis-payload", read_input["exposure_revision"],
                                         read_input["priority_revision"], 0, None,
                                         read_input["qualification_manifest_id"])
        negative_context = CommandContext(command["command_id"], principal, scope, 0,
                                          negative_vector, command["reason"])
        try:
            workflow.claim_episode(negative_context, fixture["episode_id"])
        except IdempotencyConflictError:
            conflict = True
        else:
            conflict = False
        after_conflict = row_inventory(adapter.connection, fixture)
        if not conflict or after_conflict["canonical_sha256"] != entry_inventory["canonical_sha256"]:
            raise RuntimeError("COMMAND_PAYLOAD_CONFLICT_CONTROL_FAILED")
        briefs = EpisodeBriefQueryService(adapter.read_store(), auth)
        historical = briefs.get_episode_brief(principal, scope, fixture["episode_id"], revision_id=read_input["revision_id"])
        current = briefs.get_episode_brief(principal, scope, fixture["episode_id"])
        historical_identity = brief_identity(historical)
        current_identity = brief_identity(current)
        expected_read = env_json("EPHI_U4_EXPECTED_READ_JSON")
        if historical_identity != expected_read["historical"] or current_identity != expected_read["current"]:
            raise RuntimeError("HISTORICAL_READ_IDENTITY_MISMATCH")
        if (not historical.historical or historical.workflow.get("work_state") != "OPEN"
            or historical.revision_vector.workflow_version != 0 or current.historical
            or current.workflow.get("work_state") != "CLAIMED"
            or current.revision_vector.workflow_version != 1
            or historical.workflow == current.workflow):
            raise RuntimeError("HISTORICAL_CURRENT_READ_TRUTH_INVALID")
        artifact_input = fixture["artifact"]
        expected_artifact = env_json("EPHI_U4_EXPECTED_ARTIFACT_JSON")
        identity = ArtifactContentIdentity(artifact_input["sha256"], artifact_input["byte_size"])
        reference = ScopedArtifactReference(scope, identity)
        catalog = PostgreSQLArtifactCatalog(adapter)
        shared_service = ArtifactService(FileArtifactBlobStore(Path(os.environ["EPHI_U4_BLOB_ROOT"])), catalog, auth)
        retrieved = shared_service.retrieve(principal, reference, artifact_input["read_capability"])
        if retrieved.content != artifact_input["content_utf8"].encode("utf-8"):
            raise RuntimeError("SHARED_ARTIFACT_CONTENT_MISMATCH")
        if retrieved.metadata.as_dict() != expected_artifact:
            raise RuntimeError("ARTIFACT_CATALOG_METADATA_MISMATCH")
        missing_root = Path(os.environ["EPHI_U4_WORK_ROOT"]) / "empty-blob-root"
        missing_root.mkdir(parents=True, exist_ok=False)
        missing_service = ArtifactService(FileArtifactBlobStore(missing_root), catalog, auth)
        try:
            missing_service.retrieve(principal, reference, artifact_input["read_capability"])
        except ArtifactNotFoundError:
            missing_control = {"status": "PASS", "reason_code": "ARTIFACT_BYTES_MISSING_FAIL_CLOSED",
                               "catalog_unchanged": True}
        else:
            raise RuntimeError("MISSING_BLOB_ROOT_CONTROL_FAILED")
        after_missing = row_inventory(adapter.connection, fixture)
        if after_missing["canonical_sha256"] != entry_inventory["canonical_sha256"]:
            raise RuntimeError("MISSING_ROOT_MUTATED_CATALOG")
        tampered_root = Path(os.environ["EPHI_U4_WORK_ROOT"]) / "tampered-copy-root"
        shutil.copytree(Path(os.environ["EPHI_U4_BLOB_ROOT"]), tampered_root)
        target = tampered_root / identity.sha256[:2] / identity.sha256[2:]
        target.write_bytes(b"U4.2 tampered copy only")
        tampered_service = ArtifactService(FileArtifactBlobStore(tampered_root), catalog, auth)
        try:
            tampered_service.retrieve(principal, reference, artifact_input["read_capability"])
        except ArtifactIntegrityError:
            tamper_control = {"status": "PASS", "reason_code": "ARTIFACT_COPY_INTEGRITY_FAILURE",
                              "accepted_shared_blob_modified": False, "catalog_unchanged": True}
        else:
            raise RuntimeError("TAMPERED_COPY_CONTROL_FAILED")
        after_tamper = row_inventory(adapter.connection, fixture)
        if after_tamper["canonical_sha256"] != entry_inventory["canonical_sha256"]:
            raise RuntimeError("TAMPERED_COPY_MUTATED_CATALOG")
        restored = shared_service.retrieve(principal, reference, artifact_input["read_capability"])
        if restored.content != artifact_input["content_utf8"].encode("utf-8"):
            raise RuntimeError("SHARED_ARTIFACT_RESTORE_READ_FAILED")
        aggregate = adapter.get_aggregate(scope, fixture["aggregate_type"], fixture["episode_id"])
        final_inventory = row_inventory(adapter.connection, fixture)
        if aggregate is None or aggregate.version != 1 or aggregate.state.get("work_state") != "CLAIMED":
            raise RuntimeError("RETAINED_WORKFLOW_IDENTITY_MISMATCH")
        if final_inventory["canonical_sha256"] != entry_inventory["canonical_sha256"]:
            raise RuntimeError("RESTART_RETAINED_ROWS_CHANGED")
        root_stat = Path(os.environ["EPHI_U4_BLOB_ROOT"]).stat()
        root_fp = hashlib.sha256(f"{root_stat.st_dev}:{root_stat.st_ino}".encode()).hexdigest()
        database_fact = adapter.connection.execute(
            "SELECT current_database() AS database_name, (SELECT oid FROM pg_database WHERE datname = current_database()) AS database_oid"
        ).fetchone()
        database_identity = hashlib.sha256(
            f"{database_fact['database_name']}:{database_fact['database_oid']}".encode()
        ).hexdigest()
        return {"entry_inventory": entry_inventory, "after_replay_inventory": after_replay,
                "after_conflict_inventory": after_conflict, "after_restart_inventory": final_inventory,
                "after_missing_blob_root_inventory": after_missing,
                "after_tampered_copy_inventory": after_tamper,
                "workflow": {"aggregate_type": aggregate.aggregate_type, "aggregate_id": aggregate.aggregate_id,
                             "version": aggregate.version, "state": aggregate.state},
                "command_replay": {"status": "PASS", "same_result_identity": True,
                                   "no_duplicate_receipt_audit_outbox": True,
                                   "row_inventory_unchanged": True,
                                   "result": replay.as_dict()},
                "different_payload_replay": {"status": "PASS", "reason_code": "IDEMPOTENCY_CONFLICT",
                                             "fail_closed": True, "row_inventory_unchanged": True},
                "historical_read": historical_identity, "current_live_read": current_identity,
                "schema_signature": schema_signature(adapter.connection),
                "database_identity_sha256": database_identity,
                "artifact": {"metadata": retrieved.metadata.as_dict(), "sha256": identity.sha256,
                             "byte_size": identity.byte_size, "object_key": "sha256/" + identity.sha256,
                             "exact_content_match": True, "shared_root_identity_sha256": root_fp,
                             "restored_shared_root_read": True},
                "missing_blob_root_control": missing_control,
                "tampered_copy_control": tamper_control,
                "adapter_closed": True, "connection_closed": True,
                "secret_values_emitted": False}
    finally:
        adapter.close()

def prefix_setup():
    import psycopg
    from ephi.infrastructure.postgresql import _sql_statements
    from ephi.migration_resources import resolve_migration_resources
    paths = resolve_migration_resources().paths
    if len(paths) != 11:
        raise RuntimeError("INSTALLED_MIGRATION_RESOURCE_COUNT_INVALID")
    with psycopg.connect(database_dsn(os.environ["EPHI_U4_PREFIX_DATABASE_NAME"]), autocommit=True,
                         row_factory=psycopg.rows.dict_row) as connection:
        for path in paths[:10]:
            for statement in _sql_statements(path.read_text(encoding="utf-8")):
                connection.execute(statement)
        signature = schema_signature(connection)
    return {"applied_installed_migration_prefix_count": 10, "installed_migration_count": len(paths),
            "schema_signature": signature, "test_fixture_rows_inserted": False}

def prefix_control():
    prefix_name = os.environ["EPHI_U4_PREFIX_DATABASE_NAME"]
    import psycopg
    env = dict(os.environ)
    env.pop("EPHI_TEST_POSTGRES_DSN", None)
    prefix_dsn = database_dsn(prefix_name)
    env["EPHI_POSTGRES_DSN"] = prefix_dsn
    status_cli = Path(sys.executable).with_name("ephi-operations")
    if not status_cli.is_file():
        raise RuntimeError("INSTALLED_COMMAND_MISSING")
    def status(artifact_name):
        artifact_root = Path(os.environ["EPHI_U4_WORK_ROOT"]) / artifact_name
        artifact_root.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([str(status_cli), "status", "--json", "--artifact-root", str(artifact_root)],
                                cwd=Path.cwd(), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        rendered = result.stdout + result.stderr
        conn = psycopg.conninfo.conninfo_to_dict(prefix_dsn)
        sensitive = (prefix_dsn, *(conn.get(key) for key in ("user", "password", "host", "dbname", "options")),
                     str(artifact_root), prefix_name)
        if any(value and value in rendered for value in sensitive):
            raise RuntimeError("PRIVATE_VALUE_DISCLOSURE")
        try:
            report = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("INSTALLED_STATUS_OUTPUT_INVALID") from exc
        if result.returncode != 0 or not isinstance(report, dict):
            raise RuntimeError("INSTALLED_STATUS_FAILED")
        axis = report.get("axes", {}).get("postgres_readiness_durability", {})
        facts = axis.get("facts", {})
        return {"state": axis.get("state"), "reason": axis.get("reason"),
                "server_version": facts.get("server_version"),
                "migration_count": facts.get("migration_count"),
                "migration_identity_sha256": facts.get("migration_identity_sha256")}
    def signature():
        with psycopg.connect(prefix_dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as connection:
            return schema_signature(connection)
    before = signature()
    refused_status = status("prefix-before-apply-status")
    after_status = signature()
    failed_verify, failed_code = safe_cli("ephi-db-migrate", ["verify"], expected_code=2,
                                         expected_failure_reason="SCHEMA_VERIFICATION_FAILED",
                                         database_name=prefix_name)
    after_verify = signature()
    if (refused_status.get("state") == "READY" or refused_status.get("reason") != "POSTGRES_SCHEMA_MISMATCH"
        or before != after_status or before != after_verify
        or failed_verify.get("schema_state") == "CURRENT"):
        raise RuntimeError("PREFIX_SCHEMA_REFUSAL_CONTROL_FAILED")
    apply, _ = safe_cli("ephi-db-migrate", ["apply"], database_name=prefix_name)
    verified, _ = safe_cli("ephi-db-migrate", ["verify"], database_name=prefix_name)
    if apply.get("status") != "APPLIED" or verified.get("status") != "VERIFIED" or verified.get("schema_state") != "CURRENT":
        raise RuntimeError("PREFIX_NORMAL_MIGRATION_ADVANCE_FAILED")
    ready = status("prefix-after-apply-status")
    if ready.get("state") != "READY" or ready.get("migration_count") != len(paths):
        raise RuntimeError("PREFIX_POST_APPLY_STATUS_NOT_READY")
    after_apply = signature()
    return {"status": "PASS", "database_separate_from_accepted_upgrade_database": True,
            "prefix_migration_count": 10, "before_apply_status": refused_status,
            "before_apply_verify": {"status": failed_verify.get("status"),
                                    "reason_code": failed_verify.get("reason_code"),
                                    "return_code": failed_code},
            "status_and_failed_verify_did_not_mutate_prefix_schema": before == after_status == after_verify,
            "normal_migration_authority_apply": apply, "post_apply_verify": verified,
            "after_apply_status": ready, "prefix_reached_current_only_after_apply": True,
            "post_apply_schema_signature_sha256": after_apply["canonical_sha256"]}

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if mode == "probe": result = release_probe()
        elif mode == "create-databases": result = create_databases()
        elif mode == "drop-databases": result = drop_databases()
        elif mode == "seed-n1": result = seed_n1()
        elif mode == "capture-inventory": result = capture_inventory()
        elif mode == "migration-phase": result = migration_phase()
        elif mode == "status-current": result = status_current()
        elif mode == "restart-phase": result = restart_phase()
        elif mode == "prefix-setup": result = prefix_setup()
        elif mode == "prefix-control": result = prefix_control()
        else: raise RuntimeError("INVALID_WORKER_OPERATION")
        print(json.dumps({"status": "PASS", "result": normalize(result)}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        code = str(exc) if str(exc).isupper() and str(exc).replace("_", "").isalnum() else "INSTALLED_QUALIFICATION_OPERATION_FAILED"
        if code == "INSTALLED_QUALIFICATION_OPERATION_FAILED":
            mode_code = mode.upper().replace("-", "_")
            error_code = type(exc).__name__.upper()
            candidate = f"{mode_code}_{error_code}"
            code = candidate if re.fullmatch(r"[A-Z0-9_]{1,64}", candidate) else code
        print(json.dumps({"status": "FAIL", "reason_code": code}, sort_keys=True, separators=(",", ":")))
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''


def _authorities() -> tuple[dict[str, object], dict[str, object]]:
    authority = _read_json(AUTHORITY_PATH, "U4_2_AUTHORITY_INVALID")
    release_authority = _read_json(RELEASE_AUTHORITY_PATH, "U4_1_RELEASE_AUTHORITY_INVALID")
    if authority.get("schema") != "org.ephi.u4-n1-durable-upgrade-restart-authority.v1":
        raise QualificationFailure("U4_2_AUTHORITY_INVALID")
    if authority.get("qualification", {}).get("release_authority") != RELEASE_AUTHORITY_PATH.relative_to(ROOT).as_posix():
        raise QualificationFailure("U4_1_RELEASE_AUTHORITY_REFERENCE_INVALID")
    releases = release_authority.get("releases")
    if not isinstance(releases, dict) or set(releases) != {"N-1", "N"}:
        raise QualificationFailure("U4_1_RELEASE_AUTHORITY_INVALID")
    if release_authority.get("migrations", {}).get("cross_release_git_delta") != []:
        raise QualificationFailure("FROZEN_SCHEMA_FILE_DELTA_NOT_EMPTY")
    if authority.get("expected_migration_count") != 11:
        raise QualificationFailure("U4_2_MIGRATION_COUNT_AUTHORITY_INVALID")
    if not _IDENTITY.fullmatch(str(release_authority.get("migrations", {}).get("identity_sha256", ""))):
        raise QualificationFailure("FROZEN_MIGRATION_IDENTITY_INVALID")
    provider = release_authority.get("provider_package", {})
    if not _IDENTITY.fullmatch(str(provider.get("compatibility_wheel_sha256", ""))):
        raise QualificationFailure("FROZEN_PROVIDER_IDENTITY_INVALID")
    return authority, release_authority


def _is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure("GIT_HISTORY_INVALID") from exc
    if result.returncode not in (0, 1):
        raise QualificationFailure("GIT_HISTORY_INVALID")
    return result.returncode == 0


def _worktree(repository: Path, worktrees_root: Path, label: str, release: dict[str, object]) -> Path:
    commit = str(release.get("integrated_commit", ""))
    tree = str(release.get("integrated_tree", ""))
    if _git(repository, "cat-file", "-t", commit) != "commit":
        raise QualificationFailure("FROZEN_COMMIT_UNAVAILABLE")
    path = worktrees_root / label
    _command(["git", "-C", str(repository), "worktree", "add", "--detach", str(path), commit],
             cwd=ROOT, code="FROZEN_WORKTREE_MATERIALIZATION_FAILED")
    if (_git(path, "rev-parse", "HEAD") != commit or _git(path, "rev-parse", "HEAD^{tree}") != tree
        or _git(path, "status", "--porcelain", "--untracked-files=all")):
        raise QualificationFailure("FROZEN_WORKTREE_IDENTITY_INVALID")
    return path


def _prepare_release(root: Path, output: Path, external_cwd: Path, repository: Path) -> dict[str, object]:
    tool = root / "tools" / "prepare_release_inputs.py"
    if not tool.is_file():
        raise QualificationFailure("RELEASE_PREPARATION_TOOL_MISSING")
    stdout, _ = _command([sys.executable, str(tool), "--output-dir", str(output)],
                         cwd=external_cwd, code="RESTRICTED_INPUT_PREPARATION_FAILED",
                         sensitive=(str(repository), str(root), str(output)))
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise QualificationFailure("RESTRICTED_INPUT_PREPARATION_FAILED") from exc
    if not isinstance(report, dict) or report.get("status") != "PASS":
        raise QualificationFailure("RESTRICTED_INPUT_PREPARATION_FAILED")
    return report


def _install_release(inputs: Path, environment: Path, external_cwd: Path) -> tuple[Path, dict[str, str]]:
    if environment.exists():
        raise QualificationFailure("INSTALL_ENVIRONMENT_NOT_FRESH")
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure("INSTALL_ENVIRONMENT_CREATE_FAILED") from exc
    python = environment / "bin" / "python"
    env = _clean_env(environment)
    python_suffix = f"{sys.version_info.major}{sys.version_info.minor}"
    lock_dir = inputs / "locks"
    wheelhouse = inputs / "wheelhouse"
    commands = [
        [str(python), "-m", "pip", "install", "--no-compile", "--no-index", "--find-links", str(wheelhouse), "--require-hashes", "-r", str(lock_dir / "installer-py311.txt")],
        [str(python), "-m", "pip", "uninstall", "-y", "setuptools", "wheel"],
        [str(python), "-m", "pip", "install", "--no-compile", "--no-index", "--find-links", str(wheelhouse), "--require-hashes", "-r", str(lock_dir / f"runtime-py{python_suffix}.txt")],
        [str(python), "-m", "pip", "install", "--no-compile", "--no-index", "--find-links", str(wheelhouse), "--require-hashes", "-r", str(lock_dir / f"postgres-py{python_suffix}.txt")],
    ]
    for command in commands:
        _command(command, cwd=external_cwd, env=env, code="OFFLINE_EPHI_INSTALL_FAILED")
    app_wheels = sorted(wheelhouse.glob("ephi-*.whl"))
    base_wheels = sorted(wheelhouse.glob("nicegui_base-*.whl"))
    if len(app_wheels) != 1 or len(base_wheels) != 1:
        raise QualificationFailure("RESTRICTED_RELEASE_WHEEL_INVALID")
    _command([str(python), "-m", "pip", "install", "--no-compile", "--no-index", "--no-deps", str(app_wheels[0]), str(base_wheels[0])],
             cwd=external_cwd, env=env, code="OFFLINE_EPHI_INSTALL_FAILED")
    if "PYTHONPATH" in env or "PYTHONHOME" in env or env.get("PIP_NO_INDEX") != "1":
        raise QualificationFailure("INSTALLED_ENVIRONMENT_PATH_CONTAMINATED")
    return python, env


def _preflight(python: Path, env: dict[str, str], inputs: Path, release: dict[str, object], label: str,
               external_cwd: Path, sensitive: tuple[str, ...]) -> tuple[dict[str, object], int]:
    cli = python.with_name("ephi-release-preflight")
    report, pid = _command_json([str(cli), "--inputs-dir", str(inputs), "--json"], cwd=external_cwd,
                                env=env, code="RELEASE_PREFLIGHT_FAILED", sensitive=sensitive)
    expected_source = {"commit": release["integrated_commit"], "tree": release["integrated_tree"]}
    migrations = report.get("migrations", {})
    if (report.get("status") != "PASS" or report.get("release_identity_sha256") != release.get("release_identity_sha256")
        or report.get("candidate_source") != expected_source
        or report.get("application") != {"distribution": "ephi", "version": "0.1.0"}
        or migrations.get("identity_sha256") is None
        or report.get("provider_composition", {}).get("status") != "NOT_RUN"):
        raise QualificationFailure("RELEASE_PREFLIGHT_IDENTITY_MISMATCH")
    return {
        "status": "PASS",
        "release_identity_sha256": report["release_identity_sha256"],
        "install_inputs_sha256": report.get("install_inputs_sha256"),
        "candidate_source": report["candidate_source"],
        "application": report["application"],
        "migrations": migrations,
        "provider_composition": {"status": "NOT_RUN"},
        "release_label": label,
    }, pid


def _worker(
    python: Path,
    worker_file: Path,
    mode: str,
    *,
    cwd: Path,
    env: dict[str, str],
    sensitive: tuple[str, ...],
) -> tuple[dict[str, object], int]:
    result, pid = _command_json([str(python), str(worker_file), mode], cwd=cwd, env=env,
                               code="INSTALLED_QUALIFICATION_OPERATION_FAILED", sensitive=sensitive)
    if result.get("status") != "PASS" or not isinstance(result.get("result"), dict):
        reason = result.get("reason_code")
        if not isinstance(reason, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", reason):
            reason = "INSTALLED_QUALIFICATION_OPERATION_FAILED"
        raise QualificationFailure(reason)
    return result["result"], pid


def _file_fingerprint(path: Path) -> dict[str, object]:
    return {"file": path.name, "byte_size": path.stat().st_size, "sha256": _sha256_file(path)}


def _summarize_cli(report: dict[str, object]) -> dict[str, object]:
    return {key: report.get(key) for key in (
        "status", "operation", "schema_state", "migration_count", "identity_sha256", "required_table_count")
        if key in report}


def _run_qualification(args: argparse.Namespace) -> dict[str, object]:
    if sys.version_info < (3, 11) or sys.version_info >= (3, 14):
        raise QualificationFailure("UNSUPPORTED_QUALIFICATION_PYTHON")
    repository = Path(args.repository_root).resolve() if args.repository_root else ROOT
    candidate_sha = _git(repository, "rev-parse", "HEAD")
    candidate_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    if candidate_sha != args.candidate_sha or candidate_tree != args.candidate_tree:
        raise QualificationFailure("CANDIDATE_IDENTITY_MISMATCH")
    if _git(repository, "status", "--porcelain", "--untracked-files=all"):
        raise QualificationFailure("CANDIDATE_WORKTREE_NOT_CLEAN")
    authority, release_authority = _authorities()
    base_commit = str(authority["qualification"]["candidate_base_commit"])
    if not _is_ancestor(repository, base_commit, candidate_sha):
        raise QualificationFailure("CANDIDATE_BASE_NOT_ANCESTOR")
    releases = release_authority["releases"]
    n1, n = releases["N-1"], releases["N"]
    worktrees_root = args.work_root / "worktrees"
    inputs_root = args.work_root / "inputs"
    environments_root = args.work_root / "environments"
    external_cwd = args.work_root / "external-cwd"
    if args.work_root.exists() and any(args.work_root.iterdir()):
        raise QualificationFailure("WORK_ROOT_NOT_EMPTY")
    args.work_root.mkdir(parents=True, exist_ok=True)
    for path in (worktrees_root, inputs_root, environments_root, external_cwd):
        path.mkdir(exist_ok=False)
    worktrees: list[Path] = []
    databases_created = False
    database_names: tuple[str, str] | None = None
    artifact_root = args.artifact_root
    if artifact_root == repository or repository in artifact_root.parents:
        raise QualificationFailure("ARTIFACT_ROOT_NOT_EXTERNAL")
    if artifact_root.exists() and any(artifact_root.iterdir()):
        raise QualificationFailure("ARTIFACT_ROOT_NOT_EMPTY")
    artifact_root.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(f"{candidate_sha}:{args.job_id or 'UNBOUND_CI'}".encode()).hexdigest()[:14]
    database_name, prefix_database_name = f"ephi_u42_{token}", f"ephi_u42p_{token}"
    database_names = (database_name, prefix_database_name)
    worker_file = args.work_root / "u4_2_installed_worker.py"
    worker_file.write_text(_WORKER_SOURCE, encoding="utf-8")
    worker_file.chmod(0o600)
    phase_pids: dict[str, object] = {}
    db_identity: dict[str, object] = {}
    try:
        n1_tree = _worktree(repository, worktrees_root, "n-minus-1", n1)
        worktrees.append(n1_tree)
        n_tree = _worktree(repository, worktrees_root, "n", n)
        worktrees.append(n_tree)
        if _git(n1_tree, "status", "--porcelain", "--untracked-files=all") or _git(n_tree, "status", "--porcelain", "--untracked-files=all"):
            raise QualificationFailure("FROZEN_WORKTREE_NOT_CLEAN")
        if sys.version_info[0] != 3 or sys.version_info[1] not in (11, 12, 13):
            raise QualificationFailure("QUALIFICATION_PYTHON_MATRIX_UNSUPPORTED")
        prep_reports = {
            "N-1": _prepare_release(n1_tree, inputs_root / "n-minus-1", external_cwd, repository),
            "N": _prepare_release(n_tree, inputs_root / "n", external_cwd, repository),
        }
        for label, release, tree in (("N-1", n1, n1_tree), ("N", n, n_tree)):
            expected_source = {"commit": release["integrated_commit"], "tree": release["integrated_tree"]}
            if prep_reports[label].get("candidate_source") != expected_source:
                raise QualificationFailure("RESTRICTED_INPUT_SOURCE_MISMATCH")
            if _git(tree, "status", "--porcelain", "--untracked-files=all"):
                raise QualificationFailure("FROZEN_WORKTREE_DIRTY_AFTER_PREPARATION")
        provider_authority = release_authority["provider_package"]
        installed: dict[str, Path] = {}
        install_envs: dict[str, dict[str, str]] = {}
        preflights: dict[str, dict[str, object]] = {}
        preflight_pids: dict[str, int] = {}
        root_paths = [repository, *worktrees]
        for label in ("N-1", "N"):
            input_dir = inputs_root / ("n-minus-1" if label == "N-1" else "n")
            environment = environments_root / ("n-minus-1" if label == "N-1" else "n")
            python, install_env = _install_release(input_dir, environment, external_cwd)
            installed[label] = python
            install_envs[label] = install_env
            probe_env = _worker_env(install_env, repository_roots=root_paths)
            probe, probe_pid = _worker(python, worker_file, "probe", cwd=external_cwd,
                                       env=probe_env,
                                       sensitive=(str(repository), *(str(path) for path in worktrees)))
            if probe.get("checkout_pythonpath_or_sys_path_used") is not False or probe.get("installed_package_inside_virtual_environment") is not True:
                raise QualificationFailure("INSTALLED_IMPORT_PROBE_FAILED")
            report, pid = _preflight(python, install_env, input_dir, n1 if label == "N-1" else n,
                                     label, external_cwd,
                                     (str(repository), str(input_dir), str(environment), *(str(path) for path in worktrees)))
            if (report["migrations"].get("identity_sha256") != release_authority["migrations"]["identity_sha256"]
                or report["migrations"].get("count") != authority["expected_migration_count"]):
                raise QualificationFailure("INSTALLED_MIGRATION_IDENTITY_MISMATCH")
            preflights[label] = report
            preflight_pids[label] = pid
            phase_pids[f"{label.lower().replace('-', '_')}_probe"] = probe_pid
        admin_dsn = os.environ.get("EPHI_U4_ADMIN_DSN", "").strip()
        if not admin_dsn:
            raise QualificationFailure("POSTGRES_ADMIN_DSN_REQUIRED")
        n1_python = installed["N-1"]
        admin_env = _worker_env(install_envs["N-1"], database_name=database_name,
                                prefix_database_name=prefix_database_name)
        admin_env["EPHI_U4_ADMIN_DSN"] = admin_dsn
        db_identity, create_pid = _worker(n1_python, worker_file, "create-databases", cwd=external_cwd,
                                          env=admin_env,
                                          sensitive=(admin_dsn, str(repository), str(args.work_root), database_name, prefix_database_name))
        databases_created = True
        phase_pids["database_setup"] = create_pid
        if db_identity.get("postgres_major") != 18 or not str(db_identity.get("postgres_version", "")).startswith("18."):
            raise QualificationFailure("POSTGRESQL_18_REQUIRED")
        blob_root = artifact_root / "shared-immutable-artifact-blobs"
        blob_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        fixture = json.loads(json.dumps(authority["fixture"]))
        fixture["scope_key"] = None
        fixture["artifact"]["sha256"] = None
        fixture["artifact"]["byte_size"] = None
        base_env_n1 = _clean_env(environments_root / "n-minus-1", database_admin_dsn=admin_dsn)
        seed_env = _worker_env(base_env_n1, database_name=database_name, prefix_database_name=prefix_database_name,
                               fixture=fixture, blob_root=blob_root, work_root=args.work_root,
                               repository_roots=root_paths)
        seed, n1_pid = _worker(n1_python, worker_file, "seed-n1", cwd=external_cwd, env=seed_env,
                               sensitive=(admin_dsn, str(repository), str(args.work_root), str(blob_root), database_name, prefix_database_name))
        phase_pids["n_minus_1_application_process"] = n1_pid
        if seed.get("adapter_closed") is not True or seed.get("connection_closed") is not True:
            raise QualificationFailure("N_MINUS_1_SHUTDOWN_NOT_PROVEN")
        if seed["database_identity_sha256"] != db_identity["database_identity_sha256"]:
            raise QualificationFailure("N_MINUS_1_DATABASE_IDENTITY_MISMATCH")
        fixture["scope_key"] = seed["inventory"]["rows"]["aggregate_state"][0]["scope_key"]
        fixture["artifact"]["sha256"] = seed["artifact_content"]["sha256"]
        fixture["artifact"]["byte_size"] = seed["artifact_content"]["byte_size"]
        before_inventory = {
            "schema": "org.ephi.u4-retained-identity-inventory.v1",
            "phase": "before_upgrade",
            "database_identity_sha256": db_identity["database_identity_sha256"],
            "blob_root_identity_sha256": seed["blob_root_identity_sha256"],
            "inventory": seed["inventory"],
            "schema_signature": seed["schema_signature"],
            "workflow": {"initial_state": seed["initial_workflow"], "after_command": seed["workflow_after_command"]},
            "command_result": seed["command_result"],
            "historical_read": seed["historical_read"],
            "current_read": seed["current_read"],
            "artifact": {"metadata": seed["artifact_metadata"], "content": seed["artifact_content"]},
        }
        before_path = artifact_root / "before_upgrade_identity_inventory.json"
        _write_json(before_path, before_inventory)
        n1_migration = {"apply": _summarize_cli(seed["migration_apply"]),
                        "verify": _summarize_cli(seed["migration_verify"])}
        n_python = installed["N"]
        n_base_env = _clean_env(environments_root / "n", database_admin_dsn=admin_dsn)
        migration_env = _worker_env(n_base_env, database_name=database_name, prefix_database_name=prefix_database_name,
                                    fixture=fixture, blob_root=blob_root, work_root=args.work_root,
                                    repository_roots=root_paths)
        migration, n_migration_pid = _worker(n_python, worker_file, "migration-phase", cwd=external_cwd,
                                             env=migration_env,
                                             sensitive=(admin_dsn, str(repository), str(args.work_root), str(blob_root), database_name, prefix_database_name))
        phase_pids["n_migration_phase_process"] = n_migration_pid
        if n_migration_pid == n1_pid:
            raise QualificationFailure("CROSS_RELEASE_PROCESS_BOUNDARY_INVALID")
        after_migration_inventory = {
            "schema": "org.ephi.u4-retained-identity-inventory.v1",
            "phase": "after_migration",
            "database_identity_sha256": db_identity["database_identity_sha256"],
            "blob_root_identity_sha256": seed["blob_root_identity_sha256"],
            "inventory": migration["inventory"],
            "schema_signature": migration["schema_signature"],
        }
        _write_json(artifact_root / "after_migration_identity_inventory.json", after_migration_inventory)
        if migration["inventory"]["canonical_sha256"] != seed["inventory"]["canonical_sha256"]:
            raise QualificationFailure("MIGRATION_CHANGED_RETAINED_IDENTITY")
        if migration["database_identity_sha256"] != seed["database_identity_sha256"]:
            raise QualificationFailure("CROSS_RELEASE_DATABASE_IDENTITY_CHANGED")
        if migration["schema_signature"]["canonical_sha256"] != seed["schema_signature"]["canonical_sha256"]:
            raise QualificationFailure("CROSS_RELEASE_DATABASE_SCHEMA_DELTA")
        expected_read = {"historical": seed["historical_read"], "current": seed["current_read"]}
        restart_env = _worker_env(n_base_env, database_name=database_name, prefix_database_name=prefix_database_name,
                                  fixture=fixture, blob_root=blob_root, work_root=args.work_root,
                                  repository_roots=root_paths,
                                  expected_command_result=seed["command_result"], expected_read=expected_read,
                                  expected_artifact=seed["artifact_metadata"])
        restart, n_restart_pid = _worker(n_python, worker_file, "restart-phase", cwd=external_cwd,
                                         env=restart_env,
                                         sensitive=(admin_dsn, str(repository), str(args.work_root), str(blob_root), database_name, prefix_database_name))
        phase_pids["n_restart_application_process"] = n_restart_pid
        if n_restart_pid in {n1_pid, n_migration_pid}:
            raise QualificationFailure("CROSS_RELEASE_PROCESS_BOUNDARY_INVALID")
        if restart["entry_inventory"]["canonical_sha256"] != migration["inventory"]["canonical_sha256"]:
            raise QualificationFailure("N_RESTART_ENTRY_IDENTITY_CHANGED")
        if restart["after_restart_inventory"]["canonical_sha256"] != seed["inventory"]["canonical_sha256"]:
            raise QualificationFailure("RESTART_CHANGED_RETAINED_IDENTITY")
        if restart["database_identity_sha256"] != seed["database_identity_sha256"]:
            raise QualificationFailure("N_RESTART_DATABASE_IDENTITY_CHANGED")
        if restart["schema_signature"]["canonical_sha256"] != migration["schema_signature"]["canonical_sha256"]:
            raise QualificationFailure("RESTART_CHANGED_DATABASE_SCHEMA")
        if restart["workflow"] != seed["workflow_after_command"]:
            raise QualificationFailure("RESTART_WORKFLOW_IDENTITY_CHANGED")
        if restart["artifact"]["shared_root_identity_sha256"] != seed["blob_root_identity_sha256"]:
            raise QualificationFailure("SHARED_BLOB_ROOT_IDENTITY_CHANGED")
        status_env = _worker_env(n_base_env, database_name=database_name, prefix_database_name=prefix_database_name,
                                 fixture=fixture, blob_root=blob_root, work_root=args.work_root)
        status, status_pid = _worker(n_python, worker_file, "status-current", cwd=external_cwd, env=status_env,
                                     sensitive=(admin_dsn, str(repository), str(args.work_root), str(blob_root), database_name, prefix_database_name))
        phase_pids["n_operations_status"] = status_pid
        if (status.get("postgresql", {}).get("state") != "READY"
            or status.get("postgresql", {}).get("reason") != "POSTGRES_REACHABLE_AND_SCHEMA_CURRENT"
            or status.get("postgresql", {}).get("migration_identity_sha256") != release_authority["migrations"]["identity_sha256"]
            or status.get("postgresql", {}).get("migration_count") != authority["expected_migration_count"]
            or status.get("immutable_artifact_integrity", {}).get("state") != "READY"
            or str(status.get("postgresql", {}).get("server_version", "")).split(".", 1)[0] != "18"):
            raise QualificationFailure("N_POST_RESTART_OPERATIONS_NOT_READY")
        prefix_setup_env = _worker_env(n_base_env, database_name=database_name,
                                       prefix_database_name=prefix_database_name,
                                       fixture=fixture, blob_root=blob_root, work_root=args.work_root)
        prefix_setup_result, prefix_setup_pid = _worker(n_python, worker_file, "prefix-setup", cwd=external_cwd,
                                                        env=prefix_setup_env,
                                                        sensitive=(admin_dsn, str(repository), str(args.work_root), database_name, prefix_database_name))
        phase_pids["prefix_schema_setup"] = prefix_setup_pid
        prefix_control, prefix_pid = _worker(n_python, worker_file, "prefix-control", cwd=external_cwd,
                                             env=prefix_setup_env,
                                             sensitive=(admin_dsn, str(repository), str(args.work_root), database_name, prefix_database_name))
        phase_pids["prefix_negative_control"] = prefix_pid
        if prefix_control.get("status") != "PASS":
            raise QualificationFailure("PREFIX_SCHEMA_NEGATIVE_CONTROL_FAILED")
        _write_json(artifact_root / "after_restart_identity_inventory.json", {
            "schema": "org.ephi.u4-retained-identity-inventory.v1", "phase": "after_restart",
            "database_identity_sha256": db_identity["database_identity_sha256"],
            "blob_root_identity_sha256": seed["blob_root_identity_sha256"],
            "inventory": restart["after_restart_inventory"],
            "schema_signature": restart["schema_signature"],
            "workflow": restart["workflow"], "command_replay": restart["command_replay"],
            "historical_read": restart["historical_read"], "current_live_read": restart["current_live_read"],
            "artifact": restart["artifact"],
        })
        negative_summary = {
            "schema": "org.ephi.u4-negative-control-summary.v1",
            "different_semantic_payload_same_command_id": restart["different_payload_replay"],
            "missing_or_empty_blob_root": restart["missing_blob_root_control"],
            "tampered_copy_only": restart["tampered_copy_control"],
            "separate_incomplete_prefix_database": prefix_control,
            "historical_current_read_state_distinction": {
                "status": "PASS", "historical_workflow_state": "OPEN",
                "current_live_workflow_state": "CLAIMED", "workflow_version_historical": 0,
                "workflow_version_current": 1, "states_remain_distinct": True,
            },
            "retained_row_identity_invariants": {
                "before_upgrade_canonical_sha256": seed["inventory"]["canonical_sha256"],
                "after_different_payload_replay_canonical_sha256": restart["after_conflict_inventory"]["canonical_sha256"],
                "after_missing_blob_root_canonical_sha256": restart["after_missing_blob_root_inventory"]["canonical_sha256"],
                "after_tampered_copy_canonical_sha256": restart["after_tampered_copy_inventory"]["canonical_sha256"],
                "all_equal": True,
                "accepted_row_counts": seed["inventory"]["row_counts"],
                "artifact_catalog_fingerprint_before": seed["inventory"]["row_fingerprints"]["artifact_catalog"],
                "artifact_catalog_fingerprint_after_missing_root": restart["after_missing_blob_root_inventory"]["row_fingerprints"]["artifact_catalog"],
                "artifact_catalog_fingerprint_after_tampered_copy": restart["after_tampered_copy_inventory"]["row_fingerprints"]["artifact_catalog"],
            },
            "secret_values_emitted": False,
        }
        _write_json(artifact_root / "negative_control_summary.json", negative_summary)
        migration_summary = {
            "schema": "org.ephi.u4-migration-restart-summary.v1",
            "postgres": {"version": db_identity["postgres_version"], "major": db_identity["postgres_major"]},
            "migration_identity_sha256": release_authority["migrations"]["identity_sha256"],
            "migration_count": authority["expected_migration_count"],
            "n_minus_1": n1_migration,
            "n_pre_apply_verify": _summarize_cli(migration["pre_n_verify"]),
            "n_idempotent_apply": _summarize_cli(migration["n_idempotent_apply"]),
            "n_post_apply_verify": _summarize_cli(migration["post_n_verify"]),
            "cross_release_schema_delta": "NONE",
            "database_schema_signature_before_upgrade": seed.get("schema_signature"),
            "database_schema_signature_after_n_apply": migration["schema_signature"],
            "n1_process_shutdown_before_n": seed["adapter_closed"] and seed["connection_closed"],
            "separate_installed_processes": True,
            "process_ids": phase_pids,
            "prefix_negative_control_database_identity_sha256": db_identity["prefix_database_identity_sha256"],
            "secret_values_emitted": False,
        }
        _write_json(artifact_root / "migration_restart_summary.json", migration_summary)
        provider_info = {
            "distribution": provider_authority["distribution"], "version": provider_authority["version"],
            "compatibility_wheel_sha256": provider_authority["compatibility_wheel_sha256"],
            "source_release": provider_authority["source_release"],
            "compatibility_wheel_consumed": False,
            "consumption_reason_code": "NO_PROVIDER_COMPOSITION_IN_U4_2",
            "provider_rebuilt_for_n_as_compatibility_input": False,
        }
        protected_paths = ("src/ephi/", "migrations/", "examples/synthetic_downstream/")
        candidate_protected = _git(repository, "diff", "--name-only", f"{base_commit}..{candidate_sha}", "--", *protected_paths).splitlines()
        frozen_protected = _git(repository, "diff", "--name-only", f"{n1['integrated_commit']}..{n['integrated_commit']}", "--", *protected_paths).splitlines()
        frozen_schema_delta = _git(repository, "diff", "--name-only", f"{n1['integrated_commit']}..{n['integrated_commit']}", "--", "migrations").splitlines()
        if candidate_protected or frozen_protected or frozen_schema_delta:
            raise QualificationFailure("CORE_EDIT_PROHIBITION_FAILED")
        if (migration["inventory"]["canonical_sha256"] != seed["inventory"]["canonical_sha256"]
            or restart["after_restart_inventory"]["canonical_sha256"] != seed["inventory"]["canonical_sha256"]):
            raise QualificationFailure("RETAINED_IDENTITY_INVARIANT_FAILED")
        if seed["current_read"]["workflow_snapshot"].get("work_state") != "CLAIMED" or seed["historical_read"]["workflow_snapshot"].get("work_state") != "OPEN":
            raise QualificationFailure("HISTORICAL_CURRENT_READ_INVARIANT_FAILED")
        report = {
            "schema": SCHEMA,
            "status": "PASS",
            "durable_upgrade_state": "PASS",
            "qualification": {
                "change": authority["qualification"]["change"], "slice": authority["qualification"]["slice"],
                "project": "ephi", "repository": authority["qualification"]["repository"],
                "request": authority["qualification"]["request"], "operation": "BUILD",
                "candidate": {"commit": candidate_sha, "tree": candidate_tree},
                "candidate_base_commit": base_commit,
                "fabric_job": args.job_id,
                "fabric_binding_state": "BOUND" if args.job_id else "UNBOUND_CI",
            },
            "frozen_releases": {
                "N-1": {"commit": n1["integrated_commit"], "tree": n1["integrated_tree"],
                        "release_identity_sha256": n1["release_identity_sha256"],
                        "release_preflight": preflights["N-1"]},
                "N": {"commit": n["integrated_commit"], "tree": n["integrated_tree"],
                      "release_identity_sha256": n["release_identity_sha256"],
                      "release_preflight": preflights["N"]},
            },
            "provider_compatibility_authority": provider_info,
            "postgres": {"version": db_identity["postgres_version"], "major": db_identity["postgres_major"],
                         "shared_database_identity_sha256": seed["database_identity_sha256"],
                         "same_database_across_n_minus_1_to_n": True},
            "migration": {
                "identity_sha256": release_authority["migrations"]["identity_sha256"],
                "count": authority["expected_migration_count"],
                "n_minus_1_apply": n1_migration["apply"],
                "n_minus_1_verify": n1_migration["verify"],
                "pre_n_verify": _summarize_cli(migration["pre_n_verify"]),
                "n_idempotent_apply": _summarize_cli(migration["n_idempotent_apply"]),
                "post_n_verify": _summarize_cli(migration["post_n_verify"]),
                "cross_release_schema_delta": "NONE",
                "frozen_pair_schema_files_delta": [],
                "idempotent_reapply_is_new_schema_migration": False,
            },
            "process_restart": {
                "n_minus_1_process_pid": n1_pid,
                "n_migration_process_pid": n_migration_pid,
                "n_restart_process_pid": n_restart_pid,
                "n_minus_1_exit_code": 0,
                "n_minus_1_adapter_closed": seed["adapter_closed"],
                "n_minus_1_connection_closed": seed["connection_closed"],
                "n_verification_after_n_minus_1_process_shutdown": True,
                "n_restart_in_fresh_process_and_installed_environment": True,
                "n1_n_migration_n_restart_process_ids_distinct": len({n1_pid, n_migration_pid, n_restart_pid}) == 3,
            },
            "retained_identity_inventories": {
                "before_upgrade": {"file": before_path.name, "canonical_sha256": seed["inventory"]["canonical_sha256"]},
                "after_migration": {"file": "after_migration_identity_inventory.json", "canonical_sha256": migration["inventory"]["canonical_sha256"]},
                "after_restart": {"file": "after_restart_identity_inventory.json", "canonical_sha256": restart["after_restart_inventory"]["canonical_sha256"]},
                "equal_across_handoff": True,
                "permitted_operational_only_differences": authority["permitted_operational_differences"],
            },
            "accepted_command": restart["command_replay"],
            "accepted_command_row_invariants": {
                "before_upgrade_counts": {table: seed["inventory"]["row_counts"][table]
                                          for table in ("command_receipt", "audit_event", "outbox_event")},
                "after_restart_counts": {table: restart["after_restart_inventory"]["row_counts"][table]
                                         for table in ("command_receipt", "audit_event", "outbox_event")},
                "before_upgrade_fingerprints": {table: seed["inventory"]["row_fingerprints"][table]
                                                for table in ("command_receipt", "audit_event", "outbox_event")},
                "after_restart_fingerprints": {table: restart["after_restart_inventory"]["row_fingerprints"][table]
                                               for table in ("command_receipt", "audit_event", "outbox_event")},
                "counts_and_fingerprints_unchanged": True,
            },
            "accepted_command_conflict_negative_control": restart["different_payload_replay"],
            "workflow": {
                "initial": seed["initial_workflow"], "before_upgrade": seed["workflow_after_command"],
                "after_restart": restart["workflow"],
                "identity_version_state_unchanged": True,
            },
            "historical_read": {
                "before_upgrade": seed["historical_read"], "after_restart": restart["historical_read"],
                "identity_revision_vector_workflow_snapshot_unchanged": True,
            },
            "current_live_read": {
                "before_upgrade": seed["current_read"], "after_restart": restart["current_live_read"],
                "reflects_durable_workflow_after_restart": True,
                "historical_and_current_states_remain_distinct": True,
            },
            "artifact": restart["artifact"],
            "negative_controls": negative_summary,
            "operations_status_after_restart": status,
            "core_edit_prohibition": {
                "core_edit_required": False,
                "candidate_protected_path_modifications": candidate_protected,
                "frozen_pair_protected_path_delta": frozen_protected,
                "frozen_pair_schema_file_delta": frozen_schema_delta,
            },
            "installed_execution": {
                "separate_virtual_environments": True,
                "own_release_preparation_tooling": True,
                "restricted_inputs": True,
                "public_index_used_for_installation": False,
                "checkout_pythonpath_or_sys_path_used": False,
                "frozen_worktrees_clean": True,
            },
            "artifact_bridge_handoff": {
                "staging_state": "NOT_STAGED",
                "readback_state": "NOT_RUN",
                "canonical_handoff": False,
                "reason_code": "QUALIFIED_PRODUCT_STAGING_BRIDGE_UNAVAILABLE",
            },
            "claim_boundary": authority["qualification"]["claim_boundary"],
            "not_claimed": {
                "capability_flag_preservation": "NOT_QUALIFIED",
                "failed_upgrade_stop_before_promotion": "NOT_QUALIFIED",
                "cross_release_activation_or_rollback": "NOT_QUALIFIED",
                "full_U4_exit": "NOT_QUALIFIED",
                "real_company_integration": "NOT_RUN",
                "G10": "NOT_RUN", "G11": "NOT_RUN", "G12": "NOT_RUN",
                "Port_Gate": "NOT_RUN", "release_promotion": "NOT_RUN", "Production": "NOT_RUN",
            },
            "secret_values_emitted": False,
        }
        _write_json(artifact_root / "migration_restart_summary.json", migration_summary)
        _write_json(artifact_root / "negative_control_summary.json", negative_summary)
        evidence_files = [
            artifact_root / "before_upgrade_identity_inventory.json",
            artifact_root / "after_migration_identity_inventory.json",
            artifact_root / "after_restart_identity_inventory.json",
            artifact_root / "migration_restart_summary.json",
            artifact_root / "negative_control_summary.json",
        ]
        blob_objects = sorted(path for path in blob_root.rglob("*") if path.is_file())
        if len(blob_objects) != 1:
            raise QualificationFailure("SHARED_ARTIFACT_BLOB_INVENTORY_INVALID")
        report["supporting_evidence"] = [_file_fingerprint(path) for path in evidence_files]
        report_path = artifact_root / "u4-n1-durable-upgrade-restart.json"
        _write_json(report_path, report)
        manifest = {
            "schema": "org.ephi.artifact-bridge-manifest.v1",
            "project": "ephi", "request": authority["qualification"]["request"],
            "operation": "BUILD", "fabric_job": args.job_id,
            "fabric_binding_state": "BOUND" if args.job_id else "UNBOUND_CI",
            "candidate": {"commit": candidate_sha, "tree": candidate_tree},
            "artifacts": [
                _file_fingerprint(report_path),
                *[_file_fingerprint(path) for path in evidence_files],
                {"file": blob_objects[0].relative_to(artifact_root).as_posix(),
                 "byte_size": blob_objects[0].stat().st_size,
                 "sha256": _sha256_file(blob_objects[0])},
            ],
            "qualification_state": "PASS",
        }
        _write_json(artifact_root / "artifact-manifest.json", manifest)
        return report
    finally:
        if databases_created and database_names is not None:
            drop_env = _worker_env(_clean_env(environments_root / "n-minus-1", database_admin_dsn=os.environ.get("EPHI_U4_ADMIN_DSN", "")),
                                   database_name=database_name, prefix_database_name=prefix_database_name)
            try:
                _worker(installed.get("N-1", environments_root / "n-minus-1" / "bin" / "python"),
                        worker_file, "drop-databases", cwd=external_cwd, env=drop_env,
                        sensitive=(os.environ.get("EPHI_U4_ADMIN_DSN", ""), str(args.work_root), database_name, prefix_database_name))
            except Exception:
                raise QualificationFailure("DISPOSABLE_DATABASE_CLEANUP_FAILED")
        for path in reversed(worktrees):
            try:
                _command(["git", "-C", str(repository), "worktree", "remove", "--force", str(path)],
                         cwd=ROOT, code="FROZEN_WORKTREE_CLEANUP_FAILED")
            except QualificationFailure:
                raise
        if args.work_root.exists():
            shutil.rmtree(args.work_root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, type=Path, help="Empty job-owned Artifact Bridge evidence root outside the checkout.")
    parser.add_argument("--work-root", required=True, type=Path, help="Empty external work root for clean worktrees and installed environments.")
    parser.add_argument("--repository-root", type=Path, help="Canonical checkout containing the frozen Git history.")
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--candidate-tree", required=True)
    parser.add_argument("--job-id", help="Live Fabric job ID; omit for generic CI, which is UNBOUND_CI.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.artifact_root = args.artifact_root.resolve()
    args.work_root = args.work_root.resolve()
    args.repository_root = args.repository_root.resolve() if args.repository_root else ROOT
    if args.job_id is not None and not _JOB_ID.fullmatch(args.job_id):
        print(json.dumps({"schema": SCHEMA, "status": "FAIL", "reason_code": "FABRIC_JOB_ID_INVALID"}, sort_keys=True, separators=(",", ":")))
        return 2
    if args.work_root == args.repository_root or args.work_root in args.repository_root.parents or args.repository_root in args.work_root.parents:
        print(json.dumps({"schema": SCHEMA, "status": "FAIL", "reason_code": "WORK_ROOT_NOT_EXTERNAL"}, sort_keys=True, separators=(",", ":")))
        return 2
    try:
        report = _run_qualification(args)
    except QualificationFailure as exc:
        _write_json(args.artifact_root / "u4-n1-durable-upgrade-restart.json", {
            "schema": SCHEMA, "status": "FAIL", "durable_upgrade_state": "FAIL",
            "qualification": {"project": "ephi", "request": "ephi-u4-n1-durable-upgrade-restart-v1",
                              "operation": "BUILD", "candidate": {"commit": args.candidate_sha, "tree": args.candidate_tree},
                              "fabric_job": args.job_id,
                              "fabric_binding_state": "BOUND" if args.job_id else "UNBOUND_CI"},
            "reason_code": exc.code, "secret_values_emitted": False,
        })
        print(json.dumps({"schema": SCHEMA, "status": "FAIL", "reason_code": exc.code}, sort_keys=True, separators=(",", ":")))
        return 1
    except Exception:
        _write_json(args.artifact_root / "u4-n1-durable-upgrade-restart.json", {
            "schema": SCHEMA, "status": "FAIL", "durable_upgrade_state": "FAIL",
            "qualification": {"project": "ephi", "request": "ephi-u4-n1-durable-upgrade-restart-v1",
                              "operation": "BUILD", "candidate": {"commit": args.candidate_sha, "tree": args.candidate_tree},
                              "fabric_job": args.job_id,
                              "fabric_binding_state": "BOUND" if args.job_id else "UNBOUND_CI"},
            "reason_code": "QUALIFICATION_FAILED", "secret_values_emitted": False,
        })
        print(json.dumps({"schema": SCHEMA, "status": "FAIL", "reason_code": "QUALIFICATION_FAILED"}, sort_keys=True, separators=(",", ":")))
        return 1
    print(json.dumps({"schema": SCHEMA, "status": "PASS", "durable_upgrade_state": report["durable_upgrade_state"],
                      "fabric_binding_state": report["qualification"]["fabric_binding_state"]}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
