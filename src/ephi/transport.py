"""EPHI browser transport and runtime-security boundary.

The pinned NiceGUI Base runtime owns session middleware, cookies, proxy
forwarding, docs/server-header suppression, and framework security headers.
This module adds only the EPHI-specific exact-origin gate and a bounded
preflight projection of that combined configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
import re
from typing import Any, Mapping
from urllib.parse import SplitResult, urlsplit


class BrowserTransportPolicyError(ValueError):
    """The EPHI browser transport policy is malformed or incomplete."""


class OriginDeniedError(BrowserTransportPolicyError):
    """A browser request does not carry an approved exact origin."""


_ENVIRONMENTS = {"development", "test", "qa", "production"}
_BROWSER_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_HOST_LABEL = re.compile(r"^[A-Za-z0-9-]{1,63}$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _effective_port(scheme: str, parsed: SplitResult) -> int:
    try:
        port = parsed.port
    except ValueError as exc:
        raise BrowserTransportPolicyError("MALFORMED_ORIGIN_PORT") from exc
    if port is None:
        return 80 if scheme == "http" else 443
    if not 1 <= port <= 65535:
        raise BrowserTransportPolicyError("ORIGIN_PORT_OUT_OF_RANGE")
    return port


def _canonical_host(parsed: SplitResult) -> str:
    if parsed.username is not None or parsed.password is not None:
        raise BrowserTransportPolicyError("ORIGIN_USERINFO_FORBIDDEN")
    host = parsed.hostname
    if not host or "%" in host or any(ord(char) > 127 for char in host):
        raise BrowserTransportPolicyError("AMBIGUOUS_ORIGIN_HOST")
    host = host.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.startswith(".") or host.endswith(".") or ".." in host:
            raise BrowserTransportPolicyError("AMBIGUOUS_ORIGIN_HOST")
        labels = host.split(".")
        if len(labels) == 4 and all(label.isdigit() for label in labels):
            raise BrowserTransportPolicyError("AMBIGUOUS_ORIGIN_HOST")
        if any(not _HOST_LABEL.fullmatch(label) or label.startswith("-") or label.endswith("-") for label in labels):
            raise BrowserTransportPolicyError("AMBIGUOUS_ORIGIN_HOST")
    else:
        host = address.compressed
    if ":" in host:
        return f"[{host}]"
    return host


def normalize_browser_origin(value: str) -> str:
    """Normalize one browser origin to ``scheme://host:effective-port``.

    The parser intentionally rejects origin-looking values with a path,
    query, fragment, userinfo, wildcard, ``null`` or an ambiguous host.
    """

    if not isinstance(value, str) or not value or value != value.strip() or any(ord(char) < 0x20 for char in value):
        raise BrowserTransportPolicyError("MALFORMED_ORIGIN")
    if value.lower() in {"null", "*"} or "*" in value:
        raise BrowserTransportPolicyError("WILDCARD_OR_NULL_ORIGIN")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise BrowserTransportPolicyError("MALFORMED_ORIGIN") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise BrowserTransportPolicyError("ORIGIN_SCHEME_OR_HOST_REQUIRED")
    if parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise BrowserTransportPolicyError("ORIGIN_MUST_NOT_HAVE_PATH_QUERY_OR_FRAGMENT")
    if parsed.netloc.endswith(":"):
        raise BrowserTransportPolicyError("MALFORMED_ORIGIN_PORT")
    host = _canonical_host(parsed)
    port = _effective_port(scheme, parsed)
    return f"{scheme}://{host}:{port}"


def _origin_is_loopback(origin: str) -> bool:
    parsed = urlsplit(origin)
    hostname = parsed.hostname or ""
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _header_values(scope: Mapping[str, Any], name: str) -> tuple[str, ...]:
    wanted = name.lower().encode("latin-1")
    values = []
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() == wanted:
            values.append(raw_value.decode("latin-1"))
    return tuple(values)


@dataclass(frozen=True, slots=True)
class BrowserTransportPolicy:
    """EPHI's exact browser-origin policy.

    A policy never has an implicit wildcard or development fallback. The
    allowlist is normalized during construction and is the only browser
    origin decision used by the ASGI gate.
    """

    environment: str
    allowed_origins: tuple[str, ...]
    state_changing_methods: frozenset[str] = _BROWSER_METHODS
    require_websocket_origin: bool = True

    def __post_init__(self) -> None:
        environment = self.environment.lower().strip()
        if environment not in _ENVIRONMENTS:
            raise BrowserTransportPolicyError("INVALID_ENVIRONMENT")
        normalized: list[str] = []
        for origin in self.allowed_origins:
            normalized.append(normalize_browser_origin(origin))
        if len(set(normalized)) != len(normalized):
            raise BrowserTransportPolicyError("DUPLICATE_BROWSER_ORIGIN")
        if environment in {"development", "test"} and any(not _origin_is_loopback(origin) for origin in normalized):
            raise BrowserTransportPolicyError("NON_LOOPBACK_DEVELOPMENT_ORIGIN")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "allowed_origins", tuple(sorted(normalized)))
        object.__setattr__(self, "state_changing_methods", frozenset(method.upper() for method in self.state_changing_methods))

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "BrowserTransportPolicy":
        values = os.environ if environ is None else environ
        raw = values.get("EPHI_ALLOWED_BROWSER_ORIGINS", "")
        origins = tuple(item.strip() for item in raw.split(",") if item.strip())
        policy = cls(values.get("EPHI_ENV", "development"), origins)
        policy.validate()
        return policy

    def validate(self) -> None:
        if not self.allowed_origins:
            raise BrowserTransportPolicyError("BROWSER_ORIGIN_ALLOWLIST_REQUIRED")
        if self.environment in {"qa", "production"} and not self.allowed_origins:
            raise BrowserTransportPolicyError("BROWSER_ORIGIN_ALLOWLIST_REQUIRED")

    def normalize_request_origin(self, value: str | None) -> str:
        if value is None or value.lower() in {"null", "*"}:
            raise OriginDeniedError("MISSING_OR_NULL_BROWSER_ORIGIN")
        try:
            normalized = normalize_browser_origin(value)
        except BrowserTransportPolicyError as exc:
            raise OriginDeniedError(str(exc)) from exc
        if normalized not in self.allowed_origins:
            raise OriginDeniedError("ORIGIN_NOT_ALLOWED")
        return normalized

    def as_dict(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "allowed_origins": list(self.allowed_origins),
            "state_changing_methods": sorted(self.state_changing_methods),
            "require_websocket_origin": self.require_websocket_origin,
        }


class BrowserTransportMiddleware:
    """Pure ASGI exact-origin gate installed outside NiceGUI handling."""

    def __init__(self, app: Any, policy: BrowserTransportPolicy):
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        origin_values = _header_values(scope, "origin")
        if scope_type == "websocket" and self.policy.require_websocket_origin:
            if len(origin_values) != 1:
                await self._reject_websocket(send, "MISSING_OR_AMBIGUOUS_ORIGIN")
                return
            try:
                self.policy.normalize_request_origin(origin_values[0])
            except OriginDeniedError:
                await self._reject_websocket(send, "ORIGIN_NOT_ALLOWED")
                return
        elif scope_type == "http" and str(scope.get("method", "")).upper() in self.policy.state_changing_methods:
            if len(origin_values) != 1:
                await self._reject_http(send)
                return
            try:
                self.policy.normalize_request_origin(origin_values[0])
            except OriginDeniedError:
                await self._reject_http(send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject_http(send: Any) -> None:
        body = b'{"detail":"origin denied"}'
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _reject_websocket(send: Any, reason: str) -> None:
        await send({"type": "websocket.close", "code": 1008, "reason": reason})


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    normalized = value.lower().strip()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise BrowserTransportPolicyError("INVALID_BOOLEAN_CONFIGURATION")


def _parse_optional_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return _parse_bool(value)


def normalize_root_path(value: str) -> str:
    if value == "/":
        return ""
    if value and (not value.startswith("/") or "?" in value or "#" in value or "\\" in value or "//" in value):
        raise BrowserTransportPolicyError("INVALID_ROOT_PATH")
    return value.rstrip("/")


def _trusted_proxies(values: Mapping[str, str]) -> tuple[str, ...]:
    if "NICEGUI_BASE_TRUSTED_PROXIES" in values:
        raw = values["NICEGUI_BASE_TRUSTED_PROXIES"]
        result = tuple(item.strip() for item in raw.split(",") if item.strip())
    else:
        result = ("127.0.0.1", "::1")
    if any(item == "*" or "*" in item for item in result):
        raise BrowserTransportPolicyError("WILDCARD_TRUSTED_PROXY_FORBIDDEN")
    return result


def build_runtime_config(settings: Any, environ: Mapping[str, str] | None = None) -> Any:
    """Construct the pinned Base RuntimeConfig from the EPHI boundary."""

    values = os.environ if environ is None else environ
    from nicegui_base import ProxyConfig, RuntimeConfig, RuntimeEnvironment

    environment_map = {
        "development": RuntimeEnvironment.DEV,
        "test": RuntimeEnvironment.TEST,
        "qa": RuntimeEnvironment.QA,
        "production": RuntimeEnvironment.PROD,
    }
    environment = environment_map.get(str(settings.environment).lower())
    if environment is None:
        raise BrowserTransportPolicyError("INVALID_ENVIRONMENT")
    trusted = _trusted_proxies(values)
    proxy = ProxyConfig(
        enabled=_parse_bool(values.get("NICEGUI_BASE_PROXY_ENABLED"), default=False),
        trusted_proxies=trusted,
        root_path=normalize_root_path(values.get("NICEGUI_BASE_ROOT_PATH", "")),
    )
    expected_replicas = int(values.get("NICEGUI_BASE_EXPECTED_REPLICAS", "1"))
    same_site = values.get("NICEGUI_BASE_SAME_SITE", "strict").lower().strip()
    config = RuntimeConfig(
        app_name=settings.application_name,
        app_version="0.1.0",
        environment=environment,
        host=settings.host,
        port=settings.port,
        title="EPHI",
        show_browser=False,
        reload=False,
        storage_secret_env="NICEGUI_BASE_STORAGE_SECRET",
        require_storage_secret=True,
        secure_session_cookie=_parse_optional_bool(values.get("NICEGUI_BASE_SECURE_SESSION_COOKIE")),
        same_site=same_site,
        proxy=proxy,
        diagnostics_enabled=_parse_bool(values.get("NICEGUI_BASE_DIAGNOSTICS_ENABLED"), default=False),
        debug=_parse_bool(values.get("NICEGUI_BASE_DEBUG"), default=False),
        expected_replicas=expected_replicas,
    )
    return config


def _origin_observations(values: Mapping[str, str]) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    errors: list[str] = []
    raw = values.get("EPHI_ALLOWED_BROWSER_ORIGINS", "")
    for item in (part.strip() for part in raw.split(",") if part.strip()):
        try:
            normalized.append(normalize_browser_origin(item))
        except BrowserTransportPolicyError as exc:
            errors.append(str(exc))
    return sorted(set(normalized)), sorted(set(errors))


def security_preflight(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Return bounded, secret-safe EPHI/Base runtime security facts."""

    values = os.environ if environ is None else environ
    environment = values.get("EPHI_ENV", "development").strip().lower()
    normalized_origins, origin_errors = _origin_observations(values)
    reasons = list(origin_errors)
    if environment not in _ENVIRONMENTS:
        reasons.append("INVALID_ENVIRONMENT")
    if not normalized_origins:
        reasons.append("BROWSER_ORIGIN_ALLOWLIST_REQUIRED")

    storage_secret_present = bool(values.get("NICEGUI_BASE_STORAGE_SECRET"))
    if not storage_secret_present:
        reasons.append("MISSING_STORAGE_SECRET")

    try:
        secure_override = _parse_optional_bool(values.get("NICEGUI_BASE_SECURE_SESSION_COOKIE"))
    except BrowserTransportPolicyError as exc:
        secure_override = None
        reasons.append(str(exc))
    effective_secure_cookie = secure_override if secure_override is not None else environment == "production"
    same_site = values.get("NICEGUI_BASE_SAME_SITE", "strict").strip().lower()
    if same_site not in {"lax", "strict", "none"}:
        reasons.append("INVALID_SAMESITE")
    if same_site == "none" and not effective_secure_cookie:
        reasons.append("INSECURE_SAMESITE_NONE")
    if environment == "production" and not effective_secure_cookie:
        reasons.append("PRODUCTION_COOKIE_NOT_SECURE")

    try:
        proxy_enabled = _parse_bool(values.get("NICEGUI_BASE_PROXY_ENABLED"), default=False)
        trusted = _trusted_proxies(values)
    except BrowserTransportPolicyError as exc:
        proxy_enabled = False
        trusted = ()
        reasons.append(str(exc))
    if proxy_enabled and not trusted:
        reasons.append("TRUSTED_PROXY_REQUIRED")
    if proxy_enabled and values.get("EPHI_HOST", "127.0.0.1") in {"127.0.0.1", "localhost", "::1"}:
        reasons.append("PROXY_MODE_BOUND_TO_LOOPBACK")
    try:
        root_path = normalize_root_path(values.get("NICEGUI_BASE_ROOT_PATH", ""))
    except BrowserTransportPolicyError as exc:
        root_path = ""
        reasons.append(str(exc))

    try:
        expected_replicas = int(values.get("NICEGUI_BASE_EXPECTED_REPLICAS", "1"))
        if expected_replicas < 1:
            raise ValueError
    except ValueError:
        expected_replicas = 0
        reasons.append("INVALID_EXPECTED_REPLICAS")
    shared_storage = bool(values.get("NICEGUI_REDIS_URL"))
    session_affinity = values.get("NICEGUI_BASE_SESSION_AFFINITY_CONFIRMED", "").lower() in _TRUE_VALUES
    if expected_replicas > 1 and not shared_storage:
        reasons.append("MULTI_REPLICA_WITHOUT_SHARED_STORAGE")
    if expected_replicas > 1 and not session_affinity:
        reasons.append("MULTI_REPLICA_WITHOUT_SESSION_AFFINITY_CONFIRMATION")

    diagnostics_enabled = values.get("NICEGUI_BASE_DIAGNOSTICS_ENABLED", "").lower() in _TRUE_VALUES
    reason_codes = sorted(set(reasons))
    return {
        "environment": environment,
        "status": "PASS" if not reason_codes else "BLOCKED",
        "reason_codes": reason_codes,
        "storage_secret_present": storage_secret_present,
        "effective_secure_cookie": effective_secure_cookie,
        "same_site": same_site,
        "configured_normalized_origin_count": len(normalized_origins),
        "normalized_non_secret_origins": normalized_origins,
        "proxy_enabled": proxy_enabled,
        "trusted_proxy_count": len(trusted),
        "normalized_root_path": root_path,
        "diagnostics_enabled": diagnostics_enabled,
        "expected_replicas": expected_replicas,
        "shared_storage_configured": shared_storage,
        "session_affinity_confirmed": session_affinity,
        "security_headers_policy": {
            "identity": "nicegui-base.SecurityHeadersMiddleware",
            "summary": "Base security headers active; CSP intentionally not enabled pending certification; production HSTS is Base-controlled",
            "csp": "NOT_ENABLED",
            "server_header": False,
            "fastapi_docs": False,
            "endpoint_documentation": "none",
        },
        "company_identity_established": False,
        "target_tls_ingress_qualification_established": False,
        "qualification_boundary": "Real company identity and target TLS/ingress qualification are NOT established by CHG-152/O8.2.",
    }


def require_security_preflight(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    report = security_preflight(environ)
    if report["status"] != "PASS":
        reasons = ",".join(str(value) for value in report["reason_codes"])
        raise RuntimeError(f"EPHI security preflight blocked: {reasons}")
    return report


__all__ = [
    "BrowserTransportMiddleware",
    "BrowserTransportPolicy",
    "BrowserTransportPolicyError",
    "OriginDeniedError",
    "build_runtime_config",
    "normalize_browser_origin",
    "normalize_root_path",
    "require_security_preflight",
    "security_preflight",
]
