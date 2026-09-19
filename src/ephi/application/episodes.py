"""Coherent O3 Episode decision-brief reads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .context import AccessScope, Principal, RevisionVector
from .errors import CoherentReadConflictError, ValidationFailureError
from .read import CurrentReadBundle, HistoricalReadBundle, ReadSnapshotStore


EPISODE_READ_CAPABILITY = "ephi.episode.read"
EPISODE_ENTITY_TYPE = "episode"
EPISODE_WORKFLOW_AGGREGATE_TYPE = "episode_workflow"


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    return value


def _state_object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CoherentReadConflictError(f"{field} is unavailable or not an object")
    return dict(value)


@dataclass(frozen=True, slots=True)
class EpisodeBrief:
    """One coherent analytical revision plus workflow and source state."""

    episode_id: str
    revision_id: str
    known_at: object
    published_at: object
    analytical: dict[str, Any]
    workflow: dict[str, Any]
    revision_vector: RevisionVector
    capability_state: dict[str, Any]
    historical: bool = False

    def __post_init__(self) -> None:
        _identity(self.episode_id, "episode_id")
        _identity(self.revision_id, "revision_id")
        if not isinstance(self.analytical, dict) or not isinstance(self.workflow, dict):
            raise ValidationFailureError("Episode brief payloads must be objects")
        if not isinstance(self.revision_vector, RevisionVector):
            raise ValidationFailureError("revision_vector must be a RevisionVector")
        if not isinstance(self.capability_state, dict):
            raise ValidationFailureError("capability_state must be an object")

    @property
    def source_state(self) -> dict[str, Any]:
        return self.capability_state

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "revision_id": self.revision_id,
            "known_at": self.known_at,
            "published_at": self.published_at,
            "analytical": self.analytical,
            "workflow": self.workflow,
            "revision_vector": self.revision_vector.as_dict(),
            "capability_state": self.capability_state,
            "historical": self.historical,
        }


def _bundle_brief(bundle: CurrentReadBundle | HistoricalReadBundle, *, historical: bool) -> EpisodeBrief:
    revision = bundle.read_revision
    workflow = bundle.workflow_aggregate
    if revision.entity_type != EPISODE_ENTITY_TYPE:
        raise CoherentReadConflictError("read revision is not an Episode revision")
    if revision.entity_id != workflow.aggregate_id or workflow.aggregate_type != EPISODE_WORKFLOW_AGGREGATE_TYPE:
        raise CoherentReadConflictError("Episode analytical revision and workflow aggregate identity disagree")
    if revision.revision_vector.workflow_version != revision.workflow_aggregate.version:
        raise CoherentReadConflictError("immutable Episode revision has an incoherent workflow version")
    if workflow.scope_key != revision.scope.canonical_key:
        raise CoherentReadConflictError("Episode workflow scope disagrees with the read revision")
    payload = dict(revision.payload)
    capability_state = payload.get("capability_state", payload.get("source_capability_state"))
    capability_state = _state_object(capability_state, "Episode capability/source state")
    analytical_revision = payload.get("analytical_revision")
    if analytical_revision is not None and analytical_revision != revision.revision_vector.analysis_revision:
        raise CoherentReadConflictError("Episode payload analytical revision disagrees with its revision vector")
    return EpisodeBrief(
        revision.entity_id,
        revision.revision_id,
        revision.known_at,
        revision.published_at,
        payload,
        dict(workflow.state),
        bundle.revision_vector,
        capability_state,
        historical,
    )


class EpisodeBriefQueryService:
    """Use the existing coherent current/historical read contracts."""

    def __init__(self, read_store: ReadSnapshotStore):
        if not isinstance(read_store, ReadSnapshotStore):
            raise TypeError("read_store must implement the durable coherent-read boundary")
        self.read_store = read_store

    def get_episode_brief(
        self,
        principal: Principal,
        scope: AccessScope,
        episode_id: str,
        *,
        revision_id: str | None = None,
    ) -> EpisodeBrief:
        episode_id = _identity(episode_id, "episode_id")
        if revision_id is None:
            bundle = self.read_store.read_current_bundle(
                principal,
                scope,
                EPISODE_ENTITY_TYPE,
                episode_id,
                EPISODE_READ_CAPABILITY,
            )
            return _bundle_brief(bundle, historical=False)
        revision_id = _identity(revision_id, "revision_id")
        bundle = self.read_store.read_historical_bundle(principal, scope, revision_id, EPISODE_READ_CAPABILITY)
        if bundle.read_revision.entity_id != episode_id:
            raise CoherentReadConflictError("requested Episode does not match the historical revision")
        return _bundle_brief(bundle, historical=True)


GetEpisodeBrief = EpisodeBriefQueryService

