"""Dependency-light CHG-161/O10.2 benchmark contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import o10_performance_capacity as capacity
from tools.o10_performance_capacity import (
    BenchmarkEnvironmentBlocked,
    DatasetConfig,
    SAMPLE_FLOORS,
    aggregate_repetitions,
    evaluate_benchmark_acceptance,
    evaluate_session_isolation,
    expected_fixture_counts,
    main,
    percentile,
    qualify_benchmark,
    qualify_environment,
    summarize_samples,
    validate_workload_envelope,
    validate_fixture_counts,
)
from ephi.application import AccessScope, Principal, VersionedReadRow
from ephi.application.errors import StorageFailureError
from ephi.infrastructure.postgresql_reads import PostgreSQLReadSnapshotStore


def _provenance() -> dict[str, object]:
    return {"source": "fabric-attested-test-fixture", "method": "independent-resource-observation", "observed_at": "2026-09-20T12:00:00+00:00", "evidence_digest": "a" * 64}


def _resource(resource_id: str, authority_id: str, isolation: str, cpu: float, memory: float, *, limit_kind: str = "measured") -> dict[str, object]:
    return {
        "resource_id": resource_id,
        "resource_authority_id": authority_id,
        "process_isolation_domain": isolation,
        "cpu_limit_vcpu": cpu,
        "memory_limit_gib": memory,
        "limit_kind": limit_kind,
        "measurement_provenance": _provenance(),
    }


def _qualifying_environment(*, postgres_version: str = "18.4", web_cpu: float = 4, web_memory: float = 8) -> dict[str, object]:
    return {
        "executor_kind": "fabric-linux-container",
        "executor_profile": "n1-qualifying",
        "measurement_window": {"started_at": "2026-09-20T12:00:00+00:00", "ended_at": "2026-09-20T12:05:00+00:00"},
        "authority_validation": {"status": "VALIDATED", **_provenance()},
        "web": _resource("web-node-a", "authority-web", "web-cgroup", web_cpu, web_memory),
        "postgres": _resource("postgres-node-b", "authority-postgres", "postgres-vm", 4, 16),
        "worker": _resource("worker-node-c", "authority-worker", "worker-cgroup", 2, 4, limit_kind="declared"),
        "separation_proof": {"status": "PASS", "method": "distinct-attested-resource-authorities-and-isolation-domains", "distinct_resource_authorities": True, "distinct_process_isolation_domains": True},
        "client_rtt": {"p95_ms": 12, "path": "actual_browser_client_to_web", "measurement_provenance": _provenance()},
        "postgres_server_version_observed": postgres_version,
        "postgres_server_version_source": "direct_benchmark_database_query",
    }


def _full_acceptance_report() -> dict[str, object]:
    environment = _qualifying_environment()
    environment_decision = qualify_environment(environment).as_dict()
    workload = validate_workload_envelope(
        browser_sessions=100,
        family_partitions=30,
        attention_items=10_000,
        archived_read_records=1_000_000,
        attention_page_size=50,
        repetitions=3,
        duration_seconds=30,
    )
    service = {}
    for name, floor in SAMPLE_FLOORS.items():
        query_count = capacity.QUERY_COUNT_CEILINGS[name]
        samples = [{"elapsed_ms": 1, "status": "ok", "query_count": query_count} for _ in range(floor)]
        summary = summarize_samples(samples)
        service[name] = {
            "repetition_count": 3,
            "all_repetitions_valid": True,
            "repetitions": [{"repetition": index, "summary": summary, "samples": samples} for index in range(1, 4)],
            "budget": {"status": "PASS"},
            "query_count_ceiling": query_count,
        }
    browser_repetitions = []
    for repetition in range(1, 4):
        samples = [{"session": index, "status": "ok", "session_identity_fingerprint": f"{index:064x}", "console_errors": [], "page_errors": [], "failed_network_count": 0} for index in range(100)]
        browser_repetitions.append({
            "repetition": repetition,
            "status": "PASS",
            "requested_session_count": 100,
            "samples": samples,
            "attention_useful_paint_ms": {"p95_ms": 1},
            "episode_useful_paint_ms": {"p95_ms": 1},
            "attention_payload_bytes": {"max_ms": 1},
            "episode_payload_bytes": {"max_ms": 1},
            "console_or_network_failures": 0,
            "page_errors": [],
        })
    source_repetitions = [
        {
            "repetition": index,
            "status": "PASS",
            "application_operation": "AttentionQueryService.list_attention",
            "read_sample_count": 4,
            "read_failure_count": 0,
            "read_latency_summary": {"sample_count": 4},
            "observed_source_states": ["STALE", "UNAVAILABLE"],
            "healthy_or_empty_fallback": False,
        }
        for index in range(1, 4)
    ]
    worker_repetitions = [
        {
            "repetition": index,
            "status": "PASS",
            "foreground_read_sample_count": 4,
            "foreground_read_failure_count": 0,
            "accepted_command_count": 2,
            "accepted_command_failure_count": 0,
            "expired_health_state": "STALE",
            "stale_worker_effect_applied": False,
            "accepted_commands_durable": True,
        }
        for index in range(1, 4)
    ]
    resilience_repetitions = [{"repetition": index, "status": "PASS"} for index in range(1, 4)]
    restore_repetitions = [
        {
            "repetition": index,
            "status": "PASS",
            "backup_verification": "VERIFIED",
            "restore_verification": "VERIFIED",
            "reconciliation_status": "PASS",
            "application_coherence": {"status": "PASS"},
        }
        for index in range(1, 4)
    ]
    return {
        "execution_mode": "QUALIFYING_BENCHMARK",
        "environment": environment,
        "environment_decision": environment_decision,
        "workload_envelope": workload,
        "dataset": {"facts": {"counts": expected_fixture_counts(DatasetConfig())}, "fixture_validation": {"status": "PASS"}},
        "service_workloads": service,
        "browser_session_workloads": {"status": "PASS", "repetition_count": 3, "repetitions": browser_repetitions},
        "workflow_conflicts": {"status": "PASS", "typed_conflict_observed": True, "unintended_mutation_count": 0, "committed_count": 1, "expected_conflict_count": 1},
        "durability": {"exactly_once": True, "acknowledged_effects_lost_after_restart": 0, "before_restart": {"read_your_write": True}, "after_restart": {"same_result_identity": True, "read_your_write": True}},
        "resilience": {
            "source_degraded": {"status": "PASS", "synthetic_operational_resilience": True, "authentic_family_science": "NOT_CLAIMED", "foreground_load_continued": True, "observed_source_states": ["STALE", "UNAVAILABLE"], "repetition_count": 3, "repetitions": source_repetitions},
            "worker_starvation": {"status": "PASS", "repetition_count": 3, "repetitions": worker_repetitions},
            "web_crash_restart": {"status": "PASS", "repetition_count": 3, "repetitions": resilience_repetitions},
            "restore": {"status": "PASS", "repetition_count": 3, "repetitions": restore_repetitions, "scale_authority": {"mode": "approved_representative", "approved": True, "immutable": True, "authority_id": "o9-real-rehearsal-representative-v1", "fixture_identity": "o9-rehearsal-scope/episode-o9-1/read-o9-1", "benchmark_scale_counts_bound": False, "reason": "The O9 restore fixture is representative and does not bind the 10k/1M benchmark scale."}},
        },
        "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
    }


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

    def test_postgresql_18_is_direct_authority_and_17_is_blocked(self) -> None:
        self.assertEqual(qualify_environment(_qualifying_environment())["state"], "QUALIFYING_PRODUCTION_LIKE_EXECUTOR")
        decision = qualify_environment(_qualifying_environment(postgres_version="17.11"))
        self.assertEqual(decision["state"], "BLOCKED_BENCHMARK_ENVIRONMENT")
        self.assertIn("PostgreSQL server major is not 18", decision["mismatches"])

    def test_environment_requires_split_attested_resources(self) -> None:
        environment = _qualifying_environment()
        environment["web"] = {"cpu_limit_vcpu": 4, "memory_limit_gib": 8}
        decision = qualify_environment(environment)
        self.assertEqual(decision["state"], "BLOCKED_BENCHMARK_ENVIRONMENT")
        self.assertIn("web resource identity is not stable and non-secret", decision["mismatches"])

    def test_resource_comparison_accepts_conservative_but_rejects_favorable_or_unrepresentative(self) -> None:
        self.assertEqual(qualify_environment(_qualifying_environment(web_cpu=2, web_memory=4))["state"], "QUALIFYING_PRODUCTION_LIKE_EXECUTOR")
        self.assertEqual(qualify_environment(_qualifying_environment(web_cpu=5))["state"], "BLOCKED_BENCHMARK_ENVIRONMENT")
        self.assertEqual(qualify_environment(_qualifying_environment(web_cpu=0.5))["state"], "BLOCKED_BENCHMARK_ENVIRONMENT")

    def test_workload_envelope_rejects_undersized_qualification(self) -> None:
        valid = validate_workload_envelope(browser_sessions=100, family_partitions=30, attention_items=10_000, archived_read_records=1_000_000, attention_page_size=50, repetitions=3, duration_seconds=30)
        self.assertEqual(valid["status"], "PASS")
        for kwargs in (
            {"browser_sessions": 2, "family_partitions": 30, "attention_items": 10_000, "archived_read_records": 1_000_000, "attention_page_size": 50, "repetitions": 3, "duration_seconds": 30},
            {"browser_sessions": 100, "family_partitions": 29, "attention_items": 10_000, "archived_read_records": 1_000_000, "attention_page_size": 50, "repetitions": 3, "duration_seconds": 30},
            {"browser_sessions": 100, "family_partitions": 30, "attention_items": 10_000, "archived_read_records": 1_000_000, "attention_page_size": 50, "repetitions": 3, "duration_seconds": 2},
            {"browser_sessions": 100, "family_partitions": 30, "attention_items": 10_000, "archived_read_records": 1_000_000, "attention_page_size": 50, "repetitions": 2, "duration_seconds": 30},
        ):
            self.assertEqual(validate_workload_envelope(**kwargs)["status"], "FAIL")

    def test_session_isolation_requires_100_unique_successful_runtime_identities(self) -> None:
        unique = {"requested_session_count": 100, "samples": [{"status": "ok", "session_identity_fingerprint": f"{index:064x}"} for index in range(100)]}
        self.assertEqual(evaluate_session_isolation(unique)["status"], "PASS")
        missing = {"requested_session_count": 100, "samples": [{"status": "ok", "session_identity_fingerprint": f"{index:064x}"} for index in range(99)] + [{"status": "ok"}]}
        self.assertEqual(evaluate_session_isolation(missing)["status"], "FAIL")
        duplicate = {"requested_session_count": 100, "samples": [{"status": "ok", "session_identity_fingerprint": f"{index:064x}"} for index in range(99)] + [{"status": "ok", "session_identity_fingerprint": "0" * 64}]}
        self.assertEqual(evaluate_session_isolation(duplicate)["status"], "FAIL")
        undercount = {"requested_session_count": 100, "samples": [{"status": "ok", "session_identity_fingerprint": f"{index:064x}"} for index in range(2)]}
        self.assertEqual(evaluate_session_isolation(undercount)["status"], "FAIL")

    def test_acceptance_evaluator_is_fail_closed_and_blocks_conflict(self) -> None:
        report = _full_acceptance_report()
        self.assertEqual(evaluate_benchmark_acceptance(report)["status"], "PASS")
        report["workflow_conflicts"] = {"status": "FAIL", "typed_conflict_observed": False, "unintended_mutation_count": 0, "committed_count": 1, "expected_conflict_count": 0}
        decision = evaluate_benchmark_acceptance(report)
        self.assertEqual(decision["status"], "FAIL")
        self.assertIn("workflow_controlled_conflict", decision["failed"])
        self.assertEqual(evaluate_benchmark_acceptance({})["status"], "FAIL")

    def test_source_degraded_acceptance_requires_real_attention_reads_and_typed_states(self) -> None:
        report = _full_acceptance_report()
        report["resilience"]["source_degraded"]["repetitions"][0]["application_operation"] = "SELECT 1"
        decision = evaluate_benchmark_acceptance(report)
        self.assertEqual(decision["status"], "FAIL")
        self.assertIn("source_degraded_real_attention_reads", decision["failed"])

    def test_worker_starvation_acceptance_requires_concurrent_reads_commands_and_fencing(self) -> None:
        report = _full_acceptance_report()
        report["resilience"]["worker_starvation"]["repetitions"][0]["accepted_commands_durable"] = False
        decision = evaluate_benchmark_acceptance(report)
        self.assertEqual(decision["status"], "FAIL")
        self.assertIn("worker_starvation_resilience", decision["failed"])

    def test_restore_acceptance_requires_post_restore_application_coherence(self) -> None:
        report = _full_acceptance_report()
        report["resilience"]["restore"]["repetitions"][0]["application_coherence"] = {"status": "NOT_RUN"}
        decision = evaluate_benchmark_acceptance(report)
        self.assertEqual(decision["status"], "FAIL")
        self.assertIn("restore_rehearsal_and_application_coherence", decision["failed"])

    def test_query_count_acceptance_blocks_per_row_retained_snapshot_behavior(self) -> None:
        report = _full_acceptance_report()
        report["service_workloads"]["attention_warm"]["repetitions"][0]["samples"][0]["query_count"] = 55
        decision = evaluate_benchmark_acceptance(report)
        self.assertEqual(decision["status"], "FAIL")
        self.assertIn("query_count_no_n_plus_one", decision["failed"])
        report = _full_acceptance_report()
        del report["service_workloads"]["episode_brief"]["repetitions"][0]["samples"][0]["query_count"]
        decision = evaluate_benchmark_acceptance(report)
        self.assertEqual(decision["status"], "FAIL")
        self.assertIn("query_count_no_n_plus_one", decision["failed"])

    def test_unexpected_exception_is_execution_failure_and_environment_block_is_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "failure"
            with mock.patch.object(capacity, "run", side_effect=RuntimeError("fixture regression")):
                self.assertEqual(main(["--output", str(output)]), 1)
            failure = json.loads((output / "benchmark_report.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["status"], "FAIL_BENCHMARK_EXECUTION")
            self.assertEqual(failure["error_type"], "RuntimeError")
            with mock.patch.object(capacity, "run", side_effect=BenchmarkEnvironmentBlocked(qualify_environment({}))):
                self.assertEqual(main(["--output", str(output)]), 2)
            blocked = json.loads((output / "benchmark_report.json").read_text(encoding="utf-8"))
            self.assertEqual(blocked["status"], "BLOCKED_BENCHMARK_ENVIRONMENT")

    def test_retained_snapshot_batch_preserves_order_and_rolls_back_batch_failure(self) -> None:
        class Result:
            def fetchone(self):
                return {}

        class Cursor:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def executemany(self, _sql, values):
                self.connection.batches.append(list(values))
                if self.connection.fail_batch:
                    raise RuntimeError("batch failure")

        class Connection:
            def __init__(self, fail_batch=False):
                self.fail_batch = fail_batch
                self.batches = []
                self.executed_sql = []
                self.rollback_count = 0
                self.commit_count = 0

            def execute(self, sql, _params=None):
                self.executed_sql.append(sql)
                if sql == "BEGIN":
                    return Result()
                return Result()

            def cursor(self):
                return Cursor(self)

            def rollback(self):
                self.rollback_count += 1

            def commit(self):
                self.commit_count += 1

        class Adapter:
            def __init__(self, connection):
                self.connection = connection

        scope = AccessScope("batch-scope")
        principal = Principal("batch-subject", ("read",), (scope,), 1, 1)
        stored = type("Stored", (), {"public": "snapshot"})()
        for row_count in (3, 50):
            rows = tuple(VersionedReadRow(f"row-{index:02d}", index % 3, {"value": index}) for index in range(row_count))
            connection = Connection()
            store = PostgreSQLReadSnapshotStore(Adapter(connection))
            with mock.patch.object(store, "_snapshot_from_row", return_value=stored):
                self.assertEqual(store.create_query_snapshot(principal, scope, {"query": f"batch-{row_count}"}, "read", rows), "snapshot")
            self.assertEqual(len(connection.batches), 1)
            self.assertEqual(len(connection.batches[0]), row_count)
            self.assertEqual([sql for sql in connection.executed_sql if "INSERT INTO query_snapshot_row" in sql], [])
            self.assertEqual([item[1] for item in connection.batches[0]], list(range(1, row_count + 1)))
            self.assertEqual([item[2] for item in connection.batches[0]], [f"row-{index:02d}" for index in range(row_count)])
            self.assertEqual(connection.commit_count, 1)

        failing_connection = Connection(fail_batch=True)
        failing_store = PostgreSQLReadSnapshotStore(Adapter(failing_connection))
        with mock.patch.object(failing_store, "_snapshot_from_row", return_value=stored), self.assertRaises(StorageFailureError):
            failing_store.create_query_snapshot(principal, scope, {"query": "batch"}, "read", rows)
        self.assertEqual(failing_connection.rollback_count, 1)
        self.assertEqual(failing_connection.commit_count, 0)


if __name__ == "__main__":
    unittest.main()
