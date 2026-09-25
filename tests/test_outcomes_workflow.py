"""U2.2/O7.1 synthetic outcomes and independent-review regressions."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application.context import (
    AccessScope,
    CommandContext,
    CurrentAuthorizationAuthority,
    Principal,
)
from ephi.application.errors import AuthorizationDeniedError, IdempotencyConflictError, StorageFailureError, ValidationFailureError, VersionConflictError
from ephi.application.transactions import VersionedAggregateCommandExecutor
from ephi.infrastructure.sqlite import SQLiteReferenceTransactionAdapter
from ephi.value import (
    ClaimAttribution,
    ClaimGroupIdentity,
    EventPeriod,
    EvidenceMaturity,
    OutcomesService,
    ReviewDecision,
    SupersessionConflictError,
    SupersessionError,
    ValueRevisionKind,
)
from ephi.value.repository import OutcomeAggregateRepository


UTC = timezone.utc
JAN_1 = datetime(2026, 1, 1, tzinfo=UTC)
JAN_2 = datetime(2026, 1, 2, tzinfo=UTC)
JAN_3 = datetime(2026, 1, 3, tzinfo=UTC)
JAN_4 = datetime(2026, 1, 4, tzinfo=UTC)
JAN_5 = datetime(2026, 1, 5, tzinfo=UTC)
SCOPE = AccessScope("outcomes-test", site_id="test-site", family_id="test-family")


class OutcomesWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ephi-outcomes-")
        self.adapter = SQLiteReferenceTransactionAdapter(Path(self.temp.name) / "outcomes.sqlite3")
        self.owner = Principal("claimant", ("value.submit", "value.read", "value.validate"), (SCOPE,), 1, 1)
        self.reviewer = Principal("reviewer", ("value.read", "value.validate"), (SCOPE,), 1, 1)
        self.no_read = Principal("no-read", ("value.submit",), (SCOPE,), 1, 1)
        self.current = {item.subject: item for item in (self.owner, self.reviewer, self.no_read)}
        self.authorization = CurrentAuthorizationAuthority(lambda subject: self.current[subject])
        self.now = JAN_2
        self.repo = OutcomeAggregateRepository(self.adapter)
        self.service = OutcomesService(
            self.repo,
            VersionedAggregateCommandExecutor(self.adapter, self.authorization),
            self.authorization,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.adapter.close()
        self.temp.cleanup()

    def context(self, principal=None, command_id="command", version=0):
        return CommandContext(command_id, principal or self.owner, SCOPE, version)

    def submit(self, *, event_key="event-1", category="benefit", amount="10.00", currency="USD", event_at=JAN_1, evidence=None, attribution=None, maturity=EvidenceMaturity.OBSERVED):
        return self.service.submit_claim(
            self.context(command_id=f"submit-{event_key}"),
            economic_event_key=event_key,
            category=category,
            amount=amount,
            currency=currency,
            event_at=event_at,
            owner="value-owner",
            evidence_ids=evidence or (f"evidence-{event_key}",),
            cost_model_identity="cost-model-v1",
            maturity=maturity,
            attribution=attribution or ClaimAttribution(),
            coverage_numerator=7,
            coverage_denominator=10,
        )

    def query(self, principal=None, *, cutoff=None, period=EventPeriod(JAN_1, JAN_5), currency=None, maturity=None):
        return self.service.query(
            principal or self.owner,
            SCOPE,
            event_period=period,
            knowledge_cutoff=cutoff or self.now,
            currency=currency,
            maturity=maturity,
        )

    def entries(self, result):
        return self.repo.get_group(SCOPE, result.aggregate_id).state["value_entries"]

    def test_event_identity_attribution_decimal_and_receipt_replay(self):
        attribution = ClaimAttribution(
            episode_ids=("episode-1", "episode-1", "episode-2"),
            decision_ids=("decision-1", "decision-1"),
            action_ids=("action-1", "action-1"),
            contributor_ids=("person-1", "person-1"),
        )
        context = self.context(command_id="submit-once")
        args = dict(
            economic_event_key="shared-economic-event",
            category="estimated_opportunity",
            amount=Decimal("1234.5600"),
            currency="USD",
            event_at=JAN_1,
            owner="value-owner",
            evidence_ids=("artifact-1",),
            cost_model_identity="cost-model-v1",
            maturity=EvidenceMaturity.PENDING,
            attribution=attribution,
        )
        first = self.service.submit_claim(context, **args)
        self.now = JAN_3
        replay = self.service.submit_claim(context, **args)
        self.assertEqual(first.result_identity, replay.result_identity)
        self.assertEqual(first.aggregate_version, 1)
        self.assertEqual(replay.aggregate_version, 1)
        query = self.query()
        self.assertEqual(len(query.rows), 1)
        row = query.rows[0]
        self.assertEqual(row.value.amount, Decimal("1234.5600"))
        self.assertEqual(row.value.as_dict()["amount"], "1234.5600")
        self.assertEqual(row.claim.attribution.episode_ids, ("episode-1", "episode-2"))
        self.assertEqual(row.claim.attribution.decision_ids, ("decision-1",))
        self.assertEqual(query.summaries[0].estimated_opportunity, Decimal("1234.5600"))
        self.assertEqual(query.summaries[0].claim_group_count, 1)
        stored_state = self.adapter.connection.execute(
            "SELECT state_json FROM aggregate_state WHERE aggregate_id = ?", (first.aggregate_id,),
        ).fetchone()["state_json"]
        self.assertNotIn("value_entries", stored_state)
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) FROM outcome_value_revision").fetchone()[0], 1)
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) FROM command_receipt").fetchone()[0], 1)
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0], 1)
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) FROM outbox_event").fetchone()[0], 1)
        with self.assertRaises(IdempotencyConflictError):
            self.service.submit_claim(context, **{**args, "amount": "1234.56"})
        self.assertEqual(self.adapter.connection.execute("SELECT COUNT(*) FROM outcome_value_revision").fetchone()[0], 1)
        with self.assertRaises(VersionConflictError):
            self.service.submit_claim(self.context(command_id="submit-duplicate-key"), **args)

    def test_decimal_summation_is_exact_and_value_is_not_duplicated_in_aggregate_json(self):
        self.now = JAN_5
        self.submit(event_key="exact-tenth-a", category="estimated_opportunity", amount="0.1")
        self.submit(event_key="exact-tenth-b", category="estimated_opportunity", amount="0.2")
        summary = self.query().summaries[0]
        self.assertEqual(summary.estimated_opportunity, Decimal("0.3"))
        persisted = self.adapter.connection.execute(
            "SELECT state_json FROM aggregate_state WHERE aggregate_type = 'outcome_claim_group'"
        ).fetchall()
        self.assertTrue(all("value_entries" not in row["state_json"] for row in persisted))
        self.assertEqual(
            sorted(row["amount"] for row in self.adapter.connection.execute(
                "SELECT amount FROM outcome_value_revision"
            ).fetchall()),
            ["0.1", "0.2"],
        )

    def test_value_revision_and_o2_records_roll_back_in_one_transaction(self):
        self.adapter.connection.execute(
            "CREATE TRIGGER reject_outcome_audit BEFORE INSERT ON audit_event "
            "WHEN NEW.event_type = 'SubmitOutcomeClaim' "
            "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
        )
        with self.assertRaises(StorageFailureError):
            self.submit(event_key="atomic-failure")
        self.adapter.connection.execute("DROP TRIGGER reject_outcome_audit")
        for table in ("aggregate_state", "outcome_value_revision", "command_receipt", "audit_event", "outbox_event"):
            self.assertEqual(
                self.adapter.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
                table,
            )

    def test_review_correction_and_void_have_cutoff_bound_identities_and_no_resurrection(self):
        root = self.submit(event_key="void-chain", amount="10.00")
        root_id = self.entries(root)[0]["entry_id"]
        self.now = JAN_3
        review_period = EventPeriod(JAN_1, JAN_2)
        before_review = self.query(cutoff=JAN_3, period=review_period)
        self.assertEqual(before_review.rows[0].state, "PENDING")
        review_context = self.context(self.reviewer, "approve-root-before-correction", 1)
        approved = self.service.review_value(
            review_context, group_id=root.state["group_id"], value_entry_id=root_id,
            decision=ReviewDecision.APPROVED, knowledge_cutoff=JAN_2,
            rationale="Synthetic root evidence independently reviewed.",
        )
        after_review = self.query(cutoff=JAN_3, period=review_period)
        self.assertEqual(after_review.rows[0].state, "VALIDATED")
        self.assertEqual(after_review.query_identity, before_review.query_identity)
        self.assertNotEqual(after_review.result_identity, before_review.result_identity)
        replayed_review = self.service.review_value(
            review_context, group_id=root.state["group_id"], value_entry_id=root_id,
            decision=ReviewDecision.APPROVED, knowledge_cutoff=JAN_2,
            rationale="Synthetic root evidence independently reviewed.",
        )
        self.assertEqual(replayed_review.result_identity, approved.result_identity)

        correction = self.service.record_value_revision(
            self.context(command_id="move-void-chain", version=2),
            group_id=root.state["group_id"], category="benefit", amount="12.345",
            currency="USD", event_at=JAN_3, owner="value-owner",
            evidence_ids=("moved-evidence",), cost_model_identity="cost-model-v1",
            maturity=EvidenceMaturity.OBSERVED, supersedes=root_id,
        )
        correction_id = self.entries(correction)[-1]["entry_id"]
        new_period = EventPeriod(JAN_3, JAN_4)
        self.now = JAN_4
        before_void = self.query(cutoff=JAN_4, period=new_period)
        self.assertEqual(before_void.rows[0].value.amount, Decimal("12.345"))
        self.assertEqual(before_void.rows[0].state, "PENDING")
        void_context = self.context(command_id="void-moved-value", version=3)
        voided = self.service.void_value_revision(
            void_context, group_id=root.state["group_id"], value_entry_id=correction_id,
        )
        void_revision = self.entries(voided)[-1]
        self.assertEqual(void_revision["revision_kind"], "VOID")
        self.assertIsNone(void_revision["amount"])
        self.assertEqual(void_revision["supersedes"], correction_id)
        corrected_revision = next(item for item in self.entries(correction) if item["entry_id"] == correction_id)
        for identity_field in ("scope", "group_id", "category", "currency"):
            self.assertEqual(void_revision[identity_field], corrected_revision[identity_field])
        self.assertEqual(datetime.fromisoformat(void_revision["known_at"]), JAN_4)
        after_void = self.query(cutoff=JAN_4, period=new_period)
        self.assertEqual(after_void.query_identity, before_void.query_identity)
        self.assertNotEqual(after_void.result_identity, before_void.result_identity)
        self.assertEqual(after_void.rows[0].value.revision_kind, ValueRevisionKind.VOID)
        self.assertEqual(after_void.rows[0].state, "VOID")
        self.assertEqual(after_void.summaries[0].validated_net, Decimal("0"))
        self.assertEqual(
            self.query(cutoff=JAN_4, period=new_period, maturity="VOID").rows[0].value.entry_id,
            void_revision["entry_id"],
        )
        before_void_cutoff = self.query(cutoff=JAN_3, period=new_period)
        self.assertEqual(before_void_cutoff.rows[0].value.entry_id, correction_id)
        self.assertEqual(before_void_cutoff.rows[0].value.amount, Decimal("12.345"))
        old_period_after_void = self.query(cutoff=JAN_4, period=review_period)
        self.assertEqual(old_period_after_void.rows, ())
        self.assertTrue(old_period_after_void.restated)
        replayed_void = self.service.void_value_revision(
            void_context, group_id=root.state["group_id"], value_entry_id=correction_id,
        )
        self.assertEqual(replayed_void.result_identity, voided.result_identity)
        self.assertEqual(len(self.entries(voided)), 3)
        with self.assertRaises(ValidationFailureError):
            self.service.review_value(
                self.context(self.reviewer, "review-void-leaf", 4),
                group_id=root.state["group_id"], value_entry_id=void_revision["entry_id"],
                decision=ReviewDecision.APPROVED, knowledge_cutoff=JAN_4,
                rationale="A void revision cannot validate monetary value.",
            )

    def test_duplicate_economic_event_key_is_one_group_and_not_recreated(self):
        self.submit(event_key="event-dedup")
        with self.assertRaises(VersionConflictError):
            self.service.submit_claim(
                self.context(command_id="submit-event-dedup-again"),
                economic_event_key="event-dedup",
                category="benefit",
                amount="10.00",
                currency="USD",
                event_at=JAN_1,
                owner="value-owner",
                evidence_ids=("evidence-event-dedup",),
                cost_model_identity="cost-model-v1",
                maturity=EvidenceMaturity.OBSERVED,
                coverage_numerator=7,
                coverage_denominator=10,
            )
        identity = ClaimGroupIdentity(SCOPE.canonical_key, "event-dedup")
        self.assertEqual(len(self.repo.list_groups(SCOPE, limit=10)), 1)
        self.assertEqual(self.repo.list_groups(SCOPE, limit=10)[0].aggregate_id, identity.group_id)

    def test_period_moving_correction_and_later_approval_respect_cutoffs(self):
        first = self.submit(event_key="moving-event", amount="10.00")
        old_entry = self.entries(first)[0]["entry_id"]
        early = self.query(cutoff=JAN_2, period=EventPeriod(JAN_1, JAN_2))
        self.assertEqual(early.summaries[0].validated_net, Decimal("0"))
        self.now = JAN_3
        review_context = self.context(self.reviewer, "review-before-correction", 1)
        review = self.service.review_value(
            review_context,
            group_id=first.state["group_id"],
            value_entry_id=old_entry,
            decision=ReviewDecision.APPROVED,
            knowledge_cutoff=JAN_2,
            rationale="Evidence and cost model reviewed",
        )
        self.assertEqual(review.aggregate_version, 2)
        before_review_cutoff = self.query(cutoff=JAN_2, period=EventPeriod(JAN_1, JAN_2))
        self.assertEqual(before_review_cutoff.rows[0].state, "PENDING")
        self.assertEqual(before_review_cutoff.summaries[0].validated_net, Decimal("0"))
        after_review = self.query(cutoff=JAN_3, period=EventPeriod(JAN_1, JAN_2))
        self.assertEqual(after_review.rows[0].state, "VALIDATED")
        self.assertEqual(after_review.rows[0].review.claim_revision_id, after_review.rows[0].claim.claim_revision_id)
        self.assertEqual(after_review.summaries[0].validated_net, Decimal("10.00"))

        self.now = JAN_4
        review_replay = self.service.review_value(
            review_context,
            group_id=first.state["group_id"],
            value_entry_id=old_entry,
            decision=ReviewDecision.APPROVED,
            knowledge_cutoff=JAN_2,
            rationale="Evidence and cost model reviewed",
        )
        self.assertEqual(review_replay.result_identity, review.result_identity)
        self.assertEqual(review_replay.aggregate_version, review.aggregate_version)
        corrected = self.service.record_value_revision(
            self.context(command_id="correct-event", version=2),
            group_id=first.state["group_id"],
            category="benefit",
            amount="12.345",
            currency="USD",
            event_at=JAN_3,
            owner="value-owner",
            evidence_ids=("corrected-evidence",),
            cost_model_identity="cost-model-v1",
            maturity=EvidenceMaturity.OBSERVED,
            supersedes=old_entry,
        )
        self.assertEqual(corrected.aggregate_version, 3)
        self.now = JAN_5
        correction_replay = self.service.record_value_revision(
            self.context(command_id="correct-event", version=2),
            group_id=first.state["group_id"],
            category="benefit",
            amount="12.345",
            currency="USD",
            event_at=JAN_3,
            owner="value-owner",
            evidence_ids=("corrected-evidence",),
            cost_model_identity="cost-model-v1",
            maturity=EvidenceMaturity.OBSERVED,
            supersedes=old_entry,
        )
        self.assertEqual(correction_replay.result_identity, corrected.result_identity)
        self.assertEqual(correction_replay.aggregate_version, corrected.aggregate_version)
        old_period_after = self.query(cutoff=JAN_5, period=EventPeriod(JAN_1, JAN_2))
        self.assertEqual(old_period_after.rows, ())
        self.assertTrue(old_period_after.restated)
        new_period_after = self.query(cutoff=JAN_5, period=EventPeriod(JAN_3, JAN_4))
        self.assertEqual(new_period_after.rows[0].value.amount, Decimal("12.345"))
        self.assertEqual(new_period_after.rows[0].state, "PENDING")
        self.assertEqual(new_period_after.summaries[0].validated_net, Decimal("0"))

    def test_self_validation_revocation_and_authorization_before_read(self):
        submitted = self.submit(event_key="review-policy")
        value_id = self.entries(submitted)[0]["entry_id"]
        with self.assertRaises(AuthorizationDeniedError):
            self.service.review_value(
                self.context(self.owner, "self-review", 1),
                group_id=submitted.state["group_id"],
                value_entry_id=value_id,
                decision=ReviewDecision.APPROVED,
                knowledge_cutoff=JAN_2,
                rationale="same claimant",
            )
        with self.assertRaises(AuthorizationDeniedError):
            self.query(self.no_read)

        self.current["reviewer"] = Principal("reviewer", ("value.read",), (SCOPE,), 2, 2)
        with self.assertRaises(AuthorizationDeniedError):
            self.service.review_value(
                self.context(self.reviewer, "revoked-review", 1),
                group_id=submitted.state["group_id"],
                value_entry_id=value_id,
                decision=ReviewDecision.APPROVED,
                knowledge_cutoff=JAN_2,
                rationale="revoked reviewer",
            )
        self.assertEqual(self.query().rows[0].review, None)

    def test_zero_negative_pending_and_censored_remain_visible_states(self):
        self.now = JAN_5
        self.submit(event_key="zero", category="benefit", amount="0", maturity=EvidenceMaturity.OBSERVED)
        self.submit(event_key="negative", category="benefit", amount="-2.75", maturity=EvidenceMaturity.OBSERVED)
        self.submit(event_key="censored", category="benefit", amount="4", maturity=EvidenceMaturity.CENSORED)
        self.submit(event_key="insufficient", category="benefit", amount="5", maturity=EvidenceMaturity.INSUFFICIENT_EVIDENCE)
        rejected_claim = self.submit(event_key="rejected", category="benefit", amount="6", maturity=EvidenceMaturity.OBSERVED)
        rejected_entry = self.entries(rejected_claim)[0]["entry_id"]
        self.service.review_value(
            self.context(self.reviewer, "reject-evidence", 1),
            group_id=rejected_claim.state["group_id"],
            value_entry_id=rejected_entry,
            decision=ReviewDecision.REJECTED,
            knowledge_cutoff=JAN_5,
            rationale="Synthetic evidence did not support the submitted benefit.",
        )
        rows = {row.economic_event_key: row for row in self.query().rows}
        self.assertEqual(rows["zero"].amount_state, "ZERO")
        self.assertEqual(rows["negative"].amount_state, "NEGATIVE")
        self.assertEqual(rows["censored"].state, "CENSORED")
        self.assertEqual(rows["insufficient"].state, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(rows["rejected"].state, "REJECTED")
        self.assertEqual(self.query(maturity="ZERO").rows[0].economic_event_key, "zero")
        self.assertEqual(self.query(maturity="NEGATIVE").rows[0].economic_event_key, "negative")

    def test_currencies_are_separate_and_never_combined(self):
        self.now = JAN_5
        self.submit(event_key="usd-event", category="estimated_opportunity", amount="10", currency="USD")
        self.submit(event_key="eur-event", category="estimated_opportunity", amount="20", currency="EUR")
        query = self.query()
        self.assertEqual(query.currencies, ("EUR", "USD"))
        self.assertEqual({summary.currency: summary.estimated_opportunity for summary in query.summaries}, {"EUR": Decimal("20"), "USD": Decimal("10")})
        euro_query = self.query(currency="EUR")
        self.assertEqual(euro_query.summaries[0].estimated_opportunity, Decimal("20"))
        self.assertNotEqual(euro_query.query_identity, query.query_identity)
        self.assertEqual(self.query().result_identity, query.result_identity)
        self.assertNotEqual(self.query(maturity="NEGATIVE").query_identity, query.query_identity)
        self.assertNotEqual(self.query(cutoff=JAN_4).query_identity, query.query_identity)
        self.assertNotEqual(
            self.query(period=EventPeriod(JAN_1, JAN_4)).query_identity,
            query.query_identity,
        )
        other_scope = AccessScope("outcomes-test-other", site_id="test-site", family_id="test-family")
        scoped_reader = Principal("scoped-reader", ("value.read",), (SCOPE, other_scope), 1, 1)
        self.current[scoped_reader.subject] = scoped_reader
        other_scope_query = self.service.query(
            scoped_reader, other_scope, event_period=EventPeriod(JAN_1, JAN_5),
            knowledge_cutoff=JAN_5,
        )
        self.assertNotEqual(other_scope_query.query_identity, query.query_identity)

    def test_concurrent_successors_cannot_branch(self):
        submitted = self.submit(event_key="concurrent-event", category="estimated_opportunity", amount="1")
        self.now = JAN_3
        group_id = submitted.state["group_id"]
        entry_id = self.entries(submitted)[0]["entry_id"]
        barrier = threading.Barrier(2)
        results = []

        def correct(command_id, amount):
            barrier.wait()
            try:
                result = self.service.record_value_revision(
                    self.context(command_id=command_id, version=1),
                    group_id=group_id,
                    category="estimated_opportunity",
                    amount=amount,
                    currency="USD",
                    event_at=JAN_1,
                    owner="value-owner",
                    evidence_ids=(f"evidence-{command_id}",),
                    cost_model_identity="cost-model-v1",
                    maturity=EvidenceMaturity.PENDING,
                    supersedes=entry_id,
                )
                results.append(("ok", result.aggregate_version))
            except (SupersessionError, VersionConflictError) as exc:
                results.append(("conflict", type(exc).__name__))

        threads = [threading.Thread(target=correct, args=(f"correction-{i}", str(i))) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(item[0] == "ok" for item in results), 1)
        self.assertEqual(len(self.repo.get_group(SCOPE, group_id).state["value_entries"]), 2)


if __name__ == "__main__":
    unittest.main()
