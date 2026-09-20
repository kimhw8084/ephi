"""Typed server-derived command context contracts."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import json
from typing import Final

from .errors import AuthorizationDeniedError, ValidationFailureError


RevisionIdentity = int | str


def _required_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise TypeError(f"{field} must be a non-empty canonical string")
    return value


def _revision_identity(value: object, field: str) -> RevisionIdentity:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a non-negative integer or canonical string")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{field} must be non-negative")
        return value
    if isinstance(value, str):
        return _required_identity(value, field)
    raise TypeError(f"{field} must be a non-negative integer or canonical string")


def _version(value: object, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{field} must be a non-negative integer" + (" or None" if optional else ""))
    return value


@dataclass(frozen=True, slots=True)
class AccessScope:
    """Explicit product scope identity used by authorization and receipt keys."""

    scope_id: str
    site_id: str | None = None
    area_id: str | None = None
    family_id: str | None = None
    project_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", _required_identity(self.scope_id, "scope_id"))
        for field in ("site_id", "area_id", "family_id"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _required_identity(value, field))
        if not isinstance(self.project_ids, (tuple, list, frozenset)):
            raise TypeError("project_ids must be a tuple, list, or frozenset of strings")
        projects = []
        for project_id in self.project_ids:
            projects.append(_required_identity(project_id, "project_id"))
        object.__setattr__(self, "project_ids", tuple(sorted(set(projects))))

    def as_dict(self) -> dict[str, object]:
        return {
            "scope_id": self.scope_id,
            "site_id": self.site_id,
            "area_id": self.area_id,
            "family_id": self.family_id,
            "project_ids": list(self.project_ids),
        }

    @property
    def canonical_key(self) -> str:
        """Stable serialized identity suitable for composite durable keys."""

        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class Principal:
    """Server-derived subject, grants and authorization freshness revisions."""

    subject: str
    capabilities: frozenset[str] | tuple[str, ...]
    scope_grants: tuple[AccessScope, ...]
    auth_session_revision: RevisionIdentity
    security_revision: RevisionIdentity | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject", _required_identity(self.subject, "subject"))
        if not isinstance(self.capabilities, (frozenset, tuple, list)):
            raise TypeError("capabilities must be a frozenset, tuple, or list of strings")
        capabilities = []
        for capability in self.capabilities:
            capabilities.append(_required_identity(capability, "capability"))
        object.__setattr__(self, "capabilities", frozenset(capabilities))
        if not isinstance(self.scope_grants, (tuple, list)):
            raise TypeError("scope_grants must be a tuple or list of AccessScope values")
        if any(not isinstance(scope, AccessScope) for scope in self.scope_grants):
            raise TypeError("scope_grants must contain only AccessScope values")
        grants = sorted({scope.canonical_key: scope for scope in self.scope_grants}.values(), key=lambda item: item.canonical_key)
        object.__setattr__(self, "scope_grants", tuple(grants))
        object.__setattr__(self, "auth_session_revision", _revision_identity(self.auth_session_revision, "auth_session_revision"))
        security_revision = self.security_revision
        if security_revision is None:
            security_revision = self.auth_session_revision
        object.__setattr__(self, "security_revision", _revision_identity(security_revision, "security_revision"))

    @property
    def granted_capabilities(self) -> frozenset[str]:
        return self.capabilities

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def grants_scope(self, scope: AccessScope) -> bool:
        return isinstance(scope, AccessScope) and any(
            grant.canonical_key == scope.canonical_key for grant in self.scope_grants
        )


class CurrentAuthorizationAuthority:
    """Resolve and verify current authorization before protected disclosure.

    The presented ``Principal`` remains the operation identity.  A resolver
    only supplies the current server-side state for comparison; this class
    never upgrades the presented principal or returns the resolved one.
    """

    def __init__(self, resolver: Callable[[str], Principal]):
        if not callable(resolver):
            raise TypeError("current authorization resolver must be callable")
        self._resolver = resolver

    @classmethod
    def from_provider(cls, provider: Callable[[], Principal]) -> "CurrentAuthorizationAuthority":
        """Bind an operation-time provider without retaining its Principal."""

        if not callable(provider):
            raise TypeError("current authorization provider must be callable")
        return cls(lambda _subject: provider())

    def authorize(
        self,
        presented: Principal,
        scope: AccessScope,
        required_capability: str,
    ) -> None:
        """Fail closed unless the presented state is still current and allowed."""

        if not isinstance(presented, Principal):
            raise AuthorizationDeniedError("current authorization does not permit this operation")
        if not isinstance(scope, AccessScope):
            raise ValidationFailureError("scope must be an AccessScope")
        required_capability = _required_identity(required_capability, "required_capability")
        try:
            current = self._resolver(presented.subject)
        except Exception as exc:
            raise AuthorizationDeniedError("current authorization is unavailable") from exc
        if not isinstance(current, Principal):
            raise AuthorizationDeniedError("current authorization is unavailable")

        # Revision equality is the freshness boundary.  Checking the
        # presented grants too prevents this authority from silently
        # upgrading an operation with grants that were not presented.
        if (
            current.subject != presented.subject
            or current.auth_session_revision != presented.auth_session_revision
            or current.security_revision != presented.security_revision
            or not presented.grants_scope(scope)
            or not presented.has_capability(required_capability)
            or not current.grants_scope(scope)
            or not current.has_capability(required_capability)
        ):
            raise AuthorizationDeniedError("current authorization does not permit this operation")


class MutableCurrentAuthorizationAuthority(CurrentAuthorizationAuthority):
    """Explicit mutable authority for deterministic unit/integration tests."""

    def __init__(self, principal: Principal):
        if not isinstance(principal, Principal):
            raise TypeError("test current authorization requires a Principal")
        self._current = principal
        super().__init__(self._resolve)

    def _resolve(self, _subject: str) -> Principal:
        return self._current

    def set_principal(self, principal: Principal) -> None:
        if not isinstance(principal, Principal):
            raise TypeError("test current authorization requires a Principal")
        self._current = principal


@dataclass(frozen=True, slots=True)
class RevisionVector:
    """Opaque revision identities returned with decision-sensitive responses."""

    analysis_revision: str
    exposure_revision: str | None
    priority_revision: str | None
    workflow_version: int
    plan_version: int | None
    qualification_manifest_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "analysis_revision", _required_identity(self.analysis_revision, "analysis_revision"))
        for field in ("exposure_revision", "priority_revision", "qualification_manifest_id"):
            value = getattr(self, field)
            if value is None and field != "qualification_manifest_id":
                continue
            object.__setattr__(self, field, _required_identity(value, field))
        object.__setattr__(self, "workflow_version", _version(self.workflow_version, "workflow_version"))
        object.__setattr__(self, "plan_version", _version(self.plan_version, "plan_version", optional=True))

    def as_dict(self) -> dict[str, object]:
        return {
            "analysis_revision": self.analysis_revision,
            "exposure_revision": self.exposure_revision,
            "priority_revision": self.priority_revision,
            "workflow_version": self.workflow_version,
            "plan_version": self.plan_version,
            "qualification_manifest_id": self.qualification_manifest_id,
        }


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Server-resolved context carried by every canonical command."""

    command_id: str
    principal: Principal
    scope: AccessScope
    expected_workflow_version: int | None
    viewed_revisions: RevisionVector | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", _required_identity(self.command_id, "command_id"))
        if not isinstance(self.principal, Principal):
            raise TypeError("principal must be a server-derived Principal")
        if not isinstance(self.scope, AccessScope):
            raise TypeError("scope must be an AccessScope")
        object.__setattr__(
            self,
            "expected_workflow_version",
            _version(self.expected_workflow_version, "expected_workflow_version", optional=True),
        )
        if self.viewed_revisions is not None and not isinstance(self.viewed_revisions, RevisionVector):
            raise TypeError("viewed_revisions must be a RevisionVector or None")
        if self.reason is not None:
            object.__setattr__(self, "reason", _required_identity(self.reason, "reason"))

    def require_existing_aggregate_version(self) -> int:
        if self.expected_workflow_version is None:
            raise ValueError("existing-aggregate commands require expected_workflow_version")
        return self.expected_workflow_version


_PUBLIC_CONTRACTS: Final = (
    AccessScope,
    Principal,
    CurrentAuthorizationAuthority,
    MutableCurrentAuthorizationAuthority,
    RevisionVector,
    CommandContext,
)
