"""CHG-126 real PostgreSQL worker lease/fencing/effect evidence."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sys
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_LEASE_DURATION,
    EffectIdempotencyConflictError,
    InvalidTransitionError,
    JobSemanticConflictError,
    NoEligibleJobError,
    Principal,
    StaleLeaseError,
    WorkerJobPort,
    WorkerLeaseConfig,
)
from ephi.infrastructure import (  # noqa: E402
    PostgreSQLReferenceTransactionAdapter,
    PostgreSQLWorkerStore,
)


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


class WorkerContractTests(unittest.TestCase):
    def test_defaults_and_test_overrides_are_explicit(self):
        self.assertEqual(DEFAULT_LEASE_DURATION, timedelta(seconds=120))
        self.assertEqual(DEFAULT_HEARTBEAT_INTERVAL, timedelta(seconds=30))
        config = WorkerLeaseConfig(timedelta(milliseconds=20), timedelta(milliseconds=5))
        self.assertEqual(config.lease_duration, timedelta(milliseconds=20))
        self.assertEqual(config.heartbeat_interval, timedelta(milliseconds=5))


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLWorkerTests(unittest.TestCase):
    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.adapter.connection.execute(
            "TRUNCATE applied_effect, job, outbox_event, audit_event, command_receipt, aggregate_state"
        )
        self.scope = AccessScope("worker-scope", site_id="site-1", area_id="area-1", family_id="family-1")
        self.adapter.seed_aggregate(self.scope, "fixture", "aggregate-1", {"effect_count": 0, "seed": "worker"})
        self.config = WorkerLeaseConfig(timedelta(milliseconds=300), timedelta(milliseconds=50))
        self.worker = PostgreSQLWorkerStore(self.adapter, config=self.config)
        self.assertIsInstance(self.worker, WorkerJobPort)

    def _store(self, config=None):
        adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(adapter.close)
        return adapter, PostgreSQLWorkerStore(adapter, config=config or self.config)

    def _enqueue(self, semantic_key="job-1", **kwargs):
        return self.worker.enqueue(self.scope, "FixtureJob", semantic_key, {"value": 1}, **kwargs)

    def _expire(self):
        time.sleep(self.config.lease_duration.total_seconds() + 0.15)

    def test_migration_is_idempotent_and_has_only_narrow_worker_tables(self):
        self.adapter.apply_migrations()
        names = {
            row["table_name"]
            for row in self.adapter.connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name IN ('job', 'applied_effect')"
            ).fetchall()
        }
        self.assertEqual(names, {"job", "applied_effect"})

    def test_enqueue_is_idempotent_by_semantic_key_and_conflicts_without_mutation(self):
        first = self._enqueue("same-key")
        replay = self.worker.enqueue(self.scope, "FixtureJob", "same-key", {"value": 1})
        self.assertEqual(replay, first)
        with self.assertRaises(JobSemanticConflictError):
            self.worker.enqueue(self.scope, "FixtureJob", "same-key", {"value": 2})
        with self.assertRaises(JobSemanticConflictError):
            self.worker.enqueue(self.scope, "OtherJob", "same-key", {"value": 1})
        self.assertEqual(len(self.worker.inspect(self.scope)), 1)

    def test_concurrent_enqueue_race_reconciles_one_logical_job(self):
        first, first_worker = self._store()
        second, second_worker = self._store()
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def attempt(worker):
            try:
                barrier.wait(timeout=10)
                results.append(worker.enqueue(self.scope, "FixtureJob", "raced-key", {"value": 1}))
            except Exception as exc:  # pragma: no cover - assertion reports race failures
                failures.append(exc)

        threads = [threading.Thread(target=attempt, args=(worker,)) for worker in (first_worker, second_worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(failures, failures)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.worker.inspect(self.scope)), 1)

    def test_claim_heartbeat_complete_and_inspect_use_fenced_short_transactions(self):
        self._enqueue()
        claimed = self.worker.claim(self.scope, "worker-a")
        self.assertEqual(claimed.status, "RUNNING")
        self.assertEqual(claimed.lease_epoch, 1)
        renewed = self.worker.heartbeat(claimed.lease)
        self.assertGreater(renewed.lease_expires_at, claimed.lease_expires_at)
        completed = self.worker.complete(renewed.lease)
        self.assertEqual(completed.status, "SUCCEEDED")
        self.assertIsNone(completed.lease_owner)
        self.assertEqual(self.worker.inspect(self.scope, statuses=("SUCCEEDED",))[0].job_id, claimed.job_id)
        with self.assertRaises(InvalidTransitionError):
            self.worker.complete(renewed.lease)

    def test_bounded_retry_and_terminal_failure_metadata(self):
        first = self._enqueue("retry", max_attempts=2)
        claimed = self.worker.claim(self.scope, "worker-a")
        retry = self.worker.fail(
            claimed.lease,
            retryable=True,
            error_code="TEMPORARY",
            error_message="fixture retry",
            next_available_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        self.assertEqual(retry.status, "QUEUED")
        self.assertEqual(retry.attempts, 1)
        self.assertEqual(retry.last_failure_code, "TEMPORARY")
        claimed_again = self.worker.claim(self.scope, "worker-b")
        dead = self.worker.fail(
            claimed_again.lease,
            retryable=True,
            error_code="TEMPORARY",
            error_message="fixture exhausted",
            next_available_at=datetime.now(timezone.utc),
        )
        self.assertEqual(dead.status, "DEAD_LETTER")
        self.assertEqual(dead.attempts, 2)
        self.assertEqual(dead.job_id, first.job_id)

        terminal = self._enqueue("non-retryable")
        terminal_claim = self.worker.claim(self.scope, "worker-c")
        failed = self.worker.fail(
            terminal_claim.lease,
            retryable=False,
            error_code="INVALID_FIXTURE",
            error_message="not retryable",
        )
        self.assertEqual(failed.status, "FAILED")

    def test_defer_and_cancel_are_durable_and_unclaimed_cancel_cannot_be_claimed(self):
        deferred = self._enqueue("defer")
        claimed = self.worker.claim(self.scope, "worker-a")
        available_at = datetime.now(timezone.utc) + timedelta(milliseconds=250)
        deferred_state = self.worker.defer(claimed.lease, available_at)
        self.assertEqual(deferred_state.status, "DEFERRED")
        self.assertIsNone(self.worker.claim(self.scope, "worker-b"))
        time.sleep(0.35)
        claimed_again = self.worker.claim(self.scope, "worker-b")
        self.assertEqual(claimed_again.job_id, deferred.job_id)
        self.worker.fail(
            claimed_again.lease,
            retryable=False,
            error_code="STOP",
            error_message="cleanup",
        )

        canceled = self._enqueue("cancel")
        canceled_state = self.worker.cancel(self.scope, canceled.job_id)
        self.assertEqual(canceled_state.status, "CANCELED")
        self.assertIsNone(self.worker.claim(self.scope, "worker-c"))

    def test_database_time_decides_eligibility_and_expiry(self):
        future = self._enqueue(
            "future",
            available_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.assertIsNone(self.worker.claim(self.scope, "worker-a"))
        self.assertEqual(self.worker.inspect(self.scope, job_id=future.job_id)[0].status, "QUEUED")

        current = self._enqueue("database-expiry")
        claimed = self.worker.claim(self.scope, "worker-a")
        self.adapter.connection.execute(
            "UPDATE job SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE job_id = %s",
            (claimed.job_id,),
        )
        with self.assertRaises(StaleLeaseError):
            self.worker.heartbeat(claimed.lease)
        takeover = self.worker.claim(self.scope, "worker-b")
        self.assertEqual(takeover.job_id, current.job_id)
        self.assertEqual(takeover.lease_epoch, claimed.lease_epoch + 1)

    def test_overlapping_single_claim_has_one_winner_on_separate_connections(self):
        queued = self._enqueue("one-raced-job")
        adapters_workers = [self._store()[1] for _ in range(2)]
        barrier = threading.Barrier(2)
        claims = []
        failures = []

        def attempt(worker, owner):
            try:
                barrier.wait(timeout=10)
                claims.append(worker.claim(self.scope, owner))
            except Exception as exc:  # pragma: no cover - assertion reports race failures
                failures.append(exc)

        threads = [
            threading.Thread(target=attempt, args=(adapters_workers[0], "race-a")),
            threading.Thread(target=attempt, args=(adapters_workers[1], "race-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(failures, failures)
        winners = [claim for claim in claims if claim is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].job_id, queued.job_id)
        self.assertEqual(winners[0].lease_epoch, 1)

    def test_claim_skips_a_row_locked_by_another_database_transaction(self):
        queued = self._enqueue("held-row")
        holder_adapter, _holder_worker = self._store()
        claimant_adapter, claimant = self._store()
        holder_adapter.connection.execute("BEGIN")
        holder_adapter.connection.execute("SELECT job_id FROM job WHERE job_id = %s FOR UPDATE", (queued.job_id,))
        try:
            # No Python lock coordinates this call; PostgreSQL owns the row
            # lock and the claim's SKIP LOCKED candidate selection.
            self.assertIsNone(claimant.claim(self.scope, "skip-locked-owner"))
        finally:
            holder_adapter.connection.rollback()
        self.assertEqual(claimant.claim(self.scope, "skip-locked-owner").job_id, queued.job_id)

    def test_overlapping_multiple_claims_distribute_without_duplicate_ownership(self):
        for index in range(8):
            self._enqueue(f"distributed-{index}", priority=index % 3)
        worker_pairs = [self._store() for _ in range(4)]
        barrier = threading.Barrier(4)
        claims = []
        failures = []

        def drain(worker, owner):
            try:
                barrier.wait(timeout=10)
                for _ in range(8):
                    claim = worker.claim(self.scope, owner)
                    if claim is not None:
                        claims.append((claim.job_id, claim.lease_owner, claim.lease_epoch))
                    else:
                        time.sleep(0.01)
            except Exception as exc:  # pragma: no cover - assertion reports race failures
                failures.append(exc)

        threads = [
            threading.Thread(target=drain, args=(worker, f"distributed-owner-{index}"))
            for index, (_adapter, worker) in enumerate(worker_pairs)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(failures, failures)
        self.assertEqual(len(claims), 8)
        self.assertEqual(len({job_id for job_id, _owner, _epoch in claims}), 8)
        self.assertTrue(all(epoch == 1 for _job_id, _owner, epoch in claims))

    def test_takeover_fences_old_heartbeat_complete_and_local_effect(self):
        queued = self._enqueue("takeover")
        first = self.worker.claim(self.scope, "worker-a")
        self._expire()
        second_adapter, second_worker = self._store()
        second = second_worker.claim(self.scope, "worker-b")
        self.assertEqual(second.job_id, queued.job_id)
        self.assertEqual(second.lease_epoch, first.lease_epoch + 1)
        for action in (
            lambda: self.worker.heartbeat(first.lease),
            lambda: self.worker.complete(first.lease),
            lambda: self.worker.commit_local_effect(
                first.lease, "effect-1", {"input": 1}, aggregate_type="fixture", aggregate_id="aggregate-1"
            ),
        ):
            with self.assertRaises(StaleLeaseError):
                action()
        state = self.adapter.get_aggregate(self.scope, "fixture", "aggregate-1")
        self.assertEqual(state.version, 0)
        self.assertEqual(state.state["effect_count"], 0)
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) AS count FROM applied_effect").fetchone()["count"], 0)

        receipt = second_worker.commit_local_effect(
            second.lease, "effect-1", {"input": 1}, aggregate_type="fixture", aggregate_id="aggregate-1"
        )
        self.assertEqual(receipt.committed_revision, 1)
        self.assertEqual(second_worker.complete(second.lease).status, "SUCCEEDED")

    def test_local_effect_receipt_reconciles_after_crash_without_duplicate_mutation(self):
        self._enqueue("crash-before-complete")
        first = self.worker.claim(self.scope, "worker-a")
        first_receipt = self.worker.commit_local_effect(
            first.lease, "effect-1", {"input": 1}, aggregate_type="fixture", aggregate_id="aggregate-1"
        )
        self._expire()
        _second_adapter, second_worker = self._store()
        second = second_worker.claim(self.scope, "worker-b")
        replay = second_worker.commit_local_effect(
            second.lease, "effect-1", {"input": 1}, aggregate_type="fixture", aggregate_id="aggregate-1"
        )
        self.assertEqual(replay, first_receipt)
        self.assertEqual(self.adapter.get_aggregate(self.scope, "fixture", "aggregate-1").version, 1)
        with self.assertRaises(EffectIdempotencyConflictError):
            second_worker.commit_local_effect(
                second.lease, "effect-1", {"input": 2}, aggregate_type="fixture", aggregate_id="aggregate-1"
            )
        with self.assertRaises(InvalidTransitionError):
            second_worker.cancel(self.scope, second.job_id, lease=second.lease)
        self.assertEqual(second_worker.complete(second.lease).status, "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
