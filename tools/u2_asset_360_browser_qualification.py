#!/usr/bin/env python3
"""Candidate-bound PostgreSQL 18 browser qualification for CHG-234 U2.4."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
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
from urllib.parse import quote, urlencode


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from ephi.config import RuntimeSettings  # noqa: E402
from ephi.downstream import compose_downstream  # noqa: E402
from examples.synthetic_downstream.assets import (  # noqa: E402
    FIXTURE_DISCLAIMER,
    INCOMPATIBLE_ASSET,
    PEER_ASSET,
    PRIMARY_ASSET,
    seed_asset_360_fixture,
)
from examples.synthetic_downstream.provider import build_flagship_bundle  # noqa: E402
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter  # noqa: E402


UTC = timezone.utc
TARGET_COMMIT = "4482233202ff2667262ff0d47ff390e72d398d3c"
CHANGE_ID = "CHG-234 U2.4"
INPUT_ROOTS = (ROOT / "src/ephi", ROOT / "examples/synthetic_downstream", ROOT / "tests", ROOT / "tools", ROOT / "migrations")


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _candidate_digest() -> str:
    digest = hashlib.sha256()
    paths = sorted(
        item for base in INPUT_ROOTS for item in base.rglob("*")
        if item.is_file() and "__pycache__" not in item.parts and item.suffix in {".py", ".sql"}
    )
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _head_commit() -> str:
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def _port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait(port: int, process: subprocess.Popen[str], timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("ASSET_APPLICATION_STARTUP_EXITED")
        with closing(socket.socket()) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("ASSET_APPLICATION_STARTUP_TIMEOUT")


def _compose(dsn: str, artifact_root: Path):
    os.environ["EPHI_TEST_POSTGRES_DSN"] = dsn
    os.environ["EPHI_SYNTHETIC_ARTIFACT_ROOT"] = str(artifact_root)
    bundle = build_flagship_bundle()
    runtime_class = bundle.runtime.public_metadata.target_environment_class
    return compose_downstream(bundle, runtime_settings=RuntimeSettings(environment=runtime_class))


def _seed(dsn: str, artifact_root: Path, anchor: datetime, *, capability_case: str = "READY") -> dict[str, Any]:
    os.environ.update({
        "EPHI_ENV": "test",
        "EPHI_TEST_POSTGRES_DSN": dsn,
        "EPHI_SYNTHETIC_SUBJECT": "synthetic-engineer",
        "EPHI_SYNTHETIC_ASSET_OBSERVATION_ANCHOR": anchor.isoformat(),
    })
    composition = _compose(dsn, artifact_root)
    try:
        version = composition.adapter.server_version()
        if not version.startswith("18."):
            raise RuntimeError("REAL_POSTGRESQL_18_REQUIRED")
        facts = seed_asset_360_fixture(composition, source_capability_case=capability_case)
        facts["postgres_version"] = version
        facts["observation_anchor"] = anchor.isoformat()
        return facts
    finally:
        composition.close()


def _environment(dsn: str, port: int, storage: Path, artifact_root: Path, anchor: datetime) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT), env.get("PYTHONPATH", ""))),
        "EPHI_ENV": "test",
        "EPHI_HOST": "127.0.0.1",
        "EPHI_PORT": str(port),
        "EPHI_ALLOWED_BROWSER_ORIGINS": f"http://127.0.0.1:{port}",
        "EPHI_TEST_POSTGRES_DSN": dsn,
        "EPHI_POSTGRES_DSN": dsn,
        "EPHI_DOWNSTREAM_ENTRYPOINT": "examples.synthetic_downstream.provider:build_flagship_bundle",
        "EPHI_SYNTHETIC_SUBJECT": "synthetic-engineer",
        "EPHI_SYNTHETIC_ASSET_OBSERVATION_ANCHOR": anchor.isoformat(),
        "EPHI_SYNTHETIC_ARTIFACT_ROOT": str(artifact_root),
        "NICEGUI_BASE_ROOT_PATH": "",
        "NICEGUI_BASE_PROXY_ENABLED": "false",
        "NICEGUI_BASE_TRUSTED_PROXIES": "127.0.0.1,::1",
        "NICEGUI_BASE_STORAGE_SECRET": secrets.token_urlsafe(36),
        "NICEGUI_STORAGE_PATH": str(storage),
    })
    return env


def _url(asset_id: str, anchor: datetime, *, peer: str | None = None) -> str:
    cutoff = datetime.now(UTC)
    params = {
        "start": (anchor - timedelta(days=30)).isoformat(),
        "end": cutoff.isoformat(),
        "cutoff": cutoff.isoformat(),
    }
    if peer:
        params["peer"] = peer
    return f"/ephi/assets/{quote(asset_id, safe='')}?{urlencode(params)}"


def _inventory(page: Any, events: dict[str, list[dict[str, object]]]) -> None:
    page.on("console", lambda item: events["console_errors"].append({"type": item.type, "message_digest": _sha(item.text)}) if item.type == "error" else None)
    page.on("pageerror", lambda error: events["page_errors"].append({"type": type(error).__name__, "message_digest": _sha(str(error))}))
    page.on("request", lambda request: events["requests"].append({"method": request.method, "path": request.url.split("?", 1)[0]}))
    page.on("requestfailed", lambda request: events["request_failures"].append({"method": request.method, "path": request.url.split("?", 1)[0]}))
    page.on("response", lambda response: events["responses"].append({"method": response.request.method, "status": response.status, "path": response.url.split("?", 1)[0]}))
    page.on("response", lambda response: events["http_failures"].append({"status": response.status, "path": response.url.split("?", 1)[0]}) if response.status >= 400 else None)


def _geometry(page: Any, width: int, height: int, action_label: str | None = None) -> dict[str, object]:
    action = page.get_by_role("button", name=action_label).first if action_label else None
    if action is not None and action.count():
        action.scroll_into_view_if_needed()
        action_box = action.bounding_box()
    else:
        action_box = None
    result = page.evaluate("""() => ({
      viewport_width: innerWidth, viewport_height: innerHeight,
      document_width: document.documentElement.scrollWidth,
      document_height: document.documentElement.scrollHeight,
      no_horizontal_overflow: document.documentElement.scrollWidth <= innerWidth + 1,
      body_width: document.body.scrollWidth
    })""")
    result["viewport"] = {"width": width, "height": height}
    result["action_box"] = action_box
    result["action_reachable"] = bool(
        action_box and action_box["height"] >= 44 and action_box["x"] >= 0
        and action_box["x"] + action_box["width"] <= width + 1
        and action_box["y"] >= 0 and action_box["y"] + action_box["height"] <= height + 1
    ) if action_box else None
    return result


def _keyboard_focus(page: Any, artifacts: Path, name: str, suffix: str) -> dict[str, object]:
    target = page.get_by_role("button", name="Apply filters")
    for count in range(1, 61):
        page.keyboard.press("Tab")
        if target.evaluate("element => element === document.activeElement"):
            # Allow the pinned Base focus helper's short visual state transition
            # to settle before checking its computed indicator and capturing it.
            page.wait_for_timeout(250)
            state = target.evaluate("element => { const style=getComputedStyle(element), shadow=style.boxShadow, transparent=/rgba?\\([^)]*,\\s*0(?:\\.0+)?\\s*\\)/.test(shadow)||/\\/\\s*0(?:\\.0+)?\\)/.test(shadow), helper=element.querySelector('.q-focus-helper'), helperStyle=helper&&getComputedStyle(helper), before=getComputedStyle(element,'::before'); return {focus_visible:element.matches(':focus-visible'),outline:style.outlineStyle,outline_width:style.outlineWidth,box_shadow:shadow,box_shadow_transparent:transparent,before_shadow:before.boxShadow,before_outline:before.outline,helper_opacity:helperStyle?.opacity,helper_background:helperStyle?.backgroundColor,tag:element.tagName,classes:element.className,active:element===document.activeElement}; }")
            state["screenshot"] = f"asset-list-{name}-keyboard-focus{suffix}.png"
            page.screenshot(path=str(artifacts / state["screenshot"]))
            state["screenshot_sha256"] = _sha((artifacts / state["screenshot"]).read_bytes())
            has_focus_indicator = (state["outline"] != "none" and state["outline_width"] != "0px") or (state["box_shadow"] != "none" and not state["box_shadow_transparent"])
            return {"target_reached": True, "tab_count": count, "focus_visible": bool(state["focus_visible"] and has_focus_indicator), "focus_style": state}
    return {"target_reached": False, "focus_visible": False}


def _browser_view(base: str, width: int, height: int, artifacts: Path, anchor: datetime, *, stale: bool) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    name = "desktop" if width > 600 else "mobile"
    events: dict[str, list[dict[str, object]]] = {
        "console_errors": [], "page_errors": [], "request_failures": [], "http_failures": [],
        "requests": [], "responses": [],
    }
    screenshots: list[dict[str, str]] = []
    states: dict[str, object] = {}
    keyboard: dict[str, object] = {}
    suffix = "-stale" if stale else ""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, reduced_motion="reduce")
        page = context.new_page()
        _inventory(page, events)

        def capture(label: str, *, asset_url: str, expected: tuple[str, ...], tab: str | None = None, action_label: str | None = None) -> dict[str, object]:
            page.goto(base + asset_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(750)
            ready_marker = "Asset 360" if "/ephi/assets/" in asset_url else "synthetic-cd-asset-primary"
            body = page.locator("body").inner_text()
            if ready_marker not in body and "Asset data unavailable" not in body and "Asset permission required" not in body:
                filename = f"asset-{label}-{width}x{height}-startup.png"
                page.screenshot(path=str(artifacts / filename), full_page=True)
                raise RuntimeError(f"ASSET_PAGE_NOT_READY:{asset_url}:{body[-2500:]}")
            if tab:
                page.get_by_role("tab", name=tab).click()
                page.wait_for_timeout(150)
            body = page.locator("body").inner_text()
            missing = [item for item in expected if item not in body]
            svg = page.locator("svg.ephi-assets-chart")
            chart = None
            if svg.count():
                chart = svg.evaluate("element => ({circle_count:element.querySelectorAll('circle').length, polyline_count:element.querySelectorAll('polyline').length, path_count:element.querySelectorAll('path').length, aria_label:element.getAttribute('aria-label')})")
            geometry = _geometry(page, width, height, action_label)
            filename = f"asset-{label}-{width}x{height}.png"
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(100)
            page.screenshot(path=str(artifacts / filename), full_page=True)
            screenshot = artifacts / filename
            screenshots.append({"path": screenshot.name, "sha256": _sha(screenshot.read_bytes())})
            return {"missing": missing, "geometry": geometry, "chart": chart, "body_digest": _sha(body), "screenshot": screenshot.name}

        asset_list = capture(
            f"list-{name}{suffix}", asset_url="/ephi/assets",
            expected=("synthetic-cd-asset-primary", "synthetic-cd-asset-peer", "synthetic-cd-asset-incompatible", "qualified EPHI Episode truth"),
            action_label="Open",
        )
        if name == "desktop":
            keyboard = _keyboard_focus(page, artifacts, name, suffix)
        episodes = capture(
            f"360-episodes-{name}{suffix}", asset_url=_url(PRIMARY_ASSET, anchor),
            expected=("Synthetic mean CD excursion", "Historical workflow snapshot", "Open engineering work", "company asset-master completeness"),
            action_label="Open Episode",
        )
        trend = capture(
            f"360-gap-compatible-{name}{suffix}", asset_url=_url(PRIMARY_ASSET, anchor, peer=PEER_ASSET if not stale else None),
            expected=("Observation points are unconnected", "synthetic-asset-primary-p3", *( ("CAPABILITY STALE",) if stale else ("QUALIFIED DESCRIPTIVE COMPARE", "synthetic-qualified-peer-population") )),
            tab="Measurement/quality",
        )
        deep_link = {"status": "NOT_RUN"}
        if name == "desktop":
            page.goto(base + _url(PRIMARY_ASSET, anchor), wait_until="domcontentloaded", timeout=30000)
            page.get_by_text("Open Episode", exact=True).first.wait_for(timeout=15000)
            page.get_by_text("Open Episode", exact=True).first.click()
            page.get_by_text("Episode investigation workspace", exact=True).wait_for(timeout=30000)
            return_link = page.get_by_role("link", name="Return to originating Asset 360")
            deep_link["origin_preserved"] = return_link.count() == 1
            if return_link.count():
                return_link.click()
                page.get_by_text("Asset 360", exact=False).first.wait_for(timeout=15000)
                deep_link["returned_to_asset"] = True
            else:
                deep_link["returned_to_asset"] = False
            deep_link["status"] = "PASS" if deep_link["origin_preserved"] and deep_link["returned_to_asset"] else "FAIL"
        blocked = None if stale else capture(
            f"360-compare-blocked-{name}{suffix}", asset_url=_url(PRIMARY_ASSET, anchor, peer=INCOMPATIBLE_ASSET),
            expected=("COMPARE BLOCKED", "FAMILY_CONTEXT_CHARACTERISTIC_OR_UNIT_MISMATCH"),
            tab="Measurement/quality",
        )
        stale_case: dict[str, object] | None = None
        if stale:
            stale_case = capture(
                f"360-stale-capability-{name}", asset_url=_url(PRIMARY_ASSET, anchor),
                expected=("SOURCE STALE", "CAPABILITY STALE", "CAPABILITY_NOT_READY"),
                tab="Measurement/quality",
            )
        page.close()
        context.close()
        browser.close()

    geometry_items = [asset_list["geometry"], episodes["geometry"], trend["geometry"]]
    if blocked:
        geometry_items.append(blocked["geometry"])
    if stale_case:
        geometry_items.append(stale_case["geometry"])
    no_overflow = all(bool(item["no_horizontal_overflow"]) for item in geometry_items)
    actions_reachable = bool(asset_list["geometry"]["action_reachable"] and episodes["geometry"]["action_reachable"])
    if trend["chart"]:
        expected_circles = 4 if stale else 6
        chart_ok = trend["chart"]["circle_count"] == expected_circles and trend["chart"]["polyline_count"] == 0 and trend["chart"]["path_count"] == 0
    else:
        chart_ok = False
    missing = asset_list["missing"] + episodes["missing"] + trend["missing"] + (blocked["missing"] if blocked else [])
    if stale_case:
        missing += stale_case["missing"]
    focus_ok = keyboard.get("target_reached", True) and keyboard.get("focus_visible", True)
    deep_link_ok = deep_link.get("status") in {"PASS", "NOT_RUN"}
    clean = not any(events[key] for key in ("console_errors", "page_errors", "request_failures", "http_failures"))
    passed = not missing and no_overflow and actions_reachable and chart_ok and focus_ok and deep_link_ok and clean
    return {
        "status": "PASS" if passed else "FAIL",
        "viewport": {"width": width, "height": height},
        "states": {"asset_list": asset_list, "asset_360_episodes": episodes, "history_gap_compatible_compare": trend, "blocked_compare": blocked, **({"stale_capability": stale_case} if stale_case else {})},
        "missing_content": missing,
        "no_horizontal_overflow": no_overflow,
        "actions_reachable": actions_reachable,
        "chart_gap_rendering": {"pass": chart_ok, "expected_separate_points": 4 if stale else 6, "actual": trend["chart"]},
        "keyboard_focus": keyboard,
        "episode_deep_link": deep_link,
        "inventories": events,
        "inventories_clean": clean,
        "screenshots": screenshots,
    }


def qualify(dsn: str, output: Path, artifact_dir: Path) -> dict[str, object]:
    if not dsn:
        raise RuntimeError("EPHI_TEST_POSTGRES_DSN_REQUIRED")
    output = output.resolve()
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ephi-chg234-asset-360-") as temporary:
        temp = Path(temporary)
        artifact_root = temp / "artifacts"
        anchor = datetime.now(UTC).replace(microsecond=0)
        seed_facts = _seed(dsn, artifact_root, anchor)
        port = _port()
        env = _environment(dsn, port, temp / "nicegui-storage", artifact_root, anchor)
        log_path = temp / "server.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen([sys.executable, "-m", "ephi", "--serve"], cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT, text=True)
        try:
            _wait(port, process)
            base = f"http://127.0.0.1:{port}"
            desktop = _browser_view(base, 1440, 900, artifact_dir, anchor, stale=False)
            mobile = _browser_view(base, 390, 844, artifact_dir, anchor, stale=False)
            stale_fixture = _seed(dsn, artifact_root, anchor, capability_case="STALE")
            stale_desktop = _browser_view(base, 1440, 900, artifact_dir, anchor, stale=True)
            stale_mobile = _browser_view(base, 390, 844, artifact_dir, anchor, stale=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    screenshots = [
        {"path": item.relative_to(ROOT).as_posix(), "bytes": item.stat().st_size, "sha256": _sha(item.read_bytes())}
        for item in sorted(artifact_dir.glob("*.png"))
    ]
    browser_pass = all(item["status"] == "PASS" for item in (desktop, mobile, stale_desktop, stale_mobile))
    report = {
        "schema_version": 1,
        "project": "ephi",
        "change": CHANGE_ID,
        "scope": "synthetic upstream Asset list and Asset 360/history only",
        "base_commit": TARGET_COMMIT,
        "target_ref": f"main@{TARGET_COMMIT}",
        "candidate": {"head_commit": _head_commit(), "source_test_tool_digest": _candidate_digest(), "state": "working-tree candidate based on registered target"},
        "synthetic_only": True,
        "fixture_disclaimer": FIXTURE_DISCLAIMER,
        "postgres": {"version": seed_facts["postgres_version"], "real_postgresql_18": seed_facts["postgres_version"].startswith("18."), "restart_and_identity_regression": "tests.test_assets_postgresql"},
        "fixture": seed_facts,
        "stale_capability_fixture": stale_fixture,
        "browser": {"desktop_1440x900": desktop, "phone_390x844": mobile, "stale_capability_desktop": stale_desktop, "stale_capability_phone": stale_mobile},
        "screenshots": screenshots,
        "security": {"credentials_recorded": False, "raw_source_rows_recorded": False, "browser_headers_recorded": False, "request_paths_only": True},
        "non_claims": {
            "company_asset_master_completeness": "NOT_CLAIMED",
            "historical_application_source_identity_or_equivalence": "NOT_CLAIMED",
            "real_family_g02_g06": "NOT_CLAIMED",
            "production_capacity_g10": "NOT_CLAIMED",
            "port_gate_g12_or_production": "NOT_CLAIMED",
            "operations_destination": "NOT_CLAIMED",
            "causal_rca_or_predictive_composite_health": "NOT_CLAIMED",
        },
        "status": "PASS" if browser_pass else "FAIL",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("EPHI_TEST_POSTGRES_DSN", ""))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = qualify(args.dsn, args.output, args.artifacts)
    except Exception as error:
        print(json.dumps({"status": "FAIL", "error_type": type(error).__name__, "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps({"status": report["status"], "output": str(args.output), "postgres": report["postgres"]["version"]}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
