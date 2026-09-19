"""Secret-safe source-reality configuration and preflight seam."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import importlib
import importlib.util
import os
from urllib.parse import urlsplit

from .context import AccessScope
from .errors import SourceBindingUnavailableError, SourceQuarantineError
from .source_ingress import MetrologySourceBinding


SOURCE_BINDING_ENVIRONMENT = (
    "EPHI_METROLOGY_SOURCE_ADAPTER",
    "EPHI_METROLOGY_SOURCE_ID",
    "EPHI_METROLOGY_PROVIDER_ID",
    "EPHI_METROLOGY_FAMILY_ID",
    "EPHI_METROLOGY_CAPABILITY_ID",
    "EPHI_METROLOGY_SCOPE_ID",
    "EPHI_METROLOGY_SCHEMA_ID",
    "EPHI_METROLOGY_MAPPING_VERSION",
    "EPHI_METROLOGY_MAPPING_HASH",
    "EPHI_METROLOGY_UNIT",
)


def _safe_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def redacted_connection_facts(value: object) -> dict[str, object]:
    """Return only non-secret DSN shape facts, never the DSN or credentials."""

    raw = _safe_text(value).strip()
    if not raw:
        return {"configured": False, "credentials_redacted": True}
    try:
        parsed = urlsplit(raw)
        return {
            "configured": True,
            "scheme": parsed.scheme or "unknown",
            "host_present": bool(parsed.hostname),
            "port_present": parsed.port is not None,
            "database_present": bool(parsed.path.strip("/")),
            "credentials_redacted": True,
        }
    except ValueError:
        return {"configured": True, "scheme": "invalid", "credentials_redacted": True}


@dataclass(frozen=True, slots=True)
class SourceBindingConfiguration:
    """Logical environment declaration; no secret-bearing values are retained."""

    adapter_entrypoint: str | None
    source_id: str | None
    provider_id: str | None
    family_id: str | None
    capability_id: str | None
    scope_id: str | None
    site_id: str | None
    area_id: str | None
    schema_id: str | None
    mapping_version: str | None
    mapping_hash: str | None
    unit: str | None
    reference_population_id: str | None
    comparable_population_id: str | None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "SourceBindingConfiguration":
        values = os.environ if environ is None else environ
        get = lambda name: (values.get(name) or "").strip() or None
        return cls(
            get("EPHI_METROLOGY_SOURCE_ADAPTER"),
            get("EPHI_METROLOGY_SOURCE_ID"),
            get("EPHI_METROLOGY_PROVIDER_ID"),
            get("EPHI_METROLOGY_FAMILY_ID"),
            get("EPHI_METROLOGY_CAPABILITY_ID"),
            get("EPHI_METROLOGY_SCOPE_ID"),
            get("EPHI_METROLOGY_SITE_ID"),
            get("EPHI_METROLOGY_AREA_ID"),
            get("EPHI_METROLOGY_SCHEMA_ID"),
            get("EPHI_METROLOGY_MAPPING_VERSION"),
            get("EPHI_METROLOGY_MAPPING_HASH"),
            get("EPHI_METROLOGY_UNIT"),
            get("EPHI_METROLOGY_REFERENCE_POPULATION_ID"),
            get("EPHI_METROLOGY_COMPARABLE_POPULATION_ID"),
        )

    @property
    def missing_required(self) -> tuple[str, ...]:
        names = {
            "EPHI_METROLOGY_SOURCE_ADAPTER": self.adapter_entrypoint,
            "EPHI_METROLOGY_SOURCE_ID": self.source_id,
            "EPHI_METROLOGY_PROVIDER_ID": self.provider_id,
            "EPHI_METROLOGY_FAMILY_ID": self.family_id,
            "EPHI_METROLOGY_CAPABILITY_ID": self.capability_id,
            "EPHI_METROLOGY_SCOPE_ID": self.scope_id,
            "EPHI_METROLOGY_SCHEMA_ID": self.schema_id,
            "EPHI_METROLOGY_MAPPING_VERSION": self.mapping_version,
            "EPHI_METROLOGY_MAPPING_HASH": self.mapping_hash,
            "EPHI_METROLOGY_UNIT": self.unit,
        }
        return tuple(name for name, value in names.items() if not value)

    @property
    def adapter_module_available(self) -> bool:
        if not self.adapter_entrypoint or ":" not in self.adapter_entrypoint:
            return False
        module_name, _ = self.adapter_entrypoint.split(":", 1)
        if not module_name or any(char.isspace() for char in module_name):
            return False
        try:
            return importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError):
            return False

    def to_binding(self) -> MetrologySourceBinding:
        missing = self.missing_required
        if missing:
            raise SourceBindingUnavailableError(
                "approved real metrology source binding is incomplete",
                details={"missing_prerequisites": list(missing)},
            )
        try:
            scope = AccessScope(self.scope_id or "", self.site_id, self.area_id, self.family_id)
            return MetrologySourceBinding(
                scope,
                self.source_id or "",
                self.provider_id or "",
                self.family_id or "",
                self.capability_id or "",
                self.adapter_entrypoint or "",
                self.schema_id or "",
                self.mapping_version or "",
                self.mapping_hash or "",
                self.unit or "",
                self.reference_population_id,
                self.comparable_population_id,
            )
        except (SourceQuarantineError, ValueError, TypeError) as exc:
            raise SourceBindingUnavailableError(
                "approved real metrology source binding failed canonical validation",
                details={"missing_prerequisites": ["canonical_binding_identity_or_mapping"]},
            ) from exc

    def load_adapter(self) -> object:
        if self.missing_required:
            raise SourceBindingUnavailableError("approved real metrology source binding is absent")
        if not self.adapter_entrypoint or ":" not in self.adapter_entrypoint:
            raise SourceBindingUnavailableError("source adapter must be an explicit module:factory entrypoint")
        module_name, attribute_name = self.adapter_entrypoint.split(":", 1)
        try:
            factory = getattr(importlib.import_module(module_name), attribute_name)
            adapter = factory() if callable(factory) else factory
        except Exception as exc:
            raise SourceBindingUnavailableError(
                "configured source adapter could not be loaded",
                details={"adapter_configured": True},
            ) from exc
        if not hasattr(adapter, "describe") or not hasattr(adapter, "read_partition"):
            raise SourceBindingUnavailableError(
                "configured source adapter does not implement the bounded observer contract"
            )
        return adapter

    def safe_dict(self) -> dict[str, object]:
        return {
            "declared": not bool(self.missing_required),
            "adapter_configured": bool(self.adapter_entrypoint),
            "adapter_module_available": self.adapter_module_available,
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "family_id": self.family_id,
            "capability_id": self.capability_id,
            "scope_id": self.scope_id,
            "site_id": self.site_id,
            "area_id": self.area_id,
            "schema_id": self.schema_id,
            "mapping_version": self.mapping_version,
            "mapping_hash": self.mapping_hash,
            "unit": self.unit,
            "reference_population_id_present": bool(self.reference_population_id),
            "comparable_population_id_present": bool(self.comparable_population_id),
        }


def preflight_source_reality(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Produce deterministic, secret-safe evidence for the source boundary."""

    values = os.environ if environ is None else environ
    configuration = SourceBindingConfiguration.from_environment(values)
    missing = list(configuration.missing_required)
    if configuration.reference_population_id is None and configuration.comparable_population_id is None:
        missing.append("EPHI_METROLOGY_REFERENCE_POPULATION_ID_or_comparable_population_id")
    if not configuration.adapter_module_available:
        missing.append("approved_observer_adapter_module_and_bounded_partition_probe")
    try:
        binding = configuration.to_binding() if not configuration.missing_required else None
    except SourceBindingUnavailableError:
        binding = None
    status = "BLOCKED_REAL_SOURCE"
    if binding is not None and not missing:
        # Configuration is complete, but only an actual bounded read and
        # durable publication can establish source reality.  This preflight
        # deliberately does not call an external source or invent a snapshot.
        status = "VERIFY_REAL_SOURCE"
        missing.append("bounded_partition_read_artifact_verification_and_postgresql_publication")
    return {
        "status": status,
        "snapshot_qualification": "NOT_RUN",
        "source_binding": configuration.safe_dict(),
        "binding": binding.as_dict() if binding is not None else None,
        "required_facts": {
            "canonical_scope_family_capability_ids": binding is not None,
            "explicit_unit": binding is not None,
            "source_partition_and_revision": False,
            "event_at_source_available_at_ingested_at": False,
            "mapping_hash_and_version": binding is not None,
            "reference_or_comparable_population_identity": bool(
                configuration.reference_population_id or configuration.comparable_population_id
            ),
        },
        "capability": {
            "state": "UNAVAILABLE",
            "latest_event_at": None,
            "latest_available_at": None,
            "checked_at": None,
            "freshness_age_seconds": None,
            "reason": "REAL_SOURCE_SNAPSHOT_NOT_PUBLISHED",
        },
        "missing_prerequisites": sorted(set(missing)),
        "postgresql": redacted_connection_facts(values.get("EPHI_POSTGRES_DSN")),
        "secret_safety": {
            "credentials_printed": False,
            "raw_rows_printed": False,
            "dsn_printed": False,
        },
        "evidence_boundary": "No synthetic fixture can satisfy G02 or G06; real-family evidence is NOT_RUN.",
    }


def require_runtime_source_binding(environ: Mapping[str, str] | None = None) -> tuple[object, MetrologySourceBinding]:
    """Fail closed before composition when no approved observer binding exists."""

    configuration = SourceBindingConfiguration.from_environment(environ)
    adapter = configuration.load_adapter()
    expected = configuration.to_binding()
    try:
        described = adapter.describe()
    except Exception as exc:
        raise SourceBindingUnavailableError("configured source adapter could not describe its binding") from exc
    if not isinstance(described, MetrologySourceBinding) or described != expected:
        raise SourceBindingUnavailableError("configured source adapter binding does not match declared identity")
    return adapter, described


__all__ = [
    "SOURCE_BINDING_ENVIRONMENT",
    "SourceBindingConfiguration",
    "preflight_source_reality",
    "redacted_connection_facts",
    "require_runtime_source_binding",
]
