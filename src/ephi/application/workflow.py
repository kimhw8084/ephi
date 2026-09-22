"""The deliberately small durable Episode claim/acknowledge workflow."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .context import CommandContext, CurrentAuthorizationAuthority, RevisionVector
from .errors import AuthorizationDeniedError, InvalidTransitionError, ValidationFailureError
from .transactions import CommandResult, VersionedAggregateCommandExecutor


EPISODE_WORKFLOW_AGGREGATE_TYPE = "episode_workflow"
CLAIM_EPISODE_CAPABILITY = "ephi.episode.claim"
ACKNOWLEDGE_EPISODE_CAPABILITY = "ephi.episode.acknowledge"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decision_payload(context: CommandContext) -> dict[str, Any]:
    if context.viewed_revisions is None:
        raise ValidationFailureError("decision-sensitive Episode commands require viewed_revisions")
    if context.expected_workflow_version is None:
        raise ValidationFailureError("Episode workflow commands require expected_workflow_version")
    if context.viewed_revisions.workflow_version != context.expected_workflow_version:
        raise ValidationFailureError("viewed_revisions.workflow_version must equal expected_workflow_version")
    return {"viewed_revisions": context.viewed_revisions.as_dict()}


def _workflow_state(current: Mapping[str, Any]) -> dict[str, Any]:
    state = dict(current)
    work_state = state.get("work_state")
    if work_state not in {"OPEN", "CLAIMED", "ACKNOWLEDGED", "CLOSED"}:
        raise ValidationFailureError("Episode workflow state is not a supported W1 state")
    if "owner" not in state:
        raise ValidationFailureError("Episode workflow state has no owner field")
    return state


class EpisodeWorkflowCommandService:
    """Thin domain facade over the existing VersionedAggregateCommandExecutor."""

    def __init__(self, store, current_authorization: CurrentAuthorizationAuthority):
        self.executor = VersionedAggregateCommandExecutor(store, current_authorization)

    def claim_episode(
        self,
        context: CommandContext,
        episode_id: str,
        *,
        requested_owner: str | None = None,
        reason: str | None = None,
    ) -> CommandResult:
        if not isinstance(episode_id, str) or not episode_id.strip():
            raise ValidationFailureError("episode_id must be a non-empty string")
        requested_owner = requested_owner or context.principal.subject
        if requested_owner != context.principal.subject:
            raise AuthorizationDeniedError("W1 claim can only assign the server-resolved engineer")
        payload = {"requested_owner": requested_owner, **_decision_payload(context)}

        def effect(current: Mapping[str, Any], _payload: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _workflow_state(current)
            if state["work_state"] != "OPEN" or state.get("owner") not in (None, ""):
                raise InvalidTransitionError("Episode is already claimed; refresh before claiming")
            state.update({
                "owner": context.principal.subject,
                "work_state": "CLAIMED",
                "claimed_at": _timestamp(),
                "claimed_by": context.principal.subject,
            })
            return state

        return self.executor.execute(
            context,
            command_type="ClaimEpisode",
            aggregate_type=EPISODE_WORKFLOW_AGGREGATE_TYPE,
            aggregate_id=episode_id,
            payload=payload,
            required_capability=CLAIM_EPISODE_CAPABILITY,
            effect=effect,
        )

    def acknowledge_episode(self, context: CommandContext, episode_id: str) -> CommandResult:
        if not isinstance(episode_id, str) or not episode_id.strip():
            raise ValidationFailureError("episode_id must be a non-empty string")
        payload = _decision_payload(context)

        def effect(current: Mapping[str, Any], _payload: Mapping[str, Any]) -> Mapping[str, Any]:
            state = _workflow_state(current)
            if state["work_state"] != "CLAIMED" or state.get("owner") != context.principal.subject:
                raise InvalidTransitionError("Episode must be claimed by this engineer before acknowledgement")
            state.update({
                "work_state": "ACKNOWLEDGED",
                "acknowledged_at": _timestamp(),
                "acknowledged_by": context.principal.subject,
            })
            return state

        return self.executor.execute(
            context,
            command_type="AcknowledgeEpisode",
            aggregate_type=EPISODE_WORKFLOW_AGGREGATE_TYPE,
            aggregate_id=episode_id,
            payload=payload,
            required_capability=ACKNOWLEDGE_EPISODE_CAPABILITY,
            effect=effect,
        )


ClaimEpisode = EpisodeWorkflowCommandService.claim_episode
AcknowledgeEpisode = EpisodeWorkflowCommandService.acknowledge_episode
