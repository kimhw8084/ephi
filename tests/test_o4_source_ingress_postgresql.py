"""CHG-144/O4.1 real PostgreSQL source-manifest evidence."""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    ArtifactService,
    MutableCurrentAuthorizationAuthority,
    MetrologyObservation,
    MetrologySourceBinding,
    Principal,
    SourceSnapshotNotFoundError,
    SourceCapabilityState,
    SourceSnapshotConflictError,
    SourceSnapshotDraft,
    SourceSnapshotIngressService,
    SourceSnapshotStatus,
)
from ephi.infrastructure import (  # noqa: E402
    FileArtifactBlobStore,
    PostgreSQLArtifactCatalog,
    PostgreSQLReferenceTransactionAdapter,
    PostgreSQLSourceSnapshotStore,
)


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLSourceIngressTests(unittest.TestCase):
    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.adapter.connection.execute("TRUNCATE source_capability, source_snapshot, artifact_catalog")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.scope = AccessScope("o4-pg-scope", site_id="site-1", area_id="area-1", family_id="family-1")
        self.other_scope = AccessScope("o4-other-scope", site_id="site-2")
        self.principal = Principal(
            "o4-engineer",
            ("ephi.source.read", "ephi.source.ingest", "ephi.source.artifact.read", "ephi.source.artifact.write"),
            (self.scope,),
            1,
            1,
        )
        self.current_authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.binding = MetrologySourceBinding(
            self.scope,
            "real-source-contract-test",
            "approved-provider-test",
            "family-1",
            "metrology.measurement",
            "test.authority",
            "schema-v1",
            "mapping-v1",
            "d" * 64,
            "mm",
        )
        self.blob_store = FileArtifactBlobStore(Path(self.temp.name) / "blobs", max_artifact_size=4096)
        self.artifact_service = ArtifactService(
            self.blob_store,
            PostgreSQLArtifactCatalog(self.adapter),
            self.current_authorization,
        )
        self.store = PostgreSQLSourceSnapshotStore(self.adapter)

    def observation(self, *, age_seconds=60):
        return MetrologyObservation(
            "row-1",
            "asset-1",
            "tool-1",
            "head-1",
            "context-1",
            "characteristic-1",
            "mm",
            2.5,
            self.now - timedelta(seconds=age_seconds + 30),
            self.now - timedelta(seconds=age_seconds),
        )

    def draft(self, *, content=b"raw bounded source bytes", partition="partition-1", status=SourceSnapshotStatus.PUBLISHED, age_seconds=60, binding=None):
        written = self.artifact_service.write_and_register(
            self.principal,
            self.scope,
            content,
            media_type="application/octet-stream",
            logical_purpose="o4-source-snapshot-manifest-input",
            required_write_capability="ephi.source.artifact.write",
        )
        row = self.observation(age_seconds=age_seconds)
        return SourceSnapshotDraft(
            binding or self.binding,
            partition,
            "revision-1",
            row.event_at,
            row.event_at,
            row.source_available_at,
            written.metadata.reference,
            (row,),
            status,
        )

    def service(self, now=None):
        return SourceSnapshotIngressService(
            self.store,
            self.artifact_service,
            clock=lambda: self.now if now is None else now,
        )

    def test_migration_has_only_bounded_manifest_and_capability_fields(self):
        self.adapter.apply_migrations()
        snapshot_columns = {
            row["column_name"]
            for row in self.adapter.connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'source_snapshot'"
            ).fetchall()
        }
        self.assertIn("manifest_artifact_sha256", snapshot_columns)
        self.assertIn("available_cutoff", snapshot_columns)
        self.assertNotIn("raw_rows_json", snapshot_columns)
        self.assertNotIn("telemetry_value_json", snapshot_columns)

    def test_source_snapshot_rows_are_immutable(self):
        draft = self.draft(partition="immutable")
        record, _ = self.service().publish(self.principal, draft, freshness_age_seconds=3600)
        with self.assertRaises(Exception):
            self.adapter.connection.execute(
                "UPDATE source_snapshot SET row_count = row_count + 1 WHERE snapshot_id = %s",
                (record.snapshot_id,),
            )
        with self.assertRaises(Exception):
            self.adapter.connection.execute(
                "DELETE FROM source_snapshot WHERE snapshot_id = %s",
                (record.snapshot_id,),
            )
        self.assertEqual(
            self.adapter.connection.execute("SELECT COUNT(*) AS count FROM source_snapshot").fetchone()["count"],
            1,
        )

    def test_artifact_verification_precedes_publication_and_restart_is_idempotent(self):
        draft = self.draft()
        first, capability = self.service().publish(self.principal, draft, freshness_age_seconds=3600)
        self.assertEqual(capability.state, SourceCapabilityState.READY)
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) AS count FROM source_snapshot").fetchone()["count"], 1)
        self.adapter.close()
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.store = PostgreSQLSourceSnapshotStore(self.adapter)
        self.artifact_service = ArtifactService(
            self.blob_store,
            PostgreSQLArtifactCatalog(self.adapter),
            self.current_authorization,
        )
        replay, replay_capability = self.service().publish(self.principal, draft, freshness_age_seconds=3600)
        self.assertEqual(replay.snapshot_id, first.snapshot_id)
        self.assertEqual(replay.manifest_hash, first.manifest_hash)
        self.assertEqual(replay_capability.state, SourceCapabilityState.READY)
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) AS count FROM source_snapshot").fetchone()["count"], 1)

    def test_same_logical_revision_conflict_does_not_overwrite_history(self):
        first, _ = self.service().publish(self.principal, self.draft(), freshness_age_seconds=3600)
        with self.assertRaises(SourceSnapshotConflictError):
            self.service().publish(
                self.principal,
                self.draft(content=b"different exact source bytes"),
                freshness_age_seconds=3600,
            )
        row = self.adapter.connection.execute(
            "SELECT manifest_artifact_sha256, manifest_hash FROM source_snapshot WHERE snapshot_id = %s",
            (first.snapshot_id,),
        ).fetchone()
        self.assertEqual(row["manifest_artifact_sha256"], first.artifact_reference.content.sha256)
        self.assertEqual(row["manifest_hash"], first.manifest_hash)

    def test_scope_isolation_and_truthful_partial_stale_and_missing_states(self):
        first, _ = self.service().publish(self.principal, self.draft(), freshness_age_seconds=3600)
        with self.assertRaises(SourceSnapshotNotFoundError):
            self.store.get_snapshot(
                Principal("other", ("ephi.source.read",), (self.other_scope,), 1, 1),
                self.other_scope,
                first.snapshot_id,
            )
        partial, partial_capability = self.service().publish(
            self.principal,
            self.draft(partition="partial", status=SourceSnapshotStatus.PARTIAL),
            freshness_age_seconds=3600,
        )
        self.assertEqual(partial.status, SourceSnapshotStatus.PARTIAL)
        self.assertEqual(partial_capability.state, SourceCapabilityState.PARTIAL)
        stale_binding = replace(self.binding, capability_id="stale-capability")
        stale, stale_capability = self.service(now=self.now).publish(
            self.principal,
            self.draft(partition="stale", age_seconds=7200, binding=stale_binding),
            freshness_age_seconds=3600,
        )
        self.assertEqual(stale.status, SourceSnapshotStatus.PUBLISHED)
        self.assertEqual(stale_capability.state, SourceCapabilityState.STALE)
        missing = self.store.get_capability(
            self.principal,
            MetrologySourceBinding(
                self.scope, "missing-source", "provider", "family-1", "capability", "adapter", "schema", "v1", "e" * 64, "mm"
            ),
        )
        self.assertEqual(missing.state, SourceCapabilityState.UNAVAILABLE)

    def test_missing_or_corrupt_artifact_blocks_snapshot_publication(self):
        row = self.observation()
        from ephi.application import ArtifactContentIdentity, ScopedArtifactReference

        missing_ref = ScopedArtifactReference(self.scope, ArtifactContentIdentity("f" * 64, 12))
        missing = SourceSnapshotDraft(
            self.binding, "missing-artifact", "revision-1", row.event_at, row.event_at,
            row.source_available_at, missing_ref, (row,),
        )
        with self.assertRaises(Exception) as missing_error:
            self.service().publish(self.principal, missing, freshness_age_seconds=3600)
        self.assertEqual(missing_error.exception.code, "ARTIFACT_NOT_FOUND")
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) AS count FROM source_snapshot").fetchone()["count"], 0)

        draft = self.draft(partition="corrupt-artifact")
        self.service().publish(self.principal, draft, freshness_age_seconds=3600)
        object_path = Path(self.temp.name) / "blobs" / draft.artifact_reference.content.sha256[:2] / draft.artifact_reference.content.sha256[2:]
        object_path.write_bytes(b"corrupt")
        corrupt = self.draft(content=b"corrupt-target", partition="corrupt-artifact-2")
        corrupt_path = Path(self.temp.name) / "blobs" / corrupt.artifact_reference.content.sha256[:2] / corrupt.artifact_reference.content.sha256[2:]
        corrupt_path.write_bytes(b"corrupt")
        with self.assertRaises(Exception) as corrupt_error:
            self.service().publish(self.principal, corrupt, freshness_age_seconds=3600)
        self.assertEqual(corrupt_error.exception.code, "ARTIFACT_INTEGRITY_ERROR")


if __name__ == "__main__":
    unittest.main()
