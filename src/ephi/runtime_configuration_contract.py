"""Canonical metadata for the EPHI runtime configuration ABI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ephi.application.source_reality import SOURCE_CONFIGURATION_ENVIRONMENT
from ephi.config import (
    CORE_RUNTIME_ENVIRONMENT,
    DEVELOPMENT_IDENTITY_ENVIRONMENT,
    DEVELOPMENT_IDENTITY_REQUIRED_ENVIRONMENT,
    EPHI_APPLICATION_NAME,
    EPHI_DEV_AREA_ID,
    EPHI_DEV_AUTH_SESSION_REVISION,
    EPHI_DEV_FAMILY_ID,
    EPHI_DEV_IDENTITY_CAPABILITIES,
    EPHI_DEV_IDENTITY_SUBJECT,
    EPHI_DEV_SITE_ID,
    EPHI_DEV_SCOPE_ID,
    EPHI_DEV_SECURITY_REVISION,
    EPHI_ENV,
    EPHI_HOST,
    EPHI_PORT,
    EPHI_POSTGRES_DSN,
    EPHI_TEST_SELECTED_EPISODE_ID,
    EPHI_W1_EPISODE_ID,
    EPHI_DOWNSTREAM_ENTRYPOINT,
)
from ephi.transport import (
    EPHI_ALLOWED_BROWSER_ORIGINS,
    NICEGUI_BASE_DEBUG,
    NICEGUI_BASE_DIAGNOSTICS_ENABLED,
    NICEGUI_BASE_EXPECTED_REPLICAS,
    NICEGUI_BASE_PROXY_ENABLED,
    NICEGUI_BASE_ROOT_PATH,
    NICEGUI_BASE_SAME_SITE,
    NICEGUI_BASE_SECURE_SESSION_COOKIE,
    NICEGUI_BASE_SESSION_AFFINITY_CONFIRMED,
    NICEGUI_BASE_STORAGE_SECRET,
    NICEGUI_BASE_TRUSTED_PROXIES,
    NICEGUI_REDIS_URL,
    O8_RUNTIME_CONFIGURATION_ENVIRONMENT,
)


CONFIGURATION_CONTRACT_SCHEMA = "org.ephi.runtime-configuration.v1"
CONFIGURATION_CONTRACT_VERSION = "1.0.0"
CONFIGURATION_CONTRACT_PATH = "src/ephi/runtime_configuration_contract.json"
_HEX_64 = set("0123456789abcdef")


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest_body(value: dict[str, Any]) -> str:
    body = dict(value)
    body.pop("contract_sha256", None)
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _record(
    name: str,
    *,
    owner: str,
    consumer: str,
    profiles: tuple[str, ...],
    semantic_type: str,
    required: bool = False,
    required_profiles: tuple[str, ...] = (),
    required_condition: str | None = None,
    default: dict[str, object] | None = None,
    sensitivity: str = "private_deployment_configuration",
    raw_value_disclosure_permitted: bool = False,
    validator: str,
) -> dict[str, object]:
    requirement_mode = "conditional" if required_condition or required_profiles else "required" if required else "optional"
    return {
        "name": name,
        "owner": owner,
        "consumer": consumer,
        "applicable_profiles": list(profiles),
        "semantic_type": semantic_type,
        "required": required,
        "optional": requirement_mode == "optional",
        "requirement_mode": requirement_mode,
        "required_profiles": list(required_profiles),
        "required_condition": required_condition,
        "default": default or {"kind": "unset"},
        "sensitivity": sensitivity,
        "raw_value_disclosure_permitted": raw_value_disclosure_permitted,
        "authoritative_validator": validator,
    }


_ALL_PROFILES = ("development", "test", "qa", "production")
_DIRECT_PROFILES = ("development", "test")
_BASE_RUNTIME_CONFIGURATION_ENVIRONMENT = tuple(
    name for name in O8_RUNTIME_CONFIGURATION_ENVIRONMENT if name != EPHI_ALLOWED_BROWSER_ORIGINS
)
_CORE_OWNER = "ephi.config.RuntimeSettings"
_DIRECT_CONDITION = "when no EPHI_DOWNSTREAM_ENTRYPOINT selects direct development/test composition"


def _source_semantic_type(name: str) -> str:
    if name.endswith("_HASH"):
        return "sha256_identifier"
    if name.endswith("_VERSION"):
        return "version_identifier"
    if name.endswith("_ADAPTER"):
        return "module_factory_entrypoint"
    if name.endswith("_UNIT"):
        return "unit_identifier"
    return "private_identity_or_mapping_identifier"


def configuration_variables() -> list[dict[str, object]]:
    """Return descriptors tied to the modules that consume each setting."""

    variables: list[dict[str, object]] = [
        _record(
            EPHI_ENV, owner=_CORE_OWNER, consumer="application profile selection and O8 runtime policy",
            profiles=_ALL_PROFILES, semantic_type="environment_profile", default={"kind": "literal", "value": "development"},
            sensitivity="public_metadata", raw_value_disclosure_permitted=True,
            validator="RuntimeSettings.from_environment; BrowserTransportPolicy.from_environment",
        ),
        _record(
            EPHI_HOST, owner=_CORE_OWNER, consumer="NiceGUI Base RuntimeConfig through ephi.transport",
            profiles=_ALL_PROFILES, semantic_type="hostname_or_bind_address", default={"kind": "literal", "value": "127.0.0.1"},
            validator="RuntimeSettings.from_environment; NiceGUI Base RuntimeConfig validation",
        ),
        _record(
            EPHI_PORT, owner=_CORE_OWNER, consumer="NiceGUI Base RuntimeConfig through ephi.transport",
            profiles=_ALL_PROFILES, semantic_type="tcp_port_1_to_65535", default={"kind": "literal", "value": 8080},
            sensitivity="public_metadata", raw_value_disclosure_permitted=True,
            validator="RuntimeSettings.from_environment",
        ),
        _record(
            EPHI_APPLICATION_NAME, owner=_CORE_OWNER, consumer="NiceGUI Base RuntimeConfig through ephi.transport",
            profiles=_ALL_PROFILES, semantic_type="application_name", default={"kind": "literal", "value": "ephi"},
            validator="RuntimeSettings.from_environment; NiceGUI Base RuntimeConfig validation",
        ),
        _record(
            EPHI_DOWNSTREAM_ENTRYPOINT, owner="ephi.config and ephi.downstream.entrypoint",
            consumer="application composition root; provider discovery is a separate authority",
            profiles=_ALL_PROFILES, semantic_type="python_module_factory_entrypoint",
            required_profiles=("qa", "production"),
            validator="validate_provider_entrypoint; downstream_entrypoint_from_environment",
        ),
        _record(
            EPHI_ALLOWED_BROWSER_ORIGINS, owner="ephi.transport.BrowserTransportPolicy",
            consumer="O8 exact browser-origin and WebSocket-origin gate", profiles=_ALL_PROFILES,
            semantic_type="comma_separated_exact_http_origins", required=True,
            validator="BrowserTransportPolicy.from_environment; normalize_browser_origin",
        ),
    ]

    base_descriptors = {
        NICEGUI_BASE_TRUSTED_PROXIES: {
            "type": "comma_separated_proxy_addresses_or_cidrs", "default": {"kind": "literal", "value": "127.0.0.1,::1"}, "validator": "ephi.transport._trusted_proxies; NiceGUI Base ProxyConfig"
        },
        NICEGUI_BASE_PROXY_ENABLED: {
            "type": "boolean", "default": {"kind": "literal", "value": False}, "validator": "ephi.transport._parse_bool; NiceGUI Base ProxyConfig"
        },
        NICEGUI_BASE_EXPECTED_REPLICAS: {
            "type": "positive_integer", "default": {"kind": "literal", "value": 1}, "validator": "ephi.transport._runtime_config_inputs; NiceGUI Base RuntimeConfig"
        },
        NICEGUI_BASE_SAME_SITE: {
            "type": "enum_lax_strict_none", "default": {"kind": "literal", "value": "strict"}, "validator": "ephi.transport._runtime_config_inputs; NiceGUI Base RuntimeConfig"
        },
        NICEGUI_BASE_SECURE_SESSION_COOKIE: {
            "type": "optional_boolean", "default": {"kind": "environment_dependent", "value": "true by default only in production; false in development, test, and QA"}, "validator": "ephi.transport._parse_optional_bool; ephi.transport._validate_browser_cookie_policy; NiceGUI Base RuntimeConfig"
        },
        NICEGUI_BASE_DIAGNOSTICS_ENABLED: {
            "type": "boolean", "default": {"kind": "literal", "value": False}, "validator": "ephi.transport._parse_bool; NiceGUI Base RuntimeConfig"
        },
        NICEGUI_BASE_DEBUG: {
            "type": "boolean", "default": {"kind": "literal", "value": False}, "validator": "ephi.transport._parse_bool; NiceGUI Base RuntimeConfig"
        },
        NICEGUI_BASE_ROOT_PATH: {
            "type": "normalized_mount_path", "default": {"kind": "literal", "value": ""}, "validator": "ephi.transport.normalize_root_path; NiceGUI Base ProxyConfig"
        },
        NICEGUI_BASE_STORAGE_SECRET: {
            "type": "secret_text", "required": True, "default": {"kind": "unset"}, "sensitivity": "secret_credential_material", "validator": "NiceGUI Base RuntimeConfig.validate_environment through ephi.transport"
        },
        NICEGUI_REDIS_URL: {
            "type": "redis_connection_url", "required_condition": "required when NICEGUI_BASE_EXPECTED_REPLICAS is greater than one", "default": {"kind": "unset"}, "sensitivity": "secret_credential_material", "validator": "NiceGUI Base RuntimeConfig.validate_environment through ephi.transport"
        },
        NICEGUI_BASE_SESSION_AFFINITY_CONFIRMED: {
            "type": "boolean_confirmation", "default": {"kind": "literal", "value": False}, "validator": "NiceGUI Base RuntimeConfig.validate_environment through ephi.transport"
        },
    }
    for name in _BASE_RUNTIME_CONFIGURATION_ENVIRONMENT:
        descriptor = base_descriptors[name]
        variables.append(_record(
            name, owner="NiceGUI Base-owned / EPHI-consumed via ephi.transport",
            consumer="EPHI O8 runtime-security boundary and pinned Base runtime",
            profiles=_ALL_PROFILES, semantic_type=str(descriptor["type"]),
            required=bool(descriptor.get("required", False)),
            required_condition=descriptor.get("required_condition") if isinstance(descriptor.get("required_condition"), str) else None,
            default=descriptor.get("default") if isinstance(descriptor.get("default"), dict) else None,
            sensitivity=str(descriptor.get("sensitivity", "private_deployment_configuration")),
            raw_value_disclosure_permitted=False,
            validator=str(descriptor["validator"]),
        ))

    dev_types = {
        EPHI_DEV_IDENTITY_SUBJECT: "identity_subject",
        EPHI_DEV_IDENTITY_CAPABILITIES: "comma_separated_capability_identifiers",
        EPHI_DEV_SCOPE_ID: "scope_identifier",
        EPHI_DEV_SITE_ID: "site_identifier",
        EPHI_DEV_AREA_ID: "area_identifier",
        EPHI_DEV_FAMILY_ID: "family_identifier",
        EPHI_DEV_AUTH_SESSION_REVISION: "nonnegative_integer",
        EPHI_DEV_SECURITY_REVISION: "nonnegative_integer",
    }
    for name in DEVELOPMENT_IDENTITY_ENVIRONMENT:
        variables.append(_record(
            name, owner="ephi.ui.app development/test identity composition",
            consumer="retained environment-backed identity and scope provider",
            profiles=_DIRECT_PROFILES, semantic_type=dev_types[name],
            required=False,
            required_profiles=_DIRECT_PROFILES if name in DEVELOPMENT_IDENTITY_REQUIRED_ENVIRONMENT else (),
            required_condition=_DIRECT_CONDITION if name in DEVELOPMENT_IDENTITY_REQUIRED_ENVIRONMENT else None,
            default={"kind": "literal", "value": 1} if name in {EPHI_DEV_AUTH_SESSION_REVISION, EPHI_DEV_SECURITY_REVISION} else None,
            validator="DevelopmentIdentitySettings.from_environment; Principal; AccessScope",
        ))

    for name in SOURCE_CONFIGURATION_ENVIRONMENT:
        optional = name.endswith(("SITE_ID", "AREA_ID", "REFERENCE_POPULATION_ID", "COMPARABLE_POPULATION_ID"))
        variables.append(_record(
            name, owner="ephi.application.source_reality.SourceBindingConfiguration",
            consumer="retained direct development/test metrology source binding",
            profiles=_DIRECT_PROFILES, semantic_type=_source_semantic_type(name),
            required=False,
            required_profiles=_DIRECT_PROFILES if not optional else (),
            required_condition=_DIRECT_CONDITION if not optional else None,
            default={"kind": "unset"},
            validator="SourceBindingConfiguration.from_environment; missing_required; source_adapter_entrypoint_parts; to_binding",
        ))

    variables.extend([
        _record(
            EPHI_POSTGRES_DSN, owner="ephi.ui.app direct development/test composition and O9 operator tooling",
            consumer="direct dev/test PostgreSQL adapter; source-reality projection; O9 database operations",
            profiles=_ALL_PROFILES, semantic_type="postgresql_connection_url",
            required_condition=_DIRECT_CONDITION, required_profiles=("development", "test"),
            sensitivity="secret_credential_material", validator="PostgreSQL adapter; source_reality.redacted_connection_facts; O9 CLI"
        ),
        _record(
            "EPHI_TEST_POSTGRES_DSN", owner="repository PostgreSQL integration and qualification tooling",
            consumer="DSN-gated tests and synthetic qualification tools", profiles=("test",),
            semantic_type="postgresql_connection_url", sensitivity="secret_credential_material",
            validator="explicit test/qualification CLI and PostgreSQL test adapters"
        ),
        _record(
            "EPHI_TEST_POSTGRES_RESTART_DATA_DIR", owner="PostgreSQL restart integration test tooling",
            consumer="optional paired PostgreSQL restart rehearsal in tests.test_operations_postgresql",
            profiles=("test",), semantic_type="private_filesystem_path", sensitivity="private_deployment_configuration",
            validator="tests.test_operations_postgresql explicit paired-setting check"
        ),
        _record(
            "EPHI_TEST_POSTGRES_PG_CTL", owner="PostgreSQL restart integration test tooling",
            consumer="optional paired PostgreSQL restart rehearsal in tests.test_operations_postgresql",
            profiles=("test",), semantic_type="private_executable_path", sensitivity="private_deployment_configuration",
            validator="tests.test_operations_postgresql explicit paired-setting check"
        ),
        _record(
            EPHI_TEST_SELECTED_EPISODE_ID, owner="ephi.ui.app test selector",
            consumer="selected synthetic Episode in downstream test profile", profiles=("test",),
            semantic_type="episode_identifier", validator="test UI route selection"
        ),
        _record(
            EPHI_W1_EPISODE_ID, owner="ephi.ui.app W1 qualification selector",
            consumer="explicit development/test direct-route qualification fixture", profiles=_DIRECT_PROFILES,
            semantic_type="episode_identifier", validator="development/test UI route selection"
        ),
    ])

    return sorted(variables, key=lambda item: str(item["name"]))


def build_runtime_configuration_contract() -> dict[str, object]:
    body: dict[str, object] = {
        "schema": CONFIGURATION_CONTRACT_SCHEMA,
        "version": CONFIGURATION_CONTRACT_VERSION,
        "sensitivity_classes": [
            "public_metadata",
            "private_deployment_configuration",
            "secret_credential_material",
        ],
        "variables": configuration_variables(),
        "opaque_configuration": {
            "owner": "downstream provider boundary",
            "variable_names": "opaque; provider-specific names are not enumerated by EPHI",
            "values_in_ephi_release_inventory": False,
            "validation_authority": "the explicitly selected downstream provider and its deployment boundary",
        },
        "preflight_boundary": {
            "entrypoint": "ephi-config-preflight",
            "provider_loaded": False,
            "external_connections_attempted": False,
            "pass_code": "CONFIG_CONTRACT_PASS",
            "does_not_assert": [
                "provider composition", "database connectivity", "company identity", "source qualification",
                "TLS or ingress qualification", "production readiness"
            ],
        },
    }
    return {**body, "contract_sha256": _digest_body(body)}


def contract_document_bytes(value: dict[str, object] | None = None) -> bytes:
    contract = build_runtime_configuration_contract() if value is None else value
    return canonical_json_bytes(contract) + b"\n"


def verify_contract_document(value: object, raw: bytes) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("runtime configuration contract must be an object")
    if raw != canonical_json_bytes(value) + b"\n":
        raise ValueError("runtime configuration contract is not canonical JSON")
    if value.get("schema") != CONFIGURATION_CONTRACT_SCHEMA or value.get("version") != CONFIGURATION_CONTRACT_VERSION:
        raise ValueError("runtime configuration contract schema is unsupported")
    digest = value.get("contract_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in _HEX_64 for char in digest):
        raise ValueError("runtime configuration contract digest is invalid")
    if _digest_body(value) != digest:
        raise ValueError("runtime configuration contract digest mismatch")
    return value


def read_contract_document(path: str | Path) -> tuple[dict[str, Any], bytes]:
    file_path = Path(path)
    raw = file_path.read_bytes()
    value = json.loads(raw)
    return verify_contract_document(value, raw), raw


def contract_identity(path: str | Path) -> dict[str, str]:
    value, raw = read_contract_document(path)
    return {
        "schema": str(value["schema"]),
        "version": str(value["version"]),
        "path": CONFIGURATION_CONTRACT_PATH,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def expected_contract_is_current(path: str | Path) -> bool:
    try:
        _, raw = read_contract_document(path)
        return raw == contract_document_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return False


__all__ = [
    "CONFIGURATION_CONTRACT_PATH",
    "CONFIGURATION_CONTRACT_SCHEMA",
    "CONFIGURATION_CONTRACT_VERSION",
    "build_runtime_configuration_contract",
    "canonical_json_bytes",
    "contract_document_bytes",
    "contract_identity",
    "expected_contract_is_current",
    "read_contract_document",
    "verify_contract_document",
]
