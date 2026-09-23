"""U1 composed provider durability against real PostgreSQL 18.x."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()

from ephi.application import (  # noqa: E402
    AccessScope,
    CommandContext,
    DeliveryResult,
    RevisionVector,
    SourceSnapshotDraft,
)
from ephi.config import RuntimeSettings  # noqa: E402
from ephi.downstream import ProviderBinding, compose_downstream  # noqa: E402
from examples.synthetic_downstream.provider import build_bundle  # noqa: E402


class _AmbiguousChannel:
    def __init__(self):
        self.calls = []

    def send(self, resolution, payload, idempotency_key):
        self.calls.append((resolution, dict(payload), idempotency_key))
        return DeliveryResult("UNKNOWN", error_code="SYNTHETIC_AMBIGUOUS", retryable=False)

    def reconcile(self, resolution, idempotency_key, external_reference):
        return DeliveryResult("DELIVERED", external_reference=external_reference or f"synthetic:{idempotency_key}")


class _NotificationProvider:
    def __init__(self, recipients, channel):
        self.recipients = recipients
        self.channel = channel


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not configured; PostgreSQL integration is NOT_RUN")
class DownstreamPostgreSQLCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(
            os.environ,
            {"EPHI_SYNTHETIC_ARTIFACT_ROOT": str(Path(self.temp.name) / "blobs")},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.bundle = build_bundle()
        runtime_class = self.bundle.runtime.public_metadata.target_environment_class
        self.settings = RuntimeSettings(environment=runtime_class)
        self.channel = _AmbiguousChannel()
        original = self.bundle.notifications
        self.bundle = replace(
            self.bundle,
            notifications=ProviderBinding(
                original.contract,
                _NotificationProvider(original.implementation.recipients, self.channel),
            ),
        )
        self.composition = compose_downstream(self.bundle, runtime_settings=self.settings)
        self.addCleanup(self._close_composition)
        self.adapter = self.composition.adapter
        self.assertTrue(self.adapter.server_version().startswith("18."), self.adapter.server_version())
        self.adapter.connection.execute(
            "TRUNCATE handoff_delivery_attempt, handoff_delivery_status, handoff_intent, decision_snapshot, "
            "source_capability, source_snapshot, artifact_catalog, o3_attention_projection, query_snapshot_row, "
            "query_snapshot, read_head, read_revision, applied_effect, job, outbox_event, audit_event, "
            "command_receipt, aggregate_state CASCADE"
        )
        self.scope = self.composition.scope_provider()
        self.principal = self.composition.principal_provider()

    def _close_composition(self):
        composition = getattr(self, "composition", None)
        if composition is not None:
            try:
                composition.close()
            except Exception:
                pass

    def _context(self, command_id: str, version: int) -> CommandContext:
        return CommandContext(
            command_id,
            self.principal,
            self.scope,
            version,
            RevisionVector("u1-analysis", "u1-exposure", "u1-priority", version, None, "u1-manifest"),
        )

    def test_authorized_durable_read_claim_artifact_and_handoff_survive_recomposition(self):
        self.adapter.seed_aggregate(
            self.scope,
            "episode_workflow",
            "episode-u1",
            {"owner": None, "work_state": "OPEN"},
            version=0,
        )
        initialized = self.composition.decision_loop.initialize_decision_loop(
            self._context("u1-initialize", 0), "episode-u1"
        )
        current_workflow = self.adapter.get_aggregate(self.scope, "episode_workflow", "episode-u1")
        self.adapter.seed_attention_projection(
            self.scope,
            "episode-u1",
            {
                "title": "Synthetic downstream Episode",
                "asset_id": "synthetic-asset",
                "priority": "P2",
                "severity": "MEDIUM",
                "technical_state": "READY",
                "source_state": "NOT_QUALIFIED",
                "deadline": None,
                "age": "1",
            },
        )
        self.adapter.publish_current_revision(
            self.scope,
            "episode",
            "episode-u1",
            "u1-read-revision",
            RevisionVector("u1-analysis", None, None, initialized.aggregate_version, None, "u1-manifest"),
            {
                "title": "Synthetic downstream Episode",
                "analytical_revision": "u1-analysis",
                "capability_state": {"source": "NOT_QUALIFIED"},
            },
            current_workflow,
        )
        page = self.composition.attention.list_attention(self.principal, self.scope)
        self.assertEqual([item.episode_id for item in page.rows], ["episode-u1"])
        brief = self.composition.episode_briefs.get_episode_brief(self.principal, self.scope, "episode-u1")
        self.assertEqual(brief.workflow["work_state"], "OPEN")

        claimed = self.composition.workflow.claim_episode(
            self._context("u1-claim", initialized.aggregate_version), "episode-u1"
        )
        context = self._context("u1-snapshot", claimed.aggregate_version)
        snapshot = self.composition.handoff.create_decision_snapshot(
            context,
            "episode-u1",
            what_changed="A synthetic committed claim is ready for review.",
            why_it_matters="The assigned engineer has the next review.",
            key_limitation="The synthetic source is not family qualification.",
            next_authorized_action="Review the Episode through the existing workflow.",
        )
        outbox = next(row for row in self.adapter.list_outbox_events() if row["command_id"] == "u1-claim")
        created = self.composition.handoff.project_outbox_event(
            context,
            event_id=outbox["event_id"],
            snapshot_id=snapshot.snapshot_id,
            recipient_selector="synthetic-engineer",
        )
        replay = self.composition.handoff.project_outbox_event(
            self._context("u1-handoff-replay", claimed.aggregate_version),
            event_id=outbox["event_id"],
            snapshot_id=snapshot.snapshot_id,
            recipient_selector="synthetic-engineer",
        )
        self.assertEqual(created["intent_id"], replay["intent_id"])
        self.assertEqual(created["job_id"], replay["job_id"])
        before_delivery = self.adapter.get_aggregate(self.scope, "episode_workflow", "episode-u1")
        self.composition.handoff.dispatch_once(
            self.principal,
            self.scope,
            worker_id="u1-synthetic-worker",
            channel_adapter=self.composition.notification_channel,
        )
        status = self.composition.handoff.read_handoff_status(self.principal, self.scope, created["intent_id"])
        self.assertEqual(status["delivery_state"], "UNKNOWN")
        self.assertEqual(len(self.channel.calls), 1)
        self.assertEqual(self.channel.calls[0][2], status["idempotency_key"])
        self.assertNotIn("raw_measurements", self.channel.calls[0][1])
        self.assertEqual(self.adapter.get_aggregate(self.scope, "episode_workflow", "episode-u1"), before_delivery)
        reconciled = self.composition.handoff.reconcile_unknown(
            self.principal,
            self.scope,
            created["intent_id"],
            self.composition.notification_channel,
        )
        self.assertEqual(reconciled["delivery_state"], "DELIVERED")

        source_content = b"synthetic source partition artifact"
        source_artifact = self.composition.artifact_service.write_and_register(
            self.principal,
            self.scope,
            source_content,
            media_type="application/octet-stream",
            logical_purpose="u1-synthetic-source-artifact",
            required_write_capability="synthetic.artifact.write",
        )
        event_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        observations = self.composition.source_observer.read_partition(
            start_at=event_at,
            end_at=event_at,
            limit=100,
        )
        source_draft = SourceSnapshotDraft(
            self.composition.source_binding,
            "synthetic-partition",
            "synthetic-revision-1",
            event_at,
            event_at,
            event_at,
            source_artifact.metadata.reference,
            observations,
        )
        source_record, source_capability = self.composition.source_ingress.publish(
            self.principal,
            source_draft,
            freshness_age_seconds=60,
        )
        self.assertEqual(source_record.row_count, 1)
        self.assertEqual(source_capability.state.value, "STALE")
        source_columns = {
            row["column_name"]
            for row in self.adapter.connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'source_snapshot'"
            ).fetchall()
        }
        self.assertNotIn("observations_json", source_columns)
        self.assertNotIn("source_rows_json", source_columns)

        artifact_content = b"synthetic downstream immutable artifact"
        artifact = self.composition.artifact_service.write_and_register(
            self.principal,
            self.scope,
            artifact_content,
            media_type="application/octet-stream",
            logical_purpose="u1-synthetic-evidence",
            required_write_capability="synthetic.artifact.write",
        )
        self.assertEqual(
            self.composition.artifact_service.retrieve(
                self.principal,
                artifact.metadata.reference,
                "synthetic.artifact.read",
            ).content,
            artifact_content,
        )
        self.assertIs(self.composition.artifact_service.catalog.adapter, self.adapter)
        self.assertIs(self.composition.handoff.worker.adapter, self.adapter)
        self.assertIs(self.composition.handoff.storage.adapter, self.adapter)
        self.assertEqual(self.adapter.count_rows()["aggregate_state"], 1)

        self.composition.close()
        self.composition = compose_downstream(self.bundle, runtime_settings=self.settings)
        self.adapter = self.composition.adapter
        restarted_principal = self.composition.principal_provider()
        restarted_scope = self.composition.scope_provider()
        restarted = self.composition.episode_briefs.get_episode_brief(
            restarted_principal, restarted_scope, "episode-u1"
        )
        self.assertEqual(restarted.workflow["owner"], self.principal.subject)
        restarted_status = self.composition.handoff.read_handoff_status(
            restarted_principal, restarted_scope, created["intent_id"]
        )
        self.assertEqual(restarted_status["delivery_state"], "DELIVERED")
        restarted_source = self.composition.source_ingress.repository.get_snapshot(
            restarted_principal,
            restarted_scope,
            source_record.snapshot_id,
        )
        self.assertEqual(restarted_source.row_count, 1)
        self.assertEqual(restarted_source.immutable_identity, source_record.immutable_identity)
        self.assertEqual(
            self.composition.artifact_service.retrieve(
                restarted_principal,
                artifact.metadata.reference,
                "synthetic.artifact.read",
            ).content,
            artifact_content,
        )
        self.assertGreaterEqual(self.adapter.count_rows()["aggregate_state"], 1)


if __name__ == "__main__":
    unittest.main()
