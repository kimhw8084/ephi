"""Typed advisory state and the small read projection used by Attention."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class TechnicalEpisodeState(str, Enum):
    """Technical authority for the underlying episode."""

    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class EngineeringWorkState(str, Enum):
    """Independent authority for human engineering work."""

    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.RESOLVED, self.DISMISSED}

    @property
    def is_open(self) -> bool:
        return not self.is_terminal


def revision_identity(episode_id: str, workflow_version: int) -> str:
    """Return the deterministic revision identity for an aggregate version."""

    return f"{episode_id}:workflow:{workflow_version}"


@dataclass(frozen=True)
class AdvisoryEpisode:
    """The canonical aggregate with separate technical and work authorities."""

    episode_id: str
    technical_state: TechnicalEpisodeState
    engineering_work_state: EngineeringWorkState
    workflow_version: int
    revision_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.episode_id, str) or not self.episode_id.strip():
            raise ValueError("episode_id must be a non-empty string")
        if not isinstance(self.technical_state, TechnicalEpisodeState):
            raise TypeError("technical_state must be TechnicalEpisodeState")
        if not isinstance(self.engineering_work_state, EngineeringWorkState):
            raise TypeError("engineering_work_state must be EngineeringWorkState")
        if isinstance(self.workflow_version, bool) or not isinstance(self.workflow_version, int):
            raise ValueError("workflow_version must be a non-negative integer")
        if self.workflow_version < 0:
            raise ValueError("workflow_version must be a non-negative integer")
        if not isinstance(self.revision_id, str) or not self.revision_id:
            raise ValueError("revision_id must be a non-empty string")
        expected_revision = revision_identity(self.episode_id, self.workflow_version)
        if self.revision_id != expected_revision:
            raise ValueError("revision_id is incoherent with episode_id and workflow_version")

    @classmethod
    def create(
        cls,
        episode_id: str,
        *,
        technical_state: TechnicalEpisodeState = TechnicalEpisodeState.ACTIVE,
        engineering_work_state: EngineeringWorkState = EngineeringWorkState.OPEN,
    ) -> "AdvisoryEpisode":
        return cls(
            episode_id=episode_id,
            technical_state=technical_state,
            engineering_work_state=engineering_work_state,
            workflow_version=0,
            revision_id=revision_identity(episode_id, 0),
        )

    @property
    def engineering_work_open(self) -> bool:
        """Whether the engineering authority still has open work."""

        return self.engineering_work_state.is_open

    def as_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "technical_state": self.technical_state.value,
            "engineering_work_state": self.engineering_work_state.value,
            "workflow_version": self.workflow_version,
            "revision_id": self.revision_id,
        }


@dataclass(frozen=True)
class AttentionProjection:
    """Canonical open-work row; technical state is informative, not the filter."""

    episode_id: str
    technical_state: TechnicalEpisodeState
    engineering_work_state: EngineeringWorkState
    workflow_version: int
    revision_id: str
    visible_in_attention: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "technical_state": self.technical_state.value,
            "engineering_work_state": self.engineering_work_state.value,
        }
