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
from .recovery import (
    CriterionResult,
    IntegrityAttribution,
    ObservationOutcome,
    RecoveryAssessment,
    RecoveryEpisode,
    RecoveryEvaluator,
    RecoveryEligibility,
    RecoveryObservation,
    RecoveryPolicy,
    RecoveryReasonCode,
    RecoveryService,
    RecoveryState,
    RecoveryValidationError,
    Severity,
)
from .value import (
    EventPeriod,
    InMemoryValueRepository,
    MixedCurrencyError,
    SupersessionConflictError,
    SupersessionError,
    UnknownPredecessorError,
    ValueEntry,
    ValueService,
    ValueValidationError,
)

__version__ = ApplicationIdentity.version

__all__ = [
    "ApplicationIdentity",
    "AdvisoryEpisode",
    "AdvisoryService",
    "AttentionProjection",
    "CriterionResult",
    "EngineeringWorkState",
    "EventPeriod",
    "IntegrityAttribution",
    "InMemoryValueRepository",
    "MixedCurrencyError",
    "ObservationOutcome",
    "RecoveryAssessment",
    "RecoveryEpisode",
    "RecoveryEvaluator",
    "RecoveryEligibility",
    "RecoveryObservation",
    "RecoveryPolicy",
    "RecoveryReasonCode",
    "RecoveryService",
    "RecoveryState",
    "RecoveryValidationError",
    "RuntimeSettings",
    "Severity",
    "TechnicalEpisodeState",
    "SupersessionConflictError",
    "SupersessionError",
    "UnknownPredecessorError",
    "ValueEntry",
    "ValueService",
    "ValueValidationError",
    "WorkflowVersionConflict",
    "application_identity",
    "self_check",
]
