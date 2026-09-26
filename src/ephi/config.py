"""Small, deterministic runtime configuration boundary for the canonical baseline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from typing import Mapping


EPHI_ENV = "EPHI_ENV"
EPHI_HOST = "EPHI_HOST"
EPHI_PORT = "EPHI_PORT"
EPHI_APPLICATION_NAME = "EPHI_APPLICATION_NAME"
EPHI_DOWNSTREAM_ENTRYPOINT = "EPHI_DOWNSTREAM_ENTRYPOINT"
EPHI_POSTGRES_DSN = "EPHI_POSTGRES_DSN"
EPHI_DEV_IDENTITY_SUBJECT = "EPHI_DEV_IDENTITY_SUBJECT"
EPHI_DEV_IDENTITY_CAPABILITIES = "EPHI_DEV_IDENTITY_CAPABILITIES"
EPHI_DEV_SCOPE_ID = "EPHI_DEV_SCOPE_ID"
EPHI_DEV_SITE_ID = "EPHI_DEV_SITE_ID"
EPHI_DEV_AREA_ID = "EPHI_DEV_AREA_ID"
EPHI_DEV_FAMILY_ID = "EPHI_DEV_FAMILY_ID"
EPHI_DEV_AUTH_SESSION_REVISION = "EPHI_DEV_AUTH_SESSION_REVISION"
EPHI_DEV_SECURITY_REVISION = "EPHI_DEV_SECURITY_REVISION"
EPHI_TEST_SELECTED_EPISODE_ID = "EPHI_TEST_SELECTED_EPISODE_ID"
EPHI_W1_EPISODE_ID = "EPHI_W1_EPISODE_ID"

CORE_RUNTIME_ENVIRONMENT = (EPHI_ENV, EPHI_HOST, EPHI_PORT, EPHI_APPLICATION_NAME)
DEVELOPMENT_IDENTITY_ENVIRONMENT = (
    EPHI_DEV_IDENTITY_SUBJECT,
    EPHI_DEV_IDENTITY_CAPABILITIES,
    EPHI_DEV_SCOPE_ID,
    EPHI_DEV_SITE_ID,
    EPHI_DEV_AREA_ID,
    EPHI_DEV_FAMILY_ID,
    EPHI_DEV_AUTH_SESSION_REVISION,
    EPHI_DEV_SECURITY_REVISION,
)
DEVELOPMENT_IDENTITY_REQUIRED_ENVIRONMENT = (
    EPHI_DEV_IDENTITY_SUBJECT,
    EPHI_DEV_IDENTITY_CAPABILITIES,
    EPHI_DEV_SCOPE_ID,
)
TEST_SELECTOR_ENVIRONMENT = (EPHI_TEST_SELECTED_EPISODE_ID, EPHI_W1_EPISODE_ID)


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
        raw_port = values.get(EPHI_PORT, str(cls.port))
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{EPHI_PORT} must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"{EPHI_PORT} must be between 1 and 65535")
        return cls(
            environment=values.get(EPHI_ENV, cls.environment),
            host=values.get(EPHI_HOST, cls.host),
            port=port,
            application_name=values.get(EPHI_APPLICATION_NAME, cls.application_name),
        )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def required_environment_text(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value or value != value.strip() or "\x00" in value:
        raise RuntimeError(f"missing or invalid {name} binding")
    return value


def nonnegative_environment_integer(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True)
class DevelopmentIdentitySettings:
    """Environment-backed identity and scope values for direct dev/test use."""

    subject: str
    capabilities: tuple[str, ...]
    scope_id: str
    site_id: str | None
    area_id: str | None
    family_id: str | None
    auth_session_revision: int
    security_revision: int

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "DevelopmentIdentitySettings":
        values = os.environ if environ is None else environ
        optional = lambda name: values.get(name) or None
        capabilities = tuple(
            item.strip()
            for item in required_environment_text(values, EPHI_DEV_IDENTITY_CAPABILITIES).split(",")
            if item.strip()
        )
        return cls(
            subject=required_environment_text(values, EPHI_DEV_IDENTITY_SUBJECT),
            capabilities=capabilities,
            scope_id=required_environment_text(values, EPHI_DEV_SCOPE_ID),
            site_id=optional(EPHI_DEV_SITE_ID),
            area_id=optional(EPHI_DEV_AREA_ID),
            family_id=optional(EPHI_DEV_FAMILY_ID),
            auth_session_revision=nonnegative_environment_integer(values, EPHI_DEV_AUTH_SESSION_REVISION, 1),
            security_revision=nonnegative_environment_integer(values, EPHI_DEV_SECURITY_REVISION, 1),
        )


def downstream_entrypoint_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """Return the configured provider entrypoint, failing closed outside dev/test."""

    values = os.environ if environ is None else environ
    environment = values.get(EPHI_ENV, "development").strip().lower()
    entrypoint = values.get(EPHI_DOWNSTREAM_ENTRYPOINT, "").strip()
    if not entrypoint and environment not in {"development", "test"}:
        raise RuntimeError("non-development EPHI requires an explicit downstream provider bundle")
    return entrypoint
