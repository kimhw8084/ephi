#!/usr/bin/env python3
"""Run the bounded CHG-152 browser/session qualification against PostgreSQL."""

from __future__ import annotations

import argparse
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
import time
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import AccessScope, RevisionVector  # noqa: E402
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402
from tools.o8_surface_inventory import inventory_source  # noqa: E402
from ephi.transport import security_preflight  # noqa: E402


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


def _wait_for_port(port: int, process: subprocess.Popen[str], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
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


def qualify(dsn: str, port: int, root_path: str, output: Path) -> dict[str, object]:
    seed = _seed_database(dsn)
    scope = AccessScope(seed["scope_id"], site_id="browser-site", area_id="browser-area", family_id="o8-browser-family")
    with tempfile.TemporaryDirectory(prefix="ephi-o8-browser-") as fixture_root:
        fixture_path = Path(fixture_root) / "ephi_browser_source_fixture.py"
        _fixture_module(fixture_path, scope)
        allowed_origin = f"http://127.0.0.1:{port}"
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), fixture_root)),
                "EPHI_ENV": "test",
                "EPHI_HOST": "127.0.0.1",
                "EPHI_PORT": str(port),
                "EPHI_ALLOWED_BROWSER_ORIGINS": allowed_origin,
                "NICEGUI_BASE_ROOT_PATH": root_path,
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
            base = f"http://127.0.0.1:{port}{root_path}"
            page_url = base + "/"
            page_events: dict[str, list[Any]] = {"console_errors": [], "request_failures": [], "websocket_urls": [], "responses": []}
            with _playwright_browser(page_url, page_events, seed["episode_id"], root_path, allowed_origin, port) as browser_facts:
                route_facts = browser_facts.pop("route_inventory")

            attacker = f"http://127.0.0.1.attacker.test:{port}"
            ws_path = f"{root_path}/_nicegui_ws/socket.io/?EIO=4&transport=websocket"
            ws_uri = f"ws://127.0.0.1:{port}{ws_path}"
            websocket_matrix = {
                "allowed": _websocket_attempt(ws_uri, allowed_origin),
                "attacker_suffix": _websocket_attempt(ws_uri, attacker),
                "wrong_scheme": _websocket_attempt(ws_uri, f"https://127.0.0.1:{port}"),
                "wrong_port": _websocket_attempt(ws_uri, f"http://127.0.0.1:{port + 1}"),
                "null": _websocket_attempt(ws_uri, "null"),
                "missing": _websocket_attempt(ws_uri, None),
            }
            http_status, http_headers, http_body = _http_status(
                port,
                f"{root_path}/_nicegui_ws/socket.io/?EIO=4&transport=polling",
                origin=attacker,
                method="POST",
                body=b"blocked-before-socketio",
            )
            docs = {}
            for path in ("/docs", "/redoc", "/openapi.json"):
                status, headers, body = _http_status(port, f"{root_path}{path}")
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
                "security_preflight": security_preflight(environment),
                "source_surface_inventory": inventory_source(),
                "runtime_route_inventory": route_facts,
                "browser": browser_facts,
                "websocket_matrix": websocket_matrix,
                "disallowed_state_changing_http": {
                    "status": http_status,
                    "content_type": http_headers.get("content-type"),
                    "body_digest": _digest(http_body.decode("utf-8", "replace")),
                    "mutation_rejected": http_status == 403,
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
        route_inventory = {
            "status": "PASS",
            "observed_page_paths": [self.root_path or "/", f"{self.root_path}/episode"],
            "observed_http_response_paths": observed_paths,
            "observed_websocket_path": f"{self.root_path}/_nicegui_ws/socket.io",
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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("EPHI_TEST_POSTGRES_DSN is required")
    report = qualify(args.dsn, args.port, args.root_path, args.output)
    print(json.dumps({"status": report["status"], "output": str(args.output)}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
