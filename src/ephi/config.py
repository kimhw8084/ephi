"""Small, deterministic runtime configuration boundary for EPHI."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from typing import Mapping


@dataclass(frozen=True)
class RuntimeConfig:
    """Configuration needed to identify the local application boundary.

    It intentionally contains no company bindings, storage configuration, or
    product behavior.  Those are future, explicitly qualified boundaries.
    """

    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8080

    def __post_init__(self) -> None:
        if not self.environment or self.environment.strip() != self.environment:
            raise ValueError("environment must be a non-empty trimmed value")
        if not self.host or self.host.strip() != self.host:
            raise ValueError("host must be a non-empty trimmed value")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("port must be an integer from 1 through 65535")

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> "RuntimeConfig":
        """Read only the bounded EPHI runtime identity settings."""

        values = os.environ if values is None else values
        raw_port = values.get("EPHI_PORT", "8080")
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("EPHI_PORT must be an integer") from exc
        return cls(
            environment=values.get("EPHI_ENV", "development"),
            host=values.get("EPHI_HOST", "127.0.0.1"),
            port=port,
        )

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)
