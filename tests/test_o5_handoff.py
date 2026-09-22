"""CHG-169 offline snapshot and protected-read regressions."""

from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (
    ACTION_RECORD_CAPABILITY,
    ASSIGNMENT_HANDOFF,
    AccessScope,
    CommandContext,
    DECISION_LOOP_CREATE_CAPABILITY,
    DECISION_SNAPSHOT_CREATE_CAPABILITY,
    DECISION_SNAPSHOT_READ_CAPABILITY,
    DecisionLoopCommandService,
    DecisionSnapshotHandoffService,
    DeterministicRecipientDirectory,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RevisionVector,
    HANDOFF_CREATE_CAPABILITY,
    HANDOFF_READ_CAPABILITY,
    CLAIM_EPISODE_CAPABILITY,
    EpisodeWorkflowCommandService,
    JobRecord,
    AuthorizationDeniedError,
    canonical_json,
    ValidationFailureError,
    VersionConflictError,
)
from ephi.application.decision_loop import EPISODE_WORKFLOW_AGGREGATE_TYPE
from ephi.infrastructure import SQLiteReferenceTransactionAdapter


class _ProjectionWorker:
    """Small offline queue double; PostgreSQL owns queue/lease evidence."""

    def __init__(self):
        self.jobs = {}

    def enqueue(self, scope, job_type, semantic_key, payload, **_kwargs):
        existing = self.jobs.get((scope.canonical_key, job_type, semantic_key))
        if existing is not None:
            return existing
        now = datetime.now(timezone.utc)
        job = JobRecord(
            f"job-{len(self.jobs) + 1}", scope.canonical_key, job_type, semantic_key, "payload-hash", dict(payload),
            "QUEUED", 0, now, 0, 3, None, 0, None, None, None, None, now, now,
        )
        self.jobs[(scope.canonical_key, job_type, semantic_key)] = job
        return job

    def claim(self, *_args, **_kwargs):
        return None

    def heartbeat(self, *_args, **_kwargs):
        raise NotImplementedError

    def complete(self, *_args, **_kwargs):
        raise NotImplementedError

    def fail(self, *_args, **_kwargs):
        raise NotImplementedError

    def defer(self, *_args, **_kwargs):
        raise NotImplementedError

    def cancel(self, *_args, **_kwargs):
        raise NotImplementedError

    def inspect(self, *_args, **_kwargs):
        return ()

    def commit_local_effect(self, *_args, **_kwargs):
        raise NotImplementedError


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


class O5HandoffProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ephi.db"
        self.store = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)
        self.scope = AccessScope("handoff-projection", site_id="site-1")
        capabilities = (
            ACTION_RECORD_CAPABILITY,
            DECISION_LOOP_CREATE_CAPABILITY,
            DECISION_SNAPSHOT_CREATE_CAPABILITY,
            DECISION_SNAPSHOT_READ_CAPABILITY,
            HANDOFF_CREATE_CAPABILITY,
            HANDOFF_READ_CAPABILITY,
        )
        self.principal = Principal("engineer-1", capabilities, (self.scope,), 1, 1)
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.store.seed_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-1", {"owner": None, "work_state": "OPEN"})
        self.workflow = DecisionLoopCommandService(self.store, self.authorization)
        self.initialized = self.workflow.initialize_decision_loop(self.context("initialize", 0), "episode-1")
        self.version = self.initialized.aggregate_version
        self.worker = _ProjectionWorker()
        self.recipients = DeterministicRecipientDirectory()
        self.recipients.register(self.scope, "owner", "engineer-1")
        self.service = DecisionSnapshotHandoffService(
            self.store,
            self.authorization,
            worker=self.worker,
            recipients=self.recipients,
        )

    def context(self, command_id: str, version: int, *, principal: Principal | None = None) -> CommandContext:
        principal = principal or self.principal
        return CommandContext(
            command_id,
            principal,
            self.scope,
            version,
            RevisionVector("analysis-1", "exposure-1", "priority-1", version, None, "qualification-1"),
        )

    def _source(self, suffix: str = "one"):
        result = self.workflow.record_external_action(
            self.context(f"action-{suffix}", self.version),
            "episode-1",
            f"action-{suffix}",
            action_type="APPROVED_WORK_REQUEST",
            external_system="approved-work-system",
            work_request_id=f"work-request-{suffix}",
        )
        self.version = result.aggregate_version
        event = next(row for row in self.store.list_outbox_events() if row["command_id"] == f"action-{suffix}")
        snapshot = self.service.create_decision_snapshot(
            self.context(f"snapshot-{suffix}", result.aggregate_version),
            "episode-1",
            what_changed="an approved work request was recorded",
            why_it_matters="the owner must review the bounded decision context",
            key_limitation="company notification adapters are not implemented",
            next_authorized_action="review the Episode and request approved work",
        )
        return event, snapshot, result.aggregate_version

    def test_initialize_and_unsupported_event_cannot_create_handoff(self):
        snapshot = self.service.create_decision_snapshot(
            self.context("snapshot-init", self.initialized.aggregate_version),
            "episode-1",
            what_changed="the decision loop was initialized",
            why_it_matters="the workflow is now ready for checks",
            key_limitation="initialization is not an assignment",
            next_authorized_action="review the Episode",
        )
        event = next(row for row in self.store.list_outbox_events() if row["command_id"] == "initialize")
        with self.assertRaises(ValidationFailureError):
            self.service.project_outbox_event(
                self.context("project-init", self.initialized.aggregate_version),
                event_id=event["event_id"],
                snapshot_id=snapshot.snapshot_id,
                event_kind="ASSIGNMENT_HANDOFF",
                recipient_selector="owner",
            )
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) AS count FROM handoff_intent").fetchone()["count"], 0)

    def test_derived_classification_rejects_relabel_and_signature_without_writes(self):
        event, snapshot, version = self._source()
        before = self.store.connection.execute("SELECT COUNT(*) AS count FROM handoff_intent").fetchone()["count"]
        with self.assertRaises(ValidationFailureError):
            self.service.project_outbox_event(
                self.context("wrong-kind", version),
                event_id=event["event_id"],
                snapshot_id=snapshot.snapshot_id,
                event_kind="ASSIGNMENT_HANDOFF",
                recipient_selector="owner",
            )
        with self.assertRaises(ValidationFailureError):
            self.service.project_outbox_event(
                self.context("wrong-signature", version),
                event_id=event["event_id"],
                snapshot_id=snapshot.snapshot_id,
                material_change_signature="caller-invented",
                recipient_selector="owner",
            )
        after = self.store.connection.execute("SELECT COUNT(*) AS count FROM handoff_intent").fetchone()["count"]
        self.assertEqual(before, after)

    def test_exact_replay_is_one_job_and_distinct_authoritative_events_are_distinct(self):
        event, snapshot, version = self._source("one")
        first = self.service.project_outbox_event(
            self.context("project-one", version), event_id=event["event_id"], snapshot_id=snapshot.snapshot_id, recipient_selector="owner"
        )
        replay = self.service.project_outbox_event(
            self.context("project-one-replay", version), event_id=event["event_id"], snapshot_id=snapshot.snapshot_id, recipient_selector="owner"
        )
        self.assertEqual(first["intent_id"], replay["intent_id"])
        self.assertEqual(first["job_id"], replay["job_id"])

        event_two, snapshot_two, version_two = self._source("two")
        second = self.service.project_outbox_event(
            self.context("project-two", version_two), event_id=event_two["event_id"], snapshot_id=snapshot_two.snapshot_id, recipient_selector="owner"
        )
        self.assertNotEqual(first["intent_id"], second["intent_id"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) AS count FROM handoff_intent").fetchone()["count"], 2)

    def test_claim_event_is_the_only_assignment_handoff_source(self):
        claim_principal = Principal(
            "engineer-1",
            frozenset((*self.principal.capabilities, CLAIM_EPISODE_CAPABILITY)),
            (self.scope,),
            1,
            1,
        )
        claim_authorization = MutableCurrentAuthorizationAuthority(claim_principal)
        claim_workflow = EpisodeWorkflowCommandService(self.store, claim_authorization)
        claimed = claim_workflow.claim_episode(
            self.context("claim", self.initialized.aggregate_version, principal=claim_principal),
            "episode-1",
        )
        event = next(row for row in self.store.list_outbox_events() if row["command_id"] == "claim")
        snapshot = self.service.create_decision_snapshot(
            self.context("snapshot-claim", claimed.aggregate_version),
            "episode-1",
            what_changed="the Episode was assigned to the current engineer",
            why_it_matters="the assigned engineer owns the next review",
            key_limitation="assignment is still human workflow state",
            next_authorized_action="review the Episode",
        )
        projected = self.service.project_outbox_event(
            self.context("project-claim", claimed.aggregate_version),
            event_id=event["event_id"], snapshot_id=snapshot.snapshot_id, recipient_selector="owner",
        )
        self.assertEqual(projected["event_kind"], ASSIGNMENT_HANDOFF)

    def test_outbox_payload_semantic_mismatch_fails_closed(self):
        event, snapshot, version = self._source("payload-mismatch")
        payload = dict(event["payload"])
        payload["command_type"] = "ClaimEpisode"
        self.store.connection.execute(
            "UPDATE outbox_event SET payload_json = ? WHERE event_id = ?",
            (canonical_json(payload), event["event_id"]),
        )
        with self.assertRaises(ValidationFailureError):
            self.service.project_outbox_event(
                self.context("payload-mismatch-project", version),
                event_id=event["event_id"], snapshot_id=snapshot.snapshot_id, recipient_selector="owner",
            )
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) AS count FROM handoff_intent").fetchone()["count"], 0)


if __name__ == "__main__":
    unittest.main()
