"""Focused CHG-134 O3 Attention → Episode application evidence."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    AttentionQueryService,
    AuthorizationDeniedError,
    CommandContext,
    CoherentReadConflictError,
    EpisodeBriefQueryService,
    EpisodeWorkflowCommandService,
    IdempotencyConflictError,
    MutableCurrentAuthorizationAuthority,
    Principal,
    QuerySnapshotExpiredError,
    ReadRevision,
    ReadRevisionIdentity,
    RevisionVector,
    ScopeDeniedError,
    VersionedReadRow,
    VersionConflictError,
)
from ephi.application.errors import ValidationFailureError  # noqa: E402
from ephi.application.read import (  # noqa: E402
    CurrentReadBundle,
    HistoricalReadBundle,
    PageResult,
    ReadRevisionDraft,
    ReadSnapshotStore,
    RetainedQuerySnapshot,
    RetainedSnapshotRow,
)
from ephi.infrastructure import AggregateSnapshot, SQLiteReferenceTransactionAdapter  # noqa: E402


class FakeAttentionRows:
    def __init__(self, rows):
        self.rows = tuple(rows)
        self.calls = []

    def fetch_attention_rows(self, principal, scope, filters, order):
        self.calls.append((principal, scope, dict(filters), tuple(order)))
        return self.rows


class FakeReadStore:
    """Small retained/coherent read double; no production code uses it."""

    def __init__(self):
        self.snapshots = {}
        self.expire = False
        self.current = None
        self.historical = {}
        self.counter = 0

    def publish_read_revision(self, *args, **kwargs):
        raise NotImplementedError

    def read_current_bundle(self, principal, scope, entity_type, entity_id, required_read_capability):
        self._authorize(principal, scope, required_read_capability)
        if self.current is None:
            raise KeyError(entity_id)
        return self.current

    def read_historical_bundle(self, principal, scope, revision_id, required_read_capability):
        self._authorize(principal, scope, required_read_capability)
        return self.historical[revision_id]

    def create_query_snapshot(self, principal, scope, query_identity, required_read_capability, rows, *, ttl_seconds=300):
        self._authorize(principal, scope, required_read_capability)
        self.counter += 1
        snapshot_id = f"snapshot-{self.counter}"
        now = datetime.now(timezone.utc)
        snapshot = RetainedQuerySnapshot(
            snapshot_id,
            "0" * 64,
            scope,
            principal.subject,
            principal.security_revision,
            required_read_capability,
            now,
            now + timedelta(seconds=ttl_seconds),
            len(rows),
        )
        self.snapshots[snapshot_id] = (snapshot, tuple(rows))
        return snapshot

    def read_query_snapshot_page(self, principal, scope, snapshot_id, query_identity, required_read_capability, *, page_size=50, cursor=None):
        self._authorize(principal, scope, required_read_capability)
        if self.expire:
            raise QuerySnapshotExpiredError(reason="test_expired")
        snapshot, rows = self.snapshots[snapshot_id]
        start = int(cursor or 0)
        selected = rows[start:start + page_size]
        next_cursor = str(start + len(selected)) if start + len(selected) < len(rows) else None
        return PageResult(
            snapshot,
            tuple(RetainedSnapshotRow(snapshot_id, start + index + 1, row.row_id, row.row_version, row.payload) for index, row in enumerate(selected)),
            next_cursor,
        )

    @staticmethod
    def _authorize(principal, scope, capability):
        if not principal.grants_scope(scope):
            raise ScopeDeniedError("scope denied")
        if not principal.has_capability(capability):
            raise AuthorizationDeniedError("capability denied")


class O3ApplicationTests(unittest.TestCase):
    read_capability = "ephi.attention.read"
    episode_capability = "ephi.episode.read"
    claim_capability = "ephi.episode.claim"
    acknowledge_capability = "ephi.episode.acknowledge"

    def setUp(self):
        self.scope = AccessScope("o3-scope", site_id="site-1")
        self.principal = Principal(
            "engineer-1",
            (self.read_capability, self.episode_capability, self.claim_capability, self.acknowledge_capability),
            (self.scope,),
            1,
            10,
        )
        self.current_authorization = MutableCurrentAuthorizationAuthority(self.principal)

    def test_attention_validation_and_retained_order_are_explicit(self):
        rows = FakeAttentionRows(
            (
                VersionedReadRow("ep-1", 1, {"episode_id": "ep-1", "priority": "P1"}),
                VersionedReadRow("ep-2", 1, {"episode_id": "ep-2", "priority": "P2"}),
            )
        )
        retained = FakeReadStore()
        service = AttentionQueryService(rows, retained, self.current_authorization)
        first = service.list_attention(self.principal, self.scope, page_size=1)
        self.assertEqual([row.episode_id for row in first.rows], ["ep-1"])
        second = service.list_attention(
            self.principal,
            self.scope,
            page_size=1,
            snapshot_id=first.snapshot_id,
            cursor=first.next_cursor,
        )
        self.assertEqual([row.episode_id for row in second.rows], ["ep-2"])
        self.assertEqual(rows.calls[0][3][-1], {"field": "episode_id", "direction": "asc"})
        retained.expire = True
        with self.assertRaises(QuerySnapshotExpiredError):
            service.list_attention(self.principal, self.scope, page_size=1, snapshot_id=first.snapshot_id, cursor=second.next_cursor)
        retained.expire = False
        renewed = service.list_attention(self.principal, self.scope, page_size=1)
        self.assertNotEqual(renewed.snapshot_id, first.snapshot_id)
        revoked = Principal("engineer-1", (), (self.scope,), 2, 11)
        with self.assertRaises(AuthorizationDeniedError):
            service.list_attention(revoked, self.scope, page_size=1)
        with self.assertRaises(ValidationFailureError):
            service.list_attention(self.principal, self.scope, filters={"unknown": "x"})
        with self.assertRaises(ValidationFailureError):
            service.list_attention(self.principal, self.scope, order=("unsupported_desc",))
        revoked = Principal("engineer-1", (), (self.scope,), 2, 11)
        with self.assertRaises(AuthorizationDeniedError):
            service.list_attention(revoked, self.scope, page_size=1, snapshot_id=first.snapshot_id, cursor=first.next_cursor)

    def test_episode_brief_requires_one_coherent_revision_and_source_state(self):
        retained = FakeReadStore()
        workflow_v1 = AggregateSnapshot(self.scope.canonical_key, "episode_workflow", "ep-1", 1, {"owner": None, "work_state": "OPEN"})
        revision = ReadRevision(
            ReadRevisionIdentity("read-1", self.scope, "episode", "ep-1"),
            RevisionVector("analysis-1", None, None, 1, None, "manifest-1"),
            {"episode_id": "ep-1", "analytical_revision": "analysis-1", "capability_state": {"source": "READY"}},
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
            workflow_v1,
        )
        current_workflow = AggregateSnapshot(self.scope.canonical_key, "episode_workflow", "ep-1", 2, {"owner": "engineer-1", "work_state": "CLAIMED"})
        retained.current = CurrentReadBundle(revision, current_workflow, RevisionVector("analysis-1", None, None, 2, None, "manifest-1"))
        brief = EpisodeBriefQueryService(retained, self.current_authorization).get_episode_brief(self.principal, self.scope, "ep-1")
        self.assertEqual(brief.revision_id, "read-1")
        self.assertEqual(brief.revision_vector.workflow_version, 2)
        self.assertEqual(brief.capability_state["source"], "READY")
        retained.historical["read-1"] = HistoricalReadBundle(revision, workflow_v1, revision.revision_vector)
        historical = EpisodeBriefQueryService(retained, self.current_authorization).get_episode_brief(self.principal, self.scope, "ep-1", revision_id="read-1")
        self.assertTrue(historical.historical)

        retained.current = CurrentReadBundle(
            ReadRevision(
                revision.identity,
                revision.revision_vector,
                {"episode_id": "ep-1", "analytical_revision": "different", "capability_state": {"source": "READY"}},
                revision.known_at,
                revision.published_at,
                workflow_v1,
            ),
            workflow_v1,
            revision.revision_vector,
        )
        with self.assertRaises(CoherentReadConflictError):
            EpisodeBriefQueryService(retained, self.current_authorization).get_episode_brief(self.principal, self.scope, "ep-1")

    def test_claim_acknowledge_cas_replay_conflict_revocation_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "o3.sqlite3"
            store = SQLiteReferenceTransactionAdapter(path)
            store.seed_aggregate(self.scope, "episode_workflow", "ep-1", {"owner": None, "work_state": "OPEN"})
            service = EpisodeWorkflowCommandService(store, self.current_authorization)
            vector = RevisionVector("analysis-1", None, None, 0, None, "manifest-1")
            context = CommandContext("claim-1", self.principal, self.scope, 0, vector, "claim")
            committed = service.claim_episode(context, "ep-1")
            self.assertEqual(service.claim_episode(context, "ep-1"), committed)
            with self.assertRaises(IdempotencyConflictError):
                service.claim_episode(CommandContext("claim-1", self.principal, self.scope, 0, vector, "different"), "ep-1")
            competing = Principal("engineer-2", (self.claim_capability,), (self.scope,), 2, 11)
            self.current_authorization.set_principal(competing)
            with self.assertRaises(VersionConflictError):
                service.claim_episode(
                    CommandContext("claim-2", competing, self.scope, 0, vector),
                    "ep-1",
                )
            ack_vector = RevisionVector("analysis-1", None, None, 1, None, "manifest-1")
            self.current_authorization.set_principal(self.principal)
            acknowledged = service.acknowledge_episode(
                CommandContext("ack-1", self.principal, self.scope, 1, ack_vector, "ack"),
                "ep-1",
            )
            self.assertEqual(acknowledged.aggregate_version, 2)
            store.close()
            reopened = SQLiteReferenceTransactionAdapter(path)
            self.assertEqual(reopened.get_aggregate(self.scope, "episode_workflow", "ep-1").state["work_state"], "ACKNOWLEDGED")
            with self.assertRaises(AuthorizationDeniedError):
                service_reopened = EpisodeWorkflowCommandService(reopened, self.current_authorization)
                service_reopened.acknowledge_episode(
                    CommandContext("ack-1", Principal("engineer-1", (), (self.scope,), 3, 12), self.scope, 1, ack_vector),
                    "ep-1",
                )
            reopened.close()

    def test_decision_sensitive_commands_require_viewed_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteReferenceTransactionAdapter(Path(directory) / "o3.sqlite3")
            store.seed_aggregate(self.scope, "episode_workflow", "ep-1", {"owner": None, "work_state": "OPEN"})
            with self.assertRaises(ValidationFailureError):
                EpisodeWorkflowCommandService(store, self.current_authorization).claim_episode(CommandContext("x", self.principal, self.scope, 0, None), "ep-1")
            store.close()


if __name__ == "__main__":
    unittest.main()
