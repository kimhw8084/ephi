"""Installed O9 operations command contract and secret-safety regressions."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application.operations import PostgreSQLHealthFacts  # noqa: E402
from ephi.application.source_reality import SOURCE_CONFIGURATION_ENVIRONMENT  # noqa: E402
from ephi.infrastructure import postgresql  # noqa: E402
from ephi.operations_status import (  # noqa: E402
    _worker_axis,
    main,
    operations_status,
)


AXES = {
    "process_transport",
    "postgres_readiness_durability",
    "immutable_artifact_integrity",
    "source_capability_freshness",
    "durable_worker_job_state",
    "evidence_qualification_freshness",
}


class InstalledOperationsStatusTests(unittest.TestCase):
    def test_no_dsn_is_a_six_axis_report_and_ignores_test_dsn_fallback(self):
        with patch.dict(os.environ, {"EPHI_TEST_POSTGRES_DSN": "postgresql://test-only.invalid/db"}, clear=True), patch(
            "ephi.operations_status._connect_readonly", side_effect=AssertionError("must not connect")
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["status", "--json"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(set(report["axes"]), AXES)
        self.assertEqual(report["axes"]["process_transport"]["state"], "READY")
        for axis in (
            "postgres_readiness_durability",
            "immutable_artifact_integrity",
            "durable_worker_job_state",
        ):
            self.assertEqual(report["axes"][axis]["state"], "UNAVAILABLE")
        self.assertEqual(report["axes"]["source_capability_freshness"]["state"], "UNAVAILABLE")
        self.assertEqual(report["axes"]["evidence_qualification_freshness"]["state"], "NOT_QUALIFIED")
        self.assertFalse({"healthy", "ready", "overall", "status"} & set(report))

    def test_source_projection_and_unconfigured_artifact_root_hide_private_values(self):
        sentinels = {
            name: f"PRIVATE_{name}_SENTINEL" for name in SOURCE_CONFIGURATION_ENVIRONMENT
        }
        sentinels["EPHI_METROLOGY_SOURCE_ADAPTER"] = "private_adapter_sentinel:private_factory_sentinel"
        artifact_path = "/private/physical/artifact-root-sentinel"
        with patch.dict(os.environ, sentinels, clear=True):
            report = operations_status(dsn=None, artifact_root=artifact_path)
        encoded = json.dumps(report, sort_keys=True)
        for sentinel in (*sentinels.values(), artifact_path):
            self.assertNotIn(sentinel, encoded)
        self.assertEqual(report["axes"]["source_capability_freshness"]["reason"], "BLOCKED_REAL_SOURCE")

    def test_dsn_and_arbitrary_connection_exception_details_are_structurally_omitted(self):
        dsn_values = (
            "pg-user-sentinel",
            "pg-password-sentinel",
            "pg-private-host-sentinel.invalid",
            "pg-private-database-sentinel",
        )
        dsn = f"postgresql://{dsn_values[0]}:{dsn_values[1]}@{dsn_values[2]}:5432/{dsn_values[3]}"
        artifact_path = "/private/root/artifact-path-sentinel"
        source_values = ("private-source-id-sentinel", "private-mapping-id-sentinel")
        detail = "private exception detail sentinel " + " ".join((*dsn_values, artifact_path, *source_values))
        source_environment = {
            "EPHI_METROLOGY_SOURCE_ID": source_values[0],
            "EPHI_METROLOGY_MAPPING_VERSION": source_values[1],
        }
        with patch.dict(os.environ, source_environment, clear=True), patch(
            "ephi.operations_status._connect_readonly", side_effect=RuntimeError(detail)
        ):
            report = operations_status(dsn=dsn, artifact_root=artifact_path)
        encoded = json.dumps(report, sort_keys=True)
        for sentinel in (*dsn_values, *source_values, "private exception detail sentinel", artifact_path):
            self.assertNotIn(sentinel, encoded)
        self.assertEqual(report["axes"]["postgres_readiness_durability"]["reason"], "POSTGRES_UNAVAILABLE")

    def test_exact_current_tables_without_011_column_is_nonready_and_read_only(self):
        class Connection:
            def __init__(self):
                self.statements = []
                self.rolled_back = False
                self.closed = False

            def execute(self, statement, _parameters=None):
                self.statements.append(statement)
                if "information_schema.tables" in statement:
                    return Cursor([{"table_name": name} for name in postgresql._REQUIRED_SCHEMA_TABLES])
                if "information_schema.columns" in statement:
                    return Cursor([])
                raise AssertionError("status reached data probes before current-schema validation")

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        class Cursor:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        connection = Connection()
        with patch("ephi.operations_status._connect_readonly", return_value=connection):
            report = operations_status(
                dsn="postgresql://prefix-user:prefix-password@prefix-host.invalid/prefix-db",
                artifact_root="/prefix/private/root",
            )
        axis = report["axes"]["postgres_readiness_durability"]
        self.assertEqual((axis["state"], axis["reason"]), ("ERROR", "POSTGRES_SCHEMA_MISMATCH"))
        self.assertNotEqual(axis["state"], "READY")
        self.assertEqual(report["axes"]["immutable_artifact_integrity"]["reason"], "POSTGRES_SCHEMA_NOT_CURRENT")
        self.assertEqual(report["axes"]["durable_worker_job_state"]["reason"], "POSTGRES_SCHEMA_NOT_CURRENT")
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        self.assertTrue(connection.statements)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in connection.statements))

    def test_current_schema_projection_uses_shared_facts_and_preserves_worker_failures(self):
        class Connection:
            def __init__(self):
                self.statements = []
                self.rolled_back = False
                self.closed = False

            def execute(self, statement, _parameters=None):
                self.statements.append(statement)
                if "FROM artifact_catalog" in statement:
                    return Cursor([], None)
                if "FROM job" in statement:
                    return Cursor([], {
                        "job_count": 3,
                        "failed_count": 1,
                        "dead_letter_count": 1,
                        "running_count": 1,
                        "expired_running_count": 1,
                    })
                raise AssertionError("unexpected status query")

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        class Cursor:
            def __init__(self, rows, row):
                self.rows = rows
                self.row = row

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.row

        connection = Connection()
        facts = PostgreSQLHealthFacts(
            "18.6",
            19,
            19,
            0,
            11,
            "c661c7a41eae8cd2b637778998bee77b8ebdec4a2ef78639d3ed7f69b23b1e8b",
            "NOT_BOUND_NO_MIGRATION_LEDGER",
            None,
        )
        with patch("ephi.operations_status._connect_readonly", return_value=connection), patch(
            "ephi.operations_status.operations_health_facts_for_connection", return_value=facts
        ):
            report = operations_status(dsn="configured", artifact_root="/empty-artifact-root")
        postgres_axis = report["axes"]["postgres_readiness_durability"]
        self.assertEqual(postgres_axis["state"], "READY")
        self.assertEqual(postgres_axis["facts"]["server_version"], "18.6")
        self.assertEqual(postgres_axis["facts"]["migration_count"], 11)
        self.assertEqual(postgres_axis["facts"]["migration_ledger_state"], "NOT_BOUND_NO_MIGRATION_LEDGER")
        self.assertEqual(report["axes"]["durable_worker_job_state"]["reason"], "DURABLE_WORKER_TERMINAL_FAILURE")
        self.assertEqual(report["axes"]["durable_worker_job_state"]["facts"]["expired_running_count"], 1)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_worker_axis_keeps_expired_lease_stale_when_no_terminal_failures_exist(self):
        class Connection:
            def execute(self, statement):
                self.statement = statement
                return Cursor()

        class Cursor:
            def fetchone(self):
                return {
                    "job_count": 1,
                    "failed_count": 0,
                    "dead_letter_count": 0,
                    "running_count": 1,
                    "expired_running_count": 1,
                }

        connection = Connection()
        axis = _worker_axis(connection)
        self.assertEqual((axis.state.value, axis.reason), ("STALE", "DURABLE_WORKER_EXPIRED_LEASE"))
        self.assertIn("clock_timestamp()", connection.statement)

    def test_artifact_verification_uses_catalog_identity_without_emitting_hash_or_root(self):
        secret_hash = "f" * 64
        artifact_path = "/private/artifact/root/sentinel"

        class Connection:
            def execute(self, statement, _parameters=None):
                if "FROM artifact_catalog" in statement:
                    return Cursor([{"sha256": secret_hash, "byte_size": 42}], None)
                return Cursor([], {
                    "job_count": 0,
                    "failed_count": 0,
                    "dead_letter_count": 0,
                    "running_count": 0,
                    "expired_running_count": 0,
                })

            def rollback(self):
                pass

            def close(self):
                pass

        class Cursor:
            def __init__(self, rows, row):
                self.rows = rows
                self.row = row

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.row

        facts = PostgreSQLHealthFacts(
            "18.6", 19, 19, 0, 11,
            "c661c7a41eae8cd2b637778998bee77b8ebdec4a2ef78639d3ed7f69b23b1e8b",
            "NOT_BOUND_NO_MIGRATION_LEDGER", None,
        )
        with patch("ephi.operations_status._connect_readonly", return_value=Connection()), patch(
            "ephi.operations_status.operations_health_facts_for_connection", return_value=facts
        ), patch(
            "ephi.operations_status.verify_artifact_inventory",
            return_value=({"sha256": secret_hash, "reason": "MISSING_ARTIFACT_BYTES"},),
        ) as verify:
            report = operations_status(dsn="configured", artifact_root=artifact_path)
        verify.assert_called_once_with(artifact_path, ({"sha256": secret_hash, "byte_size": 42},))
        axis = report["axes"]["immutable_artifact_integrity"]
        self.assertEqual((axis["state"], axis["reason"], axis["facts"]["integrity_failure_count"]),
                         ("ERROR", "IMMUTABLE_ARTIFACT_BYTES_FAILED", 1))
        encoded = json.dumps(report, sort_keys=True)
        self.assertNotIn(secret_hash, encoded)
        self.assertNotIn(artifact_path, encoded)

    def test_package_and_checkout_cli_errors_discard_arbitrary_exception_text(self):
        leaked = "arbitrary-status-exception-sentinel"
        with patch("ephi.operations_status.operations_status", side_effect=RuntimeError(leaked)), redirect_stdout(
            io.StringIO()
        ) as package_output:
            self.assertEqual(main(["status", "--dsn", "dsn-sentinel"]), 2)
        self.assertNotIn(leaked, package_output.getvalue())
        self.assertNotIn("dsn-sentinel", package_output.getvalue())

        from tools import o9_operations

        with patch.object(o9_operations, "operations_status", side_effect=RuntimeError(leaked)), redirect_stdout(
            io.StringIO()
        ) as checkout_output:
            self.assertEqual(o9_operations.main(["status", "--dsn", "checkout-dsn-sentinel", "--json"]), 2)
        self.assertNotIn(leaked, checkout_output.getvalue())
        self.assertNotIn("checkout-dsn-sentinel", checkout_output.getvalue())

    def test_checkout_status_function_delegates_to_package_authority(self):
        from tools import o9_operations

        report = {"package_authority": True}
        with patch.object(o9_operations, "_package_operations_status", return_value=report) as authority:
            self.assertIs(
                o9_operations.operations_status(dsn="configured-dsn", artifact_root="/artifact-root"),
                report,
            )
        authority.assert_called_once_with(dsn="configured-dsn", artifact_root="/artifact-root")


if __name__ == "__main__":
    unittest.main()
