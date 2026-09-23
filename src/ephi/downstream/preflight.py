"""Secret-safe downstream discovery, compatibility, and composition preflight."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
from typing import Any

from ephi.identity import ApplicationIdentity

from .boundary import check_synthetic_boundary
from .composition import compose_downstream
from .contracts import (
    DownstreamFailure,
    DownstreamReasonCode,
    ProviderBundle,
    REQUIRED_CATEGORIES,
)
from .validation import provider_inventory, safe_manifest, safe_manifest_hash, validate_provider_bundle


_MODULE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", re.ASCII)
_FACTORY = re.compile(r"^[A-Za-z_]\w*$", re.ASCII)


def load_provider_bundle(entrypoint: str) -> ProviderBundle:
    """Load one exact ``module:factory`` entrypoint without package scanning."""

    if not isinstance(entrypoint, str) or entrypoint.count(":") != 1:
        raise DownstreamFailure(DownstreamReasonCode.INVALID_ENTRYPOINT)
    module_name, factory_name = entrypoint.split(":", 1)
    if not _MODULE.fullmatch(module_name) or not _FACTORY.fullmatch(factory_name):
        raise DownstreamFailure(DownstreamReasonCode.INVALID_ENTRYPOINT)
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, factory_name)
        if not callable(factory):
            raise TypeError
        bundle = factory()
    except Exception:
        raise DownstreamFailure(DownstreamReasonCode.PROVIDER_LOAD_ERROR) from None
    if type(bundle) is not ProviderBundle:
        raise DownstreamFailure(DownstreamReasonCode.PROVIDER_LOAD_ERROR)
    return bundle


def _reason(error: BaseException) -> tuple[str, list[str]]:
    if isinstance(error, DownstreamFailure):
        return error.reason_code.value, list(error.categories)
    return DownstreamReasonCode.COMPOSITION_FAIL_CLOSED.value, []


def preflight(
    entrypoint: str | None = None,
    *,
    compose: bool = True,
) -> dict[str, Any]:
    """Produce deterministic JSON-ready conformance facts without private data."""

    declared = entrypoint if entrypoint is not None else os.environ.get("EPHI_DOWNSTREAM_ENTRYPOINT", "")
    bundle: object | None = None
    load_error: DownstreamFailure | None = None
    if declared:
        try:
            bundle = load_provider_bundle(declared)
        except DownstreamFailure as exc:
            load_error = exc
    compatibility_error: DownstreamFailure | None = load_error
    if bundle is not None:
        try:
            validate_provider_bundle(bundle)
        except DownstreamFailure as exc:
            compatibility_error = exc
    elif not declared:
        compatibility_error = DownstreamFailure(
            DownstreamReasonCode.MISSING_REQUIRED_PROVIDER,
            categories=REQUIRED_CATEGORIES,
        )

    composition_result: dict[str, object] = {"status": "NOT_RUN", "reason_code": "NOT_RUN"}
    if compose and bundle is not None and compatibility_error is None:
        composed = None
        try:
            composed = compose_downstream(bundle)
            composition_result = {"status": "PASS", "reason_code": DownstreamReasonCode.CONTRACT_PASS.value}
        except DownstreamFailure as exc:
            code, categories = _reason(exc)
            composition_result = {"status": "FAIL", "reason_code": code, "categories": categories}
        except Exception:
            composition_result = {
                "status": "FAIL",
                "reason_code": DownstreamReasonCode.COMPOSITION_FAIL_CLOSED.value,
                "categories": [],
            }
        finally:
            if composed is not None:
                try:
                    composed.close()
                except DownstreamFailure as exc:
                    code, categories = _reason(exc)
                    composition_result = {"status": "FAIL", "reason_code": code, "categories": categories}
                except Exception:
                    composition_result = {
                        "status": "FAIL",
                        "reason_code": DownstreamReasonCode.COMPOSITION_FAIL_CLOSED.value,
                        "categories": [],
                    }

    code = (
        compatibility_error.reason_code.value
        if compatibility_error is not None
        else (
            composition_result["reason_code"]
            if composition_result["status"] == "FAIL"
            else DownstreamReasonCode.CONTRACT_PASS.value
        )
    )
    categories = list(compatibility_error.categories) if compatibility_error is not None else list(
        composition_result.get("categories", [])
    )
    compatibility_status = "PASS" if compatibility_error is None else "FAIL"
    identity = ApplicationIdentity()
    manifest = safe_manifest(bundle)
    return {
        "schema_version": 1,
        "status_code": code,
        "ephi_identity": {
            "distribution": identity.distribution,
            "version": identity.version,
            "implementation": identity.implementation,
            "source_authority": identity.source_authority,
            "historical_identity_claimed": identity.historical_identity_claimed,
        },
        "downstream_abi": {
            "id": manifest["abi"]["id"],  # type: ignore[index]
            "version": manifest["abi"]["version"],  # type: ignore[index]
            "safe_manifest_hash": safe_manifest_hash(bundle),
            "manifest": manifest,
        },
        "providers": provider_inventory(bundle),
        "compatibility": {
            "verdict": DownstreamReasonCode.CONTRACT_PASS.value if compatibility_status == "PASS" else code,
            "status": compatibility_status,
            "reason_code": code if compatibility_status != "PASS" else DownstreamReasonCode.CONTRACT_PASS.value,
            "categories": categories,
        },
        "safe_composition_smoke": composition_result,
        "synthetic_boundary": check_synthetic_boundary(),
        "target_qualification": {
            "real_family_source_science_g02_g06": "NOT_RUN",
            "company_identity_and_tls": "NOT_RUN",
            "production_like_performance_capacity_g10": "NOT_RUN",
            "g12_port_gate_production": "NOT_CLAIMED",
            "company_deployment_readiness": "NOT_CLAIMED",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the secret-safe EPHI downstream ABI preflight.")
    parser.add_argument("--entrypoint", help="One explicit downstream module:factory entrypoint.")
    parser.add_argument("--contracts-only", action="store_true", help="Skip PostgreSQL composition smoke.")
    parser.add_argument("--json", action="store_true", help="Emit deterministic JSON (the default output).")
    args = parser.parse_args(argv)
    report = preflight(args.entrypoint, compose=not args.contracts_only)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if report["status_code"] == DownstreamReasonCode.CONTRACT_PASS.value:
        if args.contracts_only or report["safe_composition_smoke"]["status"] == "PASS":
            return 0
    return 2


__all__ = ["load_provider_bundle", "preflight", "main"]
