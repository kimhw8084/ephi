"""CHG-169 PostgreSQL 18 durability and authorization evidence."""

from datetime import timedelta
import os
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    CommandContext,
    DECISION_LOOP_CREATE_CAPABILITY,
    DECISION_SNAPSHOT_CREATE_CAPABILITY,
    DECISION_SNAPSHOT_READ_CAPABILITY,
    DELIVERY_DISPATCH_CAPABILITY,
    DELIVERY_RECONCILE_CAPABILITY,
    DecisionLoopCommandService,
    DecisionSnapshotHandoffService,
    DeterministicInAppChannel,
    DeterministicRecipientDirectory,
    HANDOFF_CREATE_CAPABILITY,
    HANDOFF_READ_CAPABILITY,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RevisionVector,
    StaleLeaseError,
    UNKNOWN,
    FAILED,
    DeliveryResult,
)
from ephi.application.decision_loop import EPISODE_WORKFLOW_AGGREGATE_TYPE  # noqa: E402
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not configured; PostgreSQL integration is NOT_RUN")
class O5HandoffPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.assertTrue(self.adapter.server_version().startswith("18."), self.adapter.server_version())
        self.adapter.connection.execute(
            "TRUNCATE handoff_delivery_attempt, handoff_delivery_status, handoff_intent, decision_snapshot, applied_effect, job, outbox_event, audit_event, command_receipt, aggregate_state CASCADE"
        )
        self.scope = AccessScope("o5.2-pg", site_id="site-1", area_id="area-1")
        capabilities = (
            DECISION_LOOP_CREATE_CAPABILITY,
            DECISION_SNAPSHOT_CREATE_CAPABILITY,
            DECISION_SNAPSHOT_READ_CAPABILITY,
            HANDOFF_CREATE_CAPABILITY,
            HANDOFF_READ_CAPABILITY,
            DELIVERY_DISPATCH_CAPABILITY,
            DELIVERY_RECONCILE_CAPABILITY,
        )
        self.principal = Principal("engineer-5", capabilities, (self.scope,), 1, 1)
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.adapter.seed_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-pg", {"owner": None, "work_state": "OPEN"})
        workflow = DecisionLoopCommandService(self.adapter, self.authorization)
        self.initialized = workflow.initialize_decision_loop(self.context("initialize", 0), "episode-pg")
        self.worker = self.adapter.worker_store()
        self.recipients = DeterministicRecipientDirectory()
        self.recipients.register(self.scope, "owner", "engineer-5")
        self.service = DecisionSnapshotHandoffService(self.adapter, self.authorization, worker=self.worker, recipients=self.recipients)

    def context(self, command_id: str, version: int) -> CommandContext:
        return CommandContext(
            command_id,
            self.principal,
            self.scope,
            version,
            RevisionVector("analysis-pg", "exposure-pg", "priority-pg", version, None, "qualification-pg"),
        )

    def snapshot(self, command_id: str = "snapshot"):
        return self.service.create_decision_snapshot(
            self.context(command_id, self.initialized.aggregate_version),
            "episode-pg",
            what_changed="the committed check result requires action",
            why_it_matters="the owner must review the bounded decision context",
            key_limitation="company notification adapters are not implemented",
            next_authorized_action="review the Episode and request an approved work action",
        )

    def outbox_event_id(self) -> str:
        rows = [row for row in self.adapter.list_outbox_events() if row["command_id"] == "initialize"]
        self.assertEqual(len(rows), 1)
        return rows[0]["event_id"]

    def handoff(self, *, signature: str = "material-1", selector: str = "owner"):
        snap = self.snapshot(f"snapshot-{signature}-{selector}")
        return self.service.project_outbox_event(
            self.context(f"handoff-{signature}-{selector}", self.initialized.aggregate_version),
            event_id=self.outbox_event_id(),
            snapshot_id=snap.snapshot_id,
            event_kind="ASSIGNMENT_HANDOFF",
            material_change_signature=signature,
            recipient_selector=selector,
        )

    def test_snapshot_immutability_and_stale_no_partial_effects(self):
        snap = self.snapshot()
        with self.assertRaises(Exception):
            self.adapter.connection.execute("UPDATE decision_snapshot SET content_json = '{}' WHERE snapshot_id = %s", (snap.snapshot_id,))
        before = self.adapter.connection.execute("SELECT COUNT(*) AS count FROM decision_snapshot").fetchone()["count"]
        stale = CommandContext(
            "stale",
            self.principal,
            self.scope,
            self.initialized.aggregate_version,
            RevisionVector("analysis-pg", None, None, self.initialized.aggregate_version + 1, None, "qualification-pg"),
        )
        with self.assertRaises(Exception):
            self.service.create_decision_snapshot(
                stale,
                "episode-pg",
                what_changed="changed",
                why_it_matters="matters",
                key_limitation="limited",
                next_authorized_action="review",
            )
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) AS count FROM decision_snapshot").fetchone()["count"], before)

    def test_outbox_projection_dedup_race_and_material_change_separation(self):
        first = self.handoff()
        replay = self.handoff()
        self.assertEqual(first["intent_id"], replay["intent_id"])
        self.assertEqual(first["job_id"], replay["job_id"])
        different = self.handoff(signature="material-2")
        self.assertNotEqual(first["intent_id"], different["intent_id"])
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) AS count FROM handoff_intent").fetchone()["count"], 2)
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) AS count FROM job WHERE job_type = 'HANDOFF_DELIVERY'").fetchone()["count"], 2)

        first_adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        second_adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(first_adapter.close)
        self.addCleanup(second_adapter.close)
        first_service = DecisionSnapshotHandoffService(first_adapter, self.authorization, worker=first_adapter.worker_store(), recipients=self.recipients)
        second_service = DecisionSnapshotHandoffService(second_adapter, self.authorization, worker=second_adapter.worker_store(), recipients=self.recipients)
        snap = self.snapshot("race-snapshot")
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def attempt(service, command):
            try:
                barrier.wait(timeout=10)
                results.append(service.project_outbox_event(
                    self.context(command, self.initialized.aggregate_version),
                    event_id=self.outbox_event_id(), snapshot_id=snap.snapshot_id,
                    event_kind="ASSIGNMENT_HANDOFF", material_change_signature="race-signature", recipient_selector="owner",
                ))
            except Exception as exc:  # pragma: no cover - assertion reports driver failures
                failures.append(exc)

        threads = [threading.Thread(target=attempt, args=(first_service, "race-one")), threading.Thread(target=attempt, args=(second_service, "race-two"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(failures, failures)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["intent_id"], results[1]["intent_id"])
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) AS count FROM handoff_intent").fetchone()["count"], 3)

    def test_restart_persists_snapshot_intent_job_and_status(self):
        intent = self.handoff(signature="restart")
        snapshot = self.service.read_decision_snapshot(
            self.principal, self.scope, intent["decision_snapshot_id"]
        )
        self.assertEqual(snapshot.snapshot_id, intent["decision_snapshot_id"])

        self.adapter.close()
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.assertTrue(self.adapter.server_version().startswith("18."), self.adapter.server_version())
        self.worker = self.adapter.worker_store()
        restarted_service = DecisionSnapshotHandoffService(
            self.adapter,
            self.authorization,
            worker=self.worker,
            recipients=self.recipients,
        )
        persisted_snapshot = restarted_service.read_decision_snapshot(
            self.principal, self.scope, intent["decision_snapshot_id"]
        )
        persisted_status = restarted_service.read_handoff_status(
            self.principal, self.scope, intent["intent_id"]
        )
        persisted_job = self.worker.inspect(self.scope, job_id=intent["job_id"])[0]
        self.assertEqual(persisted_snapshot.content_hash, snapshot.content_hash)
        self.assertEqual(persisted_status["dedup_key"], intent["dedup_key"])
        self.assertEqual(persisted_status["delivery_state"], "PENDING")
        self.assertEqual(persisted_job.job_id, intent["job_id"])

    def test_delivery_revocation_unknown_reconciliation_status_and_workflow_separation(self):
        delivered = self.handoff(signature="delivered")
        workflow_before = self.adapter.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-pg")
        channel = DeterministicInAppChannel()
        self.service.dispatch_once(self.principal, self.scope, worker_id="delivery-a", channel_adapter=channel)
        status = self.service.read_handoff_status(self.principal, self.scope, delivered["intent_id"])
        self.assertEqual(status["delivery_state"], "DELIVERED")
        workflow_after = self.adapter.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-pg")
        self.assertEqual(workflow_before, workflow_after)
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) AS count FROM applied_effect").fetchone()["count"], 1)

        revoked = self.handoff(signature="revoked")
        self.recipients.revoke(self.scope, "owner")
        self.service.dispatch_once(self.principal, self.scope, worker_id="delivery-b", channel_adapter=channel)
        revoked_status = self.service.read_handoff_status(self.principal, self.scope, revoked["intent_id"])
        self.assertEqual(revoked_status["delivery_state"], FAILED)
        self.assertEqual(revoked_status["last_failure_code"], "RECIPIENT_REVOKED")
        self.assertNotIn(revoked["intent_id"], channel.sent)

    def test_unknown_is_not_blindly_retried_and_explicit_reconciliation_can_resolve(self):
        self.recipients.register(self.scope, "owner", "engineer-5")
        intent = self.handoff(signature="ambiguous")
        channel = DeterministicInAppChannel()
        channel.next_result = DeliveryResult(UNKNOWN, "external-ambiguous", "TIMEOUT", "transport timeout")
        self.service.dispatch_once(self.principal, self.scope, worker_id="delivery-c", channel_adapter=channel)
        status = self.service.read_handoff_status(self.principal, self.scope, intent["intent_id"])
        self.assertEqual(status["delivery_state"], UNKNOWN)
        self.assertIn("ambiguous", status["ambiguity_warning"])
        self.assertIsNone(self.service.dispatch_once(self.principal, self.scope, worker_id="delivery-d", channel_adapter=channel))
        channel.sent[status["idempotency_key"]] = {"recipient": "engineer-5", "payload": {}, "external_reference": "external-ambiguous"}
        resolved = self.service.reconcile_unknown(self.principal, self.scope, intent["intent_id"], channel)
        self.assertEqual(resolved["delivery_state"], "DELIVERED")

    def test_stale_worker_cannot_commit_delivery_status_and_retry_exhaustion_is_visible(self):
        crash = self.handoff(signature="crash")
        crashed = self.worker.claim(self.scope, "crashed-worker", job_type="HANDOFF_DELIVERY")
        self.adapter.connection.execute("UPDATE job SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE job_id = %s", (crashed.job_id,))
        crash_channel = DeterministicInAppChannel()
        self.service.dispatch_once(self.principal, self.scope, worker_id="takeover-worker", channel_adapter=crash_channel)
        crash_status = self.service.read_handoff_status(self.principal, self.scope, crash["intent_id"])
        self.assertEqual(crash_status["delivery_state"], "UNKNOWN")
        self.assertNotIn(crash_status["idempotency_key"], crash_channel.sent)

        intent = self.handoff(signature="fencing")
        first = self.worker.claim(self.scope, "old-worker", job_type="HANDOFF_DELIVERY")
        self.assertIsNotNone(first)
        self.adapter.connection.execute("UPDATE job SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE job_id = %s", (first.job_id,))
        takeover = self.worker.claim(self.scope, "new-worker", job_type="HANDOFF_DELIVERY")
        self.assertEqual(takeover.job_id, first.job_id)
        with self.assertRaises(StaleLeaseError):
            self.adapter.handoff_store().commit_delivery_effect(
                first.lease, "delivery:stale", {"intent_id": intent["intent_id"]}, intent_id=intent["intent_id"], state="DELIVERED",
            )

        retry = self.handoff(signature="retry")
        self.adapter.connection.execute("UPDATE job SET max_attempts = 2 WHERE job_id = %s", (retry["job_id"],))
        channel = DeterministicInAppChannel()
        channel.next_result = DeliveryResult(FAILED, error_code="TEMPORARY", error_message="retry", retryable=True)
        self.service.dispatch_once(self.principal, self.scope, worker_id="retry-a", channel_adapter=channel)
        self.adapter.connection.execute("UPDATE job SET available_at = clock_timestamp() - interval '1 second' WHERE semantic_key = %s", (f"handoff:{retry['dedup_key']}",))
        channel.next_result = DeliveryResult(FAILED, error_code="TEMPORARY", error_message="exhausted", retryable=True)
        self.service.dispatch_once(self.principal, self.scope, worker_id="retry-b", channel_adapter=channel)
        retry_status = self.service.read_handoff_status(self.principal, self.scope, retry["intent_id"])
        self.assertEqual(retry_status["delivery_state"], FAILED)
        self.assertEqual(self.adapter.connection.execute("SELECT status FROM job WHERE job_id = %s", (retry["job_id"],)).fetchone()["status"], "FAILED")

        revoked = Principal("engineer-5", (), (self.scope,), 2, 2)
        self.authorization.set_principal(revoked)
        with self.assertRaises(Exception):
            self.service.read_handoff_status(revoked, self.scope, intent["intent_id"])


if __name__ == "__main__":
    unittest.main()
