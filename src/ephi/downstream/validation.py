"""Pure compatibility and safe-manifest functions for downstream ABI v1."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ephi.application import (
    ArtifactBlobStore,
    ArtifactCatalog,
    CheckTemplateCatalog,
    CurrentAuthorizationAuthority,
    DeliveryChannelAdapter,
    MetrologySourceBinding,
    PlannerPolicy,
    Principal,
    RecipientResolver,
    TargetContext,
)
from ephi.recovery import RecoveryPolicy

from .contracts import (
    ABI_ID,
    ABI_VERSION,
    EXPECTED_CONTRACTS,
    MANIFEST_SCHEMA,
    REQUIRED_CATEGORIES,
    SUPPORTED_ABI_MAJOR,
    ArtifactProvider,
    DownstreamFailure,
    DownstreamReasonCode,
    IdentityProvider,
    NotificationProvider,
    PolicyConfiguration,
    PolicyProvider,
    ProviderBinding,
    ProviderBundle,
    ProviderCategory,
    PolicySchemaMetadata,
    RuntimeCapabilities,
    RuntimeProvider,
    SourceProviderBinding,
)


_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _major(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = _SEMVER.fullmatch(value)
    return int(match.group(1)) if match else None


def _binding_implementation_ok(category: ProviderCategory, implementation: object, binding: ProviderBinding) -> bool:
    if category is ProviderCategory.IDENTITY:
        return isinstance(implementation, IdentityProvider)
    if category is ProviderCategory.SOURCE:
        return isinstance(implementation, SourceProviderBinding) and callable(getattr(implementation.observer, "describe", None)) and callable(
            getattr(implementation.observer, "read_partition", None)
        )
    if category is ProviderCategory.ARTIFACTS:
        return isinstance(implementation, ArtifactProvider)
    if category is ProviderCategory.NOTIFICATIONS:
        if not isinstance(implementation, NotificationProvider):
            return False
        try:
            return isinstance(implementation.recipients, RecipientResolver) and isinstance(
                implementation.channel, DeliveryChannelAdapter
            )
        except Exception:
            return False
    if category is ProviderCategory.POLICY:
        if not isinstance(implementation, PolicyProvider):
            return False
        try:
            config = implementation.configuration
            return isinstance(config, PolicyConfiguration) and isinstance(config.check_catalog, CheckTemplateCatalog) and isinstance(
                config.planner_policy, PlannerPolicy
            ) and isinstance(config.recovery_policy, RecoveryPolicy) and all(
                isinstance(item.contexts[0], TargetContext) if item.contexts else True
                for item in config.family_contexts
            )
        except Exception:
            return False
    if category is ProviderCategory.RUNTIME:
        if binding.public_metadata is None or not isinstance(binding.public_metadata, RuntimeCapabilities):
            return False
        if not isinstance(implementation, RuntimeProvider):
            return False
        try:
            return implementation.capabilities == binding.public_metadata
        except Exception:
            return False
    return False


def _metadata_shape_ok(category: ProviderCategory, binding: ProviderBinding) -> bool:
    metadata = binding.public_metadata
    if category is ProviderCategory.RUNTIME:
        return isinstance(metadata, RuntimeCapabilities)
    if category is ProviderCategory.POLICY:
        return isinstance(metadata, PolicySchemaMetadata)
    return metadata is None


def provider_inventory(bundle: object | None) -> list[dict[str, Any]]:
    """Return only fixed category facts and validated public contract metadata."""

    inventory: list[dict[str, Any]] = []
    for category in ProviderCategory:
        binding = bundle.binding(category) if type(bundle) is ProviderBundle else None
        item: dict[str, Any] = {
            "category": category.value,
            "required": True,
            "status": "MISSING" if binding is None else "INCOMPATIBLE",
            "contract": None,
            "policy_schema": None,
        }
        if type(binding) is ProviderBinding:
            try:
                expected_id, required_capabilities = EXPECTED_CONTRACTS[category]
                contract = binding.contract
                major = _major(contract.version)
                public_shape = (
                    contract.category is category
                    and contract.contract_id == expected_id
                    and set(contract.required_capabilities) == set(required_capabilities)
                    and (category is not ProviderCategory.SOURCE or not any(
                        forbidden in capability
                        for capability in contract.optional_capabilities
                        for forbidden in ("command", "control", "actuator", "manufactur", "write")
                    ))
                    and _metadata_shape_ok(category, binding)
                )
                if public_shape:
                    item["contract"] = contract.safe_dict()
                metadata_compatible = True
                if category is ProviderCategory.RUNTIME and isinstance(binding.public_metadata, RuntimeCapabilities):
                    item["runtime_capabilities"] = binding.public_metadata.safe_dict()
                elif category is ProviderCategory.POLICY and isinstance(binding.public_metadata, PolicySchemaMetadata):
                    metadata_compatible = (
                        binding.public_metadata.schema_id == "org.ephi.policy-configuration"
                        and _major(binding.public_metadata.version) == 1
                        and _major(binding.public_metadata.configuration_version) == 1
                    )
                    if metadata_compatible:
                        item["policy_schema"] = binding.public_metadata.safe_dict()
                if public_shape and major == 1 and metadata_compatible:
                    item["status"] = "COMPATIBLE"
            except Exception:
                item["status"] = "INCOMPATIBLE"
                item["contract"] = None
        inventory.append(item)
    return inventory


def safe_manifest(bundle: object | None) -> dict[str, object]:
    """Canonical metadata projection. Provider implementations are never traversed."""

    providers = []
    runtime: dict[str, object] | None = None
    policy_schema: dict[str, str] | None = None
    for item in provider_inventory(bundle):
        providers.append(
            {
                "category": item["category"],
                "required": True,
                "contract": item["contract"],
            }
        )
        if item["category"] == ProviderCategory.RUNTIME.value:
            runtime = item.get("runtime_capabilities")
        elif item["category"] == ProviderCategory.POLICY.value:
            policy_schema = item.get("policy_schema")
    abi_id = bundle.abi_id if type(bundle) is ProviderBundle and bundle.abi_id == ABI_ID else "UNKNOWN"
    abi_version = bundle.abi_version if type(bundle) is ProviderBundle and _major(bundle.abi_version) is not None else "UNKNOWN"
    return {
        "schema": MANIFEST_SCHEMA,
        "abi": {"id": abi_id, "version": abi_version},
        "required_categories": list(REQUIRED_CATEGORIES),
        "providers": providers,
        "policy_schema": policy_schema,
        "runtime": runtime,
    }


def safe_manifest_hash(bundle: object | None) -> str:
    from ephi.application import canonical_json

    return hashlib.sha256(canonical_json(safe_manifest(bundle)).encode("utf-8")).hexdigest()


def validate_provider_bundle(bundle: object) -> ProviderBundle:
    """Validate public compatibility before the composition root calls a provider."""

    if type(bundle) is not ProviderBundle:
        raise DownstreamFailure(DownstreamReasonCode.INCOMPATIBLE_PROVIDER_CONTRACT)
    abi_major = _major(bundle.abi_version)
    if bundle.abi_id != ABI_ID or abi_major != SUPPORTED_ABI_MAJOR:
        raise DownstreamFailure(DownstreamReasonCode.INCOMPATIBLE_ABI)

    missing = tuple(category.value for category in ProviderCategory if getattr(bundle, category.value) is None)
    if missing:
        raise DownstreamFailure(DownstreamReasonCode.MISSING_REQUIRED_PROVIDER, categories=missing)

    incompatible: list[str] = []
    # Public metadata is checked across the complete bundle before protocol
    # inspection touches a provider object.
    for category in ProviderCategory:
        binding = getattr(bundle, category.value)
        if type(binding) is not ProviderBinding:
            incompatible.append(category.value)
            continue
        expected_id, expected_capabilities = EXPECTED_CONTRACTS[category]
        contract = binding.contract
        if (
            contract.category is not category
            or contract.contract_id != expected_id
            or _major(contract.version) != 1
            or set(contract.required_capabilities) != set(expected_capabilities)
            or not _metadata_shape_ok(category, binding)
            or (
                category is ProviderCategory.SOURCE
                and any(
                    forbidden in capability
                    for capability in contract.optional_capabilities
                    for forbidden in ("command", "control", "actuator", "manufactur", "write")
                )
            )
        ):
            incompatible.append(category.value)
    if incompatible:
        raise DownstreamFailure(
            DownstreamReasonCode.INCOMPATIBLE_PROVIDER_CONTRACT,
            categories=tuple(incompatible),
        )
    policy_metadata = bundle.policy.public_metadata
    if (
        not isinstance(policy_metadata, PolicySchemaMetadata)
        or policy_metadata.schema_id != "org.ephi.policy-configuration"
        or _major(policy_metadata.version) != 1
        or _major(policy_metadata.configuration_version) != 1
    ):
        raise DownstreamFailure(
            DownstreamReasonCode.POLICY_SCHEMA_UNSUPPORTED,
            categories=(ProviderCategory.POLICY.value,),
        )
    incompatible_implementations = [
        category.value
        for category in ProviderCategory
        if not _binding_implementation_ok(category, getattr(bundle, category.value).implementation, getattr(bundle, category.value))
    ]
    if incompatible_implementations:
        raise DownstreamFailure(
            DownstreamReasonCode.INCOMPATIBLE_PROVIDER_CONTRACT,
            categories=tuple(incompatible_implementations),
        )
    policy = bundle.policy.implementation.configuration
    if policy.schema_id != policy_metadata.schema_id or policy.version != policy_metadata.version:
        raise DownstreamFailure(
            DownstreamReasonCode.POLICY_SCHEMA_UNSUPPORTED,
            categories=(ProviderCategory.POLICY.value,),
        )
    return bundle


__all__ = ["provider_inventory", "safe_manifest", "safe_manifest_hash", "validate_provider_bundle"]
