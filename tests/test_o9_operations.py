"""CHG-147/O9.1 focused operations-health and restore-contract evidence."""

from pathlib import Path
import copy
import hashlib
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    OperationalState,
    OperationsAxis,
    operations_health_snapshot,
)
from ephi.application.operations import (  # noqa: E402
    artifact_blob_path,
    build_reconciliation_report,
    file_sha256,
    verify_artifact_inventory,
)
from ephi.application.source_reality import redacted_connection_facts  # noqa: E402
from tools.o9_operations import (  # noqa: E402
    MANIFEST_SCHEMA,
    ROOT,
    _migration_identity,
    verify_backup,
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
        self.assertEqual(first["migration_count"], 6)
        self.assertEqual(len(first["identity_sha256"]), 64)
        self.assertEqual([item["path"] for item in first["files"]], sorted(item["path"] for item in first["files"]))

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
            manifest["immutable_artifacts"]["inventory_sha256"] = __import__("tools.o9_operations", fromlist=["canonical_sha256"]).canonical_sha256([])
            path = root / "backup_manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            broken = copy.deepcopy(manifest)
            broken["migration_schema_identity"] = {"identity_sha256": "f" * 64}
            broken_path = root / "broken.json"
            broken_path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                verify_backup(manifest_path=broken_path, repo_root=ROOT, pg_restore_command="/usr/bin/true")

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


if __name__ == "__main__":
    unittest.main()
