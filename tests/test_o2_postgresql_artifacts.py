"""CHG-133 PostgreSQL scoped artifact catalog integration evidence."""

import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    ArtifactIntegrityError,
    ArtifactMetadata,
    ArtifactMetadataConflictError,
    ArtifactNotFoundError,
    ArtifactService,
    AuthorizationDeniedError,
    MutableCurrentAuthorizationAuthority,
    Principal,
    ScopedArtifactReference,
)
from ephi.infrastructure import (  # noqa: E402
    FileArtifactBlobStore,
    PostgreSQLArtifactCatalog,
    PostgreSQLReferenceTransactionAdapter,
)


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLArtifactCatalogTests(unittest.TestCase):
    write_capability = "artifact.fixture.write"
    read_capability = "artifact.fixture.read"

    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.adapter.connection.execute("TRUNCATE artifact_catalog")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "blobs"
        self.scope = AccessScope("pg-artifact-scope", site_id="site-1", area_id="area-1")
        self.other_scope = AccessScope("pg-other-scope", site_id="site-2")
        self.principal = Principal(
            "subject-1",
            (self.write_capability, self.read_capability),
            (self.scope,),
            3,
            4,
        )
        self.store = FileArtifactBlobStore(self.root, max_artifact_size=1024)
        self.catalog = PostgreSQLArtifactCatalog(self.adapter)
        self.current_authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.service = ArtifactService(self.store, self.catalog, self.current_authorization)

    def write(self, content=b"postgres artifact", **kwargs):
        return self.service.write_and_register(
            self.principal,
            self.scope,
            content,
            media_type=kwargs.pop("media_type", "application/octet-stream"),
            logical_purpose=kwargs.pop("logical_purpose", "fixture-evidence"),
            required_write_capability=self.write_capability,
            **kwargs,
        )

    def test_migration_is_idempotent_and_catalog_has_no_transport_identity(self):
        self.adapter.apply_migrations()
        columns = {
            row["column_name"]
            for row in self.adapter.connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'artifact_catalog'"
            ).fetchall()
        }
        self.assertEqual(
            columns,
            {
                "scope_key",
                "sha256",
                "byte_size",
                "media_type",
                "logical_purpose",
                "object_key",
                "producing_job_id",
                "revision_id",
                "created_at",
            },
        )
        self.assertNotIn("filesystem_path", columns)
        self.assertNotIn("download_url", columns)
        self.assertNotIn("signed_url", columns)

    def test_write_restart_retrieve_and_current_authorization(self):
        written = self.write(producing_job_id="job-1", revision_id="revision-1")
        reference = written.metadata.reference
        self.adapter.close()
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        service = ArtifactService(
            FileArtifactBlobStore(self.root, max_artifact_size=1024),
            PostgreSQLArtifactCatalog(self.adapter),
            self.current_authorization,
        )
        retrieved = service.retrieve(self.principal, reference, self.read_capability)
        self.assertEqual(retrieved.content, b"postgres artifact")
        self.assertEqual(retrieved.metadata.immutable_metadata_key(), written.metadata.immutable_metadata_key())

        with self.assertRaises(AuthorizationDeniedError):
            service.retrieve(
                Principal("subject-2", (self.read_capability,), (self.other_scope,), 4, 5),
                reference,
                self.read_capability,
            )
        with self.assertRaises(AuthorizationDeniedError):
            service.retrieve(
                Principal("subject-1", (), (self.scope,), 4, 5),
                reference,
                self.read_capability,
            )

    def test_one_physical_hash_has_separate_scoped_catalog_records(self):
        identity = self.store.put_bytes(b"shared physical bytes").content
        first = ArtifactMetadata(
            ScopedArtifactReference(self.scope, identity),
            "application/octet-stream",
            "fixture",
        )
        second = ArtifactMetadata(
            ScopedArtifactReference(self.other_scope, identity),
            "application/octet-stream",
            "fixture",
        )
        self.service.register_existing(self.principal, first, required_write_capability=self.write_capability)
        other_principal = Principal(
            "subject-2",
            (self.write_capability, self.read_capability),
            (self.other_scope,),
            1,
            1,
        )
        other_authority = MutableCurrentAuthorizationAuthority(other_principal)
        other_service = ArtifactService(self.store, self.catalog, other_authority)
        other_service.register_existing(other_principal, second, required_write_capability=self.write_capability)
        self.assertEqual(self.catalog.count(), 2)
        with self.assertRaises(AuthorizationDeniedError):
            self.service.retrieve(self.principal, second.reference, self.read_capability)

    def test_registration_is_idempotent_or_typed_conflict_without_rewrite(self):
        first = self.write()
        replay = self.write()
        self.assertFalse(replay.catalog_created)
        self.assertEqual(replay.metadata, first.metadata)
        with self.assertRaises(ArtifactMetadataConflictError):
            self.write(logical_purpose="different-purpose")
        self.assertEqual(self.catalog.count(), 1)

    def test_missing_or_corrupt_registration_and_post_registration_reads_fail_closed(self):
        content = b"register me"
        identity = self.store.put_bytes(content).content
        reference = ScopedArtifactReference(self.scope, identity)
        metadata = ArtifactMetadata(reference, "application/octet-stream", "fixture")
        object_path = self.root / identity.sha256[:2] / identity.sha256[2:]
        object_path.unlink()
        with self.assertRaises(ArtifactNotFoundError):
            self.service.register_existing(self.principal, metadata, required_write_capability=self.write_capability)
        self.assertEqual(self.catalog.count(), 0)

        self.store.put_bytes(content)
        self.service.register_existing(self.principal, metadata, required_write_capability=self.write_capability)
        object_path.write_bytes(b"corrupt after registration")
        with self.assertRaises(ArtifactIntegrityError):
            self.service.retrieve(self.principal, reference, self.read_capability)
        with self.assertRaises(ArtifactIntegrityError):
            self.service.verify_publish_preconditions(self.principal, (reference,), self.read_capability)


if __name__ == "__main__":
    unittest.main()
