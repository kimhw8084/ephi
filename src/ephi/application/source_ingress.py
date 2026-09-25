"""Typed, fail-closed ingress contracts for one metrology source family.

This module contains no company schema knowledge and no raw telemetry storage.
Adapters validate a bounded partition here, while PostgreSQL stores only the
immutable manifest and the bounded capability projection.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import math
import re
import unicodedata
from typing import Protocol

from .artifacts import ArtifactServicePort, ScopedArtifactReference
from .context import AccessScope, Principal
from .errors import (
    ArtifactError,
    AuthorizationDeniedError,
    ScopeDeniedError,
    SourceQuarantineError,
    ValidationFailureError,
)


SOURCE_READ_CAPABILITY = "ephi.source.read"
SOURCE_INGEST_CAPABILITY = "ephi.source.ingest"
SOURCE_ARTIFACT_READ_CAPABILITY = "ephi.source.artifact.read"
SOURCE_SNAPSHOT_SCHEMA_VERSION = "o4.1.v1"
MAX_SOURCE_ROWS = 100_000
MAX_SOURCE_ID_BYTES = 256
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# These are accepted spellings only.  No conversion is performed by this
# boundary.  A family binding must declare exactly one of them and every row
# must carry that exact unit.
SUPPORTED_UNITS = frozenset(
    {
        "a",
        "c",
        "cm",
        "count",
        "deg",
        "in",
        "k",
        "kpa",
        "m",
        "mm",
        "ms",
        "nm",
        "ns",
        "ohm",
        "pa",
        "ppm",
        "rad",
        "s",
        "um",
        "us",
        "v",
        "%",
    }
)


class SourceSnapshotStatus(StrEnum):
    PUBLISHED = "PUBLISHED"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    QUARANTINED = "QUARANTINED"


class SourceCapabilityState(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    INSUFFICIENT = "INSUFFICIENT"
    ERROR = "ERROR"
    NOT_QUALIFIED = "NOT_QUALIFIED"


def _canonical_id(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise SourceQuarantineError(f"{field} must be an explicit canonical identifier")
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized != normalized.strip()
        or "\x00" in normalized
        or any(ord(char) < 0x20 or 0x7F <= ord(char) < 0xA0 for char in normalized)
        or len(normalized.encode("utf-8")) > MAX_SOURCE_ID_BYTES
    ):
        raise SourceQuarantineError(f"{field} is missing, ambiguous, or outside the identifier bound")
    return normalized


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SourceQuarantineError(f"{field} must be canonical lowercase SHA-256")
    return value


def _timestamp(value: object, field: str) -> datetime:
    """Parse only an unambiguous timezone-aware instant and normalize UTC."""

    parsed = value
    if isinstance(value, str):
        if "T" not in value or not (value.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", value)):
            raise SourceQuarantineError(f"{field} must include an explicit UTC offset or Z")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SourceQuarantineError(f"{field} is not a valid timestamp") from exc
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceQuarantineError(f"{field} must be timezone-aware")
    # A zoneinfo value can carry either side of a DST fold.  Requiring a fixed
    # offset or an explicitly selected, unambiguous fold prevents guessing.
    try:
        if parsed.tzinfo is not timezone.utc:
            first = parsed.replace(fold=0).utcoffset()
            second = parsed.replace(fold=1).utcoffset()
            if first != second:
                raise SourceQuarantineError(f"{field} has an ambiguous timezone fold")
    except SourceQuarantineError:
        raise
    except Exception as exc:
        raise SourceQuarantineError(f"{field} timezone interpretation is unavailable") from exc
    return parsed.astimezone(timezone.utc)


def _unit(value: object, field: str = "unit") -> str:
    normalized = _canonical_id(value, field)
    assert normalized is not None
    if normalized not in SUPPORTED_UNITS:
        raise SourceQuarantineError(f"{field} is unsupported; no conversion is guessed")
    return normalized


def _finite_number(value: object, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceQuarantineError(f"{field} must be a finite numeric observation")
    if not math.isfinite(float(value)):
        raise SourceQuarantineError(f"{field} must be finite")
    return value


@dataclass(frozen=True, slots=True)
class MetrologySourceBinding:
    """One explicitly declared family/capability and its canonical mapping."""

    scope: AccessScope
    source_id: str
    provider_id: str
    family_id: str
    capability_id: str
    adapter_id: str
    schema_id: str
    mapping_version: str
    mapping_hash: str
    unit: str
    reference_population_id: str | None = None
    comparable_population_id: str | None = None
    required_identifiers: tuple[str, ...] = ("asset_id", "context_id", "characteristic_id")

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AccessScope):
            raise SourceQuarantineError("source binding scope must be an AccessScope")
        for field in (
            "source_id",
            "provider_id",
            "family_id",
            "capability_id",
            "adapter_id",
            "schema_id",
            "mapping_version",
        ):
            object.__setattr__(self, field, _canonical_id(getattr(self, field), field))
        object.__setattr__(self, "mapping_hash", _sha256(self.mapping_hash, "mapping_hash"))
        object.__setattr__(self, "unit", _unit(self.unit))
        for field in ("reference_population_id", "comparable_population_id"):
            object.__setattr__(self, field, _canonical_id(getattr(self, field), field, optional=True))
        if not isinstance(self.required_identifiers, (tuple, list, frozenset)):
            raise SourceQuarantineError("required_identifiers must be an explicit identifier tuple")
        allowed = {"asset_id", "tool_id", "head_id", "context_id", "characteristic_id"}
        required = tuple(sorted(set(self.required_identifiers)))
        if not required or any(item not in allowed for item in required):
            raise SourceQuarantineError("required_identifiers contains an unsupported or ambiguous identifier")
        object.__setattr__(self, "required_identifiers", required)

    @property
    def scope_key(self) -> str:
        return self.scope.canonical_key

    def validate_observation(self, observation: "MetrologyObservation") -> None:
        if not isinstance(observation, MetrologyObservation):
            raise SourceQuarantineError("source partition contains an untyped observation")
        if observation.unit != self.unit:
            raise SourceQuarantineError("observation unit does not match the declared family mapping; no conversion guessed")
        for identifier in self.required_identifiers:
            if getattr(observation, identifier) is None:
                raise SourceQuarantineError(f"required {identifier} identifier is absent")
        if self.reference_population_id is not None and observation.reference_population_id != self.reference_population_id:
            raise SourceQuarantineError("reference population identity is missing or conflicts with the binding")
        if self.comparable_population_id is not None and observation.comparable_population_id != self.comparable_population_id:
            raise SourceQuarantineError("comparable population identity is missing or conflicts with the binding")

    def as_dict(self) -> dict[str, object]:
        return {
            "scope_key": self.scope_key,
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "family_id": self.family_id,
            "capability_id": self.capability_id,
            "adapter_id": self.adapter_id,
            "schema_id": self.schema_id,
            "mapping_version": self.mapping_version,
            "mapping_hash": self.mapping_hash,
            "unit": self.unit,
            "reference_population_id": self.reference_population_id,
            "comparable_population_id": self.comparable_population_id,
            "required_identifiers": list(self.required_identifiers),
        }


@dataclass(frozen=True, slots=True)
class MetrologyObservation:
    """Validated bounded-row identity/time/unit facts; raw values stay upstream."""

    source_row_id: str
    asset_id: str | None
    tool_id: str | None
    head_id: str | None
    context_id: str | None
    characteristic_id: str | None
    unit: str
    value: int | float
    event_at: datetime
    source_available_at: datetime
    reference_population_id: str | None = None
    comparable_population_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_row_id", _canonical_id(self.source_row_id, "source_row_id"))
        for field in ("asset_id", "tool_id", "head_id", "context_id", "characteristic_id"):
            object.__setattr__(self, field, _canonical_id(getattr(self, field), field, optional=True))
        object.__setattr__(self, "unit", _unit(self.unit))
        object.__setattr__(self, "value", _finite_number(self.value, "value"))
        object.__setattr__(self, "event_at", _timestamp(self.event_at, "event_at"))
        object.__setattr__(self, "source_available_at", _timestamp(self.source_available_at, "source_available_at"))
        if self.source_available_at < self.event_at:
            raise SourceQuarantineError("source_available_at cannot precede event_at")
        for field in ("reference_population_id", "comparable_population_id"):
            object.__setattr__(self, field, _canonical_id(getattr(self, field), field, optional=True))


@dataclass(frozen=True, slots=True)
class RevisionPinnedObservationBatch:
    """Bounded proof envelope for rows read from one exact O4 revision."""

    source_partition: str
    source_revision: str
    binding: MetrologySourceBinding
    observations: tuple[MetrologyObservation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_partition", _canonical_id(self.source_partition, "source_partition"))
        object.__setattr__(self, "source_revision", _canonical_id(self.source_revision, "source_revision"))
        if not isinstance(self.binding, MetrologySourceBinding):
            raise SourceQuarantineError("revision-pinned observation batch requires its exact source binding")
        if not isinstance(self.observations, (tuple, list)) or len(self.observations) > MAX_SOURCE_ROWS:
            raise SourceQuarantineError("revision-pinned observation batch exceeds the bounded row contract")
        observations = tuple(self.observations)
        if any(not isinstance(item, MetrologyObservation) for item in observations):
            raise SourceQuarantineError("revision-pinned observation batch contains a non-canonical row")
        object.__setattr__(self, "observations", observations)


@dataclass(frozen=True, slots=True)
class SourceSnapshotDraft:
    """Bounded immutable-publication input; no raw rows cross the persistence port."""

    binding: MetrologySourceBinding
    source_partition: str
    source_revision: str
    event_start: datetime
    event_end: datetime
    available_cutoff: datetime
    artifact_reference: ScopedArtifactReference
    observations: tuple[MetrologyObservation, ...]
    status: SourceSnapshotStatus = SourceSnapshotStatus.PUBLISHED
    freshness_age_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.binding, MetrologySourceBinding):
            raise SourceQuarantineError("source snapshot requires one typed metrology binding")
        for field in ("source_partition", "source_revision"):
            object.__setattr__(self, field, _canonical_id(getattr(self, field), field))
        object.__setattr__(self, "event_start", _timestamp(self.event_start, "event_start"))
        object.__setattr__(self, "event_end", _timestamp(self.event_end, "event_end"))
        object.__setattr__(self, "available_cutoff", _timestamp(self.available_cutoff, "available_cutoff"))
        if self.event_end < self.event_start:
            raise SourceQuarantineError("event_end cannot precede event_start")
        if not isinstance(self.artifact_reference, ScopedArtifactReference):
            raise SourceQuarantineError("source snapshot requires a scoped immutable artifact reference")
        if self.artifact_reference.scope != self.binding.scope:
            raise ScopeDeniedError("source artifact scope does not match the source binding scope")
        if not isinstance(self.observations, (tuple, list)):
            raise SourceQuarantineError("source observations must be a bounded tuple")
        if len(self.observations) > MAX_SOURCE_ROWS:
            raise SourceQuarantineError("source partition exceeds the bounded ingress row limit")
        if not isinstance(self.status, SourceSnapshotStatus):
            try:
                object.__setattr__(self, "status", SourceSnapshotStatus(self.status))
            except ValueError as exc:
                raise SourceQuarantineError("source snapshot status is unsupported") from exc
        if self.freshness_age_seconds is not None and (
            isinstance(self.freshness_age_seconds, bool)
            or not isinstance(self.freshness_age_seconds, int)
            or self.freshness_age_seconds <= 0
        ):
            raise SourceQuarantineError("freshness_age_seconds must be a positive integer when declared")
        for observation in self.observations:
            self.binding.validate_observation(observation)
            if not self.event_start <= observation.event_at <= self.event_end:
                raise SourceQuarantineError("observation event_at is outside the declared event window")
            if observation.source_available_at > self.available_cutoff:
                raise SourceQuarantineError("available_cutoff is earlier than a source fact in the snapshot")
            if observation.event_at > self.available_cutoff:
                raise SourceQuarantineError("available_cutoff cannot precede the event being ingested")
        if self.status is SourceSnapshotStatus.PUBLISHED and not self.observations:
            raise SourceQuarantineError("an empty source partition cannot be published READY")

    @property
    def row_count(self) -> int:
        return len(self.observations)

    @property
    def snapshot_id(self) -> str:
        payload = {
            "scope_key": self.binding.scope_key,
            "source_id": self.binding.source_id,
            "family_id": self.binding.family_id,
            "capability_id": self.binding.capability_id,
            "source_partition": self.source_partition,
            "source_revision": self.source_revision,
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    @property
    def manifest_hash(self) -> str:
        payload = {
            "binding": self.binding.as_dict(),
            "source_partition": self.source_partition,
            "source_revision": self.source_revision,
            "event_start": self.event_start.isoformat(),
            "event_end": self.event_end.isoformat(),
            "available_cutoff": self.available_cutoff.isoformat(),
            "artifact_sha256": self.artifact_reference.content.sha256,
            "artifact_byte_size": self.artifact_reference.content.byte_size,
            "row_count": self.row_count,
            "status": self.status.value,
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def immutable_identity(self) -> tuple[object, ...]:
        return (
            self.snapshot_id,
            self.manifest_hash,
            self.binding.scope_key,
            self.binding.source_id,
            self.binding.family_id,
            self.binding.capability_id,
            self.source_partition,
            self.source_revision,
            self.event_start,
            self.event_end,
            self.available_cutoff,
            self.binding.mapping_hash,
            self.binding.mapping_version,
            self.binding.unit,
            self.artifact_reference.content.sha256,
            self.artifact_reference.content.byte_size,
            self.row_count,
            self.status.value,
            self.freshness_age_seconds,
        )


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _authorize(principal: Principal, scope: AccessScope, capability: str) -> None:
    if not isinstance(principal, Principal):
        raise AuthorizationDeniedError("a current server-derived Principal is required")
    if not principal.grants_scope(scope):
        raise ScopeDeniedError("principal is not granted the requested source scope")
    if not principal.has_capability(capability):
        raise AuthorizationDeniedError("principal is not currently granted the required source capability")


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    return _timestamp(clock(), "ingested_at")


@dataclass(frozen=True, slots=True)
class SourceSnapshotRecord:
    snapshot_id: str
    binding: MetrologySourceBinding
    source_partition: str
    source_revision: str
    event_start: datetime
    event_end: datetime
    available_cutoff: datetime
    artifact_reference: ScopedArtifactReference
    row_count: int
    status: SourceSnapshotStatus
    manifest_hash: str
    ingested_at: datetime
    published_at: datetime
    created_at: datetime
    schema_version: str = SOURCE_SNAPSHOT_SCHEMA_VERSION
    freshness_age_seconds: int | None = None

    def __post_init__(self) -> None:
        value = self.freshness_age_seconds
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValidationFailureError("freshness_age_seconds must be a positive integer when declared")

    @property
    def immutable_identity(self) -> tuple[object, ...]:
        return (
            self.snapshot_id,
            self.manifest_hash,
            self.binding.scope_key,
            self.binding.source_id,
            self.binding.family_id,
            self.binding.capability_id,
            self.source_partition,
            self.source_revision,
            self.event_start,
            self.event_end,
            self.available_cutoff,
            self.binding.mapping_hash,
            self.binding.mapping_version,
            self.binding.unit,
            self.artifact_reference.content.sha256,
            self.artifact_reference.content.byte_size,
            self.row_count,
            self.status.value,
            self.freshness_age_seconds,
        )


@dataclass(frozen=True, slots=True)
class SourceCapabilityRecord:
    binding: MetrologySourceBinding
    state: SourceCapabilityState
    latest_snapshot_id: str | None
    latest_event_at: datetime | None
    latest_available_at: datetime | None
    checked_at: datetime
    freshness_age_seconds: int
    reason: str
    latest_source_partition: str | None = None
    latest_source_revision: str | None = None
    schema_version: str = SOURCE_SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.binding, MetrologySourceBinding):
            raise ValidationFailureError("capability record requires a metrology binding")
        if not isinstance(self.state, SourceCapabilityState):
            object.__setattr__(self, "state", SourceCapabilityState(self.state))
        for field in ("latest_snapshot_id", "latest_source_partition", "latest_source_revision"):
            object.__setattr__(self, field, _canonical_id(getattr(self, field), field, optional=True))
        for field in ("latest_event_at", "latest_available_at"):
            value = getattr(self, field)
            object.__setattr__(self, field, None if value is None else _timestamp(value, field))
        object.__setattr__(self, "checked_at", _timestamp(self.checked_at, "checked_at"))
        if isinstance(self.freshness_age_seconds, bool) or not isinstance(self.freshness_age_seconds, int) or self.freshness_age_seconds <= 0:
            raise ValidationFailureError("freshness_age_seconds must be a positive integer")
        object.__setattr__(self, "reason", _canonical_id(self.reason, "reason"))

    @property
    def fresh(self) -> bool:
        if self.latest_available_at is None:
            return False
        return self.checked_at - self.latest_available_at <= timedelta(seconds=self.freshness_age_seconds)


class SourceSnapshotRepository(Protocol):
    def publish_snapshot(
        self,
        draft: SourceSnapshotDraft,
        ingested_at: datetime,
        capability: SourceCapabilityRecord,
    ) -> tuple[SourceSnapshotRecord, SourceCapabilityRecord]: ...

    def get_snapshot(self, principal: Principal, scope: AccessScope, snapshot_id: str) -> SourceSnapshotRecord: ...

    def get_latest_snapshot_as_of(
        self,
        principal: Principal,
        binding: MetrologySourceBinding,
        knowledge_cutoff: datetime,
    ) -> SourceSnapshotRecord | None: ...

    def get_capability(
        self,
        principal: Principal,
        binding: MetrologySourceBinding,
    ) -> SourceCapabilityRecord: ...


class SourceSnapshotIngressService:
    """Validate, verify and atomically publish one bounded source partition."""

    def __init__(
        self,
        repository: SourceSnapshotRepository,
        artifact_service: ArtifactServicePort,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not hasattr(repository, "publish_snapshot"):
            raise ValidationFailureError("source ingress requires a durable snapshot repository")
        if not hasattr(artifact_service, "verify_publish_preconditions"):
            raise ValidationFailureError("source ingress requires the existing artifact verification boundary")
        self.repository = repository
        self.artifact_service = artifact_service
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def publish(
        self,
        principal: Principal,
        draft: SourceSnapshotDraft,
        *,
        freshness_age_seconds: int,
    ) -> tuple[SourceSnapshotRecord, SourceCapabilityRecord]:
        if not isinstance(draft, SourceSnapshotDraft):
            raise ValidationFailureError("source ingress requires a SourceSnapshotDraft")
        _authorize(principal, draft.binding.scope, SOURCE_INGEST_CAPABILITY)
        if isinstance(freshness_age_seconds, bool) or not isinstance(freshness_age_seconds, int) or freshness_age_seconds <= 0:
            raise ValidationFailureError("freshness_age_seconds must be a positive integer")
        ingested_at = _utc_now(self.clock)
        if draft.available_cutoff > ingested_at or draft.event_end > ingested_at:
            raise SourceQuarantineError("future source facts cannot be published from the current ingress instant")
        try:
            self.artifact_service.verify_publish_preconditions(
                principal,
                (draft.artifact_reference,),
                SOURCE_ARTIFACT_READ_CAPABILITY,
            )
        except ArtifactError:
            raise
        state, reason = _capability_state(draft.status, draft.row_count, draft.available_cutoff, ingested_at, freshness_age_seconds)
        if draft.freshness_age_seconds is not None and draft.freshness_age_seconds != freshness_age_seconds:
            raise ValidationFailureError("snapshot freshness policy conflicts with the declared publication policy")
        draft = replace(draft, freshness_age_seconds=freshness_age_seconds)
        capability = SourceCapabilityRecord(
            draft.binding,
            state,
            draft.snapshot_id,
            draft.event_end,
            draft.available_cutoff,
            ingested_at,
            freshness_age_seconds,
            reason,
            draft.source_partition,
            draft.source_revision,
        )
        return self.repository.publish_snapshot(draft, ingested_at, capability)


def _capability_state(
    status: SourceSnapshotStatus,
    row_count: int,
    available_cutoff: datetime,
    checked_at: datetime,
    age_limit_seconds: int,
) -> tuple[SourceCapabilityState, str]:
    if status is SourceSnapshotStatus.PARTIAL:
        return SourceCapabilityState.PARTIAL, "PARTIAL_SNAPSHOT"
    if status is SourceSnapshotStatus.INSUFFICIENT or row_count == 0:
        return SourceCapabilityState.INSUFFICIENT, "NO_SUFFICIENT_SOURCE_ROWS"
    if status is SourceSnapshotStatus.QUARANTINED:
        return SourceCapabilityState.UNAVAILABLE, "SOURCE_QUARANTINED"
    age = checked_at - available_cutoff
    if age > timedelta(seconds=age_limit_seconds):
        return SourceCapabilityState.STALE, "SOURCE_AVAILABILITY_EXCEEDS_FRESHNESS_LIMIT"
    return SourceCapabilityState.READY, "FRESH_PUBLISHED_SNAPSHOT"


def as_known_eligible(record: SourceSnapshotRecord, knowledge_cutoff: datetime) -> bool:
    """Return true only when the complete published snapshot was knowable then."""

    cutoff = _timestamp(knowledge_cutoff, "knowledge_cutoff")
    if record.status is not SourceSnapshotStatus.PUBLISHED or record.row_count <= 0:
        return False
    return all(
        value <= cutoff
        for value in (
            record.event_end,
            record.available_cutoff,
            record.ingested_at,
            record.published_at,
        )
    )


def source_replay_eligible(record: SourceSnapshotRecord, knowledge_cutoff: datetime) -> bool:
    """Return true only for source facts independently available by the cutoff."""

    cutoff = _timestamp(knowledge_cutoff, "knowledge_cutoff")
    if record.status is SourceSnapshotStatus.QUARANTINED or record.row_count <= 0:
        return False
    return record.event_end <= cutoff and record.available_cutoff <= cutoff


def validate_binding_observation(binding: MetrologySourceBinding, observation: MetrologyObservation) -> None:
    binding.validate_observation(observation)


__all__ = [
    "MAX_SOURCE_ROWS",
    "SOURCE_ARTIFACT_READ_CAPABILITY",
    "SOURCE_INGEST_CAPABILITY",
    "SOURCE_READ_CAPABILITY",
    "SUPPORTED_UNITS",
    "MetrologyObservation",
    "MetrologySourceBinding",
    "RevisionPinnedObservationBatch",
    "SourceCapabilityRecord",
    "SourceCapabilityState",
    "SourceSnapshotDraft",
    "SourceSnapshotRecord",
    "SourceSnapshotRepository",
    "SourceSnapshotStatus",
    "SourceSnapshotIngressService",
    "as_known_eligible",
    "source_replay_eligible",
    "validate_binding_observation",
]
