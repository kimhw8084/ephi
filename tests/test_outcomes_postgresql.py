"""CHG-205 PostgreSQL restart, identity and concurrent outcome evidence."""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import sys
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application.context import AccessScope, CommandContext, CurrentAuthorizationAuthority, Principal  # noqa: E402
from ephi.application.errors import AuthorizationDeniedError, IdempotencyConflictError, ValidationFailureError, VersionConflictError  # noqa: E402
from ephi.application.transactions import VersionedAggregateCommandExecutor  # noqa: E402
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter  # noqa: E402
from ephi.value import ClaimAttribution, EventPeriod, EvidenceMaturity, OutcomesService, ReviewDecision, ValueRevisionKind  # noqa: E402
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


class _AuditFailureUnitOfWork:
    def __init__(self, transaction):
        self._transaction = transaction

    def __getattr__(self, name):
        return getattr(self._transaction, name)

    def append_audit(self, **_kwargs):
        raise RuntimeError("injected Outcomes audit failure")


class _AuditFailureStore:
    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        return getattr(self.store, name)

    @contextmanager
    def command_transaction(self):
        with self.store.command_transaction() as transaction:
            yield _AuditFailureUnitOfWork(transaction)


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLOutcomesTests(unittest.TestCase):
    def setUp(self):
        self.store = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.store.close)
        self.store.connection.execute("TRUNCATE outbox_event, audit_event, command_receipt, outcome_value_revision, aggregate_state")
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

    def entries(self, result, store=None):
        return OutcomeAggregateRepository(store or self.store).get_group(SCOPE, result.aggregate_id).state["value_entries"]

    def query(self, service=None, *, cutoff=None, event_period=EventPeriod(START, LATE)):
        return (service or self.service).query(
            self.owner, SCOPE, event_period=event_period, knowledge_cutoff=cutoff or self.now,
        )

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
        value_id = self.entries(result)[0]["entry_id"]
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
        approved_result = self.query(cutoff=LATE)
        self.assertEqual(approved_result.rows[0].state, "VALIDATED")
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
        self.assertEqual(self.entries(result)[0]["amount"], "10.00")
        stored_state = self.store.connection.execute(
            "SELECT state_json FROM aggregate_state WHERE scope_key = %s AND aggregate_id = %s",
            (SCOPE.canonical_key, result.aggregate_id),
        ).fetchone()["state_json"]
        self.assertNotIn("value_entries", stored_state)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT data_type FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'outcome_value_revision' AND column_name = 'amount'"
            ).fetchone()["data_type"],
            "numeric",
        )
        durable_amount = self.store.connection.execute(
            "SELECT amount FROM outcome_value_revision WHERE scope_key = %s AND group_id = %s AND entry_id = %s",
            (SCOPE.canonical_key, result.aggregate_id, value_id),
        ).fetchone()["amount"]
        self.assertIsInstance(durable_amount, Decimal)
        self.assertEqual(durable_amount, Decimal("10.00"))
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
        restarted_service = self.make_service(restarted)
        restarted_query = self.query(restarted_service, cutoff=LATE)
        self.assertEqual(restarted_query.query_identity, approved_result.query_identity)
        self.assertEqual(restarted_query.result_identity, approved_result.result_identity)
        self.assertEqual(self.query(restarted_service, cutoff=LATE).result_identity, approved_result.result_identity)

    def test_postgresql_numeric_round_trip_and_exact_decimal_summation(self):
        precise = "0.1000000000000000000000000000000000000001"
        result = self.submit(self.service, command_id="pg-precise", event_key="pg-precise", amount=precise)
        self.assertEqual(self.entries(result)[0]["amount"], precise)
        precise_query = self.query().rows
        self.assertEqual(next(row for row in precise_query if row.economic_event_key == "pg-precise").value.as_dict()["amount"], precise)
        self.submit(self.service, command_id="pg-tenth-a", event_key="pg-tenth-a", category="estimated_opportunity", amount="0.1")
        self.submit(self.service, command_id="pg-tenth-b", event_key="pg-tenth-b", category="estimated_opportunity", amount="0.2")
        estimated = next(item for item in self.query().summaries if item.currency == "USD").estimated_opportunity
        self.assertEqual(estimated, Decimal("0.3"))
        rows = self.store.connection.execute(
            "SELECT state_json FROM aggregate_state WHERE aggregate_type = 'outcome_claim_group'"
        ).fetchall()
        self.assertTrue(all("value_entries" not in row["state_json"] for row in rows))

    def test_receipt_payload_conflicts_and_revocation_remain_authoritative(self):
        created = self.submit(self.service, command_id="pg-receipt-owner", event_key="pg-receipt-owner")
        with self.assertRaises(IdempotencyConflictError):
            self.submit(
                self.service, command_id="pg-receipt-owner",
                event_key="pg-receipt-owner", amount="11.00",
            )
        self.current[self.owner.subject] = Principal(
            self.owner.subject, ("value.read",), (SCOPE,), 2, 2,
        )
        with self.assertRaises(AuthorizationDeniedError):
            self.submit(self.service, command_id="pg-receipt-owner", event_key="pg-receipt-owner")
        self.assertEqual(len(self.entries(created)), 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM command_receipt").fetchone()["count"], 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM outcome_value_revision").fetchone()["count"], 1)

    def test_normalized_revision_and_o2_writes_roll_back_together(self):
        failing_service = self.make_service(_AuditFailureStore(self.store))
        with self.assertRaisesRegex(RuntimeError, "injected Outcomes audit failure"):
            self.submit(failing_service, command_id="pg-atomic-failure", event_key="pg-atomic-failure")
        for table in ("aggregate_state", "outcome_value_revision", "command_receipt", "audit_event", "outbox_event"):
            self.assertEqual(
                self.store.connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"],
                0,
                table,
            )

    def test_void_is_cutoff_safe_auditable_and_does_not_resurrect_predecessor(self):
        created = self.submit(self.service, command_id="pg-void-root", event_key="pg-void-event")
        value_id = self.entries(created)[0]["entry_id"]
        self.now = LATE
        void_context = self.context("pg-void", version=1)
        voided = self.service.void_value_revision(
            void_context, group_id=created.state["group_id"], value_entry_id=value_id,
        )
        void_rows = self.entries(voided)
        void_revision = next(item for item in void_rows if item["revision_kind"] == "VOID")
        self.assertEqual(void_revision["amount"], None)
        self.assertEqual(void_revision["supersedes"], value_id)
        root_revision = next(item for item in self.entries(created) if item["entry_id"] == value_id)
        for identity_field in ("scope", "group_id", "category", "currency"):
            self.assertEqual(void_revision[identity_field], root_revision[identity_field])
        self.assertEqual(datetime.fromisoformat(void_revision["known_at"]), LATE)
        before = self.query(cutoff=MID)
        self.assertEqual(before.rows[0].value.entry_id, value_id)
        self.assertEqual(before.rows[0].value.amount, Decimal("10.00"))
        after = self.query(cutoff=LATE)
        self.assertEqual(len(after.rows), 1)
        self.assertEqual(after.rows[0].value.revision_kind, ValueRevisionKind.VOID)
        self.assertEqual(after.rows[0].value.amount, None)
        self.assertEqual(after.rows[0].state, "VOID")
        self.assertEqual(after.summaries[0].validated_net, Decimal("0"))
        self.assertEqual(after.rows[0].value.supersedes, value_id)
        with self.assertRaises(ValidationFailureError):
            self.service.review_value(
                self.context("pg-review-void", self.reviewer, 2),
                group_id=created.state["group_id"], value_entry_id=void_revision["entry_id"],
                decision=ReviewDecision.APPROVED, knowledge_cutoff=LATE,
                rationale="A void has no reviewable monetary value.",
            )
        with self.assertRaisesRegex(Exception, "outcome value revisions are append-only"):
            self.store.connection.execute(
                "UPDATE outcome_value_revision SET maturity = maturity WHERE scope_key = %s AND group_id = %s",
                (SCOPE.canonical_key, created.aggregate_id),
            )

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
        root_id = self.entries(created)[0]["entry_id"]
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
