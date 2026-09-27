"""Installed, read-only O9 multi-axis operational status command."""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Sequence

from ephi.application.operations import (
    OperationalState,
    OperationsAxis,
    operations_health_snapshot,
    verify_artifact_inventory,
)
from ephi.application.source_reality import preflight_source_reality
from ephi.infrastructure.postgresql import (
    RequiredSchemaMismatchError,
    operations_health_facts_for_connection,
)


_VERSION = re.compile(r"^(\d+(?:\.\d+){0,2})")
_WORKER_HEALTH_SQL = """
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


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("invalid arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--dsn", help="PostgreSQL DSN; defaults to EPHI_POSTGRES_DSN.")
    status.add_argument("--artifact-root", help="Explicit immutable artifact root.")
    status.add_argument("--json", action="store_true", help="Emit the JSON status report (the default).")
    return parser


def _connect_readonly(dsn: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row

        connection = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    except Exception as exc:
        raise RuntimeError("PostgreSQL connection failed") from exc
    try:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return connection
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        try:
            connection.close()
        except Exception:
            pass
        raise RuntimeError("PostgreSQL read-only transaction could not be started") from exc


def _source_axis() -> OperationsAxis:
    try:
        report = preflight_source_reality()
        capability = report.get("capability", {})
        state = capability.get("state", "UNAVAILABLE") if isinstance(capability, dict) else "UNAVAILABLE"
        reason_by_state = {
            "UNAVAILABLE": "BLOCKED_REAL_SOURCE",
            "PARTIAL": "SOURCE_CAPABILITY_PARTIAL",
            "INSUFFICIENT": "SOURCE_CAPABILITY_INSUFFICIENT",
            "STALE": "SOURCE_CAPABILITY_STALE",
            "READY": "SOURCE_CAPABILITY_READY",
            "CURRENT": "SOURCE_CAPABILITY_CURRENT",
            "PENDING": "SOURCE_CAPABILITY_PENDING",
            "BLOCKED": "SOURCE_CAPABILITY_BLOCKED",
            "EXPIRED": "SOURCE_CAPABILITY_EXPIRED",
            "DEGRADED": "SOURCE_CAPABILITY_DEGRADED",
            "ERROR": "SOURCE_CAPABILITY_ERROR",
            "NOT_QUALIFIED": "SOURCE_CAPABILITY_NOT_QUALIFIED",
        }
        if state == "PARTIAL":
            state = "DEGRADED"
        elif state == "INSUFFICIENT":
            state = "NOT_QUALIFIED"
        if state not in {item.value for item in OperationalState}:
            state = "UNAVAILABLE"
        checked_at = capability.get("checked_at") if isinstance(capability, dict) else None
        return OperationsAxis.create(
            state,
            reason_by_state.get(state, "SOURCE_CAPABILITY_UNAVAILABLE"),
            {"freshness_known": checked_at is not None},
        )
    except Exception:
        return OperationsAxis.create(
            "UNAVAILABLE", "SOURCE_PREFLIGHT_UNAVAILABLE", {"freshness_known": False}
        )


def _postgres_axis(facts: Any) -> OperationsAxis:
    match = _VERSION.match(facts.server_version)
    return OperationsAxis.create(
        "READY",
        "POSTGRES_REACHABLE_AND_SCHEMA_CURRENT",
        {
            "server_version": match.group(1) if match else "UNKNOWN",
            "required_table_count": facts.required_table_count,
            "migration_count": facts.migration_file_count,
            "migration_identity_sha256": facts.migration_manifest_sha256,
            "migration_ledger_state": facts.migration_ledger_state,
        },
    )


def _artifact_axis(connection: Any, artifact_root: str | os.PathLike[str] | None) -> OperationsAxis:
    if artifact_root is None:
        return OperationsAxis.create(
            "UNAVAILABLE", "IMMUTABLE_ARTIFACT_ROOT_NOT_CONFIGURED", {"configured": False}
        )
    rows = connection.execute(
        "SELECT sha256, byte_size FROM artifact_catalog ORDER BY sha256"
    ).fetchall()
    inventory = tuple(
        {"sha256": row["sha256"], "byte_size": int(row["byte_size"])} for row in rows
    )
    failures = verify_artifact_inventory(artifact_root, inventory)
    return OperationsAxis.create(
        "ERROR" if failures else "READY",
        "IMMUTABLE_ARTIFACT_BYTES_FAILED" if failures else "IMMUTABLE_ARTIFACT_INVENTORY_VERIFIED",
        {
            "artifact_count": len(inventory),
            "integrity_failure_count": len(failures),
            "inventory_known": True,
        },
    )


def _worker_axis(connection: Any) -> OperationsAxis:
    row = connection.execute(_WORKER_HEALTH_SQL).fetchone()
    if row is None:
        raise RuntimeError("worker state query returned no result")
    facts = {
        "job_count": int(row["job_count"]),
        "failed_count": int(row["failed_count"]),
        "dead_letter_count": int(row["dead_letter_count"]),
        "running_count": int(row["running_count"]),
        "expired_running_count": int(row["expired_running_count"]),
        "authoritative_clock_used": True,
    }
    if facts["failed_count"] + facts["dead_letter_count"]:
        return OperationsAxis.create("ERROR", "DURABLE_WORKER_TERMINAL_FAILURE", facts)
    if facts["expired_running_count"]:
        return OperationsAxis.create("STALE", "DURABLE_WORKER_EXPIRED_LEASE", facts)
    return OperationsAxis.create("READY", "DURABLE_WORKER_STATE_REACHABLE", facts)


def operations_status(
    *, dsn: str | None, artifact_root: str | os.PathLike[str] | None
) -> dict[str, object]:
    """Return O9's six independently classified axes using read-only probes."""

    process = OperationsAxis.create("READY", "PROCESS_ENTRYPOINT_RESPONDED", {"pid_present": True})
    source = _source_axis()
    if not dsn:
        postgres = OperationsAxis.create("UNAVAILABLE", "POSTGRES_DSN_NOT_CONFIGURED", {"configured": False})
        artifacts = OperationsAxis.create(
            "UNAVAILABLE", "POSTGRES_REQUIRED_FOR_CATALOG_INTEGRITY", {"configured": False}
        )
        workers = OperationsAxis.create(
            "UNAVAILABLE", "POSTGRES_REQUIRED_FOR_DURABLE_WORKER_STATE", {"configured": False}
        )
    else:
        connection = None
        try:
            connection = _connect_readonly(dsn)
            facts = operations_health_facts_for_connection(connection)
            postgres = _postgres_axis(facts)
            try:
                artifacts = _artifact_axis(connection, artifact_root)
            except Exception:
                artifacts = OperationsAxis.create(
                    "UNAVAILABLE", "IMMUTABLE_ARTIFACT_INTEGRITY_UNAVAILABLE", {"configured": True}
                )
            try:
                workers = _worker_axis(connection)
            except Exception:
                workers = OperationsAxis.create(
                    "UNAVAILABLE", "DURABLE_WORKER_STATE_UNAVAILABLE", {"configured": True}
                )
        except RequiredSchemaMismatchError:
            postgres = OperationsAxis.create("ERROR", "POSTGRES_SCHEMA_MISMATCH", {"configured": True})
            artifacts = OperationsAxis.create("UNAVAILABLE", "POSTGRES_SCHEMA_NOT_CURRENT", {"configured": True})
            workers = OperationsAxis.create("UNAVAILABLE", "POSTGRES_SCHEMA_NOT_CURRENT", {"configured": True})
        except Exception:
            postgres = OperationsAxis.create("UNAVAILABLE", "POSTGRES_UNAVAILABLE", {"configured": True})
            artifacts = OperationsAxis.create("UNAVAILABLE", "POSTGRES_UNAVAILABLE", {"configured": True})
            workers = OperationsAxis.create("UNAVAILABLE", "POSTGRES_UNAVAILABLE", {"configured": True})
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass

    evidence = OperationsAxis.create(
        "NOT_QUALIFIED", "QUALIFICATION_AUTHORITY_NOT_BOUND", {"freshness_known": False}
    )
    return operations_health_snapshot(
        process_transport=process,
        postgres=postgres,
        immutable_artifacts=artifacts,
        source_capability=source,
        durable_worker_jobs=workers,
        evidence_qualification=evidence,
    ).as_dict()


def _write(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        dsn = args.dsn if args.dsn is not None else os.environ.get("EPHI_POSTGRES_DSN")
        report = operations_status(
            dsn=dsn.strip() if isinstance(dsn, str) and dsn.strip() else None,
            artifact_root=args.artifact_root,
        )
    except Exception:
        _write({"schema_version": "o9.1.v1", "status": "ERROR", "reason_code": "STATUS_REPORT_FAILED"})
        return 2
    _write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
