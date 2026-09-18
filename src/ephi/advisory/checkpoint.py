"""Strict, deterministic JSON checkpoint contract for advisory state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .model import AdvisoryEpisode, EngineeringWorkState, TechnicalEpisodeState


CHECKPOINT_SCHEMA = "ephi.advisory.checkpoint"
CHECKPOINT_VERSION = 1
_CHECKPOINT_FIELDS = frozenset(
    {
        "checkpoint_schema",
        "checkpoint_version",
        "episode_id",
        "technical_state",
        "engineering_work_state",
        "workflow_version",
        "revision_id",
    }
)


class CheckpointError(ValueError):
    """Base class for fail-closed checkpoint validation errors."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class IncompleteCheckpointError(CheckpointError):
    """A source-only or partial payload lacks complete workflow authority."""

    def __init__(self, fields: list[str]):
        super().__init__(
            "INCOMPLETE_AUTHORITY",
            f"checkpoint is missing required authority fields: {', '.join(fields)}",
        )


class CheckpointSchemaError(CheckpointError):
    """The checkpoint schema or version is unsupported or malformed."""


class CheckpointIdentityError(CheckpointError):
    """The checkpoint identity does not match the restore boundary."""


class CheckpointStateError(CheckpointError):
    """Checkpoint state fields are invalid or internally incoherent."""


def _duplicate_key_error(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError("MALFORMED_PAYLOAD", f"duplicate checkpoint field: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise CheckpointError("MALFORMED_PAYLOAD", f"non-finite checkpoint number: {value}")


def _decode(payload: str | bytes | bytearray | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return dict(payload)
    if not isinstance(payload, (str, bytes, bytearray)):
        raise CheckpointError("MALFORMED_PAYLOAD", "checkpoint must be JSON text or an object")
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_duplicate_key_error,
            parse_constant=_reject_nonfinite,
        )
    except CheckpointError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise CheckpointError("MALFORMED_PAYLOAD", f"invalid checkpoint JSON: {exc}") from exc
    if not isinstance(decoded, Mapping):
        raise CheckpointError("MALFORMED_PAYLOAD", "checkpoint JSON must contain an object")
    return decoded


def serialize_checkpoint(episode: AdvisoryEpisode) -> str:
    """Serialize one complete aggregate into deterministic JSON text."""

    if not isinstance(episode, AdvisoryEpisode):
        raise TypeError("checkpoint source must be AdvisoryEpisode")
    payload = {
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "checkpoint_version": CHECKPOINT_VERSION,
        **episode.as_dict(),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save_checkpoint(episode: AdvisoryEpisode) -> str:
    """Public save name for the deterministic checkpoint contract."""

    return serialize_checkpoint(episode)


def restore_checkpoint(
    payload: str | bytes | bytearray | Mapping[str, Any],
    *,
    expected_episode_id: str | None = None,
) -> AdvisoryEpisode:
    """Restore a complete aggregate, rejecting partial or incoherent authority."""

    decoded = _decode(payload)
    if any(not isinstance(key, str) for key in decoded):
        raise CheckpointError("MALFORMED_PAYLOAD", "checkpoint fields must be strings")
    missing = sorted(_CHECKPOINT_FIELDS - set(decoded))
    if missing:
        raise IncompleteCheckpointError(missing)
    extra = sorted(set(decoded) - _CHECKPOINT_FIELDS)
    if extra:
        raise CheckpointError("MALFORMED_PAYLOAD", f"unsupported checkpoint fields: {', '.join(extra)}")

    if decoded["checkpoint_schema"] != CHECKPOINT_SCHEMA:
        raise CheckpointSchemaError("UNSUPPORTED_SCHEMA", "unsupported checkpoint schema")
    version = decoded["checkpoint_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != CHECKPOINT_VERSION:
        raise CheckpointSchemaError("UNSUPPORTED_VERSION", "unsupported checkpoint version")

    episode_id = decoded["episode_id"]
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise CheckpointIdentityError("INVALID_IDENTITY", "checkpoint episode_id must be a non-empty string")
    if expected_episode_id is not None and episode_id != expected_episode_id:
        raise CheckpointIdentityError("IDENTITY_MISMATCH", "checkpoint episode identity does not match restore target")

    try:
        technical_state = TechnicalEpisodeState(decoded["technical_state"])
        engineering_work_state = EngineeringWorkState(decoded["engineering_work_state"])
    except (TypeError, ValueError) as exc:
        raise CheckpointStateError("INVALID_STATE", "checkpoint contains an invalid enum/state") from exc

    workflow_version = decoded["workflow_version"]
    if isinstance(workflow_version, bool) or not isinstance(workflow_version, int) or workflow_version < 0:
        raise CheckpointStateError("INVALID_WORKFLOW_VERSION", "checkpoint workflow_version must be non-negative")
    revision_id = decoded["revision_id"]
    if not isinstance(revision_id, str) or not revision_id:
        raise CheckpointStateError("INVALID_REVISION", "checkpoint revision_id must be a non-empty string")

    try:
        return AdvisoryEpisode(
            episode_id=episode_id,
            technical_state=technical_state,
            engineering_work_state=engineering_work_state,
            workflow_version=workflow_version,
            revision_id=revision_id,
        )
    except (TypeError, ValueError) as exc:
        raise CheckpointStateError("INCOHERENT_STATE", str(exc)) from exc


def deserialize_checkpoint(
    payload: str | bytes | bytearray | Mapping[str, Any],
    *,
    expected_episode_id: str | None = None,
) -> AdvisoryEpisode:
    """Public restore alias for callers that describe the operation as decoding."""

    return restore_checkpoint(payload, expected_episode_id=expected_episode_id)
