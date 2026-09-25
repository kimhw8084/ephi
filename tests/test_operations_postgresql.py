"""Real PostgreSQL integration for the authorized CHG-252 query boundary."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    ArtifactContentIdentity,
    ArtifactService,
    MetrologySourceBinding,
    MutableCurrentAuthorizationAuthority,
    OperationsQueryService,
    Principal,
    ScopedArtifactReference,
)
from ephi.infrastructure.artifacts.filesystem import FileArtifactBlobStore  # noqa: E402
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter  # noqa: E402
from ephi.infrastructure.artifacts.catalog import PostgreSQLArtifactCatalog  # noqa: E402
from ephi.application.operations import artifact_blob_path  # noqa: E402


UTC = timezone.utc


@unittest.skipUnless(os.environ.get("EPHI_TEST_POSTGRES_DSN"), "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class OperationsPostgreSQLIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["EPHI_TEST_POSTGRES_DSN"]
        self.adapter = PostgreSQLReferenceTransactionAdapter(self.dsn)
        self.addCleanup(self._close_adapter)
        self.assertTrue(self.adapter.server_version().startswith("18."), "real PostgreSQL 18 is required")
        self.scope = AccessScope(f"operations-integration-{uuid4()}", site_id="synthetic-site", family_id="synthetic-family")
        self.principal = Principal(
            "synthetic-operations-operator",
            (
                "ephi.operations.read", "ephi.source.read", "ephi.artifact.read",
                "ephi.artifact.write", "ephi.source.ingest",
            ),
            (self.scope,), 1, 1,
        )
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.binding = MetrologySourceBinding(
            self.scope, "synthetic-source", "synthetic-provider", "synthetic-family", "synthetic-capability",
            "synthetic.adapter", "synthetic-schema.v1", "1.0.0", "a" * 64, "um",
        )
        self.artifact_directory = tempfile.TemporaryDirectory(prefix="ephi-operations-artifacts-")
        self.addCleanup(self.artifact_directory.cleanup)
        self.blob_store = FileArtifactBlobStore(Path(self.artifact_directory.name) / "blobs")

    def _close_adapter(self):
        adapter = getattr(self, "adapter", None)
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass

    def _query(self, adapter=None):
        current = adapter or self.adapter
        return OperationsQueryService(
            current_authorization=self.authorization,
            postgres=current,
            worker_jobs=current.worker_store(),
            source_repository=current.source_store(),
            source_binding=self.binding,
            artifact_service=ArtifactService(self.blob_store, current.artifact_catalog(), self.authorization),
        )

    def _job(self, worker, name, *, max_attempts=3, job_type=None, priority=0):
        return worker.enqueue(
            self.scope,
            job_type or f"SyntheticOperationsFixture.{name}",
            f"operations-fixture-{name}-{uuid4()}",
            {"fixture": "synthetic", "private_source_row": "SAMPLE-ROW-MUST-NOT-LEAK"},
            priority=priority,
            max_attempts=max_attempts,
        )

    def test_mixed_axis_artifact_integrity_worker_detail_and_restart_stability(self):
        worker = self.adapter.worker_store()
        running = self._job(worker, "running")
        running = worker.claim(self.scope, "synthetic-worker-owner", job_type="SyntheticOperationsFixture.running")
        self.assertIsNotNone(running)
        self.adapter.seed_aggregate(self.scope, "operations_fixture", "committed-local-effect", {"effect_count": 0})
        worker.commit_local_effect(
            running.lease,
            "synthetic-local-effect",
            {"fixture": "private effect payload is not disclosed"},
            aggregate_type="operations_fixture",
            aggregate_id="committed-local-effect",
            mutation=lambda current, _payload: {**current, "committed": True},
        )
        deferred = self._job(worker, "deferred")
        deferred_claim = worker.claim(self.scope, "synthetic-defer-owner", job_type="SyntheticOperationsFixture.deferred")
        self.assertEqual(deferred_claim.job_id, deferred.job_id)
        worker.defer(deferred_claim.lease, datetime.now(UTC) + timedelta(hours=2))
        failed = self._job(worker, "failed")
        failed_claim = worker.claim(self.scope, "synthetic-fail-owner", job_type="SyntheticOperationsFixture.failed")
        self.assertEqual(failed_claim.job_id, failed.job_id)
        worker.fail(
            failed_claim.lease,
            retryable=False,
            error_code="SYNTHETIC_FAILURE",
            error_message="serial=SAMPLE-ROW-MUST-NOT-LEAK password=fixture-secret /private/customer/source.csv",
        )
        dead = self._job(worker, "dead-letter", max_attempts=1)
        dead_claim = worker.claim(self.scope, "synthetic-dead-owner", job_type="SyntheticOperationsFixture.dead-letter")
        self.assertEqual(dead_claim.job_id, dead.job_id)
        worker.fail(
            dead_claim.lease,
            retryable=True,
            error_code="SYNTHETIC_RETRY_EXHAUSTED",
            error_message="fixture dead-letter failure",
        )
        succeeded = self._job(
            worker, "succeeded", job_type="SyntheticOperationsFixture.succeeded", priority=1,
        )
        succeeded_high = self._job(
            worker, "succeeded-high", job_type="SyntheticOperationsFixture.succeeded", priority=8,
        )
        succeeded_claim = worker.claim(self.scope, "synthetic-success-owner", job_type="SyntheticOperationsFixture.succeeded")
        self.assertEqual(succeeded_claim.job_id, succeeded_high.job_id)
        worker.complete(succeeded_claim.lease)
        succeeded_claim = worker.claim(self.scope, "synthetic-success-owner", job_type="SyntheticOperationsFixture.succeeded")
        self.assertEqual(succeeded_claim.job_id, succeeded.job_id)
        worker.complete(succeeded_claim.lease)
        canceled = self._job(worker, "canceled")
        self.assertEqual(worker.cancel(self.scope, canceled.job_id).status, "CANCELED")
        expired = self._job(worker, "expired")
        expired_claim = worker.claim(self.scope, "synthetic-expired-owner", job_type="SyntheticOperationsFixture.expired")
        self.assertEqual(expired_claim.job_id, expired.job_id)
        self.adapter.connection.execute(
            "UPDATE job SET lease_expires_at = clock_timestamp() - INTERVAL '1 second' WHERE job_id = %s",
            (expired.job_id,),
        )
        queued = self._job(worker, "queued")

        artifact_service = self._query().artifact_service
        artifact = artifact_service.write_and_register(
            self.principal,
            self.scope,
            b"synthetic operations artifact fixture",
            media_type="application/octet-stream",
            logical_purpose="synthetic-operations-health",
            required_write_capability="ephi.artifact.write",
        ).metadata
        reference = artifact.reference

        query = self._query()
        ready = query.read(self.principal, self.scope)
        axes = ready.health.as_dict()["axes"]
        self.assertEqual(axes["process_transport"]["state"], "READY")
        self.assertEqual(axes["postgres_readiness_durability"]["state"], "READY")
        self.assertEqual(axes["immutable_artifact_integrity"]["state"], "READY")
        self.assertEqual(axes["source_capability_freshness"]["state"], "UNAVAILABLE")
        self.assertEqual(axes["durable_worker_job_state"]["state"], "ERROR")
        self.assertEqual(axes["durable_worker_job_state"]["facts"]["dead_letter_count"], 1)
        self.assertEqual(axes["durable_worker_job_state"]["facts"]["failed_count"], 1)
        jobs = {item.job_id: item for item in ready.jobs}
        self.assertEqual(jobs[queued.job_id].status, "QUEUED")
        self.assertEqual(jobs[deferred.job_id].status, "DEFERRED")
        self.assertEqual(jobs[failed.job_id].status, "FAILED")
        self.assertEqual(jobs[dead.job_id].status, "DEAD_LETTER")
        self.assertEqual(jobs[succeeded.job_id].status, "SUCCEEDED")
        self.assertEqual(jobs[canceled.job_id].status, "CANCELED")
        self.assertEqual(jobs[expired.job_id].lease_state, "EXPIRED")
        self.assertTrue(jobs[running.job_id].committed_local_effect_receipt)
        self.assertEqual(jobs[failed.job_id].failure_reason, "Failure detail withheld; review the owning worker authority.")
        encoded = str(ready.as_dict())
        self.assertNotIn("SAMPLE-ROW-MUST-NOT-LEAK", encoded)
        self.assertNotIn("fixture-secret", encoded)
        self.assertNotIn("/private/customer", encoded)
        self.assertNotIn("private effect payload", encoded)

        filtered = query.read(
            self.principal,
            self.scope,
            worker_statuses=("SUCCEEDED",),
            worker_job_type="SyntheticOperationsFixture.succeeded",
        )
        self.assertEqual([item.job_id for item in filtered.jobs], [succeeded_high.job_id, succeeded.job_id])
        self.assertEqual([item.status for item in filtered.jobs], ["SUCCEEDED", "SUCCEEDED"])
        self.assertEqual(
            filtered.as_dict()["worker_filters"],
            {"statuses": ["SUCCEEDED"], "job_type": "SyntheticOperationsFixture.succeeded"},
        )
        filtered_axis = filtered.health.as_dict()["axes"]["durable_worker_job_state"]
        self.assertEqual(filtered_axis["state"], "ERROR")
        self.assertEqual(filtered_axis["facts"]["failed_count"], 1)
        self.assertEqual(filtered_axis["facts"]["dead_letter_count"], 1)

        missing_path = artifact_blob_path(self.artifact_directory.name + "/blobs", reference.sha256)
        missing_path.unlink()
        missing = query.read(self.principal, self.scope)
        missing_axes = missing.health.as_dict()["axes"]
        self.assertEqual(missing_axes["immutable_artifact_integrity"]["state"], "ERROR")
        for axis_name in set(axes) - {"immutable_artifact_integrity"}:
            self.assertEqual(missing_axes[axis_name], axes[axis_name])
        self.assertEqual(missing.artifacts.missing_count, 1)

        missing_path.parent.mkdir(parents=True, exist_ok=True)
        missing_path.write_bytes(b"corrupt synthetic fixture bytes")
        corrupt = query.read(self.principal, self.scope)
        self.assertEqual(corrupt.artifacts.corrupt_count, 1)

        expected_jobs = [item.as_dict() for item in corrupt.jobs]
        expected_filtered_jobs = [item.as_dict() for item in filtered.jobs]
        restart_data_dir = os.environ.get("EPHI_TEST_POSTGRES_RESTART_DATA_DIR")
        pg_ctl = os.environ.get("EPHI_TEST_POSTGRES_PG_CTL")
        if restart_data_dir or pg_ctl:
            self.assertTrue(restart_data_dir and pg_ctl, "both explicit PostgreSQL restart settings are required")
            self.assertTrue(Path(restart_data_dir).is_dir(), "explicit PostgreSQL test data directory is unavailable")
            self.adapter.close()
            restart = subprocess.run(
                [pg_ctl, "-D", restart_data_dir, "-m", "fast", "-w", "-t", "30", "restart"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
            self.assertEqual(restart.returncode, 0, "isolated PostgreSQL 18 test server restart failed")
        else:
            # The ordinary DSN-gated run still checks deterministic results
            # across a fresh adapter/connection when no local pg_ctl is bound.
            self.adapter.close()
        self.adapter = PostgreSQLReferenceTransactionAdapter(self.dsn)
        restarted = self._query(self.adapter).read(self.principal, self.scope)
        self.assertEqual(
            expected_jobs,
            [item.as_dict() for item in restarted.jobs],
        )
        restarted_filtered = self._query(self.adapter).read(
            self.principal,
            self.scope,
            worker_statuses=("SUCCEEDED",),
            worker_job_type="SyntheticOperationsFixture.succeeded",
        )
        self.assertEqual(expected_filtered_jobs, [item.as_dict() for item in restarted_filtered.jobs])
        restarted_axes = restarted.health.as_dict()["axes"]
        self.assertEqual(restarted_axes["postgres_readiness_durability"]["state"], "READY")
        self.assertEqual(restarted_axes["durable_worker_job_state"]["state"], "ERROR")
        self.assertEqual(restarted_filtered.health.as_dict()["axes"]["durable_worker_job_state"]["state"], "ERROR")
        self.assertEqual(restarted.jobs_truncated, False)


if __name__ == "__main__":
    unittest.main()
