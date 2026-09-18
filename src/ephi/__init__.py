"""Canonical repository-native EPHI application package."""

from .app import application_identity, self_check
from .advisory import (
    AdvisoryEpisode,
    AdvisoryService,
    AttentionProjection,
    EngineeringWorkState,
    TechnicalEpisodeState,
    WorkflowVersionConflict,
)
from .config import RuntimeSettings
from .identity import ApplicationIdentity

__version__ = ApplicationIdentity.version

__all__ = [
    "ApplicationIdentity",
    "AdvisoryEpisode",
    "AdvisoryService",
    "AttentionProjection",
    "EngineeringWorkState",
    "RuntimeSettings",
    "TechnicalEpisodeState",
    "WorkflowVersionConflict",
    "application_identity",
    "self_check",
]
