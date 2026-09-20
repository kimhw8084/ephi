"""CHG-134 PostgreSQL evidence for the durable O3 W1 vertical slice."""

import os
from pathlib import Path
import sys
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    AuthorizationDeniedError,
    CommandContext,
    EpisodeBriefQueryService,
    EpisodeWorkflowCommandService,
    MutableCurrentAuthorizationAuthority,
    Principal,
    QuerySnapshotExpiredError,
    RevisionVector,
    VersionConflictError,
    AttentionQueryService,
)
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLO3AttentionEpisodeTests(unittest.TestCase):
    read_capability = "ephi.episode.read"
    attention_capability = "ephi.attention.read"
    claim_capability = "ephi.episode.claim"
    acknowledge_capability = "ephi.episode.acknowledge"

    def setUp(self):
        self.store = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.store.close)
        self.store.connection.execute(
            "TRUNCATE o3_attention_projection, query_snapshot_row, query_snapshot, read_head, read_revision, outbox_event, audit_event, command_receipt, aggregate_state"
        )
        self.scope = AccessScope("o3-pg-scope", site_id="site-1", area_id="area-1")
        self.principal = Principal(
            "engineer-1",
            (self.read_capability, self.attention_capability, self.claim_capability, self.acknowledge_capability),
            (self.scope,),
            auth_session_revision=1,
            security_revision=1,
        )
        self.current_authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.store.seed_aggregate(
            self.scope,
            "episode_workflow",
            "episode-1",
            {"work_state": "OPEN", "owner": None},
            version=0,
        )
        self.store.seed_attention_projection(
            self.scope,
            "episode-1",
            {
                "title": "Durable Attention case",
                "asset_id": "asset-1",
                "priority": "P1",
                "severity": "HIGH",
                "technical_state": "READY",
                "source_state": "READY",
                "deadline": "2026-09-19T12:00:00Z",
                "age": "10",
            },
        )
        self.workflow = EpisodeWorkflowCommandService(self.store, self.current_authorization)
        self.attention = AttentionQueryService(self.store.o3_store(), self.store.read_store(), self.current_authorization)
        self.briefs = EpisodeBriefQueryService(self.store.read_store(), self.current_authorization)
        workflow = self.store.get_aggregate(self.scope, "episode_workflow", "episode-1")
        self.store.publish_current_revision(
            self.scope,
            "episode",
            "episode-1",
            "episode-read-1",
            RevisionVector("analysis-1", "exposure-1", "priority-1", 0, None, "manifest-1"),
            {
                "title": "Durable Attention case",
                "analytical_revision": "analysis-1",
                "capability_state": {"source": "READY"},
            },
            workflow,
        )

    def context(self, command_id: str, *, principal=None, expected=0, reason="W1 test"):
        return CommandContext(
            command_id,
            principal or self.principal,
            self.scope,
            expected,
            RevisionVector("analysis-1", "exposure-1", "priority-1", expected, None, "manifest-1"),
            reason,
        )

    def test_restart_persists_retained_attention_and_workflow_receipt(self):
        first = self.attention.list_attention(self.principal, self.scope, page_size=1)
        self.assertEqual([row.episode_id for row in first.rows], ["episode-1"])
        self.assertIsNone(first.next_cursor)

        brief = self.briefs.get_episode_brief(self.principal, self.scope, "episode-1")
        committed = self.workflow.claim_episode(self.context("claim-1"), "episode-1")
        self.assertEqual((brief.revision_id, brief.revision_vector.workflow_version), ("episode-read-1", 0))
        self.assertEqual(committed.aggregate_version, 1)
        self.store.close()

        self.store = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.store.close)
        self.workflow = EpisodeWorkflowCommandService(self.store, self.current_authorization)
        self.attention = AttentionQueryService(self.store.o3_store(), self.store.read_store(), self.current_authorization)
        self.briefs = EpisodeBriefQueryService(self.store.read_store(), self.current_authorization)
        replay = self.workflow.claim_episode(self.context("claim-1"), "episode-1")
        self.assertEqual(replay, committed)
        current = self.briefs.get_episode_brief(self.principal, self.scope, "episode-1")
        self.assertEqual((current.workflow["work_state"], current.revision_vector.workflow_version), ("CLAIMED", 1))
        self.assertEqual(self.store.count_rows()["command_receipt"], 1)
        self.assertEqual(self.store.count_rows()["audit_event"], 1)
        self.assertEqual(self.store.count_rows()["outbox_event"], 1)

    def test_two_sessions_expected_version_conflict_and_atomic_acknowledgement(self):
        first = PostgreSQLReferenceTransactionAdapter(DSN)
        second = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first_workflow = EpisodeWorkflowCommandService(first, self.current_authorization)
        second_workflow = EpisodeWorkflowCommandService(second, self.current_authorization)
        barrier = threading.Barrier(2)
        committed = []
        conflicts = []
        failures = []

        def attempt(service, command_id):
            try:
                barrier.wait(timeout=10)
                committed.append(service.claim_episode(self.context(command_id), "episode-1"))
            except VersionConflictError as exc:
                conflicts.append(exc)
            except Exception as exc:  # pragma: no cover - assertion reports any race failure
                failures.append(exc)

        threads = [
            threading.Thread(target=attempt, args=(first_workflow, "winner-1")),
            threading.Thread(target=attempt, args=(second_workflow, "winner-2")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(failures, failures)
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(conflicts), 1)
        winner = committed[0]
        acknowledged = first_workflow.acknowledge_episode(self.context("ack-1", expected=1), "episode-1")
        self.assertEqual((winner.aggregate_version, acknowledged.aggregate_version), (1, 2))
        self.assertEqual(first.get_aggregate(self.scope, "episode_workflow", "episode-1").state["work_state"], "ACKNOWLEDGED")
        self.assertEqual(first.count_rows()["command_receipt"], 2)
        self.assertEqual(first.count_rows()["audit_event"], 2)
        self.assertEqual(first.count_rows()["outbox_event"], 2)

    def test_current_authorization_blocks_retained_page_receipt_replay_and_write(self):
        page = self.attention.list_attention(self.principal, self.scope, page_size=1)
        revoked = Principal("engineer-1", (), (self.scope,), 2, 2)
        self.current_authorization.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.attention.list_attention(revoked, self.scope, snapshot_id=page.snapshot_id, cursor=page.next_cursor, page_size=1)

        self.current_authorization.set_principal(self.principal)
        committed = self.workflow.claim_episode(self.context("revoked-claim"), "episode-1")
        self.current_authorization.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.workflow.claim_episode(self.context("revoked-claim", principal=revoked), "episode-1")
        with self.assertRaises(AuthorizationDeniedError):
            self.workflow.claim_episode(self.context("revoked-claim", reason="different semantic payload"), "episode-1")
        self.assertEqual(committed.aggregate_version, 1)

    def test_security_revision_change_expires_retained_snapshot(self):
        page = self.attention.list_attention(self.principal, self.scope, page_size=1)
        rotated = Principal(
            self.principal.subject,
            self.principal.capabilities,
            self.principal.scope_grants,
            auth_session_revision=1,
            security_revision=2,
        )
        self.current_authorization.set_principal(rotated)
        with self.assertRaises(QuerySnapshotExpiredError):
            self.attention.list_attention(
                rotated,
                self.scope,
                snapshot_id=page.snapshot_id,
                cursor=page.next_cursor,
                page_size=1,
            )
