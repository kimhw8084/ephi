"""CHG-147/O9.1 focused operations-health and restore-contract evidence."""

from pathlib import Path
import copy
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    OperationalState,
    OperationsAxis,
    operations_health_snapshot,
)
from ephi.application.operations import (  # noqa: E402
    artifact_blob_path,
    canonical_sha256,
    build_reconciliation_report,
    file_sha256,
    verify_artifact_inventory,
)
from ephi.application.source_reality import redacted_connection_facts  # noqa: E402
from ephi.operations_status import operations_status  # noqa: E402
from ephi.o9_operations import (  # noqa: E402
    LEGACY_MANIFEST_SCHEMA,
    MANIFEST_SCHEMA,
    OperationsFailure,
    _dsn_with_database,
    _migration_identity,
    _private_libpq_environment,
    _run_dump,
    _run_restore,
    create_backup,
    reconcile,
    restore_rehearsal,
    verify_backup,
)
from ephi import o9_operations as o9_authority  # noqa: E402
from ephi.release_identity import installed_release_identity  # noqa: E402
from tools.o9_operations import (  # noqa: E402
    ROOT,
    create_backup as checkout_create_backup,
    reconcile as checkout_reconcile,
    restore_rehearsal as checkout_restore_rehearsal,
    verify_backup as checkout_verify_backup,
)


class O9OperationsContractTests(unittest.TestCase):
    def axis(self, state="READY", reason="fixture"):
        return OperationsAxis.create(state, reason, {"known": True})

    def test_health_axes_are_separate_without_collapsed_global_healthy_claim(self):
        snapshot = operations_health_snapshot(
            process_transport=self.axis(),
            postgres=self.axis(),
            immutable_artifacts=self.axis(),
            source_capability=self.axis("UNAVAILABLE", "BLOCKED_REAL_SOURCE"),
            durable_worker_jobs=self.axis(),
            evidence_qualification=self.axis("NOT_QUALIFIED", "QUALIFICATION_NOT_BOUND"),
        ).as_dict()
        self.assertEqual(set(snapshot["axes"]), {
            "process_transport",
            "postgres_readiness_durability",
            "immutable_artifact_integrity",
            "source_capability_freshness",
            "durable_worker_job_state",
            "evidence_qualification_freshness",
        })
        encoded = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn('"healthy"', encoded)
        self.assertNotIn('"overall"', encoded)

    def test_secret_safe_dsn_redaction(self):
        facts = redacted_connection_facts("postgresql://secret-user:secret-password@db.internal:5432/ephi")
        encoded = json.dumps(facts, sort_keys=True)
        self.assertTrue(facts["credentials_redacted"])
        self.assertNotIn("secret-user", encoded)
        self.assertNotIn("secret-password", encoded)
        self.assertNotIn("db.internal", encoded)

    def test_process_and_database_can_be_ready_while_source_is_unavailable(self):
        snapshot = operations_health_snapshot(
            process_transport=self.axis(),
            postgres=self.axis(),
            immutable_artifacts=self.axis(),
            source_capability=self.axis("UNAVAILABLE", "BLOCKED_REAL_SOURCE"),
            durable_worker_jobs=self.axis(),
            evidence_qualification=self.axis("NOT_QUALIFIED", "QUALIFICATION_NOT_BOUND"),
        ).as_dict()
        self.assertEqual(snapshot["axes"]["process_transport"]["state"], "READY")
        self.assertEqual(snapshot["axes"]["postgres_readiness_durability"]["state"], "READY")
        self.assertEqual(snapshot["axes"]["source_capability_freshness"], {
            "state": "UNAVAILABLE",
            "reason": "BLOCKED_REAL_SOURCE",
            "facts": {"known": True},
        })

    def test_migration_identity_is_deterministic_and_exact(self):
        first = _migration_identity(ROOT)
        second = _migration_identity(ROOT)
        self.assertEqual(first, second)
        self.assertEqual(first["migration_count"], 11)
        self.assertEqual(len(first["identity_sha256"]), 64)
        self.assertEqual([item["path"] for item in first["files"]], sorted(item["path"] for item in first["files"]))
        self.assertIn("migrations/008_o6_comparable_case_history.sql", [item["path"] for item in first["files"]])
        self.assertIn("migrations/009_o7_outcome_group_identity.sql", [item["path"] for item in first["files"]])
        self.assertIn("migrations/010_o7_normalized_value_revisions.sql", [item["path"] for item in first["files"]])
        self.assertIn("migrations/011_o4_revision_pinned_asset_reads.sql", [item["path"] for item in first["files"]])

    def test_backup_artifact_verification_fails_closed_for_missing_and_corrupt_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"immutable fixture bytes"
            digest = hashlib.sha256(content).hexdigest()
            inventory = [{"sha256": digest, "byte_size": len(content)}]
            self.assertEqual(verify_artifact_inventory(root, inventory)[0]["reason"], "MISSING_ARTIFACT_BYTES")
            path = artifact_blob_path(root, digest)
            path.parent.mkdir()
            path.write_bytes(b"corrupt")
            self.assertEqual(verify_artifact_inventory(root, inventory)[0]["reason"], "CORRUPT_ARTIFACT_BYTES")

    def test_backup_output_must_not_overlap_active_artifact_root(self):
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active-artifacts"
            active.mkdir()
            before = sorted(active.iterdir())
            with self.assertRaisesRegex(OperationsFailure, "distinct from the active artifact root"):
                create_backup(dsn="synthetic-private-dsn", artifact_root=active, output_dir=active / "backup")
            self.assertEqual(sorted(active.iterdir()), before)

    def test_schema_migration_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dump = root / "database.dump"
            dump.write_bytes(b"logical dump fixture")
            migrations = _migration_identity(ROOT)
            manifest = {
                "schema_version": MANIFEST_SCHEMA,
                "dump": {"path": "database.dump", "sha256": file_sha256(dump)[0], "byte_size": dump.stat().st_size},
                "immutable_artifacts": {"backup_root": "artifacts", "inventory": [], "inventory_sha256": hashlib.sha256(b"[]").hexdigest()},
                "migration_schema_identity": migrations,
                "postgresql": {"server_major": 17},
            }
            # The deterministic JSON hash used by the tool is not a raw hash
            # of the two bytes; use its canonical identity through a fresh
            # empty inventory check and only mutate the schema identity here.
            manifest["immutable_artifacts"]["inventory_sha256"] = canonical_sha256([])
            path = root / "backup_manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            broken = copy.deepcopy(manifest)
            broken["migration_schema_identity"] = {"identity_sha256": "f" * 64}
            broken_path = root / "broken.json"
            broken_path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                verify_backup(manifest_path=broken_path, repo_root=ROOT)

    def test_post_snapshot_reconciliation_identifies_later_accepted_writes(self):
        backup = {"command_receipt": {"row_identity_hashes": ["before-receipt"]}}
        current = {"command_receipt": {"row_identity_hashes": ["before-receipt", "after-receipt"]}}
        report = build_reconciliation_report(
            backup_cutoff={"transaction_id": 10, "cutoff_at_server": "2026-09-19T10:00:00Z"},
            backup_tables=backup,
            current_tables=current,
            observed_at="2026-09-19T10:01:00Z",
        )
        self.assertEqual(report["post_cutoff_delta_count"], 1)
        self.assertFalse(report["post_cutoff_writes_present_in_restored_snapshot"])
        self.assertEqual(report["post_cutoff_deltas"][0]["classification"], "CONSEQUENTIAL_OR_EXTERNAL_REQUIRES_CONTROLLED_HANDLING")
        self.assertEqual(report["production_disaster_rpo_rto_claim"], "NOT_ESTABLISHED")

    def test_restored_acknowledged_receipt_audit_outbox_workflow_and_reads_match(self):
        tables = {
            "command_receipt": {"row_identity_hashes": ["receipt"], "content_sha256": "a"},
            "audit_event": {"row_identity_hashes": ["audit"], "content_sha256": "b"},
            "outbox_event": {"row_identity_hashes": ["outbox"], "content_sha256": "c"},
            "aggregate_state": {"row_identity_hashes": ["workflow-v2"], "content_sha256": "d"},
            "read_revision": {"row_identity_hashes": ["read-v1"], "content_sha256": "e"},
            "query_snapshot": {"row_identity_hashes": ["snapshot"], "content_sha256": "f"},
            "job": {"row_identity_hashes": ["job-epoch-2"], "content_sha256": "g"},
            "applied_effect": {"row_identity_hashes": ["effect-1"], "content_sha256": "h"},
        }
        report = build_reconciliation_report(
            backup_cutoff={"transaction_id": 20},
            backup_tables=tables,
            current_tables=json.loads(json.dumps(tables)),
            observed_at="2026-09-19T10:02:00Z",
        )
        self.assertEqual(report["post_cutoff_delta_count"], 0)
        self.assertFalse(report["required_controlled_recovery_action"])

    def test_stale_worker_effect_identity_classification_is_nonduplicating(self):
        report = build_reconciliation_report(
            backup_cutoff={"transaction_id": 30},
            backup_tables={"job": {"row_identity_hashes": ["old"]}, "applied_effect": {"row_identity_hashes": ["effect"]}},
            current_tables={"job": {"row_identity_hashes": ["old", "new"]}, "applied_effect": {"row_identity_hashes": ["effect", "effect-new"]}},
            observed_at="2026-09-19T10:03:00Z",
        )
        classes = {item["table"]: item["classification"] for item in report["post_cutoff_deltas"]}
        self.assertEqual(classes["job"], "SAFE_LOCAL_IDEMPOTENT_WORK_REPLAY_CANDIDATE")
        self.assertEqual(classes["applied_effect"], "ALREADY_APPLIED_LOCAL_EFFECT_DO_NOT_REPLAY")

    def test_o4_source_snapshot_capability_fingerprints_restore_without_authentic_claim(self):
        tables = {
            "source_snapshot": {"row_identity_hashes": ["snapshot"], "row_versions": [{"identity_hash": "snapshot", "row_hash": "manifest", "version": None}]},
            "source_capability": {"row_identity_hashes": ["capability"], "row_versions": [{"identity_hash": "capability", "row_hash": "ready", "version": None}]},
        }
        report = build_reconciliation_report(
            backup_cutoff={"transaction_id": 35},
            backup_tables=tables,
            current_tables=json.loads(json.dumps(tables)),
            observed_at="2026-09-19T10:03:30Z",
        )
        self.assertEqual(report["post_cutoff_delta_count"], 0)
        self.assertNotIn("authentic", json.dumps(report).lower())

    def test_reconciliation_reports_changed_state_identity_not_only_new_rows(self):
        report = build_reconciliation_report(
            backup_cutoff={"transaction_id": 36},
            backup_tables={"aggregate_state": {"row_identity_hashes": ["aggregate"], "row_versions": [{"identity_hash": "aggregate", "row_hash": "old"}]}},
            current_tables={"aggregate_state": {"row_identity_hashes": ["aggregate"], "row_versions": [{"identity_hash": "aggregate", "row_hash": "new"}]}},
            observed_at="2026-09-19T10:03:40Z",
        )
        self.assertEqual(report["post_cutoff_delta_count"], 1)
        self.assertEqual(report["post_cutoff_deltas"][0]["changed_identity_hashes"], ["aggregate"])

    def test_local_rehearsal_cannot_be_serialized_as_production_rpo_rto_pass(self):
        report = build_reconciliation_report(
            backup_cutoff={"transaction_id": 40},
            backup_tables={},
            current_tables={},
            observed_at="2026-09-19T10:04:00Z",
        )
        self.assertEqual(report["timing_evidence_scope"], "LOCAL_RESTORE_REHEARSAL_ONLY")
        self.assertEqual(report["production_disaster_rpo_rto_claim"], "NOT_ESTABLISHED")
        self.assertNotIn("RPO<=15m", json.dumps(report))
        self.assertNotIn("RTO<=60m", json.dumps(report))

    def test_native_tool_argv_is_secret_free_for_uri_and_keyword_conninfo(self):
        uri_password = "uri p@ss/%=:"
        uri = "postgresql://native-user:" + quote(uri_password, safe="") + "@db.internal:6543/ephi?sslmode=require"
        keyword_password = "keyword p@ss/%=:"
        keyword = (
            "host=db.internal port=6543 user=native-user password='"
            + keyword_password
            + "' dbname=ephi sslmode=require"
        )
        prepared_parameters = {
            uri: {
                "host": "db.internal",
                "port": "6543",
                "user": "native-user",
                "password": uri_password,
                "dbname": "ephi",
                "sslmode": "require",
            },
            keyword: {
                "host": "db.internal",
                "port": "6543",
                "user": "native-user",
                "password": keyword_password,
                "dbname": "ephi",
                "sslmode": "require",
            },
        }
        calls = []
        service_file_records = []

        def fake_run(command, **kwargs):
            calls.append({"argv": list(command), "kwargs": kwargs})
            if "--dbname=service=o9_restore" in command:
                service_path = Path(kwargs["env"]["PGSERVICEFILE"])
                service_file_records.append(
                    (
                        service_path.read_text(encoding="utf-8"),
                        service_path.stat().st_mode & 0o777,
                    )
                )
            return SimpleNamespace(returncode=0, stdout=keyword_password.encode(), stderr=uri_password.encode())

        def prepared_parser(dsn, *, database=None):
            parameters = dict(prepared_parameters[dsn])
            if database is not None:
                parameters["dbname"] = database
            return parameters

        with tempfile.TemporaryDirectory() as directory:
            dump_path = Path(directory) / "database.dump"
            dump_path.write_bytes(b"logical dump fixture")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("ephi.o9_operations._libpq_parameters", side_effect=prepared_parser):
                with patch("ephi.o9_operations.subprocess.run", side_effect=fake_run):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        _run_dump(["pg_dump"], dsn=uri, snapshot="00000003-1", output=Path(directory) / "new.dump")
                        _run_restore(["pg_restore"], dsn=keyword, dump_path=dump_path)

        self.assertEqual(len(calls), 2)
        self.assertNotIn(uri, repr(calls[0]["argv"]))
        self.assertNotIn(keyword, repr(calls[1]["argv"]))
        self.assertNotIn(uri_password, repr(calls[0]["argv"]))
        self.assertNotIn(keyword_password, repr(calls[1]["argv"]))
        for private_value in ("native-user", "db.internal", "ephi"):
            self.assertNotIn(private_value, repr(calls[0]["argv"]))
            self.assertNotIn(private_value, repr(calls[1]["argv"]))
        self.assertNotIn(uri_password, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(keyword_password, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(calls[0]["kwargs"]["env"]["PGPASSWORD"], uri_password)
        self.assertEqual(calls[1]["kwargs"]["env"]["PGPASSWORD"], keyword_password)
        self.assertEqual(calls[0]["kwargs"]["env"]["PGHOST"], "db.internal")
        self.assertEqual(calls[0]["kwargs"]["env"]["PGPORT"], "6543")
        self.assertEqual(calls[0]["kwargs"]["env"]["PGUSER"], "native-user")
        self.assertEqual(calls[0]["kwargs"]["env"]["PGDATABASE"], "ephi")
        self.assertNotIn("DATABASE_URL", calls[0]["kwargs"]["env"])
        self.assertNotIn("EPHI_POSTGRES_DSN", calls[0]["kwargs"]["env"])
        self.assertEqual(service_file_records, [("[o9_restore]\n", 0o600)])
        self.assertEqual(calls[1]["kwargs"]["env"]["PGUSER"], "native-user")
        self.assertEqual(calls[1]["kwargs"]["env"]["PGHOST"], "db.internal")
        self.assertEqual(calls[1]["kwargs"]["env"]["PGDATABASE"], "ephi")
        self.assertEqual(calls[1]["kwargs"]["env"]["PGPASSWORD"], keyword_password)
        self.assertNotIn(keyword_password, service_file_records[0][0])

    def test_private_libpq_environment_is_dependency_free_and_clears_ambient_settings(self):
        secret = "prepared p@ss"
        environment = _private_libpq_environment(
            {"host": "db.internal", "password": secret, "dbname": "ephi"},
            base_environment={
                "PGPASSWORD": "ambient-secret",
                "DATABASE_URL": "postgresql://ambient-secret",
                "EPHI_POSTGRES_DSN": "host=db.internal password=ambient-secret",
                "PATH": "/usr/bin",
            },
        )
        self.assertEqual(environment["PGPASSWORD"], secret)
        self.assertEqual(environment["PGHOST"], "db.internal")
        self.assertEqual(environment["PGDATABASE"], "ephi")
        self.assertNotIn("DATABASE_URL", environment)
        self.assertNotIn("EPHI_POSTGRES_DSN", environment)
        self.assertEqual(environment["PATH"], "/usr/bin")

    def test_native_tool_failure_text_does_not_include_secret_or_stderr(self):
        secret = "failure-only-secret"
        calls = []

        def fake_run(command, **kwargs):
            calls.append({"argv": list(command), "kwargs": kwargs})
            return SimpleNamespace(returncode=1, stdout=b"", stderr=secret.encode())

        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            dump_path = Path(directory) / "database.dump"
            dump_path.write_bytes(b"logical dump fixture")
            with patch(
                "ephi.o9_operations._libpq_parameters",
                return_value={"host": "db.internal", "user": "native-user", "password": secret, "dbname": "ephi"},
            ):
                with patch("ephi.o9_operations.subprocess.run", side_effect=fake_run):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        with self.assertRaises(OperationsFailure) as raised:
                            _run_restore(
                                ["pg_restore"],
                                dsn="host=db.internal user=native-user password='" + secret + "' dbname=ephi",
                                dump_path=dump_path,
                            )
        self.assertEqual(len(calls), 1)
        self.assertNotIn(secret, calls[0]["argv"])
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())


class O9InstalledRecoveryAuthorityTests(unittest.TestCase):
    def _manifest(self, directory: Path, *, with_artifact: bool = False) -> dict[str, object]:
        class Cursor:
            def fetchall(self):
                return []

        class Connection:
            def execute(self, _query):
                return Cursor()

        tables = o9_authority._table_inventory(Connection())
        artifact_inventory = []
        artifact_content = b"deterministic immutable O9 fixture"
        if with_artifact:
            digest = hashlib.sha256(artifact_content).hexdigest()
            artifact_path = artifact_blob_path(directory / "artifacts", digest)
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_bytes(artifact_content)
            artifact_inventory.append({
                "scope_key_sha256": o9_authority.safe_identity_hash("private-scope-sentinel"),
                "sha256": digest,
                "byte_size": len(artifact_content),
                "metadata_sha256": canonical_sha256({
                    "media_type": "application/octet-stream",
                    "logical_purpose": "private-material-sentinel",
                    "producing_job_id": "private-job-sentinel",
                    "revision_id": None,
                }),
            })
        dump_content = b"deterministic logical dump fixture"
        (directory / "database.dump").write_bytes(dump_content)
        return {
            "schema_version": MANIFEST_SCHEMA,
            "operation": "CHG-147/O9.1",
            "release_identity": installed_release_identity(),
            "migration_schema_identity": _migration_identity(),
            "postgresql": {
                "server_version": "18.6",
                "server_major": 18,
                "safe_database_identity": {
                    "database_identity_sha256": o9_authority.safe_identity_hash("private-database-sentinel"),
                    "schema_identity_sha256": o9_authority.safe_identity_hash("private-schema-sentinel"),
                    "connection": {"configured": True, "credentials_redacted": True},
                },
                "tooling": {"pg_dump_version": "PostgreSQL 18.6", "pg_dump_major": 18},
            },
            "backup_cutoff_high_water": {
                "cutoff_at_server": "2026-09-27T10:00:00.000000Z",
                "transaction_id": 100,
                "transaction_snapshot": "100:100:",
                "snapshot_exported_for_pg_dump": True,
            },
            "dump": {
                "path": "database.dump",
                "sha256": hashlib.sha256(dump_content).hexdigest(),
                "byte_size": len(dump_content),
            },
            "immutable_artifacts": {
                "backup_root": "artifacts",
                "count": len(artifact_inventory),
                "inventory_sha256": canonical_sha256(artifact_inventory),
                "inventory": artifact_inventory,
            },
            "durable_state": {"tables": tables, "state_sha256": tables["_state"]["content_sha256"]},
            "verification": {"overall_verification_state": "VERIFIED"},
        }

    def _verify(self, manifest_path: Path, **patches):
        with patch.object(o9_authority, "_tool_prefix", return_value=["pg_restore"]), \
             patch.object(o9_authority, "_tool_version", return_value=("PostgreSQL 18.6", 18)), \
             patch.object(o9_authority, "_verify_dump_structure"):
            for name, value in patches.items():
                patcher = patch.object(o9_authority, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return verify_backup(manifest_path=manifest_path)

    def test_backup_verify_positive_control_binds_release_and_migrations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self._manifest(root, with_artifact=True)
            path = root / "backup_manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self._verify(path)
        self.assertEqual(result["verification_state"], "VERIFIED")
        self.assertEqual(result["release_identity"], installed_release_identity())
        self.assertEqual(result["migration_schema_identity"], _migration_identity())

    def test_backup_verify_fails_closed_for_corrupt_dump_and_artifacts(self):
        for corruption in ("dump", "missing_artifact", "corrupt_artifact"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                manifest = self._manifest(root, with_artifact=True)
                path = root / "backup_manifest.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                if corruption == "dump":
                    (root / "database.dump").write_bytes(b"corrupt")
                else:
                    digest = manifest["immutable_artifacts"]["inventory"][0]["sha256"]
                    artifact = artifact_blob_path(root / "artifacts", digest)
                    if corruption == "missing_artifact":
                        artifact.unlink()
                    else:
                        artifact.write_bytes(b"corrupt")
                with self.assertRaises(OperationsFailure):
                    self._verify(path)

    def test_backup_verify_rejects_changed_or_unsupported_identity_and_native_major(self):
        for identity, expected in (
            ("release", "packaged release identity mismatch"),
            ("migration", "migration/schema identity mismatch"),
            ("schema", "backup manifest schema/version is unsupported"),
            ("native_major", "pg_restore major version does not match the PostgreSQL server"),
        ):
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                manifest = self._manifest(root)
                if identity == "release":
                    manifest["release_identity"]["release_identity_sha256"] = "f" * 64
                elif identity == "migration":
                    manifest["migration_schema_identity"]["identity_sha256"] = "f" * 64
                elif identity == "schema":
                    manifest["schema_version"] = "o9.1.backup.v99"
                path = root / "backup_manifest.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                if identity == "native_major":
                    with patch.object(o9_authority, "_tool_prefix", return_value=["pg_restore"]), \
                         patch.object(o9_authority, "_tool_version", return_value=("PostgreSQL 17.11", 17)):
                        with self.assertRaisesRegex(OperationsFailure, expected):
                            verify_backup(manifest_path=path)
                else:
                    with self.assertRaisesRegex(OperationsFailure, expected):
                        self._verify(path)

    def test_malformed_manifest_and_legacy_v1_fail_with_bounded_reasons(self):
        secret_path = "../private-path-sentinel"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self._manifest(root)
            manifest["dump"]["path"] = secret_path
            path = root / "bad.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(OperationsFailure) as raised:
                self._verify(path)
            self.assertNotIn(secret_path, str(raised.exception))
            manifest["schema_version"] = LEGACY_MANIFEST_SCHEMA
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(OperationsFailure, "legacy backup manifest lacks packaged release identity"):
                verify_backup(manifest_path=path)

    def test_legacy_reconcile_projects_only_hashed_recovery_facts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self._manifest(root)
            manifest["schema_version"] = LEGACY_MANIFEST_SCHEMA
            manifest["repository"] = {"candidate_branch": "o9-private-raw-row-sentinel"}
            manifest["postgresql"]["server_version"] = "PostgreSQL 18.6 o9-private-exception-sentinel"
            manifest["postgresql"]["tooling"]["pg_dump_version"] = (
                "pg_dump (PostgreSQL) 18.6 o9-private-path-sentinel"
            )
            manifest["postgresql"]["safe_database_identity"]["database_name"] = (
                "o9-private-database-sentinel"
            )
            tables = manifest["durable_state"]["tables"]
            audit = tables["audit_event"]
            audit_row = {
                "identity_hash": "a" * 64,
                "row_hash": "b" * 64,
                "version": "o9-private-raw-row-sentinel",
            }
            audit["row_count"] = 1
            audit["row_identity_hashes"] = [audit_row["identity_hash"]]
            audit["row_versions"] = [audit_row]
            audit["content_sha256"] = canonical_sha256([audit_row])
            tables["private-table-sentinel"] = {"raw": "o9-private-material-identifier-sentinel"}
            state_rows = sum(tables[table]["row_count"] for table in o9_authority.CRITICAL_TABLES)
            fingerprints = [
                {
                    "table": table,
                    "row_count": tables[table]["row_count"],
                    "content_sha256": tables[table]["content_sha256"],
                }
                for table in o9_authority.CRITICAL_TABLES
            ]
            tables["_state"]["row_count"] = state_rows
            tables["_state"]["content_sha256"] = canonical_sha256(fingerprints)
            manifest["durable_state"]["state_sha256"] = tables["_state"]["content_sha256"]
            path = root / "legacy.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")

            class Cursor:
                def fetchone(self):
                    return {"now": "2026-09-27T10:05:00+00:00"}

            class Connection:
                def execute(self, _query):
                    return Cursor()

                def close(self):
                    return None

            connection = Connection()
            current = {table: tables[table] for table in o9_authority.CRITICAL_TABLES}
            with patch.object(o9_authority, "_connect", return_value=connection), \
                 patch.object(o9_authority, "validate_required_schema"), \
                 patch.object(o9_authority, "_table_inventory", return_value=current):
                report = reconcile(manifest_path=path, dsn="private-dsn-sentinel")

        encoded = json.dumps(report, sort_keys=True)
        self.assertEqual(report["backup_identity_state"], "LEGACY_RELEASE_IDENTITY_UNBOUND")
        for private_value in (
            "o9-private-raw-row-sentinel",
            "o9-private-material-identifier-sentinel",
            "o9-private-exception-sentinel",
            "o9-private-path-sentinel",
            "o9-private-database-sentinel",
            "private-table-sentinel",
            "candidate_branch",
        ):
            self.assertNotIn(private_value, encoded)

    def test_checkout_wrapper_delegates_all_four_operations_to_package_authority(self):
        calls = (
            ("create_backup", checkout_create_backup),
            ("verify_backup", checkout_verify_backup),
            ("restore_rehearsal", checkout_restore_rehearsal),
            ("reconcile", checkout_reconcile),
        )
        for name, wrapper in calls:
            with self.subTest(operation=name), patch.object(o9_authority, name, return_value=name) as delegated:
                self.assertEqual(wrapper(marker=name), name)
                delegated.assert_called_once_with(marker=name)


try:
    PSYCOPG_AVAILABLE = importlib.util.find_spec("psycopg") is not None
except ModuleNotFoundError:
    PSYCOPG_AVAILABLE = False


@unittest.skipUnless(PSYCOPG_AVAILABLE, "psycopg[binary]==3.3.6 is unavailable; parser qualification is NOT_RUN")
class O9RealLibpqParserQualificationTests(unittest.TestCase):
    def test_real_psycopg_parser_is_secret_safe_for_uri_and_keyword_conninfo(self):
        uri_password = "uri p@ss/%=:"
        uri = "postgresql://native-user:" + quote(uri_password, safe="") + "@db.internal:6543/ephi?sslmode=require"
        keyword_password = "keyword p@ss/%=:"
        keyword = (
            "host=db.internal port=6543 user=native-user password='"
            + keyword_password
            + "' dbname=ephi sslmode=require"
        )
        from ephi.o9_operations import _libpq_parameters

        uri_parameters = _libpq_parameters(uri)
        keyword_parameters = _libpq_parameters(keyword)
        self.assertEqual(uri_parameters["password"], uri_password)
        self.assertEqual(keyword_parameters["password"], keyword_password)
        self.assertEqual(uri_parameters["dbname"], "ephi")
        self.assertEqual(keyword_parameters["dbname"], "ephi")

        calls = []

        def fake_run(command, **kwargs):
            calls.append({"argv": list(command), "kwargs": kwargs})
            return SimpleNamespace(returncode=0, stdout=keyword_password.encode(), stderr=uri_password.encode())

        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            dump_path = Path(directory) / "database.dump"
            dump_path.write_bytes(b"logical dump fixture")
            with patch("ephi.o9_operations.subprocess.run", side_effect=fake_run):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    _run_dump(["pg_dump"], dsn=uri, snapshot="00000003-1", output=Path(directory) / "new.dump")
                    _run_restore(["pg_restore"], dsn=keyword, dump_path=dump_path)

        target = _dsn_with_database(uri, "ephi_restore")
        self.assertEqual(len(calls), 2)
        self.assertNotIn(uri, repr(calls[0]["argv"]))
        self.assertNotIn(keyword, repr(calls[1]["argv"]))
        self.assertNotIn(uri_password, repr(calls[0]["argv"]))
        self.assertNotIn(keyword_password, repr(calls[1]["argv"]))
        self.assertNotIn(uri_password, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(keyword_password, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(calls[0]["kwargs"]["env"]["PGPASSWORD"], uri_password)
        self.assertEqual(calls[1]["kwargs"]["env"]["PGPASSWORD"], keyword_password)
        self.assertIn("ephi_restore", target)
        self.assertIn(uri_password, target)
        self.assertNotIn("/ephi?", target)


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class O9WorkerHealthPostgreSQLTests(unittest.TestCase):
    scope_key = "o9-worker-health-test"

    def setUp(self):
        from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter

        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self._cleanup)
        self.adapter.connection.execute("TRUNCATE job CASCADE")
        self._delete_jobs()

    def _delete_jobs(self):
        self.adapter.connection.execute("DELETE FROM job WHERE scope_key = %s", (self.scope_key,))

    def _cleanup(self):
        try:
            self._delete_jobs()
        finally:
            self.adapter.close()

    def _insert_job(self, status, *, lease_expression="NULL"):
        self._delete_jobs()
        self.adapter.connection.execute(
            f"""
            INSERT INTO job(
                job_id, scope_key, job_type, semantic_key, payload_hash, payload_json,
                status, max_attempts, lease_owner, lease_expires_at
            ) VALUES (%s, %s, 'O9HealthFixture', %s, %s, '{{}}'::jsonb, %s, 3,
                      %s, {lease_expression})
            """,
            ("o9-health-job", self.scope_key, "o9-health-job", "a" * 64, status, "o9-worker" if status == "RUNNING" else None),
        )

    def _worker_axis(self):
        return operations_status(dsn=DSN, artifact_root=None)["axes"]["durable_worker_job_state"]

    def test_worker_health_is_authoritative_and_independent_from_source_capability(self):
        ready = self._worker_axis()
        self.assertEqual(ready["state"], "READY")
        self.assertEqual(ready["facts"]["expired_running_count"], 0)

        self._insert_job("FAILED")
        failed = self._worker_axis()
        self.assertEqual(failed["state"], "ERROR")
        self.assertEqual(failed["reason"], "DURABLE_WORKER_TERMINAL_FAILURE")
        self.assertEqual(failed["facts"]["failed_count"], 1)

        self._insert_job("DEAD_LETTER")
        dead_letter = self._worker_axis()
        self.assertEqual(dead_letter["state"], "ERROR")
        self.assertEqual(dead_letter["facts"]["dead_letter_count"], 1)

        self._insert_job("RUNNING", lease_expression="clock_timestamp() - interval '1 second'")
        expired = self._worker_axis()
        self.assertEqual(expired["state"], "STALE")
        self.assertEqual(expired["reason"], "DURABLE_WORKER_EXPIRED_LEASE")
        self.assertEqual(expired["facts"]["expired_running_count"], 1)

        self._insert_job("RUNNING", lease_expression="clock_timestamp() + interval '1 hour'")
        current = operations_status(dsn=DSN, artifact_root=None)
        worker = current["axes"]["durable_worker_job_state"]
        self.assertEqual(worker["state"], "READY")
        self.assertEqual(worker["facts"]["expired_running_count"], 0)
        self.assertTrue(worker["facts"]["authoritative_clock_used"])
        self.assertEqual(current["axes"]["process_transport"]["state"], "READY")
        self.assertEqual(current["axes"]["postgres_readiness_durability"]["state"], "READY")
        self.assertEqual(current["axes"]["source_capability_freshness"]["state"], "UNAVAILABLE")
        self.assertEqual(current["axes"]["source_capability_freshness"]["reason"], "BLOCKED_REAL_SOURCE")
        encoded = json.dumps(current, sort_keys=True)
        self.assertNotIn("o9-health-job", encoded)
        self.assertNotIn("O9HealthFixture", encoded)


if __name__ == "__main__":
    unittest.main()
