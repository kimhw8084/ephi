"""Canonical EPHI application boundary and deterministic self-check."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import RuntimeConfig


@dataclass(frozen=True)
class ApplicationIdentity:
    """Repository-native identity for this new EPHI implementation."""

    application_id: str = "ephi"
    package_name: str = "ephi"
    version: str = "0.1.0"
    python_requires: str = ">=3.11,<3.14"
    source_root: str = "src/ephi"
    framework_distribution: str = "nicegui-base"
    framework_version: str = "3.0.0a8"
    framework_commit: str = "000298562d6bcbf6df304edbd41b98b30fe4bfcf"
    nicegui_version: str = "3.15.0"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class EphiApplication:
    """The minimal application object exposed to future adapters and entrypoints."""

    config: RuntimeConfig
    identity: ApplicationIdentity = ApplicationIdentity()

    def self_check(self) -> dict[str, object]:
        """Return deterministic identity/config facts without external I/O."""

        return {
            "status": "PASS",
            "application": self.identity.as_dict(),
            "runtime": self.config.as_dict(),
            "checks": {
                "canonical_package": "ephi",
                "behavioral_features": "NOT_IMPLEMENTED",
                "company_bindings": "NOT_IMPLEMENTED",
                "storage": "NOT_IMPLEMENTED",
            },
        }


def create_application(config: RuntimeConfig | None = None) -> EphiApplication:
    """Construct the canonical application boundary."""

    return EphiApplication(config=config or RuntimeConfig())

