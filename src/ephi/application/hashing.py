"""Deterministic, fail-closed command payload normalization and hashing."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
from typing import Any

from .context import AccessScope, RevisionVector
from .errors import ValidationFailureError


_TRANSPORT_FIELDS = frozenset(
    {
        "request_id",
        "trace_id",
        "correlation_id",
        "authorization",
        "credential",
        "credentials",
        "password",
        "token",
        "access_token",
    }
)


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValidationFailureError("non-finite Decimal values are not canonical")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def normalize_domain_payload(value: object, *, _path: str = "payload") -> Any:
    """Return a JSON-shaped canonical value or reject unsupported values.

    Floats, sets, bytes, arbitrary objects and transport/credential fields are
    rejected.  The command layer therefore never silently hashes an object's
    repr or turns non-canonical input into accidental domain data.
    """

    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and "\x00" in value:
            raise ValidationFailureError(f"{_path} contains a NUL character")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationFailureError(f"{_path} contains a non-finite float")
        raise ValidationFailureError(f"{_path} contains unsupported float data")
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_text(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationFailureError(f"{_path} datetime must be timezone-aware")
        utc_value = value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return {"$datetime": utc_value}
    if isinstance(value, Enum):
        return normalize_domain_payload(value.value, _path=_path)
    if isinstance(value, AccessScope):
        return {"$access_scope": normalize_domain_payload(value.as_dict(), _path=f"{_path}.scope")}
    if isinstance(value, RevisionVector):
        return {"$revision_vector": normalize_domain_payload(value.as_dict(), _path=f"{_path}.revisions")}
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or "\x00" in key:
                raise ValidationFailureError(f"{_path} mapping keys must be non-empty strings")
            if key.lower() in _TRANSPORT_FIELDS:
                raise ValidationFailureError(f"{_path}.{key} is transport data, not domain payload")
            if key in result:
                raise ValidationFailureError(f"{_path} contains duplicate key {key!r}")
            result[key] = normalize_domain_payload(item, _path=f"{_path}.{key}")
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, (list, tuple)):
        return [normalize_domain_payload(item, _path=f"{_path}[{index}]") for index, item in enumerate(value)]
    raise ValidationFailureError(f"{_path} contains unsupported value type {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Serialize an already-normalized value with one stable JSON spelling."""

    normalized = normalize_domain_payload(value)
    try:
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (UnicodeEncodeError, TypeError, ValueError) as exc:
        raise ValidationFailureError("value cannot be canonically serialized") from exc


def canonical_command_payload_hash(
    command_type: str,
    scope: AccessScope,
    expected_workflow_version: int | None,
    viewed_revisions: RevisionVector | None,
    domain_payload: object,
    *,
    target: Mapping[str, str] | None = None,
    reason: str | None = None,
) -> str:
    """Hash semantic command identity while excluding transport identity.

    ``command_id``, request/trace IDs, credentials and the Principal are not
    arguments to this function.  They cannot accidentally become part of the
    semantic payload hash.
    """

    if not isinstance(command_type, str) or not command_type or command_type != command_type.strip():
        raise ValidationFailureError("command_type must be a non-empty canonical string")
    if not isinstance(scope, AccessScope):
        raise ValidationFailureError("scope must be an AccessScope")
    if expected_workflow_version is not None and (
        isinstance(expected_workflow_version, bool)
        or not isinstance(expected_workflow_version, int)
        or expected_workflow_version < 0
    ):
        raise ValidationFailureError("expected_workflow_version must be a non-negative integer or None")
    if viewed_revisions is not None and not isinstance(viewed_revisions, RevisionVector):
        raise ValidationFailureError("viewed_revisions must be a RevisionVector or None")
    if target is not None:
        if not isinstance(target, Mapping) or set(target) != {"aggregate_type", "aggregate_id"}:
            raise ValidationFailureError("target must contain aggregate_type and aggregate_id only")
        for field, value in target.items():
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValidationFailureError(f"target.{field} must be a non-empty canonical string")
    if reason is not None and (not isinstance(reason, str) or not reason or reason != reason.strip()):
        raise ValidationFailureError("reason must be a non-empty canonical string or None")
    envelope = {
        "command_type": command_type,
        "target": target,
        "scope": scope,
        "expected_workflow_version": expected_workflow_version,
        "viewed_revisions": viewed_revisions,
        "reason": reason,
        "domain_payload": domain_payload,
    }
    serialized = canonical_json(envelope).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


command_payload_hash = canonical_command_payload_hash
