"""Secret-safe installed-release PostgreSQL migration operator command."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Sequence

from ephi.migration_resources import MigrationResourceError, resolve_migration_resources


_SCHEMA = "org.ephi.db-migrate.v1"


class _UsageFailure(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _UsageFailure


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("identity", help="Show the installed migration plan without connecting to PostgreSQL.")
    for command in ("verify", "apply"):
        operation = commands.add_parser(command)
        operation.add_argument("--dsn", help="PostgreSQL DSN; defaults to EPHI_POSTGRES_DSN.")
    return parser


def _migration_facts(identity: dict[str, Any]) -> dict[str, object]:
    return {
        "migration_count": identity["migration_count"],
        "identity_sha256": identity["identity_sha256"],
        "migrations": [
            {
                "name": str(record["path"]).rsplit("/", 1)[-1],
                "sha256": record["sha256"],
                "byte_size": record["byte_size"],
            }
            for record in identity["files"]
        ],
    }


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def _verify_database(dsn: str) -> int:
    from ephi.infrastructure.postgresql import validate_required_schema

    with _connect(dsn) as connection:
        return validate_required_schema(connection)


def _apply_database(dsn: str) -> dict[str, Any]:
    from ephi.infrastructure.postgresql import apply_migrations_to_connection

    with _connect(dsn) as connection:
        return apply_migrations_to_connection(connection)


def _dsn(explicit: str | None) -> str | None:
    value = explicit if explicit is not None else os.environ.get("EPHI_POSTGRES_DSN")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _write(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _UsageFailure:
        _write({"schema": _SCHEMA, "status": "FAIL", "reason_code": "INVALID_ARGUMENTS"})
        return 2

    if args.command == "identity":
        try:
            facts = _migration_facts(resolve_migration_resources().identity)
        except (MigrationResourceError, OSError, TypeError, ValueError, KeyError):
            _write({"schema": _SCHEMA, "status": "FAIL", "operation": "identity", "reason_code": "MIGRATION_RESOURCES_UNAVAILABLE"})
            return 2
        _write({"schema": _SCHEMA, "status": "PLAN", "operation": "identity", **facts})
        return 0

    dsn = _dsn(args.dsn)
    if dsn is None:
        _write({"schema": _SCHEMA, "status": "FAIL", "operation": args.command, "reason_code": "DSN_REQUIRED"})
        return 2

    if args.command == "verify":
        try:
            facts = _migration_facts(resolve_migration_resources().identity)
            required_table_count = _verify_database(dsn)
        except Exception:
            _write({"schema": _SCHEMA, "status": "FAIL", "operation": "verify", "reason_code": "SCHEMA_VERIFICATION_FAILED"})
            return 2
        _write({
            "schema": _SCHEMA,
            "status": "VERIFIED",
            "operation": "verify",
            "schema_state": "CURRENT",
            "required_table_count": required_table_count,
            **facts,
        })
        return 0

    try:
        identity = _apply_database(dsn)
        facts = _migration_facts(identity)
        from ephi.infrastructure.postgresql import _REQUIRED_SCHEMA_TABLES

        required_table_count = len(_REQUIRED_SCHEMA_TABLES)
    except Exception:
        _write({"schema": _SCHEMA, "status": "FAIL", "operation": "apply", "reason_code": "MIGRATION_APPLY_FAILED"})
        return 2
    _write({
        "schema": _SCHEMA,
        "status": "APPLIED",
        "operation": "apply",
        "schema_state": "CURRENT",
        "required_table_count": required_table_count,
        **facts,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
