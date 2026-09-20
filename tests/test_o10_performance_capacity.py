"""Dependency-light CHG-161/O10.2 benchmark contract tests."""

from __future__ import annotations

import unittest

from tools.o10_performance_capacity import (
    DatasetConfig,
    aggregate_repetitions,
    expected_fixture_counts,
    percentile,
    qualify_benchmark,
    qualify_environment,
    summarize_samples,
    validate_fixture_counts,
)


class O10PerformanceCapacityMathTests(unittest.TestCase):
    def test_nearest_rank_percentiles_are_recomputable(self) -> None:
        values = [5, 1, 9, 2, 7]
        self.assertEqual(percentile(values, 0.50), 5.0)
        self.assertEqual(percentile(values, 0.95), 9.0)
        self.assertEqual(percentile(values, 0.99), 9.0)

    def test_summary_keeps_failures_and_expected_conflicts_out_of_latency(self) -> None:
        summary = summarize_samples(
            [
                {"elapsed_ms": 10, "queue_delay_ms": 1, "scheduling_delay_ms": 1, "status": "ok"},
                {"elapsed_ms": 20, "queue_delay_ms": 2, "scheduling_delay_ms": 2, "status": "ok", "expected_conflict": True},
                {"elapsed_ms": 99, "queue_delay_ms": 3, "scheduling_delay_ms": 3, "status": "failure", "error_type": "TimeoutError"},
            ]
        )
        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["valid_latency_sample_count"], 1)
        self.assertEqual(summary["expected_conflicts"], 1)
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["latency_ms"]["p95_ms"], 10.0)

    def test_all_repetitions_are_required(self) -> None:
        result = aggregate_repetitions(
            [
                {"repetition": 1, "summary": summarize_samples([{"elapsed_ms": 1, "status": "ok"}])},
                {"repetition": 2, "summary": summarize_samples([{"elapsed_ms": 2, "status": "failure", "error_type": "x"}])},
                {"repetition": 3, "summary": summarize_samples([{"elapsed_ms": 3, "status": "ok"}])},
            ]
        )
        self.assertFalse(result["all_repetitions_valid"])

    def test_fixture_count_validation_is_exact(self) -> None:
        config = DatasetConfig()
        expected = expected_fixture_counts(config)
        self.assertEqual(validate_fixture_counts(expected, config)["status"], "PASS")
        with self.assertRaises(ValueError):
            validate_fixture_counts({**expected, "read_revision": expected["read_revision"] - 1}, config)

    def test_environment_without_measured_authority_fails_closed(self) -> None:
        decision = qualify_environment({"executor_kind": "developer-macos", "measured": False})
        self.assertEqual(decision["state"], "BLOCKED_BENCHMARK_ENVIRONMENT")

    def test_capacity_cannot_pass_when_environment_is_blocked(self) -> None:
        result = qualify_benchmark(
            {"executor_kind": "developer-macos", "measured": False},
            {},
            {"status": "PASS"},
            {"acknowledged_effects_lost_after_restart": 0},
            {},
        )
        self.assertEqual(result["state"], "BLOCKED_BENCHMARK_ENVIRONMENT")
        self.assertEqual(result["budgets"], "NOT_EVALUATED")


if __name__ == "__main__":
    unittest.main()
