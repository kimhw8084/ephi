"""Renderer-independent EPHI application contracts and command execution."""

from .context import AccessScope, CommandContext, Principal, RevisionVector
from .errors import (
    AggregateNotFoundError,
    AuthorizationError,
    AuthorizationDeniedError,
    CommandError,
    DurableStorageError,
    IdempotencyConflictError,
    ScopeDeniedError,
    StorageFailureError,
    ValidationError,
    ValidationFailureError,
    VersionConflictError,
)
from .hashing import (
    canonical_command_payload_hash,
    canonical_json,
    normalize_domain_payload,
)
from .transactions import CommandResult, VersionedAggregateCommandExecutor

__all__ = [
    "AccessScope",
    "AggregateNotFoundError",
    "AuthorizationError",
    "AuthorizationDeniedError",
    "CommandContext",
    "CommandError",
    "CommandResult",
    "DurableStorageError",
    "IdempotencyConflictError",
    "Principal",
    "RevisionVector",
    "ScopeDeniedError",
    "StorageFailureError",
    "ValidationError",
    "ValidationFailureError",
    "VersionConflictError",
    "VersionedAggregateCommandExecutor",
    "canonical_command_payload_hash",
    "canonical_json",
    "normalize_domain_payload",
]
