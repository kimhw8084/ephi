"""CHG-150/O8.1 real PostgreSQL current-authorization evidence."""

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
    AttentionQueryService,
    AuthorizationDeniedError,
    CommandContext,
    EpisodeBriefQueryService,
    EpisodeWorkflowCommandService,
    MutableCurrentAuthorizationAuthority,
    Principal,
    QuerySnapshotExpiredError,
    RevisionVector,
)
from ephi.infrastructure import (  # noqa: E402
    FileArtifactBlobStore,
    PostgreSQLArtifactCatalog,
    PostgreSQLReferenceTransactionAdapter,
)


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLO8CurrentAuthorizationTests(unittest.TestCase):
    attention_capability = "ephi.attention.read"
    episode_capability = "ephi.episode.read"
    claim_capability = "ephi.episode.claim"
    acknowledge_capability = "ephi.episode.acknowledge"
    artifact_read_capability = "ephi.artifact.read"
    artifact_write_capability = "ephi.artifact.write"

    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.adapter.apply_migrations()
        self.adapter.connection.execute(
            "TRUNCATE o3_attention_projection, query_snapshot_row, query_snapshot, read_head, read_revision, "
            "artifact_catalog, outbox_event, audit_event, command_receipt, aggregate_state"
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.blob_store = FileArtifactBlobStore(Path(self.temp.name) / "artifacts", max_artifact_size=4096)
        self.scope = AccessScope("o8-pg-scope", site_id="site-1")
        self.other_scope = AccessScope("o8-pg-other-scope", site_id="site-2")
        self.capabilities = (
            self.attention_capability,
            self.episode_capability,
            self.claim_capability,
            self.acknowledge_capability,
            self.artifact_read_capability,
            self.artifact_write_capability,
        )
        self.principal = Principal("o8-subject-1", self.capabilities, (self.scope,), 1, 1)
        self.authority = MutableCurrentAuthorizationAuthority(self.principal)
        self.attention = AttentionQueryService(
            self.adapter.o3_store(),
            self.adapter.read_store(),
            self.authority,
        )
        self.briefs = EpisodeBriefQueryService(self.adapter.read_store(), self.authority)
        self.workflow = EpisodeWorkflowCommandService(self.adapter, self.authority)
        self.artifacts = ArtifactService(
            self.blob_store,
            PostgreSQLArtifactCatalog(self.adapter),
            self.authority,
        )
        for episode_id, title in (("episode-1", "O8 case one"), ("episode-2", "O8 case two")):
            workflow = self.adapter.seed_aggregate(
                self.scope,
                "episode_workflow",
                episode_id,
                {"work_state": "OPEN", "owner": None},
                version=0,
            )
            self.adapter.seed_attention_projection(
                self.scope,
                episode_id,
                {"title": title, "priority": "P1", "source_state": "READY"},
            )
            self.adapter.publish_current_revision(
                self.scope,
                "episode",
                episode_id,
                f"{episode_id}-revision-1",
                RevisionVector("analysis-1", None, None, 0, None, "manifest-1"),
                {"title": title, "capability_state": {"source": "READY"}},
                workflow,
            )

    def context(self, command_id, *, principal=None, expected=0):
        return CommandContext(
            command_id,
            principal or self.principal,
            self.scope,
            expected,
            RevisionVector("analysis-1", None, None, expected, None, "manifest-1"),
            "O8 integration evidence",
        )

    def rotated(self, *, subject="o8-subject-1", scope=None, capabilities=None, auth=2, security=2):
        return Principal(
            subject,
            self.capabilities if capabilities is None else capabilities,
            (self.scope if scope is None else scope,),
            auth,
            security,
        )

    def test_receipt_replay_revocation_and_cross_scope_fail_before_disclosure(self):
        claimed = self.workflow.claim_episode(self.context("claim-1"), "episode-1")
        acknowledged = self.workflow.acknowledge_episode(self.context("ack-1", expected=1), "episode-1")
        self.assertEqual((claimed.aggregate_version, acknowledged.aggregate_version), (1, 2))

        fresh_grants = self.rotated()
        self.authority.set_principal(fresh_grants)
        with self.assertRaises(AuthorizationDeniedError):
            self.workflow.acknowledge_episode(self.context("ack-1", expected=1), "episode-1")

        revoked = self.rotated(capabilities=(), auth=3, security=3)
        self.authority.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.workflow.acknowledge_episode(
                self.context("ack-1", principal=revoked, expected=1),
                "episode-1",
            )

        other = self.rotated(scope=self.other_scope, auth=4, security=4)
        self.authority.set_principal(other)
        with self.assertRaises(AuthorizationDeniedError):
            self.workflow.claim_episode(
                CommandContext("cross-scope", other, self.scope, 0, RevisionVector("analysis-1", None, None, 0, None, "manifest-1")),
                "episode-1",
            )

        self.authority.set_principal(fresh_grants)
        replay = self.workflow.acknowledge_episode(self.context("ack-1", principal=fresh_grants, expected=1), "episode-1")
        self.assertEqual(replay.result_identity, acknowledged.result_identity)

    def test_episode_current_historical_and_cross_scope_reads_use_current_authority(self):
        brief = self.briefs.get_episode_brief(self.principal, self.scope, "episode-1")
        historical = self.briefs.get_episode_brief(
            self.principal,
            self.scope,
            "episode-1",
            revision_id=brief.revision_id,
        )
        self.assertEqual((brief.revision_id, historical.revision_id), ("episode-1-revision-1", "episode-1-revision-1"))

        fresh = self.rotated()
        self.authority.set_principal(fresh)
        with self.assertRaises(AuthorizationDeniedError):
            self.briefs.get_episode_brief(self.principal, self.scope, "episode-1")
        with self.assertRaises(AuthorizationDeniedError):
            self.briefs.get_episode_brief(self.principal, self.scope, "episode-1", revision_id=brief.revision_id)

        other = self.rotated(scope=self.other_scope, auth=3, security=3)
        self.authority.set_principal(other)
        with self.assertRaises(AuthorizationDeniedError):
            self.briefs.get_episode_brief(other, self.scope, "episode-1")
        with self.assertRaises(AuthorizationDeniedError):
            self.briefs.get_episode_brief(other, self.scope, "episode-1", revision_id=brief.revision_id)

    def test_attention_snapshot_revision_subject_and_fresh_query_behavior(self):
        first = self.attention.list_attention(self.principal, self.scope, page_size=1)
        self.assertEqual(first.total_count, 2)
        self.assertIsNotNone(first.next_cursor)

        fresh = self.rotated()
        self.authority.set_principal(fresh)
        with self.assertRaises(QuerySnapshotExpiredError):
            self.attention.list_attention(
                fresh,
                self.scope,
                page_size=1,
                snapshot_id=first.snapshot_id,
                cursor=first.next_cursor,
            )
        renewed = self.attention.list_attention(fresh, self.scope, page_size=1)
        self.assertNotEqual(first.snapshot_id, renewed.snapshot_id)

        other_subject = self.rotated(subject="o8-subject-2", auth=2, security=2)
        self.authority.set_principal(other_subject)
        with self.assertRaises(AuthorizationDeniedError):
            self.attention.list_attention(
                other_subject,
                self.scope,
                page_size=1,
                snapshot_id=renewed.snapshot_id,
                cursor=renewed.next_cursor,
            )

    def test_artifact_catalog_and_blob_retrieval_fail_closed_for_scope_and_revocation(self):
        written = self.artifacts.write_and_register(
            self.principal,
            self.scope,
            b"O8 immutable artifact fixture",
            media_type="application/octet-stream",
            logical_purpose="o8-integration",
            required_write_capability=self.artifact_write_capability,
        )
        reference = written.metadata.reference
        retrieved = self.artifacts.retrieve(self.principal, reference, self.artifact_read_capability)
        self.assertEqual(retrieved.metadata.reference, reference)

        revoked = self.rotated(capabilities=(), auth=2, security=2)
        self.authority.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.artifacts.get_metadata(self.principal, reference, self.artifact_read_capability)
        with self.assertRaises(AuthorizationDeniedError):
            self.artifacts.retrieve(self.principal, reference, self.artifact_read_capability)
        with self.assertRaises(AuthorizationDeniedError):
            self.artifacts.verify_publish_preconditions(self.principal, (reference,), self.artifact_read_capability)

        other = self.rotated(scope=self.other_scope, auth=3, security=3)
        self.authority.set_principal(other)
        with self.assertRaises(AuthorizationDeniedError):
            self.artifacts.retrieve(other, reference, self.artifact_read_capability)


if __name__ == "__main__":
    unittest.main()
