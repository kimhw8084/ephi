"""Storage-neutral contracts for immutable, scoped artifact identity.

Artifacts are identified by the SHA-256 of their exact bytes and the exact
byte size.  Scope and metadata are catalog facts; they never change the
content identity and a content hash is never an authorization credential.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
import unicodedata
from typing import Protocol, runtime_checkable

from .context import AccessScope, CurrentAuthorizationAuthority, Principal
from .errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStorageConfigurationError,
    ValidationFailureError,
)


ARTIFACT_METADATA_MAX_BYTES = 128
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_bounded_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationFailureError(f"{field} must be a non-empty canonical string")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or "\x00" in normalized or any(ord(character) < 0x20 or 0x7F <= ord(character) < 0xA0 for character in normalized):
        raise ValidationFailureError(f"{field} contains unsupported control characters")
    if len(normalized.encode("utf-8")) > ARTIFACT_METADATA_MAX_BYTES:
        raise ValidationFailureError(
            f"{field} exceeds the {ARTIFACT_METADATA_MAX_BYTES}-byte reference bound"
        )
    return normalized


def _optional_bounded_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _canonical_bounded_string(value, field)


def _validate_required_capability(value: object) -> str:
    return _canonical_bounded_string(value, "required_capability")


@dataclass(frozen=True, slots=True)
class ArtifactContentIdentity:
    """Canonical identity of exact artifact content."""

    sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or _SHA256_RE.fullmatch(self.sha256) is None:
            raise ValidationFailureError("sha256 must be canonical lowercase 64-hex")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int) or self.byte_size < 0:
            raise ValidationFailureError("byte_size must be a non-negative integer")

    @classmethod
    def from_bytes(cls, content: bytes) -> "ArtifactContentIdentity":
        if not isinstance(content, bytes):
            raise ValidationFailureError("artifact content must be exact bytes")
        return cls(hashlib.sha256(content).hexdigest(), len(content))

    @property
    def digest(self) -> str:
        """Compatibility name for callers that call the SHA-256 a digest."""

        return self.sha256


@dataclass(frozen=True, slots=True)
class ScopedArtifactReference:
    """Stable scoped identity; it intentionally contains no storage path."""

    scope: AccessScope
    content: ArtifactContentIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        if not isinstance(self.content, ArtifactContentIdentity):
            raise ValidationFailureError("content must be an ArtifactContentIdentity")

    @property
    def scope_key(self) -> str:
        return self.scope.canonical_key

    @property
    def sha256(self) -> str:
        return self.content.sha256

    @property
    def byte_size(self) -> int:
        return self.content.byte_size


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """Immutable scoped catalog metadata returned after verified storage."""

    reference: ScopedArtifactReference
    media_type: str
    logical_purpose: str
    producing_job_id: str | None = None
    revision_id: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reference, ScopedArtifactReference):
            raise ValidationFailureError("reference must be a ScopedArtifactReference")
        object.__setattr__(self, "media_type", _canonical_bounded_string(self.media_type, "media_type").lower())
        object.__setattr__(self, "logical_purpose", _canonical_bounded_string(self.logical_purpose, "logical_purpose"))
        object.__setattr__(self, "producing_job_id", _optional_bounded_string(self.producing_job_id, "producing_job_id"))
        object.__setattr__(self, "revision_id", _optional_bounded_string(self.revision_id, "revision_id"))
        if self.created_at is not None and (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise ValidationFailureError("created_at must be timezone-aware or None")

    @property
    def content(self) -> ArtifactContentIdentity:
        return self.reference.content

    def immutable_metadata_key(self) -> tuple[object, ...]:
        """The caller-controlled fields used for idempotency comparison."""

        return (
            self.reference.scope_key,
            self.content.sha256,
            self.content.byte_size,
            self.media_type,
            self.logical_purpose,
            self.producing_job_id,
            self.revision_id,
        )

    def as_dict(self) -> dict[str, object]:
        """Public DTO form; no absolute or internal filesystem path is exposed."""

        return {
            "scope_key": self.reference.scope_key,
            "sha256": self.content.sha256,
            "byte_size": self.content.byte_size,
            "media_type": self.media_type,
            "logical_purpose": self.logical_purpose,
            "producing_job_id": self.producing_job_id,
            "revision_id": self.revision_id,
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ArtifactBlobWriteResult:
    """Result of publishing or reusing a content-addressed blob."""

    content: ArtifactContentIdentity
    reused: bool


@dataclass(frozen=True, slots=True)
class ArtifactCatalogRegistration:
    """Result of an idempotent scoped catalog registration."""

    metadata: ArtifactMetadata
    created: bool


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    """Verified write plus scoped catalog registration result."""

    metadata: ArtifactMetadata
    blob_reused: bool
    catalog_created: bool


@dataclass(frozen=True, slots=True)
class VerifiedArtifactRead:
    """Bytes and metadata returned only after a verified, authorized read."""

    metadata: ArtifactMetadata
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, ArtifactMetadata) or not isinstance(self.content, bytes):
            raise ValidationFailureError("verified artifact reads require metadata and exact bytes")
        actual = ArtifactContentIdentity.from_bytes(self.content)
        if actual != self.metadata.content:
            raise ArtifactIntegrityError("returned artifact bytes do not match their registered identity")


@dataclass(frozen=True, slots=True)
class PublishPreconditionResult:
    """All scoped references proved present and byte-integrity-valid."""

    references: tuple[ScopedArtifactReference, ...]
    metadata: tuple[ArtifactMetadata, ...]


@dataclass(frozen=True, slots=True)
class ArtifactScopeVerification:
    """Bounded integrity summary for catalog references in one authorized scope."""

    inspected_count: int
    missing_count: int
    corrupt_count: int
    other_unavailable_count: int
    truncated: bool
    safe_reasons: tuple[tuple[str, int], ...]


@runtime_checkable
class ArtifactBlobStore(Protocol):
    """Immutable content store addressed only by content identity."""

    def put_bytes(self, content: bytes) -> ArtifactBlobWriteResult: ...

    def verify(self, identity: ArtifactContentIdentity) -> None: ...

    def read(self, identity: ArtifactContentIdentity) -> bytes: ...


@runtime_checkable
class ArtifactCatalog(Protocol):
    """Scoped metadata catalog separate from physical blob storage."""

    def register(self, metadata: ArtifactMetadata, *, object_key: str) -> ArtifactCatalogRegistration: ...

    def get(self, reference: ScopedArtifactReference) -> ArtifactMetadata | None: ...


@runtime_checkable
class ArtifactServicePort(Protocol):
    """Authorization-aware artifact service used by future publishers/readers."""

    def retrieve(
        self,
        principal: Principal,
        reference: ScopedArtifactReference,
        required_read_capability: str,
    ) -> VerifiedArtifactRead: ...

    def verify_publish_preconditions(
        self,
        principal: Principal,
        references: Sequence[ScopedArtifactReference],
        required_read_capability: str,
    ) -> PublishPreconditionResult: ...


def internal_artifact_object_key(identity: ArtifactContentIdentity) -> str:
    """Derive the private catalog key from the validated SHA only."""

    if not isinstance(identity, ArtifactContentIdentity):
        raise ValidationFailureError("identity must be an ArtifactContentIdentity")
    return f"sha256/{identity.sha256}"


class ArtifactService:
    """Coordinate current authorization, immutable bytes and scoped metadata."""

    def __init__(
        self,
        blob_store: ArtifactBlobStore,
        catalog: ArtifactCatalog,
        current_authorization: CurrentAuthorizationAuthority,
    ):
        if not isinstance(blob_store, ArtifactBlobStore) or not isinstance(catalog, ArtifactCatalog):
            raise ArtifactStorageConfigurationError("artifact service requires separate blob-store and catalog ports")
        if not isinstance(current_authorization, CurrentAuthorizationAuthority):
            raise ArtifactStorageConfigurationError(
                "artifact service requires a current authorization authority"
            )
        self.blob_store = blob_store
        self.catalog = catalog
        self.current_authorization = current_authorization

    def _authorize(self, principal: Principal, scope: AccessScope, capability: str) -> None:
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        capability = _validate_required_capability(capability)
        self.current_authorization.authorize(principal, scope, capability)

    def _register_verified(self, metadata: ArtifactMetadata) -> ArtifactCatalogRegistration:
        self.blob_store.verify(metadata.content)
        return self.catalog.register(
            metadata,
            object_key=internal_artifact_object_key(metadata.content),
        )

    def register_existing(
        self,
        principal: Principal,
        metadata: ArtifactMetadata,
        *,
        required_write_capability: str,
    ) -> ArtifactWriteResult:
        if not isinstance(metadata, ArtifactMetadata):
            raise ValidationFailureError("metadata must be an ArtifactMetadata")
        self._authorize(principal, metadata.reference.scope, required_write_capability)
        registration = self._register_verified(metadata)
        return ArtifactWriteResult(registration.metadata, True, registration.created)

    def write_and_register(
        self,
        principal: Principal,
        scope: AccessScope,
        content: bytes,
        *,
        media_type: str,
        logical_purpose: str,
        required_write_capability: str,
        producing_job_id: str | None = None,
        revision_id: str | None = None,
    ) -> ArtifactWriteResult:
        self._authorize(principal, scope, required_write_capability)
        if not isinstance(content, bytes):
            raise ValidationFailureError("artifact content must be exact bytes")
        expected_content = ArtifactContentIdentity.from_bytes(content)
        metadata = ArtifactMetadata(
            ScopedArtifactReference(scope, expected_content),
            media_type,
            logical_purpose,
            producing_job_id,
            revision_id,
        )
        blob = self.blob_store.put_bytes(content)
        if blob.content != expected_content:
            raise ArtifactIntegrityError("blob store returned a content identity different from exact input bytes")
        registration = self._register_verified(metadata)
        return ArtifactWriteResult(registration.metadata, blob.reused, registration.created)

    # Short aliases keep the service usable by future application publishers
    # without adding another parallel artifact API.
    write = write_and_register
    register = register_existing

    def get_metadata(
        self,
        principal: Principal,
        reference: ScopedArtifactReference,
        required_read_capability: str,
    ) -> ArtifactMetadata:
        if not isinstance(reference, ScopedArtifactReference):
            raise ValidationFailureError("reference must be a ScopedArtifactReference")
        self._authorize(principal, reference.scope, required_read_capability)
        metadata = self.catalog.get(reference)
        if metadata is None:
            raise ArtifactNotFoundError("artifact is not registered in the requested scope")
        self.blob_store.verify(reference.content)
        if metadata.reference != reference:
            raise ArtifactIntegrityError("catalog metadata does not match the requested artifact identity")
        return metadata

    def retrieve(
        self,
        principal: Principal,
        reference: ScopedArtifactReference,
        required_read_capability: str,
    ) -> VerifiedArtifactRead:
        metadata = self.get_metadata(principal, reference, required_read_capability)
        content = self.blob_store.read(reference.content)
        return VerifiedArtifactRead(metadata, content)

    def inspect_scope(
        self,
        principal: Principal,
        scope: AccessScope,
        required_read_capability: str,
        *,
        limit: int = 500,
    ) -> ArtifactScopeVerification:
        """Verify server-configured catalog references without returning bytes or keys."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationFailureError("artifact inspection limit must be between 1 and 500")
        self._authorize(principal, scope, required_read_capability)
        list_for_scope = getattr(self.catalog, "list_for_scope", None)
        if not callable(list_for_scope):
            raise ArtifactStorageConfigurationError("artifact catalog does not provide bounded scope inspection")
        # O8 authorization is complete before the catalog reveals whether any
        # artifact reference exists in this scope.
        rows = list_for_scope(scope, limit=limit + 1)
        truncated = len(rows) > limit
        inspected = rows[:limit]
        failures = {
            "MISSING_ARTIFACT_BYTES": 0,
            "CORRUPT_ARTIFACT_BYTES": 0,
            "ARTIFACT_VERIFICATION_UNAVAILABLE": 0,
        }
        for metadata in inspected:
            try:
                # Reuse the ordinary scoped metadata/blob verifier. This does
                # not call read() and never materializes private content.
                self.get_metadata(principal, metadata.reference, required_read_capability)
            except ArtifactNotFoundError:
                failures["MISSING_ARTIFACT_BYTES"] += 1
            except ArtifactIntegrityError:
                failures["CORRUPT_ARTIFACT_BYTES"] += 1
            except AuthorizationDeniedError:
                raise
            except Exception:
                failures["ARTIFACT_VERIFICATION_UNAVAILABLE"] += 1
        return ArtifactScopeVerification(
            len(inspected),
            failures["MISSING_ARTIFACT_BYTES"],
            failures["CORRUPT_ARTIFACT_BYTES"],
            failures["ARTIFACT_VERIFICATION_UNAVAILABLE"],
            truncated,
            tuple((key, value) for key, value in sorted(failures.items()) if value),
        )

    read = retrieve

    def verify_publish_preconditions(
        self,
        principal: Principal,
        references: Sequence[ScopedArtifactReference],
        required_read_capability: str,
    ) -> PublishPreconditionResult:
        if not isinstance(references, Sequence) or isinstance(references, (str, bytes, bytearray)):
            raise ValidationFailureError("artifact references must be a sequence")
        if not references:
            raise ValidationFailureError("publish preconditions require at least one artifact reference")
        verified_references: list[ScopedArtifactReference] = []
        verified_metadata: list[ArtifactMetadata] = []
        for reference in references:
            if not isinstance(reference, ScopedArtifactReference):
                raise ValidationFailureError("artifact references must contain ScopedArtifactReference values")
            verified_references.append(reference)
        # Preflight every requested scope/capability before any catalog or
        # blob existence check, including multi-reference publish paths.
        for reference in verified_references:
            self._authorize(principal, reference.scope, required_read_capability)
        for reference in verified_references:
            metadata = self.get_metadata(principal, reference, required_read_capability)
            # get_metadata has already performed current authorization and a
            # verified blob read; the second explicit check documents the
            # publish-before-head invariant at this boundary.
            self.blob_store.verify(reference.content)
            verified_metadata.append(metadata)
        return PublishPreconditionResult(tuple(verified_references), tuple(verified_metadata))

    verify_publish_precondition = verify_publish_preconditions


__all__ = [
    "ARTIFACT_METADATA_MAX_BYTES",
    "ArtifactBlobStore",
    "ArtifactBlobWriteResult",
    "ArtifactCatalog",
    "ArtifactCatalogRegistration",
    "ArtifactContentIdentity",
    "ArtifactMetadata",
    "ArtifactService",
    "ArtifactServicePort",
    "ArtifactScopeVerification",
    "ArtifactWriteResult",
    "PublishPreconditionResult",
    "ScopedArtifactReference",
    "VerifiedArtifactRead",
    "internal_artifact_object_key",
]
