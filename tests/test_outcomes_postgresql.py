"""CHG-205 PostgreSQL restart, identity and concurrent outcome evidence."""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application.context import AccessScope, CommandContext, CurrentAuthorizationAuthority, Principal  # noqa: E402
from ephi.application.errors import VersionConflictError  # noqa: E402
from ephi.application.transactions import VersionedAggregateCommandExecutor  # noqa: E402
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter  # noqa: E402
from ephi.value import ClaimAttribution, EvidenceMaturity, OutcomesService  # noqa: E402
from ephi.value.repository import OutcomeAggregateRepository  # noqa: E402


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()
UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
MID = datetime(2026, 1, 2, tzinfo=UTC)
LATE = datetime(2026, 1, 3, tzinfo=UTC)
SCOPE = AccessScope("outcomes-postgres", site_id="site-1", family_id="family-1")


class _BarrierUnitOfWork:
    def __init__(self, transaction, barrier):
        self._transaction = transaction
        self._barrier = barrier

    def __getattr__(self, name):
        return getattr(self._transaction, name)

    def get_aggregate(self, scope_key, aggregate_type, aggregate_id, *, for_update=False):
        result = self._transaction.get_aggregate(scope_key, aggregate_type, aggregate_id, for_update=for_update)
        if aggregate_type == "outcome_claim_group" and result is None:
            self._barrier.wait(timeout=15)
        return result


class _BarrierCommandStore:
    def __init__(self, store, barrier):
        self.store = store
        self.barrier = barrier

    @contextmanager
    def command_transaction(self):
        with self.store.command_transaction() as transaction:
            yield _BarrierUnitOfWork(transaction, self.barrier)


class _BarrierRepository:
    def __init__(self, repository, barrier):
        self.repository = repository
        self.barrier = barrier

    def __getattr__(self, name):
        return getattr(self.repository, name)

    def get_group(self, scope, group_id):
        result = self.repository.get_group(scope, group_id)
        self.barrier.wait(timeout=15)
        return result


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLOutcomesTests(unittest.TestCase):
    def setUp(self):
        self.store = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.store.close)
        self.store.connection.execute("TRUNCATE outbox_event, audit_event, command_receipt, aggregate_state")
        self.owner = Principal("outcome-claimant", ("value.submit", "value.read"), (SCOPE,), 1, 1)
        self.reviewer = Principal("outcome-reviewer", ("value.validate", "value.read"), (SCOPE,), 1, 1)
        self.current = {self.owner.subject: self.owner, self.reviewer.subject: self.reviewer}
        self.authorization = CurrentAuthorizationAuthority(lambda subject: self.current[subject])
        self.now = MID
        self.service = self.make_service(self.store)

    def make_service(self, store, repository=None):
        return OutcomesService(
            repository or OutcomeAggregateRepository(store),
            VersionedAggregateCommandExecutor(store, self.authorization),
            self.authorization,
            clock=lambda: self.now,
        )

    def context(self, command_id, principal=None, version=0):
        return CommandContext(command_id, principal or self.owner, SCOPE, version)

    def submit(self, service, *, command_id, event_key, category="benefit", amount="10.00", event_at=START):
        return service.submit_claim(
            self.context(command_id),
            economic_event_key=event_key,
            category=category,
            amount=amount,
            currency="USD",
            event_at=event_at,
            owner="outcome-owner",
            evidence_ids=(f"evidence-{event_key}",),
            cost_model_identity="synthetic-cost-v1",
            maturity=EvidenceMaturity.OBSERVED,
            attribution=ClaimAttribution(episode_ids=("episode-a", "episode-a", "episode-b")),
        )

    def test_postgresql_restart_preserves_exact_claim_values_reviews_and_o2_receipt(self):
        result = self.submit(self.service, command_id="pg-submit", event_key="pg-economic-event")
        value_id = result.state["value_entries"][0]["entry_id"]
        review_context = self.context("pg-review", self.reviewer, 1)
        review = self.service.review_value(
            review_context,
            group_id=result.state["group_id"],
            value_entry_id=value_id,
            decision="APPROVED",
            knowledge_cutoff=MID,
            rationale="Synthetic evidence and cost model reviewed.",
        )
        self.now = LATE
        submit_replay = self.submit(self.service, command_id="pg-submit", event_key="pg-economic-event")
        review_replay = self.service.review_value(
            review_context,
            group_id=result.state["group_id"],
            value_entry_id=value_id,
            decision="APPROVED",
            knowledge_cutoff=MID,
            rationale="Synthetic evidence and cost model reviewed.",
        )
        self.assertEqual(submit_replay.result_identity, result.result_identity)
        self.assertEqual(review_replay.result_identity, review.result_identity)
        self.assertEqual(review_replay.aggregate_version, review.aggregate_version)
        receipt = self.store.get_command_receipt(SCOPE.canonical_key, self.owner.subject, "pg-submit")
        self.assertIsNotNone(receipt)
        self.assertEqual(result.state["value_entries"][0]["amount"], "10.00")
        self.store.close()

        restarted = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(restarted.close)
        repository = OutcomeAggregateRepository(restarted)
        snapshot = repository.get_group(SCOPE, result.state["group_id"])
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.version, review.aggregate_version)
        self.assertEqual(snapshot.state["value_entries"][0]["amount"], "10.00")
        self.assertEqual(len(snapshot.state["review_revisions"]), 1)
        self.assertEqual(restarted.get_command_receipt(SCOPE.canonical_key, self.owner.subject, "pg-submit").result_identity, result.result_identity)

    def test_unique_scoped_event_identity_serializes_concurrent_first_claims(self):
        second = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)
        first_service = self.make_service(self.store)
        second_service = self.make_service(second)
        # Hold both command transactions after their empty read so the unique
        # aggregate/event identity has to arbitrate the creation race.
        first_service.commands = VersionedAggregateCommandExecutor(_BarrierCommandStore(self.store, barrier), self.authorization)
        second_service.commands = VersionedAggregateCommandExecutor(_BarrierCommandStore(second, barrier), self.authorization)
        outcomes = []

        def create(service, command_id):
            try:
                result = self.submit(service, command_id=command_id, event_key="concurrent-event")
                outcomes.append(("created", result.aggregate_version))
            except VersionConflictError:
                outcomes.append(("deduplicated", "CONFLICT"))

        threads = [threading.Thread(target=create, args=(first_service, "pg-create-a")), threading.Thread(target=create, args=(second_service, "pg-create-b"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(item[0] == "created" for item in outcomes), 1)
        self.assertEqual(len(OutcomeAggregateRepository(self.store).list_groups(SCOPE, limit=10)), 1)

    def test_concurrent_successors_commit_one_value_chain_leaf(self):
        created = self.submit(self.service, command_id="pg-root", event_key="pg-successor-event", category="estimated_opportunity")
        group_id = created.state["group_id"]
        root_id = created.state["value_entries"][0]["entry_id"]
        self.now = LATE
        second = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)
        services = [
            self.make_service(self.store, _BarrierRepository(OutcomeAggregateRepository(self.store), barrier)),
            self.make_service(second, _BarrierRepository(OutcomeAggregateRepository(second), barrier)),
        ]
        outcomes = []

        def correct(service, command_id, amount):
            try:
                result = service.record_value_revision(
                    self.context(command_id, version=1), group_id=group_id,
                    category="estimated_opportunity", amount=amount, currency="USD",
                    event_at=START, owner="outcome-owner", evidence_ids=(f"{command_id}-evidence",),
                    cost_model_identity="synthetic-cost-v1", maturity=EvidenceMaturity.PENDING,
                    supersedes=root_id,
                )
                outcomes.append(("committed", result.aggregate_version))
            except (VersionConflictError, ValueError):
                outcomes.append(("conflict", "CONFLICT"))

        threads = [threading.Thread(target=correct, args=(services[0], "pg-correction-a", "11")), threading.Thread(target=correct, args=(services[1], "pg-correction-b", "12"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(item[0] == "committed" for item in outcomes), 1)
        snapshot = OutcomeAggregateRepository(self.store).get_group(SCOPE, group_id)
        self.assertEqual(len(snapshot.state["value_entries"]), 2)
        self.assertEqual(sum(item.get("supersedes") == root_id for item in snapshot.state["value_entries"]), 1)


if __name__ == "__main__":
    unittest.main()
