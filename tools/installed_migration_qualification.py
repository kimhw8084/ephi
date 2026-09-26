#!/usr/bin/env python3
"""Prove installed-wheel migrations on PostgreSQL 18 using isolated schemas."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any


EXPECTED_MIGRATION_IDENTITY = "c661c7a41eae8cd2b637778998bee77b8ebdec4a2ef78639d3ed7f69b23b1e8b"


class QualificationFailure(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _run_cli(cli: Path, operation: str, *, dsn: str | None = None, cwd: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("EPHI_TEST_POSTGRES_DSN", None)
    if dsn is None:
        env.pop("EPHI_POSTGRES_DSN", None)
    else:
        env["EPHI_POSTGRES_DSN"] = dsn
    result = subprocess.run(
        [str(cli), operation],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationFailure("MIGRATION_COMMAND_OUTPUT_INVALID") from exc
    if result.returncode != 0 or not isinstance(report, dict):
        raise QualificationFailure(f"MIGRATION_{operation.upper()}_FAILED")
    if dsn is not None:
        import psycopg

        connection_facts = psycopg.conninfo.conninfo_to_dict(dsn)
        rendered = result.stdout + result.stderr
        for value in (dsn, *(connection_facts.get(key) for key in ("user", "password", "host", "dbname"))):
            if value and value in rendered:
                raise QualificationFailure("MIGRATION_COMMAND_DISCLOSED_CONNECTION_FACT")
    return report


def _schema_dsn(psycopg: Any, base_dsn: str, schema: str) -> str:
    return psycopg.conninfo.make_conninfo(base_dsn, options=f"-c search_path={schema}")


def _create_schema(psycopg: Any, dsn: str, schema: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema))
        )


def _table_names(connection: Any) -> set[str]:
    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
    ).fetchall()
    return {str(row["table_name"] if isinstance(row, dict) else row[0]) for row in rows}


def _row_values(row: Any) -> tuple[Any, ...]:
    return tuple(row.values()) if isinstance(row, dict) else tuple(row)


def _insert_prefix_row(connection: Any) -> tuple[Any, ...]:
    now = datetime.now(timezone.utc)
    artifact_hash = "b" * 64
    manifest_hash = "c" * 64
    connection.execute(
        """
        INSERT INTO source_snapshot(
            snapshot_id, schema_version, scope_key, source_id, provider_id, family_id,
            capability_id, adapter_id, schema_id, mapping_version, mapping_hash, unit,
            required_identifiers_json, source_partition, source_revision, event_start,
            event_end, available_cutoff, manifest_artifact_sha256,
            manifest_artifact_byte_size, manifest_artifact_object_key, row_count, status,
            manifest_hash, ingested_at, published_at
        ) VALUES (
            'prefix-fixture-snapshot', 'o4.1.v1', 'prefix-fixture-scope', 'prefix-source',
            'prefix-provider', 'prefix-family', 'prefix-capability', 'prefix.adapter',
            'prefix-schema.v1', '1.0.0', %s, 'um', '[]'::jsonb, 'prefix-partition',
            'prefix-revision', %s, %s, %s, %s, 0, %s, 0, 'PUBLISHED', %s, %s, %s
        )
        """,
        (
            "a" * 64,
            now,
            now,
            now,
            artifact_hash,
            f"sha256/{artifact_hash}",
            manifest_hash,
            now,
            now,
        ),
    )
    row = connection.execute(
        "SELECT snapshot_id, scope_key, source_revision, manifest_hash, row_count "
        "FROM source_snapshot WHERE snapshot_id = 'prefix-fixture-snapshot'"
    ).fetchone()
    if row is None:
        raise QualificationFailure("PREFIX_FIXTURE_ROW_MISSING")
    return tuple(row.values()) if isinstance(row, dict) else tuple(row)


def _run(args: argparse.Namespace) -> dict[str, object]:
    import psycopg
    import ephi
    from ephi.application.operations import file_sha256
    from ephi.infrastructure.postgresql import _REQUIRED_SCHEMA_TABLES, _sql_statements
    from ephi.migration_resources import resolve_migration_resources

    repository = Path(__file__).resolve().parents[1]
    source_tree = (repository / "src").resolve()
    package_path = Path(ephi.__file__).resolve()
    if source_tree == package_path or source_tree in package_path.parents:
        raise QualificationFailure("SOURCE_TREE_IMPORT_AUTHORITY")
    if any(Path(item or ".").resolve() == source_tree for item in sys.path):
        raise QualificationFailure("SOURCE_TREE_ON_IMPORT_PATH")

    base_dsn = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()
    if not base_dsn:
        raise QualificationFailure("POSTGRES_FIXTURE_UNAVAILABLE")
    resources = resolve_migration_resources()
    identity = resources.identity
    if identity["migration_count"] != 11 or identity["identity_sha256"] != EXPECTED_MIGRATION_IDENTITY:
        raise QualificationFailure("INSTALLED_MIGRATION_IDENTITY_MISMATCH")
    if len(resources.paths) != 11:
        raise QualificationFailure("INSTALLED_MIGRATION_RESOURCE_COUNT_MISMATCH")

    cli = Path(sys.executable).with_name("ephi-db-migrate")
    if not cli.is_file():
        raise QualificationFailure("INSTALLED_MIGRATION_COMMAND_MISSING")
    cwd = Path(args.work_dir).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    plan = _run_cli(cli, "identity", cwd=cwd)
    if plan.get("status") != "PLAN" or plan.get("migration_count") != 11 or plan.get("identity_sha256") != EXPECTED_MIGRATION_IDENTITY:
        raise QualificationFailure("INSTALLED_IDENTITY_COMMAND_MISMATCH")

    with psycopg.connect(base_dsn, autocommit=True) as connection:
        version = str(connection.execute("SHOW server_version").fetchone()[0])
    if not version.startswith("18."):
        raise QualificationFailure("POSTGRESQL_18_REQUIRED")

    empty_schema = "ephi_u3_empty_" + secrets.token_hex(6)
    prefix_schema = "ephi_u3_prefix_" + secrets.token_hex(6)
    created: list[str] = []
    try:
        _create_schema(psycopg, base_dsn, empty_schema)
        created.append(empty_schema)
        empty_dsn = _schema_dsn(psycopg, base_dsn, empty_schema)
        empty_apply = _run_cli(cli, "apply", dsn=empty_dsn, cwd=cwd)
        empty_verify = _run_cli(cli, "verify", dsn=empty_dsn, cwd=cwd)
        if empty_apply.get("status") != "APPLIED" or empty_verify.get("status") != "VERIFIED":
            raise QualificationFailure("EMPTY_SCHEMA_MIGRATION_FAILED")
        if empty_apply.get("identity_sha256") != EXPECTED_MIGRATION_IDENTITY or empty_verify.get("migration_count") != 11:
            raise QualificationFailure("EMPTY_SCHEMA_IDENTITY_MISMATCH")
        with psycopg.connect(empty_dsn, autocommit=True) as connection:
            if not set(_REQUIRED_SCHEMA_TABLES).issubset(_table_names(connection)):
                raise QualificationFailure("EMPTY_SCHEMA_REQUIRED_TABLES_MISSING")

        _create_schema(psycopg, base_dsn, prefix_schema)
        created.append(prefix_schema)
        prefix_dsn = _schema_dsn(psycopg, base_dsn, prefix_schema)
        fixture_root = Path(args.fixture_migration_dir).resolve()
        records = {str(record["path"]).rsplit("/", 1)[-1]: record for record in identity["files"]}
        prefix_paths = resources.paths[:10]
        if len(prefix_paths) != 10:
            raise QualificationFailure("PREFIX_RESOURCE_COUNT_INVALID")
        with psycopg.connect(prefix_dsn, autocommit=True) as connection:
            for installed_path in prefix_paths:
                fixture_path = fixture_root / installed_path.name
                fixture_record = records.get(installed_path.name)
                if fixture_record is None:
                    raise QualificationFailure("PREFIX_FIXTURE_MIGRATION_MISSING")
                digest, size = file_sha256(fixture_path)
                if digest != fixture_record["sha256"] or size != fixture_record["byte_size"]:
                    raise QualificationFailure("PREFIX_FIXTURE_MIGRATION_MISMATCH")
                for statement in _sql_statements(fixture_path.read_text(encoding="utf-8")):
                    connection.execute(statement)
            before_row = _insert_prefix_row(connection)

        prefix_apply = _run_cli(cli, "apply", dsn=prefix_dsn, cwd=cwd)
        prefix_verify = _run_cli(cli, "verify", dsn=prefix_dsn, cwd=cwd)
        if prefix_apply.get("status") != "APPLIED" or prefix_verify.get("status") != "VERIFIED":
            raise QualificationFailure("PREFIX_ADVANCE_FAILED")
        if prefix_apply.get("migration_count") != 11 or prefix_apply.get("identity_sha256") != EXPECTED_MIGRATION_IDENTITY:
            raise QualificationFailure("PREFIX_ADVANCE_IDENTITY_MISMATCH")

        with psycopg.connect(prefix_dsn, autocommit=True) as connection:
            columns = {
                str(row["column_name"] if isinstance(row, dict) else row[0])
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'source_snapshot'"
                ).fetchall()
            }
            after_row = _row_values(connection.execute(
                "SELECT snapshot_id, scope_key, source_revision, manifest_hash, row_count "
                "FROM source_snapshot WHERE snapshot_id = 'prefix-fixture-snapshot'"
            ).fetchone())
            tables_before_reapply = _table_names(connection)
        if "freshness_age_seconds" not in columns:
            raise QualificationFailure("CURRENT_SOURCE_FRESHNESS_COLUMN_MISSING")
        if before_row != after_row:
            raise QualificationFailure("PREFIX_DURABLE_ROW_CHANGED")
        if not set(_REQUIRED_SCHEMA_TABLES).issubset(tables_before_reapply):
            raise QualificationFailure("PREFIX_REQUIRED_TABLES_MISSING")

        reapply = _run_cli(cli, "apply", dsn=prefix_dsn, cwd=cwd)
        with psycopg.connect(prefix_dsn, autocommit=True) as connection:
            tables_after_reapply = _table_names(connection)
            after_reapply_row = _row_values(connection.execute(
                "SELECT snapshot_id, scope_key, source_revision, manifest_hash, row_count "
                "FROM source_snapshot WHERE snapshot_id = 'prefix-fixture-snapshot'"
            ).fetchone())
        if reapply.get("status") != "APPLIED" or tables_after_reapply != tables_before_reapply or after_reapply_row != before_row:
            raise QualificationFailure("IDEMPOTENT_REAPPLY_CHANGED_STATE")

        return {
            "schema": "org.ephi.installed-migration-qualification.v1",
            "status": "PASS",
            "postgres_major": 18,
            "installed_migration_count": 11,
            "migration_identity_sha256": EXPECTED_MIGRATION_IDENTITY,
            "empty_schema_initialize_and_verify": "PASS",
            "prefix_001_010_advance_and_data_preservation": "PASS",
            "prefix_fixture_scope": "SCHEMA_PREFIX_ENGINE_ONLY",
            "n_minus_1_release_compatibility": "NOT_CLAIMED",
            "current_set_idempotent_reapply": "PASS",
            "secret_values_emitted": False,
        }
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as connection:
            for schema in reversed(created):
                connection.execute(
                    psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(schema))
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-migration-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    args = parser.parse_args()
    try:
        result = _run(args)
    except QualificationFailure as exc:
        print(json.dumps({"status": "FAIL", "reason_code": exc.reason_code}, sort_keys=True, separators=(",", ":")))
        return 1
    except Exception:
        print(json.dumps({"status": "FAIL", "reason_code": "INSTALLED_POSTGRESQL_QUALIFICATION_FAILED"}, sort_keys=True, separators=(",", ":")))
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
