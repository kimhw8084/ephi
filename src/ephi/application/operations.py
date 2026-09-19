"""Typed, secret-safe operational health and restore evidence contracts.

This module deliberately contains no second operational state store. It only
normalizes observations made against the existing PostgreSQL, worker, read,
artifact, and source authorities into bounded evidence values.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path

from .hashing import canonical_json


class OperationalState(StrEnum):
    READY = "READY"
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
