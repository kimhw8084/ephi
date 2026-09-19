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


class JobSemanticConflictError(CommandError):
    """A semantic enqueue key is already bound to another payload/type."""

    code = "JOB_SEMANTIC_CONFLICT"

    def __init__(self, scope_key: str, semantic_key: str):
        super().__init__(
            "semantic job key was already committed with a different payload",
            details={"scope_key": scope_key, "semantic_key": semantic_key},
        )
        self.scope_key = scope_key
        self.semantic_key = semantic_key


class NoEligibleJobError(CommandError):
    code = "NO_ELIGIBLE_JOB"


class JobNotFoundError(CommandError):
    code = "JOB_NOT_FOUND"


class StaleLeaseError(CommandError):
    """The supplied owner/epoch no longer has a current database lease."""

    code = "STALE_LEASE"

    def __init__(self, job_id: str):
        super().__init__(
            "job lease is stale, expired, or fenced by a newer worker",
            details={"job_id": job_id},
        )
        self.job_id = job_id


class EffectIdempotencyConflictError(CommandError):
    code = "EFFECT_IDEMPOTENCY_CONFLICT"

    def __init__(self, job_id: str, effect_key: str):
        super().__init__(
            "local effect key was already committed with a different input",
            details={"job_id": job_id, "effect_key": effect_key},
        )
        self.job_id = job_id
        self.effect_key = effect_key


class InvalidTransitionError(CommandError):
    code = "INVALID_TRANSITION"


class ReadError(CommandError):
    """Base class for typed, fail-closed read/snapshot errors."""


class ReadRevisionConflictError(ReadError):
    """A revision identity or current-head CAS precondition was already won."""

    code = "READ_REVISION_CONFLICT"


class CoherentReadConflictError(ReadError):
    """The selected read revision and workflow aggregate are not one pair."""

    code = "COHERENT_READ_CONFLICT"


class ReadRevisionNotFoundError(ReadError):
    code = "READ_REVISION_NOT_FOUND"


class QuerySnapshotExpiredError(ReadError):
    """Retained state is unavailable or no longer valid; restart the query."""

    code = "QUERY_SNAPSHOT_EXPIRED"

    def __init__(self, message: str = "query snapshot expired or is unavailable; restart the query", *, reason: str | None = None):
        details = {"restart_query": True}
        if reason is not None:
            details["reason"] = reason
        super().__init__(message, details=details)


class QueryTooBroadError(ReadError):
    """A retained result would exceed the bounded reference implementation."""

    code = "QUERY_TOO_BROAD"

    def __init__(self, message: str = "query result exceeds the bounded retained snapshot limit", *, limit: int | None = None):
        details = {}
        if limit is not None:
            details["max_retained_rows"] = limit
        super().__init__(message, details=details)


class QueryIdentityMismatchError(ReadError):
    """A cursor, snapshot, or requested query identity does not match."""

    code = "QUERY_IDENTITY_MISMATCH"


class QueryCursorValidationError(QueryIdentityMismatchError):
    """A cursor is malformed, tampered with, or has an invalid position."""

    code = "QUERY_CURSOR_INVALID"


class ArtifactError(CommandError):
    """Base error for the generic immutable-artifact boundary."""


class ArtifactIntegrityError(ArtifactError):
    """Stored bytes do not prove the content identity recorded by the caller."""

    code = "ARTIFACT_INTEGRITY_ERROR"


class ArtifactMetadataConflictError(ArtifactError):
    """A scoped content identity is already registered with other metadata."""

    code = "ARTIFACT_METADATA_CONFLICT"


class ArtifactNotFoundError(ArtifactError):
    """The requested immutable blob or scoped catalog record is unavailable."""

    code = "ARTIFACT_NOT_FOUND"


class ArtifactStorageConfigurationError(ArtifactError):
    """The reference filesystem adapter has an unsafe or incomplete setup."""

    code = "ARTIFACT_STORAGE_CONFIGURATION_ERROR"


class ArtifactStorageSafetyError(ArtifactError):
    """The reference adapter cannot establish a safe filesystem boundary."""

    code = "ARTIFACT_STORAGE_SAFETY_ERROR"


class ArtifactTooLargeError(ArtifactError):
    """Reference filesystem storage rejected content above its explicit bound."""

    code = "ARTIFACT_TOO_LARGE"


class ArtifactWriteInterruptedError(ArtifactError):
    """A deterministic test fault interrupted a write before publication."""

    code = "ARTIFACT_WRITE_INTERRUPTED"


# A concise compatibility name for callers that describe the persistence
# boundary as durable storage rather than a retryable infrastructure failure.
DurableStorageError = StorageFailureError
AuthorizationError = AuthorizationDeniedError
ValidationError = ValidationFailureError
