"""Real PostgreSQL 18 regressions for CHG-234 U2.4 Asset 360."""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
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
    ASSET_READ_CAPABILITY,
    AccessScope,
    AuthorizationDeniedError,
    CommandContext,
    EPISODE_READ_CAPABILITY,
    Principal,
    RevisionVector,
    SourceBindingUnavailableError,
    SourceCapabilityState,
)
from ephi.application.assets import Asset360QueryService  # noqa: E402
from ephi.config import RuntimeSettings  # noqa: E402
from ephi.downstream import compose_downstream  # noqa: E402
from examples.synthetic_downstream.assets import (  # noqa: E402
    INCOMPATIBLE_ASSET,
    PEER_ASSET,
    PRIMARY_ASSET,
    PRIMARY_EPISODES,
    seed_asset_360_fixture,
)
from examples.synthetic_downstream.provider import build_flagship_bundle  # noqa: E402


UTC = timezone.utc


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class Asset360PostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.anchor = datetime.now(UTC).replace(microsecond=0)
        self.environment = patch.dict(os.environ, {
            "EPHI_SYNTHETIC_ARTIFACT_ROOT": str(Path(self.temp.name) / "blobs"),
            "EPHI_SYNTHETIC_ASSET_OBSERVATION_ANCHOR": self.anchor.isoformat(),
        }, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.bundle = build_flagship_bundle()
        self._compose()
        self.seed = seed_asset_360_fixture(self.composition)
        self.scope = self.composition.scope_provider()
        self.principal = self.composition.principal_provider()
        self.service = self._service(self.composition)

    def tearDown(self):
        self._close()

    def _compose(self):
        settings = RuntimeSettings(environment=self.bundle.runtime.public_metadata.target_environment_class)
        self.composition = compose_downstream(self.bundle, runtime_settings=settings)
        self.assertTrue(self.composition.adapter.server_version().startswith("18."), self.composition.adapter.server_version())

    @staticmethod
    def _service(composition):
        return Asset360QueryService(
            composition.adapter.o3_store(), composition.adapter.read_store(),
            composition.current_authorization, composition.source_observer,
            composition.source_binding, composition.adapter.source_store(),
        )

    def _close(self):
        composition = getattr(self, "composition", None)
        if composition is not None:
            try:
                composition.close()
            finally:
                self.composition = None

    def _query(self, *, cutoff=None, peer=None):
        cutoff = cutoff or datetime.now(UTC) - timedelta(milliseconds=1)
        return self.service.get_asset_360(
            self.principal, self.scope, PRIMARY_ASSET,
            knowledge_cutoff=cutoff,
            window_start=self.anchor - timedelta(days=30),
            window_end=cutoff,
            peer_asset_id=peer,
        )

    def test_authorization_precedes_asset_existence_and_exact_asset_ids(self):
        self.assertEqual(ASSET_READ_CAPABILITY, EPISODE_READ_CAPABILITY)
        denied = Principal(
            self.principal.subject,
            tuple(item for item in self.principal.capabilities if item != EPISODE_READ_CAPABILITY),
            self.principal.scope_grants,
            self.principal.auth_session_revision,
            self.principal.security_revision,
        )
        with patch.object(self.service.row_source, "fetch_asset_episode_heads", side_effect=AssertionError("asset scan ran before O8")) as heads:
            with self.assertRaises(AuthorizationDeniedError):
                self.service.list_assets(denied, self.scope)
            heads.assert_not_called()

        page = self.service.list_assets(self.principal, self.scope, page_size=10)
        self.assertEqual(page.total_count, 3)
        self.assertEqual({row["asset_id"] for row in page.rows}, {PRIMARY_ASSET, PEER_ASSET, INCOMPATIBLE_ASSET})
        primary = next(row for row in page.rows if row["asset_id"] == PRIMARY_ASSET)
        self.assertEqual(primary["episode_count"], 3)
        self.assertEqual(primary["family_identity"], "synthetic-cd-metrology-family")
        self.assertEqual(primary["context_identity"], "synthetic-recipe-r47")
        context_filtered = self.service.list_assets(
            self.principal, self.scope, filters={"context": "synthetic-recipe-r47", "site": "synthetic-site"},
        )
        self.assertEqual({row["asset_id"] for row in context_filtered.rows}, {PRIMARY_ASSET, PEER_ASSET})
        mismatched_site = self.service.list_assets(self.principal, self.scope, filters={"site": "synthetic-other-site"})
        self.assertEqual(mismatched_site.total_count, 0)

    def test_as_of_timeline_o5_state_and_identity_survive_postgresql_recomposition(self):
        cutoff = datetime.now(UTC) - timedelta(milliseconds=1)
        first = self._query(cutoff=cutoff)
        self.assertEqual(len(first.episodes), 4)
        self.assertEqual(len({item["episode_id"] for item in first.episodes}), 3)
        self.assertEqual(sum(item["historical_revision"] for item in first.episodes), 1)
        self.assertTrue(any(item["workflow_label"] == "Historical workflow snapshot at Episode publication" for item in first.episodes))
        self.assertNotIn("synthetic-cd-primary-future-episode", {item["episode_id"] for item in first.episodes})
        self.assertEqual(first.asset["open_work_count"], 3)
        self.assertTrue(any(item["kind"] == "ACTION" for item in first.changes))
        action = next(item for item in first.changes if item["kind"] == "ACTION")
        self.assertIsNotNone(action["recorded_at"])
        self.assertEqual(action["reconciliation_state"], "UNKNOWN")
        self.assertIsNotNone(action["requested_at"])

        identity_before = first.query_identity
        result_before = first.result_identity
        self._close()
        self._compose()
        self.service = self._service(self.composition)
        self.scope = self.composition.scope_provider()
        self.principal = self.composition.principal_provider()
        after_restart = self._query(cutoff=cutoff)
        self.assertEqual(after_restart.query_identity, identity_before)
        self.assertEqual(after_restart.result_identity, result_before)

        episode_id = PRIMARY_EPISODES[-1]
        aggregate = self.composition.adapter.get_aggregate(
            self.scope, "episode_workflow", episode_id,
        )
        context = CommandContext(
            "synthetic-asset-regression-claim", self.principal, self.scope, aggregate.version,
            RevisionVector("synthetic-analysis-claim", None, None, aggregate.version, None, "synthetic-manifest-claim"),
        )
        self.composition.workflow.claim_episode(context, episode_id)
        still_as_of_cutoff = self._query(cutoff=cutoff)
        self.assertEqual(still_as_of_cutoff.query_identity, identity_before)
        later = self._query(cutoff=datetime.now(UTC) + timedelta(minutes=1))
        self.assertNotEqual(later.query_identity, identity_before)
        latest = next(item for item in later.episodes if item["episode_id"] == episode_id and not item["historical_revision"])
        self.assertEqual(latest["workflow_state"], "CLAIMED")
        self.assertEqual(latest["workflow_label"], "O5 workflow as known at cutoff")
        historical = next(item for item in later.episodes if item["episode_id"] == episode_id and item["historical_revision"])
        self.assertEqual(historical["workflow_state"], "OPEN")
        self.assertFalse(historical["actions"])

    def test_source_observations_are_exact_bounded_and_comparison_fails_closed(self):
        cutoff = datetime.now(UTC) - timedelta(milliseconds=1)
        result = self._query(cutoff=cutoff)
        row_ids = {item["source_row_id"] for item in result.measurement["points"]}
        self.assertEqual(row_ids, {
            "synthetic-asset-primary-p0", "synthetic-asset-primary-p1",
            "synthetic-asset-primary-p2", "synthetic-asset-primary-p3",
        })
        times = [item["event_at"] for item in result.measurement["points"]]
        self.assertGreaterEqual((times[-1] - times[-2]).total_seconds(), 20 * 24 * 3600)
        self.assertEqual(result.source["state"], "READY")

        compatible = self._query(cutoff=cutoff, peer=PEER_ASSET)
        self.assertEqual(compatible.compare["state"], "READY")
        self.assertEqual(compatible.compare["population_identity"], self.composition.source_binding.comparable_population_id)
        self.assertEqual(compatible.compare["context_identity"], "synthetic-recipe-r47")
        self.assertEqual(compatible.compare["unit_identity"], "nm")

        blocked = self._query(cutoff=cutoff, peer=INCOMPATIBLE_ASSET)
        self.assertEqual(blocked.compare["state"], "BLOCKED")
        self.assertEqual(blocked.compare["reason"], "FAMILY_CONTEXT_CHARACTERISTIC_OR_UNIT_MISMATCH")

        mismatched = type("MismatchedObserver", (), {
            "describe": lambda _self: replace(self.composition.source_binding, mapping_version="different"),
            "read_partition": lambda *_args, **_kwargs: (),
        })()
        mismatch_service = Asset360QueryService(
            self.service.row_source, self.service.read_store, self.service.current_authorization,
            mismatched, self.service.source_binding, self.service.source_store,
        )
        with self.assertRaises(SourceBindingUnavailableError):
            mismatch_service.get_asset_360(
                self.principal, self.scope, PRIMARY_ASSET,
                knowledge_cutoff=cutoff, window_start=self.anchor - timedelta(days=30), window_end=cutoff,
            )

    def test_o4_capability_partial_stale_and_unavailable_are_never_ready(self):
        states = (
            ("STALE", "STALE"),
            ("PARTIAL", "PARTIAL"),
            ("UNAVAILABLE", "UNAVAILABLE"),
        )
        for capability_case, expected in states:
            with self.subTest(capability_case=capability_case):
                self.seed = seed_asset_360_fixture(self.composition, source_capability_case=capability_case)
                result = self._query(cutoff=datetime.now(UTC) + timedelta(seconds=10))
                self.assertEqual(result.source["state"], expected)
                self.assertNotEqual(result.source["state"], "READY")

    def test_retained_asset_pages_survive_head_change_and_restart(self):
        first = self.service.list_assets(self.principal, self.scope, page_size=1)
        self.assertIsNotNone(first.next_cursor)
        first_row = first.rows[0]
        episode_id = first_row["latest_episode_id"]
        head = self.composition.adapter.read_store().get_current_head(self.scope, "episode", episode_id)
        revision = self.composition.adapter.read_store().get_read_revision(head.revision_id)
        aggregate = self.composition.adapter.get_aggregate(self.scope, "episode_workflow", episode_id)
        payload = dict(revision.payload)
        payload["title"] = "Synthetic revised title after retained Asset snapshot"
        self.composition.adapter.publish_current_revision(
            self.scope, "episode", episode_id, f"synthetic-retained-head-{episode_id}",
            RevisionVector("synthetic-retained-head-analysis", None, None, aggregate.version, None, "synthetic-retained-head-manifest"),
            payload, aggregate, expected_head_version=head.head_version, expected_revision_id=head.revision_id,
        )
        current = self.service.list_assets(self.principal, self.scope, page_size=10)
        changed_asset = next(item for item in current.rows if item["asset_id"] == first_row["asset_id"])
        self.assertNotEqual(changed_asset["latest_revision_id"], first_row["latest_revision_id"])

        snapshot_identity = first.query_identity["facts_identity"]
        snapshot_id = first.snapshot_id
        cursor = first.next_cursor
        self._close()
        self._compose()
        self.service = self._service(self.composition)
        self.scope = self.composition.scope_provider()
        self.principal = self.composition.principal_provider()
        second = self.service.list_assets(
            self.principal, self.scope, page_size=1, snapshot_id=snapshot_id,
            cursor=cursor, facts_identity=snapshot_identity,
        )
        full_snapshot = self.service.read_store.read_query_snapshot_page(
            self.principal, self.scope, snapshot_id, first.query_identity, ASSET_READ_CAPABILITY,
            page_size=2, cursor=None,
        )
        self.assertEqual(second.total_count, 3)
        self.assertEqual(second.rows[0]["asset_id"], full_snapshot.rows[1].payload["asset_id"])
        self.assertNotEqual(second.rows[0]["asset_id"], first_row["asset_id"])


if __name__ == "__main__":
    unittest.main()
