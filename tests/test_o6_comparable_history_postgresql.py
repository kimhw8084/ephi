"""CHG-174 O6.2 real PostgreSQL 18 restart/read-only history evidence."""

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    COMPARABLE_HISTORY_READ_CAPABILITY,
    COMPARABLE_PROFILE_KEY,
    COMPARABLE_PROFILE_SCHEMA,
    AccessScope,
    ComparableCaseHistoryQueryService,
    ComparableCaseQuery,
    ComparableQueryState,
    ExactStructuredFingerprint,
    FingerprintFeature,
    HistoricalSourceIdentity,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RevisionVector,
)
from ephi.application.storage import AggregateSnapshot  # noqa: E402
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLComparableHistoryTests(unittest.TestCase):
    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(lambda: self.adapter.close())
        self.assertTrue(self.adapter.server_version().startswith("18."), self.adapter.server_version())
        self.scope = AccessScope(f"chg174-{uuid4().hex}", site_id="site-o6", family_id="family-o6")
        self.principal = Principal(
            "chg174-history-reader",
            (COMPARABLE_HISTORY_READ_CAPABILITY,),
            (self.scope,),
            1,
            1,
        )
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.now = datetime.now(timezone.utc)
        self.current_episode_id = f"current-{uuid4().hex}"
        self.current_revision_id = f"current-revision-{uuid4().hex}"
        self.current_cycle_id = f"cycle-{uuid4().hex}"
        self.current_source = self._insert_source("current")
        self.candidate_sources = {
            "case-a": self._insert_source("case-a"),
            "case-b": self._insert_source("case-b"),
        }
        self.adapter.seed_aggregate(
            self.scope,
            "episode_workflow",
            self.current_episode_id,
            {"decision_loop": {"active_cycle_id": self.current_cycle_id}},
            version=4,
        )
        current_profile = self._profile(self.current_source, {"regime": "recipe-1", "geometry": "shape-1"})
        self.adapter.publish_current_revision(
            self.scope,
            "episode",
            self.current_episode_id,
            self.current_revision_id,
            RevisionVector("analysis-current", None, None, 4, None, "qualification-current"),
            {"episode_id": self.current_episode_id, COMPARABLE_PROFILE_KEY: current_profile},
            AggregateSnapshot(
                self.scope.canonical_key,
                "episode_workflow",
                self.current_episode_id,
                4,
                {"decision_loop": {"active_cycle_id": self.current_cycle_id}},
            ),
        )
        for episode_id in ("case-a", "case-b"):
            workflow = AggregateSnapshot(
                self.scope.canonical_key,
                "episode_workflow",
                episode_id,
                1,
                {"decision_loop": {"active_cycle_id": f"old-cycle-{episode_id}"}},
            )
            profile = self._profile(
                self.candidate_sources[episode_id],
                {"regime": "recipe-1", "geometry": "shape-1", "tool_class": "tool-2"},
            )
            self.adapter.publish_current_revision(
                self.scope,
                "episode",
                episode_id,
                f"revision-{episode_id}-{uuid4().hex}",
                RevisionVector(f"analysis-{episode_id}", None, None, 1, None, f"qualification-{episode_id}"),
                {"episode_id": episode_id, COMPARABLE_PROFILE_KEY: profile},
                workflow,
            )
        self.query = ComparableCaseQuery(
            self.scope,
            self.current_episode_id,
            self.current_cycle_id,
            4,
            self.current_revision_id,
            self.now + timedelta(hours=1),
            self.current_source,
            self._fingerprint_identity({"regime": "recipe-1", "geometry": "shape-1"}),
            "family-o6",
            "context-o6",
            10,
        )
        self.truth_before = self._episode_truth()

    def _insert_source(self, suffix):
        snapshot_id = f"snapshot-{suffix}-{uuid4().hex}"
        source_revision = f"source-revision-{suffix}-{uuid4().hex}"
        manifest_hash = _sha(f"manifest-{snapshot_id}")
        artifact_sha256 = _sha(f"artifact-{snapshot_id}")
        ingested_at = self.now - timedelta(minutes=5)
        published_at = self.now - timedelta(minutes=4)
        available_at = self.now - timedelta(hours=1)
        self.adapter.connection.execute(
            """
            INSERT INTO source_snapshot(
                snapshot_id, scope_key, source_id, provider_id, family_id, capability_id,
                adapter_id, schema_id, mapping_version, mapping_hash, unit,
                required_identifiers_json, source_partition, source_revision,
                event_start, event_end, available_cutoff,
                manifest_artifact_sha256, manifest_artifact_byte_size, manifest_artifact_object_key,
                row_count, status, manifest_hash, ingested_at, published_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                snapshot_id,
                self.scope.canonical_key,
                f"source-{suffix}-{uuid4().hex}",
                "approved-provider-fixture",
                "family-o6",
                "measurement",
                "adapter-fixture",
                "schema-fixture-v1",
                "mapping-fixture-v1",
                _sha("mapping-fixture"),
                "mm",
                "[]",
                f"partition-{suffix}-{uuid4().hex}",
                source_revision,
                self.now - timedelta(days=1),
                self.now - timedelta(hours=1),
                available_at,
                artifact_sha256,
                512,
                f"sha256/{artifact_sha256}",
                2,
                "PUBLISHED",
                manifest_hash,
                ingested_at,
                published_at,
            ),
        )
        return HistoricalSourceIdentity(snapshot_id, source_revision, manifest_hash, artifact_sha256)

    @staticmethod
    def _fingerprint_identity(values):
        fingerprint = ExactStructuredFingerprint(
            "exact-structured.v1",
            tuple(FingerprintFeature(key, _sha(value)) for key, value in values.items()),
        )
        return fingerprint.identity

    @staticmethod
    def _profile(source, values):
        return {
            "schema": COMPARABLE_PROFILE_SCHEMA,
            "family_identity": "family-o6",
            "context_identity": "context-o6",
            "source_identity": source.as_dict(),
            "fingerprint": {
                "version": "exact-structured.v1",
                "features": [
                    {"feature_id": key, "value_sha256": _sha(value)} for key, value in values.items()
                ],
            },
            "eligibility_state": "QUALIFIED",
            "eligibility_identity": f"eligibility-{source.snapshot_id}",
            "qualification_evidence_identity": f"qualification-evidence-{source.snapshot_id}",
            "curation_state": "CURATED",
            "curation_evidence_identity": f"curation-evidence-{source.snapshot_id}",
            "data_completeness_limitations": [],
            "claims": [],
        }

    def _service(self, adapter=None):
        adapter = adapter or self.adapter
        return ComparableCaseHistoryQueryService(adapter, adapter, self.authorization)

    def _episode_truth(self):
        row = self.adapter.connection.execute(
            "SELECT version, state_json FROM aggregate_state WHERE scope_key = %s AND aggregate_type = 'episode_workflow' AND aggregate_id = %s",
            (self.scope.canonical_key, self.current_episode_id),
        ).fetchone()
        head = self.adapter.connection.execute(
            "SELECT revision_id, head_version FROM read_head WHERE scope_key = %s AND entity_type = 'episode' AND entity_id = %s",
            (self.scope.canonical_key, self.current_episode_id),
        ).fetchone()
        return (row["version"], row["state_json"], head["revision_id"], head["head_version"])

    def test_postgresql_18_restart_reproduces_bounded_history_and_preserves_episode_truth(self):
        first_service = self._service()
        first_page = first_service.retrieve(self.principal, self.query, page_size=1)
        self.assertEqual(first_page.state, ComparableQueryState.READY)
        self.assertEqual([case.episode_id for case in first_page.cases], ["case-a"])
        self.assertIsNotNone(first_page.next_cursor)
        original_result_identity = first_page.result_identity
        original_snapshot = first_page.snapshot_id
        original_cursor = first_page.next_cursor
        original_exclusions = first_page.excluded_candidates
        self.adapter.close()

        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.assertTrue(self.adapter.server_version().startswith("18."), self.adapter.server_version())
        restarted_service = self._service(self.adapter)
        reproduced = restarted_service.retrieve(self.principal, self.query, page_size=10)
        self.assertEqual(reproduced.result_identity, original_result_identity)
        self.assertEqual([case.episode_id for case in reproduced.cases], ["case-a", "case-b"])
        self.assertEqual(reproduced.excluded_candidates, original_exclusions)
        continuation = restarted_service.retrieve(
            self.principal,
            self.query,
            page_size=1,
            snapshot_id=original_snapshot,
            cursor=original_cursor,
        )
        self.assertEqual(continuation.result_identity, original_result_identity)
        self.assertEqual([case.episode_id for case in continuation.cases], ["case-b"])
        self.assertEqual(self._episode_truth(), self.truth_before)


if __name__ == "__main__":
    unittest.main()
