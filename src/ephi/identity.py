"""Identity for the canonical implementation, separate from historical provenance."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ApplicationIdentity:
    """Stable package identity used by installation and deterministic self-checks."""

    distribution: str = "ephi"
    version: str = "0.1.0"
    implementation: str = "canonical-repository"
    source_authority: str = "kimhw8084/ephi"
    historical_identity_claimed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)
