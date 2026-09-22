"""CHG-169 offline snapshot and protected-read regressions."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (
    AccessScope,
    CommandContext,
    DECISION_LOOP_CREATE_CAPABILITY,
    DECISION_SNAPSHOT_CREATE_CAPABILITY,
    DECISION_SNAPSHOT_READ_CAPABILITY,
    DecisionLoopCommandService,
    DecisionSnapshotHandoffService,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RevisionVector,
    AuthorizationDeniedError,
    ValidationFailureError,
    VersionConflictError,
)
from ephi.application.decision_loop import EPISODE_WORKFLOW_AGGREGATE_TYPE
from ephi.infrastructure import SQLiteReferenceTransactionAdapter


class O5HandoffSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ephi.db"
        self.store = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)
        self.scope = AccessScope("handoff-scope", site_id="site-1")
        capabilities = (
            DECISION_LOOP_CREATE_CAPABILITY,
            DECISION_SNAPSHOT_CREATE_CAPABILITY,
            DECISION_SNAPSHOT_READ_CAPABILITY,
        )
        self.principal = Principal("engineer-1", capabilities, (self.scope,), 1, 1)
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.store.seed_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-1", {"owner": None, "work_state": "OPEN"})
        self.workflow = DecisionLoopCommandService(self.store, self.authorization)
        initialized = self.workflow.initialize_decision_loop(self.context("initialize", 0), "episode-1")
        self.version = initialized.aggregate_version
        self.service = DecisionSnapshotHandoffService(self.store, self.authorization)

    def context(self, command_id: str, version: int, *, principal: Principal | None = None) -> CommandContext:
        principal = principal or self.principal
        return CommandContext(
            command_id,
            principal,
            self.scope,
            version,
            RevisionVector("analysis-1", "exposure-1", "priority-1", version, None, "qualification-1"),
        )

    def make_snapshot(self, command_id: str = "snapshot", version: int | None = None):
        return self.service.create_decision_snapshot(
            self.context(command_id, self.version if version is None else version),
            "episode-1",
            source_knowledge_cutoff="2026-09-22T12:00:00Z",
            source_capability_facts={"state": "READY", "freshness": "bounded"},
            what_changed="a check result requires review",
            why_it_matters="the engineer must decide the next authorized action",
            key_limitation="source family is not company-qualified",
            next_authorized_action="review the evidence and request an approved work action",
        )

    def test_snapshot_is_deterministic_bounded_and_immutable(self):
        first = self.make_snapshot()
        second = self.make_snapshot("same-content")
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.workflow_version, self.version)
        self.assertEqual(first.viewed_revisions["workflow_version"], self.version)
        self.assertNotIn("measurements", str(first.content))
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "UPDATE decision_snapshot SET content_json = ? WHERE snapshot_id = ?",
                ('{"tampered":true}', first.snapshot_id),
            )
        self.assertEqual(self.service.read_decision_snapshot(self.principal, self.scope, first.snapshot_id), first)

    def test_stale_view_or_workflow_rejects_without_partial_snapshot(self):
        before = self.store.connection.execute("SELECT COUNT(*) AS count FROM decision_snapshot").fetchone()["count"]
        with self.assertRaises(VersionConflictError):
            self.make_snapshot("stale", self.version + 1)
        after = self.store.connection.execute("SELECT COUNT(*) AS count FROM decision_snapshot").fetchone()["count"]
        self.assertEqual(before, after)
        with self.assertRaises(VersionConflictError):
            self.service.create_decision_snapshot(
                CommandContext(
                    "mismatched-view",
                    self.principal,
                    self.scope,
                    self.version,
                    RevisionVector("analysis-1", None, None, self.version + 1, None, "qualification-1"),
                ),
                "episode-1",
                what_changed="changed",
                why_it_matters="matters",
                key_limitation="limited",
                next_authorized_action="review",
            )
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) AS count FROM decision_snapshot").fetchone()["count"], before)

    def test_later_episode_state_does_not_rewrite_old_snapshot_and_reads_reauthorize(self):
        snapshot = self.make_snapshot()
        self.assertEqual(self.service.read_decision_snapshot(self.principal, self.scope, snapshot.snapshot_id), snapshot)
        revoked = Principal("engineer-1", (), (self.scope,), 2, 2)
        self.authorization.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.service.read_decision_snapshot(revoked, self.scope, snapshot.snapshot_id)
        self.assertEqual(self.store.handoff_store().get_decision_snapshot(self.scope, snapshot.snapshot_id), snapshot)

    def test_snapshot_rejects_unrestricted_artifact_urls(self):
        with self.assertRaises(ValidationFailureError):
            self.service.create_decision_snapshot(
                self.context("url-rejected", self.version),
                "episode-1",
                source_capability_facts={"artifact": "https://object.example/private"},
                what_changed="a bounded change",
                why_it_matters="review is required",
                key_limitation="qualification is bounded",
                next_authorized_action="review",
            )


if __name__ == "__main__":
    unittest.main()
