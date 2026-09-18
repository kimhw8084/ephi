"""Small, deterministic runtime configuration boundary for the canonical baseline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from typing import Mapping


@dataclass(frozen=True)
class RuntimeSettings:
    """Configuration identity without storage, company bindings or product behavior."""

    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8080
    application_name: str = "ephi"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "RuntimeSettings":
        values = os.environ if environ is None else environ
        raw_port = values.get("EPHI_PORT", str(cls.port))
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("EPHI_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("EPHI_PORT must be between 1 and 65535")
        return cls(
            environment=values.get("EPHI_ENV", cls.environment),
            host=values.get("EPHI_HOST", cls.host),
            port=port,
            application_name=values.get("EPHI_APPLICATION_NAME", cls.application_name),
        )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)
