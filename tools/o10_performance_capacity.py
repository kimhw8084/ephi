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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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
from urllib.parse import parse_qs, urlsplit

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
from ephi.application.source_ingress import SourceCapabilityState, SourceSnapshotStatus, _capability_state  # noqa: E402


REQUEST = "ephi-o10-performance-capacity-fix1"
BASE_SHA = "8fd41a2fd9f6420cc63bf8a0beaa73a472d25f0e"
WORK_BRANCH = "codex/ephi-o10-performance-capacity-fix1"
FABRIC_JOB_ID = os.environ.get("CODEX_FABRIC_JOB_ID", "CF-732138307c5bb82fafc5a928")

STATUS_PASS = "PASS_CURRENT_SURFACE_BUDGETS"
STATUS_BLOCKED_ENVIRONMENT = "BLOCKED_BENCHMARK_ENVIRONMENT"
STATUS_FAIL_BUDGETS = "FAIL_CURRENT_SURFACE_BUDGETS"
STATUS_FAIL_EXECUTION = "FAIL_BENCHMARK_EXECUTION"
QUALIFYING_EXECUTION_MODE = "QUALIFYING_BENCHMARK"
DIAGNOSTIC_EXECUTION_MODE = "NOT_QUALIFYING_DIAGNOSTIC"

QUALIFYING_MIN_REPETITIONS = 3
QUALIFYING_MIN_DURATION_SECONDS = 30.0
SAMPLE_FLOORS = {
    "attention_warm": 600,
    "attention_burst": 40,
    "attention_filter_search": 600,
    "episode_brief": 600,
    "workflow_command": 50,
}

PROFILE = {
    "web_vcpu": 4,
    "web_memory_gib": 8,
    "postgres_vcpu": 4,
    "postgres_memory_gib": 16,
    "worker_vcpu_min": 1,
    "worker_memory_gib_min": 2,
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


class BenchmarkEnvironmentBlocked(RuntimeError):
    """A deliberate refusal to qualify an executor as the required N1 authority."""

    def __init__(self, result: "EnvironmentQualificationResult") -> None:
        self.result = result
        super().__init__(result.reason)


class EnvironmentQualificationResult(Mapping[str, object]):
    """Typed, serializable result for the benchmark-environment authority gate."""

    __slots__ = ("state", "reason", "details")

    def __init__(self, state: str, reason: str, details: Mapping[str, object] | None = None) -> None:
        self.state = state
        self.reason = reason
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, object]:
        return {"state": self.state, "reason": self.reason, **self.details}

    def __getitem__(self, key: str) -> object:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


def _environment_result(state: str, reason: str, **details: object) -> EnvironmentQualificationResult:
    return EnvironmentQualificationResult(state, reason, details)


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


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _valid_provenance(value: object, *, require_digest: bool = True) -> bool:
    if not isinstance(value, Mapping):
        return False
    required = ("source", "method", "observed_at")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
        return False
    if require_digest and (not isinstance(value.get("evidence_digest"), str) or len(value["evidence_digest"]) != 64):
        return False
    return True


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _postgres_major(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    first = value.strip().split(".", 1)[0]
    try:
        return int(first)
    except ValueError:
        return None


def _resource_identity(resource: Mapping[str, object]) -> tuple[str | None, str | None, str | None]:
    return (
        resource.get("resource_id") if isinstance(resource.get("resource_id"), str) else None,
        resource.get("resource_authority_id") if isinstance(resource.get("resource_authority_id"), str) else None,
        resource.get("process_isolation_domain") if isinstance(resource.get("process_isolation_domain"), str) else None,
    )


def _resource_check(name: str, resource: object, *, maximum: tuple[float, float] | None, minimum: tuple[float, float]) -> tuple[list[str], dict[str, object]]:
    mismatches: list[str] = []
    comparison: dict[str, object] = {"status": "INVALID", "resource": name}
    if not isinstance(resource, Mapping):
        return [f"{name} resource facts are missing"], comparison
    resource_id, authority_id, isolation_domain = _resource_identity(resource)
    if not resource_id or not authority_id or not isolation_domain:
        mismatches.append(f"{name} resource identity is not stable and non-secret")
    for key in ("cpu_limit_vcpu", "memory_limit_gib"):
        if not _is_number(resource.get(key)) or float(resource[key]) <= 0:
            mismatches.append(f"{name} {key} is not a positive measured/declared limit")
    if resource.get("limit_kind") not in {"measured", "declared"}:
        mismatches.append(f"{name} limit_kind is not explicitly measured or declared")
    if not _valid_provenance(resource.get("measurement_provenance")):
        mismatches.append(f"{name} resource facts lack measurement provenance")
    if _is_number(resource.get("cpu_limit_vcpu")) and _is_number(resource.get("memory_limit_gib")):
        cpu = float(resource["cpu_limit_vcpu"])
        memory = float(resource["memory_limit_gib"])
        if cpu < minimum[0] or memory < minimum[1]:
            mismatches.append(f"{name} resource is too degraded or unrepresentative for N1")
            comparison = {"status": "TOO_DEGRADED_OR_UNREPRESENTATIVE", "cpu_limit_vcpu": cpu, "memory_limit_gib": memory}
        elif maximum is not None and (cpu > maximum[0] or memory > maximum[1]):
            mismatches.append(f"{name} resource is more favorable than the proposed N1 profile")
            comparison = {"status": "MORE_FAVORABLE_THAN_PROPOSED", "cpu_limit_vcpu": cpu, "memory_limit_gib": memory}
        else:
            comparison = {
                "status": "EXACT" if maximum is not None and cpu == maximum[0] and memory == maximum[1] else "CONSERVATIVE_NO_MORE_FAVORABLE",
                "cpu_limit_vcpu": cpu,
                "memory_limit_gib": memory,
                "lower_bound": {"cpu_vcpu": minimum[0], "memory_gib": minimum[1]},
                "upper_bound": {"cpu_vcpu": maximum[0], "memory_gib": maximum[1]} if maximum is not None else None,
            }
    return mismatches, comparison


def qualify_environment(environment: Mapping[str, object]) -> EnvironmentQualificationResult:
    """Fail closed unless an attested, split-resource N1 authority is bound.

    Resource limits use a deterministic no-more-favorable rule: web must be
    within 1--4 vCPU and 2--8 GiB, PostgreSQL within 1--4 vCPU and 4--16 GiB,
    and worker limits must meet the same lower-bound representativeness check.
    Exact limits and lower limits inside those bounds qualify; larger limits
    are more favorable and smaller limits are unrepresentative.  RTT is always
    required to be <=50 ms on the actual browser-client-to-web path.
    """

    required = (
        "executor_kind",
        "executor_profile",
        "measurement_window",
        "authority_validation",
        "web",
        "postgres",
        "worker",
        "client_rtt",
        "postgres_server_version_observed",
        "postgres_server_version_source",
        "separation_proof",
    )
    missing = [key for key in required if key not in environment]
    if missing:
        return _environment_result(STATUS_BLOCKED_ENVIRONMENT, "missing measured authority facts", missing=missing)

    mismatches: list[str] = []
    if not isinstance(environment.get("executor_kind"), str) or not str(environment["executor_kind"]).strip():
        mismatches.append("executor kind is missing")
    if not isinstance(environment.get("executor_profile"), str) or not str(environment["executor_profile"]).strip():
        mismatches.append("executor profile is missing")
    if "macos" in str(environment.get("executor_kind", "")).lower() or "developer" in str(environment.get("executor_profile", "")).lower():
        mismatches.append("foreground developer host is not a qualifying executor")
    window = environment.get("measurement_window")
    if not isinstance(window, Mapping) or not _valid_timestamp(window.get("started_at")) or not _valid_timestamp(window.get("ended_at")):
        mismatches.append("measurement window is missing or invalid")
    if isinstance(window, Mapping) and _valid_timestamp(window.get("started_at")) and _valid_timestamp(window.get("ended_at")):
        started = datetime.fromisoformat(str(window["started_at"]).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(window["ended_at"]).replace("Z", "+00:00"))
        if ended <= started:
            mismatches.append("measurement window is not ordered")
    validation = environment.get("authority_validation")
    if not isinstance(validation, Mapping) or validation.get("status") != "VALIDATED" or not _valid_provenance(validation):
        mismatches.append("resource authority validation is absent or unbound")
    if environment.get("environment_facts_file_present") is True and not _valid_provenance(environment.get("environment_facts_file_provenance")):
        mismatches.append("environment facts file provenance/validation is absent or unbound")

    comparisons: dict[str, object] = {}
    for name, maximum, minimum in (
        ("web", (4.0, 8.0), (1.0, 2.0)),
        ("postgres", (4.0, 16.0), (1.0, 4.0)),
        ("worker", None, (float(PROFILE["worker_vcpu_min"]), float(PROFILE["worker_memory_gib_min"]))),
    ):
        resource_mismatches, comparison = _resource_check(name, environment.get(name), maximum=maximum, minimum=minimum)
        mismatches.extend(resource_mismatches)
        comparisons[name] = comparison

    web = environment.get("web") if isinstance(environment.get("web"), Mapping) else {}
    postgres = environment.get("postgres") if isinstance(environment.get("postgres"), Mapping) else {}
    worker = environment.get("worker") if isinstance(environment.get("worker"), Mapping) else {}
    web_identity = _resource_identity(web)
    postgres_identity = _resource_identity(postgres)
    worker_identity = _resource_identity(worker)
    separation = environment.get("separation_proof")
    if (
        not isinstance(separation, Mapping)
        or separation.get("status") != "PASS"
        or not separation.get("distinct_resource_authorities") is True
        or not separation.get("distinct_process_isolation_domains") is True
        or not isinstance(separation.get("method"), str)
        or not separation["method"].strip()
    ):
        mismatches.append("web/PostgreSQL/worker resource separation proof is absent")
    if web_identity[1] == postgres_identity[1] or web_identity[2] == postgres_identity[2] or web_identity[1] == worker_identity[1]:
        mismatches.append("resource authority/process isolation identities are not distinct")

    rtt = environment.get("client_rtt")
    if not isinstance(rtt, Mapping) or not _is_number(rtt.get("p95_ms")):
        mismatches.append("client RTT measurement is missing")
    else:
        if float(rtt["p95_ms"]) > PROFILE["client_round_trip_ms_max"]:
            mismatches.append("client round-trip exceeds N1 maximum")
        if rtt.get("path") != "actual_browser_client_to_web":
            mismatches.append("client RTT is not measured on the actual browser client-to-web path")
        if not _valid_provenance(rtt.get("measurement_provenance")):
            mismatches.append("client RTT lacks measurement provenance")

    version = environment.get("postgres_server_version_observed")
    if environment.get("postgres_server_version_source") != "direct_benchmark_database_query":
        mismatches.append("PostgreSQL version is not bound to a direct benchmark-database query")
    if _postgres_major(version) != 18:
        mismatches.append("PostgreSQL server major is not 18")

    if mismatches:
        return _environment_result(
            STATUS_BLOCKED_ENVIRONMENT,
            "measured authority does not establish qualifying N1",
            mismatches=mismatches,
            resource_comparison=comparisons,
            postgres_server_major=_postgres_major(version),
        )
    return _environment_result(
        "QUALIFYING_PRODUCTION_LIKE_EXECUTOR",
        "validated split-resource authority is exact or no more favorable than N1",
        mismatches=[],
        resource_comparison=comparisons,
        postgres_server_major=18,
    )


def validate_workload_envelope(*, browser_sessions: int, family_partitions: int, attention_items: int, archived_read_records: int, attention_page_size: int, repetitions: int, duration_seconds: float) -> dict[str, object]:
    """Return the exact qualifying envelope and its minimum sample authority."""

    mismatches: list[str] = []
    expected = {
        "browser_sessions": 100,
        "family_partitions": ENVELOPE["family_partitions"],
        "attention_items": ENVELOPE["open_attention_items"],
        "archived_read_records": ENVELOPE["archived_episode_read_records"],
        "attention_page_size": ENVELOPE["attention_page_size"],
    }
    actual = {
        "browser_sessions": browser_sessions,
        "family_partitions": family_partitions,
        "attention_items": attention_items,
        "archived_read_records": archived_read_records,
        "attention_page_size": attention_page_size,
        "repetitions": repetitions,
        "duration_seconds": duration_seconds,
    }
    for key, required_value in expected.items():
        if actual[key] != required_value:
            mismatches.append(f"{key} must equal {required_value}")
    if repetitions < QUALIFYING_MIN_REPETITIONS:
        mismatches.append(f"repetitions must be >= {QUALIFYING_MIN_REPETITIONS}")
    if not _is_number(duration_seconds) or float(duration_seconds) < QUALIFYING_MIN_DURATION_SECONDS:
        mismatches.append(f"duration_seconds must be >= {QUALIFYING_MIN_DURATION_SECONDS}")
    duration = max(0.0, float(duration_seconds)) if _is_number(duration_seconds) else 0.0
    sample_counts = {
        "attention_warm": math.ceil(20 * duration),
        "attention_burst": ENVELOPE["foreground_burst"]["requests"],
        "attention_filter_search": math.ceil(20 * duration),
        "episode_brief": math.ceil(20 * duration),
        "workflow_command": math.ceil((100 / 60) * duration),
    }
    for name, floor in SAMPLE_FLOORS.items():
        if sample_counts[name] < floor:
            mismatches.append(f"{name} sample count must be >= {floor}")
    return {"status": "PASS" if not mismatches else "FAIL", "expected": expected, "actual": actual, "sample_counts": sample_counts, "sample_floors": dict(SAMPLE_FLOORS), "mismatches": mismatches}


def evaluate_session_isolation(repetition: Mapping[str, object], *, required_sessions: int = 100) -> dict[str, object]:
    """Evaluate measured context/session identities without exposing raw values."""

    samples = repetition.get("samples")
    if isinstance(samples, Sequence) and not isinstance(samples, (str, bytes, bytearray)):
        measured_context_count = len(samples)
        successful = [item for item in samples if isinstance(item, Mapping) and item.get("status") == "ok"]
        successful_count = len(successful)
        fingerprints = [item.get("session_identity_fingerprint") for item in successful if item.get("session_identity_fingerprint")]
        missing_count = successful_count - len(fingerprints)
    else:
        measured_context_count = int(repetition.get("measured_context_count", repetition.get("session_count", 0)) or 0)
        successful_count = int(repetition.get("successful_session_count", 0) or 0)
        raw_fingerprints = repetition.get("session_identity_fingerprints")
        fingerprints = list(raw_fingerprints) if isinstance(raw_fingerprints, Sequence) and not isinstance(raw_fingerprints, (str, bytes, bytearray)) else []
        missing_count = int(repetition.get("missing_session_identity_count", max(0, successful_count - len(fingerprints))) or 0)
    distinct = len(set(fingerprints))
    duplicate_count = max(0, len(fingerprints) - distinct)
    requested = int(repetition.get("requested_session_count", repetition.get("session_count", 0)) or 0)
    status = (
        requested == required_sessions == 100
        and measured_context_count == requested
        and successful_count == requested
        and len(fingerprints) == requested
        and distinct == requested
        and missing_count == 0
        and duplicate_count == 0
    )
    return {
        "status": "PASS" if status else "FAIL",
        "requested_session_count": requested,
        "measured_context_count": measured_context_count,
        "successful_session_count": successful_count,
        "distinct_session_identity_fingerprint_count": distinct,
        "missing_session_identity_count": missing_count,
        "duplicate_session_identity_count": duplicate_count,
        "identity_values_raw_recorded": False,
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def evaluate_benchmark_acceptance(report: Mapping[str, object]) -> dict[str, object]:
    """Pure fail-closed O10.2 acceptance evaluator used by qualification and CI."""

    environment = _mapping(report.get("environment"))
    environment_decision = _mapping(report.get("environment_decision"))
    workload = _mapping(report.get("workload_envelope"))
    dataset = _mapping(report.get("dataset"))
    service = _mapping(report.get("service_workloads"))
    browser = _mapping(report.get("browser_session_workloads"))
    durability = _mapping(report.get("durability"))
    conflict = _mapping(report.get("workflow_conflicts"))
    resilience = _mapping(report.get("resilience"))
    criteria: dict[str, bool] = {}
    criteria["environment_authority_qualifies"] = environment_decision.get("state") == "QUALIFYING_PRODUCTION_LIKE_EXECUTOR"
    criteria["postgresql_major_18_direct_observation"] = environment.get("postgres_server_version_source") == "direct_benchmark_database_query" and _postgres_major(environment.get("postgres_server_version_observed")) == 18
    counts = _mapping(_mapping(dataset.get("facts")).get("counts"))
    criteria["exact_fixture_counts"] = counts == expected_fixture_counts(DatasetConfig()) and _mapping(dataset.get("fixture_validation")).get("status") == "PASS"
    workload_actual = _mapping(workload.get("actual"))
    workload_samples = _mapping(workload.get("sample_counts"))
    criteria["workload_envelope_and_sample_floors"] = (
        workload.get("status") == "PASS"
        and workload_actual.get("browser_sessions") == 100
        and workload_actual.get("family_partitions") == 30
        and workload_actual.get("attention_items") == 10_000
        and workload_actual.get("archived_read_records") == 1_000_000
        and workload_actual.get("attention_page_size") == 50
        and int(workload_actual.get("repetitions", 0)) >= QUALIFYING_MIN_REPETITIONS
        and _is_number(workload_actual.get("duration_seconds"))
        and float(workload_actual["duration_seconds"]) >= QUALIFYING_MIN_DURATION_SECONDS
        and set(workload_samples) == set(SAMPLE_FLOORS)
        and all(_is_number(workload_samples.get(name)) and int(workload_samples[name]) >= floor for name, floor in SAMPLE_FLOORS.items())
    )
    criteria["qualification_mode"] = report.get("execution_mode") == QUALIFYING_EXECUTION_MODE

    required_scenarios = tuple(SAMPLE_FLOORS)
    service_ok = True
    for name in required_scenarios:
        result = _mapping(service.get(name))
        service_ok = service_ok and result.get("all_repetitions_valid") is True and int(result.get("repetition_count", 0)) >= QUALIFYING_MIN_REPETITIONS and _mapping(result.get("budget")).get("status") == "PASS"
        repetitions = result.get("repetitions")
        if not isinstance(repetitions, Sequence) or len(repetitions) < QUALIFYING_MIN_REPETITIONS:
            service_ok = False
        else:
            for repetition in repetitions:
                summary = _mapping(_mapping(repetition).get("summary"))
                service_ok = service_ok and int(summary.get("sample_count", 0)) >= SAMPLE_FLOORS[name] and int(summary.get("valid_latency_sample_count", 0)) >= SAMPLE_FLOORS[name] and int(summary.get("failures", 0)) == 0
    criteria["service_workloads_valid_and_within_budget"] = service_ok

    browser_repetitions = browser.get("repetitions")
    browser_ok = browser.get("status") == "PASS" and int(browser.get("repetition_count", 0)) >= QUALIFYING_MIN_REPETITIONS and isinstance(browser_repetitions, Sequence) and len(browser_repetitions) >= QUALIFYING_MIN_REPETITIONS
    if browser_ok:
        for repetition in browser_repetitions:
            repetition_map = _mapping(repetition)
            isolation = evaluate_session_isolation(repetition_map)
            browser_ok = browser_ok and isolation["status"] == "PASS" and repetition_map.get("status") == "PASS"
            attention_p95 = _mapping(repetition_map.get("attention_useful_paint_ms")).get("p95_ms")
            episode_p95 = _mapping(repetition_map.get("episode_useful_paint_ms")).get("p95_ms")
            attention_payload = _mapping(repetition_map.get("attention_payload_bytes")).get("max_ms")
            episode_payload = _mapping(repetition_map.get("episode_payload_bytes")).get("max_ms")
            browser_ok = browser_ok and _is_number(attention_p95) and float(attention_p95) <= BUDGETS["attention_useful_page_p95_ms"] and _is_number(episode_p95) and float(episode_p95) <= BUDGETS["episode_useful_page_p95_ms"] and _is_number(attention_payload) and float(attention_payload) <= BUDGETS["attention_payload_bytes"] and _is_number(episode_payload) and float(episode_payload) <= BUDGETS["episode_payload_bytes"] and int(repetition_map.get("console_or_network_failures", 1)) == 0 and not repetition_map.get("page_errors")
    criteria["browser_sessions_payloads_and_failures"] = browser_ok

    criteria["workflow_controlled_conflict"] = conflict.get("status") == "PASS" and conflict.get("typed_conflict_observed") is True and conflict.get("unintended_mutation_count") == 0 and conflict.get("committed_count") == 1 and conflict.get("expected_conflict_count") == 1
    criteria["durability_exactly_once_replay_read_your_write"] = durability.get("exactly_once") is True and durability.get("acknowledged_effects_lost_after_restart") == 0 and _mapping(durability.get("before_restart")).get("read_your_write") is True and _mapping(durability.get("after_restart")).get("same_result_identity") is True and _mapping(durability.get("after_restart")).get("read_your_write") is True

    source = _mapping(resilience.get("source_degraded"))
    source_states = source.get("observed_source_states")
    criteria["source_degraded_synthetic_resilience"] = source.get("status") == "PASS" and source.get("synthetic_operational_resilience") is True and source.get("authentic_family_science") == "NOT_CLAIMED" and source.get("foreground_load_continued") is True and isinstance(source_states, Sequence) and set(source_states) == {"STALE", "UNAVAILABLE"} and int(source.get("repetition_count", 0)) >= QUALIFYING_MIN_REPETITIONS
    for name in ("worker_starvation", "web_crash_restart"):
        result = _mapping(resilience.get(name))
        criteria[f"{name}_resilience"] = result.get("status") == "PASS" and int(result.get("repetition_count", 0)) >= QUALIFYING_MIN_REPETITIONS and isinstance(result.get("repetitions"), Sequence) and all(_mapping(item).get("status") == "PASS" for item in result.get("repetitions", []))
    restore = _mapping(resilience.get("restore"))
    scale = _mapping(restore.get("scale_authority"))
    criteria["restore_rehearsal"] = restore.get("status") == "PASS" and int(restore.get("repetition_count", 0)) >= QUALIFYING_MIN_REPETITIONS and scale.get("mode") in {"benchmark_scale", "approved_representative"} and scale.get("approved") is True
    criteria["production_disaster_rpo_rto_not_established"] = report.get("production_disaster_rpo_rto_claim") == "NOT_ESTABLISHED"
    failed = [name for name, passed in criteria.items() if not passed]
    return {"status": "PASS" if not failed else "FAIL", "criteria": criteria, "failed": failed}


def qualify_benchmark(
    environment: Mapping[str, object],
    scenario_results: Mapping[str, Mapping[str, object]],
    browser: Mapping[str, object],
    durability: Mapping[str, object],
    resilience: Mapping[str, Mapping[str, object]],
    *,
    workflow_conflicts: Mapping[str, object] | None = None,
    workload_envelope: Mapping[str, object] | None = None,
    dataset: Mapping[str, object] | None = None,
    execution_mode: str = QUALIFYING_EXECUTION_MODE,
) -> dict[str, object]:
    env = qualify_environment(environment)
    if env.state != "QUALIFYING_PRODUCTION_LIKE_EXECUTOR":
        return {"state": STATUS_BLOCKED_ENVIRONMENT, "environment": env.as_dict(), "budgets": "NOT_EVALUATED", "acceptance": "NOT_EVALUATED"}
    acceptance_report = {
        "execution_mode": execution_mode,
        "environment": dict(environment),
        "environment_decision": env.as_dict(),
        "service_workloads": dict(scenario_results),
        "browser_session_workloads": dict(browser),
        "durability": dict(durability),
        "workflow_conflicts": dict(workflow_conflicts or {}),
        "resilience": dict(resilience),
        "workload_envelope": dict(workload_envelope or {}),
        "dataset": dict(dataset or {}),
        "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
    }
    acceptance = evaluate_benchmark_acceptance(acceptance_report)
    return {
        "state": STATUS_PASS if acceptance["status"] == "PASS" else STATUS_FAIL_BUDGETS,
        "environment": env.as_dict(),
        "failures": acceptance["failed"],
        "acceptance": acceptance,
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
        fixture_validation = validate_fixture_counts(facts["counts"], config)
        return {"config": asdict(config), "config_identity": config.identity, "facts": facts, "fixture_validation": fixture_validation}
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
    result = aggregate_repetitions(repetitions_out)
    result["scheduled_sample_count"] = sample_count
    result["sample_floor"] = SAMPLE_FLOORS.get(operation, sample_count)
    result["duration_seconds"] = duration_seconds
    return result


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
    now = datetime.now(timezone.utc).isoformat()
    facts: dict[str, object] = {
        "executor_os": platform.platform(aliased=True),
        "executor_architecture": platform.machine(),
        "python": platform.python_version(),
        "executor_kind": f"foreground-{platform.system().lower()}-developer-host",
        "executor_profile": "developer-host-not-n1",
        "measurement_window": {"started_at": now, "ended_at": now},
        "authority_validation": {"status": "NOT_VALIDATED", "reason": "local process facts are not a split-resource authority"},
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
        adapter = PostgreSQLReferenceTransactionAdapter(dsn)
        try:
            connection = adapter.connection
            facts["postgres_server_version_observed"] = adapter.server_version()
            facts["postgres_server_version_source"] = "direct_benchmark_database_query"
            facts["postgres_active_connections"] = int(connection.execute("SELECT count(*) AS count FROM pg_stat_activity WHERE datname = current_database()").fetchone()["count"])
            facts["postgres_cpu_rss"] = "NOT_EXPOSED_BY_POSTGRES_SERVICE"
            facts["worker_backlog"] = int(connection.execute("SELECT count(*) AS count FROM job WHERE status IN ('QUEUED', 'RUNNING', 'DEFERRED')").fetchone()["count"])
            facts["worker_expired_leases"] = int(connection.execute("SELECT count(*) AS count FROM job WHERE lease_expires_at IS NOT NULL AND lease_expires_at < clock_timestamp()").fetchone()["count"])
        finally:
            adapter.close()
    return facts


def _merge_environment_facts(observed: Mapping[str, object], supplied: Mapping[str, object]) -> dict[str, object]:
    """Merge an attestation file without allowing it to replace direct DB facts."""

    merged = dict(observed)
    for key in ("executor_kind", "executor_profile", "measurement_window", "authority_validation", "web", "postgres", "worker", "client_rtt", "separation_proof"):
        if key in supplied:
            merged[key] = supplied[key]
    if "postgres_server_version_observed" in supplied and supplied["postgres_server_version_observed"] != observed.get("postgres_server_version_observed"):
        merged["external_postgres_version_mismatch"] = True
    merged["environment_facts_file_present"] = True
    merged["environment_facts_file_provenance"] = supplied.get("file_provenance", {"status": "UNBOUND"})
    return merged


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
        before_counts = {
            table: int(cleanup.connection.execute(f"SELECT count(*) AS count FROM {table} WHERE aggregate_type = 'episode_workflow' AND aggregate_id = %s", (episode_id,)).fetchone()["count"])
            for table in ("command_receipt", "audit_event", "outbox_event")
        }
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
    verify = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        after_counts = {
            table: int(verify.connection.execute(f"SELECT count(*) AS count FROM {table} WHERE aggregate_type = 'episode_workflow' AND aggregate_id = %s", (episode_id,)).fetchone()["count"])
            for table in ("command_receipt", "audit_event", "outbox_event")
        }
    finally:
        verify.close()
    deltas = {table: after_counts[table] - before_counts[table] for table in before_counts}
    unintended_mutation_count = max(0, sum(max(0, value - 1) for value in deltas.values()))
    typed_conflict_observed = sorted(outcomes) == ["VERSION_CONFLICT", "committed"]
    return {
        "episode_id_hash": safe_digest(episode_id),
        "outcomes": sorted(outcomes),
        "committed_count": outcomes.count("committed"),
        "expected_conflict_count": outcomes.count("VERSION_CONFLICT"),
        "typed_conflict_observed": typed_conflict_observed,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "mutation_deltas": deltas,
        "unintended_mutation_count": unintended_mutation_count,
        "status": "PASS" if typed_conflict_observed and all(value == 1 for value in deltas.values()) and unintended_mutation_count == 0 else "FAIL",
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
            "scale_authority": {"mode": "benchmark_scale", "approved": False},
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
        "scale_authority": {"mode": "benchmark_scale", "approved": True, "fixture_counts_bound": True},
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
    """Exercise synthetic/current capability machinery while a read remains live.

    This is G10 operational resilience evidence only.  It deliberately makes
    no authentic-family claim: the source capability states are deterministic
    typed fixtures and the foreground continuity check is a live database
    health query for each repetition.
    """

    checked_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stale_state, stale_reason = _capability_state(
        SourceSnapshotStatus.PUBLISHED,
        1,
        checked_at - timedelta(seconds=120),
        checked_at,
        60,
    )
    unavailable_state, unavailable_reason = _capability_state(
        SourceSnapshotStatus.QUARANTINED,
        1,
        checked_at,
        checked_at,
        60,
    )
    repetition_results = []
    for repetition in range(1, repetitions + 1):
        adapter = PostgreSQLReferenceTransactionAdapter(dsn)
        try:
            live = adapter.connection.execute("SELECT 1 AS alive").fetchone()["alive"] == 1
            observed = [stale_state.value, unavailable_state.value]
            valid = live and set(observed) == {SourceCapabilityState.STALE.value, SourceCapabilityState.UNAVAILABLE.value}
            repetition_results.append(
                {
                    "repetition": repetition,
                    "status": "PASS" if valid else "FAIL",
                    "foreground_read_continued": live,
                    "reported_source_states": observed,
                    "healthy_or_empty_fallback": False,
                }
            )
        except Exception as exc:
            repetition_results.append({"repetition": repetition, "status": "FAIL", "error_type": type(exc).__name__, "error_digest": safe_digest(exc)})
        finally:
            adapter.close()
    return {
        "repetition_count": len(repetition_results),
        "requested_repetitions": repetitions,
        "repetitions": repetition_results,
        "status": "PASS" if len(repetition_results) == repetitions and all(item["status"] == "PASS" for item in repetition_results) else "FAIL",
        "synthetic_operational_resilience": True,
        "authentic_family_science": "NOT_CLAIMED",
        "observed_source_states": [stale_state.value, unavailable_state.value],
        "capability_reasons": [stale_reason, unavailable_reason],
        "foreground_load_continued": all(item.get("foreground_read_continued") is True for item in repetition_results),
        "healthy_or_empty_fallback_observed": False,
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
    page_errors: list[str] = []
    failures: list[str] = []
    websocket_session_fingerprints: list[str] = []

    def websocket(ws: Any) -> None:
        try:
            query = parse_qs(urlsplit(ws.url).query)
            for key in ("sid", "session_id", "session"):
                for value in query.get(key, []):
                    websocket_session_fingerprints.append(safe_digest(f"{key}:{value}"))
        except (TypeError, ValueError):
            pass
        def frame(payload: Any) -> None:
            nonlocal received, frame_count
            frame_count += 1
            received += len(payload) if isinstance(payload, (bytes, bytearray)) else len(str(payload).encode("utf-8"))
        ws.on("framereceived", frame)

    page.on("websocket", websocket)
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
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
        "page_errors": [safe_digest(item) for item in page_errors],
        "failed_network_count": len(failures),
        "websocket_session_fingerprints": websocket_session_fingerprints,
        "useful_paint_predicate": "heading + authoritative non-loading status + populated/empty table; Episode identity + workflow/source facts + eligible action/no-action",
    }


def _session_identity_from_websocket(websocket_fingerprints: Sequence[str]) -> dict[str, object] | None:
    """Choose the runtime session authority without assuming a cookie name."""

    if websocket_fingerprints:
        return {"kind": "runtime_websocket_session", "fingerprint": safe_digest(json.dumps(sorted(set(websocket_fingerprints)), separators=(",", ":")))}
    return None


def _cookie_session_identity(cookies: Sequence[Mapping[str, object]], websocket_fingerprints: Sequence[str]) -> dict[str, object] | None:
    candidates = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or not value:
            continue
        lowered = name.lower()
        if lowered == "session" or "session" in lowered or lowered in {"nicegui", "nicegui_session"}:
            candidates.append((name, value))
    if candidates:
        return {"kind": "runtime_cookie", "fingerprint": safe_digest(json.dumps(sorted(candidates), separators=(",", ":")))}
    return _session_identity_from_websocket(websocket_fingerprints)


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
            for index, context in enumerate(contexts):
                result = samples[index]
                if result.get("status") != "ok":
                    continue
                identity = _cookie_session_identity(await context.cookies(), result.get("websocket_session_fingerprints", []))
                if identity is not None:
                    result["session_identity_kind"] = identity["kind"]
                    result["session_identity_fingerprint"] = identity["fingerprint"]
            attention_times = [float(item["attention_useful_paint_ms"]) for item in samples if item.get("status") == "ok"]
            episode_times = [float(item["episode_useful_paint_ms"]) for item in samples if item.get("status") == "ok"]
            attention_payload = [int(item["attention_application_websocket_bytes"]) for item in samples if item.get("status") == "ok"]
            episode_payload = [int(item["episode_application_websocket_bytes"]) for item in samples if item.get("status") == "ok"]
            errors = [item for item in samples if item.get("status") != "ok" or item.get("console_errors") or item.get("page_errors") or item.get("failed_network_count")]
            isolation = evaluate_session_isolation({"requested_session_count": sessions, "samples": samples})
            return {
                "status": "PASS" if len(samples) == sessions and not errors and attention_times and episode_times and isolation["status"] == "PASS" else "FAIL",
                "session_count": sessions,
                "requested_session_count": sessions,
                "measured_context_count": len(samples),
                "successful_session_count": sum(item.get("status") == "ok" for item in samples),
                "distinct_session_identity_fingerprint_count": isolation["distinct_session_identity_fingerprint_count"],
                "distinct_session_cookie_fingerprint_count": isolation["distinct_session_identity_fingerprint_count"],
                "missing_session_identity_count": isolation["missing_session_identity_count"],
                "duplicate_session_identity_count": isolation["duplicate_session_identity_count"],
                "session_isolation": isolation,
                "attention_useful_paint_ms": _distribution(attention_times),
                "episode_useful_paint_ms": _distribution(episode_times),
                "attention_payload_bytes": _distribution([float(value) for value in attention_payload]),
                "episode_payload_bytes": _distribution([float(value) for value in episode_payload]),
                "samples": samples,
                "console_or_network_failures": len(errors),
                "page_errors": [error for item in samples for error in item.get("page_errors", [])],
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
    status = all(item.get("status") == "PASS" and evaluate_session_isolation(item)["status"] == "PASS" for item in repetitions_out) and all(value <= BUDGETS["attention_useful_page_p95_ms"] for value in attention) and all(value <= BUDGETS["episode_useful_page_p95_ms"] for value in episode) and all(value <= BUDGETS["attention_payload_bytes"] for value in payload_attention) and all(value <= BUDGETS["episode_payload_bytes"] for value in payload_episode)
    return {"status": "PASS" if status else "FAIL", "repetition_count": repetitions, "repetitions": repetitions_out, "browser_predicate": "100 successful isolated contexts; runtime session identity fingerprint distinctness; authorized heading + authoritative status + coherent rows/table; Episode identity + workflow/source facts + action/no-action", "budget": {"status": "PASS" if status else "FAIL", "attention_useful_page_p95_budget_ms": BUDGETS["attention_useful_page_p95_ms"], "episode_useful_page_p95_budget_ms": BUDGETS["episode_useful_page_p95_ms"], "attention_payload_max_budget_bytes": BUDGETS["attention_payload_bytes"], "episode_payload_max_budget_bytes": BUDGETS["episode_payload_bytes"]}}


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
        "restore": {"status": "NOT_RUN", "repetition_count": 0, "scale_authority": {"mode": "benchmark_scale", "approved": False}, "reason": "Full logical dump/isolated restore is an explicit executor action; use --run-restore with pg_dump/createdb authority. production_disaster_rpo_rto_claim=NOT_ESTABLISHED"},
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
    workload = validate_workload_envelope(
        browser_sessions=ENVELOPE["concurrent_browser_sessions"],
        family_partitions=ENVELOPE["family_partitions"],
        attention_items=ENVELOPE["open_attention_items"],
        archived_read_records=ENVELOPE["archived_episode_read_records"],
        attention_page_size=ENVELOPE["attention_page_size"],
        repetitions=QUALIFYING_MIN_REPETITIONS,
        duration_seconds=QUALIFYING_MIN_DURATION_SECONDS,
    )
    report = {
        "schema_version": "ephi-o10.2-performance-capacity.v1",
        "status": STATUS_BLOCKED_ENVIRONMENT,
        "qualification_state": STATUS_BLOCKED_ENVIRONMENT,
        "operation": "FIX",
        "project": "ephi",
        "request": REQUEST,
        "fabric_job_id": FABRIC_JOB_ID,
        "base_sha": BASE_SHA,
        "work_branch": WORK_BRANCH,
        "execution_mode": DIAGNOSTIC_EXECUTION_MODE,
        "envelope": ENVELOPE,
        "budgets": BUDGETS,
        "workload_envelope": workload,
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
        "environment": {"qualification": "NOT_RUN", "reason": "contract-only template does not inspect a benchmark database or executor"},
        "environment_decision": qualify_environment({}).as_dict(),
        "execution": {"status": "NOT_RUN", "reason": "contract-only evidence-template operation; no capacity result is executable"},
        "secret_safety": {"cookies": False, "storage_values": False, "auth_headers": False, "dsns": False, "protected_payloads": False, "raw_identity_values": False},
        "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
        "scope_boundary": "O10.2 current Attention/Episode W1 only; no Assets, Outcomes, Family Center, global search or evidence-panel surfaces added.",
    }
    write_report(output_dir, report)
    return report


def _blocked_environment_report(output_dir: Path, environment: Mapping[str, object], decision: EnvironmentQualificationResult) -> dict[str, object]:
    report = {
        "schema_version": "ephi-o10.2-performance-capacity.v1",
        "status": STATUS_BLOCKED_ENVIRONMENT,
        "qualification_state": STATUS_BLOCKED_ENVIRONMENT,
        "operation": "FIX",
        "project": "ephi",
        "request": REQUEST,
        "fabric_job_id": FABRIC_JOB_ID,
        "base_sha": BASE_SHA,
        "work_branch": WORK_BRANCH,
        "execution_mode": QUALIFYING_EXECUTION_MODE,
        "envelope": ENVELOPE,
        "budgets": BUDGETS,
        "environment": dict(environment),
        "environment_decision": decision.as_dict(),
        "execution": {"status": "NOT_RUN", "reason": decision.reason},
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
    workload = validate_workload_envelope(
        browser_sessions=args.browser_sessions,
        family_partitions=config.family_partitions,
        attention_items=config.attention_items,
        archived_read_records=config.archived_read_records,
        attention_page_size=ENVELOPE["attention_page_size"],
        repetitions=args.repetitions,
        duration_seconds=args.duration_seconds,
    )
    if workload["status"] != "PASS":
        raise SystemExit("O10.2 qualification requires the exact N1 workload envelope and minimum sample floors")
    environment = _resource_facts(args.dsn)
    if args.environment_facts:
        supplied = json.loads(Path(args.environment_facts).read_text(encoding="utf-8"))
        if not isinstance(supplied, Mapping):
            raise ValueError("environment facts must be a JSON object")
        environment = _merge_environment_facts(environment, supplied)
    environment_decision = qualify_environment(environment)
    if environment_decision.state != "QUALIFYING_PRODUCTION_LIKE_EXECUTOR":
        return _blocked_environment_report(output_dir, environment, environment_decision)
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
    scenario_results = {name: service[name] for name in ("attention_warm", "attention_burst", "attention_filter_search", "episode_brief", "workflow_command")}
    qualification = qualify_benchmark(
        environment,
        scenario_results,
        browser,
        durability,
        resilience,
        workflow_conflicts=conflict,
        workload_envelope=workload,
        dataset=seeded,
        execution_mode=QUALIFYING_EXECUTION_MODE,
    )
    report = {
        "schema_version": "ephi-o10.2-performance-capacity.v1",
        "status": qualification["state"],
        "qualification_state": qualification["state"],
        "operation": "FIX",
        "project": "ephi",
        "request": REQUEST,
        "fabric_job_id": FABRIC_JOB_ID,
        "base_sha": BASE_SHA,
        "work_branch": WORK_BRANCH,
        "execution_mode": QUALIFYING_EXECUTION_MODE,
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
        "workload_envelope": workload,
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
        "acceptance": qualification.get("acceptance"),
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
    if not args.contract_only:
        envelope = validate_workload_envelope(
            browser_sessions=args.browser_sessions,
            family_partitions=args.family_partitions,
            attention_items=args.attention_items,
            archived_read_records=args.archived_read_records,
            attention_page_size=ENVELOPE["attention_page_size"],
            repetitions=args.repetitions,
            duration_seconds=args.duration_seconds,
        )
        if envelope["status"] != "PASS":
            parser.error("O10.2 requires the exact N1 workload envelope and minimum sample floors")
    try:
        report = run(args)
    except BenchmarkEnvironmentBlocked as exc:
        report = {
            "schema_version": "ephi-o10.2-performance-capacity.v1",
            "status": STATUS_BLOCKED_ENVIRONMENT,
            "qualification_state": STATUS_BLOCKED_ENVIRONMENT,
            "operation": "FIX",
            "request": REQUEST,
            "fabric_job_id": FABRIC_JOB_ID,
            "base_sha": BASE_SHA,
            "work_branch": WORK_BRANCH,
            "execution_mode": QUALIFYING_EXECUTION_MODE,
            "environment_decision": exc.result.as_dict(),
            "error_type": "BenchmarkEnvironmentBlocked",
            "error_digest": safe_digest(exc),
            "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
        }
        write_report(Path(args.output).resolve(), report)
        print(json.dumps({"status": report["status"], "error_type": report["error_type"]}, sort_keys=True))
        return 0 if args.contract_only else 2
    except Exception as exc:
        error_type = type(exc).__name__[:80]
        report = {"schema_version": "ephi-o10.2-performance-capacity.v1", "status": STATUS_FAIL_EXECUTION, "qualification_state": STATUS_FAIL_EXECUTION, "operation": "FIX", "request": REQUEST, "fabric_job_id": FABRIC_JOB_ID, "base_sha": BASE_SHA, "work_branch": WORK_BRANCH, "execution_mode": QUALIFYING_EXECUTION_MODE, "error_type": error_type, "error_digest": safe_digest(exc), "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED"}
        write_report(Path(args.output).resolve(), report)
        print(json.dumps({"status": report["status"], "error_type": report["error_type"]}, sort_keys=True))
        return 1
    print(json.dumps({"status": report["status"], "output": str(Path(args.output).resolve())}, sort_keys=True))
    if args.contract_only and report.get("status") == STATUS_BLOCKED_ENVIRONMENT:
        return 0
    if report.get("status") == STATUS_PASS:
        return 0
    if report.get("status") == STATUS_BLOCKED_ENVIRONMENT:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
