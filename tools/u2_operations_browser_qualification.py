#!/usr/bin/env python3
"""Candidate-bound synthetic CHG-252 Operations browser qualification."""

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from ephi.config import RuntimeSettings  # noqa: E402
from ephi.downstream import compose_downstream  # noqa: E402
from ephi.application.operations import artifact_blob_path  # noqa: E402
from examples.synthetic_downstream.family_center import (  # noqa: E402
    GREEN_RELEASE_ID,
    publish_current_synthetic_source,
    seed_synthetic_workspace,
)
from examples.synthetic_downstream.provider import (  # noqa: E402
    build_bundle,
    build_operations_unbound_bundle,
)
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter  # noqa: E402


UTC = timezone.utc
TARGET_COMMIT = "705aca4d86095e5be6e80538a9bd7bd5bd05faa6"
CHANGE_ID = "CHG-252 U2.5/O9.2"
INPUT_ROOTS = (ROOT / "src/ephi", ROOT / "examples/synthetic_downstream", ROOT / "tests", ROOT / "tools", ROOT / "migrations")
_TRUNCATE_SQL = (
    "TRUNCATE outcome_value_revision, handoff_delivery_attempt, handoff_delivery_status, handoff_intent, "
    "decision_snapshot, source_capability, source_snapshot, artifact_catalog, o3_attention_projection, "
    "query_snapshot_row, query_snapshot, read_head, read_revision, applied_effect, job, outbox_event, "
    "audit_event, command_receipt, aggregate_state CASCADE"
)


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


def _wait(port: int, process: subprocess.Popen[str], timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("OPERATIONS_APPLICATION_STARTUP_EXITED")
        with closing(socket.socket()) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("OPERATIONS_APPLICATION_STARTUP_TIMEOUT")


def _compose(dsn: str, artifact_root: Path, *, unbound: bool = False):
    os.environ["EPHI_TEST_POSTGRES_DSN"] = dsn
    os.environ["EPHI_SYNTHETIC_ARTIFACT_ROOT"] = str(artifact_root)
    bundle = build_operations_unbound_bundle() if unbound else build_bundle()
    runtime_class = bundle.runtime.public_metadata.target_environment_class
    return compose_downstream(bundle, runtime_settings=RuntimeSettings(environment=runtime_class))


def _enqueue_fixture_jobs(composition: Any) -> dict[str, str]:
    worker = composition.adapter.worker_store()
    scope = composition.scope_provider()
    records: dict[str, str] = {}

    def enqueue(name: str, *, max_attempts: int = 3):
        record = worker.enqueue(
            scope,
            f"SyntheticOperationsFixture.{name}",
            f"synthetic-operations-{name}-{datetime.now(UTC).isoformat()}",
            {"fixture": "synthetic-non-production", "private_source_row": "SAMPLE-ROW-NEVER-IN-DTO"},
            max_attempts=max_attempts,
        )
        records[name] = record.job_id
        return record

    enqueue("queued")
    running = enqueue("running")
    worker.claim(scope, "synthetic-worker-owner", job_type="SyntheticOperationsFixture.running")
    deferred = enqueue("deferred")
    deferred_lease = worker.claim(scope, "synthetic-deferral-owner", job_type="SyntheticOperationsFixture.deferred")
    worker.defer(deferred_lease.lease, datetime.now(UTC) + timedelta(hours=1))
    failed = enqueue("failed")
    failed_lease = worker.claim(scope, "synthetic-failure-owner", job_type="SyntheticOperationsFixture.failed")
    worker.fail(
        failed_lease.lease,
        retryable=False,
        error_code="SYNTHETIC_FAILURE",
        error_message="fixture diagnostic serial=SAMPLE-ROW-NEVER-IN-DTO password=fixture-secret /private/company/source.csv",
    )
    dead = enqueue("dead-letter", max_attempts=1)
    dead_lease = worker.claim(scope, "synthetic-dead-letter-owner", job_type="SyntheticOperationsFixture.dead-letter")
    worker.fail(
        dead_lease.lease,
        retryable=True,
        error_code="SYNTHETIC_RETRY_EXHAUSTED",
        error_message="synthetic retry exhausted",
    )
    expired = enqueue("expired-lease")
    expired_lease = worker.claim(scope, "synthetic-expired-owner", job_type="SyntheticOperationsFixture.expired-lease")
    composition.adapter.connection.execute(
        "UPDATE job SET lease_expires_at = clock_timestamp() - INTERVAL '1 second' WHERE job_id = %s",
        (expired_lease.job_id,),
    )
    return records


def _seed(dsn: str, artifact_root: Path, case: str, *, unbound: bool = False) -> dict[str, object]:
    os.environ.update({
        "EPHI_ENV": "test",
        "EPHI_TEST_POSTGRES_DSN": dsn,
        "EPHI_SYNTHETIC_SUBJECT": "synthetic-engineer",
        "EPHI_SYNTHETIC_SCOPE_ID": "synthetic-u1-scope",
    })
    composition = _compose(dsn, artifact_root, unbound=unbound)
    try:
        version = composition.adapter.server_version()
        if not version.startswith("18."):
            raise RuntimeError("REAL_POSTGRESQL_18_REQUIRED")
        composition.adapter.connection.execute(_TRUNCATE_SQL)
        binding_snapshot = publish_current_synthetic_source(composition, revision=f"operations-{case}")
        jobs = _enqueue_fixture_jobs(composition)
        fixture: dict[str, object] = {
            "case": case,
            "postgres_version": version,
            "source_snapshot_seeded": bool(binding_snapshot),
            "worker_states_seeded": ["QUEUED", "RUNNING", "DEFERRED", "FAILED", "DEAD_LETTER", "EXPIRED_LEASE"],
            "job_ids": jobs,
            "synthetic": True,
            "production_operator_capability_bound": False,
        }

        if case in {"mixed_axes", "source_stale", "qualification_expired", "qualification_pending", "artifact_missing", "artifact_corrupt"}:
            if case == "qualification_expired":
                qualification = seed_synthetic_workspace(composition, GREEN_RELEASE_ID, "expired")
            elif case == "qualification_pending":
                qualification = seed_synthetic_workspace(composition, GREEN_RELEASE_ID, "pending")
            else:
                qualification = seed_synthetic_workspace(composition, GREEN_RELEASE_ID, "green")
            fixture["qualification"] = {
                "state": case,
                "synthetic": qualification["synthetic"],
                "production_approval": qualification["production_approval"],
            }

        if case == "source_stale":
            composition.adapter.connection.execute(
                "UPDATE source_capability SET state = 'STALE', reason = 'SOURCE_AVAILABILITY_EXCEEDS_FRESHNESS_LIMIT' "
                "WHERE scope_key = %s AND source_id = %s AND family_id = %s AND capability_id = %s",
                (
                    composition.scope_provider().canonical_key,
                    composition.source_binding.source_id,
                    composition.source_binding.family_id,
                    composition.source_binding.capability_id,
                ),
            )
            fixture["source_state_injected"] = "STALE"
        elif case == "source_unavailable":
            composition.adapter.connection.execute(
                "DELETE FROM source_capability WHERE scope_key = %s AND source_id = %s AND family_id = %s AND capability_id = %s",
                (
                    composition.scope_provider().canonical_key,
                    composition.source_binding.source_id,
                    composition.source_binding.family_id,
                    composition.source_binding.capability_id,
                ),
            )
            fixture["source_state_injected"] = "UNAVAILABLE"
            fixture["qualification"] = {"state": "NOT_QUALIFIED", "authority": "BOUND_FAMILY_CENTER"}
        elif case in {"artifact_missing", "artifact_corrupt"}:
            artifact = composition.artifact_service.write_and_register(
                composition.principal_provider(),
                composition.scope_provider(),
                b"synthetic operations integrity fixture",
                media_type="application/octet-stream",
                logical_purpose=f"synthetic-operations-{case}",
                required_write_capability="synthetic.artifact.write",
            ).metadata
            blob_path = artifact_blob_path(composition.artifact_service.blob_store.root, artifact.content.sha256)
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            if case == "artifact_missing":
                blob_path.unlink()
            else:
                blob_path.write_bytes(b"corrupt synthetic integrity fixture")
            fixture["artifact_case"] = case
            fixture["artifact_catalog_referenced"] = True
            fixture["artifact_private_bytes_recorded"] = False
        elif case == "qualification_unbound":
            fixture["qualification"] = {"state": "NOT_QUALIFIED", "authority": "NOT_BOUND"}
        return fixture
    finally:
        composition.close()


def _environment(dsn: str, port: int, storage: Path, artifact_root: Path, *, unbound: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    entrypoint = "examples.synthetic_downstream.provider:build_operations_unbound_bundle" if unbound else "examples.synthetic_downstream.provider:build_bundle"
    env.update({
        "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT), env.get("PYTHONPATH", ""))),
        "EPHI_ENV": "test",
        "EPHI_HOST": "127.0.0.1",
        "EPHI_PORT": str(port),
        "EPHI_ALLOWED_BROWSER_ORIGINS": f"http://127.0.0.1:{port}",
        "EPHI_TEST_POSTGRES_DSN": dsn,
        "EPHI_POSTGRES_DSN": dsn,
        "EPHI_DOWNSTREAM_ENTRYPOINT": entrypoint,
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


def _start_server(dsn: str, temp: Path, artifact_root: Path, *, unbound: bool = False):
    port = _port()
    env = _environment(dsn, port, temp / f"nicegui-storage-{port}", artifact_root, unbound=unbound)
    log_path = temp / f"server-{port}.log"
    log_file = log_path.open("w", encoding="utf-8")
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
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log_file.close()
        raise
    return process, log_file, f"http://127.0.0.1:{port}"


def _stop_server(process: subprocess.Popen[str], log_file: Any) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    log_file.close()


def _record_browser_events(page: Any, events: dict[str, list[dict[str, object]]]) -> None:
    page.on("console", lambda item: events["console_errors"].append({"type": item.type, "digest": _sha(item.text)}) if item.type == "error" else None)
    page.on("pageerror", lambda error: events["page_errors"].append({"type": type(error).__name__, "digest": _sha(str(error))}))
    page.on("request", lambda request: events["requests"].append({"method": request.method, "path": request.url.split("?", 1)[0]}))
    page.on("requestfailed", lambda request: events["request_failures"].append({"method": request.method, "path": request.url.split("?", 1)[0]}))
    page.on("response", lambda response: events["http_failures"].append({"status": response.status, "path": response.url.split("?", 1)[0]}) if response.status >= 400 else None)


def _keyboard_focus(page: Any) -> dict[str, object]:
    target = page.get_by_role("button", name="Refresh")
    for count in range(1, 81):
        page.keyboard.press("Tab")
        if target.evaluate("element => element === document.activeElement"):
            style = target.evaluate("element => { const value=getComputedStyle(element); return {focusVisible:element.matches(':focus-visible'), outline:value.outlineStyle, outlineWidth:value.outlineWidth, outlineColor:value.outlineColor, boxShadow:value.boxShadow}; }")
            visible = style["focusVisible"] and (
                (style["outline"] != "none" and style["outlineWidth"] != "0px")
                or style["boxShadow"] != "none"
            )
            target.press("Enter")
            page.get_by_text("Observation refreshed at", exact=False).wait_for(timeout=15000)
            focused_after = target.evaluate("element => element === document.activeElement")
            return {"target_reached": True, "tab_count": count, "focus_visible": visible, "focus_after_refresh": focused_after, "style": style}
    return {"target_reached": False, "focus_visible": False, "focus_after_refresh": False}


def _browser_view(base: str, case: str, width: int, height: int, artifact_dir: Path, expected: tuple[str, ...]) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    events: dict[str, list[dict[str, object]]] = {
        "console_errors": [], "page_errors": [], "request_failures": [], "http_failures": [], "requests": [],
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, reduced_motion="reduce")
        page = context.new_page()
        _record_browser_events(page, events)
        page.goto(base + "/ephi/operations", wait_until="domcontentloaded", timeout=45000)
        try:
            page.get_by_text("EPHI platform operations", exact=True).wait_for(timeout=30000)
        except Exception:
            page.screenshot(path="/tmp/ephi-operations-startup-debug.png", full_page=True)
            raise RuntimeError("OPERATIONS_PAGE_STARTUP_MARKER_MISSING") from None
        page.get_by_text("Observation refreshed at", exact=False).wait_for(timeout=30000)
        page.get_by_text("Operator controls", exact=True).wait_for(timeout=30000)
        body = page.locator("body").inner_text()
        missing = [item for item in expected if item not in body]
        page_error_free = not events["page_errors"]
        secret_markers = ["fixture-secret", "SAMPLE-ROW-NEVER-IN-DTO", "/private/company", "postgresql://"]
        leaked = [item for item in secret_markers if item in body]
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(100)
        geometry = page.evaluate("() => ({viewport_width:innerWidth, viewport_height:innerHeight, document_width:document.documentElement.scrollWidth, body_width:document.body.scrollWidth, no_horizontal_overflow:document.documentElement.scrollWidth <= innerWidth + 1})")
        if width <= 600:
            mobile_detail_alternative = page.locator(".ephi-operations-job-cards").is_visible()
            desktop_worker_table = None
            worker_presentation_ok = mobile_detail_alternative
        else:
            mobile_detail_alternative = True
            desktop_worker_table = page.get_by_role("table", name="Bounded durable worker jobs").is_visible()
            worker_presentation_ok = desktop_worker_table
        expected_control_names = (
            "Retry eligible job", "Pause source/family processing", "Request projection repair", "Start approved restore rehearsal",
        )
        controls = {name: page.get_by_role("button", name=name).is_disabled() for name in expected_control_names}
        restore = page.get_by_role("button", name="Start approved restore rehearsal")
        restore.scroll_into_view_if_needed()
        box = restore.bounding_box()
        action_reachable = bool(
            box and box["height"] >= 44 and box["x"] >= 0
            and box["x"] + box["width"] <= width + 1
            and box["y"] >= 0 and box["y"] + box["height"] <= height + 1
        )
        keyboard = _keyboard_focus(page)
        page.evaluate("window.scrollTo(0, 0)")
        screenshot_name = f"operations-{case}-{'desktop' if width > 600 else 'phone'}-{width}x{height}.png"
        screenshot_path = artifact_dir / screenshot_name
        page.screenshot(path=str(screenshot_path), full_page=True)
        body_digest = _sha(body)
        aria_digest = _sha(page.locator("body").aria_snapshot())
        page.close()
        context.close()
        browser.close()

    clean = not any(events[key] for key in ("console_errors", "page_errors", "request_failures", "http_failures"))
    no_horizontal_overflow = bool(geometry["no_horizontal_overflow"])
    passed = (
        not missing and not leaked and page_error_free and clean and no_horizontal_overflow
        and mobile_detail_alternative and worker_presentation_ok and all(controls.values())
        and action_reachable and keyboard["target_reached"] and keyboard["focus_visible"]
        and keyboard["focus_after_refresh"]
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "case": case,
        "viewport": {"width": width, "height": height},
        "missing_content": missing,
        "private_markers_in_browser_text": leaked,
        "geometry": geometry,
        "disabled_controls": controls,
        "restore_control_reachable": action_reachable,
        "accessible_worker_detail_alternative": mobile_detail_alternative,
        "worker_table_visible_on_desktop": desktop_worker_table,
        "keyboard_focus": keyboard,
        "body_digest": body_digest,
        "accessibility_snapshot_digest": aria_digest,
        "inventories": events,
        "inventories_clean": clean,
        "screenshot": screenshot_name,
        "screenshot_sha256": _sha(screenshot_path.read_bytes()),
    }


def qualify(dsn: str, output: Path, artifact_dir: Path) -> dict[str, object]:
    if not dsn:
        raise RuntimeError("EPHI_TEST_POSTGRES_DSN_REQUIRED")
    output = output.resolve()
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    cases = (
        ("mixed_axes", ("PostgreSQL readiness and durability", "FAILED", "DEAD_LETTER", "READY", "Operator controls")),
        ("source_stale", ("Source freshness detail", "STALE", "OPERATIONS_QUERY_PATH_RESPONDED", "FAILED")),
        ("source_unavailable", ("Source freshness detail", "UNAVAILABLE", "CAPABILITY_RECORD_MISSING", "FAILED")),
        ("qualification_expired", ("Qualification and evidence freshness", "EXPIRED", "EVIDENCE_EXPIRED", "FAILED")),
        ("qualification_pending", ("Qualification and evidence freshness", "PENDING", "SYNTHETIC_REPLAY_PENDING", "FAILED")),
        ("artifact_missing", ("Artifact integrity detail", "MISSING_ARTIFACT_BYTES", "1", "FAILED")),
        ("artifact_corrupt", ("Artifact integrity detail", "CORRUPT_ARTIFACT_BYTES", "1", "FAILED")),
    )
    browsers: dict[str, dict[str, object]] = {}
    fixtures: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory(prefix="ephi-chg252-operations-") as temporary:
        temp = Path(temporary)
        artifact_root = temp / "server-artifacts"
        process: subprocess.Popen[str] | None = None
        log_file = None
        base = ""
        try:
            for case, expected in cases:
                fixture = _seed(dsn, artifact_root, case)
                fixtures[case] = fixture
                if process is None:
                    process, log_file, base = _start_server(dsn, temp, artifact_root)
                desktop = _browser_view(base, case, 1440, 900, artifact_dir, expected)
                phone = _browser_view(base, case, 390, 844, artifact_dir, expected)
                browsers[f"{case}_desktop_1440x900"] = desktop
                browsers[f"{case}_phone_390x844"] = phone
        finally:
            if process is not None and log_file is not None:
                _stop_server(process, log_file)
        process, log_file, base = None, None, ""

        unbound_fixture = _seed(dsn, artifact_root, "qualification_unbound", unbound=True)
        fixtures["qualification_unbound"] = unbound_fixture
        process, log_file, base = _start_server(dsn, temp, artifact_root, unbound=True)
        try:
            expected_unbound = ("Qualification and evidence freshness", "NOT_QUALIFIED", "QUALIFICATION_AUTHORITY_NOT_BOUND")
            browsers["qualification_unbound_desktop_1440x900"] = _browser_view(base, "qualification_unbound", 1440, 900, artifact_dir, expected_unbound)
            browsers["qualification_unbound_phone_390x844"] = _browser_view(base, "qualification_unbound", 390, 844, artifact_dir, expected_unbound)
        finally:
            _stop_server(process, log_file)

    screenshots = [
        {"path": item.relative_to(ROOT).as_posix(), "bytes": item.stat().st_size, "sha256": _sha(item.read_bytes())}
        for item in sorted(artifact_dir.glob("operations-*.png"))
    ]
    browser_pass = bool(browsers) and all(item["status"] == "PASS" for item in browsers.values())
    report = {
        "schema_version": 1,
        "project": "ephi",
        "change": CHANGE_ID,
        "scope": "synthetic authorized EPHI platform Operations cockpit",
        "base_commit": TARGET_COMMIT,
        "target_ref": f"main@{TARGET_COMMIT}",
        "candidate": {
            "head_commit": _head_commit(),
            "source_test_tool_digest": _candidate_digest(),
            "state": "working-tree candidate based on registered target",
        },
        "synthetic_only": True,
        "postgres": {
            "version": next(iter(fixtures.values()))["postgres_version"],
            "real_postgresql_18": True,
            "integration": "tests.test_operations_postgresql",
        },
        "fixtures": fixtures,
        "browser": browsers,
        "screenshots": screenshots,
        "inventories_clean": all(item["inventories_clean"] for item in browsers.values()),
        "security": {
            "credentials_recorded": False,
            "DSN_or_path_browser_input": False,
            "raw_source_rows_recorded": False,
            "private_artifact_bytes_recorded": False,
            "browser_headers_recorded": False,
            "request_paths_only": True,
        },
        "non_claims": {
            "manufacturing_or_tool_health": "NOT_CLAIMED",
            "production_durability_or_slo": "NOT_CLAIMED",
            "production_rpo_rto": "NOT_ESTABLISHED",
            "production_operator_power": "NOT_BOUND",
            "real_family_g02_g06": "NOT_CLAIMED",
            "production_capacity_g10": "NOT_CLAIMED",
            "port_gate_g12_or_production": "NOT_CLAIMED",
            "browser_restore_execution": "NOT_IMPLEMENTED",
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
        print(json.dumps({"status": "FAIL", "error_type": type(error).__name__, "error_code": "OPERATIONS_BROWSER_QUALIFICATION_FAILED"}, sort_keys=True))
        return 1
    print(json.dumps({"status": report["status"], "output": str(args.output), "browser_views": len(report["browser"])}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
