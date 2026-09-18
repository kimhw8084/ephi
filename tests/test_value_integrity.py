"""Offline F04 temporal value and append-only ledger regressions."""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.value import (  # noqa: E402
    EventPeriod,
    InMemoryValueRepository,
    MixedCurrencyError,
    SupersessionConflictError,
    SupersessionError,
    UnknownPredecessorError,
    ValueEntry,
    ValueService,
    ValueValidationError,
    decimal_json_default,
)
from tools.w0_integrity_regressions import run_canonical_integrity_regressions  # noqa: E402


UTC = timezone.utc
JANUARY_1 = datetime(2026, 1, 1, tzinfo=UTC)
JANUARY_2 = datetime(2026, 1, 2, tzinfo=UTC)
JANUARY_3 = datetime(2026, 1, 3, tzinfo=UTC)
JANUARY_4 = datetime(2026, 1, 4, tzinfo=UTC)


def make_entry(
    entry_id: str,
    amount: object,
    *,
    known_at: datetime = JANUARY_1,
    event_at: datetime = JANUARY_1,
    supersedes: str | None = None,
    scope: str = "scope-1",
    group_id: str = "group-1",
    category: str = "operating_cost",
    currency: str = "USD",
) -> ValueEntry:
    return ValueEntry(
        entry_id=entry_id,
        scope=scope,
        group_id=group_id,
        category=category,
        amount=amount,  # type: ignore[arg-type]
        currency=currency,
        event_at=event_at,
        known_at=known_at,
        supersedes=supersedes,
    )


def fixture_repository(*, moved_period: bool = False) -> InMemoryValueRepository:
    original = make_entry("original", Decimal("10"))
    repository = InMemoryValueRepository([original])
    repository.append(
        make_entry(
            "correction",
            Decimal("20"),
            known_at=JANUARY_3,
            event_at=JANUARY_3 if moved_period else JANUARY_1,
            supersedes=original.entry_id,
        )
    )
    return repository


class ValueIntegrityTests(unittest.TestCase):
    def test_canonical_fixture_is_temporally_correct_at_both_cutoffs(self):
        service = ValueService(fixture_repository())
        period = EventPeriod(JANUARY_1, JANUARY_4)

        before_correction = service.aggregate(
            scope="scope-1",
            group_id="group-1",
            category="operating_cost",
            currency="USD",
            knowledge_cutoff=JANUARY_2,
            event_period=period,
        )
        after_correction = service.aggregate(
            scope="scope-1",
            group_id="group-1",
            category="operating_cost",
            currency="USD",
            knowledge_cutoff=JANUARY_4,
            event_period=period,
        )

        self.assertEqual(before_correction, Decimal("10"))
        self.assertEqual(after_correction, Decimal("20"))

    def test_period_move_is_filtered_after_cutoff_leaf_selection(self):
        service = ValueService(fixture_repository(moved_period=True))
        original_period = EventPeriod(JANUARY_1, JANUARY_2)
        correction_period = EventPeriod(JANUARY_3, JANUARY_4)

        earlier_cutoff = service.aggregate(
            scope="scope-1",
            group_id="group-1",
            category="operating_cost",
            currency="USD",
            knowledge_cutoff=JANUARY_2,
            event_period=original_period,
        )
        old_period_after_correction = service.aggregate(
            scope="scope-1",
            group_id="group-1",
            category="operating_cost",
            currency="USD",
            knowledge_cutoff=JANUARY_4,
            event_period=original_period,
        )
        new_period_after_correction = service.aggregate(
            scope="scope-1",
            group_id="group-1",
            category="operating_cost",
            currency="USD",
            knowledge_cutoff=JANUARY_4,
            event_period=correction_period,
        )

        self.assertEqual(earlier_cutoff, Decimal("10"))
        self.assertEqual(old_period_after_correction, Decimal("0"))
        self.assertEqual(new_period_after_correction, Decimal("20"))

    def test_repository_preserves_predecessor_and_rejects_unknown_predecessor(self):
        repository = InMemoryValueRepository()
        original = repository.append(make_entry("original", Decimal("10")))
        correction = make_entry("correction", Decimal("20"), known_at=JANUARY_3, supersedes="original")
        repository.append(correction)

        self.assertEqual(repository.get("original"), original)
        self.assertEqual(repository.all_entries(), (original, correction))
        with self.assertRaises(UnknownPredecessorError):
            repository.append(make_entry("unknown-successor", Decimal("1"), supersedes="missing"))

    def test_self_supersession_branch_and_cross_identity_are_rejected(self):
        repository = InMemoryValueRepository([make_entry("original", Decimal("10"))])
        with self.assertRaises(SupersessionError):
            repository.append(make_entry("self", Decimal("11"), known_at=JANUARY_2, supersedes="self"))
        repository.append(make_entry("successor", Decimal("20"), known_at=JANUARY_2, supersedes="original"))
        with self.assertRaises(SupersessionConflictError):
            repository.append(make_entry("branch", Decimal("30"), known_at=JANUARY_3, supersedes="original"))

        for field, value in (
            ("scope", "other-scope"),
            ("group_id", "other-group"),
            ("category", "other-category"),
            ("currency", "EUR"),
        ):
            with self.subTest(field=field):
                values = {field: value}
                isolated = InMemoryValueRepository([make_entry("root", Decimal("10"))])
                with self.assertRaises(SupersessionConflictError):
                    isolated.append(
                        make_entry(
                            "cross-identity",
                            Decimal("20"),
                            known_at=JANUARY_2,
                            supersedes="root",
                            **values,
                        )
                    )

    def test_forward_reference_makes_cycles_structurally_impossible(self):
        repository = InMemoryValueRepository()
        with self.assertRaises(UnknownPredecessorError):
            repository.append(make_entry("cycle-a", Decimal("10"), supersedes="cycle-b"))
        self.assertEqual(repository.all_entries(), ())

    def test_invalid_money_timestamps_identities_and_currency_fail_closed(self):
        for amount in (1.5, float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity"), "not-a-decimal"):
            with self.subTest(amount=repr(amount)):
                with self.assertRaises(ValueValidationError):
                    make_entry("invalid", amount)
        with self.assertRaises(ValueValidationError):
            make_entry("naive-time", Decimal("1"), known_at=datetime(2026, 1, 1))
        with self.assertRaises(ValueValidationError):
            make_entry("malformed-time", Decimal("1"), known_at="2026-01-01T00:00:00Z")  # type: ignore[arg-type]
        with self.assertRaises(ValueValidationError):
            make_entry("empty-scope", Decimal("1"), scope=" ")
        with self.assertRaises(ValueValidationError):
            make_entry("bad-currency", Decimal("1"), currency="usd")

    def test_mixed_currency_aggregation_is_rejected_without_fx_policy(self):
        repository = InMemoryValueRepository(
            [
                make_entry("usd", Decimal("10"), currency="USD"),
                make_entry("eur", Decimal("20"), currency="EUR"),
            ]
        )
        with self.assertRaises(MixedCurrencyError):
            ValueService(repository).aggregate(
                scope="scope-1",
                group_id="group-1",
                category="operating_cost",
                knowledge_cutoff=JANUARY_2,
            )

    def test_json_money_is_a_decimal_string(self):
        entry = make_entry("json", Decimal("10.00"))
        payload = json.loads(entry.to_json())
        self.assertEqual(payload["amount"], "10.00")
        self.assertIsInstance(payload["amount"], str)
        self.assertEqual(json.loads(json.dumps({"amount": Decimal("10")}, default=decimal_json_default))["amount"], "10")

    def test_canonical_runner_passes_f02_f03_f04_and_leaves_f05_out_of_scope(self):
        result = run_canonical_integrity_regressions()
        findings = {item["id"]: item for item in result["findings"]}

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(findings["F02"]["status"], "PASS")
        self.assertEqual(findings["F03"]["status"], "PASS")
        self.assertEqual(findings["F04"]["status"], "PASS")
        self.assertEqual(findings["F04"]["observed"]["actual_operating_cost"], Decimal("10"))
        self.assertEqual(findings["F05"]["status"], "NOT_IMPLEMENTED")
        self.assertEqual(findings["F05"]["execution"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
