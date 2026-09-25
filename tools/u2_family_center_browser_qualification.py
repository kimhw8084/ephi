#!/usr/bin/env python3
"""Qualify the synthetic Family Center UI at desktop and phone viewports."""

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from ephi.config import RuntimeSettings  # noqa: E402
from ephi.downstream import compose_downstream  # noqa: E402
from examples.synthetic_downstream.family_center import seed_synthetic_workspace  # noqa: E402
from examples.synthetic_downstream.provider import build_bundle  # noqa: E402
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter  # noqa: E402


FAMILY_ID = "synthetic-u1-family"
GREEN_RELEASE = "synthetic-release-1"
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
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait(port: int, process: subprocess.Popen[str], timeout: float = 40) -> None:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if process.poll() is not None:
            raise RuntimeError("APPLICATION_STARTUP_EXITED")
        with closing(socket.socket()) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("APPLICATION_STARTUP_TIMEOUT")


def _compose(dsn: str, artifact_root: Path):
    os.environ["EPHI_SYNTHETIC_ARTIFACT_ROOT"] = str(artifact_root)
    bundle = build_bundle()
    runtime_class = bundle.runtime.public_metadata.target_environment_class
    return compose_downstream(bundle, runtime_settings=RuntimeSettings(environment=runtime_class))


def _seed(dsn: str, artifact_root: Path) -> dict[str, object]:
    os.environ["EPHI_TEST_POSTGRES_DSN"] = dsn
    os.environ["EPHI_SYNTHETIC_SUBJECT"] = "synthetic-engineer"
    os.environ["EPHI_ENV"] = "test"
    composition = _compose(dsn, artifact_root)
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
        facts = {}
        for release, mode in (
            (GREEN_RELEASE, "green"),
            ("synthetic-release-ambiguous", "ambiguous"),
            ("synthetic-release-expired", "expired"),
            ("synthetic-release-failed", "failed"),
            ("synthetic-release-pending", "pending"),
        ):
            facts[release] = seed_synthetic_workspace(composition, release, mode)
        return {
            "postgres_version": version,
            "family_id": FAMILY_ID,
            "green_release": GREEN_RELEASE,
            "states": facts,
            "synthetic": True,
            "production_approval": False,
        }
    finally:
        composition.close()


def _environment(dsn: str, port: int, storage: Path, artifact_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), env.get("PYTHONPATH", ""))),
        "EPHI_ENV": "test",
        "EPHI_HOST": "127.0.0.1",
        "EPHI_PORT": str(port),
        "EPHI_ALLOWED_BROWSER_ORIGINS": f"http://127.0.0.1:{port}",
        "EPHI_TEST_POSTGRES_DSN": dsn,
        "EPHI_POSTGRES_DSN": dsn,
        "EPHI_DOWNSTREAM_ENTRYPOINT": "examples.synthetic_downstream.provider:build_bundle",
        "EPHI_SYNTHETIC_SUBJECT": "synthetic-engineer",
        "EPHI_SYNTHETIC_SCOPE_ID": "synthetic-u1-scope",
        "EPHI_SYNTHETIC_ARTIFACT_ROOT": str(artifact_root),
        "NICEGUI_BASE_ROOT_PATH": "",
        "NICEGUI_BASE_PROXY_ENABLED": "false",
        "NICEGUI_BASE_TRUSTED_PROXIES": "127.0.0.1,::1",
        "NICEGUI_BASE_STORAGE_SECRET": secrets.token_urlsafe(36),
        "NICEGUI_STORAGE_PATH": str(storage),
    })
    return env


def _select_release(page: Any, release: str) -> None:
    selector = page.get_by_label("Capability and target release")
    selector.click()
    page.get_by_text(f"synthetic-capability · synthetic-product · {release}", exact=True).last.click()
    page.get_by_text("Promotion readiness", exact=True).wait_for(timeout=10000)
    page.wait_for_timeout(250)


def _browser_page(base: str, width: int, height: int, artifacts: Path, *, expected: tuple[str, ...]) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    events: dict[str, list[dict[str, str]]] = {"console_errors": [], "page_errors": [], "request_failures": [], "http_failures": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, reduced_motion="reduce")
        page = context.new_page()
        page.on("console", lambda item: events["console_errors"].append({"type": item.type, "digest": _sha(item.text)}) if item.type == "error" else None)
        page.on("pageerror", lambda error: events["page_errors"].append({"type": type(error).__name__, "digest": _sha(str(error))}))
        page.on("requestfailed", lambda request: events["request_failures"].append({"method": request.method, "path": request.url.split("?", 1)[0].split("/", 3)[-1]}))
        page.on("response", lambda response: events["http_failures"].append({"status": str(response.status), "path": response.url.split("?", 1)[0].split("/", 3)[-1]}) if response.status >= 400 else None)
        page.goto(base + f"/ephi/families/{FAMILY_ID}", wait_until="domcontentloaded", timeout=45000)
        page.get_by_text("Family Center", exact=True).last.wait_for(timeout=30000)
        page.wait_for_timeout(300)
        body = page.locator("body").inner_text()
        if "Promotion readiness" not in body:
            page.screenshot(path=str(artifacts / f"family-center-startup-error-{width}x{height}.png"), full_page=True)
            raise RuntimeError("PROMOTION_READINESS_NOT_RENDERED:" + body[-2000:])
        missing = [fact for fact in expected if fact not in body]
        scroll = page.evaluate("({viewport:innerWidth,document:document.documentElement.scrollWidth})")
        no_overflow = scroll["document"] <= scroll["viewport"] + 1
        page.screenshot(path=str(artifacts / f"family-center-green-{width}x{height}.png"), full_page=True)
        enabled_when_green = page.get_by_role("button", name="Promote generic qualification").is_enabled()
        focus = page.get_by_role("button", name="Refresh workspace")
        keyboard = {"target_reached": False, "focus_visible": False}
        for count in range(1, 41):
            page.keyboard.press("Tab")
            if focus.evaluate("element => element === document.activeElement"):
                style = focus.evaluate("""element => {
                  const value=getComputedStyle(element);
                  const context=element.closest('.ephi-family-focus-context');
                  const contextStyle=context ? getComputedStyle(context) : null;
                  return {outline:value.outlineStyle,width:value.outlineWidth,color:value.outlineColor,focusVisible:element.matches(':focus-visible'),tag:element.tagName,classes:element.className,contextOutline:contextStyle?.outlineStyle,contextWidth:contextStyle?.outlineWidth,contextFound:Boolean(context)};
                }""")
                focus_indicator = style["outline"] != "none" and style["width"] != "0px" or style["contextOutline"] not in (None, "none") and style["contextWidth"] != "0px"
                keyboard = {"target_reached": True, "tab_count": count, "focus_visible": style["focusVisible"] and focus_indicator}
                break
        focus.click()
        try:
            page.wait_for_function("() => document.activeElement?.hasAttribute('data-family-focus-return')", timeout=5000)
            keyboard["focus_continuity_after_refresh"] = True
        except Exception:
            keyboard["focus_continuity_after_refresh"] = False
        status_views = {}
        for release, label, state, blocker_text in (
            ("synthetic-release-ambiguous", "ambiguous", "BLOCKED", "AMBIGUOUS_CANONICAL_ROLE_MAPPING"),
            ("synthetic-release-expired", "expired", "EXPIRED", "DISCOVER_MAP:EXPIRED"),
            ("synthetic-release-failed", "failed", "FAIL", "DISCOVER_MAP:FAIL"),
            ("synthetic-release-pending", "pending", "PENDING", "REPLAY:PENDING"),
        ):
            _select_release(page, release)
            state_locator = page.get_by_text(state, exact=True)
            visible = state_locator.count() > 0
            blocker = page.get_by_text(blocker_text, exact=False).count() > 0
            pending_job_queued = " · QUEUED" in page.locator("body").inner_text() if label == "pending" else None
            status_views[label] = {
                "visible": visible,
                "precise_blocker_visible": blocker,
                **({"durable_job_queued": pending_job_queued} if label == "pending" else {}),
            }
            if not visible or not blocker:
                missing.append(f"{label}:{state}:blocker")
            if label == "pending" and not pending_job_queued:
                missing.append("pending:durable-job-status")
            page.screenshot(path=str(artifacts / f"family-center-{label}-{width}x{height}.png"), full_page=True)
        green = page.get_by_label("Capability and target release")
        green.click()
        page.get_by_text(f"synthetic-capability · synthetic-product · {GREEN_RELEASE}", exact=True).last.click()
        page.wait_for_function(
            "() => document.body.innerText.includes('CURRENT') && document.body.innerText.includes('Promote generic qualification')",
            timeout=10000,
        )
        blocked_action_disabled = False
        _select_release(page, "synthetic-release-ambiguous")
        blocked_action_disabled = page.get_by_role("button", name="Promote generic qualification").is_disabled()
        page.screenshot(path=str(artifacts / f"family-center-blocked-{width}x{height}.png"), full_page=True)
        body = page.locator("body").inner_text()
        accessibility_digest = _sha(page.locator("body").aria_snapshot())
        geometry = {"viewport": {"width": width, "height": height}, "document_scroll_width": scroll["document"], "no_horizontal_overflow": no_overflow}
        page.close()
        context.close()
        browser.close()
    passed = (
        not missing and no_overflow and keyboard["target_reached"] and keyboard["focus_visible"]
        and keyboard["focus_continuity_after_refresh"] and enabled_when_green and blocked_action_disabled and not any(events.values())
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "viewport": {"width": width, "height": height},
        "geometry": geometry,
        "required_rendered_states": status_views,
        "missing_rendered_states": missing,
        "promotion_enabled_when_ready": enabled_when_green,
        "promotion_disabled_when_blocked": blocked_action_disabled,
        "keyboard_focus": keyboard,
        "accessibility_snapshot_digest": accessibility_digest,
        "events": events,
    }


def _invalidate_source(dsn: str, artifact_root: Path) -> dict[str, object]:
    os.environ["EPHI_TEST_POSTGRES_DSN"] = dsn
    os.environ["EPHI_SYNTHETIC_SUBJECT"] = "synthetic-engineer"
    os.environ["EPHI_ENV"] = "test"
    composition = _compose(dsn, artifact_root)
    try:
        result = seed_synthetic_workspace(composition, "synthetic-release-stale", "stale")
        return result
    finally:
        composition.close()


def qualify(dsn: str, output: Path, artifact_dir: Path) -> dict[str, object]:
    if not dsn:
        raise RuntimeError("EPHI_TEST_POSTGRES_DSN_REQUIRED")
    output = output.resolve()
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ephi-u2-family-center-") as temporary:
        temp = Path(temporary)
        artifact_root = temp / "artifacts"
        seed_facts = _seed(dsn, artifact_root)
        port = _port()
        env = _environment(dsn, port, temp / "nicegui-storage", artifact_root)
        log_path = temp / "server.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [sys.executable, "-m", "ephi", "--serve"],
                cwd=ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        try:
            _wait(port, process)
            base = f"http://127.0.0.1:{port}"
            desktop = _browser_page(
                base, 1440, 900, artifact_dir,
                expected=("SYNTHETIC FIXTURE", "PASS", "NOT_APPLICABLE", "Promotion readiness", "NOT G12", "Mapping hash", "Data Reality"),
            )
            mobile = _browser_page(
                base, 390, 844, artifact_dir,
                expected=("SYNTHETIC FIXTURE", "PASS", "NOT_APPLICABLE", "Promotion readiness", "NOT G12", "Mapping hash", "Data Reality"),
            )
            stale = _invalidate_source(dsn, artifact_root)
            with __import__("playwright.sync_api", fromlist=["sync_playwright"]).sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
                page = context.new_page()
                page.goto(base + f"/ephi/families/{FAMILY_ID}", wait_until="domcontentloaded", timeout=30000)
                page.get_by_text("Promotion readiness", exact=True).wait_for(timeout=30000)
                selector = page.get_by_label("Capability and target release")
                selector.click()
                page.get_by_text("synthetic-capability · synthetic-product · synthetic-release-stale", exact=True).last.click()
                page.get_by_text("Historical promotion records", exact=True).wait_for(timeout=10000)
                page.screenshot(path=str(artifact_dir / "family-center-stale-1440x900.png"), full_page=True)
                stale_body = page.locator("body").inner_text()
                stale_required = ("Data Reality", "STALE", "Historical promotion records", "NOT G12")
                stale_missing = [item for item in stale_required if item not in stale_body]
                page.close()
                context.close()
                browser.close()
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
    report = {
        "schema_version": 1,
        "project": "ephi",
        "change": "CHG-233 U2.3 / O7.2",
        "scope": "synthetic Family Center qualification only",
        "base_commit": "de61d564b4aa9d70f17e3ab375bf867f034677f0",
        "target_ref": "main@de61d564b4aa9d70f17e3ab375bf867f034677f0",
        "candidate": {"head_commit": _head_commit(), "source_test_tool_digest": _candidate_digest(), "state": "working-tree candidate based on registered target"},
        "synthetic_only": True,
        "g12_production_approval": False,
        "postgres": {"version": seed_facts["postgres_version"], "restart_history_test": "tests.test_family_center_postgresql", "version_18": str(seed_facts["postgres_version"]).startswith("18.")},
        "fixture": seed_facts,
        "dependency_invalidation_fixture": stale,
        "browser": {
            "desktop_1440x900": desktop,
            "phone_390x844": mobile,
            "stale_invalidation": {"status": "PASS" if not stale_missing else "FAIL", "missing": stale_missing},
        },
        "screenshots": screenshots,
        "security": {"credentials_recorded": False, "raw_source_rows_recorded": False, "artifact_bytes_recorded": False, "browser_headers_recorded": False},
        "non_claims": {"real_company_mapping": "NOT_CLAIMED", "real_family_g02_g06": "NOT_CLAIMED", "w3_w4_w5_human_pilot": "NOT_CLAIMED", "second_real_family_portability": "NOT_CLAIMED", "g12_release_approval": "NOT_CLAIMED", "production": "NOT_CLAIMED"},
        "status": "PASS" if desktop["status"] == "PASS" and mobile["status"] == "PASS" and not stale_missing else "FAIL",
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
