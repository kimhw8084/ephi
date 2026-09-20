#!/usr/bin/env python3
"""CHG-161/O10.2 Attention -> Episode performance/capacity harness.

This tool is benchmark-only.  It never supplies application fallback data and
it refuses to call a result production-like unless the executor supplies
measured web, PostgreSQL, worker and client-resource facts matching N1.

The timed service path uses the existing PostgreSQL O3 stores and application
services.  The browser path starts the real NiceGUI application and uses
isolated headless Chromium contexts.  Raw latency samples are retained in the
output so the reported order statistics can be recomputed independently.

Percentiles use the nearest-rank method: for sorted samples x[0..n-1],
percentile(q) = x[max(0, ceil(q*n)-1)].  Warm-up samples and expected typed
conflicts are excluded from latency qualification but are reported separately.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from ephi.application.attention import AttentionQueryService  # noqa: E402
from ephi.application.context import (  # noqa: E402
    AccessScope,
    CommandContext,
    CurrentAuthorizationAuthority,
    Principal,
    RevisionVector,
)
from ephi.application.episodes import EpisodeBriefQueryService  # noqa: E402
from ephi.application.errors import CommandError, VersionConflictError  # noqa: E402
from ephi.application.workflow import EpisodeWorkflowCommandService  # noqa: E402
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter  # noqa: E402
from ephi.infrastructure.postgresql import _PostgreSQLCommandTransaction  # noqa: E402
from ephi.infrastructure.postgresql_o3 import PostgreSQLO3ProductStore  # noqa: E402
from ephi.infrastructure.postgresql_reads import PostgreSQLReadSnapshotStore  # noqa: E402
from ephi.infrastructure.postgresql_worker import WorkerLeaseConfig  # noqa: E402


REQUEST = "ephi-o10-performance-capacity-v1"
BASE_SHA = "8fd41a2fd9f6420cc63bf8a0beaa73a472d25f0e"
WORK_BRANCH = "codex/ephi-o10-performance-capacity-v1"
FABRIC_JOB_ID = os.environ.get("CODEX_FABRIC_JOB_ID", "CF-368950d1cbdb342586974252")

PROFILE = {
    "web_vcpu": 4,
    "web_memory_gib": 8,
    "postgres_vcpu": 4,
    "postgres_memory_gib": 16,
    "client_round_trip_ms_max": 50,
    "worker_resources_independently_identified": True,
}

ENVELOPE = {
    "concurrent_browser_sessions": 100,
    "family_partitions": 30,
    "open_attention_items": 10_000,
    "archived_episode_read_records": 1_000_000,
    "foreground_read_rate_per_second": 20,
    "foreground_burst": {"requests": 40, "window_seconds": 2},
    "workflow_commands_per_minute": 100,
    "attention_page_size": 50,
}

BUDGETS = {
    "attention_warm_p95_ms": 300,
    "attention_warm_p99_ms": 750,
    "attention_useful_page_p95_ms": 2_000,
    "episode_brief_p95_ms": 500,
    "episode_useful_page_p95_ms": 2_000,
    "attention_filter_p95_ms": 500,
    "workflow_command_p95_ms": 500,
    "workflow_command_p99_ms": 1_000,
    "attention_payload_bytes": 250_000,
    "episode_payload_bytes": 1_000_000,
    "acknowledged_effects_lost_after_restart": 0,
}

TABLES = (
    "query_snapshot_row",
    "query_snapshot",
    "read_head",
    "read_revision",
    "o3_attention_projection",
    "aggregate_state",
    "command_receipt",
    "audit_event",
    "outbox_event",
    "applied_effect",
    "job",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_digest(value: object) -> str:
    return sha256_bytes(str(value).encode("utf-8"))


def percentile(values: Sequence[float], q: float) -> float:
    """Return a nearest-rank percentile without silently interpolating."""

    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 <= q <= 1:
        raise ValueError("percentile q must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def _distribution(values: Sequence[float]) -> dict[str, object]:
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
    return {
        "count": len(values),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": max(values),
    }


def summarize_samples(samples: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Summarize retained samples while keeping failures/conflicts visible."""

    valid = [
        float(sample["elapsed_ms"])
        for sample in samples
        if sample.get("status") == "ok" and not sample.get("expected_conflict", False)
    ]
    queue = [float(sample["queue_delay_ms"]) for sample in samples if "queue_delay_ms" in sample]
    scheduling = [float(sample["scheduling_delay_ms"]) for sample in samples if "scheduling_delay_ms" in sample]
    failures = [sample for sample in samples if sample.get("status") == "failure"]
    conflicts = [sample for sample in samples if sample.get("expected_conflict", False)]
    return {
        "sample_count": len(samples),
        "valid_latency_sample_count": len(valid),
        "failures": len(failures),
        "expected_conflicts": len(conflicts),
        "latency_ms": _distribution(valid),
        "queue_delay_ms": _distribution(queue),
        "scheduling_delay_ms": _distribution(scheduling),
        "failure_types": dict(sorted(Counter(str(item.get("error_type", "unknown")) for item in failures).items())),
        "method": "nearest_rank; sorted; rank=max(1,ceil(q*n)); no interpolation",
    }


def aggregate_repetitions(repetitions: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Aggregate three or more repetitions without discarding a slow run."""

    summaries = [dict(item.get("summary", {})) for item in repetitions]
    all_valid = all(
        int(summary.get("failures", 0)) == 0 and int(summary.get("valid_latency_sample_count", 0)) > 0
        for summary in summaries
    )
    return {
        "repetition_count": len(repetitions),
        "all_repetitions_valid": all_valid,
        "repetitions": list(repetitions),
        "worst_p95_ms": max(
            (float(summary["latency_ms"]["p95_ms"]) for summary in summaries if summary.get("latency_ms", {}).get("p95_ms") is not None),
            default=None,
        ),
        "worst_p99_ms": max(
            (float(summary["latency_ms"]["p99_ms"]) for summary in summaries if summary.get("latency_ms", {}).get("p99_ms") is not None),
            default=None,
        ),
    }


def qualify_environment(environment: Mapping[str, object]) -> dict[str, object]:
    """Fail closed unless measured facts establish the exact N1 profile."""

    required = (
        "executor_kind",
        "measured",
        "web_vcpu",
        "web_memory_gib",
        "postgres_vcpu",
        "postgres_memory_gib",
        "client_round_trip_ms_p95",
        "worker_resources_independently_identified",
    )
    missing = [key for key in required if key not in environment]
    if missing:
        return {"state": "BLOCKED_BENCHMARK_ENVIRONMENT", "reason": "missing measured authority facts", "missing": missing}
    mismatches = []
    if not environment.get("measured"):
        mismatches.append("environment facts are not marked measured")
    for key in ("web_vcpu", "web_memory_gib", "postgres_vcpu", "postgres_memory_gib"):
        if float(environment[key]) != float(PROFILE[key]):
            mismatches.append(f"{key}={environment[key]!r} does not match N1={PROFILE[key]!r}")
    if float(environment["client_round_trip_ms_p95"]) > PROFILE["client_round_trip_ms_max"]:
        mismatches.append("client round-trip exceeds N1 maximum")
    if not environment.get("worker_resources_independently_identified"):
        mismatches.append("worker resources are not independently identified")
    if mismatches:
        return {"state": "BLOCKED_BENCHMARK_ENVIRONMENT", "reason": "measured executor does not establish N1", "mismatches": mismatches}
    return {"state": "QUALIFYING_PRODUCTION_LIKE_EXECUTOR", "reason": "measured executor matches N1", "mismatches": []}


def qualify_benchmark(
    environment: Mapping[str, object],
    scenario_results: Mapping[str, Mapping[str, object]],
    browser: Mapping[str, object],
    durability: Mapping[str, object],
    resilience: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    env = qualify_environment(environment)
    if env["state"] != "QUALIFYING_PRODUCTION_LIKE_EXECUTOR":
        return {"state": "BLOCKED_BENCHMARK_ENVIRONMENT", "environment": env, "budgets": "NOT_EVALUATED"}
    failures: list[str] = []
    for name, result in scenario_results.items():
        if not result.get("all_repetitions_valid"):
            failures.append(f"{name}: invalid repetition")
        budget = result.get("budget")
        if budget and budget.get("status") != "PASS":
            failures.append(f"{name}: {budget.get('status', 'FAIL')}")
    if browser.get("status") != "PASS":
        failures.append("browser/session cohort did not pass")
    if durability.get("acknowledged_effects_lost_after_restart") != 0:
        failures.append("durability lost acknowledged effect")
    for name, result in resilience.items():
        if result.get("status") != "PASS":
            failures.append(f"{name}: {result.get('status', 'FAIL')}")
    return {
        "state": "PASS_CURRENT_SURFACE_BUDGETS" if not failures else "FAIL_CURRENT_SURFACE_BUDGETS",
        "environment": env,
        "failures": failures,
    }


@dataclass(frozen=True, slots=True)
class DatasetConfig:
    seed: int = 161
    family_partitions: int = 30
    attention_items: int = 10_000
    archived_read_records: int = 1_000_000
    config_id: str = "ephi-o10-161-v1"

    @property
    def identity(self) -> str:
        payload = asdict(self)
        return sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def scopes_for(config: DatasetConfig) -> tuple[AccessScope, ...]:
    return tuple(
        AccessScope(
            f"benchmark-scope-{index:02d}",
            site_id="benchmark-site",
            area_id="benchmark-area",
            family_id=f"benchmark-family-{index:02d}",
        )
        for index in range(1, config.family_partitions + 1)
    )


def expected_fixture_counts(config: DatasetConfig) -> dict[str, int]:
    return {
        "o3_attention_projection": config.attention_items,
        "aggregate_state": config.attention_items,
        "read_revision": config.archived_read_records,
        "read_head": config.attention_items,
        "family_partitions": config.family_partitions,
    }


def validate_fixture_counts(actual: Mapping[str, int], config: DatasetConfig) -> dict[str, object]:
    expected = expected_fixture_counts(config)
    mismatches = {
        key: {"expected": expected[key], "actual": int(actual.get(key, -1))}
        for key in expected
        if int(actual.get(key, -1)) != expected[key]
    }
    if mismatches:
        raise ValueError(f"benchmark fixture count validation failed: {mismatches}")
    return {"status": "PASS", "expected": expected, "actual": {key: int(actual[key]) for key in expected}}


def _scope_values(config: DatasetConfig) -> list[tuple[int, str]]:
    return [(index, scope.canonical_key) for index, scope in enumerate(scopes_for(config), start=1)]


def _values_sql(values: Sequence[tuple[object, ...]]) -> str:
    return ", ".join("(" + ", ".join("%s" for _ in row) + ")" for row in values)


def _fixture_reset(connection: Any) -> None:
    connection.execute("TRUNCATE " + ", ".join(TABLES) + " CASCADE")


def seed_database(dsn: str, config: DatasetConfig, *, reset: bool = True) -> dict[str, object]:
    """Seed the benchmark-only synthetic dataset with set-based PostgreSQL SQL."""

    if not reset:
        raise ValueError("benchmark seeding requires explicit reset=True on a benchmark-only database")
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        connection = adapter.connection
        _fixture_reset(connection)
        scopes = _scope_values(config)
        scope_cte = f"VALUES {_values_sql(scopes)}"
        item_count = config.attention_items
        archive_count = config.archived_read_records - item_count
        base_ts = "2025-01-01 00:00:00+00"
        connection.execute(
            f"""
            WITH scopes(scope_no, scope_key) AS ({scope_cte})
            INSERT INTO aggregate_state(scope_key, aggregate_type, aggregate_id, version, state_json)
            SELECT s.scope_key, 'episode_workflow', 'bench-episode-' || lpad(g.i::text, 5, '0'), 0,
                   jsonb_build_object('owner', NULL, 'work_state', 'OPEN', 'claimed_at', NULL, 'acknowledged_at', NULL)
            FROM generate_series(1, %s) AS g(i)
            JOIN scopes s ON s.scope_no = ((g.i - 1) %% %s) + 1
            """,
            (*[value for row in scopes for value in row], item_count, config.family_partitions),
        )
        connection.execute(
            f"""
            WITH scopes(scope_no, scope_key) AS ({scope_cte})
            INSERT INTO o3_attention_projection(scope_key, episode_id, row_version, payload_json)
            SELECT s.scope_key, 'bench-episode-' || lpad(g.i::text, 5, '0'), '1',
                   jsonb_build_object(
                       'title', 'Synthetic attention item ' || lpad(g.i::text, 5, '0'),
                       'asset_id', 'benchmark-asset-' || lpad((((g.i - 1) %% 300) + 1)::text, 3, '0'),
                       'priority', CASE WHEN g.i %% 10 = 0 THEN 'P1' WHEN g.i %% 3 = 0 THEN 'P2' ELSE 'P3' END,
                       'severity', CASE WHEN g.i %% 10 = 0 THEN 'CRITICAL' WHEN g.i %% 4 = 0 THEN 'HIGH' ELSE 'MEDIUM' END,
                       'technical_state', 'ACTIVE', 'source_state', 'READY',
                       'deadline', to_char(%s::timestamptz + (g.i * interval '1 minute'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                       'age', (g.i %% 365)::text, 'family_partition', s.scope_no
                   )
            FROM generate_series(1, %s) AS g(i)
            JOIN scopes s ON s.scope_no = ((g.i - 1) %% %s) + 1
            """,
            (*[value for row in scopes for value in row], base_ts, item_count, config.family_partitions),
        )
        vector = json.dumps(
            {
                "analysis_revision": "benchmark-analysis-v1",
                "exposure_revision": None,
                "priority_revision": None,
                "workflow_version": 0,
                "plan_version": None,
                "qualification_manifest_id": "benchmark-qualification-v1",
            },
            separators=(",", ":"),
        )
        connection.execute(
            f"""
            WITH scopes(scope_no, scope_key) AS ({scope_cte})
            INSERT INTO read_revision(
                revision_id, scope_key, entity_type, entity_id, revision_vector_json, payload_json,
                known_at, published_at, workflow_aggregate_type, workflow_aggregate_id,
                workflow_version, workflow_state_json
            )
            SELECT CASE WHEN g.i > %s
                        THEN 'bench-current-' || lpad((((g.i - %s - 1) %% %s) + 1)::text, 5, '0')
                        ELSE 'bench-archived-' || lpad(g.i::text, 7, '0') END,
                   s.scope_key, 'episode', 'bench-episode-' || lpad((((g.i - 1) %% %s) + 1)::text, 5, '0'),
                   %s::jsonb,
                   jsonb_build_object(
                       'title', 'Synthetic episode read record ' || lpad(g.i::text, 7, '0'),
                       'analytical_revision', 'benchmark-analysis-v1',
                       'capability_state', jsonb_build_object('source', 'READY', 'synthetic', true),
                       'archive_ordinal', g.i
                   ),
                   %s::timestamptz + (g.i * interval '1 second'),
                   %s::timestamptz + (g.i * interval '1 second'),
                   'episode_workflow', 'bench-episode-' || lpad((((g.i - 1) %% %s) + 1)::text, 5, '0'),
                   0,
                   jsonb_build_object('owner', NULL, 'work_state', 'OPEN', 'claimed_at', NULL, 'acknowledged_at', NULL)
            FROM generate_series(1, %s) AS g(i)
            JOIN scopes s ON s.scope_no = ((((g.i - 1) %% %s) + 1 - 1) %% %s) + 1
            """,
            (
                *[value for row in scopes for value in row],
                archive_count,
                archive_count,
                item_count,
                item_count,
                vector,
                base_ts,
                base_ts,
                item_count,
                config.archived_read_records,
                item_count,
                config.family_partitions,
            ),
        )
        connection.execute(
            f"""
            WITH scopes(scope_no, scope_key) AS ({scope_cte})
            INSERT INTO read_head(scope_key, entity_type, entity_id, revision_id, head_version, published_at)
            SELECT s.scope_key, 'episode', 'bench-episode-' || lpad(g.i::text, 5, '0'),
                   'bench-current-' || lpad(g.i::text, 5, '0'), 1,
                   %s::timestamptz + ((%s + g.i) * interval '1 second')
            FROM generate_series(1, %s) AS g(i)
            JOIN scopes s ON s.scope_no = ((g.i - 1) %% %s) + 1
            """,
            (*[value for row in scopes for value in row], base_ts, archive_count, item_count, config.family_partitions),
        )
        for table in ("aggregate_state", "o3_attention_projection", "read_revision", "read_head"):
            connection.execute(f"ANALYZE {table}")
        facts = fixture_facts(adapter, config)
        validate_fixture_counts(facts["counts"], config)
        return {"config": asdict(config), "config_identity": config.identity, "facts": facts}
    finally:
        adapter.close()


def fixture_facts(adapter: PostgreSQLReferenceTransactionAdapter, config: DatasetConfig) -> dict[str, object]:
    connection = adapter.connection
    count_rows = {
        table: int(connection.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"])
        for table in ("o3_attention_projection", "aggregate_state", "read_revision", "read_head")
    }
    count_rows["family_partitions"] = int(
        connection.execute("SELECT count(DISTINCT scope_key) AS count FROM o3_attention_projection").fetchone()["count"]
    )
    indexes = connection.execute(
        """
        SELECT count(*) AS count
        FROM pg_indexes
        WHERE schemaname = current_schema()
          AND tablename IN ('o3_attention_projection', 'aggregate_state', 'read_revision', 'read_head')
        """
    ).fetchone()["count"]
    sizes = connection.execute(
        """
        SELECT relname, pg_total_relation_size(c.oid) AS bytes
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relname IN ('o3_attention_projection', 'aggregate_state', 'read_revision', 'read_head')
        ORDER BY relname
        """
    ).fetchall()
    high_water = connection.execute(
        """
        SELECT max(replace(episode_id, 'bench-episode-', '')::integer) AS attention_episode_high_water
        FROM o3_attention_projection
        """
    ).fetchone()
    revision_high_water = connection.execute(
        "SELECT max(published_at) AS max_published_at FROM read_revision"
    ).fetchone()
    return {
        "counts": count_rows,
        "relevant_index_count": int(indexes),
        "table_sizes_bytes": {row["relname"]: int(row["bytes"]) for row in sizes},
        "database_size_bytes": int(connection.execute("SELECT pg_database_size(current_database()) AS bytes").fetchone()["bytes"]),
        "high_water": {
            "attention_episode_number": int(high_water["attention_episode_high_water"] or 0),
            "read_published_at": revision_high_water["max_published_at"].isoformat() if revision_high_water["max_published_at"] else None,
        },
        "server_version": adapter.server_version(),
        "seed_config_identity": config.identity,
    }


class _CountingConnection:
    def __init__(self, connection: Any):
        self._connection = connection
        self.query_count = 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.query_count += 1
        return self._connection.execute(*args, **kwargs)

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return _CountingCursor(self, self._connection.cursor(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _CountingCursor:
    def __init__(self, connection: _CountingConnection, cursor: Any):
        self._connection = connection
        self._cursor = cursor

    def executemany(self, *args: Any, **kwargs: Any) -> Any:
        self._connection.query_count += 1
        return self._cursor.executemany(*args, **kwargs)

    def __enter__(self) -> "_CountingCursor":
        self._cursor.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._cursor.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _CountingAdapter:
    """Adapter facade used only to count calls inside one application operation."""

    def __init__(self, adapter: PostgreSQLReferenceTransactionAdapter):
        self._adapter = adapter
        self.connection = _CountingConnection(adapter.connection)

    def command_transaction(self):
        return _PostgreSQLCommandTransaction(self)

    def _receipt_from_row(self, row: Mapping[str, Any]):
        return self._adapter._receipt_from_row(row)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)


@dataclass(slots=True)
class _ServiceSession:
    adapter: PostgreSQLReferenceTransactionAdapter
    counted: _CountingAdapter
    attention: AttentionQueryService
    briefs: EpisodeBriefQueryService
    workflow: EpisodeWorkflowCommandService
    principal: Principal
    scopes: tuple[AccessScope, ...]

    @classmethod
    def open(cls, dsn: str, config: DatasetConfig) -> "_ServiceSession":
        adapter = PostgreSQLReferenceTransactionAdapter(dsn)
        scopes = scopes_for(config)
        principal = Principal(
            "benchmark-engineer",
            (
                "ephi.attention.read",
                "ephi.episode.read",
                "ephi.episode.claim",
                "ephi.episode.acknowledge",
            ),
            scopes,
            1,
            1,
        )
        authority = CurrentAuthorizationAuthority.from_provider(lambda: principal)
        counted = _CountingAdapter(adapter)
        attention = AttentionQueryService(PostgreSQLO3ProductStore(counted), PostgreSQLReadSnapshotStore(counted), authority)
        briefs = EpisodeBriefQueryService(PostgreSQLReadSnapshotStore(counted), authority)
        workflow = EpisodeWorkflowCommandService(counted, authority)
        return cls(adapter, counted, attention, briefs, workflow, principal, scopes)

    def close(self) -> None:
        self.adapter.close()


def _sample(
    operation: str,
    sample_id: str,
    scheduled_ns: int,
    started_ns: int,
    finished_ns: int,
    *,
    status: str = "ok",
    expected_conflict: bool = False,
    error_type: str | None = None,
    query_count: int | None = None,
) -> dict[str, object]:
    return {
        "operation": operation,
        "sample_id": sample_id,
        "scheduled_at_monotonic_ns": scheduled_ns,
        "actual_start_monotonic_ns": started_ns,
        "scheduling_delay_ms": round(max(0, started_ns - scheduled_ns) / 1_000_000, 3),
        "queue_delay_ms": round(max(0, started_ns - scheduled_ns) / 1_000_000, 3),
        "elapsed_ms": round((finished_ns - started_ns) / 1_000_000, 3),
        "status": status,
        "expected_conflict": expected_conflict,
        **({"error_type": error_type} if error_type else {}),
        **({"query_count": query_count} if query_count is not None else {}),
    }


def _run_rate(
    operation: str,
    repetitions: int,
    rate_per_second: float,
    duration_seconds: float,
    warmup_samples: int,
    worker: Callable[[int, int], tuple[str, bool, int | None]],
    *,
    max_workers: int = 64,
) -> dict[str, object]:
    from concurrent.futures import ThreadPoolExecutor

    repetitions_out = []
    sample_count = max(1, int(math.ceil(rate_per_second * duration_seconds)))
    interval_ns = int(1_000_000_000 / rate_per_second)
    for repetition in range(1, repetitions + 1):
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"ephi-{operation}") as executor:
            # Warm the same executor threads that will carry steady-state
            # work. Opening a PostgreSQL connection on the caller thread would
            # leave the measured worker threads cold and contaminate samples.
            warmup_count = max(warmup_samples, min(max_workers, 16))
            warmups = [executor.submit(worker, repetition, -(index + 1)) for index in range(warmup_count)]
            for future in warmups:
                try:
                    future.result()
                except Exception:
                    # Warm-up is deliberately excluded from evidence but never
                    # turns into a steady-state failure or a hidden result.
                    pass
            origin = time.monotonic_ns() + 100_000_000

            def invoke(index: int) -> dict[str, object]:
                scheduled = origin + index * interval_ns
                while time.monotonic_ns() < scheduled:
                    remaining = scheduled - time.monotonic_ns()
                    if remaining > 100_000:
                        time.sleep(remaining / 1_000_000_000)
                started = time.monotonic_ns()
                try:
                    status, expected_conflict, query_count = worker(repetition, index)
                    finished = time.monotonic_ns()
                    return _sample(operation, f"r{repetition}-s{index}", scheduled, started, finished, status=status, expected_conflict=expected_conflict, query_count=query_count)
                except Exception as exc:
                    finished = time.monotonic_ns()
                    return _sample(operation, f"r{repetition}-s{index}", scheduled, started, finished, status="failure", error_type=type(exc).__name__)

            samples = list(executor.map(invoke, range(sample_count)))
        repetitions_out.append({"repetition": repetition, "summary": summarize_samples(samples), "samples": samples})
    return aggregate_repetitions(repetitions_out)


def _scope_for_index(scopes: Sequence[AccessScope], episode_number: int) -> AccessScope:
    return scopes[(episode_number - 1) % len(scopes)]


def run_service_workloads(dsn: str, config: DatasetConfig, *, repetitions: int, duration_seconds: float) -> dict[str, object]:
    local = threading.local()
    sessions: list[_ServiceSession] = []
    session_lock = threading.Lock()

    def session() -> _ServiceSession:
        value = getattr(local, "session", None)
        if value is None:
            value = _ServiceSession.open(dsn, config)
            local.session = value
            with session_lock:
                sessions.append(value)
        value.counted.connection.query_count = 0
        return value

    def attention_worker(_rep: int, _index: int):
        value = session()
        page = value.attention.list_attention(value.principal, value.scopes[0], page_size=50)
        return ("ok" if len(page.rows) <= 50 else "failure", False, value.counted.connection.query_count)

    def filter_worker(_rep: int, index: int):
        value = session()
        page = value.attention.list_attention(
            value.principal,
            value.scopes[0],
            filters={"search": f"bench-episode-{(index % 300) + 1:05d}"},
            page_size=50,
        )
        return ("ok" if len(page.rows) <= 50 else "failure", False, value.counted.connection.query_count)

    def episode_worker(_rep: int, index: int):
        value = session()
        episode_number = (index % config.attention_items) + 1
        brief = value.briefs.get_episode_brief(value.principal, _scope_for_index(value.scopes, episode_number), f"bench-episode-{episode_number:05d}")
        return ("ok" if brief.episode_id else "failure", False, value.counted.connection.query_count)

    def command_worker(rep: int, index: int):
        value = session()
        if index < 0:
            episode_number = 100 + (-index)
        else:
            episode_number = ((rep - 1) * max(1, int(duration_seconds * 2)) + index) % config.attention_items + 1
        scope = _scope_for_index(value.scopes, episode_number)
        episode_id = f"bench-episode-{episode_number:05d}"
        context = CommandContext(
            f"bench-load-command-r{rep}-s{index}",
            value.principal,
            scope,
            0,
            RevisionVector("benchmark-analysis-v1", None, None, 0, None, "benchmark-qualification-v1"),
            "synthetic O10.2 load command",
        )
        try:
            value.workflow.claim_episode(context, episode_id)
        except VersionConflictError:
            # A repeated short run can legitimately meet a previously claimed
            # row. It is reported as an expected typed conflict, never as a
            # successful latency sample.
            return ("ok", True, value.counted.connection.query_count)
        return ("ok", False, value.counted.connection.query_count)

    try:
        results = {
            "attention_warm": _run_rate("attention_warm", repetitions, 20, duration_seconds, 5, attention_worker),
            "attention_burst": _run_rate("attention_burst", repetitions, ENVELOPE["foreground_burst"]["requests"] / ENVELOPE["foreground_burst"]["window_seconds"], ENVELOPE["foreground_burst"]["window_seconds"], 5, attention_worker),
            "attention_filter_search": _run_rate("attention_filter_search", repetitions, 20, duration_seconds, 5, filter_worker),
            "episode_brief": _run_rate("episode_brief", repetitions, 20, duration_seconds, 5, episode_worker),
            "workflow_command": _run_rate("workflow_command", repetitions, 100 / 60, duration_seconds, 3, command_worker),
        }
    finally:
        for item in sessions:
            item.close()
    return results


def _budget_result(summary: Mapping[str, object], *, p95: float | None = None, p99: float | None = None, p95_budget: float | None = None, p99_budget: float | None = None) -> dict[str, object]:
    actual_p95 = float(summary["worst_p95_ms"]) if summary.get("worst_p95_ms") is not None else None
    actual_p99 = float(summary["worst_p99_ms"]) if summary.get("worst_p99_ms") is not None else None
    ok = bool(summary.get("all_repetitions_valid")) and actual_p95 is not None
    if p95_budget is not None:
        ok = ok and actual_p95 is not None and actual_p95 <= p95_budget
    if p99_budget is not None:
        ok = ok and actual_p99 is not None and actual_p99 <= p99_budget
    return {"status": "PASS" if ok else "FAIL", "p95_ms": actual_p95, "p99_ms": actual_p99, "p95_budget_ms": p95_budget, "p99_budget_ms": p99_budget}


def attach_service_budgets(results: dict[str, object]) -> dict[str, object]:
    budgets = {
        "attention_warm": (BUDGETS["attention_warm_p95_ms"], BUDGETS["attention_warm_p99_ms"]),
        "attention_burst": (BUDGETS["attention_warm_p95_ms"], BUDGETS["attention_warm_p99_ms"]),
        "attention_filter_search": (BUDGETS["attention_filter_p95_ms"], None),
        "episode_brief": (BUDGETS["episode_brief_p95_ms"], None),
        "workflow_command": (BUDGETS["workflow_command_p95_ms"], BUDGETS["workflow_command_p99_ms"]),
    }
    for name, (p95_budget, p99_budget) in budgets.items():
        result = results[name]
        result["budget"] = _budget_result(result, p95_budget=p95_budget, p99_budget=p99_budget)
    return results


def _explain_json(connection: Any, sql: str, params: Sequence[object]) -> dict[str, object]:
    row = connection.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).fetchone()
    plan = row["QUERY PLAN"] if isinstance(row, Mapping) else row[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    root = plan[0]["Plan"] if isinstance(plan, list) and plan else plan
    nodes: list[Mapping[str, object]] = []

    def visit(node: Mapping[str, object]) -> None:
        nodes.append(node)
        for child in node.get("Plans", []) if isinstance(node.get("Plans", []), list) else []:
            if isinstance(child, Mapping):
                visit(child)

    if isinstance(root, Mapping):
        visit(root)
    return {
        "plan_sha256": sha256_bytes(encoded.encode("utf-8")),
        "plan": plan,
        "scan_nodes": [
            {
                "node_type": node.get("Node Type"),
                "relation": node.get("Relation Name"),
                "index": node.get("Index Name"),
                "plan_rows": node.get("Plan Rows"),
                "actual_rows": node.get("Actual Rows"),
                "actual_loops": node.get("Actual Loops"),
                "shared_hit_blocks": node.get("Shared Hit Blocks"),
                "shared_read_blocks": node.get("Shared Read Blocks"),
            }
            for node in nodes
            if node.get("Node Type") in {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Bitmap Index Scan"}
        ],
    }


def profile_database(dsn: str, config: DatasetConfig) -> dict[str, object]:
    """Capture plans outside timed samples and record unavailable profiler facts."""

    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        connection = adapter.connection
        scope = scopes_for(config)[0].canonical_key
        attention_sql = """
            SELECT p.episode_id, p.row_version, p.payload_json
            FROM o3_attention_projection AS p
            JOIN aggregate_state AS a
              ON a.scope_key = %s AND a.aggregate_type = 'episode_workflow' AND a.aggregate_id = p.episode_id
            WHERE p.scope_key = %s
            ORDER BY CASE p.payload_json->>'priority' WHEN 'P1' THEN 3 WHEN 'P2' THEN 2 WHEN 'P3' THEN 1 ELSE 0 END DESC,
                     p.payload_json->>'deadline' ASC NULLS LAST, p.episode_id ASC
            LIMIT 1001
        """
        search_sql = """
            SELECT p.episode_id
            FROM o3_attention_projection AS p
            JOIN aggregate_state AS a ON a.scope_key = %s AND a.aggregate_type = 'episode_workflow' AND a.aggregate_id = p.episode_id
            WHERE p.scope_key = %s AND (p.episode_id ILIKE %s OR coalesce(p.payload_json->>'title', '') ILIKE %s OR coalesce(p.payload_json->>'asset_id', '') ILIKE %s)
            ORDER BY p.episode_id ASC LIMIT 1001
        """
        episode_head_sql = """
            SELECT r.revision_id, r.entity_id, r.workflow_version, r.payload_json
            FROM read_head AS h JOIN read_revision AS r ON r.revision_id = h.revision_id
            WHERE h.scope_key = %s AND h.entity_type = 'episode' AND h.entity_id = %s
        """
        aggregate_sql = "SELECT scope_key, aggregate_type, aggregate_id, version, state_json FROM aggregate_state WHERE scope_key = %s AND aggregate_type = 'episode_workflow' AND aggregate_id = %s"
        profiles = {
            "attention_default": _explain_json(connection, attention_sql, (scope, scope)),
            "attention_filter_search": _explain_json(connection, search_sql, (scope, scope, "%Synthetic attention%", "%Synthetic attention%", "%Synthetic attention%")),
            "episode_read_head": _explain_json(connection, episode_head_sql, (scope, "bench-episode-00001")),
            "episode_workflow_join": _explain_json(connection, aggregate_sql, (scope, "bench-episode-00001")),
        }
        settings = {}
        for key in ("max_connections", "statement_timeout", "lock_timeout", "shared_buffers", "work_mem"):
            settings[key] = connection.execute("SELECT current_setting(%s) AS value", (key,)).fetchone()["value"]
        pg_stat = {"extension": "NOT_AVAILABLE", "query_count_per_operation": "captured_by_counting_adapter"}
        try:
            row = connection.execute("SELECT extname FROM pg_extension WHERE extname = 'pg_stat_statements'").fetchone()
            if row:
                pg_stat["extension"] = "AVAILABLE"
        except Exception as exc:
            pg_stat["extension_error_type"] = type(exc).__name__
        return {
            "profiles": profiles,
            "connection_settings": settings,
            "pg_stat_statements": pg_stat,
            "relevant_relation_facts": fixture_facts(adapter, config),
            "profiled_outside_timed_samples": True,
        }
    finally:
        adapter.close()


def _resource_facts(dsn: str | None = None) -> dict[str, object]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    facts: dict[str, object] = {
        "executor_os": platform.platform(aliased=True),
        "executor_architecture": platform.machine(),
        "python": platform.python_version(),
        "process_user_cpu_seconds": usage.ru_utime,
        "process_system_cpu_seconds": usage.ru_stime,
        "process_max_rss_bytes": int(usage.ru_maxrss * (1024 if sys.platform == "darwin" else 1)),
        "logical_cpu_count": os.cpu_count(),
        "cgroup_cpu_limit": None,
        "cgroup_memory_limit_bytes": None,
    }
    for path, key in ((Path("/sys/fs/cgroup/cpu.max"), "cgroup_cpu_limit"), (Path("/sys/fs/cgroup/memory.max"), "cgroup_memory_limit_bytes")):
        try:
            value = path.read_text(encoding="utf-8").strip()
            if key == "cgroup_cpu_limit":
                facts[key] = value
            elif value != "max":
                facts[key] = int(value)
        except (OSError, ValueError):
            pass
    if dsn:
        try:
            adapter = PostgreSQLReferenceTransactionAdapter(dsn)
            try:
                connection = adapter.connection
                facts["postgres_active_connections"] = int(connection.execute("SELECT count(*) AS count FROM pg_stat_activity WHERE datname = current_database()").fetchone()["count"])
                facts["postgres_cpu_rss"] = "NOT_EXPOSED_BY_POSTGRES_SERVICE"
                facts["worker_backlog"] = int(connection.execute("SELECT count(*) AS count FROM job WHERE status IN ('QUEUED', 'RUNNING', 'DEFERRED')").fetchone()["count"])
                facts["worker_expired_leases"] = int(connection.execute("SELECT count(*) AS count FROM job WHERE lease_expires_at IS NOT NULL AND lease_expires_at < clock_timestamp()").fetchone()["count"])
            finally:
                adapter.close()
        except Exception as exc:
            facts["postgres_resource_error_type"] = type(exc).__name__
    return facts


def _principal_and_context(config: DatasetConfig, episode_number: int, command_id: str) -> tuple[Principal, AccessScope, CommandContext]:
    scopes = scopes_for(config)
    principal = Principal(
        "benchmark-durability-engineer",
        ("ephi.episode.read", "ephi.episode.claim", "ephi.episode.acknowledge", "ephi.attention.read"),
        scopes,
        1,
        1,
    )
    scope = _scope_for_index(scopes, episode_number)
    vector = RevisionVector("benchmark-analysis-v1", None, None, 0, None, "benchmark-qualification-v1")
    return principal, scope, CommandContext(command_id, principal, scope, 0, vector, "O10.2 durability probe")


def durability_probe(dsn: str, config: DatasetConfig) -> dict[str, object]:
    """Prove command receipt replay, read-your-write and exact-once local writes."""

    episode_number = config.attention_items - 3
    episode_id = f"bench-episode-{episode_number:05d}"
    principal, scope, context = _principal_and_context(config, episode_number, "bench-durability-probe")
    first = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        authority = CurrentAuthorizationAuthority.from_provider(lambda: principal)
        workflow = EpisodeWorkflowCommandService(first, authority)
        result = workflow.claim_episode(context, episode_id)
        brief = EpisodeBriefQueryService(first.read_store(), authority).get_episode_brief(principal, scope, episode_id)
        before_restart = {
            "result_identity_hash": safe_digest(result.result_identity),
            "read_your_write": brief.workflow.get("work_state") == "CLAIMED" and brief.revision_vector.workflow_version == 1,
            "receipt_count": int(first.connection.execute("SELECT count(*) AS count FROM command_receipt WHERE command_id = %s", (context.command_id,)).fetchone()["count"]),
            "audit_count": int(first.connection.execute("SELECT count(*) AS count FROM audit_event WHERE command_id = %s", (context.command_id,)).fetchone()["count"]),
            "outbox_count": int(first.connection.execute("SELECT count(*) AS count FROM outbox_event WHERE command_id = %s", (context.command_id,)).fetchone()["count"]),
        }
    finally:
        first.close()
    second = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        authority = CurrentAuthorizationAuthority.from_provider(lambda: principal)
        replay = EpisodeWorkflowCommandService(second, authority).claim_episode(context, episode_id)
        after = EpisodeBriefQueryService(second.read_store(), authority).get_episode_brief(principal, scope, episode_id)
        receipts = int(second.connection.execute("SELECT count(*) AS count FROM command_receipt WHERE command_id = %s", (context.command_id,)).fetchone()["count"])
        return {
            "acknowledged_effects_lost_after_restart": 0 if replay.result_identity == result.result_identity else 1,
            "before_restart": before_restart,
            "after_restart": {
                "same_result_identity": replay.result_identity == result.result_identity,
                "read_your_write": after.workflow.get("work_state") == "CLAIMED" and after.revision_vector.workflow_version == 1,
                "receipt_count": receipts,
            },
            "exactly_once": before_restart["receipt_count"] == before_restart["audit_count"] == before_restart["outbox_count"] == receipts == 1,
            "crash_restart_simulation": "application adapter close/reopen; command replay under current authorization",
        }
    finally:
        second.close()


def conflict_probe(dsn: str, config: DatasetConfig) -> dict[str, object]:
    """Exercise a controlled same-row/version conflict separately from latency."""

    from concurrent.futures import ThreadPoolExecutor

    episode_number = config.attention_items - 2
    episode_id = f"bench-episode-{episode_number:05d}"
    scopes = scopes_for(config)
    principal, scope, first_context = _principal_and_context(config, episode_number, "bench-conflict-a")
    second_context = CommandContext("bench-conflict-b", principal, scope, 0, first_context.viewed_revisions, "O10.2 expected conflict")

    # This reserved row is outside the scheduled command ranges. Reset only
    # benchmark-owned conflict identities so repeated diagnostic runs do not
    # turn a prior committed winner into an untyped setup failure.
    cleanup = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        cleanup.connection.execute("DELETE FROM command_receipt WHERE command_id IN (%s, %s)", (first_context.command_id, second_context.command_id))
        cleanup.connection.execute("DELETE FROM audit_event WHERE command_id IN (%s, %s)", (first_context.command_id, second_context.command_id))
        cleanup.connection.execute("DELETE FROM outbox_event WHERE command_id IN (%s, %s)", (first_context.command_id, second_context.command_id))
        cleanup.connection.execute(
            "UPDATE aggregate_state SET version = 0, state_json = %s::jsonb WHERE scope_key = %s AND aggregate_type = 'episode_workflow' AND aggregate_id = %s",
            (json.dumps({"owner": None, "work_state": "OPEN", "claimed_at": None, "acknowledged_at": None}, separators=(",", ":")), scope.canonical_key, episode_id),
        )
    finally:
        cleanup.close()

    adapters = [PostgreSQLReferenceTransactionAdapter(dsn), PostgreSQLReferenceTransactionAdapter(dsn)]

    def attempt(item: tuple[PostgreSQLReferenceTransactionAdapter, CommandContext]) -> str:
        adapter, context = item
        try:
            authority = CurrentAuthorizationAuthority.from_provider(lambda: principal)
            EpisodeWorkflowCommandService(adapter, authority).claim_episode(context, episode_id)
            return "committed"
        except VersionConflictError:
            return "VERSION_CONFLICT"
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, zip(adapters, (first_context, second_context))))
    finally:
        for adapter in adapters:
            adapter.close()
    return {
        "episode_id_hash": safe_digest(episode_id),
        "outcomes": sorted(outcomes),
        "committed_count": outcomes.count("committed"),
        "expected_conflict_count": outcomes.count("VERSION_CONFLICT"),
        "status": "PASS" if sorted(outcomes) == ["VERSION_CONFLICT", "committed"] else "FAIL",
    }


def restore_rehearsals(
    dsn: str,
    admin_dsn: str | None,
    *,
    repetitions: int,
    output_dir: Path,
    target_database_prefix: str = "ephi_o10_restore",
) -> dict[str, object]:
    """Delegate full logical dump/isolated restore to the integrated O9 tool."""

    if not admin_dsn:
        return {
            "status": "NOT_RUN",
            "repetition_count": 0,
            "reason": "--admin-dsn is required for the real logical dump and isolated restore rehearsal",
            "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
        }
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for repetition in range(1, repetitions + 1):
        with tempfile.TemporaryDirectory(prefix="ephi-o10-restore-") as temp:
            root = Path(temp)
            command = [
                sys.executable,
                str(ROOT / "tools" / "o9_real_rehearsal.py"),
                "--dsn",
                dsn,
                "--admin-dsn",
                admin_dsn,
                "--artifact-root",
                str(root / "source-artifacts"),
                "--backup-dir",
                str(root / "backup"),
                "--target-artifact-root",
                str(root / "restored-artifacts"),
                "--target-database",
                f"{target_database_prefix}_{repetition}",
                "--evidence-dir",
                str(root / "evidence"),
            ]
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=900, check=False)
            result: dict[str, object] = {"repetition": repetition, "exit_code": completed.returncode}
            if completed.returncode == 0:
                try:
                    summary = json.loads(completed.stdout)
                    result.update(
                        {
                            "status": "PASS" if summary.get("backup_verification", {}).get("verification_state") == "VERIFIED" and summary.get("restore", {}).get("verification_state") == "VERIFIED" else "FAIL",
                            "backup_verification": summary.get("backup_verification", {}).get("verification_state"),
                            "restore_verification": summary.get("restore", {}).get("verification_state"),
                            "timing_scope": summary.get("timing", {}).get("scope"),
                            "production_disaster_rpo_rto_claim": summary.get("production_disaster_rpo_rto_claim"),
                        }
                    )
                except (TypeError, json.JSONDecodeError):
                    result.update({"status": "FAIL", "error_type": "invalid_rehearsal_summary"})
            else:
                result.update({"status": "FAIL", "error_type": "o9_rehearsal_failed", "stderr_digest": safe_digest(completed.stderr)})
            results.append(result)
    return {
        "status": "PASS" if len(results) == repetitions and all(item.get("status") == "PASS" for item in results) else "FAIL",
        "repetition_count": repetitions,
        "repetitions": results,
        "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
    }


def worker_starvation_probe(dsn: str, config: DatasetConfig, repetitions: int) -> dict[str, object]:
    """Use the existing O9 lease/fencing store; payloads remain synthetic."""

    repetition_results = []
    scope = scopes_for(config)[0]
    for repetition in range(1, repetitions + 1):
        adapter = PostgreSQLReferenceTransactionAdapter(dsn)
        try:
            worker = adapter.worker_store(config=WorkerLeaseConfig(lease_duration=timedelta(milliseconds=100), heartbeat_interval=timedelta(milliseconds=20)))
            semantic = f"o10-starvation-{repetition}"
            job = worker.enqueue(scope, "O10SyntheticStarvation", semantic, {"synthetic": True}, max_attempts=3)
            lease = worker.claim(scope, f"o10-worker-a-{repetition}")
            if lease is None:
                raise RuntimeError("worker lease was not claimable")
            time.sleep(0.15)
            takeover = worker.claim(scope, f"o10-worker-b-{repetition}")
            if takeover is None or takeover.lease.epoch <= lease.lease.epoch:
                raise RuntimeError("expired worker lease did not fence and recover")
            repetition_results.append({"repetition": repetition, "status": "PASS", "old_epoch": lease.lease.epoch, "takeover_epoch": takeover.lease.epoch})
        except Exception as exc:
            repetition_results.append({"repetition": repetition, "status": "FAIL", "error_type": type(exc).__name__})
        finally:
            adapter.close()
    return {"repetition_count": repetitions, "repetitions": repetition_results, "status": "PASS" if all(item["status"] == "PASS" for item in repetition_results) else "FAIL", "foreground_reads_and_commands": "measured separately; worker store remains the integrated O9 authority"}


def source_degraded_probe(dsn: str, config: DatasetConfig, repetitions: int) -> dict[str, object]:
    """Record the current source-degradation boundary without fabricating data."""

    from ephi.application.o10 import state_for_error
    from ephi.application.errors import StorageFailureError

    error = StorageFailureError("benchmark source capability unavailable")
    state = state_for_error(error)
    # The current canonical W1 has no mutable source-capability adapter to
    # transition to STALE/UNAVAILABLE.  Keep the exercised error mapping as a
    # diagnostic contract, but do not mislabel it as a G10 load repetition.
    return {
        "repetition_count": 0,
        "repetitions": [],
        "status": "NOT_RUN",
        "diagnostic_mapping": {"truth_state": state.kind, "message_digest": safe_digest(state.message)},
        "limitation": "CHG-144 remains BLOCKED_REAL_SOURCE; no production source capability adapter exists to toggle STALE/UNAVAILABLE during a real foreground load.",
        "requested_repetitions": repetitions,
    }


def _wait_port(port: int, process: subprocess.Popen[str], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("web process exited before readiness")
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError("web readiness timeout")


def _browser_environment(dsn: str, config: DatasetConfig, port: int, fixture_root: Path, episode_id: str) -> dict[str, str]:
    scope = scopes_for(config)[0]
    (fixture_root / "ephi_o10_benchmark_source.py").write_text(
        "from ephi.application import AccessScope, MetrologySourceBinding\n"
        "class BenchmarkFixture:\n"
        "    def describe(self):\n"
        f"        scope = AccessScope({scope.scope_id!r}, site_id={scope.site_id!r}, area_id={scope.area_id!r}, family_id={scope.family_id!r})\n"
        "        return MetrologySourceBinding(scope, 'o10-benchmark-source', 'o10-benchmark-provider', 'benchmark-family-01', 'o10-benchmark-capability', 'ephi_o10_benchmark_source:factory', 'o10-benchmark-schema-v1', 'o10-benchmark-mapping-v1', 'b' * 64, 'mm', 'o10-benchmark-reference')\n"
        "    def read_partition(self, *args, **kwargs):\n"
        "        return ()\n"
        "def factory():\n"
        "    return BenchmarkFixture()\n",
        encoding="utf-8",
    )
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(fixture_root))),
        "EPHI_ENV": "test",
        "EPHI_HOST": "127.0.0.1",
        "EPHI_PORT": str(port),
        "EPHI_ALLOWED_BROWSER_ORIGINS": f"http://127.0.0.1:{port}",
        "EPHI_POSTGRES_DSN": dsn,
        "NICEGUI_BASE_STORAGE_SECRET": "o10-benchmark-secret-not-recorded",
        "NICEGUI_STORAGE_PATH": str(fixture_root / "nicegui-storage"),
        "EPHI_DEV_SCOPE_ID": scopes_for(config)[0].scope_id,
        "EPHI_DEV_SITE_ID": "benchmark-site",
        "EPHI_DEV_AREA_ID": "benchmark-area",
        "EPHI_DEV_FAMILY_ID": "benchmark-family-01",
        "EPHI_DEV_IDENTITY_SUBJECT": "benchmark-browser-engineer",
        "EPHI_DEV_IDENTITY_CAPABILITIES": "ephi.attention.read,ephi.episode.read,ephi.episode.claim,ephi.episode.acknowledge",
        "EPHI_DEV_AUTH_SESSION_REVISION": "1",
        "EPHI_DEV_SECURITY_REVISION": "1",
        "EPHI_W1_EPISODE_ID": episode_id,
        "EPHI_METROLOGY_SOURCE_ADAPTER": "ephi_o10_benchmark_source:factory",
        "EPHI_METROLOGY_SOURCE_ID": "o10-benchmark-source",
        "EPHI_METROLOGY_PROVIDER_ID": "o10-benchmark-provider",
        "EPHI_METROLOGY_FAMILY_ID": "benchmark-family-01",
        "EPHI_METROLOGY_CAPABILITY_ID": "o10-benchmark-capability",
        "EPHI_METROLOGY_SCOPE_ID": scopes_for(config)[0].scope_id,
        "EPHI_METROLOGY_SITE_ID": "benchmark-site",
        "EPHI_METROLOGY_AREA_ID": "benchmark-area",
        "EPHI_METROLOGY_SCHEMA_ID": "o10-benchmark-schema-v1",
        "EPHI_METROLOGY_MAPPING_VERSION": "o10-benchmark-mapping-v1",
        "EPHI_METROLOGY_MAPPING_HASH": "b" * 64,
        "EPHI_METROLOGY_UNIT": "mm",
        "EPHI_METROLOGY_REFERENCE_POPULATION_ID": "o10-benchmark-reference",
    }


async def _browser_one(page: Any, base: str, episode_id: str) -> dict[str, object]:
    started = time.perf_counter_ns()
    received = 0
    frame_count = 0
    errors: list[str] = []
    failures: list[str] = []

    def websocket(ws: Any) -> None:
        def frame(payload: Any) -> None:
            nonlocal received, frame_count
            frame_count += 1
            received += len(payload) if isinstance(payload, (bytes, bytearray)) else len(str(payload).encode("utf-8"))
        ws.on("framereceived", frame)

    page.on("websocket", websocket)
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("requestfailed", lambda request: failures.append(request.url))
    await page.goto(base + "/", wait_until="domcontentloaded")
    await page.get_by_role("heading", name="Attention").wait_for()
    await page.wait_for_function("""() => [...document.querySelectorAll('[role=status]')].some(node => { const t=(node.innerText||'').trim(); return t.includes('permitted Attention row'); })""")
    await page.locator('[aria-label="Attention results table"]').wait_for()
    await page.wait_for_function("""() => { const table=document.querySelector('[aria-label="Attention results table"]'); return Boolean(table && ((table.innerText||'').includes('bench-episode-') || (table.innerText||'').includes('No rows'))); }""")
    attention_ms = round((time.perf_counter_ns() - started) / 1_000_000, 3)
    attention_bytes = received
    episode_started = time.perf_counter_ns()
    received = 0
    frame_count = 0
    await page.goto(base + "/episode", wait_until="domcontentloaded")
    await page.get_by_role("heading", name="Episode decision brief").wait_for()
    await page.wait_for_function(
        """(episodeId) => { const text=document.body.innerText||''; return text.includes(episodeId) && text.includes('Workflow state') && text.includes('Source / capability state'); }""",
        arg=episode_id,
    )
    await page.wait_for_function("""() => { const text=document.body.innerText||''; return text.includes('Workflow state') && text.includes('Source / capability state') && (text.includes('Claim episode') || text.includes('Acknowledge episode') || text.includes('No action available')); }""")
    episode_ms = round((time.perf_counter_ns() - episode_started) / 1_000_000, 3)
    episode_bytes = received
    return {
        "attention_useful_paint_ms": attention_ms,
        "episode_useful_paint_ms": episode_ms,
        "attention_application_websocket_bytes": attention_bytes,
        "episode_application_websocket_bytes": episode_bytes,
        "application_websocket_frame_count": frame_count,
        "console_errors": [safe_digest(item) for item in errors],
        "failed_network_count": len(failures),
        "useful_paint_predicate": "heading + authoritative non-loading status + populated/empty table; Episode identity + workflow/source facts + eligible action/no-action",
    }


async def _browser_cohort_async(base: str, sessions: int, episode_id: str) -> dict[str, object]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        contexts = [await browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1) for _ in range(sessions + 1)]
        try:
            # Warm one isolated context and exclude it from the measured cohort.
            warm = contexts.pop()
            warm_page = await warm.new_page()
            await warm_page.goto(base + "/", wait_until="domcontentloaded")
            await warm.close()
            pages = [await context.new_page() for context in contexts]
            results = await asyncio.gather(*[_browser_one(page, base, episode_id) for page in pages], return_exceptions=True)
            samples = []
            for index, result in enumerate(results):
                if isinstance(result, Exception):
                    samples.append({"session": index, "status": "failure", "error_type": type(result).__name__, "error_digest": safe_digest(result)})
                else:
                    samples.append({"session": index, "status": "ok", **result})
            cookies = []
            for context in contexts:
                for cookie in await context.cookies():
                    if cookie.get("name") == "session":
                        cookies.append(safe_digest(cookie.get("value", "")))
            attention_times = [float(item["attention_useful_paint_ms"]) for item in samples if item.get("status") == "ok"]
            episode_times = [float(item["episode_useful_paint_ms"]) for item in samples if item.get("status") == "ok"]
            attention_payload = [int(item["attention_application_websocket_bytes"]) for item in samples if item.get("status") == "ok"]
            episode_payload = [int(item["episode_application_websocket_bytes"]) for item in samples if item.get("status") == "ok"]
            errors = [item for item in samples if item.get("status") != "ok" or item.get("console_errors") or item.get("failed_network_count")]
            return {
                "status": "PASS" if len(samples) == sessions and not errors and attention_times and episode_times else "FAIL",
                "session_count": sessions,
                "distinct_session_cookie_fingerprint_count": len(set(cookies)),
                "attention_useful_paint_ms": _distribution(attention_times),
                "episode_useful_paint_ms": _distribution(episode_times),
                "attention_payload_bytes": _distribution([float(value) for value in attention_payload]),
                "episode_payload_bytes": _distribution([float(value) for value in episode_payload]),
                "samples": samples,
                "console_or_network_failures": len(errors),
                "headless_engine": "Chromium via Playwright; foreground desktop/CUA not used",
            }
        finally:
            for context in contexts:
                await context.close()
            await browser.close()


def run_browser_cohort(dsn: str, config: DatasetConfig, *, port: int, sessions: int, repetitions: int, output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    episode_id = "bench-episode-00001"
    repetitions_out = []
    for repetition in range(1, repetitions + 1):
        with tempfile.TemporaryDirectory(prefix="ephi-o10-browser-") as temp:
            fixture_root = Path(temp)
            environment = _browser_environment(dsn, config, port, fixture_root, episode_id)
            log_path = output_dir / f"web-repetition-{repetition}.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen([sys.executable, "-m", "ephi", "--serve"], cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, text=True)
            try:
                _wait_port(port, process)
                result = asyncio.run(_browser_cohort_async(f"http://127.0.0.1:{port}", sessions, episode_id))
                result["repetition"] = repetition
                repetitions_out.append(result)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    attention = [float(item["attention_useful_paint_ms"]["p95_ms"]) for item in repetitions_out if item.get("attention_useful_paint_ms", {}).get("p95_ms") is not None]
    episode = [float(item["episode_useful_paint_ms"]["p95_ms"]) for item in repetitions_out if item.get("episode_useful_paint_ms", {}).get("p95_ms") is not None]
    payload_attention = [float(item["attention_payload_bytes"]["max_ms"]) for item in repetitions_out if item.get("attention_payload_bytes", {}).get("max_ms") is not None]
    payload_episode = [float(item["episode_payload_bytes"]["max_ms"]) for item in repetitions_out if item.get("episode_payload_bytes", {}).get("max_ms") is not None]
    status = all(item.get("status") == "PASS" for item in repetitions_out) and len({item.get("distinct_session_cookie_fingerprint_count") for item in repetitions_out}) == 1 and all(value <= BUDGETS["attention_useful_page_p95_ms"] for value in attention) and all(value <= BUDGETS["episode_useful_page_p95_ms"] for value in episode) and all(value <= BUDGETS["attention_payload_bytes"] for value in payload_attention) and all(value <= BUDGETS["episode_payload_bytes"] for value in payload_episode)
    return {"status": "PASS" if status else "FAIL", "repetition_count": repetitions, "repetitions": repetitions_out, "browser_predicate": "authorized heading + authoritative status + coherent rows/table; Episode identity + workflow/source facts + action/no-action", "budget": {"status": "PASS" if status else "FAIL", "attention_useful_page_p95_budget_ms": BUDGETS["attention_useful_page_p95_ms"], "episode_useful_page_p95_budget_ms": BUDGETS["episode_useful_page_p95_ms"], "attention_payload_max_budget_bytes": BUDGETS["attention_payload_bytes"], "episode_payload_max_budget_bytes": BUDGETS["episode_payload_bytes"]}}


def _reset_browser_restart_episode(dsn: str, config: DatasetConfig, episode_id: str) -> None:
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        connection = adapter.connection
        connection.execute("DELETE FROM command_receipt WHERE aggregate_type = 'episode_workflow' AND aggregate_id = %s", (episode_id,))
        connection.execute("DELETE FROM audit_event WHERE aggregate_type = 'episode_workflow' AND aggregate_id = %s", (episode_id,))
        connection.execute("DELETE FROM outbox_event WHERE aggregate_type = 'episode_workflow' AND aggregate_id = %s", (episode_id,))
        connection.execute(
            "UPDATE aggregate_state SET version = 0, state_json = %s::jsonb WHERE scope_key = %s AND aggregate_type = 'episode_workflow' AND aggregate_id = %s",
            (json.dumps({"owner": None, "work_state": "OPEN", "claimed_at": None, "acknowledged_at": None}, separators=(",", ":")), scopes_for(config)[0].canonical_key, episode_id),
        )
    finally:
        adapter.close()


async def _browser_claim_once(base: str, episode_id: str) -> dict[str, object]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
        page = await context.new_page()
        try:
            await page.goto(base + "/episode", wait_until="domcontentloaded")
            await page.wait_for_function("""(episodeId) => { const text=document.body.innerText||''; return text.includes(episodeId) && text.includes('Workflow state') && text.includes('Source / capability state'); }""", arg=episode_id)
            claim = page.get_by_role("button", name="Claim episode")
            await claim.click(force=True)
            await page.wait_for_function("""() => (document.body.innerText||'').includes('Acknowledge episode')""")
            return {"status": "PASS", "claim_acknowledged_in_browser": True, "console_errors": []}
        finally:
            await context.close()
            await browser.close()


def web_crash_restart_probe(dsn: str, config: DatasetConfig, *, port: int, repetitions: int, output_dir: Path) -> dict[str, object]:
    # Keep the reserved browser row in family partition 01, the synthetic
    # browser principal's exact authorized scope.
    episode_id = f"bench-episode-{config.attention_items - 9:05d}"
    results = []
    for repetition in range(1, repetitions + 1):
        _reset_browser_restart_episode(dsn, config, episode_id)
        with tempfile.TemporaryDirectory(prefix="ephi-o10-crash-restart-") as temp:
            root = Path(temp)
            environment = _browser_environment(dsn, config, port, root, episode_id)
            log_path = output_dir / f"crash-restart-{repetition}.log"
            output_dir.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen([sys.executable, "-m", "ephi", "--serve"], cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, text=True)
            try:
                _wait_port(port, process)
                first = asyncio.run(_browser_claim_once(f"http://127.0.0.1:{port}", episode_id))
                process.terminate()
                process.wait(timeout=10)
                second_process = subprocess.Popen([sys.executable, "-m", "ephi", "--serve"], cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, text=True)
                try:
                    _wait_port(port, second_process)
                    async def replay_page() -> bool:
                        from playwright.async_api import async_playwright
                        async with async_playwright() as playwright:
                            browser = await playwright.chromium.launch(headless=True)
                            context = await browser.new_context(viewport={"width": 1280, "height": 720})
                            page = await context.new_page()
                            try:
                                await page.goto(f"http://127.0.0.1:{port}/episode", wait_until="domcontentloaded")
                                await page.wait_for_function("""(episodeId) => { const text=document.body.innerText||''; return text.includes(episodeId) && text.includes('Acknowledge episode'); }""", arg=episode_id)
                                return True
                            finally:
                                await context.close()
                                await browser.close()
                    replay_visible = asyncio.run(replay_page())
                finally:
                    second_process.terminate()
                    try:
                        second_process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        second_process.kill()
                        second_process.wait(timeout=10)
                adapter = PostgreSQLReferenceTransactionAdapter(dsn)
                try:
                    connection = adapter.connection
                    receipt_count = int(connection.execute("SELECT count(*) AS count FROM command_receipt WHERE aggregate_type = 'episode_workflow' AND aggregate_id = %s", (episode_id,)).fetchone()["count"])
                    effect_count = int(connection.execute("SELECT count(*) AS count FROM audit_event WHERE aggregate_type = 'episode_workflow' AND aggregate_id = %s", (episode_id,)).fetchone()["count"])
                finally:
                    adapter.close()
                results.append({"repetition": repetition, "status": "PASS" if first["status"] == "PASS" and replay_visible and receipt_count == effect_count == 1 else "FAIL", "replay_action_visible": replay_visible, "receipt_count": receipt_count, "audit_count": effect_count, "acknowledged_effects_lost": 0 if receipt_count == 1 and effect_count == 1 else 1})
            except Exception as exc:
                results.append({"repetition": repetition, "status": "FAIL", "error_type": type(exc).__name__, "error_digest": safe_digest(exc)})
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
    return {"status": "PASS" if len(results) == repetitions and all(item["status"] == "PASS" for item in results) else "FAIL", "repetition_count": repetitions, "repetitions": results, "browser_restart": True}


def resilience_scenarios(dsn: str, config: DatasetConfig, *, repetitions: int, port: int, output_dir: Path, run_browser: bool) -> dict[str, object]:
    return {
        "source_degraded": source_degraded_probe(dsn, config, repetitions),
        "worker_starvation": worker_starvation_probe(dsn, config, repetitions),
        "web_crash_restart": web_crash_restart_probe(dsn, config, port=port, repetitions=repetitions, output_dir=output_dir / "crash-restart") if run_browser else {"status": "NOT_RUN", "repetition_count": 0, "reason": "--skip-browser"},
        "restore": {"status": "NOT_RUN", "repetition_count": 0, "reason": "Full logical dump/isolated restore is an explicit executor action; use --run-restore with pg_dump/createdb authority. production_disaster_rpo_rto_claim=NOT_ESTABLISHED"},
    }


def _git_facts(repo: Path) -> dict[str, object]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()
    return {
        "head_sha": git("rev-parse", "HEAD"),
        "tree_sha": git("rev-parse", "HEAD^{tree}"),
        "branch": git("branch", "--show-current"),
        "worktree_clean": not bool(git("status", "--porcelain")),
    }


def changed_file_inventory(repo: Path, base_sha: str, candidate_sha: str) -> list[dict[str, object]]:
    paths = subprocess.check_output(["git", "diff", "--name-only", f"{base_sha}..{candidate_sha}"], cwd=repo, text=True).splitlines()
    inventory = []
    for path in paths:
        file_path = repo / path
        data = file_path.read_bytes()
        inventory.append({"path": path, "bytes": len(data), "sha256": sha256_bytes(data)})
    return inventory


def write_report(output_dir: Path, report: Mapping[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "benchmark_report.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def contract_only(output_dir: Path) -> dict[str, object]:
    report = {
        "schema_version": "ephi-o10.2-performance-capacity.v1",
        "status": "BLOCKED_BENCHMARK_ENVIRONMENT",
        "qualification_state": "BLOCKED_BENCHMARK_ENVIRONMENT",
        "operation": "BUILD",
        "project": "ephi",
        "request": REQUEST,
        "fabric_job_id": FABRIC_JOB_ID,
        "base_sha": BASE_SHA,
        "work_branch": WORK_BRANCH,
        "envelope": ENVELOPE,
        "budgets": BUDGETS,
        "workload_definitions": {
            "attention_warm": {"rate_per_second": 20, "page_size": 50, "steady_state_repetitions": 3},
            "attention_burst": ENVELOPE["foreground_burst"],
            "attention_filter_search": {"rate_per_second": 20, "debounce_exercised": True, "pushdown": True},
            "episode_brief": {"rate_per_second": 20, "coherent_current_read": True},
            "workflow_command": {"rate_per_minute": 100, "unique_command_ids": True, "distinct_episode_ranges": True},
            "browser": {"concurrent_isolated_contexts": 100, "transport": "NiceGUI/Engine.IO WebSocket"},
        },
        "percentile_method": "nearest_rank; sorted; rank=max(1,ceil(q*n)); no interpolation",
        "dataset_contract": asdict(DatasetConfig()),
        "optimization": {
            "status": "APPLIED_AFTER_PROFILING",
            "repair": "PostgreSQL retained query-snapshot members use one cursor.executemany batch instead of one execute call per row.",
            "measured_bottleneck": "diagnostic profile observed one Attention page causing 334 individual snapshot-member execute calls before the repair",
            "index_change": "NONE; current scoped Attention index was used by the diagnostic EXPLAIN plan",
            "qualification_boundary": "diagnostic local measurements do not establish production-like capacity",
        },
        "execution": {"status": "NOT_RUN", "reason": "No qualifying measured Linux/container executor or DSN was supplied in this Fabric environment; contract/math harness remains available."},
        "secret_safety": {"cookies": False, "storage_values": False, "auth_headers": False, "dsns": False, "protected_payloads": False, "raw_identity_values": False},
        "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
        "scope_boundary": "O10.2 current Attention/Episode W1 only; no Assets, Outcomes, Family Center, global search or evidence-panel surfaces added.",
    }
    write_report(output_dir, report)
    return report


def run(args: argparse.Namespace) -> dict[str, object]:
    output_dir = Path(args.output).resolve()
    if args.contract_only:
        return contract_only(output_dir)
    if not args.dsn:
        raise SystemExit("--dsn is required unless --contract-only is used")
    config = DatasetConfig(seed=args.seed, family_partitions=args.family_partitions, attention_items=args.attention_items, archived_read_records=args.archived_read_records)
    if config.attention_items != ENVELOPE["open_attention_items"] or config.archived_read_records != ENVELOPE["archived_episode_read_records"] or config.family_partitions != ENVELOPE["family_partitions"]:
        raise SystemExit("O10.2 qualification requires the exact N1 dataset envelope; use contract-only for math tests")
    seeded = seed_database(args.dsn, config, reset=True)
    profile = profile_database(args.dsn, config)
    service = attach_service_budgets(run_service_workloads(args.dsn, config, repetitions=args.repetitions, duration_seconds=args.duration_seconds))
    durability = durability_probe(args.dsn, config)
    conflict = conflict_probe(args.dsn, config)
    browser = {"status": "NOT_RUN", "reason": "--skip-browser"}
    if not args.skip_browser:
        browser = run_browser_cohort(args.dsn, config, port=args.port, sessions=args.browser_sessions, repetitions=args.repetitions, output_dir=output_dir / "browser")
    resilience = resilience_scenarios(
        args.dsn,
        config,
        repetitions=args.repetitions,
        port=args.port + 1,
        output_dir=output_dir,
        run_browser=not args.skip_browser,
    )
    if args.run_restore:
        resilience["restore"] = restore_rehearsals(
            args.dsn,
            args.admin_dsn,
            repetitions=args.repetitions,
            output_dir=output_dir / "restore",
            target_database_prefix=args.target_database_prefix,
        )
    environment = _resource_facts(args.dsn)
    if args.environment_facts:
        environment.update(json.loads(Path(args.environment_facts).read_text(encoding="utf-8")))
    scenario_results = {name: service[name] for name in ("attention_warm", "attention_burst", "attention_filter_search", "episode_brief", "workflow_command")}
    qualification = qualify_benchmark(environment, scenario_results, browser, durability, resilience)
    report = {
        "schema_version": "ephi-o10.2-performance-capacity.v1",
        "status": qualification["state"],
        "qualification_state": qualification["state"],
        "operation": "BUILD",
        "project": "ephi",
        "request": REQUEST,
        "fabric_job_id": FABRIC_JOB_ID,
        "base_sha": BASE_SHA,
        "work_branch": WORK_BRANCH,
        "envelope": ENVELOPE,
        "budgets": BUDGETS,
        "workload_definitions": {
            "attention_warm": {"rate_per_second": 20, "page_size": 50, "steady_state_repetitions": args.repetitions},
            "attention_burst": ENVELOPE["foreground_burst"],
            "attention_filter_search": {"rate_per_second": 20, "debounce_exercised": True, "pushdown": True},
            "episode_brief": {"rate_per_second": 20, "coherent_current_read": True},
            "workflow_command": {"rate_per_minute": 100, "unique_command_ids": True, "distinct_episode_ranges": True},
            "browser": {"concurrent_isolated_contexts": args.browser_sessions, "transport": "NiceGUI/Engine.IO WebSocket"},
        },
        "environment": environment,
        "environment_decision": qualification.get("environment"),
        "dataset": seeded,
        "profiling": profile,
        "optimization": {
            "status": "APPLIED_AFTER_PROFILING",
            "repair": "PostgreSQL retained query-snapshot members use one cursor.executemany batch instead of one execute call per row.",
            "index_change": "NONE; current scoped Attention index was used by EXPLAIN",
        },
        "service_workloads": service,
        "browser_session_workloads": browser,
        "workflow_conflicts": conflict,
        "durability": durability,
        "resilience": resilience,
        "resource_sampling": [_resource_facts(args.dsn)],
        "percentile_method": "nearest_rank; sorted; rank=max(1,ceil(q*n)); no interpolation",
        "secret_safety": {"cookies": False, "storage_values": False, "auth_headers": False, "dsns": False, "protected_payloads": False, "raw_identity_values": False},
        "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
        "scope_boundary": "O10.2 current Attention/Episode W1 only; no Assets, Outcomes, Family Center, global search or evidence-panel surfaces added.",
    }
    write_report(output_dir, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("EPHI_TEST_POSTGRES_DSN", ""))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contract-only", action="store_true")
    parser.add_argument("--environment-facts", type=Path)
    parser.add_argument("--seed", type=int, default=161)
    parser.add_argument("--family-partitions", type=int, default=30)
    parser.add_argument("--attention-items", type=int, default=10_000)
    parser.add_argument("--archived-read-records", type=int, default=1_000_000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--browser-sessions", type=int, default=100)
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--run-restore", action="store_true")
    parser.add_argument("--admin-dsn", default=os.environ.get("EPHI_TEST_POSTGRES_ADMIN_DSN", ""))
    parser.add_argument("--target-database-prefix", default="ephi_o10_restore")
    args = parser.parse_args(argv)
    if args.repetitions < 3 and not args.contract_only:
        parser.error("O10.2 requires at least 3 steady-state repetitions")
    try:
        report = run(args)
    except Exception as exc:
        report = {"schema_version": "ephi-o10.2-performance-capacity.v1", "status": "BLOCKED_BENCHMARK_ENVIRONMENT", "qualification_state": "BLOCKED_BENCHMARK_ENVIRONMENT", "error_type": type(exc).__name__, "error_digest": safe_digest(exc), "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED"}
        write_report(Path(args.output).resolve(), report)
        print(json.dumps({"status": report["status"], "error_type": report["error_type"]}, sort_keys=True))
        return 2
    print(json.dumps({"status": report["status"], "output": str(Path(args.output).resolve())}, sort_keys=True))
    return 0 if report.get("status") in {"PASS_CURRENT_SURFACE_BUDGETS", "BLOCKED_BENCHMARK_ENVIRONMENT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
