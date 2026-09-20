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


def install_browser_transport_stack(app: Any, runtime_adapter: Any, policy: BrowserTransportPolicy) -> None:
    """Register the Origin gate before the pinned Base middleware stack.

    FastAPI inserts each new middleware at ``user_middleware[0]`` and
    Starlette composes that list in reverse. Registering the gate first keeps
    Base correlation/security (and a future Base identity adapter) outside the
    gate, while the gate remains before NiceGUI routing and session handling.
    """

    app.add_middleware(BrowserTransportMiddleware, policy=policy)
    runtime_adapter.install_middleware(app)


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise BrowserTransportPolicyError("INVALID_BOOLEAN_CONFIGURATION")
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
    if not isinstance(value, str):
        raise BrowserTransportPolicyError("INVALID_ROOT_PATH")
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


@dataclass(frozen=True, slots=True)
class _RuntimeConfigInputs:
    environment: str
    secure_session_cookie: bool | None
    same_site: str
    proxy_enabled: bool
    trusted_proxies: tuple[str, ...]
    root_path: str
    expected_replicas: int
    diagnostics_enabled: bool
    debug: bool


def _runtime_config_inputs(settings: Any, values: Mapping[str, str]) -> _RuntimeConfigInputs:
    environment_map = {
        "development",
        "test",
        "qa",
        "production",
    }
    environment = str(settings.environment).lower()
    if environment not in environment_map:
        raise BrowserTransportPolicyError("INVALID_ENVIRONMENT")
    trusted = _trusted_proxies(values)
    proxy_enabled = _parse_bool(values.get("NICEGUI_BASE_PROXY_ENABLED"), default=False)
    if proxy_enabled and not trusted:
        raise BrowserTransportPolicyError("TRUSTED_PROXY_REQUIRED")
    expected_replicas_raw = values.get("NICEGUI_BASE_EXPECTED_REPLICAS", "1")
    try:
        expected_replicas = int(expected_replicas_raw)
    except (TypeError, ValueError) as exc:
        raise BrowserTransportPolicyError("INVALID_EXPECTED_REPLICAS") from exc
    if expected_replicas < 1:
        raise BrowserTransportPolicyError("INVALID_EXPECTED_REPLICAS")
    same_site = str(values.get("NICEGUI_BASE_SAME_SITE", "strict")).lower().strip()
    if same_site not in {"lax", "strict", "none"}:
        raise BrowserTransportPolicyError("INVALID_SAMESITE")
    secure_session_cookie = _parse_optional_bool(values.get("NICEGUI_BASE_SECURE_SESSION_COOKIE"))
    if same_site == "none" and (secure_session_cookie is False or (secure_session_cookie is None and environment != "production")):
        raise BrowserTransportPolicyError("INSECURE_SAMESITE_NONE")
    diagnostics_enabled = _parse_bool(values.get("NICEGUI_BASE_DIAGNOSTICS_ENABLED"), default=False)
    debug = _parse_bool(values.get("NICEGUI_BASE_DEBUG"), default=False)
    if environment == "production" and debug:
        raise BrowserTransportPolicyError("PRODUCTION_DEBUG_FORBIDDEN")
    return _RuntimeConfigInputs(
        environment=environment,
        secure_session_cookie=secure_session_cookie,
        same_site=same_site,
        proxy_enabled=proxy_enabled,
        trusted_proxies=trusted,
        root_path=normalize_root_path(values.get("NICEGUI_BASE_ROOT_PATH", "")),
        expected_replicas=expected_replicas,
        diagnostics_enabled=diagnostics_enabled,
        debug=debug,
    )


def _runtime_environment_issues(inputs: _RuntimeConfigInputs, settings: Any, values: Mapping[str, str]) -> tuple[str, ...]:
    issues: list[str] = []
    if not values.get("NICEGUI_BASE_STORAGE_SECRET"):
        issues.append("missing:NICEGUI_BASE_STORAGE_SECRET")
    effective_secure_cookie = inputs.secure_session_cookie if inputs.secure_session_cookie is not None else inputs.environment == "production"
    if inputs.environment == "production" and not effective_secure_cookie:
        issues.append("production_cookie_not_secure")
    if inputs.expected_replicas > 1 and not values.get("NICEGUI_REDIS_URL"):
        issues.append("multi_replica_without_shared_storage")
    if inputs.expected_replicas > 1 and values.get("NICEGUI_BASE_SESSION_AFFINITY_CONFIRMED", "").lower() not in {"1", "true", "yes"}:
        issues.append("multi_replica_without_session_affinity_confirmation")
    if inputs.proxy_enabled and settings.host in {"127.0.0.1", "localhost"}:
        issues.append("proxy_mode_bound_to_loopback")
    return tuple(issues)


@dataclass(frozen=True, slots=True)
class _RuntimeConfigProjection:
    """Dependency-free projection used only by offline preflight."""

    inputs: _RuntimeConfigInputs

    @property
    def effective_secure_cookie(self) -> bool:
        return self.inputs.secure_session_cookie if self.inputs.secure_session_cookie is not None else self.inputs.environment == "production"

    @property
    def proxy(self) -> Any:
        return self.inputs

    def validate_environment(self, values: Mapping[str, str] | None = None, *, settings: Any = None) -> tuple[str, ...]:
        if values is None or settings is None:
            return ()
        return _runtime_environment_issues(self.inputs, settings, values)


def build_runtime_config(settings: Any, environ: Mapping[str, str] | None = None) -> Any:
    """Construct the pinned Base RuntimeConfig from the EPHI boundary."""

    values = os.environ if environ is None else environ
    inputs = _runtime_config_inputs(settings, values)
    from nicegui_base import ProxyConfig, RuntimeConfig, RuntimeEnvironment

    environment_map = {
        "development": RuntimeEnvironment.DEV,
        "test": RuntimeEnvironment.TEST,
        "qa": RuntimeEnvironment.QA,
        "production": RuntimeEnvironment.PROD,
    }
    proxy = ProxyConfig(
        enabled=inputs.proxy_enabled,
        trusted_proxies=inputs.trusted_proxies,
        root_path=inputs.root_path,
    )
    config = RuntimeConfig(
        app_name=settings.application_name,
        app_version="0.1.0",
        environment=environment_map[inputs.environment],
        host=settings.host,
        port=settings.port,
        title="EPHI",
        show_browser=False,
        reload=False,
        storage_secret_env="NICEGUI_BASE_STORAGE_SECRET",
        require_storage_secret=True,
        secure_session_cookie=inputs.secure_session_cookie,
        same_site=inputs.same_site,
        proxy=proxy,
        diagnostics_enabled=inputs.diagnostics_enabled,
        debug=inputs.debug,
        expected_replicas=inputs.expected_replicas,
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
    return sorted(normalized), sorted(set(errors))


def _base_issue_code(issue: str) -> str:
    if issue.startswith("missing:"):
        return "MISSING_STORAGE_SECRET"
    return {
        "production_cookie_not_secure": "PRODUCTION_COOKIE_NOT_SECURE",
        "multi_replica_without_shared_storage": "MULTI_REPLICA_WITHOUT_SHARED_STORAGE",
        "multi_replica_without_session_affinity_confirmation": "MULTI_REPLICA_WITHOUT_SESSION_AFFINITY_CONFIRMATION",
        "proxy_mode_bound_to_loopback": "PROXY_MODE_BOUND_TO_LOOPBACK",
    }.get(issue, "INVALID_RUNTIME_CONFIGURATION")


def _validate_browser_cookie_policy(policy: BrowserTransportPolicy, config: Any) -> None:
    if policy.environment in {"qa", "production"} and any(
        urlsplit(origin).scheme == "https" for origin in policy.allowed_origins
    ) and not config.effective_secure_cookie:
        raise BrowserTransportPolicyError("HTTPS_BROWSER_ORIGIN_REQUIRES_SECURE_COOKIE")


def build_runtime_security_contract(settings: Any, environ: Mapping[str, str] | None = None) -> tuple[BrowserTransportPolicy, Any]:
    """Build the exact browser policy and Base runtime configuration used at startup."""

    values = os.environ if environ is None else environ
    policy = BrowserTransportPolicy.from_environment(values)
    config = build_runtime_config(settings, values)
    issues = config.validate_environment(values)
    if issues:
        raise BrowserTransportPolicyError(_base_issue_code(issues[0]))
    _validate_browser_cookie_policy(policy, config)
    return policy, config


def security_preflight(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Return bounded, secret-safe EPHI/Base runtime security facts."""

    values = os.environ if environ is None else environ
    environment = str(values.get("EPHI_ENV", "development")).strip().lower()
    normalized_origins, origin_errors = _origin_observations(values)
    reasons = list(origin_errors)
    policy = None
    try:
        policy = BrowserTransportPolicy.from_environment(values)
    except BrowserTransportPolicyError as exc:
        reasons.append(str(exc))

    from .config import RuntimeSettings

    settings = None
    try:
        settings = RuntimeSettings.from_environment(values)
    except (TypeError, ValueError):
        reasons.append("INVALID_RUNTIME_SETTINGS")

    config = None
    runtime_dependency_available = True
    if settings is not None:
        try:
            config = build_runtime_config(settings, values)
        except BrowserTransportPolicyError as exc:
            reasons.append(str(exc))
        except ModuleNotFoundError as exc:
            if exc.name != "nicegui_base":
                raise
            runtime_dependency_available = False
            try:
                config = _RuntimeConfigProjection(_runtime_config_inputs(settings, values))
            except BrowserTransportPolicyError as projection_error:
                reasons.append(str(projection_error))
        except (TypeError, ValueError):
            reasons.append("INVALID_RUNTIME_CONFIGURATION")
    if config is not None:
        if isinstance(config, _RuntimeConfigProjection):
            issues = config.validate_environment(values, settings=settings)
        else:
            issues = config.validate_environment(values)
        reasons.extend(_base_issue_code(issue) for issue in issues)
    if policy is not None and config is not None:
        try:
            _validate_browser_cookie_policy(policy, config)
        except BrowserTransportPolicyError as exc:
            reasons.append(str(exc))

    storage_secret_present = bool(values.get("NICEGUI_BASE_STORAGE_SECRET"))
    if config is not None:
        effective_secure_cookie = config.effective_secure_cookie
    else:
        try:
            secure_override = _parse_optional_bool(values.get("NICEGUI_BASE_SECURE_SESSION_COOKIE"))
            effective_secure_cookie = secure_override if secure_override is not None else environment == "production"
        except BrowserTransportPolicyError:
            effective_secure_cookie = None
    same_site = str(values.get("NICEGUI_BASE_SAME_SITE", "strict")).strip().lower()
    try:
        proxy_enabled = _parse_bool(values.get("NICEGUI_BASE_PROXY_ENABLED"), default=False)
    except BrowserTransportPolicyError:
        proxy_enabled = False
    try:
        trusted = _trusted_proxies(values)
    except BrowserTransportPolicyError:
        trusted = ()
    try:
        root_path = normalize_root_path(values.get("NICEGUI_BASE_ROOT_PATH", ""))
    except BrowserTransportPolicyError:
        root_path = ""
    try:
        expected_replicas = int(values.get("NICEGUI_BASE_EXPECTED_REPLICAS", "1"))
        if expected_replicas < 1:
            expected_replicas = 0
    except (TypeError, ValueError):
        expected_replicas = 0
    shared_storage = bool(values.get("NICEGUI_REDIS_URL"))
    session_affinity = values.get("NICEGUI_BASE_SESSION_AFFINITY_CONFIRMED", "").lower() in _TRUE_VALUES
    if expected_replicas > 1 and not shared_storage:
        reasons.append("MULTI_REPLICA_WITHOUT_SHARED_STORAGE")
    if expected_replicas > 1 and not session_affinity:
        reasons.append("MULTI_REPLICA_WITHOUT_SESSION_AFFINITY_CONFIRMATION")
    try:
        diagnostics_enabled = _parse_bool(values.get("NICEGUI_BASE_DIAGNOSTICS_ENABLED"), default=False)
    except BrowserTransportPolicyError:
        diagnostics_enabled = False
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
        "runtime_dependency_available": runtime_dependency_available,
        "qa_http_insecure_cookie_policy": "Only an explicitly HTTP QA origin may use an insecure cookie for isolated qualification; target TLS/ingress remains NOT_ESTABLISHED.",
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
    "build_runtime_security_contract",
    "install_browser_transport_stack",
    "normalize_browser_origin",
    "normalize_root_path",
    "require_security_preflight",
    "security_preflight",
]
