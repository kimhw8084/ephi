"""Typed, secret-safe operational health and restore evidence contracts.

This module deliberately contains no second operational state store. It only
normalizes observations made against the existing PostgreSQL, worker, read,
artifact, and source authorities into bounded evidence values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from .artifacts import ArtifactService
from .context import AccessScope, CurrentAuthorizationAuthority, Principal
from .errors import AggregateNotFoundError, AuthorizationDeniedError
from .family_center import FAMILY_CENTER_READ, GateState, QualificationWorkspaceIdentity
from .hashing import canonical_json
from .source_ingress import (
    SOURCE_READ_CAPABILITY,
    MetrologySourceBinding,
    SourceCapabilityRecord,
    SourceCapabilityState,
    SourceSnapshotRepository,
    SourceSnapshotStatus,
)
from .worker import JobRecord, WorkerJobPort


class OperationalState(StrEnum):
    READY = "READY"
    CURRENT = "CURRENT"
    PARTIAL = "PARTIAL"
    PENDING = "PENDING"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"
    NOT_QUALIFIED = "NOT_QUALIFIED"


_MAX_REASON_LENGTH = 240
_MAX_FACTS = 12
_MAX_FACT_KEY_LENGTH = 48


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} must be non-empty and <= {maximum} characters")
    return normalized


def _safe_fact(value: object, field: str) -> bool | int | float | str:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError(f"{field} must be finite")
        return value
    if isinstance(value, str):
        return _bounded_text(value, field, 160)
    raise TypeError(f"{field} must be a bounded scalar")


@dataclass(frozen=True, slots=True)
class OperationsAxis:
    """One independently classified operational fact axis."""

    state: OperationalState
    reason: str
    facts: tuple[tuple[str, bool | int | float | str], ...] = ()

    def __post_init__(self) -> None:
        state = self.state if isinstance(self.state, OperationalState) else OperationalState(self.state)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", _bounded_text(self.reason, "reason", _MAX_REASON_LENGTH))
        if not isinstance(self.facts, tuple) or len(self.facts) > _MAX_FACTS:
            raise ValueError(f"facts must be a tuple with at most {_MAX_FACTS} entries")
        normalized: list[tuple[str, bool | int | float | str]] = []
        seen: set[str] = set()
        for key, value in self.facts:
            key = _bounded_text(key, "fact key", _MAX_FACT_KEY_LENGTH)
            if key in seen:
                raise ValueError("fact keys must be unique")
            seen.add(key)
            normalized.append((key, _safe_fact(value, key)))
        object.__setattr__(self, "facts", tuple(sorted(normalized)))

    @classmethod
    def create(
        cls,
        state: OperationalState | str,
        reason: str,
        facts: Mapping[str, bool | int | float | str] | None = None,
    ) -> "OperationsAxis":
        return cls(
            OperationalState(state),
            reason,
            tuple((str(key), value) for key, value in (facts or {}).items()),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "facts": {key: value for key, value in self.facts},
        }


@dataclass(frozen=True, slots=True)
class OperationsHealthSnapshot:
    """A stable health payload with no collapsed global ``healthy`` flag."""

    schema_version: str
    axes: tuple[tuple[str, OperationsAxis], ...]
    secret_safety: tuple[tuple[str, bool], ...] = (
        ("credentials_printed", False),
        ("tokens_printed", False),
        ("raw_rows_printed", False),
        ("private_artifact_contents_printed", False),
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _bounded_text(self.schema_version, "schema_version", 64))
        if not isinstance(self.axes, tuple) or not self.axes:
            raise ValueError("operations health requires at least one axis")
        normalized: list[tuple[str, OperationsAxis]] = []
        seen: set[str] = set()
        for name, axis in self.axes:
            name = _bounded_text(name, "axis name", 64)
            if name in seen or not isinstance(axis, OperationsAxis):
                raise ValueError("operations axes must have unique names and typed values")
            seen.add(name)
            normalized.append((name, axis))
        object.__setattr__(self, "axes", tuple(sorted(normalized)))
        safety = tuple(self.secret_safety)
        if any(not isinstance(key, str) or not isinstance(value, bool) for key, value in safety):
            raise ValueError("secret_safety values must be booleans")
        object.__setattr__(self, "secret_safety", tuple(sorted(safety)))

    def as_dict(self) -> dict[str, object]:
        # The intentionally absent global readiness/healthy key prevents a
        # process-liveness observation from being misread as data readiness.
        return {
            "schema_version": self.schema_version,
            "axes": {name: axis.as_dict() for name, axis in self.axes},
            "secret_safety": {key: value for key, value in self.secret_safety},
        }


def operations_health_snapshot(
    *,
    process_transport: OperationsAxis,
    postgres: OperationsAxis,
    immutable_artifacts: OperationsAxis,
    source_capability: OperationsAxis,
    durable_worker_jobs: OperationsAxis,
    evidence_qualification: OperationsAxis,
) -> OperationsHealthSnapshot:
    """Build the single O9 health boundary from independently probed axes."""

    return OperationsHealthSnapshot(
        "o9.1.v1",
        (
            ("process_transport", process_transport),
            ("postgres_readiness_durability", postgres),
            ("immutable_artifact_integrity", immutable_artifacts),
            ("source_capability_freshness", source_capability),
            ("durable_worker_job_state", durable_worker_jobs),
            ("evidence_qualification_freshness", evidence_qualification),
        ),
    )


OPERATIONS_READ_CAPABILITY = "ephi.operations.read"
ARTIFACT_READ_CAPABILITY = "ephi.artifact.read"
MAX_OPERATIONS_JOBS = 100
MAX_OPERATIONS_ARTIFACTS = 500


@dataclass(frozen=True, slots=True)
class PostgreSQLHealthFacts:
    server_version: str
    critical_table_count: int
    required_table_count: int
    missing_table_count: int
    migration_file_count: int
    migration_manifest_sha256: str
    migration_ledger_state: str
    observed_at: datetime


@runtime_checkable
class OperationsPostgreSQLPort(Protocol):
    def operations_health_facts(self) -> PostgreSQLHealthFacts: ...


@dataclass(frozen=True, slots=True)
class WorkerHealthFacts:
    job_count: int
    failed_count: int
    dead_letter_count: int
    running_count: int
    expired_running_count: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class OperationsJob:
    job_id: str
    job_type: str
    status: str
    priority: int
    created_at: str
    available_at: str
    updated_at: str
    attempts: int
    max_attempts: int
    lease_epoch: int
    lease_expires_at: str | None
    lease_state: str
    owner_reference: str | None
    failure_code: str | None
    failure_reason: str | None
    committed_local_effect_receipt: bool | None

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "status": self.status,
            "priority": self.priority,
            "created_at": self.created_at,
            "available_at": self.available_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "lease_epoch": self.lease_epoch,
            "lease_expires_at": self.lease_expires_at,
            "lease_state": self.lease_state,
            "owner_reference": self.owner_reference,
            "failure_code": self.failure_code,
            "failure_reason": self.failure_reason,
            "committed_local_effect_receipt": self.committed_local_effect_receipt,
        }


@dataclass(frozen=True, slots=True)
class OperationsSourceDetail:
    state: str
    reason: str
    source_id: str | None
    family_id: str | None
    capability_id: str | None
    mapping_version: str | None
    mapping_hash: str | None
    source_revision: str | None
    source_partition: str | None
    last_available_cutoff: str | None
    freshness_age_seconds: int | None
    freshness_limit_seconds: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "source_id": self.source_id,
            "family_id": self.family_id,
            "capability_id": self.capability_id,
            "mapping_version": self.mapping_version,
            "mapping_hash": self.mapping_hash,
            "source_revision": self.source_revision,
            "source_partition": self.source_partition,
            "last_available_cutoff": self.last_available_cutoff,
            "freshness_age_seconds": self.freshness_age_seconds,
            "freshness_limit_seconds": self.freshness_limit_seconds,
        }


@dataclass(frozen=True, slots=True)
class OperationsArtifactDetail:
    inspected_count: int
    missing_count: int
    corrupt_count: int
    other_unavailable_count: int
    truncated: bool
    safe_reasons: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "inspected_count": self.inspected_count,
            "missing_count": self.missing_count,
            "corrupt_count": self.corrupt_count,
            "other_unavailable_count": self.other_unavailable_count,
            "truncated": self.truncated,
            "safe_reasons": {key: count for key, count in self.safe_reasons},
        }


@dataclass(frozen=True, slots=True)
class OperationsGateDetail:
    stage: str
    state: str
    reason: str | None
    expires_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {"stage": self.stage, "state": self.state, "reason": self.reason, "expires_at": self.expires_at}


@dataclass(frozen=True, slots=True)
class OperationsQualificationDetail:
    state: str
    reason: str
    family_id: str | None = None
    capability_id: str | None = None
    release_id: str | None = None
    family_center_href: str | None = None
    gates: tuple[OperationsGateDetail, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "family_id": self.family_id,
            "capability_id": self.capability_id,
            "release_id": self.release_id,
            "family_center_href": self.family_center_href,
            "gates": [gate.as_dict() for gate in self.gates],
        }


@dataclass(frozen=True, slots=True)
class OperationsBackupStatus:
    local_o91_rehearsal: str = "AVAILABLE_IN_CLI_CONTRACT"
    target_backup_evidence: str = "NOT_BOUND"
    production_rpo_rto: str = "NOT_ESTABLISHED"
    reason: str = "DEPLOYMENT_BACKUP_EVIDENCE_NOT_BOUND"

    def as_dict(self) -> dict[str, str]:
        return {
            "local_o91_rehearsal": self.local_o91_rehearsal,
            "target_backup_evidence": self.target_backup_evidence,
            "production_rpo_rto": self.production_rpo_rto,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class OperationsCockpitSnapshot:
    observed_at: str
    health: OperationsHealthSnapshot
    source: OperationsSourceDetail
    artifacts: OperationsArtifactDetail
    jobs: tuple[OperationsJob, ...]
    jobs_truncated: bool
    qualification: OperationsQualificationDetail
    backup_restore: OperationsBackupStatus

    def as_dict(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at,
            "health": self.health.as_dict(),
            "source": self.source.as_dict(),
            "artifacts": self.artifacts.as_dict(),
            "jobs": [job.as_dict() for job in self.jobs],
            "jobs_truncated": self.jobs_truncated,
            "qualification": self.qualification.as_dict(),
            "backup_restore": self.backup_restore.as_dict(),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _safe_failure_code(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    private_markers = (
        "PASSWORD", "SECRET", "TOKEN", "CREDENTIAL", "DSN", "PATH", "URL", "COOKIE",
        "BEARER", "AUTH", "PRIVATE", "SERIAL",
    )
    if (
        not re.fullmatch(r"[A-Z][A-Z0-9_.-]{0,63}", candidate)
        or any(marker in candidate.upper() for marker in private_markers)
    ):
        return "UNCLASSIFIED_FAILURE"
    return candidate


def _safe_reason_code(value: str | None, fallback: str) -> str:
    if value is None:
        return fallback
    candidate = value.strip()
    private_markers = (
        "PASSWORD", "SECRET", "TOKEN", "CREDENTIAL", "DSN", "PATH", "URL", "COOKIE",
        "BEARER", "AUTH", "PRIVATE", "SERIAL",
    )
    if (
        not re.fullmatch(r"[A-Z][A-Z0-9_.-]{0,95}", candidate)
        or any(marker in candidate.upper() for marker in private_markers)
    ):
        return fallback
    return candidate


def _safe_job_id(value: str) -> str:
    candidate = value.strip()
    private_markers = (
        "PASSWORD", "SECRET", "TOKEN", "CREDENTIAL", "DSN", "PATH", "URL", "COOKIE",
        "BEARER", "AUTH", "PRIVATE", "SERIAL",
    )
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", candidate)
        or any(marker in candidate.upper() for marker in private_markers)
    ):
        return "job-" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return candidate


def _safe_job_type(value: str) -> str:
    candidate = value.strip()
    private_markers = (
        "PASSWORD", "SECRET", "TOKEN", "CREDENTIAL", "DSN", "PATH", "URL", "COOKIE",
        "BEARER", "AUTH", "PRIVATE", "SERIAL",
    )
    if (
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,95}", candidate)
        or any(marker in candidate.upper() for marker in private_markers)
    ):
        return "UNCLASSIFIED_JOB_TYPE"
    return candidate


def _safe_failure_reason(value: str | None) -> str | None:
    if value is None:
        return None
    # Job handlers can place provider details, row fragments, secrets, or
    # paths in this free-form field.  Do not attempt a permissive scrub: show
    # a fixed bounded explanation and retain only a separately validated code.
    return "Failure detail withheld; review the owning worker authority."


def _worker_job_summary(job: JobRecord, *, database_now: datetime, effect_receipt: bool | None) -> OperationsJob:
    expiry = job.lease_expires_at
    if job.status != "RUNNING":
        lease_state = "NONE"
    elif expiry is None:
        lease_state = "STALE"
    elif expiry <= database_now:
        lease_state = "EXPIRED"
    else:
        lease_state = "ACTIVE"
    owner = None
    if job.lease_owner is not None:
        owner = "worker-" + hashlib.sha256(job.lease_owner.encode("utf-8")).hexdigest()[:10]
    known_statuses = {"QUEUED", "RUNNING", "DEFERRED", "FAILED", "DEAD_LETTER", "COMPLETED", "CANCELLED"}
    status = job.status if job.status in known_statuses else "UNKNOWN"
    return OperationsJob(
        _safe_job_id(job.job_id),
        _safe_job_type(job.job_type),
        status,
        job.priority,
        job.created_at.isoformat(),
        job.available_at.isoformat(),
        job.updated_at.isoformat(),
        job.attempts,
        job.max_attempts,
        job.lease_epoch,
        _iso(expiry),
        lease_state,
        owner,
        _safe_failure_code(job.last_failure_code),
        _safe_failure_reason(job.last_failure_message),
        effect_receipt,
    )


def _source_state(record: SourceCapabilityRecord, *, age_seconds: int | None) -> tuple[str, str]:
    state = record.state
    safe_reason = _safe_reason_code(record.reason, f"SOURCE_STATUS_{state.value}")
    if state is SourceCapabilityState.READY:
        if age_seconds is None or age_seconds > record.freshness_age_seconds:
            return "STALE", "SOURCE_AVAILABILITY_EXCEEDS_FRESHNESS_LIMIT"
        return "READY", safe_reason
    if state is SourceCapabilityState.PARTIAL:
        return "PARTIAL", safe_reason
    if state is SourceCapabilityState.STALE:
        return "STALE", safe_reason
    if state is SourceCapabilityState.UNAVAILABLE:
        return "UNAVAILABLE", safe_reason
    if state is SourceCapabilityState.INSUFFICIENT:
        return "PARTIAL", safe_reason
    return "ERROR", safe_reason


class OperationsQueryService:
    """Authorized read composition over the existing O2/O4/O7/O8/O9 authorities."""

    def __init__(
        self,
        *,
        current_authorization: CurrentAuthorizationAuthority,
        postgres: OperationsPostgreSQLPort,
        worker_jobs: WorkerJobPort,
        source_repository: SourceSnapshotRepository | None = None,
        source_binding: MetrologySourceBinding | None = None,
        artifact_service: ArtifactService | None = None,
        family_center: object | None = None,
        qualification_workspace_provider: Callable[[], tuple[str, QualificationWorkspaceIdentity] | None] | None = None,
    ) -> None:
        if not isinstance(current_authorization, CurrentAuthorizationAuthority):
            raise TypeError("Operations requires CurrentAuthorizationAuthority")
        if not isinstance(postgres, OperationsPostgreSQLPort):
            raise TypeError("Operations requires the bound PostgreSQL reference adapter")
        if not isinstance(worker_jobs, WorkerJobPort):
            raise TypeError("Operations requires the existing WorkerJobPort")
        self.current_authorization = current_authorization
        self.postgres = postgres
        self.worker_jobs = worker_jobs
        self.source_repository = source_repository
        self.source_binding = source_binding
        self.artifact_service = artifact_service
        self.family_center = family_center
        self.qualification_workspace_provider = qualification_workspace_provider

    def read(self, principal: Principal, scope: AccessScope) -> OperationsCockpitSnapshot:
        """Read bounded operational facts after the current O8 permission check."""

        self.current_authorization.authorize(principal, scope, OPERATIONS_READ_CAPABILITY)
        observed_at = datetime.now(timezone.utc)

        postgres_axis, postgres_facts = self._postgres_axis()
        worker_axis, jobs, jobs_truncated = self._worker_axis(principal, scope)
        source_axis, source_detail = self._source_axis(principal, scope, postgres_facts)
        artifact_axis, artifact_detail = self._artifact_axis(principal, scope)
        evidence_axis, qualification = self._qualification_axis(principal, scope)

        # This axis is intentionally about the app/query path alone.  Its READY
        # value cannot upgrade, replace, or summarize any of the data axes.
        process_axis = OperationsAxis.create(
            OperationalState.READY,
            "OPERATIONS_QUERY_PATH_RESPONDED",
            {"age_seconds": 0},
        )
        health = operations_health_snapshot(
            process_transport=process_axis,
            postgres=postgres_axis,
            immutable_artifacts=artifact_axis,
            source_capability=source_axis,
            durable_worker_jobs=worker_axis,
            evidence_qualification=evidence_axis,
        )
        return OperationsCockpitSnapshot(
            observed_at.isoformat(),
            health,
            source_detail,
            artifact_detail,
            jobs,
            jobs_truncated,
            qualification,
            OperationsBackupStatus(),
        )

    def _postgres_axis(self) -> tuple[OperationsAxis, PostgreSQLHealthFacts | None]:
        try:
            facts = self.postgres.operations_health_facts()
        except Exception:
            return OperationsAxis.create("UNAVAILABLE", "POSTGRES_QUERY_UNAVAILABLE", {"age_seconds": 0}), None
        missing = facts.missing_table_count
        state = "ERROR" if missing else "READY"
        reason = "CRITICAL_SCHEMA_MISSING" if missing else "POSTGRES_REFERENCE_QUERY_RESPONDED"
        return OperationsAxis.create(
            state,
            reason,
            {
                "age_seconds": 0,
                "server_version": facts.server_version[:48],
                "critical_table_count": facts.critical_table_count,
                "required_table_count": facts.required_table_count,
                "missing_table_count": missing,
                "migration_file_count": facts.migration_file_count,
                "migration_manifest_sha256": facts.migration_manifest_sha256,
                "migration_ledger_state": facts.migration_ledger_state,
                "durability_slo_established": False,
            },
        ), facts

    def _worker_axis(
        self, principal: Principal, scope: AccessScope
    ) -> tuple[OperationsAxis, tuple[OperationsJob, ...], bool]:
        try:
            self.current_authorization.authorize(principal, scope, OPERATIONS_READ_CAPABILITY)
            health_method = getattr(self.worker_jobs, "operations_health_facts", None)
            if not callable(health_method):
                return (
                    OperationsAxis.create("UNAVAILABLE", "WORKER_DATABASE_TIME_FACTS_NOT_BOUND", {"age_seconds": 0}),
                    (),
                    False,
                )
            facts: WorkerHealthFacts = health_method(scope)
            inspected = self.worker_jobs.inspect(scope, limit=MAX_OPERATIONS_JOBS + 1)
            jobs_truncated = len(inspected) > MAX_OPERATIONS_JOBS
            visible = inspected[:MAX_OPERATIONS_JOBS]
            has_effect = getattr(self.worker_jobs, "has_committed_local_effect", None)
            summaries_list = []
            for job in visible:
                try:
                    effect_receipt = bool(has_effect(scope, job.job_id)) if callable(has_effect) else None
                except Exception:
                    effect_receipt = None
                summaries_list.append(
                    _worker_job_summary(job, database_now=facts.observed_at, effect_receipt=effect_receipt)
                )
            summaries = tuple(summaries_list)
        except AuthorizationDeniedError:
            return OperationsAxis.create("UNAVAILABLE", "WORKER_AUTHORIZATION_REQUIRED", {"age_seconds": 0}), (), False
        except Exception:
            return OperationsAxis.create("UNAVAILABLE", "WORKER_QUERY_UNAVAILABLE", {"age_seconds": 0}), (), False

        terminal = facts.failed_count + facts.dead_letter_count
        if terminal:
            state, reason = "ERROR", "DURABLE_WORKER_TERMINAL_FAILURE"
        elif facts.expired_running_count or any(item.lease_state in {"EXPIRED", "STALE"} for item in summaries):
            state, reason = "STALE", "DURABLE_WORKER_EXPIRED_OR_STALE_LEASE"
        else:
            state, reason = "READY", "DURABLE_WORKER_STATE_REACHABLE"
        return OperationsAxis.create(
            state,
            reason,
            {
                "age_seconds": 0,
                "job_count": facts.job_count,
                "failed_count": facts.failed_count,
                "dead_letter_count": facts.dead_letter_count,
                "running_count": facts.running_count,
                "expired_running_count": facts.expired_running_count,
                "visible_job_count": len(summaries),
                "list_truncated": jobs_truncated,
                "database_clock_used": True,
            },
        ), summaries, jobs_truncated

    def _source_axis(
        self,
        principal: Principal,
        scope: AccessScope,
        postgres_facts: PostgreSQLHealthFacts | None,
    ) -> tuple[OperationsAxis, OperationsSourceDetail]:
        binding = self.source_binding
        if self.source_repository is None or binding is None:
            return (
                OperationsAxis.create("UNAVAILABLE", "O4_SOURCE_BINDING_NOT_BOUND", {"age_seconds": 0}),
                OperationsSourceDetail("UNAVAILABLE", "O4_SOURCE_BINDING_NOT_BOUND", None, None, None, None, None, None, None, None, None, None),
            )
        try:
            self.current_authorization.authorize(principal, scope, SOURCE_READ_CAPABILITY)
            if binding.scope != scope:
                raise AuthorizationDeniedError("current source scope is unavailable")
            capability = self.source_repository.get_capability(principal, binding)
            if capability.binding != binding:
                return (
                    OperationsAxis.create("ERROR", "O4_SOURCE_BINDING_MISMATCH", {"age_seconds": 0}),
                    self._source_detail(binding, "ERROR", "O4_SOURCE_BINDING_MISMATCH", None, None),
                )
            snapshot_matches = True
            snapshot_status = SourceSnapshotStatus.PUBLISHED if capability.latest_snapshot_id is not None else None
            if capability.latest_snapshot_id is not None:
                snapshot = self.source_repository.get_snapshot(principal, scope, capability.latest_snapshot_id)
                snapshot_status = snapshot.status
                snapshot_matches = (
                    snapshot.snapshot_id == capability.latest_snapshot_id
                    and snapshot.binding == binding
                    and snapshot.source_partition == capability.latest_source_partition
                    and snapshot.source_revision == capability.latest_source_revision
                    and snapshot.available_cutoff == capability.latest_available_at
                )
            elif capability.state is SourceCapabilityState.READY:
                snapshot_matches = False
            now = postgres_facts.observed_at if postgres_facts is not None else datetime.now(timezone.utc)
            age_seconds = (
                max(0, int((now - capability.latest_available_at).total_seconds()))
                if capability.latest_available_at is not None
                else None
            )
            state, reason = _source_state(capability, age_seconds=age_seconds)
            if capability.latest_available_at is not None and capability.latest_available_at > now:
                state, reason = "ERROR", "FUTURE_SOURCE_CUTOFF"
            elif not snapshot_matches:
                state, reason = "ERROR", "SOURCE_SNAPSHOT_CAPABILITY_MISMATCH"
            elif snapshot_status is not None and snapshot_status is not SourceSnapshotStatus.PUBLISHED:
                state, reason = "PARTIAL", "PARTIAL_SNAPSHOT"
            elif capability.latest_snapshot_id is None and capability.state is SourceCapabilityState.READY:
                state, reason = "UNAVAILABLE", "SOURCE_SNAPSHOT_NOT_AVAILABLE"
            detail = self._source_detail(binding, state, reason, capability, age_seconds)
            return (
                OperationsAxis.create(
                    state,
                    reason,
                    {
                        "age_seconds": age_seconds if age_seconds is not None else 0,
                        "freshness_limit_seconds": capability.freshness_age_seconds,
                        "snapshot_available": snapshot_matches and snapshot_status is SourceSnapshotStatus.PUBLISHED,
                        "mapping_version": binding.mapping_version,
                        "source_revision_available": capability.latest_source_revision is not None,
                    },
                ),
                detail,
            )
        except AuthorizationDeniedError:
            reason = "SOURCE_AUTHORIZATION_REQUIRED"
            return OperationsAxis.create("UNAVAILABLE", reason, {"age_seconds": 0}), self._source_detail(binding, "UNAVAILABLE", reason, None, None)
        except Exception:
            reason = "O4_SOURCE_QUERY_UNAVAILABLE"
            return OperationsAxis.create("UNAVAILABLE", reason, {"age_seconds": 0}), self._source_detail(binding, "UNAVAILABLE", reason, None, None)

    @staticmethod
    def _source_detail(
        binding: MetrologySourceBinding,
        state: str,
        reason: str,
        capability: SourceCapabilityRecord | None,
        age_seconds: int | None,
    ) -> OperationsSourceDetail:
        return OperationsSourceDetail(
            state,
            reason,
            binding.source_id,
            binding.family_id,
            binding.capability_id,
            binding.mapping_version,
            binding.mapping_hash,
            capability.latest_source_revision if capability else None,
            capability.latest_source_partition if capability else None,
            _iso(capability.latest_available_at) if capability else None,
            age_seconds,
            capability.freshness_age_seconds if capability else None,
        )

    def _artifact_axis(
        self, principal: Principal, scope: AccessScope
    ) -> tuple[OperationsAxis, OperationsArtifactDetail]:
        empty = OperationsArtifactDetail(0, 0, 0, 0, False, ())
        if self.artifact_service is None:
            return OperationsAxis.create("UNAVAILABLE", "ARTIFACT_VERIFICATION_AUTHORITY_NOT_BOUND", {"age_seconds": 0}), empty
        try:
            self.current_authorization.authorize(principal, scope, ARTIFACT_READ_CAPABILITY)
            summary = self.artifact_service.inspect_scope(
                principal,
                scope,
                ARTIFACT_READ_CAPABILITY,
                limit=MAX_OPERATIONS_ARTIFACTS,
            )
        except AuthorizationDeniedError:
            return OperationsAxis.create("UNAVAILABLE", "ARTIFACT_AUTHORIZATION_REQUIRED", {"age_seconds": 0}), empty
        except Exception:
            return OperationsAxis.create("UNAVAILABLE", "ARTIFACT_QUERY_UNAVAILABLE", {"age_seconds": 0}), empty
        detail = OperationsArtifactDetail(
            summary.inspected_count,
            summary.missing_count,
            summary.corrupt_count,
            summary.other_unavailable_count,
            summary.truncated,
            summary.safe_reasons,
        )
        failed = summary.missing_count + summary.corrupt_count
        state = "ERROR" if failed else "READY"
        reason = "IMMUTABLE_ARTIFACT_BYTES_FAILED" if failed else "IMMUTABLE_ARTIFACT_CATALOG_REFERENCES_VERIFIED"
        facts = {
            "age_seconds": 0,
            "inspected_count": summary.inspected_count,
            "missing_count": summary.missing_count,
            "corrupt_count": summary.corrupt_count,
            "other_unavailable_count": summary.other_unavailable_count,
            "inventory_truncated": summary.truncated,
        }
        if summary.other_unavailable_count and not failed:
            state, reason = "UNAVAILABLE", "ARTIFACT_STORAGE_VERIFICATION_UNAVAILABLE"
        return OperationsAxis.create(state, reason, facts), detail

    def _qualification_axis(
        self, principal: Principal, scope: AccessScope
    ) -> tuple[OperationsAxis, OperationsQualificationDetail]:
        unbound = OperationsQualificationDetail("NOT_QUALIFIED", "QUALIFICATION_AUTHORITY_NOT_BOUND")
        if self.family_center is None or self.qualification_workspace_provider is None:
            return OperationsAxis.create("NOT_QUALIFIED", "QUALIFICATION_AUTHORITY_NOT_BOUND", {"age_seconds": 0}), unbound
        target: tuple[str, QualificationWorkspaceIdentity] | None = None
        try:
            # This permission check intentionally precedes target/workspace and
            # evidence existence reads in the Family Center authority.
            self.current_authorization.authorize(principal, scope, FAMILY_CENTER_READ)
            target = self.qualification_workspace_provider()
            if target is None:
                return OperationsAxis.create("NOT_QUALIFIED", "QUALIFICATION_AUTHORITY_NOT_BOUND", {"age_seconds": 0}), unbound
            family_id, identity = target
            if not isinstance(identity, QualificationWorkspaceIdentity) or identity.scope != scope:
                return OperationsAxis.create("UNAVAILABLE", "QUALIFICATION_TARGET_BINDING_MISMATCH", {"age_seconds": 0}), OperationsQualificationDetail("UNAVAILABLE", "QUALIFICATION_TARGET_BINDING_MISMATCH")
            view = self.family_center.get_workspace(principal, scope, identity.identity, current_identity=identity)
        except AuthorizationDeniedError:
            reason = "QUALIFICATION_AUTHORIZATION_REQUIRED"
            return OperationsAxis.create("UNAVAILABLE", reason, {"age_seconds": 0}), OperationsQualificationDetail("UNAVAILABLE", reason)
        except AggregateNotFoundError:
            if target is None:
                reason = "QUALIFICATION_WORKSPACE_NOT_FOUND"
                return OperationsAxis.create("NOT_QUALIFIED", reason, {"age_seconds": 0}), OperationsQualificationDetail("NOT_QUALIFIED", reason)
            family_id, identity = target
            reason = "QUALIFICATION_WORKSPACE_NOT_STARTED"
            detail = OperationsQualificationDetail(
                "NOT_QUALIFIED", reason, family_id, identity.capability_id, identity.release_id,
                f"/ephi/families/{quote(family_id, safe='')}",
            )
            return OperationsAxis.create("NOT_QUALIFIED", reason, {"age_seconds": 0}), detail
        except Exception:
            reason = "QUALIFICATION_QUERY_UNAVAILABLE"
            return OperationsAxis.create("UNAVAILABLE", reason, {"age_seconds": 0}), OperationsQualificationDetail("UNAVAILABLE", reason)

        gates = tuple(
            OperationsGateDetail(
                item.stage_id,
                item.state.value,
                _safe_reason_code(
                    item.invalidation_reason,
                    item.state.value if item.state is not GateState.PASS else "EVIDENCE_STATE_UNAVAILABLE",
                ) if item.state is not GateState.PASS else None,
                _iso(item.expires_at),
            )
            for item in view.gates
        )
        current_promotion = view.promotion_ready and any(item.state == "CURRENT" for item in view.promotions)
        if current_promotion:
            state, reason = "CURRENT", "CURRENT_GENERIC_QUALIFICATION_PROMOTION"
        elif any(item.state is GateState.EXPIRED for item in view.gates):
            state, reason = "EXPIRED", "QUALIFICATION_EVIDENCE_EXPIRED"
        elif any(item.state is GateState.STALE for item in view.gates):
            state, reason = "STALE", "QUALIFICATION_EVIDENCE_STALE"
        elif any(item.state is GateState.BLOCKED for item in view.gates):
            state, reason = "BLOCKED", "QUALIFICATION_GATES_BLOCKED"
        elif any(item.state is GateState.PENDING for item in view.gates):
            state, reason = "PENDING", "QUALIFICATION_EVIDENCE_PENDING"
        else:
            state, reason = "NOT_QUALIFIED", "QUALIFICATION_NOT_PROMOTED"
        detail = OperationsQualificationDetail(
            state,
            reason,
            view.family_id,
            view.capability_id,
            view.release_id,
            f"/ephi/families/{quote(family_id, safe='')}",
            gates,
        )
        return OperationsAxis.create(
            state if state in {item.value for item in OperationalState} else "NOT_QUALIFIED",
            reason,
            {
                "age_seconds": 0,
                "gate_count": len(gates),
                "expired_gate_count": sum(item.state == GateState.EXPIRED.value for item in gates),
                "pending_gate_count": sum(item.state == GateState.PENDING.value for item in gates),
                "synthetic_fixture": view.synthetic,
                "production_approval": False,
            },
        ), detail


def safe_identity_hash(value: object) -> str:
    """Hash an identity for evidence without emitting the identity itself."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: str | os.PathLike[str]) -> tuple[str, int]:
    """Return exact bytes identity and size for a regular file."""

    candidate = Path(path)
    digest = hashlib.sha256()
    size = 0
    with candidate.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def artifact_blob_path(root: str | os.PathLike[str], sha256: str) -> Path:
    if not isinstance(sha256, str) or len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise ValueError("artifact SHA-256 must be lowercase hexadecimal")
    return Path(root) / sha256[:2] / sha256[2:]


def verify_artifact_inventory(
    root: str | os.PathLike[str],
    inventory: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Verify immutable bytes against a catalog-derived hash/size inventory."""

    failures: list[dict[str, object]] = []
    for item in inventory:
        sha256 = item.get("sha256")
        expected_size = item.get("byte_size")
        if not isinstance(sha256, str) or not isinstance(expected_size, int):
            failures.append({"reason": "MANIFEST_INCONSISTENT"})
            continue
        path = artifact_blob_path(root, sha256)
        try:
            actual_sha256, actual_size = file_sha256(path)
        except (OSError, ValueError):
            failures.append({"sha256": sha256, "reason": "MISSING_ARTIFACT_BYTES"})
            continue
        if actual_sha256 != sha256 or actual_size != expected_size:
            failures.append({"sha256": sha256, "reason": "CORRUPT_ARTIFACT_BYTES"})
    return tuple(failures)


def build_reconciliation_report(
    *,
    backup_cutoff: Mapping[str, object],
    backup_tables: Mapping[str, Mapping[str, object]],
    current_tables: Mapping[str, Mapping[str, object]],
    observed_at: str,
) -> dict[str, object]:
    """Compare current durable identities with the captured high-water state."""

    deltas: list[dict[str, object]] = []
    classifications = {
        "command_receipt": "CONSEQUENTIAL_OR_EXTERNAL_REQUIRES_CONTROLLED_HANDLING",
        "audit_event": "AUDIT_ONLY_CONTROLLED_HANDLING",
        "outbox_event": "EXTERNAL_DELIVERY_REQUIRES_CONTROLLED_RECONCILIATION",
        "job": "SAFE_LOCAL_IDEMPOTENT_WORK_REPLAY_CANDIDATE",
        "applied_effect": "ALREADY_APPLIED_LOCAL_EFFECT_DO_NOT_REPLAY",
    }
    for table in sorted(set(backup_tables) | set(current_tables)):
        before_data = backup_tables.get(table, {})
        after_data = current_tables.get(table, {})
        before = set(before_data.get("row_identity_hashes", ()))
        after = set(after_data.get("row_identity_hashes", ()))
        before_rows = {row["identity_hash"]: row.get("row_hash") for row in before_data.get("row_versions", ())}
        after_rows = {row["identity_hash"]: row.get("row_hash") for row in after_data.get("row_versions", ())}
        new_rows = sorted(after - before)
        changed_rows = sorted(identity for identity in (after & before) if before_rows.get(identity) != after_rows.get(identity))
        if new_rows or changed_rows:
            deltas.append(
                {
                    "table": table,
                    "new_identity_hashes": new_rows,
                    "changed_identity_hashes": changed_rows,
                    "count": len(new_rows) + len(changed_rows),
                    "classification": classifications.get(table, "CONTROLLED_RECONCILIATION_REQUIRED"),
                }
            )
    delta_window: float | None = None
    try:
        cutoff = datetime.fromisoformat(str(backup_cutoff["cutoff_at_server"]).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        delta_window = round(max(0.0, (observed - cutoff).total_seconds()), 6)
    except (KeyError, TypeError, ValueError):
        pass
    return {
        "schema_version": "o9.1.reconciliation.v1",
        "backup_cutoff": dict(backup_cutoff),
        "observed_at": observed_at,
        "post_cutoff_writes_present_in_restored_snapshot": False,
        "post_cutoff_delta_count": sum(int(item["count"]) for item in deltas),
        "post_cutoff_deltas": deltas,
        "observed_post_cutoff_delta_window_seconds": delta_window,
        "required_controlled_recovery_action": bool(deltas),
        "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
        "timing_evidence_scope": "LOCAL_RESTORE_REHEARSAL_ONLY",
    }


def json_bytes(value: Mapping[str, object]) -> bytes:
    """Stable JSON bytes for manifests, reports, and carrier hashing."""

    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


__all__ = [
    "OperationalState",
    "OperationsAxis",
    "OperationsHealthSnapshot",
    "operations_health_snapshot",
    "safe_identity_hash",
    "file_sha256",
    "canonical_sha256",
    "artifact_blob_path",
    "verify_artifact_inventory",
    "build_reconciliation_report",
    "json_bytes",
]
