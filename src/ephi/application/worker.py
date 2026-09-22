"""Storage-neutral durable worker, lease and local-effect contracts.

The queue is at-least-once.  Exactly-once semantics apply only to bounded
LOCAL database effects, using durable effect receipts supplied by a storage
adapter.  Domain job handlers and worker loops are intentionally out of this
module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from .context import AccessScope


DEFAULT_LEASE_DURATION = timedelta(seconds=120)
DEFAULT_HEARTBEAT_INTERVAL = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class WorkerLeaseConfig:
    """Documented worker defaults with explicit short test overrides allowed."""

    lease_duration: timedelta = DEFAULT_LEASE_DURATION
    heartbeat_interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL


@dataclass(frozen=True, slots=True)
class WorkerLease:
    job_id: str
    owner: str
    epoch: int

    @property
    def lease_owner(self) -> str:
        return self.owner

    @property
    def lease_epoch(self) -> int:
        return self.epoch


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    scope_key: str
    job_type: str
    semantic_key: str
    payload_hash: str
    payload: dict[str, Any]
    status: str
    priority: int
    available_at: datetime
    attempts: int
    max_attempts: int
    lease_owner: str | None
    lease_epoch: int
    lease_expires_at: datetime | None
    last_failure_code: str | None
    last_failure_message: str | None
    last_failure_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def lease(self) -> WorkerLease:
        if self.lease_owner is None:
            raise ValueError("job does not currently have an owner")
        return WorkerLease(self.job_id, self.lease_owner, self.lease_epoch)


@dataclass(frozen=True, slots=True)
class AppliedEffectReceipt:
    job_id: str
    effect_key: str
    input_hash: str
    committed_revision: int
    result_identity: str
    result: dict[str, Any]
    committed_at: datetime


LocalEffect = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]


@runtime_checkable
class WorkerJobPort(Protocol):
    """The reusable durable queue/lease/effect port."""

    def enqueue(
        self,
        scope: AccessScope,
        job_type: str,
        semantic_key: str,
        payload: Mapping[str, object],
        *,
        available_at: datetime | None = None,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> JobRecord: ...

    def claim(self, scope: AccessScope, owner: str, *, raise_if_none: bool = False, job_type: str | None = None) -> JobRecord | None: ...

    def heartbeat(self, lease: WorkerLease) -> JobRecord: ...

    def complete(self, lease: WorkerLease) -> JobRecord: ...

    def fail(
        self,
        lease: WorkerLease,
        *,
        retryable: bool,
        error_code: str,
        error_message: str,
        next_available_at: datetime | None = None,
    ) -> JobRecord: ...

    def defer(self, lease: WorkerLease, available_at: datetime) -> JobRecord: ...

    def cancel(self, scope: AccessScope, job_id: str, *, lease: WorkerLease | None = None) -> JobRecord: ...

    def inspect(
        self,
        scope: AccessScope,
        *,
        job_id: str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 100,
    ) -> tuple[JobRecord, ...]: ...

    def commit_local_effect(
        self,
        lease: WorkerLease,
        effect_key: str,
        input_payload: Mapping[str, object],
        *,
        aggregate_type: str,
        aggregate_id: str,
        mutation: LocalEffect | None = None,
    ) -> AppliedEffectReceipt: ...
