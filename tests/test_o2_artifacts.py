"""CHG-133 offline immutable-artifact and scoped-authorization evidence."""

from pathlib import Path
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    ArtifactContentIdentity,
    ArtifactIntegrityError,
    ArtifactMetadata,
    ArtifactMetadataConflictError,
    ArtifactNotFoundError,
    ArtifactService,
    ArtifactStorageConfigurationError,
    ArtifactStorageSafetyError,
    ArtifactTooLargeError,
    ArtifactWriteInterruptedError,
    AuthorizationDeniedError,
    Principal,
    ScopeDeniedError,
    ScopedArtifactReference,
    ValidationFailureError,
)
from ephi.infrastructure import (  # noqa: E402
    FileArtifactBlobStore,
    SQLiteArtifactCatalog,
)


class ArtifactReferenceTests(unittest.TestCase):
    write_capability = "artifact.fixture.write"
    read_capability = "artifact.fixture.read"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "blobs"
        self.catalog_path = Path(self.temp.name) / "catalog.sqlite3"
        self.scope = AccessScope("artifact-scope", site_id="site-1", area_id="area-1")
        self.other_scope = AccessScope("other-scope", site_id="site-2")
        self.principal = Principal(
            "subject-1",
            (self.write_capability, self.read_capability),
            (self.scope,),
            1,
            2,
        )
        self.store = FileArtifactBlobStore(self.root, max_artifact_size=1024)
        self.catalog = SQLiteArtifactCatalog(self.catalog_path)
        self.addCleanup(self.catalog.close)
        self.service = ArtifactService(self.store, self.catalog)

    def write(self, content=b"canonical bytes", **kwargs):
        return self.service.write_and_register(
            self.principal,
            self.scope,
            content,
            media_type=kwargs.pop("media_type", "text/plain"),
            logical_purpose=kwargs.pop("logical_purpose", "fixture-evidence"),
            required_write_capability=self.write_capability,
            **kwargs,
        )

    def test_identity_is_exact_sha256_and_metadata_does_not_change_bytes_identity(self):
        identity = ArtifactContentIdentity.from_bytes(b"same")
        self.assertEqual(len(identity.sha256), 64)
        self.assertEqual(identity.byte_size, 4)
        self.assertEqual(identity, ArtifactContentIdentity.from_bytes(b"same"))
        first = self.write(media_type="text/plain", logical_purpose="a")
        second = self.write(media_type="text/plain", logical_purpose="a")
        self.assertEqual(first.metadata.content, second.metadata.content)
        self.assertFalse(first.blob_reused)
        self.assertTrue(second.blob_reused)
        self.assertFalse(second.catalog_created)
        with self.assertRaises(ArtifactMetadataConflictError):
            self.write(media_type="application/octet-stream", logical_purpose="a")

    def test_identity_and_metadata_bounds_fail_before_any_trusted_publication(self):
        with self.assertRaises(ValidationFailureError):
            ArtifactContentIdentity("A" * 64, 4)
        with self.assertRaises(ValidationFailureError):
            self.write(media_type="text/plain\x00bad")
        with self.assertRaises(ValidationFailureError):
            self.write(logical_purpose="x" * 129)
        with self.assertRaises(ValidationFailureError):
            self.service.verify_publish_preconditions(self.principal, (), self.read_capability)
        self.assertEqual(self.catalog.count(), 0)
        self.assertEqual(
            [
                path
                for path in self.root.rglob("*")
                if path.name != ".artifact-publish.lock"
            ],
            [],
        )

    def test_restart_persistence_and_current_authorization_are_required_for_known_hash(self):
        written = self.write(producing_job_id="job-1", revision_id="revision-1")
        reference = written.metadata.reference
        self.catalog.close()
        self.store = FileArtifactBlobStore(self.root, max_artifact_size=1024)
        self.catalog = SQLiteArtifactCatalog(self.catalog_path)
        self.addCleanup(self.catalog.close)
        self.service = ArtifactService(self.store, self.catalog)
        retrieved = self.service.retrieve(self.principal, reference, self.read_capability)
        self.assertEqual(retrieved.content, b"canonical bytes")
        self.assertEqual(retrieved.metadata, written.metadata)
        self.assertNotIn(str(self.root), str(retrieved.metadata.as_dict()))

        no_scope = Principal("subject-2", (self.read_capability,), (self.other_scope,), 2, 3)
        with self.assertRaises(ScopeDeniedError):
            self.service.retrieve(no_scope, reference, self.read_capability)
        no_capability = Principal("subject-1", (), (self.scope,), 2, 3)
        with self.assertRaises(AuthorizationDeniedError):
            self.service.retrieve(no_capability, reference, self.read_capability)

    def test_register_existing_verifies_missing_or_corrupt_blob_before_catalog_commit(self):
        content = b"register after storage"
        identity = self.store.put_bytes(content).content
        reference = ScopedArtifactReference(self.scope, identity)
        metadata = ArtifactMetadata(reference, "application/octet-stream", "fixture")
        object_path = self.root / identity.sha256[:2] / identity.sha256[2:]
        object_path.unlink()
        with self.assertRaises(ArtifactNotFoundError):
            self.service.register_existing(
                self.principal,
                metadata,
                required_write_capability=self.write_capability,
            )
        self.assertEqual(self.catalog.count(), 0)

        self.store.put_bytes(content)
        object_path.write_bytes(b"tampered")
        with self.assertRaises(ArtifactIntegrityError):
            self.service.register_existing(
                self.principal,
                metadata,
                required_write_capability=self.write_capability,
            )
        self.assertEqual(self.catalog.count(), 0)

    def test_post_registration_corruption_fails_verified_read_and_publish_precondition(self):
        written = self.write()
        reference = written.metadata.reference
        object_path = self.root / reference.sha256[:2] / reference.sha256[2:]
        object_path.write_bytes(b"post-registration tamper")
        with self.assertRaises(ArtifactIntegrityError):
            self.service.retrieve(self.principal, reference, self.read_capability)
        with self.assertRaises(ArtifactIntegrityError):
            self.service.verify_publish_preconditions(self.principal, (reference,), self.read_capability)

    def test_publish_precondition_checks_scope_and_returns_only_verified_references(self):
        reference = self.write().metadata.reference
        result = self.service.verify_publish_preconditions(
            self.principal,
            (reference,),
            self.read_capability,
        )
        self.assertEqual(result.references, (reference,))
        self.assertEqual(result.metadata[0].reference, reference)
        self.assertNotIn("object_key", result.metadata[0].as_dict())

        wrong_scope_reference = ScopedArtifactReference(self.other_scope, reference.content)
        with self.assertRaises(ScopeDeniedError):
            self.service.verify_publish_preconditions(self.principal, (wrong_scope_reference,), self.read_capability)

    def test_atomic_fault_before_publish_leaves_no_final_object_or_catalog_row(self):
        def interrupt(point):
            self.assertEqual(point, "before_publish")
            raise RuntimeError("deterministic test fault")

        faulted_store = FileArtifactBlobStore(self.root, max_artifact_size=1024, fault_injector=interrupt)
        faulted_service = ArtifactService(faulted_store, self.catalog)
        with self.assertRaises(ArtifactWriteInterruptedError):
            faulted_service.write_and_register(
                self.principal,
                self.scope,
                b"interrupted",
                media_type="text/plain",
                logical_purpose="fixture",
                required_write_capability=self.write_capability,
            )
        self.assertEqual(self.catalog.count(), 0)
        self.assertEqual(
            [path for path in self.root.rglob("*") if path.is_file() and not path.name.startswith(".artifact-")],
            [],
        )

    def test_concurrent_identical_writes_publish_one_valid_blob(self):
        first = FileArtifactBlobStore(self.root, max_artifact_size=1024)
        second = FileArtifactBlobStore(self.root, max_artifact_size=1024)
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def attempt(store):
            try:
                barrier.wait(timeout=5)
                results.append(store.put_bytes(b"concurrent immutable bytes"))
            except Exception as exc:  # pragma: no cover - assertion reports any race failure
                failures.append(exc)

        threads = [threading.Thread(target=attempt, args=(store,)) for store in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(failures, failures)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].content, results[1].content)
        self.assertEqual(sum(result.reused for result in results), 1)
        identity = results[0].content
        self.assertEqual(first.read(identity), b"concurrent immutable bytes")
        final_objects = [
            path
            for path in self.root.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        ]
        self.assertEqual(len(final_objects), 1)

    def test_reference_root_size_and_symlink_safety_are_explicit(self):
        with self.assertRaises(ArtifactStorageConfigurationError):
            FileArtifactBlobStore(":memory:")
        with self.assertRaises(ArtifactStorageConfigurationError):
            FileArtifactBlobStore(Path("relative-root"))
        bounded = FileArtifactBlobStore(Path(self.temp.name) / "bounded", max_artifact_size=3)
        with self.assertRaises(ArtifactTooLargeError):
            bounded.put_bytes(b"four")
        self.assertEqual(
            [
                path
                for path in (Path(self.temp.name) / "bounded").rglob("*")
                if path.name != ".artifact-publish.lock"
            ],
            [],
        )

        symlink_root = Path(self.temp.name) / "root-link"
        symlink_root.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ArtifactStorageConfigurationError):
            FileArtifactBlobStore(symlink_root)

        unsafe_prefix = Path(self.temp.name) / "unsafe-prefix"
        unsafe_prefix.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        identity = ArtifactContentIdentity.from_bytes(b"symlink")
        (unsafe_prefix / identity.sha256[:2]).symlink_to(outside, target_is_directory=True)
        unsafe_store = FileArtifactBlobStore(unsafe_prefix, max_artifact_size=1024)
        with self.assertRaises(ArtifactStorageSafetyError):
            unsafe_store.put_bytes(b"symlink")


if __name__ == "__main__":
    unittest.main()
