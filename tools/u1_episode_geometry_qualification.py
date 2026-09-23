#!/usr/bin/env python3
"""Qualify the real U1 downstream Episode path and its governed geometry."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import secrets
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
from ephi.config import RuntimeSettings  # noqa: E402
from ephi.downstream import ProviderBinding, compose_downstream, load_provider_bundle  # noqa: E402
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402


BASE = "156e47b1e0cc3c77a2a3f0e2b65875eacc9aa0b4"
PREDECESSOR = "9df7b1f3f49758fba459479404ae74215736b81f"
ENTRYPOINT = "examples.synthetic_downstream.provider:build_bundle"
EPISODE_ID = "episode-u1-geometry"
TITLE = "Synthetic downstream geometry qualification"
FORBIDDEN_MARKERS = (
    "SYNTHETIC_DSN_MARKER",
    "SYNTHETIC_PASSWORD_MARKER",
    "SYNTHETIC_TOKEN_MARKER",
    "SYNTHETIC_COOKIE_MARKER",
    "SYNTHETIC_STORAGE_SECRET_MARKER",
    "SYNTHETIC_PRIVATE_ENDPOINT_MARKER",
    "SYNTHETIC_RAW_ROW_MARKER",
    "SYNTHETIC_PRIVATE_MAPPING_MARKER",
)


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _wait_for_process_or_port(port: int, process: subprocess.Popen[str], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("APPLICATION_STARTUP_EXITED")
        with closing(socket.socket()) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("APPLICATION_STARTUP_TIMEOUT")


def _seed_downstream_postgresql(dsn: str) -> dict[str, str]:
    """Seed the existing PostgreSQL authorities through their public test API."""

    bundle = load_provider_bundle(ENTRYPOINT)
    composition = compose_downstream(bundle, runtime_settings=RuntimeSettings(environment="test"))
    try:
        adapter = composition.adapter
        version = adapter.server_version()
        if not version.startswith("18."):
            raise RuntimeError("REAL_POSTGRESQL_18_REQUIRED")
        adapter.connection.execute(
            "TRUNCATE handoff_delivery_attempt, handoff_delivery_status, handoff_intent, decision_snapshot, "
            "source_capability, source_snapshot, artifact_catalog, o3_attention_projection, query_snapshot_row, "
            "query_snapshot, read_head, read_revision, applied_effect, job, outbox_event, audit_event, "
            "command_receipt, aggregate_state CASCADE"
        )
        scope = composition.scope_provider()
        adapter.seed_aggregate(
            scope,
            "episode_workflow",
            EPISODE_ID,
            {"work_state": "OPEN", "owner": None},
            version=0,
        )
        adapter.seed_attention_projection(
            scope,
            EPISODE_ID,
            {
                "title": TITLE,
                "asset_id": "synthetic-u1-asset",
                "priority": "P2",
                "severity": "MEDIUM",
                "technical_state": "READY",
                "source_state": "NOT_QUALIFIED",
                "deadline": None,
                "age": "1",
            },
        )
        workflow = adapter.get_aggregate(scope, "episode_workflow", EPISODE_ID)
        adapter.publish_current_revision(
            scope,
            "episode",
            EPISODE_ID,
            "u1-geometry-read-revision",
            RevisionVector("u1-geometry-analysis", None, None, 0, None, "u1-geometry-manifest"),
            {
                "title": TITLE,
                "analytical_revision": "u1-geometry-analysis",
                "capability_state": {"source": "NOT_QUALIFIED"},
            },
            workflow,
        )
        return {
            "postgres_version": version,
            "scope_id": scope.scope_id,
            "subject": composition.principal_provider().subject,
            "episode_id": EPISODE_ID,
            "workflow_state": str(workflow.state.get("work_state", "OPEN")),
        }
    finally:
        composition.close()


def _interactive_fact(page: Any, locator: Any) -> dict[str, object]:
    locator.scroll_into_view_if_needed(timeout=10000)
    return page.evaluate(
        """el => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            const x = rect.left + rect.width / 2;
            const y = rect.top + rect.height / 2;
            const hit = document.elementFromPoint(x, y);
            let clipped = false;
            for (let parent = el.parentElement; parent; parent = parent.parentElement) {
                const parentStyle = getComputedStyle(parent);
                const box = parent.getBoundingClientRect();
                if (/(hidden|clip|auto|scroll)/.test(parentStyle.overflowY) &&
                    (rect.top < box.top - 1 || rect.bottom > box.bottom + 1)) clipped = true;
                if (/(hidden|clip|auto|scroll)/.test(parentStyle.overflowX) &&
                    (rect.left < box.left - 1 || rect.right > box.right + 1)) clipped = true;
            }
            return {
                visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none',
                enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
                inside_viewport: rect.left >= 0 && rect.top >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight,
                center_hit: Boolean(hit && (hit === el || el.contains(hit))),
                clipped,
                bounds: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
            };
        }""",
        locator.element_handle(),
    )


def _server_environment(dsn: str, port: int, storage_path: Path, artifact_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("EPHI_DEV_") or key.startswith("EPHI_METROLOGY_"):
            environment.pop(key, None)
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), environment.get("PYTHONPATH", ""))),
            "EPHI_ENV": "test",
            "EPHI_HOST": "127.0.0.1",
            "EPHI_PORT": str(port),
            "EPHI_ALLOWED_BROWSER_ORIGINS": f"http://127.0.0.1:{port}",
            "EPHI_TEST_POSTGRES_DSN": dsn,
            "EPHI_DOWNSTREAM_ENTRYPOINT": ENTRYPOINT,
            "EPHI_SYNTHETIC_SUBJECT": "synthetic-engineer",
            "EPHI_SYNTHETIC_SCOPE_ID": "synthetic-u1-scope",
            "EPHI_SYNTHETIC_ARTIFACT_ROOT": str(artifact_root),
            "NICEGUI_BASE_ROOT_PATH": "",
            "NICEGUI_BASE_PROXY_ENABLED": "false",
            "NICEGUI_BASE_TRUSTED_PROXIES": "127.0.0.1,::1",
            "NICEGUI_BASE_STORAGE_SECRET": secrets.token_urlsafe(36),
            "NICEGUI_STORAGE_PATH": str(storage_path),
        }
    )
    return environment


def _positive_browser_path(dsn: str, seed: dict[str, str], artifact_dir: Path) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    port = _port()
    base = f"http://127.0.0.1:{port}"
    events: dict[str, list[dict[str, object]]] = {
        "console_errors": [], "page_errors": [], "request_failures": [], "http_errors": [], "websockets": []
    }
    with tempfile.TemporaryDirectory(prefix="ephi-u1-geometry-") as temporary:
        temp = Path(temporary)
        environment = _server_environment(dsn, port, temp / "nicegui-storage", temp / "artifacts")
        log_path = temp / "server.log"
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
            _wait_for_process_or_port(port, process)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    device_scale_factor=1,
                    reduced_motion="reduce",
                )
                page = context.new_page()
                page.on("console", lambda message: events["console_errors"].append(
                    {"type": message.type, "digest": _sha256(message.text)}
                ) if message.type == "error" else None)
                page.on("pageerror", lambda error: events["page_errors"].append(
                    {"type": type(error).__name__, "digest": _sha256(str(error))}
                ))
                page.on("requestfailed", lambda request: events["request_failures"].append(
                    {"path": urlsplit(request.url).path, "failure_type": "request_failed"}
                ))
                page.on("response", lambda response: events["http_errors"].append(
                    {"path": urlsplit(response.url).path, "status": response.status}
                ) if response.status >= 400 else None)
                page.on("websocket", lambda websocket: events["websockets"].append(
                    {"path": urlsplit(websocket.url).path}
                ))

                response = page.goto(base + "/", wait_until="domcontentloaded", timeout=30000)
                if response is None or response.status != 200:
                    raise RuntimeError("ATTENTION_HTTP_STATUS")
                page.get_by_role("heading", name="Attention", exact=True).wait_for(timeout=30000)
                page.get_by_text(seed["episode_id"], exact=True).first.wait_for(timeout=30000)
                artifact_dir.mkdir(parents=True, exist_ok=True)
                attention_screenshot = artifact_dir / "attention-1440x900.png"

                first_cell = page.locator(".cui-data-table .ag-center-cols-container .ag-row").first.locator(".ag-cell").first
                first_cell.focus()
                page.keyboard.press("Space")
                open_button = page.get_by_role("button", name="Open episode")
                open_button.wait_for(timeout=30000)
                page.screenshot(path=str(attention_screenshot), full_page=True)
                open_button.focus()
                page.keyboard.press("Enter")
                page.wait_for_url("**/episode", timeout=30000)
                page.get_by_role("heading", name="Episode investigation workspace", exact=True).wait_for(timeout=30000)
                page.locator(".ephi-o10-episode-surface dl").get_by_text("OPEN", exact=True).wait_for(timeout=30000)

                heading = page.locator('[data-cui-slot="header"] h1')
                surface = page.locator(".ephi-o10-episode-surface")
                description = surface.locator("dl")
                geometry = page.evaluate(
                    """() => {
                        const box = selector => {
                            const element = document.querySelector(selector);
                            if (!element) return null;
                            const rect = element.getBoundingClientRect();
                            return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
                        };
                        return {
                            viewport: {width: innerWidth, height: innerHeight},
                            document: {width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight},
                            main: box('main'),
                            analysis_workspace: box('[data-cui-pattern="analysis_workspace"]'),
                            header_slot: box('[data-cui-slot="header"]'),
                            primary_slot: box('[data-cui-slot="primary"]'),
                            episode_surface: box('.ephi-o10-episode-surface'),
                            description_list: box('.ephi-o10-episode-surface dl'),
                            heading_in_header_slot: Boolean(document.querySelector('[data-cui-slot="header"] h1.ephi-o10-page-heading')),
                            surface_in_primary_slot: Boolean(document.querySelector('[data-cui-slot="primary"] .ephi-o10-episode-surface')),
                        };
                    }"""
                )
                episode_screenshot = artifact_dir / "episode-open-1440x900.png"
                page.screenshot(path=str(episode_screenshot))
                action_button = page.get_by_role("button", name="Claim episode")
                return_button = page.get_by_role("button", name="Return to Attention")
                action_geometry = _interactive_fact(page, action_button)
                return_geometry = _interactive_fact(page, return_button)
                action_screenshot = artifact_dir / "episode-actions-1440x900.png"
                page.screenshot(path=str(action_screenshot))

                initial_focus = page.evaluate("""() => ({
                    tag: document.activeElement?.tagName || '',
                    is_episode_heading: document.activeElement?.matches('h1.ephi-o10-page-heading') || false,
                })""")
                subject_rendered = seed["subject"] in page.locator("body").inner_text()
                action_button.focus()
                page.keyboard.press("Enter")
                page.locator(".ephi-o10-episode-surface dl").get_by_text("CLAIMED", exact=True).wait_for(timeout=30000)
                acknowledge = page.get_by_role("button", name="Acknowledge episode")
                acknowledge.wait_for(timeout=30000)
                page.wait_for_function(
                    """() => {
                        const target = document.querySelector('[data-ephi-focus-target="primary-action"]');
                        return Boolean(target && document.activeElement === target && target.getBoundingClientRect().width > 0);
                    }""",
                    timeout=30000,
                )
                action_focus_after_claim = page.evaluate(
                    "document.activeElement?.getAttribute('aria-label') || document.activeElement?.innerText || ''"
                )
                return_button = page.get_by_role("button", name="Return to Attention")
                return_button.focus()
                page.keyboard.press("Enter")
                page.wait_for_url("**/", timeout=30000)
                page.get_by_role("heading", name="Attention", exact=True).wait_for(timeout=30000)
                page.wait_for_function(
                    """() => (document.activeElement?.innerText || '').trim() === 'Attention'""",
                    timeout=30000,
                )
                focus_after_return = page.evaluate(
                    "document.activeElement?.innerText?.trim() || document.activeElement?.getAttribute('aria-label') || ''"
                )
                episode_http = context.request.get(base + "/episode", timeout=30000)
                cookies = context.cookies()
                cookie_facts = [
                    {
                        "http_only": bool(cookie.get("httpOnly")),
                        "same_site": cookie.get("sameSite"),
                        "secure": bool(cookie.get("secure")),
                        "path": cookie.get("path"),
                    }
                    for cookie in cookies
                ]
                browser.close()

        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    geometry_pass = bool(
        geometry["document"]["height"] <= geometry["viewport"]["height"] * 3
        and geometry["episode_surface"]["width"] >= geometry["viewport"]["width"] * 0.45
        and geometry["episode_surface"]["height"] <= geometry["viewport"]["height"] * 2
        and geometry["description_list"]["width"] >= geometry["viewport"]["width"] / 3
        and geometry["heading_in_header_slot"]
        and geometry["surface_in_primary_slot"]
    )
    buttons_pass = all(
        fact["visible"] and fact["enabled"] and fact["inside_viewport"] and fact["center_hit"] and not fact["clipped"]
        for fact in (action_geometry, return_geometry)
    )
    return {
        "http": {"attention_status": response.status, "episode_status": episode_http.status},
        "provider_identity": {
            "subject": seed["subject"],
            "scope_id": seed["scope_id"],
            "episode_id": seed["episode_id"],
            "workflow_before_action": seed["workflow_state"],
            "workflow_after_claim": "CLAIMED",
            "subject_rendered": subject_rendered,
        },
        "browser": {
            "engine": "headless Chromium via Playwright",
            "viewport": {"width": 1440, "height": 900},
            "allowed_origin": base,
            "websocket_session_observed": any("/_nicegui_ws/socket.io" in item["path"] for item in events["websockets"]),
            "session_cookie_attributes": cookie_facts,
            "ordinary_keyboard_flow": {
                "attention_row_selected_by_space": True,
                "episode_opened_by_enter": True,
                "initial_focus_is_episode_heading": bool(initial_focus["is_episode_heading"]),
                "claim_changes_open_to_claimed": True,
                "claim_focus_target": action_focus_after_claim.strip(),
                "return_to_attention_by_enter": True,
                "focus_after_return": focus_after_return,
            },
            "events": events,
        },
        "geometry": {
            **geometry,
            "episode_surface_width_minimum": round(geometry["viewport"]["width"] * 0.45),
            "description_list_width_minimum": 480,
            "document_height_maximum": 2700,
            "episode_surface_height_maximum": 1800,
            "status": "PASS" if geometry_pass else "FAIL",
        },
        "primary_action": action_geometry,
        "return_to_attention": return_geometry,
        "actions_status": "PASS" if buttons_pass else "FAIL",
        "screenshots": {
            "attention": {"path": attention_screenshot.name, "sha256": _sha256(attention_screenshot.read_bytes()), "bytes": attention_screenshot.stat().st_size},
            "episode": {"path": episode_screenshot.name, "sha256": _sha256(episode_screenshot.read_bytes()), "bytes": episode_screenshot.stat().st_size},
            "episode_actions": {"path": action_screenshot.name, "sha256": _sha256(action_screenshot.read_bytes()), "bytes": action_screenshot.stat().st_size},
        },
    }


def _negative_startup_matrix() -> dict[str, object]:
    marker = "SYNTHETIC_DSN_MARKER_SYNTHETIC_PASSWORD_MARKER_SYNTHETIC_TOKEN_MARKER_SYNTHETIC_COOKIE_MARKER_SYNTHETIC_STORAGE_SECRET_MARKER_SYNTHETIC_PRIVATE_ENDPOINT_MARKER_SYNTHETIC_RAW_ROW_MARKER_SYNTHETIC_PRIVATE_MAPPING_MARKER"
    dsn_marker = f"postgresql://user:{marker}@private.invalid/private_db"
    cases: list[tuple[str, str | None]] = [
        ("missing_bundle_with_legacy_authorities", None),
        ("malformed_entrypoint", "bad:entrypoint:grammar"),
        ("nonexistent_provider", "ephi_u1_provider_does_not_exist:build_bundle"),
        ("provider_import_exception", "u1_startup_import_failure_provider:build_bundle"),
        ("provider_factory_exception", "u1_startup_failure_provider:factory_failure"),
        ("incompatible_abi", "u1_startup_failure_provider:incompatible_abi"),
        ("runtime_provider_exception", "u1_startup_failure_provider:runtime_failure"),
    ]
    results: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="ephi-u1-fail-closed-") as temporary:
        fixture_root = Path(temporary)
        module = fixture_root / "u1_startup_failure_provider.py"
        module.write_text(
            "from dataclasses import replace\n"
            "from examples.synthetic_downstream.provider import build_bundle\n"
            "from ephi.downstream import ProviderBinding\n"
            f"MARKER = {marker!r}\n"
            "def factory_failure():\n    raise RuntimeError(MARKER)\n"
            "def incompatible_abi():\n    return replace(build_bundle(), abi_version='9.0.0')\n"
            "class BrokenRuntime:\n"
            "    def __init__(self, metadata): self.capabilities = metadata\n"
            "    def open_postgresql(self): raise RuntimeError(MARKER)\n"
            "def runtime_failure():\n"
            "    bundle = build_bundle()\n"
            "    runtime = ProviderBinding(bundle.runtime.contract, BrokenRuntime(bundle.runtime.public_metadata), bundle.runtime.public_metadata)\n"
            "    return replace(bundle, runtime=runtime)\n",
            encoding="utf-8",
        )
        (fixture_root / "u1_startup_import_failure_provider.py").write_text(
            f"raise RuntimeError({marker!r})\n",
            encoding="utf-8",
        )
        for index, (name, entrypoint) in enumerate(cases):
            port = _port()
            env = dict(os.environ)
            for key in tuple(env):
                if key.startswith("EPHI_DEV_") or key.startswith("EPHI_METROLOGY_"):
                    env.pop(key, None)
            env.update(
                {
                    "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(fixture_root), env.get("PYTHONPATH", ""))),
                    "EPHI_ENV": "qa" if entrypoint is None else "test",
                    "EPHI_HOST": "127.0.0.1",
                    "EPHI_PORT": str(port),
                    "EPHI_ALLOWED_BROWSER_ORIGINS": f"http://127.0.0.1:{port}",
                    "EPHI_TEST_POSTGRES_DSN": dsn_marker,
                    "NICEGUI_BASE_ROOT_PATH": "",
                    "NICEGUI_BASE_PROXY_ENABLED": "false",
                    "NICEGUI_BASE_TRUSTED_PROXIES": "127.0.0.1,::1",
                    "NICEGUI_BASE_STORAGE_SECRET": f"SYNTHETIC_STORAGE_SECRET_MARKER_{index}",
                    "NICEGUI_STORAGE_PATH": str(fixture_root / f"storage-{index}"),
                    "EPHI_SYNTHETIC_ARTIFACT_ROOT": "/private/SYNTHETIC_PRIVATE_ENDPOINT_MARKER",
                }
            )
            if entrypoint is None:
                env.update(
                    {
                        "EPHI_DEV_IDENTITY_SUBJECT": marker,
                        "EPHI_DEV_IDENTITY_CAPABILITIES": "ephi.attention.read",
                        "EPHI_DEV_SCOPE_ID": marker,
                        "EPHI_METROLOGY_SOURCE_ADAPTER": "private.mapping:factory",
                    }
                )
                env.pop("EPHI_DOWNSTREAM_ENTRYPOINT", None)
            else:
                env["EPHI_DOWNSTREAM_ENTRYPOINT"] = entrypoint
            output_path = fixture_root / f"startup-{index}.log"
            with output_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [sys.executable, "-m", "ephi", "--serve"],
                    cwd=ROOT,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                try:
                    return_code = process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    return_code = None
            output = output_path.read_text(encoding="utf-8", errors="replace")
            results[name] = {
                "status": "PASS" if return_code not in (None, 0) else "FAIL",
                "process_exit_nonzero": return_code not in (None, 0),
                "marker_leak_absent": all(
                    token not in output
                    for token in (*FORBIDDEN_MARKERS, marker, dsn_marker, "private.invalid", "private_db", "private.mapping")
                ),
                "output_sha256": _sha256(output),
                "entrypoint_explicit": entrypoint is not None,
            }
    return {"cases": results, "status": "PASS" if all(item["status"] == "PASS" and item["marker_leak_absent"] for item in results.values()) else "FAIL"}


def qualify(dsn: str, output: Path, artifacts: Path) -> dict[str, object]:
    if not dsn.strip():
        raise ValueError("EPHI_TEST_POSTGRES_DSN is required")
    seed_env = {"EPHI_ENV": "test", "EPHI_TEST_POSTGRES_DSN": dsn, "EPHI_SYNTHETIC_SCOPE_ID": "synthetic-u1-scope"}
    from unittest.mock import patch

    with patch.dict(os.environ, seed_env, clear=False):
        seed = _seed_downstream_postgresql(dsn)
    browser = _positive_browser_path(dsn, seed, artifacts)
    negative = _negative_startup_matrix()
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        stored = adapter.get_aggregate(
            AccessScope(seed["scope_id"], site_id="synthetic-site", family_id="synthetic-u1-family"),
            "episode_workflow",
            seed["episode_id"],
        )
        stored_workflow_state = stored.state.get("work_state") if stored is not None else None
    finally:
        adapter.close()

    head = _git("rev-parse", "HEAD")
    if head == PREDECESSOR:
        candidate = None
        candidate_tree = None
        status_paths = [
            line[3:].strip() for line in _git("status", "--porcelain=v1", "--untracked-files=all").splitlines()
        ]
        changed_files = sorted(set(status_paths))
        source_diff = _git("diff", "--no-ext-diff", "--unified=3", PREDECESSOR, "--", "src/ephi/ui/app.py")
    else:
        candidate = head
        candidate_tree = _git("rev-parse", "HEAD^{tree}")
        if subprocess.run(
            ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", PREDECESSOR, candidate], check=False
        ).returncode != 0:
            raise RuntimeError("FIX_CANDIDATE_ANCESTRY_INVALID")
        changed_files = _git("diff", "--name-only", PREDECESSOR, candidate).splitlines()
        source_diff = _git("diff", "--no-ext-diff", "--unified=3", PREDECESSOR, candidate, "--", "src/ephi/ui/app.py")
    forbidden_paths = [
        path for path in changed_files
        if path.startswith("migrations/")
        or "nicegui_base" in path.lower()
        or path.startswith("src/ephi/ui/") and path != "src/ephi/ui/app.py"
    ]
    report: dict[str, object] = {
        "schema_version": 1,
        "project": "ephi",
        "request": "EPHI-CHG-182-U1-R3-FIX",
        "target_base": BASE,
        "predecessor_candidate": PREDECESSOR,
        "candidate_commit": candidate,
        "candidate_state": "PRECOMMIT_WORKTREE" if candidate is None else "COMMITTED_CANDIDATE",
        "candidate_tree": candidate_tree,
        "branch": _git("branch", "--show-current"),
        "changed_files_from_predecessor": changed_files,
        "exact_episode_source_diff": source_diff,
        "scope_proof": {
            "forbidden_changed_paths": forbidden_paths,
            "nicegui_base_changed": any("nicegui_base" in path.lower() for path in changed_files),
            "migrations_changed": any(path.startswith("migrations/") for path in changed_files),
            "unrelated_ephi_ui_surfaces_changed": any(path.startswith("src/ephi/ui/") and path != "src/ephi/ui/app.py" for path in changed_files),
        },
        "postgres": {"version": seed["postgres_version"], "real_postgresql_18": seed["postgres_version"].startswith("18.")},
        "downstream_launch": {
            "entrypoint": ENTRYPOINT,
            "application_entrypoint": "python -m ephi --serve",
            "environment": "test",
            "dev_identity_authority_present": False,
            "legacy_metrology_source_authority_present": False,
            "subject": seed["subject"],
            "scope_id": seed["scope_id"],
            "episode_id": seed["episode_id"],
            "workflow_state": seed["workflow_state"],
            "workflow_state_after_browser_action": stored_workflow_state,
            "browser_acceptance": browser,
        },
        "fail_closed_startup_reconfirmation": negative,
        "predecessor_geometry": {
            "source": "R2 VERIFY CF-cb9b379d7bef4704181d277a user-provided observation",
            "document_or_main_height_px": 12022,
            "episode_surface_width_px": 73,
            "episode_surface_height_px": 11807,
            "description_list_width_px": 7,
            "description_list_height_px": 5777,
        },
        "non_claims": {
            "real_family_g02_g06": "NOT_RUN",
            "company_identity_tls": "NOT_RUN",
            "g10": "NOT_RUN",
            "g12_port_gate_production": "NOT_CLAIMED",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    passed = (
        browser["geometry"]["status"] == "PASS"
        and browser["actions_status"] == "PASS"
        and browser["http"]["attention_status"] == 200
        and browser["http"]["episode_status"] == 200
        and not any(browser["browser"]["events"][name] for name in ("console_errors", "page_errors", "request_failures", "http_errors"))
        and browser["browser"]["websocket_session_observed"]
        and browser["provider_identity"]["workflow_after_claim"] == stored_workflow_state
        and browser["browser"]["ordinary_keyboard_flow"]["initial_focus_is_episode_heading"]
        and browser["browser"]["ordinary_keyboard_flow"]["claim_focus_target"] == "Acknowledge episode"
        and browser["browser"]["ordinary_keyboard_flow"]["focus_after_return"] == "Attention"
        and negative["status"] == "PASS"
        and not forbidden_paths
    )
    return {"status": "PASS" if passed else "FAIL", "evidence": str(output), "candidate_commit": candidate, "browser_geometry": browser["geometry"], "fail_closed": negative["status"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("EPHI_TEST_POSTGRES_DSN", ""))
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/review/chg_182_u1_r3_fix_qualification.json")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "evidence/u1/chg_182_u1_r3_fix")
    args = parser.parse_args()
    try:
        result = qualify(args.dsn, args.output, args.artifacts)
    except Exception as error:
        print(json.dumps({"status": "FAIL", "error_type": type(error).__name__}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
