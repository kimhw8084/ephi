"""Public, versioned contracts for one-way downstream EPHI integrations.

Only contract metadata is serialized into the safe manifest. Implementations
and their private configuration remain in the downstream deployment package.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ephi.application import (
    AccessScope,
    ArtifactBlobStore,
    ArtifactCatalog,
    CheckTemplateCatalog,
    MetrologyObservation,
    MetrologySourceBinding,
    PlannerPolicy,
    Principal,
    RecipientResolver,
    DeliveryChannelAdapter,
    TargetContext,
)
from ephi.recovery import RecoveryPolicy


ABI_ID = "org.ephi.downstream"
ABI_VERSION = "1.0.0"
PROVIDER_CONTRACT_VERSION = "1.0.0"
MANIFEST_SCHEMA = "org.ephi.downstream.manifest.v1"
SUPPORTED_ABI_MAJOR = 1
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_PUBLIC_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9._-]{0,95}$")


class ProviderCategory(StrEnum):
    IDENTITY = "identity"
    SOURCE = "source"
    ARTIFACTS = "artifacts"
    NOTIFICATIONS = "notifications"
    POLICY = "policy"
    RUNTIME = "runtime"


REQUIRED_CATEGORIES = tuple(category.value for category in ProviderCategory)


class DownstreamReasonCode(StrEnum):
    CONTRACT_PASS = "CONTRACT_PASS"
    MISSING_REQUIRED_PROVIDER = "MISSING_REQUIRED_PROVIDER"
    INCOMPATIBLE_ABI = "INCOMPATIBLE_ABI"
    INCOMPATIBLE_PROVIDER_CONTRACT = "INCOMPATIBLE_PROVIDER_CONTRACT"
    PROVIDER_LOAD_ERROR = "PROVIDER_LOAD_ERROR"
    COMPOSITION_FAIL_CLOSED = "COMPOSITION_FAIL_CLOSED"
    SOURCE_BINDING_MISMATCH = "SOURCE_BINDING_MISMATCH"
    POLICY_SCHEMA_UNSUPPORTED = "POLICY_SCHEMA_UNSUPPORTED"
    INVALID_ENTRYPOINT = "INVALID_ENTRYPOINT"


class DownstreamFailure(Exception):
    """Bounded typed failure. It intentionally stores no raw provider text."""

    def __init__(
        self,
        reason_code: DownstreamReasonCode,
        *,
        categories: tuple[str, ...] = (),
    ) -> None:
        self.reason_code = DownstreamReasonCode(reason_code)
        allowed = {item.value for item in ProviderCategory}
        self.categories = tuple(sorted({item for item in categories if item in allowed}))
        super().__init__(self.reason_code.value)


def _version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise ValueError("version")
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError("version")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _public_token(value: object, field: str) -> str:
    if not isinstance(value, str) or _PUBLIC_TOKEN.fullmatch(value) is None:
        raise ValueError(field)
    return value


def _generic_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or len(value) > 240:
        raise ValueError(field)
    return value


def _tokens(values: object, pattern: re.Pattern[str], field: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list, frozenset)):
        raise ValueError(field)
    result = tuple(sorted(set(values)))
    if any(not isinstance(item, str) or pattern.fullmatch(item) is None for item in result):
        raise ValueError(field)
    if len(result) != len(values):
        raise ValueError(field)
    return result


@dataclass(frozen=True, slots=True)
class ProviderContract:
    """Safe identity for one provider implementation's public contract."""

    category: ProviderCategory
    contract_id: str
    version: str
    required_capabilities: tuple[str, ...]
    optional_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            category = ProviderCategory(self.category)
            contract_id = _public_token(self.contract_id, "contract_id")
            _version(self.version)
            required = _tokens(self.required_capabilities, _CAPABILITY, "required_capabilities")
            optional = _tokens(self.optional_capabilities, _CAPABILITY, "optional_capabilities")
            private_markers = ("secret", "password", "credential", "cookie", "token", "dsn", "endpoint", "url", "path", "mapping", "source-row")
            if (
                set(required) & set(optional)
                or any(not item.startswith("optional.") for item in optional)
                or any(marker in item for item in optional for marker in private_markers)
            ):
                raise ValueError("optional_capabilities")
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid public provider contract metadata") from exc
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "contract_id", contract_id)
        object.__setattr__(self, "required_capabilities", required)
        object.__setattr__(self, "optional_capabilities", optional)

    def safe_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "contract_id": self.contract_id,
            "version": self.version,
            "required_capabilities": list(self.required_capabilities),
            "optional_capabilities": list(self.optional_capabilities),
        }


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    contract: ProviderContract
    implementation: object
    public_metadata: RuntimeCapabilities | PolicySchemaMetadata | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.contract, ProviderContract):
            raise TypeError("contract must be a ProviderContract")
        if self.implementation is None:
            raise TypeError("implementation is required")
        if self.public_metadata is not None and not isinstance(self.public_metadata, (RuntimeCapabilities, PolicySchemaMetadata)):
            raise TypeError("public_metadata must be a public category metadata value or None")


@dataclass(frozen=True, slots=True)
class FamilyContextConfiguration:
    """Family-owned context identities passed through existing generic types."""

    family_id: str
    version: str
    contexts: tuple[TargetContext, ...]

    def __post_init__(self) -> None:
        _generic_identity(self.family_id, "family_id")
        _version(self.version)
        if not isinstance(self.contexts, (tuple, list)) or any(not isinstance(item, TargetContext) for item in self.contexts):
            raise TypeError("contexts must contain TargetContext values")
        contexts = tuple(sorted(self.contexts, key=lambda item: item.target_identity))
        if len({item.target_identity for item in contexts}) != len(contexts):
            raise ValueError("context target identities must be unique")
        object.__setattr__(self, "contexts", contexts)


@dataclass(frozen=True, slots=True)
class PolicyConfiguration:
    """Typed downstream values; generic validation stays with EPHI types."""

    schema_id: str
    version: str
    check_catalog: CheckTemplateCatalog
    planner_policy: PlannerPolicy
    recovery_policy: RecoveryPolicy
    family_contexts: tuple[FamilyContextConfiguration, ...] = ()

    def __post_init__(self) -> None:
        _public_token(self.schema_id, "schema_id")
        _version(self.version)
        if not isinstance(self.check_catalog, CheckTemplateCatalog):
            raise TypeError("check_catalog must be a CheckTemplateCatalog")
        if not isinstance(self.planner_policy, PlannerPolicy):
            raise TypeError("planner_policy must be a PlannerPolicy")
        if not isinstance(self.recovery_policy, RecoveryPolicy):
            raise TypeError("recovery_policy must be a RecoveryPolicy")
        if not isinstance(self.family_contexts, (tuple, list)) or any(
            not isinstance(item, FamilyContextConfiguration) for item in self.family_contexts
        ):
            raise TypeError("family_contexts must contain FamilyContextConfiguration values")
        families = tuple(sorted(self.family_contexts, key=lambda item: item.family_id))
        if len({item.family_id for item in families}) != len(families):
            raise ValueError("family identities must be unique")
        object.__setattr__(self, "family_contexts", families)


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Public preflight metadata, not a production-readiness assertion."""

    target_environment_class: str
    postgresql_major_version: int
    postgresql_contract_version: str

    def __post_init__(self) -> None:
        if self.target_environment_class not in {"development", "test", "qa", "production"}:
            raise ValueError("target_environment_class")
        if isinstance(self.postgresql_major_version, bool) or not isinstance(self.postgresql_major_version, int) or self.postgresql_major_version < 1:
            raise ValueError("postgresql_major_version")
        _version(self.postgresql_contract_version)

    def safe_dict(self) -> dict[str, object]:
        return {
            "target_environment_class": self.target_environment_class,
            "postgresql_major_version": self.postgresql_major_version,
            "postgresql_contract_version": self.postgresql_contract_version,
        }


@dataclass(frozen=True, slots=True)
class PolicySchemaMetadata:
    """Public schema identity kept separate from private policy values."""

    schema_id: str
    version: str
    configuration_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _public_token(self.schema_id, "schema_id")
        _version(self.version)
        _version(self.configuration_version)

    def safe_dict(self) -> dict[str, str]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.version,
            "configuration_version": self.configuration_version,
        }


@runtime_checkable
class IdentityProvider(Protocol):
    """Resolve operation and current identities into canonical EPHI types."""

    def resolve_principal(self) -> Principal: ...

    def resolve_scope(self) -> AccessScope: ...

    def resolve_current_principal(self, subject: str) -> Principal: ...


@runtime_checkable
class BoundedMetrologyObserver(Protocol):
    """Read-only bounded observer; it has no manufacturing command methods."""

    def describe(self) -> MetrologySourceBinding: ...

    def read_partition(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        limit: int,
    ) -> tuple[MetrologyObservation, ...]: ...


@runtime_checkable
class ArtifactProvider(Protocol):
    def blob_store(self) -> ArtifactBlobStore: ...

    def catalog_for(self, postgresql_adapter: object) -> ArtifactCatalog: ...


@runtime_checkable
class NotificationProvider(Protocol):
    @property
    def recipients(self) -> RecipientResolver: ...

    @property
    def channel(self) -> DeliveryChannelAdapter: ...


@runtime_checkable
class PolicyProvider(Protocol):
    @property
    def configuration(self) -> PolicyConfiguration: ...


@runtime_checkable
class RuntimeProvider(Protocol):
    @property
    def capabilities(self) -> RuntimeCapabilities: ...

    def open_postgresql(self) -> object: ...


@dataclass(frozen=True, slots=True)
class SourceProviderBinding:
    expected_binding: MetrologySourceBinding
    observer: BoundedMetrologyObserver

    def __post_init__(self) -> None:
        if not isinstance(self.expected_binding, MetrologySourceBinding):
            raise TypeError("expected_binding must be a MetrologySourceBinding")


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    """Immutable set of explicitly supplied downstream provider categories."""

    abi_id: str = ABI_ID
    abi_version: str = ABI_VERSION
    identity: ProviderBinding | None = None
    source: ProviderBinding | None = None
    artifacts: ProviderBinding | None = None
    notifications: ProviderBinding | None = None
    policy: ProviderBinding | None = None
    runtime: ProviderBinding | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.abi_id, str) or not self.abi_id or not isinstance(self.abi_version, str):
            raise TypeError("ABI identity must be explicit text")

    def binding(self, category: ProviderCategory) -> ProviderBinding | None:
        return getattr(self, ProviderCategory(category).value)


EXPECTED_CONTRACTS = MappingProxyType({
    ProviderCategory.IDENTITY: (
        "org.ephi.identity.current-authorization",
        ("principal.resolve", "authorization.current.resolve"),
    ),
    ProviderCategory.SOURCE: (
        "org.ephi.source.metrology-observer",
        ("observation.bounded.read", "binding.exact.describe"),
    ),
    ProviderCategory.ARTIFACTS: (
        "org.ephi.artifacts.immutable",
        ("content.sha256-size.identity", "catalog.scope-isolated", "authorization.current"),
    ),
    ProviderCategory.NOTIFICATIONS: (
        "org.ephi.notifications.handoff-delivery",
        ("outbox.committed-intents", "recipient.authorization.current", "delivery.unknown.reconcile"),
    ),
    ProviderCategory.POLICY: (
        "org.ephi.policy.curated-configuration",
        ("policy.typed.v1", "validation.upstream"),
    ),
    ProviderCategory.RUNTIME: (
        "org.ephi.runtime.deployment-capabilities",
        ("postgresql.reference.v1", "runtime-settings.preserved"),
    ),
})


def provider_contract(category: ProviderCategory, version: str = PROVIDER_CONTRACT_VERSION) -> ProviderContract:
    """Construct the published required contract descriptor for one category."""

    category = ProviderCategory(category)
    contract_id, capabilities = EXPECTED_CONTRACTS[category]
    return ProviderContract(category, contract_id, version, capabilities)


__all__ = [
    "ABI_ID",
    "ABI_VERSION",
    "PROVIDER_CONTRACT_VERSION",
    "MANIFEST_SCHEMA",
    "SUPPORTED_ABI_MAJOR",
    "REQUIRED_CATEGORIES",
    "ProviderCategory",
    "DownstreamReasonCode",
    "DownstreamFailure",
    "ProviderContract",
    "ProviderBinding",
    "ProviderBundle",
    "RuntimeCapabilities",
    "PolicySchemaMetadata",
    "PolicyConfiguration",
    "FamilyContextConfiguration",
    "IdentityProvider",
    "BoundedMetrologyObserver",
    "ArtifactProvider",
    "NotificationProvider",
    "PolicyProvider",
    "RuntimeProvider",
    "SourceProviderBinding",
    "EXPECTED_CONTRACTS",
    "provider_contract",
]
