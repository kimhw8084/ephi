"""CHG-189 durable RCA materialization and exact-view restart evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    ArtifactService,
    CoherentReadConflictError,
    CurrentAuthorizationAuthority,
    CohortEligibility,
    CohortRole,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RCA_MATERIALIZATION_WRITE_CAPABILITY,
    RCA_READ_CAPABILITY,
    RCA_SCHEMA_IDENTITY,
    RcaAnalysisService,
    RcaCohort,
    RcaCurrentFacts,
    RcaDataset,
    RcaEvidenceFact,
    RcaMaterializationCoordinator,
    RcaMaterializationState,
    RcaQuery,
    RcaState,
)
from ephi.infrastructure import (  # noqa: E402
    FileArtifactBlobStore,
    PostgreSQLArtifactCatalog,
    PostgreSQLReferenceTransactionAdapter,
)


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()
UTC = timezone.utc


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLRcaMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(lambda: self.adapter.close())
        self.assertTrue(self.adapter.server_version().startswith("18."), self.adapter.server_version())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scope = AccessScope(f"chg189-rca-{uuid4().hex}", site_id="synthetic-site", family_id="synthetic-cd")
        self.principal = Principal(
            "synthetic-rca-engineer",
            (RCA_READ_CAPABILITY, RCA_MATERIALIZATION_WRITE_CAPABILITY),
            (self.scope,), 1, 1,
        )
        self.authorization: CurrentAuthorizationAuthority = MutableCurrentAuthorizationAuthority(self.principal)
        self.analysis = RcaAnalysisService(self.authorization)
        self.query, self.facts = self._large_input()
        self.blob_root = Path(self.temp.name) / "blobs"
        self.artifacts = ArtifactService(
            FileArtifactBlobStore(self.blob_root, max_artifact_size=16_000_000),
            PostgreSQLArtifactCatalog(self.adapter),
            self.authorization,
        )
        self.coordinator = self._coordinator(self.adapter, self.artifacts)

    def _large_input(self):
        cutoff = datetime.now(UTC).replace(microsecond=0)
        episode = f"episode-{uuid4().hex}"
        revision = f"revision-{uuid4().hex}"
        cycle = f"cycle-{uuid4().hex}"
        source = "synthetic-source-snapshot"
        affected = RcaCohort(
            "affected", CohortRole.AFFECTED, CohortEligibility.QUALIFIED, "recipe-r47", "mean-cd", "nm",
            cutoff - timedelta(minutes=30), cutoff, source, "affected-qualification",
            ("context", "recipe"), ("context", "recipe"), (), (),
        )
        controls = RcaCohort(
            "peer-controls", CohortRole.CONTROL, CohortEligibility.QUALIFIED, "recipe-r47", "mean-cd", "nm",
            cutoff - timedelta(minutes=30), cutoff, source, "control-qualification",
            ("context", "recipe"), ("context", "recipe"), (), (),
        )
        evidence = tuple(
            RcaEvidenceFact(
                f"evidence-{index}", "affected", f"sample-{index}", f"run-{index}", source,
                cutoff - timedelta(minutes=25), cutoff - timedelta(minutes=20), "recipe-r47", "mean-cd", "nm",
                ("head-trajectory",) if index % 5 == 0 else (),
            )
            for index in range(501)
        ) + tuple(
            RcaEvidenceFact(
                f"control-evidence-{index}", "peer-controls", f"control-sample-{index}", f"control-run-{index}", source,
                cutoff - timedelta(minutes=25), cutoff - timedelta(minutes=20), "recipe-r47", "mean-cd", "nm", (),
            )
            for index in range(3)
        )
        dataset = RcaDataset(
            RCA_SCHEMA_IDENTITY, "synthetic-policy-v1", cutoff, cutoff - timedelta(minutes=28),
            "affected", (affected, controls), evidence,
            limitation_codes=("SYNTHETIC_DEMONSTRATION",),
        )
        query = RcaQuery(
            self.scope, episode, revision, 7, cycle, cutoff, (source,), dataset.policy_identity,
            dataset.schema_identity,
        )
        facts = RcaCurrentFacts(
            episode, revision, 7, cycle, cutoff, (source,), dataset.policy_identity,
            dataset.schema_identity, dataset,
        )
        return query, facts

    def _coordinator(self, adapter, artifacts):
        return RcaMaterializationCoordinator(
            adapter.worker_store(), adapter, artifacts, self.authorization, self.analysis,
        )

    def test_large_rca_materializes_and_survives_worker_and_adapter_restart(self):
        bounded = self.analysis.analyze(
            self.principal, self.query, load_current_facts=lambda: self.facts,
        )
        self.assertEqual(bounded.state, RcaState.MATERIALIZATION_REQUIRED)
        queued = self.coordinator.enqueue(self.principal, self.query)
        self.assertEqual(queued.state, RcaMaterializationState.PENDING)

        self.adapter.close()
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.assertTrue(self.adapter.server_version().startswith("18."), self.adapter.server_version())
        self.artifacts = ArtifactService(
            FileArtifactBlobStore(self.blob_root, max_artifact_size=16_000_000),
            PostgreSQLArtifactCatalog(self.adapter), self.authorization,
        )
        self.coordinator = self._coordinator(self.adapter, self.artifacts)
        after_restart = self.coordinator.status(self.principal, self.query)
        self.assertEqual(after_restart.state, RcaMaterializationState.PENDING)

        completed = self.coordinator.process_one(
            self.principal, self.scope, "synthetic-rca-worker",
            load_current_facts=lambda _principal, _query: self.facts,
        )
        self.assertEqual(completed.state, RcaMaterializationState.READY)
        result = self.coordinator.read_result(
            self.principal, self.query, load_current_facts=lambda: self.facts,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.analytical_revision_identity, self.query.analytical_revision_identity)
        self.assertEqual(result.included_evidence_count, 504)
        self.assertEqual(result.state, RcaState.READY)

        self.adapter.close()
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.artifacts = ArtifactService(
            FileArtifactBlobStore(self.blob_root, max_artifact_size=16_000_000),
            PostgreSQLArtifactCatalog(self.adapter), self.authorization,
        )
        self.coordinator = self._coordinator(self.adapter, self.artifacts)
        reproduced = self.coordinator.read_result(
            self.principal, self.query, load_current_facts=lambda: self.facts,
        )
        self.assertEqual(reproduced.result_identity, result.result_identity)

    def test_stale_revision_cannot_publish_materialized_result(self):
        stale_query = replace(self.query, analytical_revision_identity=f"stale-{uuid4().hex}")
        # Rebuild matching facts then force a different current revision at run time.
        stale_facts = replace(self.facts, analytical_revision_identity=stale_query.analytical_revision_identity)
        self.coordinator.enqueue(self.principal, stale_query)

        def advanced(_principal, _query):
            return replace(stale_facts, analytical_revision_identity="newer-revision")

        view = self.coordinator.process_one(
            self.principal, self.scope, "synthetic-rca-worker-stale", load_current_facts=advanced,
        )
        self.assertEqual(view.state, RcaMaterializationState.STALE)
        self.assertIsNone(self.coordinator.read_result(
            self.principal, stale_query, load_current_facts=lambda: advanced(self.principal, stale_query),
        ))


if __name__ == "__main__":
    unittest.main()
