"""Secret-safe generic runtime configuration preflight."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ephi.application import AccessScope, Principal
from ephi.application.errors import SourceBindingUnavailableError
from ephi.application.source_reality import SourceBindingConfiguration, source_adapter_entrypoint_parts
from ephi.config import (
    EPHI_DOWNSTREAM_ENTRYPOINT,
    EPHI_ENV,
    EPHI_POSTGRES_DSN,
    DevelopmentIdentitySettings,
    RuntimeSettings,
    downstream_entrypoint_from_environment,
    required_environment_text,
)
from ephi.downstream.contracts import DownstreamFailure, DownstreamReasonCode
from ephi.downstream.entrypoint import validate_provider_entrypoint
from ephi.runtime_configuration_contract import (
    CONFIGURATION_CONTRACT_PATH,
    CONFIGURATION_CONTRACT_SCHEMA,
    CONFIGURATION_CONTRACT_VERSION,
    contract_identity,
    expected_contract_is_current,
)
from ephi.transport import security_preflight


PREFLIGHT_SCHEMA = "org.ephi.runtime-config-preflight.v1"
_PROFILES = {"development", "test", "qa", "production"}


def _profile(values: Mapping[str, str]) -> str:
    candidate = values.get(EPHI_ENV, "development")
    normalized = candidate.strip().lower() if isinstance(candidate, str) else ""
    return normalized if normalized in _PROFILES else "unsupported"


def _configuration_contract_identity() -> dict[str, str] | None:
    path = Path(__file__).with_name("runtime_configuration_contract.json")
    try:
        if not expected_contract_is_current(path):
            return None
        return contract_identity(path)
    except (OSError, ValueError, TypeError):
        return None


def preflight(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Validate generic environment shape without loading providers or connecting."""

    values = os.environ if environ is None else environ
    profile = _profile(values)
    reasons: list[str] = []
    contract = _configuration_contract_identity()
    if contract is None:
        reasons.append("CONFIGURATION_CONTRACT_INVALID")

    try:
        RuntimeSettings.from_environment(values)
        core_settings_status = "PASS"
    except (TypeError, ValueError):
        core_settings_status = "FAIL"
        reasons.append("INVALID_RUNTIME_SETTINGS")

    try:
        security = security_preflight(values)
        runtime_security_status = "PASS" if security.get("status") == "PASS" else "FAIL"
        if runtime_security_status != "PASS":
            reasons.append("RUNTIME_SECURITY_CONFIGURATION_INVALID")
        origin_count = security.get("configured_normalized_origin_count", 0)
        origin_count = origin_count if isinstance(origin_count, int) and not isinstance(origin_count, bool) else 0
        storage_secret_present = security.get("storage_secret_present") is True
        shared_storage_configured = security.get("shared_storage_configured") is True
    except Exception:
        runtime_security_status = "FAIL"
        origin_count = 0
        storage_secret_present = False
        shared_storage_configured = False
        reasons.append("RUNTIME_SECURITY_PREFLIGHT_FAILED")

    declared_entrypoint = values.get(EPHI_DOWNSTREAM_ENTRYPOINT, "")
    entrypoint_configured = isinstance(declared_entrypoint, str) and bool(declared_entrypoint.strip())
    entrypoint_status = "NOT_CONFIGURED"
    selected_entrypoint = ""
    try:
        selected_entrypoint = downstream_entrypoint_from_environment(values)
    except RuntimeError:
        if profile not in {"development", "test"}:
            reasons.append("MISSING_DOWNSTREAM_ENTRYPOINT")
    if entrypoint_configured:
        try:
            validate_provider_entrypoint(selected_entrypoint)
            entrypoint_status = "VALID"
        except DownstreamFailure as exc:
            entrypoint_status = "INVALID"
            if exc.reason_code is DownstreamReasonCode.INVALID_ENTRYPOINT:
                reasons.append("INVALID_DOWNSTREAM_ENTRYPOINT")
            else:
                reasons.append("INVALID_DOWNSTREAM_ENTRYPOINT")
    elif profile in {"qa", "production", "unsupported"}:
        reasons.append("MISSING_DOWNSTREAM_ENTRYPOINT")

    legacy_direct_present = any(
        isinstance(key, str) and key.startswith(("EPHI_DEV_", "EPHI_METROLOGY_"))
        for key in values
    )
    if profile in {"qa", "production"} and not entrypoint_configured and legacy_direct_present:
        reasons.append("LEGACY_COMPOSITION_CONFIGURATION_FORBIDDEN")

    direct_configuration_status = "NOT_SELECTED"
    if profile in {"development", "test"} and not entrypoint_configured:
        direct_configuration_status = "PASS"
        try:
            required_environment_text(values, EPHI_POSTGRES_DSN)
            identity = DevelopmentIdentitySettings.from_environment(values)
            scope = AccessScope(identity.scope_id, identity.site_id, identity.area_id, identity.family_id)
            Principal(
                identity.subject,
                identity.capabilities,
                (scope,),
                identity.auth_session_revision,
                identity.security_revision,
            )
        except RuntimeError:
            direct_configuration_status = "FAIL"
            reasons.append("DEVELOPMENT_CONFIGURATION_INCOMPLETE")
        except (TypeError, ValueError):
            direct_configuration_status = "FAIL"
            reasons.append("DEVELOPMENT_CONFIGURATION_INVALID")
        source = SourceBindingConfiguration.from_environment(values)
        if source.missing_required:
            direct_configuration_status = "FAIL"
            reasons.append("DEVELOPMENT_CONFIGURATION_INCOMPLETE")
        else:
            try:
                source_adapter_entrypoint_parts(source.adapter_entrypoint)
                source.to_binding()
            except SourceBindingUnavailableError:
                direct_configuration_status = "FAIL"
                reasons.append("DEVELOPMENT_SOURCE_BINDING_INVALID")

    if not legacy_direct_present:
        legacy_configuration_status = "NOT_PRESENT"
    elif profile in {"qa", "production"} and not entrypoint_configured:
        legacy_configuration_status = "FALLBACK_FORBIDDEN"
    elif profile in {"qa", "production"}:
        legacy_configuration_status = "PRESENT_WITH_EXPLICIT_BUNDLE"
    else:
        legacy_configuration_status = "DIRECT_DEV_TEST_SETTINGS_PRESENT"

    reason_codes = sorted(set(reasons))
    status_code = "CONFIG_CONTRACT_PASS" if not reason_codes else reason_codes[0]
    return {
        "schema": PREFLIGHT_SCHEMA,
        "status": "PASS" if not reason_codes else "FAIL",
        "status_code": status_code,
        "reason_codes": reason_codes,
        "profile": profile,
        "configuration_contract": contract or {
            "schema": CONFIGURATION_CONTRACT_SCHEMA,
            "version": CONFIGURATION_CONTRACT_VERSION,
            "path": CONFIGURATION_CONTRACT_PATH,
            "sha256": None,
        },
        "checks": {
            "core_runtime_settings": core_settings_status,
            "runtime_security": {
                "status": runtime_security_status,
                "configured_browser_origin_count": origin_count,
                "storage_secret_configured": storage_secret_present,
                "shared_storage_configured": shared_storage_configured,
            },
            "downstream_entrypoint": {
                "configured": entrypoint_configured,
                "shape": entrypoint_status,
            },
            "legacy_direct_configuration": legacy_configuration_status,
            "development_test_direct_configuration": direct_configuration_status,
            "provider_composition": "NOT_RUN",
            "external_connections": "NOT_ATTEMPTED",
        },
        "claim_boundary": "Generic configuration shape only; provider composition, connectivity, company qualification, ingress/TLS, and production readiness are not asserted.",
        "secret_safety": {
            "raw_environment_values_emitted": False,
            "private_values_emitted": False,
            "provider_loaded": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic secret-safe EPHI configuration preflight.")
    parser.add_argument("--json", action="store_true", help="Emit deterministic JSON (the default output).")
    args = parser.parse_args(argv)
    report = preflight()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report["status_code"] == "CONFIG_CONTRACT_PASS" else 2


__all__ = ["main", "preflight"]
