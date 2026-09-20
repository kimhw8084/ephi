"""CHG-152/O8.2 exact-origin and secret-safe runtime boundary tests."""

import asyncio
import importlib.util
import json
import unittest

from ephi.config import RuntimeSettings
from ephi.transport import (
    BrowserTransportMiddleware,
    BrowserTransportPolicy,
    BrowserTransportPolicyError,
    OriginDeniedError,
    build_runtime_security_contract,
    install_browser_transport_stack,
    normalize_browser_origin,
    normalize_root_path,
    require_security_preflight,
    security_preflight,
)


def _scope(scope_type: str, *, method: str = "GET", origin: str | None = None, path: str = "/") -> dict:
    headers = [] if origin is None else [(b"origin", origin.encode("latin-1"))]
    return {"type": scope_type, "method": method, "path": path, "headers": headers}


class OriginNormalizationTests(unittest.TestCase):
    def test_exact_origin_is_structurally_normalized(self):
        self.assertEqual(normalize_browser_origin("HTTP://127.0.0.1"), "http://127.0.0.1:80")
        self.assertEqual(normalize_browser_origin("https://Example.test:443"), "https://example.test:443")
        self.assertEqual(normalize_browser_origin("http://[::1]:8080"), "http://[::1]:8080")

    def test_origin_suffix_wrong_scheme_and_wrong_port_are_not_equal(self):
        allowed = normalize_browser_origin("http://127.0.0.1:8080")
        self.assertNotEqual(normalize_browser_origin("http://127.0.0.1.attacker.test:8080"), allowed)
        self.assertNotEqual(normalize_browser_origin("https://127.0.0.1:8080"), allowed)
        self.assertNotEqual(normalize_browser_origin("http://127.0.0.1:8081"), allowed)

    def test_null_wildcard_malformed_and_path_bearing_values_fail(self):
        for value in ("null", "*", "http://*.example.test", "http://example.test/path", "http://example.test?x=1", "http://example.test#x", "http://example.test:bad", "http://user@example.test"):
            with self.subTest(value=value):
                with self.assertRaises(BrowserTransportPolicyError):
                    normalize_browser_origin(value)

    def test_root_path_is_normalized_without_accepting_ambiguous_paths(self):
        self.assertEqual(normalize_root_path("/ephi/"), "/ephi")
        self.assertEqual(normalize_root_path("/"), "")
        for value in ("ephi", "/ephi?x=1", "/ephi#x", "/ephi//nested"):
            with self.subTest(value=value):
                with self.assertRaises(BrowserTransportPolicyError):
                    normalize_root_path(value)


class BrowserTransportPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = BrowserTransportPolicy("test", ("http://127.0.0.1:8080",))

    def test_exact_allowed_origin_is_accepted(self):
        self.assertEqual(self.policy.normalize_request_origin("http://127.0.0.1:8080"), "http://127.0.0.1:8080")

    def test_attacker_suffix_wrong_scheme_wrong_port_null_and_missing_are_rejected(self):
        for value in ("http://127.0.0.1.attacker.test:8080", "https://127.0.0.1:8080", "http://127.0.0.1:8081", "null", None):
            with self.subTest(value=value):
                with self.assertRaises(OriginDeniedError):
                    self.policy.normalize_request_origin(value)

    def test_wildcard_and_empty_allowlist_are_rejected(self):
        with self.assertRaises(BrowserTransportPolicyError):
            BrowserTransportPolicy("test", ("*",))
        with self.assertRaises(BrowserTransportPolicyError):
            BrowserTransportPolicy.from_environment({"EPHI_ENV": "production", "EPHI_ALLOWED_BROWSER_ORIGINS": ""})

    def test_development_requires_explicit_loopback_without_changing_production(self):
        policy = BrowserTransportPolicy.from_environment({"EPHI_ENV": "development", "EPHI_ALLOWED_BROWSER_ORIGINS": "http://localhost:8080"})
        self.assertEqual(policy.allowed_origins, ("http://localhost:8080",))
        with self.assertRaises(BrowserTransportPolicyError):
            BrowserTransportPolicy.from_environment({"EPHI_ENV": "development", "EPHI_ALLOWED_BROWSER_ORIGINS": "https://example.test"})


class BrowserTransportMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.policy = BrowserTransportPolicy("test", ("http://127.0.0.1:8080",))

        async def downstream(scope, receive, send):
            self.calls.append(scope)
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})
            else:
                await send({"type": "websocket.accept"})

        self.downstream = downstream

    @staticmethod
    def invoke(app, scope):
        messages = []

        async def send(message):
            messages.append(message)

        asyncio.run(app(scope, lambda: None, send))
        return messages

    def test_disallowed_websocket_is_rejected_before_downstream_client_creation(self):
        messages = self.invoke(BrowserTransportMiddleware(self.downstream, self.policy), _scope("websocket", origin="http://127.0.0.1.attacker.test:8080"))
        self.assertEqual(messages[0]["type"], "websocket.close")
        self.assertEqual(messages[0]["code"], 1008)
        self.assertEqual(self.calls, [])

    def test_allowed_websocket_reaches_downstream(self):
        messages = self.invoke(BrowserTransportMiddleware(self.downstream, self.policy), _scope("websocket", origin="http://127.0.0.1:8080", path="/_nicegui_ws/socket.io"))
        self.assertEqual(messages, [{"type": "websocket.accept"}])
        self.assertEqual(len(self.calls), 1)

    def test_disallowed_state_changing_http_is_rejected_before_mutation(self):
        messages = self.invoke(BrowserTransportMiddleware(self.downstream, self.policy), _scope("http", method="POST", origin="http://127.0.0.1.attacker.test:8080"))
        self.assertEqual(messages[0]["status"], 403)
        self.assertEqual(self.calls, [])

    def test_missing_state_changing_origin_is_rejected_but_safe_get_is_usable(self):
        denied = self.invoke(BrowserTransportMiddleware(self.downstream, self.policy), _scope("http", method="POST"))
        allowed = self.invoke(BrowserTransportMiddleware(self.downstream, self.policy), _scope("http", method="GET"))
        self.assertEqual(denied[0]["status"], 403)
        self.assertEqual(allowed[0]["status"], 200)
        self.assertEqual(len(self.calls), 1)

    def test_origin_gate_does_not_replace_current_authorization(self):
        class AuthorizationDenied(Exception):
            pass

        async def protected(scope, receive, send):
            raise AuthorizationDenied("current authorization denied")

        with self.assertRaises(AuthorizationDenied):
            self.invoke(BrowserTransportMiddleware(protected, self.policy), _scope("http", method="POST", origin="http://127.0.0.1:8080"))


_BASE_STACK_TEST_AVAILABLE = all(importlib.util.find_spec(name) for name in ("nicegui_base", "fastapi", "httpx"))


@unittest.skipUnless(_BASE_STACK_TEST_AVAILABLE, "pinned Base runtime stack is installed only in integration qualification")
class ComposedMiddlewareStackTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from nicegui_base import NiceGUIRuntimeAdapter

        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.calls = []

        @self.app.get("/")
        async def page():
            self.calls.append("page")
            return {"status": "ok"}

        @self.app.post("/mutate")
        async def mutate():
            self.calls.append("mutate")
            return {"status": "mutated"}

        @self.app.websocket("/socket")
        async def socket(websocket):
            self.calls.append("websocket")
            await websocket.accept()
            await websocket.close()

        values = {
            "EPHI_ENV": "test",
            "EPHI_HOST": "127.0.0.1",
            "EPHI_ALLOWED_BROWSER_ORIGINS": "http://127.0.0.1:8080",
            "NICEGUI_BASE_STORAGE_SECRET": "secret-value-never-recorded",
        }
        settings = RuntimeSettings.from_environment(values)
        policy, config = build_runtime_security_contract(settings, values)
        self.policy = policy
        self.config = config
        self.adapter = NiceGUIRuntimeAdapter(config)
        install_browser_transport_stack(self.app, self.adapter, policy)

    def test_actual_composed_stack_keeps_base_outer_and_gate_before_route(self):
        names = [middleware.cls.__name__ for middleware in self.app.user_middleware]
        self.assertEqual(names, ["CorrelationIdMiddleware", "SecurityHeadersMiddleware", "BrowserTransportMiddleware"])

        async def exercise():
            import httpx

            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                denied = await client.post("/mutate", headers={"Origin": "http://127.0.0.1.attacker.test:8080"})
                allowed = await client.get("/", headers={"Origin": "http://127.0.0.1:8080"})
            return denied, allowed

        denied, allowed = asyncio.run(exercise())
        required = {
            "x-content-type-options",
            "referrer-policy",
            "x-frame-options",
            "permissions-policy",
        }
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(required.issubset(denied.headers))
        self.assertNotIn("server", denied.headers)
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(required.issubset(allowed.headers))
        self.assertNotIn("server", allowed.headers)
        self.assertEqual(self.calls, ["page"])

    def test_actual_composed_stack_rejects_websocket_before_route(self):
        messages = self.invoke(self.app, _scope("websocket", origin="http://127.0.0.1.attacker.test:8080", path="/socket"))
        self.assertEqual(messages[0]["type"], "websocket.close")
        self.assertEqual(messages[0]["code"], 1008)
        self.assertEqual(self.calls, [])

    def test_future_base_identity_remains_outer_than_correlation_security_and_gate(self):
        from fastapi import FastAPI
        from nicegui_base import NiceGUIRuntimeAdapter

        class FutureCompanyIdentityAdapter:
            async def authenticate(self, headers, client_host):
                return None

        app = FastAPI()
        adapter = NiceGUIRuntimeAdapter(self.config, auth_adapter=FutureCompanyIdentityAdapter())
        install_browser_transport_stack(app, adapter, self.policy)
        names = [middleware.cls.__name__ for middleware in app.user_middleware]
        self.assertEqual(names, ["IdentityMiddleware", "CorrelationIdMiddleware", "SecurityHeadersMiddleware", "BrowserTransportMiddleware"])

    @staticmethod
    def invoke(app, scope):
        messages = []

        async def send(message):
            messages.append(message)

        asyncio.run(app(scope, lambda: None, send))
        return messages


class SecurityPreflightTests(unittest.TestCase):
    def test_pass_report_is_bounded_and_normalized(self):
        report = security_preflight(
            {
                "EPHI_ENV": "test",
                "EPHI_ALLOWED_BROWSER_ORIGINS": "HTTP://127.0.0.1:8080",
                "NICEGUI_BASE_STORAGE_SECRET": "secret-value-never-recorded",
                "NICEGUI_BASE_ROOT_PATH": "/ephi/",
            }
        )
        encoded = json.dumps(report, sort_keys=True)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["normalized_non_secret_origins"], ["http://127.0.0.1:8080"])
        self.assertFalse(report["effective_secure_cookie"])
        self.assertEqual(report["normalized_root_path"], "/ephi")
        self.assertNotIn("secret-value-never-recorded", encoded)
        self.assertFalse(report["company_identity_established"])
        self.assertFalse(report["target_tls_ingress_qualification_established"])

    def test_missing_secret_and_empty_production_allowlist_are_blocked_without_secret_evidence(self):
        report = security_preflight({"EPHI_ENV": "production"})
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("BROWSER_ORIGIN_ALLOWLIST_REQUIRED", report["reason_codes"])
        self.assertIn("MISSING_STORAGE_SECRET", report["reason_codes"])
        self.assertNotIn("secret-value", json.dumps(report, sort_keys=True).lower())

    def test_production_cookie_policy_and_samesite_none_validation(self):
        secure = security_preflight(
            {
                "EPHI_ENV": "production",
                "EPHI_ALLOWED_BROWSER_ORIGINS": "https://ephi.example.test",
                "NICEGUI_BASE_STORAGE_SECRET": "present",
            }
        )
        insecure_none = security_preflight(
            {
                "EPHI_ENV": "production",
                "EPHI_ALLOWED_BROWSER_ORIGINS": "https://ephi.example.test",
                "NICEGUI_BASE_STORAGE_SECRET": "present",
                "NICEGUI_BASE_SECURE_SESSION_COOKIE": "false",
                "NICEGUI_BASE_SAME_SITE": "none",
            }
        )
        self.assertTrue(secure["effective_secure_cookie"])
        self.assertEqual(secure["same_site"], "strict")
        self.assertIn("INSECURE_SAMESITE_NONE", insecure_none["reason_codes"])

    def test_preflight_and_startup_contract_are_equivalent(self):
        common = {
            "EPHI_ENV": "test",
            "EPHI_ALLOWED_BROWSER_ORIGINS": "http://127.0.0.1:8080",
            "NICEGUI_BASE_STORAGE_SECRET": "secret-value-never-recorded",
        }
        cases = [
            ("valid loopback", {}, True),
            ("non-loopback development", {"EPHI_ALLOWED_BROWSER_ORIGINS": "https://qa.example.test"}, False),
            ("duplicate origins", {"EPHI_ALLOWED_BROWSER_ORIGINS": "http://127.0.0.1:8080,HTTP://127.0.0.1:8080"}, False),
            ("invalid boolean", {"NICEGUI_BASE_DEBUG": "maybe"}, False),
            ("invalid expected replicas", {"NICEGUI_BASE_EXPECTED_REPLICAS": "many"}, False),
            ("invalid root path", {"NICEGUI_BASE_ROOT_PATH": "ephi"}, False),
        ]
        for label, overrides, expected_pass in cases:
            with self.subTest(label=label):
                values = {**common, **overrides}
                report = security_preflight(values)
                constructible = True
                try:
                    settings = RuntimeSettings.from_environment(values)
                    build_runtime_security_contract(settings, values)
                except (BrowserTransportPolicyError, TypeError, ValueError):
                    constructible = False
                self.assertEqual(report["status"] == "PASS", constructible)
                self.assertEqual(constructible, expected_pass)
                if constructible:
                    self.assertEqual(require_security_preflight(values)["status"], "PASS")
                else:
                    with self.assertRaises(RuntimeError):
                        require_security_preflight(values)

    def test_preflight_matrix_covers_qa_https_cookie_and_proxy_contracts(self):
        base = {
            "EPHI_ENV": "qa",
            "EPHI_ALLOWED_BROWSER_ORIGINS": "https://qa.example.test",
            "NICEGUI_BASE_STORAGE_SECRET": "secret-value-never-recorded",
        }
        matrix = [
            ("qa https default insecure cookie is blocked", {}, "BLOCKED", False),
            ("qa https explicit secure cookie", {"NICEGUI_BASE_SECURE_SESSION_COOKIE": "true"}, "PASS", True),
            ("qa https insecure override", {"NICEGUI_BASE_SECURE_SESSION_COOKIE": "false"}, "BLOCKED", False),
            ("qa http loopback", {"EPHI_ALLOWED_BROWSER_ORIGINS": "http://127.0.0.1:8080"}, "PASS", False),
            ("production https default", {"EPHI_ENV": "production", "EPHI_ALLOWED_BROWSER_ORIGINS": "https://ephi.example.test"}, "PASS", True),
            ("production insecure override", {"EPHI_ENV": "production", "EPHI_ALLOWED_BROWSER_ORIGINS": "https://ephi.example.test", "NICEGUI_BASE_SECURE_SESSION_COOKIE": "false"}, "BLOCKED", False),
            ("SameSite None insecure", {"NICEGUI_BASE_SAME_SITE": "none", "NICEGUI_BASE_SECURE_SESSION_COOKIE": "false"}, "BLOCKED", False),
            ("proxy explicit trust", {"EPHI_HOST": "0.0.0.0", "NICEGUI_BASE_PROXY_ENABLED": "true", "NICEGUI_BASE_TRUSTED_PROXIES": "127.0.0.1", "NICEGUI_BASE_ROOT_PATH": "/ephi", "NICEGUI_BASE_SECURE_SESSION_COOKIE": "true"}, "PASS", True),
            ("proxy empty trust", {"EPHI_HOST": "0.0.0.0", "NICEGUI_BASE_PROXY_ENABLED": "true", "NICEGUI_BASE_TRUSTED_PROXIES": ""}, "BLOCKED", False),
            ("proxy wildcard trust", {"EPHI_HOST": "0.0.0.0", "NICEGUI_BASE_PROXY_ENABLED": "true", "NICEGUI_BASE_TRUSTED_PROXIES": "*"}, "BLOCKED", False),
            ("multi replica without substrate", {"NICEGUI_BASE_EXPECTED_REPLICAS": "2"}, "BLOCKED", False),
        ]
        for label, overrides, expected_status, expected_secure in matrix:
            with self.subTest(label=label):
                report = security_preflight({**base, **overrides})
                self.assertEqual(report["status"], expected_status)
                self.assertEqual(report["effective_secure_cookie"], expected_secure)
                self.assertNotIn("secret-value-never-recorded", json.dumps(report, sort_keys=True))

    def test_proxy_and_multi_replica_contracts_are_reported(self):
        report = security_preflight(
            {
                "EPHI_ENV": "qa",
                "EPHI_ALLOWED_BROWSER_ORIGINS": "https://qa.example.test",
                "NICEGUI_BASE_STORAGE_SECRET": "present",
                "NICEGUI_BASE_PROXY_ENABLED": "true",
                "NICEGUI_BASE_TRUSTED_PROXIES": "*",
                "NICEGUI_BASE_EXPECTED_REPLICAS": "2",
            }
        )
        self.assertIn("WILDCARD_TRUSTED_PROXY_FORBIDDEN", report["reason_codes"])
        self.assertIn("MULTI_REPLICA_WITHOUT_SHARED_STORAGE", report["reason_codes"])
        self.assertIn("MULTI_REPLICA_WITHOUT_SESSION_AFFINITY_CONFIRMATION", report["reason_codes"])
        self.assertEqual(report["trusted_proxy_count"], 0)


if __name__ == "__main__":
    unittest.main()
