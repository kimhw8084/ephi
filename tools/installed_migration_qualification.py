#!/usr/bin/env python3
"""Prove installed-wheel migrations and O9 status on PostgreSQL 18."""

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


def _run_cli(
    cli: Path,
    operation: str,
    *,
    dsn: str | None = None,
    cwd: Path,
    expected_failure_reason: str | None = None,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, Any]:
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
    rendered = result.stdout + result.stderr
    if dsn is not None:
        import psycopg

        connection_facts = psycopg.conninfo.conninfo_to_dict(dsn)
        sensitive = (
            dsn,
            *(connection_facts.get(key) for key in ("user", "password", "host", "dbname", "options")),
            *sensitive_values,
        )
        if any(value and value in rendered for value in sensitive):
            raise QualificationFailure("MIGRATION_COMMAND_DISCLOSED_CONNECTION_FACT")
    try:
        report = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationFailure("MIGRATION_COMMAND_OUTPUT_INVALID") from exc
    if not isinstance(report, dict):
        raise QualificationFailure(f"MIGRATION_{operation.upper()}_FAILED")
    if expected_failure_reason is not None:
        if (
            result.returncode == 0
            or report.get("status") != "FAIL"
            or report.get("reason_code") != expected_failure_reason
            or report.get("schema_state") == "CURRENT"
        ):
            raise QualificationFailure(f"MIGRATION_{operation.upper()}_REJECTION_MISSING")
    elif result.returncode != 0:
        raise QualificationFailure(f"MIGRATION_{operation.upper()}_FAILED")
    return report


def _run_status_cli(
    cli: Path,
    *,
    dsn: str | None,
    cwd: Path,
    artifact_root: Path,
    sensitive_values: tuple[str, ...] = (),
    source_sentinels: bool = False,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("EPHI_TEST_POSTGRES_DSN", None)
    env.pop("PYTHONPATH", None)
    if dsn is None:
        env.pop("EPHI_POSTGRES_DSN", None)
    else:
        env["EPHI_POSTGRES_DSN"] = dsn
    sentinels = list(sensitive_values)
    if source_sentinels:
        for name in (
            "EPHI_METROLOGY_SOURCE_ADAPTER",
            "EPHI_METROLOGY_SOURCE_ID",
            "EPHI_METROLOGY_PROVIDER_ID",
            "EPHI_METROLOGY_FAMILY_ID",
            "EPHI_METROLOGY_CAPABILITY_ID",
            "EPHI_METROLOGY_SCOPE_ID",
            "EPHI_METROLOGY_SCHEMA_ID",
            "EPHI_METROLOGY_MAPPING_VERSION",
            "EPHI_METROLOGY_MAPPING_HASH",
            "EPHI_METROLOGY_UNIT",
            "EPHI_METROLOGY_SITE_ID",
            "EPHI_METROLOGY_AREA_ID",
            "EPHI_METROLOGY_REFERENCE_POPULATION_ID",
            "EPHI_METROLOGY_COMPARABLE_POPULATION_ID",
        ):
            value = f"{name.lower()}-private-sentinel"
            env[name] = value
            sentinels.append(value)
        env["EPHI_METROLOGY_SOURCE_ADAPTER"] = "installed_status_private_adapter_sentinel:build"
        sentinels.append("installed_status_private_adapter_sentinel")
    artifact_value = str(artifact_root)
    sentinels.append(artifact_value)
    result = subprocess.run(
        [str(cli), "status", "--json", "--artifact-root", artifact_value],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    rendered = result.stdout + result.stderr
    if any(value and value in rendered for value in sentinels):
        raise QualificationFailure("OPERATIONS_STATUS_DISCLOSED_PRIVATE_VALUE")
    if dsn is not None:
        import psycopg

        connection_facts = psycopg.conninfo.conninfo_to_dict(dsn)
        connection_values = (dsn, *(connection_facts.get(key) for key in ("user", "password", "host", "dbname", "options")))
        if any(value and value in rendered for value in connection_values):
            raise QualificationFailure("OPERATIONS_STATUS_DISCLOSED_CONNECTION_FACT")
    try:
        report = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationFailure("OPERATIONS_STATUS_OUTPUT_INVALID") from exc
    if result.returncode != 0 or not isinstance(report, dict):
        raise QualificationFailure("OPERATIONS_STATUS_COMMAND_FAILED")
    expected_axes = {
        "process_transport",
        "postgres_readiness_durability",
        "immutable_artifact_integrity",
        "source_capability_freshness",
        "durable_worker_job_state",
        "evidence_qualification_freshness",
    }
    if report.get("schema_version") != "o9.1.v1" or set(report.get("axes", {})) != expected_axes:
        raise QualificationFailure("OPERATIONS_STATUS_AXIS_CONTRACT_MISMATCH")
    if {"healthy", "ready", "overall", "status"} & set(report):
        raise QualificationFailure("OPERATIONS_STATUS_COLLAPSED_HEALTH_FIELD")
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


def _schema_state(connection: Any) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    tables = tuple(sorted(_table_names(connection)))
    rows = connection.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema()"
    ).fetchall()
    columns = tuple(sorted(
        (
            str(row["table_name"] if isinstance(row, dict) else row[0]),
            str(row["column_name"] if isinstance(row, dict) else row[1]),
        )
        for row in rows
    ))
    return tables, columns


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
    status_cli = Path(sys.executable).with_name("ephi-operations")
    if not status_cli.is_file():
        raise QualificationFailure("INSTALLED_OPERATIONS_COMMAND_MISSING")
    cwd = Path(args.work_dir).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    no_dsn_status = _run_status_cli(
        status_cli,
        dsn=None,
        cwd=cwd,
        artifact_root=cwd / "private_artifact_root_sentinel",
        source_sentinels=True,
    )
    for axis_name in (
        "postgres_readiness_durability",
        "immutable_artifact_integrity",
        "durable_worker_job_state",
    ):
        if no_dsn_status["axes"][axis_name]["state"] != "UNAVAILABLE":
            raise QualificationFailure("NO_DSN_OPERATIONS_STATUS_NOT_UNAVAILABLE")
    if (
        no_dsn_status["axes"]["source_capability_freshness"]["state"] != "UNAVAILABLE"
        or no_dsn_status["axes"]["evidence_qualification_freshness"]["state"] != "NOT_QUALIFIED"
    ):
        raise QualificationFailure("NO_DSN_SOURCE_OR_EVIDENCE_STATE_UNTRUTHFUL")
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
        if (
            empty_apply.get("status") != "APPLIED"
            or empty_verify.get("status") != "VERIFIED"
            or empty_verify.get("schema_state") != "CURRENT"
        ):
            raise QualificationFailure("EMPTY_SCHEMA_MIGRATION_FAILED")
        if empty_apply.get("identity_sha256") != EXPECTED_MIGRATION_IDENTITY or empty_verify.get("migration_count") != 11:
            raise QualificationFailure("EMPTY_SCHEMA_IDENTITY_MISMATCH")
        empty_status = _run_status_cli(
            status_cli,
            dsn=empty_dsn,
            cwd=cwd,
            artifact_root=cwd / "empty_schema_artifact_root",
        )
        empty_postgres_axis = empty_status["axes"]["postgres_readiness_durability"]
        if (
            empty_postgres_axis.get("state") != "READY"
            or empty_postgres_axis.get("facts", {}).get("server_version", "").split(".", 1)[0] != "18"
            or empty_postgres_axis.get("facts", {}).get("migration_count") != 11
            or empty_postgres_axis.get("facts", {}).get("migration_identity_sha256") != EXPECTED_MIGRATION_IDENTITY
            or empty_postgres_axis.get("facts", {}).get("migration_ledger_state") != "NOT_BOUND_NO_MIGRATION_LEDGER"
        ):
            raise QualificationFailure("CURRENT_SCHEMA_OPERATIONS_STATUS_FAILED")
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
            schema_before_verify = _schema_state(connection)

        before_columns = {
            column
            for table, column in schema_before_verify[1]
            if table == "source_snapshot"
        }
        if "freshness_age_seconds" in before_columns:
            raise QualificationFailure("PREFIX_FIXTURE_ALREADY_CURRENT")

        prefix_status = _run_status_cli(
            status_cli,
            dsn=prefix_dsn,
            cwd=cwd,
            artifact_root=cwd / "prefix_artifact_root",
            sensitive_values=(prefix_schema,),
        )
        prefix_postgres_axis = prefix_status["axes"]["postgres_readiness_durability"]
        if (
            prefix_postgres_axis.get("state") == "READY"
            or prefix_postgres_axis.get("reason") != "POSTGRES_SCHEMA_MISMATCH"
        ):
            raise QualificationFailure("PRE_011_OPERATIONS_STATUS_ACCEPTED_OLD_SCHEMA")
        with psycopg.connect(prefix_dsn, autocommit=True) as connection:
            schema_after_status = _schema_state(connection)
            after_status_row = _row_values(connection.execute(
                "SELECT snapshot_id, scope_key, source_revision, manifest_hash, row_count "
                "FROM source_snapshot WHERE snapshot_id = 'prefix-fixture-snapshot'"
            ).fetchone())
        if schema_before_verify != schema_after_status or before_row != after_status_row:
            raise QualificationFailure("OPERATIONS_STATUS_MUTATED_PREFIX_SCHEMA_OR_ROW")
        status_columns = {
            column for table, column in schema_after_status[1] if table == "source_snapshot"
        }
        if "freshness_age_seconds" in status_columns:
            raise QualificationFailure("OPERATIONS_STATUS_APPLIED_MIGRATION_011")

        _run_cli(
            cli,
            "verify",
            dsn=prefix_dsn,
            cwd=cwd,
            expected_failure_reason="SCHEMA_VERIFICATION_FAILED",
            sensitive_values=(prefix_schema,),
        )
        with psycopg.connect(prefix_dsn, autocommit=True) as connection:
            schema_after_failed_verify = _schema_state(connection)
            after_failed_verify_row = _row_values(connection.execute(
                "SELECT snapshot_id, scope_key, source_revision, manifest_hash, row_count "
                "FROM source_snapshot WHERE snapshot_id = 'prefix-fixture-snapshot'"
            ).fetchone())
        after_failed_verify_columns = {
            column
            for table, column in schema_after_failed_verify[1]
            if table == "source_snapshot"
        }
        if "freshness_age_seconds" in after_failed_verify_columns:
            raise QualificationFailure("FAILED_VERIFY_ADDED_CURRENT_SCHEMA_COLUMN")
        if schema_before_verify != schema_after_failed_verify or before_row != after_failed_verify_row:
            raise QualificationFailure("FAILED_VERIFY_MUTATED_SCHEMA_OR_ROW")

        prefix_apply = _run_cli(cli, "apply", dsn=prefix_dsn, cwd=cwd)
        prefix_verify = _run_cli(cli, "verify", dsn=prefix_dsn, cwd=cwd, sensitive_values=(prefix_schema,))
        if (
            prefix_apply.get("status") != "APPLIED"
            or prefix_verify.get("status") != "VERIFIED"
            or prefix_verify.get("schema_state") != "CURRENT"
        ):
            raise QualificationFailure("PREFIX_ADVANCE_FAILED")
        if prefix_apply.get("migration_count") != 11 or prefix_apply.get("identity_sha256") != EXPECTED_MIGRATION_IDENTITY:
            raise QualificationFailure("PREFIX_ADVANCE_IDENTITY_MISMATCH")

        current_status = _run_status_cli(
            status_cli,
            dsn=prefix_dsn,
            cwd=cwd,
            artifact_root=cwd / "prefix_artifact_root",
            sensitive_values=(prefix_schema,),
        )
        if current_status["axes"]["postgres_readiness_durability"]["state"] != "READY":
            raise QualificationFailure("POST_011_OPERATIONS_STATUS_NOT_READY")

        with psycopg.connect(prefix_dsn, autocommit=True) as connection:
            schema_after_apply = _schema_state(connection)
            after_row = _row_values(connection.execute(
                "SELECT snapshot_id, scope_key, source_revision, manifest_hash, row_count "
                "FROM source_snapshot WHERE snapshot_id = 'prefix-fixture-snapshot'"
            ).fetchone())
            tables_before_reapply = _table_names(connection)
        columns = {
            column
            for table, column in schema_after_apply[1]
            if table == "source_snapshot"
        }
        if "freshness_age_seconds" not in columns:
            raise QualificationFailure("CURRENT_SOURCE_FRESHNESS_COLUMN_MISSING")
        if before_row != after_row:
            raise QualificationFailure("PREFIX_DURABLE_ROW_CHANGED")
        if not set(_REQUIRED_SCHEMA_TABLES).issubset(tables_before_reapply):
            raise QualificationFailure("PREFIX_REQUIRED_TABLES_MISSING")

        reapply = _run_cli(cli, "apply", dsn=prefix_dsn, cwd=cwd, sensitive_values=(prefix_schema,))
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
            "installed_operations_no_dsn_status": "PASS",
            "installed_operations_current_postgresql_18_status": "PASS",
            "installed_operations_pre_011_read_only_rejection": "PASS",
            "installed_operations_post_011_status": "PASS",
            "prefix_preapply_verify_rejection": "PASS",
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
