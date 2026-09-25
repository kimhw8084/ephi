"""CHG-233 Family Center gate and promotion history on PostgreSQL 18."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()

from ephi.config import RuntimeSettings  # noqa: E402
from ephi.downstream import compose_downstream  # noqa: E402
from examples.synthetic_downstream.family_center import (  # noqa: E402
    GREEN_RELEASE_ID,
    FAMILY_ID,
    publish_current_synthetic_source,
    seed_synthetic_workspace,
)
from examples.synthetic_downstream.provider import build_bundle  # noqa: E402


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not configured; PostgreSQL integration is NOT_RUN")
class FamilyCenterPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "EPHI_TEST_POSTGRES_DSN": DSN,
                "EPHI_SYNTHETIC_SUBJECT": "synthetic-engineer",
                "EPHI_SYNTHETIC_ARTIFACT_ROOT": str(Path(self.temp.name) / "blobs"),
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        bundle = build_bundle()
        self.runtime_class = bundle.runtime.public_metadata.target_environment_class
        self.composition = compose_downstream(bundle, runtime_settings=RuntimeSettings(environment=self.runtime_class))
        self.addCleanup(self._close)
        self.assertTrue(self.composition.adapter.server_version().startswith("18."), self.composition.adapter.server_version())
        self.composition.adapter.connection.execute(
            "TRUNCATE handoff_delivery_attempt, handoff_delivery_status, handoff_intent, decision_snapshot, "
            "source_capability, source_snapshot, artifact_catalog, o3_attention_projection, query_snapshot_row, "
            "query_snapshot, read_head, read_revision, applied_effect, job, outbox_event, audit_event, "
            "command_receipt, aggregate_state CASCADE"
        )

    def _close(self):
        value = getattr(self, "composition", None)
        if value is not None:
            value.close()

    def test_gate_and_promotion_history_survive_restart_then_invalidate(self):
        seeded = seed_synthetic_workspace(self.composition, GREEN_RELEASE_ID, "green")
        self.assertTrue(seeded["synthetic"])
        self.assertFalse(seeded["production_approval"])
        original_identity = seeded["workspace_id"]
        scope = self.composition.scope_provider()
        aggregate = self.composition.adapter.get_aggregate(scope, "family_qualification_workspace", original_identity)
        self.assertEqual(len(aggregate.state["promotion_records"]), 1)
        original_revisions = tuple(item["revision_id"] for item in aggregate.state["gate_revisions"])
        self.assertGreaterEqual(len(original_revisions), 8)

        self.composition.close()
        bundle = build_bundle()
        self.composition = compose_downstream(bundle, runtime_settings=RuntimeSettings(environment=self.runtime_class))
        self.assertTrue(self.composition.adapter.server_version().startswith("18."), self.composition.adapter.server_version())
        principal = self.composition.principal_provider()
        scope = self.composition.scope_provider()
        family = next(item for item in self.composition.policy_configuration.family_contexts if item.family_id == FAMILY_ID)
        target = next(item for item in family.qualification_targets if item.release_id == GREEN_RELEASE_ID)
        identity = self.composition.family_workspace_identity(FAMILY_ID, target)
        self.assertEqual(identity.identity, original_identity)
        restored = self.composition.family_center.get_workspace(principal, scope, original_identity, current_identity=identity)
        self.assertTrue(restored.promotion_ready)
        self.assertEqual(restored.promotions[-1].state, "CURRENT")
        self.assertEqual(
            tuple(item["revision_id"] for item in self.composition.adapter.get_aggregate(scope, "family_qualification_workspace", original_identity).state["gate_revisions"]),
            original_revisions,
        )

        publish_current_synthetic_source(self.composition, revision="restart-dependency-change")
        invalidated = self.composition.family_center.get_workspace(
            principal, scope, original_identity, current_identity=identity
        )
        self.assertFalse(invalidated.promotion_ready)
        self.assertEqual(invalidated.promotions[-1].state, "STALE")
        after = self.composition.adapter.get_aggregate(scope, "family_qualification_workspace", original_identity)
        self.assertEqual(len(after.state["promotion_records"]), 1)
        self.assertEqual(tuple(item["revision_id"] for item in after.state["gate_revisions"]), original_revisions)

    def test_u1_synthetic_fixture_states_are_truthful_and_non_production(self):
        cases = (
            ("synthetic-release-1", "green"),
            ("synthetic-release-ambiguous", "ambiguous"),
            ("synthetic-release-expired", "expired"),
            ("synthetic-release-failed", "failed"),
            ("synthetic-release-pending", "pending"),
        )
        seeded = {release: seed_synthetic_workspace(self.composition, release, mode) for release, mode in cases}
        for item in seeded.values():
            self.assertTrue(item["synthetic"])
            self.assertFalse(item["production_approval"])
        scope = self.composition.scope_provider()
        principal = self.composition.principal_provider()
        families = self.composition.policy_configuration.family_contexts
        configured_family = next(item for item in families if item.family_id == FAMILY_ID)

        def view(release):
            target = next(item for item in configured_family.qualification_targets if item.release_id == release)
            identity = self.composition.family_workspace_identity(FAMILY_ID, target)
            return self.composition.family_center.get_workspace(
                principal, scope, identity.identity, current_identity=identity
            )

        self.assertTrue(view("synthetic-release-1").promotion_ready)
        self.assertEqual(next(gate for gate in view("synthetic-release-ambiguous").gates if gate.stage_id == "DISCOVER_MAP").state.value, "BLOCKED")
        self.assertEqual(next(gate for gate in view("synthetic-release-expired").gates if gate.stage_id == "DISCOVER_MAP").state.value, "EXPIRED")
        self.assertEqual(next(gate for gate in view("synthetic-release-failed").gates if gate.stage_id == "DISCOVER_MAP").state.value, "FAIL")
        pending_replay = next(gate for gate in view("synthetic-release-pending").gates if gate.stage_id == "REPLAY")
        self.assertEqual(pending_replay.state.value, "PENDING")
        self.assertIsNotNone(pending_replay.job_id)
        pending_job = self.composition.adapter.worker_store().inspect(scope, job_id=pending_replay.job_id, limit=1)
        self.assertEqual(len(pending_job), 1)
        self.assertEqual(pending_job[0].status, "QUEUED")
        self.assertEqual(pending_replay.job_status, "QUEUED")

        stale_seed = seed_synthetic_workspace(self.composition, "synthetic-release-stale", "stale")
        self.assertFalse(stale_seed["promotion_ready"])
        self.assertEqual(stale_seed["promotion_state"], "STALE")
        self.assertEqual(view("synthetic-release-1").promotions[-1].state, "STALE")

    def test_future_corrupt_gate_revision_stays_blocked_after_postgresql_restart(self):
        seeded = seed_synthetic_workspace(self.composition, "synthetic-release-future", "future")
        scope = self.composition.scope_provider()
        workspace_id = seeded["workspace_id"]
        aggregate = self.composition.adapter.get_aggregate(scope, "family_qualification_workspace", workspace_id)
        revision_ids = tuple(item["revision_id"] for item in aggregate.state["gate_revisions"])
        state = aggregate.state
        future_time = datetime.now(timezone.utc) + timedelta(days=1)
        discovery = next(item for item in reversed(state["gate_revisions"]) if item["stage_id"] == "DISCOVER_MAP")
        discovery["known_at"] = future_time.isoformat()
        discovery["published_at"] = future_time.isoformat()
        discovery["expires_at"] = (future_time + timedelta(days=1)).isoformat()
        self.composition.adapter.connection.execute(
            "UPDATE aggregate_state SET state_json = %s::jsonb WHERE scope_key = %s "
            "AND aggregate_type = 'family_qualification_workspace' AND aggregate_id = %s",
            (json.dumps(state, sort_keys=True, separators=(",", ":")), scope.canonical_key, workspace_id),
        )

        self.composition.close()
        bundle = build_bundle()
        self.composition = compose_downstream(bundle, runtime_settings=RuntimeSettings(environment=self.runtime_class))
        self.assertTrue(self.composition.adapter.server_version().startswith("18."))
        principal = self.composition.principal_provider()
        scope = self.composition.scope_provider()
        family = next(item for item in self.composition.policy_configuration.family_contexts if item.family_id == FAMILY_ID)
        target = next(item for item in family.qualification_targets if item.release_id == "synthetic-release-future")
        identity = self.composition.family_workspace_identity(FAMILY_ID, target)
        view = self.composition.family_center.get_workspace(principal, scope, workspace_id, current_identity=identity)

        discovery_view = next(item for item in view.gates if item.stage_id == "DISCOVER_MAP")
        self.assertEqual(discovery_view.state.value, "BLOCKED")
        self.assertEqual(discovery_view.invalidation_reason, "FUTURE_EVIDENCE_TIME")
        self.assertFalse(view.promotion_ready)
        self.assertIn("DISCOVER_MAP:FUTURE_EVIDENCE_TIME", view.promotion_blockers)
        restored = self.composition.adapter.get_aggregate(scope, "family_qualification_workspace", workspace_id)
        self.assertEqual(tuple(item["revision_id"] for item in restored.state["gate_revisions"]), revision_ids)


if __name__ == "__main__":
    unittest.main()
