"""Typed, safe application command errors."""

from __future__ import annotations

from collections.abc import Mapping


class CommandError(Exception):
    """Base error whose code is safe for an API/UI/CLI adapter to expose."""

    code = "COMMAND_FAILED"

    def __init__(self, message: str, *, details: Mapping[str, object] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


class AuthorizationDeniedError(CommandError):
    code = "FORBIDDEN_ACTION"


class ScopeDeniedError(CommandError):
    code = "FORBIDDEN_SCOPE"


class ValidationFailureError(CommandError):
    code = "VALIDATION_FAILED"


class VersionConflictError(CommandError):
    code = "VERSION_CONFLICT"

    def __init__(self, aggregate_id: str, expected: int, actual: int):
        super().__init__(
            "aggregate version is stale; refresh and retry with a new command",
            details={
                "aggregate_id": aggregate_id,
                "expected_workflow_version": expected,
                "current_workflow_version": actual,
            },
        )
        self.aggregate_id = aggregate_id
        self.expected_workflow_version = expected
        self.current_workflow_version = actual


class IdempotencyConflictError(CommandError):
    code = "IDEMPOTENCY_CONFLICT"

    def __init__(self, command_id: str):
        super().__init__(
            "command_id was already committed with a different semantic payload",
            details={"command_id": command_id},
        )
        self.command_id = command_id


class AggregateNotFoundError(CommandError):
    code = "NOT_FOUND"


class StorageFailureError(CommandError):
    code = "RETRYABLE_STORAGE_FAILURE"


# A concise compatibility name for callers that describe the persistence
# boundary as durable storage rather than a retryable infrastructure failure.
DurableStorageError = StorageFailureError
AuthorizationError = AuthorizationDeniedError
ValidationError = ValidationFailureError
