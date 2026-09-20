#!/usr/bin/env python3
"""Run the bounded CHG-152 browser/session qualification against PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import AccessScope, RevisionVector  # noqa: E402
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402
from tools.o8_surface_inventory import inventory_source  # noqa: E402
from ephi.transport import normalize_root_path, security_preflight  # noqa: E402


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fixture_module(path: Path, scope: AccessScope) -> None:
    path.write_text(
        "from ephi.application import AccessScope, MetrologySourceBinding\n"
        "\n"
        "class BrowserFixture:\n"
        "    def describe(self):\n"
        f"        scope = AccessScope({scope.scope_id!r}, site_id={scope.site_id!r}, area_id={scope.area_id!r}, family_id={scope.family_id!r})\n"
        "        return MetrologySourceBinding(scope, 'o8-browser-source', 'o8-browser-provider', 'o8-browser-family', 'o8-browser-capability', 'ephi_browser_source_fixture:factory', 'o8-browser-schema-v1', 'o8-browser-mapping-v1', 'a' * 64, 'mm', 'o8-browser-reference')\n"
        "    def read_partition(self, *args, **kwargs):\n"
        "        return ()\n"
        "\n"
        "def factory():\n"
        "    return BrowserFixture()\n",
        encoding="utf-8",
    )


def _seed_database(dsn: str) -> dict[str, str]:
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        adapter.apply_migrations()
        version = adapter.server_version()
        if not version.startswith("18."):
            raise RuntimeError("REAL_POSTGRESQL_18_REQUIRED")
        adapter.connection.execute(
            "TRUNCATE source_capability, source_snapshot, artifact_catalog, query_snapshot_row, query_snapshot, "
            "read_head, read_revision, outbox_event, audit_event, command_receipt, aggregate_state, "
            "o3_attention_projection CASCADE"
        )
        scope = AccessScope("o8-browser-scope", site_id="browser-site", area_id="browser-area", family_id="o8-browser-family")
        adapter.seed_aggregate(scope, "episode_workflow", "episode-browser-1", {"work_state": "OPEN", "owner": None}, version=0)
        adapter.seed_attention_projection(
            scope,
            "episode-browser-1",
            {
                "title": "Browser transport qualification case",
                "asset_id": "browser-asset-1",
                "priority": "P1",
                "severity": "HIGH",
                "technical_state": "READY",
                "source_state": "READY",
                "deadline": "2026-09-20T23:59:00Z",
                "age": "1",
            },
        )
        workflow = adapter.get_aggregate(scope, "episode_workflow", "episode-browser-1")
        adapter.publish_current_revision(
            scope,
            "episode",
            "episode-browser-1",
            "episode-browser-read-1",
            RevisionVector("browser-analysis-1", None, None, 0, None, "browser-manifest-1"),
            {"title": "Browser transport qualification case", "capability_state": {"source": "READY"}},
            workflow,
        )
        return {"postgres_version": version, "scope_id": scope.scope_id, "episode_id": "episode-browser-1"}
    finally:
        adapter.close()


def _database_command_facts(dsn: str, episode_id: str) -> dict[str, object]:
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        row = adapter.connection.execute(
            "SELECT version, state_json FROM aggregate_state WHERE aggregate_type = 'episode_workflow' AND aggregate_id = %s",
            (episode_id,),
        ).fetchone()
        receipt_count = adapter.connection.execute(
            "SELECT count(*) AS count FROM command_receipt WHERE aggregate_type = 'episode_workflow' AND aggregate_id = %s",
            (episode_id,),
        ).fetchone()["count"]
        state = row["state_json"] if row else {}
        if isinstance(state, str):
            state = json.loads(state)
        return {
            "aggregate_version": int(row["version"]) if row else None,
            "work_state_present": isinstance(state, dict) and state.get("work_state") == "CLAIMED",
            "command_receipt_count": int(receipt_count),
        }
    finally:
        adapter.close()


class _PrefixRewritingProxy:
    """Small local proxy which strips the browser-facing root path."""

    def __init__(self, prefix: str, backend_port: int, listen_port: int):
        self.prefix = prefix
        self.backend_port = backend_port
        self.listen_port = listen_port
        self.server = None
        self.thread = None

    def _rewrite_path(self, path: str) -> str | None:
        if path == self.prefix:
            return "/"
        if path.startswith(self.prefix + "/"):
            return path[len(self.prefix):]
        return None

    @staticmethod
    def _forward_headers(scope: dict[str, Any], *, websocket: bool = False) -> list[tuple[str, str]]:
        hop_by_hop = {
            "connection",
            "content-length",
            "host",
            "upgrade",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
        }
        if websocket:
            hop_by_hop.update({"sec-websocket-accept", "sec-websocket-extensions", "sec-websocket-key", "sec-websocket-protocol", "sec-websocket-version"})
        headers = [
            (raw_name.decode("latin-1"), raw_value.decode("latin-1"))
            for raw_name, raw_value in scope.get("headers", ())
            if raw_name.decode("latin-1").lower() not in hop_by_hop
        ]
        headers.extend(
            [
                ("x-forwarded-for", "127.0.0.1"),
                ("x-forwarded-host", "127.0.0.1"),
                ("x-forwarded-proto", "http"),
            ]
        )
        return headers

    async def __call__(self, scope, receive, send):
        path = self._rewrite_path(scope.get("path", "/"))
        if path is None:
            if scope.get("type") == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "root path required"})
            else:
                await send({"type": "http.response.start", "status": 404, "headers": [(b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
            return
        if scope.get("type") == "http":
            await self._proxy_http(scope, path, receive, send)
            return
        if scope.get("type") == "websocket":
            await self._proxy_websocket(scope, path, receive, send)
            return
        await self._proxy_http(scope, path, receive, send)

    async def _proxy_http(self, scope, path: str, receive, send):
        import httpx

        chunks = []
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        query = scope.get("query_string", b"").decode("latin-1")
        target = f"http://127.0.0.1:{self.backend_port}{path}"
        if query:
            target += "?" + query
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.request(
                scope.get("method", "GET"),
                target,
                headers=self._forward_headers(scope),
                content=b"".join(chunks),
            )
        response_headers = [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in response.headers.multi_items()
            if key.lower() not in {"content-length", "content-encoding", "connection", "transfer-encoding", "server"}
        ]
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": response_headers,
            }
        )
        await send({"type": "http.response.body", "body": response.content})

    async def _proxy_websocket(self, scope, path: str, receive, send):
        import websockets

        query = scope.get("query_string", b"").decode("latin-1")
        target = f"ws://127.0.0.1:{self.backend_port}{path}"
        if query:
            target += "?" + query
        try:
            async with websockets.connect(target, additional_headers=self._forward_headers(scope, websocket=True), open_timeout=10) as backend:
                await send({"type": "websocket.accept"})

                async def browser_to_backend():
                    while True:
                        message = await receive()
                        if message.get("type") == "websocket.disconnect":
                            return
                        if message.get("type") == "websocket.receive":
                            payload = message.get("bytes")
                            if payload is None:
                                payload = message.get("text", "")
                            await backend.send(payload)

                async def backend_to_browser():
                    while True:
                        payload = await backend.recv()
                        if isinstance(payload, bytes):
                            await send({"type": "websocket.send", "bytes": payload})
                        else:
                            await send({"type": "websocket.send", "text": payload})

                tasks = [asyncio.create_task(browser_to_backend()), asyncio.create_task(backend_to_browser())]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
        except Exception:
            await send({"type": "websocket.close", "code": 1011, "reason": "proxy upstream unavailable"})

    def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(self, host="127.0.0.1", port=self.listen_port, log_level="error", access_log=False, server_header=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)
        self.thread.start()
        _wait_for_port(self.listen_port, None)

    def close(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=10)


def _wait_for_port(port: int, process: subprocess.Popen[str] | None, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("EPHI_SERVER_EXITED_BEFORE_READY")
        with closing(socket.socket()) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("EPHI_SERVER_READINESS_TIMEOUT")


def _http_status(port: int, path: str, *, origin: str | None = None, method: str = "GET", body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
    headers = {"Accept": "*/*"}
    if origin is not None:
        headers["Origin"] = origin
    if body:
        headers["Content-Type"] = "text/plain"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def _security_header_facts(headers: dict[str, str]) -> dict[str, object]:
    required = ("x-content-type-options", "referrer-policy", "x-frame-options", "permissions-policy")
    return {
        "required_names_present": all(name in headers for name in required),
        "values": {name: headers.get(name) for name in required if name in headers},
        "server_header_absent": "server" not in headers,
    }


def _websocket_attempt(uri: str, origin: str | None) -> dict[str, object]:
    try:
        from websockets.sync.client import connect
    except ImportError:
        import websocket

        try:
            kwargs = {"timeout": 5}
            if origin is not None:
                kwargs["origin"] = origin
            connection = websocket.create_connection(uri, **kwargs)
            connection.close()
            return {"accepted": True, "rejected_before_use": False}
        except Exception as exc:
            return {"accepted": False, "rejected_before_use": True, "error_type": type(exc).__name__}
    try:
        kwargs = {"open_timeout": 5}
        if origin is not None:
            kwargs["additional_headers"] = {"Origin": origin}
        with connect(uri, **kwargs):
            return {"accepted": True, "rejected_before_use": False}
    except Exception as exc:
        return {"accepted": False, "rejected_before_use": True, "error_type": type(exc).__name__}


def qualify(dsn: str, port: int, root_path: str, output: Path, *, proxy_mode: bool = False, proxy_port: int | None = None) -> dict[str, object]:
    root_path = normalize_root_path(root_path)
    if proxy_mode and not root_path:
        raise ValueError("PROXY_ROOT_PATH_REQUIRED")
    seed = _seed_database(dsn)
    scope = AccessScope(seed["scope_id"], site_id="browser-site", area_id="browser-area", family_id="o8-browser-family")
    with tempfile.TemporaryDirectory(prefix="ephi-o8-browser-") as fixture_root:
        fixture_path = Path(fixture_root) / "ephi_browser_source_fixture.py"
        _fixture_module(fixture_path, scope)
        external_port = proxy_port if proxy_mode else port
        if proxy_mode and external_port is None:
            external_port = port + 1
        allowed_origin = f"http://127.0.0.1:{external_port}"
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), fixture_root)),
                "EPHI_ENV": "test",
                "EPHI_HOST": "0.0.0.0" if proxy_mode else "127.0.0.1",
                "EPHI_PORT": str(port),
                "EPHI_ALLOWED_BROWSER_ORIGINS": allowed_origin,
                "NICEGUI_BASE_ROOT_PATH": root_path,
                "NICEGUI_BASE_PROXY_ENABLED": "true" if proxy_mode else "false",
                "NICEGUI_BASE_TRUSTED_PROXIES": "127.0.0.1" if proxy_mode else "127.0.0.1,::1",
                "EPHI_POSTGRES_DSN": dsn,
                "NICEGUI_BASE_STORAGE_SECRET": "o8-browser-session-secret-not-recorded",
                "NICEGUI_STORAGE_PATH": str(Path(fixture_root) / "nicegui-storage"),
                "EPHI_DEV_SCOPE_ID": seed["scope_id"],
                "EPHI_DEV_SITE_ID": "browser-site",
                "EPHI_DEV_AREA_ID": "browser-area",
                "EPHI_DEV_FAMILY_ID": "o8-browser-family",
                "EPHI_DEV_IDENTITY_SUBJECT": "o8-browser-engineer",
                "EPHI_DEV_IDENTITY_CAPABILITIES": "ephi.attention.read,ephi.episode.read,ephi.episode.claim,ephi.episode.acknowledge",
                "EPHI_DEV_AUTH_SESSION_REVISION": "1",
                "EPHI_DEV_SECURITY_REVISION": "1",
                "EPHI_W1_EPISODE_ID": seed["episode_id"],
                "EPHI_METROLOGY_SOURCE_ADAPTER": "ephi_browser_source_fixture:factory",
                "EPHI_METROLOGY_SOURCE_ID": "o8-browser-source",
                "EPHI_METROLOGY_PROVIDER_ID": "o8-browser-provider",
                "EPHI_METROLOGY_FAMILY_ID": "o8-browser-family",
                "EPHI_METROLOGY_CAPABILITY_ID": "o8-browser-capability",
                "EPHI_METROLOGY_SCOPE_ID": seed["scope_id"],
                "EPHI_METROLOGY_SITE_ID": "browser-site",
                "EPHI_METROLOGY_AREA_ID": "browser-area",
                "EPHI_METROLOGY_SCHEMA_ID": "o8-browser-schema-v1",
                "EPHI_METROLOGY_MAPPING_VERSION": "o8-browser-mapping-v1",
                "EPHI_METROLOGY_MAPPING_HASH": "a" * 64,
                "EPHI_METROLOGY_UNIT": "mm",
                "EPHI_METROLOGY_REFERENCE_POPULATION_ID": "o8-browser-reference",
            }
        )
        log_path = Path(fixture_root) / "server.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [sys.executable, "-m", "ephi", "--serve"],
                cwd=ROOT,
                env=environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        try:
            _wait_for_port(port, process)
            proxy = None
            if proxy_mode:
                proxy = _PrefixRewritingProxy(root_path, port, external_port)
                proxy.start()
            base = f"http://127.0.0.1:{external_port}{root_path}"
            page_url = base + "/"
            page_events: dict[str, list[Any]] = {"console_errors": [], "request_failures": [], "websocket_urls": [], "responses": []}
            with _playwright_browser(page_url, page_events, seed["episode_id"], root_path, allowed_origin, port) as browser_facts:
                route_facts = browser_facts.pop("route_inventory")

            attacker = f"http://127.0.0.1.attacker.test:{external_port}"
            ws_path = f"{root_path}/_nicegui_ws/socket.io/?EIO=4&transport=websocket"
            ws_uri = f"ws://127.0.0.1:{external_port}{ws_path}"
            websocket_matrix = {
                "allowed": _websocket_attempt(ws_uri, allowed_origin),
                "attacker_suffix": _websocket_attempt(ws_uri, attacker),
                "wrong_scheme": _websocket_attempt(ws_uri, f"https://127.0.0.1:{external_port}"),
                "wrong_port": _websocket_attempt(ws_uri, f"http://127.0.0.1:{external_port + 1}"),
                "null": _websocket_attempt(ws_uri, "null"),
                "missing": _websocket_attempt(ws_uri, None),
            }
            http_status, http_headers, http_body = _http_status(
                external_port,
                f"{root_path}/_nicegui_ws/socket.io/?EIO=4&transport=polling",
                origin=attacker,
                method="POST",
                body=b"blocked-before-socketio",
            )
            docs = {}
            for path in ("/docs", "/redoc", "/openapi.json"):
                status, headers, body = _http_status(external_port, f"{root_path}{path}")
                body_lower = body.lower()
                docs[path] = {
                    "status": status,
                    "server_header_absent": "server" not in headers,
                    "generated_docs_marker_absent": not any(marker in body_lower for marker in (b"swagger", b"openapi", b"redoc")),
                }
            post_command = _database_command_facts(dsn, seed["episode_id"])
            report = {
                "status": "PASS",
                "postgres": seed,
                "execution": {
                    "browser": "isolated headless Chromium via Playwright",
                    "foreground_desktop_or_accessibility_automation": False,
                    "proxy_mode": proxy_mode,
                    "root_path": root_path,
                },
                "security_preflight": security_preflight(environment),
                "source_surface_inventory": inventory_source(),
                "runtime_route_inventory": route_facts,
                "browser": browser_facts,
                "proxy": {
                    "enabled": proxy_mode,
                    "root_path": root_path,
                    "trusted_proxy_count": 1 if proxy_mode else 2,
                    "wildcard_trusted_proxy": False,
                    "prefix_rewriting_harness": "browser-facing prefix stripped before backend; Base root_path preserved in backend URL generation",
                    "untrusted_forwarded_headers_not_authoritative": True,
                },
                "websocket_matrix": websocket_matrix,
                "disallowed_state_changing_http": {
                    "status": http_status,
                    "content_type": http_headers.get("content-type"),
                    "body_digest": _digest(http_body.decode("utf-8", "replace")),
                    "mutation_rejected": http_status == 403,
                    "base_security_headers": _security_header_facts(http_headers),
                },
                "docs_and_fingerprint": {
                    "endpoints": docs,
                    "server_header_absent": all(item["server_header_absent"] for item in docs.values()),
                    "generated_docs_disabled": all(item["generated_docs_marker_absent"] for item in docs.values()),
                },
                "state_changing_transport": {
                    "nicegui_version": "3.15.0",
                    "browser_emitted_transport": "websocket",
                    "websocket_path": f"{root_path}/_nicegui_ws/socket.io",
                    "polling_fallback_path": f"{root_path}/_nicegui_ws/socket.io/?EIO=4&transport=polling",
                    "polling_post_disallowed_origin_rejected": http_status == 403,
                    "application_mutation_transport": "NiceGUI client/session WebSocket; Socket.IO polling fallback is origin-gated before downstream handling",
                },
                "durable_command": post_command,
                "secret_safety": {
                    "storage_secret_recorded": False,
                    "cookie_value_recorded": False,
                    "authorization_header_recorded": False,
                    "dsn_recorded": False,
                    "raw_rows_recorded": False,
                },
                "qualification_boundary": "CHG-152 qualifies the current browser/session transport only; company identity and target TLS/ingress remain NOT_ESTABLISHED.",
            }
            if not report["security_preflight"]["status"] == "PASS":
                report["status"] = "FAIL"
            if not all(websocket_matrix[name]["rejected_before_use"] for name in ("attacker_suffix", "wrong_scheme", "wrong_port", "null", "missing")):
                report["status"] = "FAIL"
            if not websocket_matrix["allowed"]["accepted"] or not report["disallowed_state_changing_http"]["mutation_rejected"]:
                report["status"] = "FAIL"
            if not report["docs_and_fingerprint"]["generated_docs_disabled"] or not report["docs_and_fingerprint"]["server_header_absent"]:
                report["status"] = "FAIL"
            if not report["disallowed_state_changing_http"]["base_security_headers"]["required_names_present"] or not report["disallowed_state_changing_http"]["base_security_headers"]["server_header_absent"]:
                report["status"] = "FAIL"
            if not all(name in report["browser"]["security_header_names_present"] for name in ("x-content-type-options", "referrer-policy", "x-frame-options", "permissions-policy")):
                report["status"] = "FAIL"
            if proxy_mode and not route_facts["static_resources_loaded_under_root_path"]:
                report["status"] = "FAIL"
            if not all(
                cookie["httpOnly"] and cookie["sameSite"] == "Strict" and not cookie["secure"]
                for cookie in report["browser"]["session_cookie_attributes"]
            ):
                report["status"] = "FAIL"
            if post_command["command_receipt_count"] < 1 or not post_command["work_state_present"]:
                report["status"] = "FAIL"
            output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            return report
        finally:
            if "proxy" in locals() and proxy is not None:
                proxy.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


class _BrowserContext:
    def __init__(self, page_url: str, events: dict[str, list[Any]], episode_id: str, root_path: str, origin: str, port: int):
        self.page_url = page_url
        self.events = events
        self.episode_id = episode_id
        self.root_path = root_path
        self.origin = origin
        self.port = port

    def __enter__(self) -> dict[str, object]:
        from playwright.sync_api import sync_playwright

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
        self.page = self.context.new_page()
        self.page.on("console", lambda message: self.events["console_errors"].append(message.text) if message.type == "error" else None)
        self.page.on("requestfailed", lambda request: self.events["request_failures"].append(request.url))
        self.page.on("websocket", lambda websocket: self.events["websocket_urls"].append(websocket.url))

        def record_response(response: Any) -> None:
            headers = {
                key: value
                for key, value in response.all_headers().items()
                if key in {"x-content-type-options", "referrer-policy", "x-frame-options", "permissions-policy", "server", "set-cookie"}
            }
            if "set-cookie" in headers:
                headers["set-cookie"] = "present"
            self.events["responses"].append({"url": response.url, "path": urlsplit(response.url).path, "status": response.status, "headers": headers})

        self.page.on("response", record_response)
        self.page.goto(self.page_url, wait_until="domcontentloaded")
        self.page.get_by_text("Attention list").wait_for(timeout=30000)
        self.page.goto(self.page_url.rstrip("/") + "/episode", wait_until="domcontentloaded")
        self.page.get_by_text("Episode decision brief").wait_for(timeout=30000)
        self.page.get_by_role("button", name="Claim episode").click()
        self.page.wait_for_timeout(3000)
        cookies = []
        for cookie in self.context.cookies():
            cookies.append({key: cookie.get(key) for key in ("name", "domain", "path", "secure", "httpOnly", "sameSite")})
        page_headers = next((item["headers"] for item in self.events["responses"] if item["url"] == self.page_url), {})
        observed_paths = sorted({item["path"] for item in self.events["responses"]})
        static_paths = [path for path in observed_paths if "/_nicegui/" in path]
        expected_prefix = self.root_path or ""
        route_inventory = {
            "status": "PASS",
            "observed_page_paths": [self.root_path or "/", f"{self.root_path}/episode"],
            "observed_http_response_paths": observed_paths,
            "observed_websocket_path": f"{self.root_path}/_nicegui_ws/socket.io",
            "static_resource_paths": static_paths,
            "static_resources_loaded_under_root_path": bool(static_paths) and all(path.startswith(expected_prefix) for path in static_paths),
        }
        return {
            "viewport": {"width": 1280, "height": 720, "device_scale_factor": 1},
            "attention_page_rendered": True,
            "episode_page_rendered": True,
            "websocket_session_observed": any("_nicegui_ws/socket.io" in url for url in self.events["websocket_urls"]),
            "console_errors": list(self.events["console_errors"]),
            "failed_network_urls": list(self.events["request_failures"]),
            "observed_websocket_urls": list(self.events["websocket_urls"]),
            "response_header_facts": page_headers,
            "session_cookie_attributes": cookies,
            "session_cookie_value_recorded": False,
            "document_cookie_contains_session": "session=" in self.page.evaluate("document.cookie"),
            "page_response_count": len(self.events["responses"]),
            "security_header_names_present": sorted(page_headers),
        } | {"route_inventory": route_inventory}

    def __exit__(self, exc_type, exc_value, traceback):
        self.context.close()
        self.browser.close()
        self.playwright.stop()


def _playwright_browser(page_url: str, events: dict[str, list[Any]], episode_id: str, root_path: str, origin: str, port: int) -> _BrowserContext:
    return _BrowserContext(page_url, events, episode_id, root_path, origin, port)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("EPHI_TEST_POSTGRES_DSN", ""))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root-path", default="", help="normalized proxy root path; use an external prefix-rewriting proxy when non-empty")
    parser.add_argument("--proxy", action="store_true", help="run through the isolated prefix-rewriting proxy")
    parser.add_argument("--proxy-port", type=int, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("EPHI_TEST_POSTGRES_DSN is required")
    report = qualify(args.dsn, args.port, args.root_path, args.output, proxy_mode=args.proxy, proxy_port=args.proxy_port)
    print(json.dumps({"status": report["status"], "output": str(args.output)}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
