"""Focused CHG-144/O4.1 source-ingress contract evidence.

The repository below is an explicit test authority only.  Production source
publication is covered by the real PostgreSQL suite, never by this fake.
"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    ArtifactContentIdentity,
    AuthorizationDeniedError,
    MetrologyObservation,
    MetrologySourceBinding,
    Principal,
    ScopeDeniedError,
    ScopedArtifactReference,
    SourceCapabilityRecord,
    SourceCapabilityState,
    SourceBindingUnavailableError,
    SourceQuarantineError,
    SourceSnapshotConflictError,
    SourceSnapshotDraft,
    SourceSnapshotIngressService,
    SourceSnapshotRecord,
    SourceSnapshotStatus,
    as_known_eligible,
    preflight_source_reality,
    require_runtime_source_binding,
    source_replay_eligible,
)


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


class _TestArtifactService:
    def __init__(self, *references):
        self.references = set(references)
        self.calls = []

    def verify_publish_preconditions(self, principal, references, required_read_capability):
        self.calls.append((principal.subject, tuple(references), required_read_capability))
        if any(reference not in self.references for reference in references):
            from ephi.application import ArtifactNotFoundError

            raise ArtifactNotFoundError("test artifact is not registered")


class _TestRepository:
    def __init__(self):
        self.record = None

    def publish_snapshot(self, draft, ingested_at, capability):
        if self.record is not None:
            if self.record.immutable_identity != draft.immutable_identity():
                raise SourceSnapshotConflictError(draft.snapshot_id)
            return self.record, capability
        published = ingested_at + timedelta(microseconds=1)
        self.record = SourceSnapshotRecord(
            draft.snapshot_id,
            draft.binding,
            draft.source_partition,
            draft.source_revision,
            draft.event_start,
            draft.event_end,
            draft.available_cutoff,
            draft.artifact_reference,
            draft.row_count,
            draft.status,
            draft.manifest_hash,
            ingested_at,
            published,
            published,
            freshness_age_seconds=draft.freshness_age_seconds,
        )
        return self.record, capability

    def get_snapshot(self, principal, scope, snapshot_id):
        return self.record

    def get_capability(self, principal, binding):
        return SourceCapabilityRecord(
            binding,
            SourceCapabilityState.UNAVAILABLE,
            None,
            None,
            None,
            NOW,
            3600,
            "TEST_ONLY",
        )


class SourceIngressContractTests(unittest.TestCase):
    def setUp(self):
        self.scope = AccessScope("scope-1", site_id="site-1", area_id="area-1", family_id="family-1")
        self.principal = Principal(
            "engineer-1",
            ("ephi.source.read", "ephi.source.ingest", "ephi.source.artifact.read"),
            (self.scope,),
            1,
            1,
        )
        self.binding = MetrologySourceBinding(
            self.scope,
            "source-1",
            "provider-1",
            "family-1",
            "capability-1",
            "test.adapter",
            "schema-1",
            "mapping-v1",
            "a" * 64,
            "mm",
        )
        self.observation = MetrologyObservation(
            "row-1",
            "asset-1",
            "tool-1",
            "head-1",
            "context-1",
            "characteristic-1",
            "mm",
            1.25,
            NOW - timedelta(minutes=5),
            NOW - timedelta(minutes=4),
        )
        self.artifact = ScopedArtifactReference(self.scope, ArtifactContentIdentity("b" * 64, 10))

    def draft(self, *, status=SourceSnapshotStatus.PUBLISHED, artifact=None, available=None, partition="partition-1"):
        return SourceSnapshotDraft(
            self.binding,
            partition,
            "revision-1",
            NOW - timedelta(minutes=10),
            NOW - timedelta(minutes=5),
            available or NOW - timedelta(minutes=4),
            artifact or self.artifact,
            (self.observation,),
            status,
        )

    def test_canonical_mapping_identity_and_explicit_unit_validation(self):
        self.assertEqual(self.binding.scope_key, self.scope.canonical_key)
        self.assertEqual(self.binding.as_dict()["mapping_hash"], "a" * 64)
        with self.assertRaises(SourceQuarantineError):
            MetrologySourceBinding(
                self.scope, " source", "provider", "family", "capability", "adapter", "schema", "v1", "a" * 64, "mm"
            )
        with self.assertRaises(SourceQuarantineError):
            MetrologyObservation(
                "row-2", "asset", None, None, "context", "characteristic", "inch", 1.0,
                NOW, NOW,
            )
        with self.assertRaises(SourceQuarantineError):
            MetrologySourceBinding(
                self.scope, "source", "provider", "family", "capability", "adapter", "schema", "v1", "a" * 64, "inch"
            )

    def test_event_source_available_and_ingested_times_remain_distinct(self):
        repository = _TestRepository()
        artifacts = _TestArtifactService(self.artifact)
        service = SourceSnapshotIngressService(repository, artifacts, clock=lambda: NOW)
        record, capability = service.publish(self.principal, self.draft(), freshness_age_seconds=3600)
        self.assertLess(record.event_start, record.available_cutoff)
        self.assertLess(record.available_cutoff, record.ingested_at)
        self.assertLess(record.ingested_at, record.published_at)
        self.assertEqual(capability.state, SourceCapabilityState.READY)
        self.assertEqual(artifacts.calls[0][2], "ephi.source.artifact.read")

    def test_future_unavailable_evidence_and_ambiguous_timezone_quarantine(self):
        repository = _TestRepository()
        service = SourceSnapshotIngressService(repository, _TestArtifactService(self.artifact), clock=lambda: NOW)
        with self.assertRaises(SourceQuarantineError):
            service.publish(self.principal, self.draft(available=NOW + timedelta(seconds=1)), freshness_age_seconds=3600)
        with self.assertRaises(SourceQuarantineError):
            MetrologyObservation(
                "row-dst", "asset", None, None, "context", "characteristic", "mm", 1.0,
                datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/Chicago")),
                datetime(2026, 11, 1, 1, 45, tzinfo=ZoneInfo("America/Chicago")),
            )

    def test_as_known_excludes_late_ingestion_but_source_replay_uses_provenance(self):
        repository = _TestRepository()
        service = SourceSnapshotIngressService(repository, _TestArtifactService(self.artifact), clock=lambda: NOW)
        record, _ = service.publish(self.principal, self.draft(), freshness_age_seconds=3600)
        as_known_cutoff = NOW - timedelta(minutes=4)
        self.assertFalse(as_known_eligible(record, as_known_cutoff))
        self.assertTrue(source_replay_eligible(record, as_known_cutoff))
        self.assertFalse(source_replay_eligible(record, NOW - timedelta(minutes=20)))

    def test_same_logical_partition_is_idempotent_and_scope_is_fail_closed(self):
        repository = _TestRepository()
        artifacts = _TestArtifactService(self.artifact)
        service = SourceSnapshotIngressService(repository, artifacts, clock=lambda: NOW)
        first, _ = service.publish(self.principal, self.draft(), freshness_age_seconds=3600)
        second, _ = service.publish(self.principal, self.draft(), freshness_age_seconds=3600)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.manifest_hash, second.manifest_hash)
        changed = self.draft(artifact=ScopedArtifactReference(self.scope, ArtifactContentIdentity("c" * 64, 10)))
        artifacts.references.add(changed.artifact_reference)
        with self.assertRaises(SourceSnapshotConflictError):
            service.publish(self.principal, changed, freshness_age_seconds=3600)
        with self.assertRaises(ScopeDeniedError):
            service.publish(
                Principal(self.principal.subject, self.principal.capabilities, (AccessScope("other"),), 1, 1),
                self.draft(partition="other"),
                freshness_age_seconds=3600,
            )
        with self.assertRaises(AuthorizationDeniedError):
            service.publish(
                Principal(self.principal.subject, ("ephi.source.read",), (self.scope,), 1, 1),
                self.draft(partition="denied"),
                freshness_age_seconds=3600,
            )

    def test_preflight_missing_real_binding_is_secret_safe_and_not_run(self):
        result = preflight_source_reality(
            {
                "EPHI_POSTGRES_DSN": "postgresql://secret-user:secret-password@db.internal:5432/ephi",
            }
        )
        self.assertEqual(result["status"], "BLOCKED_REAL_SOURCE")
        self.assertEqual(result["snapshot_qualification"], "NOT_RUN")
        self.assertEqual(result["capability"]["state"], "UNAVAILABLE")
        encoded = str(result)
        self.assertNotIn("secret-password", encoded)
        self.assertNotIn("db.internal", encoded)
        self.assertFalse(result["secret_safety"]["credentials_printed"])

    def test_runtime_binding_resolver_fails_closed_without_an_approved_adapter(self):
        with self.assertRaises(SourceBindingUnavailableError):
            require_runtime_source_binding({})


if __name__ == "__main__":
    unittest.main()
