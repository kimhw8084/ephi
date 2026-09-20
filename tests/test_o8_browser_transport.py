"""CHG-152/O8.2 exact-origin and secret-safe runtime boundary tests."""

import asyncio
import json
import unittest

from ephi.transport import (
    BrowserTransportMiddleware,
    BrowserTransportPolicy,
    BrowserTransportPolicyError,
    OriginDeniedError,
    normalize_browser_origin,
    normalize_root_path,
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
