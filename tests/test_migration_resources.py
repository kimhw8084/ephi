"""Installed-aware migration resource, CLI, and operations-health regressions."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application.operations import migration_schema_identity  # noqa: E402
from ephi import db_migrate  # noqa: E402
from ephi.infrastructure import postgresql  # noqa: E402
from ephi.migration_resources import MigrationResourceError, resolve_migration_resources  # noqa: E402
import ephi.migration_resources as migration_resources_module  # noqa: E402


EXPECTED_IDENTITY = "c661c7a41eae8cd2b637778998bee77b8ebdec4a2ef78639d3ed7f69b23b1e8b"
DSN_SENTINELS = (
    "private-user",
    "secret-password",
    "private.db.invalid",
    "private_database_name",
)


def _installed_layout(root: Path, migrations: Path | None) -> tuple[Path, Path]:
    package_file = root / "venv" / "lib" / "python3.13" / "site-packages" / "ephi" / "migration_resources.py"
    package_file.parent.mkdir(parents=True)
    package_file.touch()
    shutil.copyfile(ROOT / "src" / "ephi" / "release_inventory.json", package_file.with_name("release_inventory.json"))
    data_root = root / "venv"
    if migrations is not None:
        destination = data_root / "share" / "ephi" / "migrations"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(migrations, destination)
    return package_file, data_root


class MigrationResourceTests(unittest.TestCase):
    def test_source_resolver_keeps_canonical_numbered_identity(self):
        resources = resolve_migration_resources()
        self.assertEqual(resources.directory, ROOT / "migrations")
        self.assertEqual(len(resources.paths), 11)
        self.assertEqual(resources.identity["migration_count"], 11)
        self.assertEqual(resources.identity["identity_sha256"], EXPECTED_IDENTITY)
        self.assertEqual(
            [path.name for path in resources.paths],
            [f"{index:03}_{path.name.split('_', 1)[1]}" for index, path in enumerate(sorted((ROOT / "migrations").glob("*.sql")), 1)],
        )

    def test_source_and_installed_resolvers_produce_identical_order_and_hash(self):
        source = resolve_migration_resources()
        with tempfile.TemporaryDirectory() as temp:
            package_file, data_root = _installed_layout(Path(temp), ROOT / "migrations")
            with patch.object(migration_resources_module, "__file__", str(package_file)), patch.object(
                migration_resources_module.sysconfig, "get_path", return_value=str(data_root)
            ):
                installed = resolve_migration_resources()
        self.assertEqual([path.name for path in installed.paths], [path.name for path in source.paths])
        self.assertEqual(installed.identity, source.identity)

    def test_missing_installed_directory_or_files_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            package_file, data_root = _installed_layout(Path(temp), None)
            with patch.object(migration_resources_module, "__file__", str(package_file)), patch.object(
                migration_resources_module.sysconfig, "get_path", return_value=str(data_root)
            ):
                with self.assertRaises(MigrationResourceError):
                    resolve_migration_resources()
                empty = data_root / "share" / "ephi" / "migrations"
                empty.mkdir(parents=True)
                with self.assertRaises(MigrationResourceError):
                    resolve_migration_resources()

        with tempfile.TemporaryDirectory() as temp:
            package_file, data_root = _installed_layout(Path(temp), ROOT / "migrations")
            (data_root / "share" / "ephi" / "migrations" / "011_o4_revision_pinned_asset_reads.sql").unlink()
            with patch.object(migration_resources_module, "__file__", str(package_file)), patch.object(
                migration_resources_module.sysconfig, "get_path", return_value=str(data_root)
            ):
                with self.assertRaises(MigrationResourceError):
                    resolve_migration_resources()

    def test_empty_set_and_unexpected_sql_resource_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "migrations").mkdir()
            with self.assertRaises(ValueError):
                migration_schema_identity(root / "migrations")
            shutil.copytree(ROOT / "migrations", root / "migrations", dirs_exist_ok=True)
            (root / "migrations" / "notes.sql").write_text("SELECT 1;\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                migration_schema_identity(root / "migrations")
            (root / "migrations" / "notes.sql").unlink()
            (root / "migrations" / "notes.SQL").write_text("SELECT 1;\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                migration_schema_identity(root / "migrations")

    def test_changed_bytes_change_the_existing_migration_identity(self):
        expected = migration_schema_identity(ROOT / "migrations")
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / "migrations"
            shutil.copytree(ROOT / "migrations", copied)
            path = copied / "011_o4_revision_pinned_asset_reads.sql"
            path.write_bytes(path.read_bytes() + b"\n-- changed fixture\n")
            changed = migration_schema_identity(copied)
        self.assertNotEqual(changed, expected)
        self.assertEqual(changed["migration_count"], expected["migration_count"])
        self.assertNotEqual(changed["identity_sha256"], expected["identity_sha256"])

    def test_installed_operations_health_uses_current_identity_and_no_ledger_claim(self):
        class Cursor:
            def __init__(self, rows=(), row=None):
                self.rows = list(rows)
                self.row = row

            def fetchone(self):
                return self.row

            def fetchall(self):
                return self.rows

        class Connection:
            closed = False
            broken = False

            def execute(self, statement, _parameters=None):
                if "current_setting('server_version')" in statement:
                    return Cursor(row={"server_version": "18.6", "observed_at": datetime.now(timezone.utc)})
                if "information_schema.columns" in statement:
                    return Cursor(rows=[{"table_name": "source_snapshot", "column_name": "freshness_age_seconds"}])
                return Cursor(rows=[{"table_name": name} for name in postgresql._REQUIRED_SCHEMA_TABLES])

        with tempfile.TemporaryDirectory() as temp:
            package_file, data_root = _installed_layout(Path(temp), ROOT / "migrations")
            adapter = postgresql.PostgreSQLReferenceTransactionAdapter.__new__(postgresql.PostgreSQLReferenceTransactionAdapter)
            adapter._explicitly_closed = False
            adapter._connection = Connection()
            adapter._connection_lock = threading.Lock()
            with patch.object(migration_resources_module, "__file__", str(package_file)), patch.object(
                migration_resources_module.sysconfig, "get_path", return_value=str(data_root)
            ):
                facts = adapter.operations_health_facts()
        self.assertEqual(facts.migration_file_count, 11)
        self.assertEqual(facts.migration_manifest_sha256, EXPECTED_IDENTITY)
        self.assertEqual(facts.migration_ledger_state, "NOT_BOUND_NO_MIGRATION_LEDGER")


class SchemaValidationTests(unittest.TestCase):
    class Connection:
        def __init__(self, *, tables=None, columns=None):
            self.tables = set(postgresql._REQUIRED_SCHEMA_TABLES if tables is None else tables)
            self.columns = set(
                {("source_snapshot", "freshness_age_seconds")} if columns is None else columns
            )
            self.statements = []

        def execute(self, statement, _parameters=None):
            self.statements.append(statement)
            if "information_schema.tables" in statement:
                rows = [{"table_name": name} for name in sorted(self.tables)]
            else:
                rows = [
                    {"table_name": table_name, "column_name": column_name}
                    for table_name, column_name in sorted(self.columns)
                ]
            return self.Cursor(rows)

        class Cursor:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

    def test_all_required_current_schema_facts_pass(self):
        connection = self.Connection()
        self.assertEqual(postgresql.validate_required_schema(connection), 19)
        self.assertEqual(len(connection.statements), 2)
        self.assertTrue(all(statement.lstrip().startswith("SELECT") for statement in connection.statements))

    def test_all_required_tables_without_current_source_snapshot_column_fail(self):
        connection = self.Connection(columns=())
        with self.assertRaises(postgresql.StorageFailureError):
            postgresql.validate_required_schema(connection)
        self.assertEqual(len(connection.statements), 2)

    def test_missing_required_table_still_fails(self):
        tables = set(postgresql._REQUIRED_SCHEMA_TABLES) - {"outbox_event"}
        connection = self.Connection(tables=tables)
        with self.assertRaises(postgresql.StorageFailureError):
            postgresql.validate_required_schema(connection)
        self.assertEqual(len(connection.statements), 1)

    def test_reconnect_and_post_apply_paths_use_the_shared_validator(self):
        connection = object()
        with patch.object(postgresql, "validate_required_schema", side_effect=postgresql.StorageFailureError("bounded")) as validate:
            with self.assertRaises(postgresql.StorageFailureError):
                postgresql.PostgreSQLReferenceTransactionAdapter._validate_existing_schema(connection)
            validate.assert_called_once_with(connection)

        apply_connection = self.Connection()
        resources = type("Resources", (), {"paths": (), "identity": {"migration_count": 11}})()
        with patch.object(postgresql, "resolve_migration_resources", return_value=resources), patch.object(
            postgresql, "validate_required_schema", side_effect=postgresql.StorageFailureError("bounded")
        ) as validate:
            with self.assertRaises(postgresql.StorageFailureError):
                postgresql.apply_migrations_to_connection(apply_connection)
        validate.assert_called_once_with(apply_connection)


class MigrationCommandTests(unittest.TestCase):
    def test_postgresql_connection_uses_named_rows_for_shared_schema_verifier(self):
        fake_psycopg = ModuleType("psycopg")
        fake_psycopg.__path__ = []
        fake_psycopg.connect = Mock(return_value=object())
        fake_rows = ModuleType("psycopg.rows")
        fake_rows.dict_row = object()
        fake_psycopg.rows = fake_rows
        with patch.dict(sys.modules, {"psycopg": fake_psycopg, "psycopg.rows": fake_rows}):
            connection = db_migrate._connect("secret-dsn")
        self.assertIsNotNone(connection)
        fake_psycopg.connect.assert_called_once_with("secret-dsn", autocommit=True, row_factory=fake_rows.dict_row)

    def test_identity_is_offline_and_returns_bounded_installed_plan(self):
        output = io.StringIO()
        with patch.object(db_migrate, "_connect", side_effect=AssertionError("must not connect")), patch.object(
            db_migrate, "_verify_database", side_effect=AssertionError("must not verify")
        ), patch.object(db_migrate, "_apply_database", side_effect=AssertionError("must not apply")), redirect_stdout(output):
            result = db_migrate.main(["identity"])
        report = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "PLAN")
        self.assertEqual(report["migration_count"], 11)
        self.assertEqual(report["identity_sha256"], EXPECTED_IDENTITY)
        self.assertEqual(len(report["migrations"]), 11)
        self.assertTrue(all("/" not in item["name"] for item in report["migrations"]))

    def test_verify_dispatches_without_application_and_reports_no_database_detail(self):
        output = io.StringIO()
        dsn = "postgresql://private-user:secret-password@private.db.invalid/private_database_name"
        with patch.object(db_migrate, "_verify_database", return_value=19) as verify, patch.object(
            db_migrate, "_apply_database", side_effect=AssertionError("verify must not apply")
        ), redirect_stdout(output):
            result = db_migrate.main(["verify", "--dsn", dsn])
        report = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "VERIFIED")
        self.assertEqual(report["schema_state"], "CURRENT")
        self.assertEqual(report["required_table_count"], 19)
        verify.assert_called_once_with(dsn)
        for sentinel in DSN_SENTINELS:
            self.assertNotIn(sentinel, output.getvalue())

    def test_verification_helper_executes_only_schema_reads(self):
        class Connection(SchemaValidationTests.Connection):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        connection = Connection()
        with patch.object(db_migrate, "_connect", return_value=connection):
            self.assertEqual(db_migrate._verify_database("unused-secret-dsn"), 19)
        self.assertEqual(len(connection.statements), 2)
        self.assertTrue(all(statement.lstrip().startswith("SELECT") for statement in connection.statements))

    def test_verify_failure_never_echoes_dsn_or_exception_details(self):
        dsn = "postgresql://private-user:secret-password@private.db.invalid/private_database_name"
        exception_detail = " ".join(DSN_SENTINELS)
        output = io.StringIO()
        with patch.object(db_migrate, "_verify_database", side_effect=RuntimeError(exception_detail)), redirect_stdout(output):
            result = db_migrate.main(["verify", "--dsn", dsn])
        self.assertEqual(result, 2)
        report = json.loads(output.getvalue())
        self.assertEqual(report["reason_code"], "SCHEMA_VERIFICATION_FAILED")
        self.assertNotEqual(report.get("status"), "VERIFIED")
        self.assertNotEqual(report.get("schema_state"), "CURRENT")
        for sentinel in DSN_SENTINELS:
            self.assertNotIn(sentinel, output.getvalue())

    def test_apply_failure_never_echoes_dsn_or_exception_details(self):
        dsn = "postgresql://private-user:secret-password@private.db.invalid/private_database_name"
        exception_detail = " ".join(DSN_SENTINELS)
        output = io.StringIO()
        with patch.object(db_migrate, "_apply_database", side_effect=RuntimeError(exception_detail)), redirect_stdout(output):
            result = db_migrate.main(["apply", "--dsn", dsn])
        self.assertEqual(result, 2)
        report = json.loads(output.getvalue())
        self.assertEqual(report["reason_code"], "MIGRATION_APPLY_FAILED")
        for sentinel in DSN_SENTINELS:
            self.assertNotIn(sentinel, output.getvalue())

    def test_bad_arguments_return_fixed_secret_safe_failure(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = db_migrate.main(["verify", "--dsn"])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["reason_code"], "INVALID_ARGUMENTS")


if __name__ == "__main__":
    unittest.main()
