"""Real PostgreSQL 18 regressions for CHG-234 U2.4 Asset 360."""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


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
    RevisionPinnedObservationBatch,
    MetrologyObservation,
    SourceSnapshotDraft,
    SourceSnapshotStatus,
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
from examples.synthetic_downstream.provider import SyntheticAssetObserver, build_flagship_bundle  # noqa: E402


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

    def _query_service(self, service, *, cutoff, peer=None):
        return service.get_asset_360(
            self.principal, self.scope, PRIMARY_ASSET,
            knowledge_cutoff=cutoff,
            window_start=self.anchor - timedelta(days=30),
            window_end=cutoff,
            peer_asset_id=peer,
        )

    def _publish_revision(self, revision: str, available_cutoff: datetime):
        binding = self.composition.source_binding
        observer = self.composition.source_observer
        observation = next(item for item in observer._observations if item.asset_id == PRIMARY_ASSET and item.unit == binding.unit)
        artifact = self.composition.artifact_service.write_and_register(
            self.principal,
            self.scope,
            f'{{"synthetic":true,"revision":"{revision}"}}'.encode(),
            media_type="application/json",
            logical_purpose=f"synthetic-{revision}",
            required_write_capability="synthetic.artifact.write",
        )
        return self.composition.source_ingress.publish(
            self.principal,
            SourceSnapshotDraft(
                binding,
                "synthetic-asset-360-partition",
                revision,
                observation.event_at,
                observation.event_at,
                available_cutoff,
                artifact.metadata.reference,
                (observation,),
                SourceSnapshotStatus.PUBLISHED,
            ),
            freshness_age_seconds=3600,
        )[0]

    def _insert_cutoff_edge_snapshot(
        self, record, *, snapshot_id, partition, revision, ingested_at,
        published_at, available_cutoff, freshness_age_seconds=3600,
    ):
        binding = record.binding
        self.composition.adapter.connection.execute(
            """
            INSERT INTO source_snapshot(
                snapshot_id, schema_version, scope_key, source_id, provider_id, family_id, capability_id,
                adapter_id, schema_id, mapping_version, mapping_hash, unit, reference_population_id,
                comparable_population_id, required_identifiers_json, source_partition, source_revision,
                event_start, event_end, available_cutoff, manifest_artifact_sha256,
                manifest_artifact_byte_size, manifest_artifact_object_key, row_count, status,
                manifest_hash, ingested_at, published_at, freshness_age_seconds
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                %s, %s, %s, %s, %s, %s, %s, %s, 1, 'PUBLISHED', %s, %s, %s, %s
            )
            """,
            (
                snapshot_id, record.schema_version, binding.scope_key, binding.source_id,
                binding.provider_id, binding.family_id, binding.capability_id, binding.adapter_id,
                binding.schema_id, binding.mapping_version, binding.mapping_hash, binding.unit,
                binding.reference_population_id, binding.comparable_population_id,
                json.dumps(binding.required_identifiers), partition, revision,
                record.event_start, record.event_end, available_cutoff,
                record.artifact_reference.content.sha256, record.artifact_reference.content.byte_size,
                f"sha256/{record.artifact_reference.content.sha256}", hashlib.sha256(snapshot_id.encode()).hexdigest(),
                ingested_at, published_at, freshness_age_seconds,
            ),
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
        self.assertEqual(result.measurement["source_revision"], result.source["source_revision"])
        self.assertEqual(result.measurement["snapshot_id"], result.source["snapshot_id"])
        self.assertEqual(result.measurement["manifest_hash"], result.source["snapshot_manifest_hash"])
        self.assertEqual(len(result.measurement["identity_facts"]["observation_identity_set"]), 4)

        compatible = self._query(cutoff=cutoff, peer=PEER_ASSET)
        self.assertEqual(compatible.compare["state"], "READY")
        self.assertEqual(compatible.compare["population_identity"], self.composition.source_binding.comparable_population_id)
        self.assertEqual(compatible.compare["context_identity"], "synthetic-recipe-r47")
        self.assertEqual(compatible.compare["unit_identity"], "nm")
        self.assertEqual(
            compatible.compare["source_snapshot_identity"]["source_revision"],
            compatible.source["source_revision"],
        )
        self.assertEqual(
            compatible.compare["source_snapshot_identity"]["snapshot_id"],
            compatible.source["snapshot_id"],
        )
        self.assertEqual(
            compatible.compare["source_snapshot_identity"]["binding_identity"],
            compatible.measurement["binding_identity"],
        )
        self.assertTrue(compatible.compare["peer_observation_identity_set"])

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

    def test_measurements_are_pinned_to_immutable_o4_revision_after_refresh_and_restart(self):
        source_store = self.composition.adapter.source_store()
        s1 = source_store.get_latest_snapshot_as_of(
            self.principal, self.composition.source_binding, datetime.now(UTC) + timedelta(seconds=2),
        )
        self.assertIsNotNone(s1)
        cutoff = s1.published_at + timedelta(microseconds=1)
        before = self._query(cutoff=cutoff)
        self.assertEqual(before.source["snapshot_id"], s1.snapshot_id)
        self.assertEqual(before.source["source_revision"], s1.source_revision)
        s1_point_ids = tuple(item["source_row_id"] for item in before.measurement["points"])
        s1_query_identity = before.query_identity
        s1_result_identity = before.result_identity
        s1_observation_ids = before.measurement["identity_facts"]["observation_identity_set"]

        observer = self.composition.source_observer
        s1_rows = tuple(observer._observations)
        s2_rows = tuple(replace(item, value=float(item.value) + 10.0) for item in s1_rows)
        s1_key = ("synthetic-asset-360-partition", s1.source_revision)
        s2_revision = "synthetic-asset-360-ready-v2"
        s2_key = ("synthetic-asset-360-partition", s2_revision)
        prior_overrides = dict(SyntheticAssetObserver._revision_overrides)
        prior_live = SyntheticAssetObserver._live_override
        self.addCleanup(lambda: (SyntheticAssetObserver._revision_overrides.clear(), SyntheticAssetObserver._revision_overrides.update(prior_overrides)))
        self.addCleanup(setattr, SyntheticAssetObserver, "_live_override", prior_live)
        SyntheticAssetObserver._revision_overrides[s1_key] = s1_rows
        SyntheticAssetObserver._revision_overrides[s2_key] = s2_rows
        SyntheticAssetObserver._live_override = s2_rows
        live_rows = observer.read_partition(
            start_at=self.anchor - timedelta(days=30), end_at=datetime.now(UTC) + timedelta(seconds=1), limit=500,
        )
        self.assertNotEqual(live_rows[0].value, s1_rows[0].value)

        later_availability = datetime.now(UTC) - timedelta(seconds=1)
        s2 = self._publish_revision(s2_revision, later_availability)
        current_capability = source_store.get_capability(self.principal, self.composition.source_binding)
        self.assertEqual(current_capability.latest_snapshot_id, s2.snapshot_id)
        self.assertEqual(current_capability.latest_source_revision, s2.source_revision)

        still_before_restart = self._query(cutoff=cutoff)
        self.assertEqual(still_before_restart.source["snapshot_id"], s1.snapshot_id)
        self.assertEqual(still_before_restart.measurement["identity_facts"]["observation_identity_set"], s1_observation_ids)
        self.assertEqual(still_before_restart.query_identity, s1_query_identity)
        self.assertEqual(still_before_restart.result_identity, s1_result_identity)

        self._close()
        self._compose()
        self.service = self._service(self.composition)
        self.scope = self.composition.scope_provider()
        self.principal = self.composition.principal_provider()
        after_restart = self._query(cutoff=cutoff)
        self.assertEqual(after_restart.source["snapshot_id"], s1.snapshot_id)
        self.assertEqual(after_restart.source["source_revision"], s1.source_revision)
        self.assertEqual(tuple(item["source_row_id"] for item in after_restart.measurement["points"]), s1_point_ids)
        self.assertEqual(after_restart.measurement["identity_facts"]["observation_identity_set"], s1_observation_ids)
        self.assertEqual(after_restart.query_identity, s1_query_identity)
        self.assertEqual(after_restart.result_identity, s1_result_identity)

        child_env = dict(os.environ)
        child_env.update({
            "EPHI_SYNTHETIC_ASSET_LIVE_REVISION": s2_revision,
            "EPHI_SYNTHETIC_ASSET_TEST_CUTOFF": cutoff.isoformat(),
            "EPHI_SYNTHETIC_ASSET_OBSERVATION_ANCHOR": self.anchor.isoformat(),
            "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT), child_env.get("PYTHONPATH", ""))),
        })
        child_code = """
import json, os, sys
from datetime import datetime, timedelta
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])
from ephi.application.assets import Asset360QueryService
from ephi.config import RuntimeSettings
from ephi.downstream import compose_downstream
from examples.synthetic_downstream.provider import build_flagship_bundle
anchor = datetime.fromisoformat(os.environ["EPHI_SYNTHETIC_ASSET_OBSERVATION_ANCHOR"])
cutoff = datetime.fromisoformat(os.environ["EPHI_SYNTHETIC_ASSET_TEST_CUTOFF"])
bundle = build_flagship_bundle()
composition = compose_downstream(bundle, runtime_settings=RuntimeSettings(environment=bundle.runtime.public_metadata.target_environment_class))
try:
    service = Asset360QueryService(composition.adapter.o3_store(), composition.adapter.read_store(), composition.current_authorization, composition.source_observer, composition.source_binding, composition.adapter.source_store())
    result = service.get_asset_360(composition.principal_provider(), composition.scope_provider(), "synthetic-cd-asset-primary", knowledge_cutoff=cutoff, window_start=anchor - timedelta(days=30), window_end=cutoff)
    print(json.dumps({"query_identity": result.query_identity, "result_identity": result.result_identity, "snapshot_id": result.source["snapshot_id"], "source_revision": result.source["source_revision"], "point_ids": result.measurement["identity_facts"]["observation_identity_set"]}, sort_keys=True))
finally:
    composition.close()
"""
        child = subprocess.run(
            [sys.executable, "-c", child_code],
            cwd=ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        child_result = json.loads(child.stdout.strip().splitlines()[-1])
        self.assertEqual(child_result["snapshot_id"], s1.snapshot_id)
        self.assertEqual(child_result["source_revision"], s1.source_revision)
        self.assertEqual(child_result["point_ids"], s1_observation_ids)
        self.assertEqual(child_result["query_identity"], s1_query_identity)
        self.assertEqual(child_result["result_identity"], s1_result_identity)

        later_cutoff = datetime.now(UTC) + timedelta(seconds=30)
        later = self._query(cutoff=later_cutoff)
        self.assertEqual(later.source["snapshot_id"], s2.snapshot_id)
        self.assertEqual(later.source["source_revision"], s2.source_revision)
        self.assertNotEqual(later.measurement["identity_facts"]["observation_identity_set"], s1_observation_ids)
        self.assertNotEqual(later.query_identity, s1_query_identity)
        self.assertNotEqual(later.result_identity, s1_result_identity)

    def test_as_of_o4_snapshot_read_excludes_future_publication_and_availability(self):
        store = self.composition.adapter.source_store()
        cutoff = datetime.now(UTC) + timedelta(seconds=3)
        s1 = store.get_latest_snapshot_as_of(self.principal, self.composition.source_binding, cutoff)
        self.assertIsNotNone(s1)
        now = datetime.now(UTC)
        self._insert_cutoff_edge_snapshot(
            s1,
            snapshot_id="synthetic-future-publication-snapshot",
            partition="synthetic-future-publication-partition",
            revision="synthetic-future-publication-revision",
            ingested_at=cutoff,
            published_at=cutoff + timedelta(seconds=1),
            available_cutoff=cutoff - timedelta(seconds=1),
        )
        self._insert_cutoff_edge_snapshot(
            s1,
            snapshot_id="synthetic-future-availability-snapshot",
            partition="synthetic-future-availability-partition",
            revision="synthetic-future-availability-revision",
            ingested_at=now - timedelta(seconds=1),
            published_at=now,
            available_cutoff=cutoff + timedelta(seconds=1),
        )
        self.composition.adapter.connection.commit()
        selected = store.get_latest_snapshot_as_of(self.principal, self.composition.source_binding, cutoff)
        self.assertEqual(selected.snapshot_id, s1.snapshot_id)

    def test_legacy_o4_snapshot_without_immutable_freshness_policy_is_explicitly_unavailable(self):
        store = self.composition.adapter.source_store()
        current = datetime.now(UTC)
        s1 = store.get_latest_snapshot_as_of(
            self.principal, self.composition.source_binding, current + timedelta(seconds=5),
        )
        self._insert_cutoff_edge_snapshot(
            s1,
            snapshot_id="synthetic-legacy-no-freshness-snapshot",
            partition="synthetic-legacy-partition",
            revision="synthetic-legacy-revision",
            ingested_at=current - timedelta(seconds=2),
            published_at=current,
            available_cutoff=current - timedelta(seconds=3),
            freshness_age_seconds=None,
        )
        self.composition.adapter.connection.commit()
        result = self._query(cutoff=current + timedelta(seconds=5))
        self.assertEqual(result.source["snapshot_id"], "synthetic-legacy-no-freshness-snapshot")
        self.assertEqual(result.source["state"], "UNAVAILABLE")
        self.assertEqual(result.source["reason"], "O4_FRESHNESS_POLICY_NOT_RECONSTRUCTABLE")
        self.assertIn("O4_FRESHNESS_POLICY_NOT_RECONSTRUCTABLE", result.limitations)

    def test_revision_pinned_provider_absence_and_mismatches_fail_closed_without_live_fallback(self):
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        delegate = self.composition.source_observer
        live_read = Mock(wraps=delegate.read_partition)

        class LegacyObserver:
            def describe(inner_self):
                return delegate.describe()

            def read_partition(inner_self, **kwargs):
                return live_read(**kwargs)

        legacy_service = self._service(self.composition)
        legacy_service.source_observer = LegacyObserver()
        legacy = self._query_service(legacy_service, cutoff=cutoff, peer=PEER_ASSET)
        self.assertEqual(legacy.measurement["state"], "UNAVAILABLE")
        self.assertEqual(legacy.measurement["limitations"][0], "REVISION_PINNED_READ_UNSUPPORTED")
        self.assertEqual(legacy.compare["state"], "BLOCKED")
        self.assertEqual(legacy.compare["reason"], "REVISION_PINNED_READ_UNSUPPORTED")
        live_read.assert_not_called()

        class WrongRevisionObserver:
            def describe(inner_self):
                return delegate.describe()

            def read_partition(inner_self, **kwargs):
                return live_read(**kwargs)

            def read_partition_revision(inner_self, **kwargs):
                batch = delegate.read_partition_revision(**kwargs)
                return replace(batch, source_revision="synthetic-wrong-source-revision")

        class WrongBindingObserver(WrongRevisionObserver):
            def read_partition_revision(inner_self, **kwargs):
                batch = delegate.read_partition_revision(**kwargs)
                return replace(batch, binding=replace(batch.binding, mapping_version="synthetic-wrong-mapping"))

        for observer, expected in (
            (WrongRevisionObserver(), "REVISION_PINNED_SOURCE_REVISION_MISMATCH"),
            (WrongBindingObserver(), "REVISION_PINNED_SOURCE_BINDING_MISMATCH"),
        ):
            with self.subTest(expected=expected):
                service = self._service(self.composition)
                service.source_observer = observer
                result = self._query_service(service, cutoff=cutoff)
                self.assertEqual(result.measurement["state"], "UNAVAILABLE")
                self.assertEqual(result.measurement["limitations"][0], expected)
                self.assertEqual(result.source["snapshot_id"], self.seed["source_snapshot_id"])
        live_read.assert_not_called()

    def test_compare_blocks_when_exact_revision_has_no_compatible_qualification(self):
        observer = self.composition.source_observer
        revision = self.seed["source_snapshot_id"]
        snapshot = self.composition.adapter.source_store().get_snapshot(self.principal, self.scope, revision)
        key = (snapshot.source_partition, snapshot.source_revision)
        prior = SyntheticAssetObserver._revision_overrides.get(key)
        self.addCleanup(
            lambda: SyntheticAssetObserver._revision_overrides.pop(key, None)
            if prior is None else SyntheticAssetObserver._revision_overrides.__setitem__(key, prior)
        )
        SyntheticAssetObserver._revision_overrides[key] = tuple(
            replace(item, comparable_population_id=None) if item.asset_id in {PRIMARY_ASSET, PEER_ASSET} else item
            for item in observer._observations
        )
        result = self._query(cutoff=datetime.now(UTC) + timedelta(seconds=5), peer=PEER_ASSET)
        self.assertEqual(result.compare["state"], "BLOCKED")
        self.assertEqual(result.compare["reason"], "REVISION_PINNED_OBSERVATION_BINDING_INVALID")

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
