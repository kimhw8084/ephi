"""Small in-memory advisory workflow service for the W0 integrity slice."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from .checkpoint import restore_checkpoint, serialize_checkpoint
from .model import (
    AdvisoryEpisode,
    AttentionProjection,
    EngineeringWorkState,
    TechnicalEpisodeState,
    revision_identity,
)


class EpisodeNotFound(KeyError):
    """The requested canonical episode does not exist in the repository."""


class InvalidWorkflowVersion(ValueError):
    """A mutation supplied an invalid workflow version."""


class WorkflowVersionConflict(RuntimeError):
    """A compare-and-set mutation was based on a stale workflow version."""

    code = "WORKFLOW_VERSION_CONFLICT"

    def __init__(self, episode_id: str, expected: int, actual: int):
        super().__init__(
            f"workflow version conflict for {episode_id}: expected {expected}, current {actual}"
        )
        self.episode_id = episode_id
        self.expected_workflow_version = expected
        self.current_workflow_version = actual


class InMemoryAdvisoryRepository:
    """Bounded repository used by the canonical offline workflow slice."""

    def __init__(self, episodes: Iterable[AdvisoryEpisode] = ()):
        self._episodes: dict[str, AdvisoryEpisode] = {}
        for episode in episodes:
            self.add(episode)

    def add(self, episode: AdvisoryEpisode) -> AdvisoryEpisode:
        if not isinstance(episode, AdvisoryEpisode):
            raise TypeError("repository entries must be AdvisoryEpisode")
        if episode.episode_id in self._episodes:
            raise ValueError(f"episode already exists: {episode.episode_id}")
        self._episodes[episode.episode_id] = episode
        return episode

    def create_episode(
        self,
        episode_id: str,
        *,
        technical_state: TechnicalEpisodeState = TechnicalEpisodeState.ACTIVE,
        engineering_work_state: EngineeringWorkState = EngineeringWorkState.OPEN,
    ) -> AdvisoryEpisode:
        return self.add(
            AdvisoryEpisode.create(
                episode_id,
                technical_state=technical_state,
                engineering_work_state=engineering_work_state,
            )
        )

    def get(self, episode_id: str) -> AdvisoryEpisode:
        try:
            return self._episodes[episode_id]
        except KeyError as exc:
            raise EpisodeNotFound(episode_id) from exc

    def replace(
        self,
        episode: AdvisoryEpisode,
        *,
        expected_workflow_version: int,
    ) -> AdvisoryEpisode:
        if isinstance(expected_workflow_version, bool) or not isinstance(expected_workflow_version, int):
            raise InvalidWorkflowVersion("expected_workflow_version must be a non-negative integer")
        if expected_workflow_version < 0:
            raise InvalidWorkflowVersion("expected_workflow_version must be a non-negative integer")
        current = self.get(episode.episode_id)
        if current.workflow_version != expected_workflow_version:
            raise WorkflowVersionConflict(
                episode.episode_id,
                expected_workflow_version,
                current.workflow_version,
            )
        self._episodes[episode.episode_id] = episode
        return episode

    def all(self) -> tuple[AdvisoryEpisode, ...]:
        return tuple(self._episodes.values())

    def save_checkpoint(self, episode_id: str) -> str:
        return serialize_checkpoint(self.get(episode_id))

    def restore_checkpoint(
        self,
        payload: str | bytes | bytearray | Mapping[str, object],
        *,
        expected_episode_id: str | None = None,
    ) -> AdvisoryEpisode:
        episode = restore_checkpoint(payload, expected_episode_id=expected_episode_id)
        self._episodes[episode.episode_id] = episode
        return episode


class AdvisoryService:
    """Application boundary for independent technical and engineering authorities."""

    def __init__(self, repository: InMemoryAdvisoryRepository | None = None):
        self.repository = repository or InMemoryAdvisoryRepository()

    def create_episode(
        self,
        episode_id: str,
        *,
        technical_state: TechnicalEpisodeState = TechnicalEpisodeState.ACTIVE,
        engineering_work_state: EngineeringWorkState = EngineeringWorkState.OPEN,
    ) -> AdvisoryEpisode:
        return self.repository.create_episode(
            episode_id,
            technical_state=technical_state,
            engineering_work_state=engineering_work_state,
        )

    def get_episode(self, episode_id: str) -> AdvisoryEpisode:
        return self.repository.get(episode_id)

    def _mutate(
        self,
        episode_id: str,
        expected_workflow_version: int,
        change: Callable[[AdvisoryEpisode], tuple[TechnicalEpisodeState, EngineeringWorkState]],
    ) -> AdvisoryEpisode:
        if isinstance(expected_workflow_version, bool) or not isinstance(expected_workflow_version, int):
            raise InvalidWorkflowVersion("expected_workflow_version must be a non-negative integer")
        if expected_workflow_version < 0:
            raise InvalidWorkflowVersion("expected_workflow_version must be a non-negative integer")
        current = self.repository.get(episode_id)
        if current.workflow_version != expected_workflow_version:
            raise WorkflowVersionConflict(episode_id, expected_workflow_version, current.workflow_version)
        technical_state, engineering_work_state = change(current)
        next_version = current.workflow_version + 1
        return self.repository.replace(
            AdvisoryEpisode(
                episode_id=current.episode_id,
                technical_state=technical_state,
                engineering_work_state=engineering_work_state,
                workflow_version=next_version,
                revision_id=revision_identity(current.episode_id, next_version),
            ),
            expected_workflow_version=current.workflow_version,
        )

    def transition_technical_state(
        self,
        episode_id: str,
        technical_state: TechnicalEpisodeState,
        *,
        expected_workflow_version: int,
    ) -> AdvisoryEpisode:
        if not isinstance(technical_state, TechnicalEpisodeState):
            raise TypeError("technical_state must be TechnicalEpisodeState")
        return self._mutate(
            episode_id,
            expected_workflow_version,
            lambda current: (technical_state, current.engineering_work_state),
        )

    def transition_engineering_work_state(
        self,
        episode_id: str,
        engineering_work_state: EngineeringWorkState,
        *,
        expected_workflow_version: int,
    ) -> AdvisoryEpisode:
        if not isinstance(engineering_work_state, EngineeringWorkState):
            raise TypeError("engineering_work_state must be EngineeringWorkState")
        return self._mutate(
            episode_id,
            expected_workflow_version,
            lambda current: (current.technical_state, engineering_work_state),
        )

    def dispose_engineering_work(
        self,
        episode_id: str,
        *,
        expected_workflow_version: int,
        disposition: EngineeringWorkState = EngineeringWorkState.RESOLVED,
    ) -> AdvisoryEpisode:
        if not disposition.is_terminal:
            raise ValueError("engineering disposition must be terminal")
        return self.transition_engineering_work_state(
            episode_id,
            disposition,
            expected_workflow_version=expected_workflow_version,
        )

    def attention(self) -> list[AttentionProjection]:
        """Build open engineering work without consulting technical active/resolved state."""

        return [
            AttentionProjection(
                episode_id=episode.episode_id,
                technical_state=episode.technical_state,
                engineering_work_state=episode.engineering_work_state,
                workflow_version=episode.workflow_version,
                revision_id=episode.revision_id,
            )
            for episode in sorted(self.repository.all(), key=lambda item: item.episode_id)
            if episode.engineering_work_open
        ]

    def query_attention(self) -> list[AttentionProjection]:
        """Named query alias for callers using the application query vocabulary."""

        return self.attention()

    def save_checkpoint(self, episode_id: str) -> str:
        return self.repository.save_checkpoint(episode_id)

    def restore_checkpoint(
        self,
        payload: str | bytes | bytearray | Mapping[str, object],
        *,
        expected_episode_id: str | None = None,
    ) -> AdvisoryEpisode:
        return self.repository.restore_checkpoint(payload, expected_episode_id=expected_episode_id)
