"""Run the installed, synthetic-only EPHI composition and browser qualifier."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import closing
from dataclasses import replace
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

from ephi.application import AccessScope, RevisionVector
from ephi.config import EPHI_DOWNSTREAM_ENTRYPOINT, EPHI_ENV, RuntimeSettings
from ephi.config_preflight import preflight as configuration_preflight
from ephi.db_migrate import main as migration_main
from ephi.downstream import (
    DownstreamFailure,
    DownstreamReasonCode,
    ProviderBinding,
    compose_downstream,
    load_provider_bundle,
    preflight as downstream_preflight,
    validate_provider_bundle,
)
from ephi.operations_status import operations_status
from ephi.qualification_identity import (
    PROVIDER_ENTRYPOINT,
    QUALIFICATION_DISTRIBUTION,
    QUALIFICATION_VERSION,
)
from ephi.release_identity import (
    ReleaseFailure,
    _normal_name,
    canonical_json_bytes,
    installed_release_identity,
    release_preflight,
)


_EPISODE_ID = "synthetic-u3-installed-qualification-case"
_EPISODE_TITLE = "Synthetic U3 installed integration case"
_VIEWPORT = {"width": 1440, "height": 900}
_MIGRATION_TABLES = (
    "aggregate_state", "command_receipt", "audit_event", "outbox_event", "job", "applied_effect",
    "read_revision", "read_head", "query_snapshot", "query_snapshot_row", "artifact_catalog",
    "o3_attention_projection", "source_snapshot", "source_capability", "handoff_intent",
    "handoff_delivery_status", "handoff_delivery_attempt", "outcome_value_revision",
)


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _repository_location(path: Path) -> bool:
    for parent in (path, *path.parents):
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "ephi").is_dir():
            return True
    return False


def _execution_is_external() -> bool:
    if _repository_location(Path.cwd().resolve()):
        return False
    for entry in sys.path:
        candidate = Path(entry or Path.cwd()).expanduser()
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if _repository_location(candidate):
            return False
    return True


def _installed_distribution_facts() -> dict[str, object]:
    try:
        app = metadata.distribution("ephi")
        provider = metadata.distribution(QUALIFICATION_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        raise ReleaseFailure("INSTALLED_DISTRIBUTION_MISSING") from None
    app_files = {Path(str(item)).as_posix() for item in (app.files or ())}
    provider_in_core = any(
        item.startswith("examples/synthetic_downstream/") or "synthetic_downstream" in item
        for item in app_files
    )
    if provider_in_core:
        raise ReleaseFailure("SYNTHETIC_PROVIDER_IN_CORE_WHEEL")
    app_module = Path(app.locate_file("ephi/__init__.py")).resolve()
    provider_module = Path(provider.locate_file("examples/synthetic_downstream/provider.py")).resolve()
    import ephi
    import examples.synthetic_downstream.provider as synthetic_provider

    if Path(str(ephi.__file__)).resolve() != app_module:
        raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH")
    if Path(str(synthetic_provider.__file__)).resolve() != provider_module:
        raise ReleaseFailure("QUALIFICATION_PROVIDER_IMPORT_MISMATCH")
    launch_environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SYSTEMROOT")
        if key in os.environ
    }
    launch_environment.update({"EPHI_ENV": "test", "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1"})
    try:
        launch = subprocess.run(
            [sys.executable, "-m", "ephi", "--version"],
            cwd=Path.cwd(),
            env=launch_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
            text=True,
        )
    except Exception:
        raise ReleaseFailure("APPLICATION_ENTRYPOINT_LAUNCH_FAILED") from None
    if launch.returncode != 0 or launch.stdout.strip() != app.version or launch.stderr:
        raise ReleaseFailure("APPLICATION_ENTRYPOINT_LAUNCH_FAILED")
    return {
        "ephi_distribution": "ephi",
        "ephi_version": app.version,
        "ephi_imported_from_installed_distribution": True,
        "ordinary_ephi_wheel_contains_synthetic_provider": False,
        "synthetic_provider_distribution": QUALIFICATION_DISTRIBUTION,
        "synthetic_provider_version": provider.version,
        "synthetic_provider_imported_from_installed_distribution": True,
        "application_entrypoint_launch": "PASS",
        "working_directory_outside_repository": _execution_is_external(),
        "source_import_paths_absent": _execution_is_external(),
    }


def _configuration_values(*, profile: str, port: int, entrypoint: str | None) -> dict[str, str]:
    values = {
        EPHI_ENV: profile,
        "EPHI_HOST": "127.0.0.1" if profile == "test" else "0.0.0.0",
        "EPHI_PORT": str(port),
        "EPHI_ALLOWED_BROWSER_ORIGINS": (
            f"http://127.0.0.1:{port}" if profile == "test" else "https://qualification.invalid"
        ),
        "NICEGUI_BASE_STORAGE_SECRET": "synthetic-qualification-placeholder-not-a-secret",
        "NICEGUI_BASE_SECURE_SESSION_COOKIE": "true" if profile == "qa" else "false",
    }
    if entrypoint:
        values[EPHI_DOWNSTREAM_ENTRYPOINT] = entrypoint
    return values


def _capture_migration(arguments: list[str], dsn: str | None = None) -> tuple[int, dict[str, Any]]:
    from contextlib import redirect_stdout
    from io import StringIO

    output = StringIO()
    try:
        if dsn:
            prior = os.environ.get("EPHI_POSTGRES_DSN")
            os.environ["EPHI_POSTGRES_DSN"] = dsn
        else:
            prior = None
        with redirect_stdout(output):
            code = migration_main(arguments)
        if dsn:
            if prior is None:
                os.environ.pop("EPHI_POSTGRES_DSN", None)
            else:
                os.environ["EPHI_POSTGRES_DSN"] = prior
        report = json.loads(output.getvalue())
    except Exception:
        if dsn:
            if prior is None:
                os.environ.pop("EPHI_POSTGRES_DSN", None)
            else:
                os.environ["EPHI_POSTGRES_DSN"] = prior
        return 2, {"status": "FAIL", "reason_code": "MIGRATION_COMMAND_FAILED"}
    return code, report if isinstance(report, dict) else {"status": "FAIL", "reason_code": "MIGRATION_REPORT_INVALID"}


def _postgres_facts(dsn: str) -> dict[str, object]:
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=4, autocommit=True) as connection:
            row = connection.execute("SELECT current_setting('server_version'), current_setting('server_version_num')").fetchone()
    except Exception:
        return {"status": "BLOCKED", "reason_code": "POSTGRES_BINDING_UNAVAILABLE"}
    if not row:
        return {"status": "BLOCKED", "reason_code": "POSTGRES_VERSION_UNAVAILABLE"}
    version_text = str(row[0])
    match = re.match(r"^(\d+)(?:\.(\d+))?", version_text)
    numeric = int(row[1]) if str(row[1]).isdigit() else 0
    major = numeric // 10000 if numeric >= 10000 else 0
    version = f"{match.group(1)}.{match.group(2) or '0'}" if match else "UNKNOWN"
    return {
        "status": "PASS" if major == 18 else "BLOCKED",
        "reason_code": "POSTGRESQL_18_CONFIRMED" if major == 18 else "POSTGRESQL_18_REQUIRED",
        "major": major,
        "version": version,
    }


def _empty_database(composition: Any) -> bool:
    for table in _MIGRATION_TABLES:
        row = composition.adapter.connection.execute(f"SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)").fetchone()
        if row is None:
            return False
        values = tuple(row.values()) if isinstance(row, Mapping) else row
        if not values or bool(values[0]):
            return False
    return True


def _seed_product_fixture(dsn: str, artifact_root: Path) -> dict[str, object]:
    from examples.synthetic_downstream.family_center import seed_synthetic_workspace

    os.environ.update({
        EPHI_ENV: "test",
        "EPHI_TEST_POSTGRES_DSN": dsn,
        "EPHI_POSTGRES_DSN": dsn,
        "EPHI_SYNTHETIC_ARTIFACT_ROOT": str(artifact_root),
        "EPHI_SYNTHETIC_SUBJECT": "synthetic-engineer",
        "EPHI_SYNTHETIC_SCOPE_ID": "synthetic-u1-scope",
    })
    bundle = load_provider_bundle(PROVIDER_ENTRYPOINT)
    composition = compose_downstream(bundle, runtime_settings=RuntimeSettings(environment="test"))
    try:
        server_version = composition.adapter.server_version()
        if not server_version.startswith("18."):
            raise ReleaseFailure("POSTGRESQL_18_REQUIRED")
        if not _empty_database(composition):
            raise ReleaseFailure("QUALIFICATION_DATABASE_NOT_EMPTY")
        scope = composition.scope_provider()
        composition.adapter.seed_aggregate(
            scope,
            "episode_workflow",
            _EPISODE_ID,
            {"work_state": "OPEN", "owner": None},
            version=0,
        )
        composition.adapter.seed_attention_projection(
            scope,
            _EPISODE_ID,
            {
                "title": _EPISODE_TITLE,
                "asset_id": "synthetic-u3-asset",
                "priority": "P2",
                "severity": "MEDIUM",
                "technical_state": "READY",
                "source_state": "NOT_QUALIFIED",
                "deadline": None,
                "age": "1",
            },
        )
        workflow = composition.adapter.get_aggregate(scope, "episode_workflow", _EPISODE_ID)
        composition.adapter.publish_current_revision(
            scope,
            "episode",
            _EPISODE_ID,
            "u36-installed-read-revision",
            RevisionVector("u36-installed-analysis", None, None, 0, None, "u36-installed-manifest"),
            {
                "title": _EPISODE_TITLE,
                "analytical_revision": "u36-installed-analysis",
                "capability_state": {"source": "NOT_QUALIFIED"},
            },
            workflow,
        )
        family = seed_synthetic_workspace(composition, "synthetic-release-1", "green")
        return {
            "status": "PASS",
            "synthetic": True,
            "production_approval": False,
            "postgresql_major": 18,
            "attention_episode_id": _EPISODE_ID,
            "workflow_state": str(workflow.state.get("work_state", "OPEN")),
            "family_id": "synthetic-u1-family",
            "capability_id": "synthetic-capability",
            "release_id": "synthetic-release-1",
            "family_center_fixture_synthetic": family.get("synthetic") is True,
            "family_center_production_approval": family.get("production_approval") is True,
        }
    except ReleaseFailure:
        raise
    except Exception:
        raise ReleaseFailure("SYNTHETIC_FIXTURE_SEED_FAILED") from None
    finally:
        composition.close()


def _wait_for_app(port: int, process: subprocess.Popen[bytes], timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        with closing(socket.socket()) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _browser_events(page: Any, events: dict[str, list[dict[str, object]]]) -> None:
    page.on("console", lambda item: events["console_errors"].append({
        "type": "console_error", "digest": _sha256(item.text),
    }) if item.type == "error" else None)
    page.on("pageerror", lambda error: events["page_errors"].append({
        "type": "PAGE_ERROR", "digest": _sha256(str(error)),
    }))
    page.on("requestfailed", lambda request: events["failed_requests"].append({
        "method": request.method,
        "path": urlsplit(request.url).path,
        "reason_code": "REQUEST_FAILED",
    }))
    page.on("response", lambda response: events["http_errors"].append({
        "path": urlsplit(response.url).path,
        "status": response.status,
    }) if response.status >= 400 else None)


def _snapshot(page: Any, artifact_root: Path, name: str) -> dict[str, object]:
    filename = f"{name}-1440x900.png"
    path = artifact_root / filename
    page.screenshot(path=str(path), full_page=True, animations="disabled")
    raw = path.read_bytes()
    return {"path": filename, "kind": "browser_screenshot", "byte_size": len(raw), "sha256": _sha256(raw)}


def _navigate_keyboard(page: Any, href: str, heading: str) -> dict[str, object]:
    navigation = page.get_by_role("navigation", name="Primary navigation")
    item = navigation.get_by_role("listitem", name=heading).first
    item.wait_for(timeout=15000)
    item.focus()
    page.keyboard.press("Enter")
    page.wait_for_url(f"**{href}", timeout=20000)
    page.locator("main").get_by_text(heading, exact=True).first.wait_for(timeout=20000)
    return {"action": "Enter", "status": "PASS", "href": href}


def _browser_run(
    dsn: str,
    artifact_root: Path,
    storage_root: Path,
    executable: Path | None,
    provider_artifact_root: Path,
) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"status": "BLOCKED", "reason_code": "PLAYWRIGHT_DEPENDENCY_UNAVAILABLE"}
    port = _port()
    base = f"http://127.0.0.1:{port}"
    child_env = dict(os.environ)
    for key in tuple(child_env):
        if key.upper().startswith(("PYTHON", "PIP_", "UV_", "CONDA_")) or key in {
            "VIRTUAL_ENV", "__PYVENV_LAUNCHER__", "PYTHONPATH", "PYTHONHOME",
        }:
            child_env.pop(key, None)
    child_env.update({
        EPHI_ENV: "test",
        "EPHI_HOST": "127.0.0.1",
        "EPHI_PORT": str(port),
        "EPHI_ALLOWED_BROWSER_ORIGINS": base,
        "EPHI_TEST_POSTGRES_DSN": dsn,
        "EPHI_POSTGRES_DSN": dsn,
        EPHI_DOWNSTREAM_ENTRYPOINT: PROVIDER_ENTRYPOINT,
        "EPHI_SYNTHETIC_SUBJECT": "synthetic-engineer",
        "EPHI_SYNTHETIC_SCOPE_ID": "synthetic-u1-scope",
        "EPHI_SYNTHETIC_ARTIFACT_ROOT": str(provider_artifact_root),
        "NICEGUI_BASE_ROOT_PATH": "",
        "NICEGUI_BASE_PROXY_ENABLED": "false",
        "NICEGUI_BASE_TRUSTED_PROXIES": "127.0.0.1,::1",
        "NICEGUI_BASE_STORAGE_SECRET": secrets.token_urlsafe(36),
        "NICEGUI_STORAGE_PATH": str(storage_root / "nicegui-storage"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    })
    storage_root.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen[bytes] | None = None
    screenshots: list[dict[str, object]] = []
    paths: dict[str, object] = {}
    events: dict[str, list[dict[str, object]]] = {
        "console_errors": [], "page_errors": [], "failed_requests": [], "http_errors": [],
    }
    browser_version = "UNKNOWN"
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "ephi", "--serve"],
            cwd=storage_root,
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_for_app(port, process):
            return {"status": "FAIL", "reason_code": "INSTALLED_APPLICATION_STARTUP_FAILED"}
        with sync_playwright() as playwright:
            launch_options: dict[str, object] = {"headless": True}
            if executable is not None:
                launch_options["executable_path"] = str(executable)
            browser = playwright.chromium.launch(**launch_options)
            browser_version = browser.version
            context = browser.new_context(
                viewport=_VIEWPORT,
                device_scale_factor=1,
                reduced_motion="reduce",
            )
            page = context.new_page()
            _browser_events(page, events)

            response = page.goto(base + "/", wait_until="domcontentloaded", timeout=30000)
            if response is None or response.status != 200:
                raise ReleaseFailure("ATTENTION_PAGE_HTTP_FAILED")
            page.get_by_role("heading", name="Attention", exact=True).wait_for(timeout=25000)
            page.get_by_text(_EPISODE_ID, exact=True).first.wait_for(timeout=20000)
            if _EPISODE_TITLE not in page.locator("body").inner_text():
                raise ReleaseFailure("ATTENTION_SYNTHETIC_CASE_NOT_RENDERED")
            screenshots.append(_snapshot(page, artifact_root, "attention"))
            row_cell = page.locator(".cui-data-table .ag-center-cols-container .ag-row").first.locator(".ag-cell").first
            row_cell.focus()
            page.keyboard.press("Space")
            open_episode = page.get_by_role("button", name="Open episode", exact=True)
            open_episode.wait_for(timeout=15000)
            open_episode.focus()
            page.keyboard.press("Enter")
            page.wait_for_url("**/episode", timeout=20000)
            page.get_by_role("heading", name="Episode investigation workspace", exact=True).wait_for(timeout=20000)
            page.get_by_text(_EPISODE_ID, exact=True).first.wait_for(timeout=20000)
            if "OPEN" not in page.locator(".ephi-o10-episode-surface").inner_text():
                raise ReleaseFailure("EPISODE_WORKSPACE_TRUTH_NOT_RENDERED")
            screenshots.append(_snapshot(page, artifact_root, "episode"))
            paths["attention_to_episode"] = {
                "status": "PASS",
                "episode_id": _EPISODE_ID,
                "row_selection_action": "Space",
                "open_action": "Enter",
                "rendered_through_application_route": True,
            }

            _navigate_keyboard(page, "/ephi/families", "Family Center")
            family_link = page.locator('a[href="/ephi/families/synthetic-u1-family"]').first
            family_link.wait_for(timeout=15000)
            family_link.focus()
            page.keyboard.press("Enter")
            page.wait_for_url("**/ephi/families/synthetic-u1-family", timeout=20000)
            page.get_by_text("Promotion readiness", exact=True).wait_for(timeout=20000)
            family_body = page.locator("body").inner_text()
            required_family_facts = (
                "synthetic-u1-family", "synthetic-capability", "synthetic-product",
                "synthetic-release-1", "SYNTHETIC FIXTURE · NON-PRODUCTION evidence and identities",
                "NOT G12 production/release promotion", "NOT Production approval",
            )
            if any(value not in family_body for value in required_family_facts):
                raise ReleaseFailure("FAMILY_CENTER_SYNTHETIC_SCOPE_NOT_RENDERED")
            screenshots.append(_snapshot(page, artifact_root, "family-center"))
            paths["family_center"] = {
                "status": "PASS",
                "family_id": "synthetic-u1-family",
                "capability_id": "synthetic-capability",
                "release_id": "synthetic-release-1",
                "synthetic_non_production_label_visible": True,
                "g12_and_production_promotion_denied_by_fixture_semantics": True,
            }

            page_navigation = (
                ("/ephi/assets", "Assets", "assets"),
                ("/ephi/outcomes", "Outcomes", "outcomes"),
                ("/ephi/operations", "Operations", "operations"),
            )
            for href, heading, name in page_navigation:
                nav_fact = _navigate_keyboard(page, href, heading)
                body = page.locator("body").inner_text()
                if name == "assets":
                    if "No supported InvestigationProfile is present" not in body:
                        raise ReleaseFailure("ASSETS_EMPTY_STATE_NOT_TRUTHFUL")
                elif name == "outcomes":
                    if "No matching Outcomes" not in body or "This does not mean EPHI created no value" not in body:
                        raise ReleaseFailure("OUTCOMES_EMPTY_STATE_NOT_TRUTHFUL")
                else:
                    page.get_by_text(
                        "READY means only that this application and Operations query path responded",
                        exact=False,
                    ).wait_for(timeout=20000)
                    page.get_by_text(
                        "No audited OperationsControl capability is bound",
                        exact=False,
                    ).wait_for(timeout=20000)
                    body = page.locator("body").inner_text()
                    if "READY means only that this application and Operations query path responded" not in body:
                        raise ReleaseFailure("OPERATIONS_TRUTH_BOUNDARY_NOT_RENDERED")
                    if "No audited OperationsControl capability is bound" not in body:
                        raise ReleaseFailure("OPERATIONS_UNBOUND_CONTROL_NOT_RENDERED")
                screenshots.append(_snapshot(page, artifact_root, name))
                paths[name] = {
                    "status": "PASS",
                    "route": href,
                    "navigation_action": nav_fact["action"],
                    "truthful_installed_composition_state": True,
                }

            page.close()
            context.close()
            browser.close()
    except ReleaseFailure as exc:
        return {
            "status": "FAIL",
            "reason_code": exc.reason_code,
            "browser_version": browser_version,
            "paths": paths,
            "screenshots": screenshots,
            "failure_inventory": events,
        }
    except Exception:
        return {
            "status": "FAIL",
            "reason_code": "BROWSER_QUALIFICATION_FAILED",
            "browser_version": browser_version,
            "paths": paths,
            "screenshots": screenshots,
            "failure_inventory": events,
        }
    finally:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=8)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=3)
                except Exception:
                    pass
    failure_count = sum(len(value) for value in events.values())
    return {
        "status": "PASS" if failure_count == 0 else "FAIL",
        "reason_code": "BROWSER_PATH_PASS" if failure_count == 0 else "UNEXPECTED_BROWSER_FAILURES",
        "browser": {"engine": "Chromium", "version": browser_version, "executable_source": "explicit" if executable else "installed_playwright_browser"},
        "viewport_set": [_VIEWPORT],
        "keyboard_and_action_reachability": {
            "attention_row_selection": "Space",
            "open_episode": "Enter",
            "family_center_assets_outcomes_operations_navigation": "Enter",
        },
        "paths": paths,
        "failure_inventory": events,
        "screenshots": screenshots,
    }


def _browser_executable(argument: str | None) -> tuple[Path | None, str | None]:
    selected: Path | None = None
    if argument:
        candidate = Path(argument).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            selected = candidate.resolve()
        else:
            return None, "BROWSER_EXECUTABLE_UNAVAILABLE"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            options: dict[str, object] = {"headless": True}
            if selected is not None:
                options["executable_path"] = str(selected)
            browser = playwright.chromium.launch(**options)
            browser.close()
        return selected, None
    except Exception:
        pass
    return None, "BROWSER_EXECUTABLE_UNAVAILABLE"


def _negative_controls(dsn: str | None) -> list[dict[str, object]]:
    from ephi.config_preflight import PREFLIGHT_SCHEMA

    controls: list[dict[str, object]] = []

    def record(name: str, expected: str, actual: str) -> None:
        controls.append({
            "control": name,
            "status": "PASS" if actual == expected else "FAIL",
            "expected_reason_code": expected,
            "observed_reason_code": actual,
        })

    try:
        missing = downstream_preflight("", compose=False)
        record("missing_provider_entrypoint", "MISSING_REQUIRED_PROVIDER", str(missing["status_code"]))
        absent_module = downstream_preflight(
            "ephi_qualification_provider_absent.provider:build_bundle", compose=False,
        )
        record("missing_synthetic_provider_distribution", "PROVIDER_LOAD_ERROR", str(absent_module["status_code"]))

        bundle = load_provider_bundle(PROVIDER_ENTRYPOINT)
        try:
            validate_provider_bundle(replace(bundle, abi_version="99.0.0"))
            incompatible_abi = "ACCEPTED"
        except DownstreamFailure as exc:
            incompatible_abi = exc.reason_code.value
        record("incompatible_provider_abi", DownstreamReasonCode.INCOMPATIBLE_ABI.value, incompatible_abi)

        source = bundle.source
        contract = replace(
            source.contract,
            required_capabilities=(*source.contract.required_capabilities, "qualification.invalid.capability.v99"),
        )
        incompatible = replace(bundle, source=ProviderBinding(contract, source.implementation))
        try:
            validate_provider_bundle(incompatible)
            incompatible_contract = "ACCEPTED"
        except DownstreamFailure as exc:
            incompatible_contract = exc.reason_code.value
        record(
            "incompatible_provider_contract",
            DownstreamReasonCode.INCOMPATIBLE_PROVIDER_CONTRACT.value,
            incompatible_contract,
        )

        qa_values = _configuration_values(profile="qa", port=8080, entrypoint=None)
        qa = configuration_preflight(qa_values)
        missing_reason = "MISSING_DOWNSTREAM_ENTRYPOINT" if "MISSING_DOWNSTREAM_ENTRYPOINT" in qa["reason_codes"] else str(qa["status_code"])
        record("generic_non_development_missing_provider", "MISSING_DOWNSTREAM_ENTRYPOINT", missing_reason)

        prior_test = os.environ.pop("EPHI_TEST_POSTGRES_DSN", None)
        prior_normal = os.environ.pop("EPHI_POSTGRES_DSN", None)
        try:
            no_database = downstream_preflight(PROVIDER_ENTRYPOINT, compose=True)
        finally:
            if prior_test is not None:
                os.environ["EPHI_TEST_POSTGRES_DSN"] = prior_test
            if prior_normal is not None:
                os.environ["EPHI_POSTGRES_DSN"] = prior_normal
        observed = str(no_database["safe_composition_smoke"].get("reason_code", "UNKNOWN"))
        record("absent_postgresql_binding", "COMPOSITION_FAIL_CLOSED", observed)
        _ = PREFLIGHT_SCHEMA
    except Exception:
        controls.append({
            "control": "fail_closed_negative_controls",
            "status": "FAIL",
            "expected_reason_code": "SAFE_BOUNDED_FAILURE",
            "observed_reason_code": "NEGATIVE_CONTROL_EXECUTION_FAILED",
        })

    absent_browser_path = None
    try:
        with tempfile.TemporaryDirectory(prefix="ephi-browser-negative-") as temporary:
            absent_browser_path = Path(temporary) / "missing-chromium"
            resolved, blocked = _browser_executable(str(absent_browser_path))
            actual = "BROWSER_EXECUTABLE_UNAVAILABLE" if blocked and resolved is None else "BROWSER_AVAILABLE"
            record("absent_browser_prerequisite", "BROWSER_EXECUTABLE_UNAVAILABLE", actual)
    except Exception:
        record("absent_browser_prerequisite", "BROWSER_EXECUTABLE_UNAVAILABLE", "NEGATIVE_CONTROL_EXECUTION_FAILED")
    return controls


def _gate(
    gate_id: str,
    scope: str,
    state: str,
    reason_code: str,
    reason: str,
    evidence: object,
    prerequisite: str,
) -> dict[str, object]:
    identity = _sha256(canonical_json_bytes({
        "gate": gate_id,
        "declared_scope": scope,
        "state": state,
        "reason_code": reason_code,
        "evidence": evidence,
    }))
    return {
        "gate": gate_id,
        "declared_scope": scope,
        "state": state,
        "reason_code": reason_code,
        "reason": reason,
        "evidence_identity": f"sha256:{identity}" if evidence is not None else "NOT_RUN",
        "remaining_prerequisite": prerequisite,
    }


def _gate_matrix(observed: dict[str, Any]) -> list[dict[str, object]]:
    release_ok = observed.get("release_status") == "PASS" and observed.get("qualification_identity") is not None
    config_ok = observed.get("configuration_status") == "PASS"
    composition_ok = observed.get("composition_status") == "PASS"
    postgres_ok = observed.get("postgres_status") == "PASS" and observed.get("migration_status") == "PASS"
    browser_ok = observed.get("browser_status") == "PASS"
    release_evidence = observed.get("release_evidence")
    composition_evidence = observed.get("composition_evidence")
    postgres_evidence = {
        "postgresql": observed.get("postgres_evidence"),
        "migration": observed.get("migration_evidence"),
    }
    browser_evidence = observed.get("browser_evidence")
    controls_evidence = observed.get("negative_controls")
    records = [
        _gate("G00", "canonical Git commit/tree/worktree, package/dependency identity, import/launch on supported Python", "PASS" if release_ok else "BLOCKED", "INSTALLED_RELEASE_AND_KIT_IDENTITY_PASS" if release_ok else str(observed.get("release_reason", "INSTALLED_RELEASE_OR_KIT_IDENTITY_UNAVAILABLE")), "Installed EPHI and separate qualification artifacts are bound to the prepared candidate and pass package identity checks." if release_ok else "The exact installed release and qualification kit identity did not pass.", release_evidence, "Install and verify the exact EPHI and qualification wheels from the prepared offline payload."),
        _gate("G01", "275-pass baseline maintained except explicitly justified behavior corrections", "NOT_RUN", "FULL_REGRESSION_SUITE_NOT_RUN_BY_INSTALLED_QUALIFIER", "This installed runtime command does not execute repository tests.", None, "Run exact-candidate full unittest discovery and package checks."),
        _gate("G02", "no future-available input; revisions immutable; historical supersession correct", "NOT_RUN", "REAL_FAMILY_TEMPORAL_EVIDENCE_NOT_BOUND", "Synthetic fixture rows are excluded from real-family temporal qualification.", None, "Run F04 regression plus replay/property tests against approved real-family evidence."),
        _gate("G03", "low confidence, missing/stale/pipeline-suspect inputs cannot establish recovery", "NOT_RUN", "FAMILY_RECOVERY_EVIDENCE_NOT_RUN", "The installed composition path does not qualify a family's recovery thresholds.", None, "Run F05 regression and repeated-sample/context tests against approved first-family evidence."),
        _gate("G04", "technical recovery cannot hide open engineering work; closure/reopen preserved", "NOT_RUN", "WORKFLOW_CONTINUITY_MATRIX_NOT_RUN", "This browser path opens an Episode but does not test closure or reopen transitions.", None, "Run F03 regression and the terminal-state/obligation matrix."),
        _gate("G05", "source/checkpoint consistency; CAS, idempotency, fencing, outbox, no partial publication", "PARTIAL" if postgres_ok and composition_ok else "BLOCKED", "POSTGRESQL_18_INSTALLED_SMOKE_PASS" if postgres_ok and composition_ok else "POSTGRESQL_OR_SCHEMA_PREREQUISITE_BLOCKED", "PostgreSQL 18, current schema, and the bounded composition path are exercised; the durable transaction gate remains open." if postgres_ok and composition_ok else "PostgreSQL 18/current-schema evidence is unavailable.", postgres_evidence, "Run PostgreSQL kill/restart/timeout/interleaving integration tests for all durable transaction invariants."),
        _gate("G06", "units/IDs/times/context/coverage; detector/recovery behavior on representative family data", "NOT_RUN", "REAL_FAMILY_SOURCE_EVIDENCE_NOT_BOUND", "The synthetic source fixture cannot satisfy real-family G06.", None, "Run data-reality, replay, golden, and shadow qualification with approved real-family policy IDs."),
        _gate("G07", "query/command behavior identical through API/UI/CLI; scope isolation; receipt conflicts", "PARTIAL" if composition_ok else "BLOCKED", "INSTALLED_APPLICATION_AUTHORITY_PATH_PASS" if composition_ok else "APPLICATION_COMPOSITION_UNAVAILABLE", "The existing application and operation-time authorization authorities are composed; this path does not establish contract parity or full scope isolation." if composition_ok else "Installed provider composition did not pass.", composition_evidence, "Run mandatory contract, authorization, scope-isolation, and receipt-conflict regressions."),
        _gate("G08", "installed APIs/patterns/tokens, DataSource pushdown, no duplicate state authority", "PARTIAL" if composition_ok else "BLOCKED", "INSTALLED_BASE_AND_PROVIDER_PATH_EXERCISED" if composition_ok else "INSTALLED_COMPOSITION_UNAVAILABLE", "Installed Base and U1 conformance are exercised; the Base agent-check and full DataSource/runtime contract remain separate." if composition_ok else "Installed provider composition is unavailable.", composition_evidence, "Run the Base agent-check/gate/runtime contract and provider conformance regression."),
        _gate("G09", "real interactions, responsive states, keyboard, no unexpected console errors", "PARTIAL" if browser_ok else ("BLOCKED" if observed.get("browser_status") == "BLOCKED" else "NOT_RUN"), "INSTALLED_BROWSER_PATH_PASS" if browser_ok else str(observed.get("browser_reason", "BROWSER_LAYER_NOT_RUN")), "The bounded installed desktop browser path is recorded; responsive states and human visual review remain open." if browser_ok else "The real browser path did not complete; no source-only result substitutes for it.", browser_evidence, "Complete required responsive, accessibility, keyboard, and human review evidence."),
        _gate("G10", "budgets at explicit load, degraded sources, worker starvation, restore", "NOT_RUN", "PRODUCTION_LIKE_TARGET_EVIDENCE_NOT_RUN", "Local synthetic PostgreSQL/browser evidence does not satisfy G10.", None, "Run load budgets and repeated degraded-source, worker-starvation, and restore evidence on a production-like target."),
        _gate("G11", "claim/event uniqueness; as-of corrections; attribution; rates; negative net value honest", "NOT_RUN", "REAL_AUDITED_OUTCOMES_NOT_BOUND", "Synthetic or empty Outcomes state does not satisfy G11.", None, "Run value-integrity scenarios and the independent reviewer workflow on approved real Outcomes evidence."),
        _gate("G12", "EPHI release + Base pin + family/capability + current evidence + rollback readiness", "PENDING", "PROMOTION_CONTROL_PLANE_NOT_RUN", "No promotion decision or production-readiness aggregate is emitted by this qualification kit.", None, "Run the existing fail-closed promotion control plane and obtain independent approval after all applicable gates."),
    ]
    return records


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_json_bytes(value))


def _candidate_identity(inputs_dir: Path) -> dict[str, object] | None:
    try:
        value = json.loads((inputs_dir / "install_inputs.json").read_text(encoding="utf-8"))
        source = value.get("source") if isinstance(value, dict) else None
        commit = source.get("commit") if isinstance(source, dict) else None
        tree = source.get("tree") if isinstance(source, dict) else None
        if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) and isinstance(tree, str) and re.fullmatch(r"[0-9a-f]{40}", tree):
            return {"commit": commit, "tree": tree}
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return None


def _qualify(args: argparse.Namespace) -> dict[str, object]:
    artifact_root = Path(args.artifact_dir).expanduser()
    if artifact_root.is_symlink():
        raise ReleaseFailure("ARTIFACT_ROOT_INVALID")
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_root = artifact_root.resolve()
    if _repository_location(artifact_root):
        raise ReleaseFailure("ARTIFACT_ROOT_MUST_BE_OUTSIDE_REPOSITORY")
    if not _execution_is_external():
        raise ReleaseFailure("QUALIFIER_WORKING_DIRECTORY_MUST_BE_EXTERNAL")

    inputs_dir = Path(args.inputs_dir).expanduser()
    qualification_inputs_dir = Path(args.qualification_inputs_dir).expanduser()
    candidate = _candidate_identity(inputs_dir)
    failures: list[dict[str, str]] = []
    observed: dict[str, Any] = {
        "release_status": "NOT_RUN",
        "release_reason": "RELEASE_PREFLIGHT_NOT_RUN",
        "configuration_status": "NOT_RUN",
        "composition_status": "NOT_RUN",
        "postgres_status": "NOT_RUN",
        "migration_status": "NOT_RUN",
        "browser_status": "NOT_RUN",
        "browser_reason": "BROWSER_LAYER_NOT_RUN",
    }
    release_report: dict[str, Any] | None = None
    installed_facts: dict[str, object] | None = None
    qualification_identity: dict[str, object] | None = None
    try:
        os.environ[EPHI_ENV] = "test"
        installed_facts = _installed_distribution_facts()
        if installed_facts.get("working_directory_outside_repository") is not True or installed_facts.get("source_import_paths_absent") is not True:
            raise ReleaseFailure("QUALIFIER_WORKING_DIRECTORY_MUST_BE_EXTERNAL")
        current_release = installed_release_identity()
        release_report = release_preflight(
            inputs_dir,
            qualification_inputs_dir=qualification_inputs_dir,
        )
        observed["release_status"] = "PASS"
        observed["release_reason"] = "RELEASE_AND_QUALIFICATION_PREFLIGHT_PASS"
        observed["release_evidence"] = {
            "release_preflight": release_report,
            "installed_distributions": installed_facts,
        }
        observed["qualification_identity"] = release_report.get("qualification_kit")
        observed["ephi_release_identity"] = current_release
        qualification_identity = release_report.get("qualification_kit")
    except ReleaseFailure as exc:
        observed["release_status"] = "FAIL"
        observed["release_reason"] = exc.reason_code
        failures.append({"layer": "release_install", "reason_code": exc.reason_code})
    except Exception:
        observed["release_status"] = "FAIL"
        observed["release_reason"] = "RELEASE_PREFLIGHT_FAILED"
        failures.append({"layer": "release_install", "reason_code": "RELEASE_PREFLIGHT_FAILED"})

    port = _port()
    config_values = _configuration_values(profile="test", port=port, entrypoint=PROVIDER_ENTRYPOINT)
    config_report = configuration_preflight(config_values)
    observed["configuration_status"] = config_report.get("status")
    observed["configuration_evidence"] = config_report
    if config_report.get("status") != "PASS":
        failures.append({"layer": "runtime_configuration", "reason_code": str(config_report.get("status_code", "CONFIG_PREFLIGHT_FAILED"))})

    migration_identity_code, migration_identity_report = _capture_migration(["identity"])
    postgres_dsn = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()
    if not postgres_dsn:
        postgres_report: dict[str, object] = {"status": "BLOCKED", "reason_code": "POSTGRES_TEST_BINDING_REQUIRED"}
    else:
        postgres_report = _postgres_facts(postgres_dsn)
    observed["postgres_evidence"] = postgres_report
    observed["postgres_status"] = postgres_report.get("status")
    if postgres_report.get("status") == "PASS":
        apply_code, apply_report = _capture_migration(["apply"], postgres_dsn)
        verify_code, verify_report = _capture_migration(["verify"], postgres_dsn)
        migration_ok = (
            migration_identity_code == 0
            and migration_identity_report.get("status") == "PLAN"
            and apply_code == 0 and apply_report.get("schema_state") == "CURRENT"
            and verify_code == 0 and verify_report.get("schema_state") == "CURRENT"
        )
        observed["migration_status"] = "PASS" if migration_ok else "FAIL"
        observed["migration_evidence"] = {
            "identity_sha256": verify_report.get("identity_sha256"),
            "migration_count": verify_report.get("migration_count"),
            "required_table_count": verify_report.get("required_table_count"),
            "schema_state": verify_report.get("schema_state"),
        }
        if not migration_ok:
            failures.append({"layer": "migration", "reason_code": "INSTALLED_SCHEMA_PREFLIGHT_FAILED"})
        else:
            os.environ.update({
                "EPHI_TEST_POSTGRES_DSN": postgres_dsn,
                "EPHI_POSTGRES_DSN": postgres_dsn,
                EPHI_DOWNSTREAM_ENTRYPOINT: PROVIDER_ENTRYPOINT,
                EPHI_ENV: "test",
            })
            composition_report = downstream_preflight(PROVIDER_ENTRYPOINT, compose=True)
            observed["composition_status"] = (
                "PASS"
                if composition_report.get("compatibility", {}).get("status") == "PASS"
                and composition_report.get("safe_composition_smoke", {}).get("status") == "PASS"
                else "FAIL"
            )
            observed["composition_evidence"] = {
                "status_code": composition_report.get("status_code"),
                "abi_id": composition_report.get("downstream_abi", {}).get("id"),
                "abi_version": composition_report.get("downstream_abi", {}).get("version"),
                "manifest_sha256": composition_report.get("downstream_abi", {}).get("safe_manifest_hash"),
                "provider_compatibility": composition_report.get("compatibility", {}).get("status"),
                "safe_composition_smoke": composition_report.get("safe_composition_smoke", {}).get("status"),
            }
            if observed["composition_status"] != "PASS":
                failures.append({"layer": "downstream_composition", "reason_code": str(composition_report.get("status_code", "COMPOSITION_FAILED"))})
    else:
        observed["migration_status"] = "BLOCKED"
        observed["migration_evidence"] = {
            "identity_status": migration_identity_report.get("status"),
            "identity_sha256": migration_identity_report.get("identity_sha256"),
            "migration_count": migration_identity_report.get("migration_count"),
        }
        failures.append({"layer": "postgresql", "reason_code": str(postgres_report.get("reason_code", "POSTGRESQL_UNAVAILABLE"))})

    controls = _negative_controls(postgres_dsn or None)
    controls_ok = bool(controls) and all(item.get("status") == "PASS" for item in controls)
    observed["negative_controls"] = controls
    observed["negative_controls_status"] = "PASS" if controls_ok else "FAIL"
    if not controls_ok:
        failures.append({"layer": "negative_controls", "reason_code": "FAIL_CLOSED_CONTROL_FAILED"})

    browser, browser_block = _browser_executable(args.chromium_executable)
    browser_report: dict[str, object]
    if browser_block:
        browser_report = {"status": "BLOCKED", "reason_code": browser_block}
    elif observed.get("composition_status") != "PASS" or observed.get("migration_status") != "PASS" or not postgres_dsn:
        browser_report = {"status": "BLOCKED", "reason_code": "INSTALLED_POSTGRESQL_COMPOSITION_REQUIRED"}
    else:
        try:
            from examples.synthetic_downstream.family_center import seed_synthetic_workspace
            _ = seed_synthetic_workspace
            with tempfile.TemporaryDirectory(prefix="ephi-u36-installed-") as temporary:
                storage = Path(temporary)
                provider_artifact_root = storage / "provider-artifacts"
                seed_report = _seed_product_fixture(postgres_dsn, provider_artifact_root)
                o9 = operations_status(dsn=postgres_dsn, artifact_root=provider_artifact_root)
                o9_report = {
                    "process_transport": o9.get("axes", {}).get("process_transport", {}),
                    "postgres_readiness_durability": o9.get("axes", {}).get("postgres_readiness_durability", {}),
                    "immutable_artifact_integrity": o9.get("axes", {}).get("immutable_artifact_integrity", {}),
                    "source_capability_freshness": o9.get("axes", {}).get("source_capability_freshness", {}),
                    "durable_worker_job_state": o9.get("axes", {}).get("durable_worker_job_state", {}),
                    "evidence_qualification_freshness": o9.get("axes", {}).get("evidence_qualification_freshness", {}),
                }
                browser_report = _browser_run(
                    postgres_dsn,
                    artifact_root,
                    storage / "browser",
                    browser,
                    provider_artifact_root,
                )
                browser_report["fixture_seed"] = seed_report
                browser_report["o9_status"] = o9_report
                browser_report["o9_recovery"] = {
                    "status": "NOT_RUN",
                    "reason_code": "EXISTING_INSTALLED_O9_RECOVERY_QUALIFIER_RUN_SEPARATELY",
                }
        except ReleaseFailure as exc:
            browser_report = {"status": "BLOCKED", "reason_code": exc.reason_code}
        except Exception:
            browser_report = {"status": "BLOCKED", "reason_code": "QUALIFICATION_FIXTURE_OR_BROWSER_UNAVAILABLE"}
    observed["browser_status"] = browser_report.get("status")
    observed["browser_reason"] = browser_report.get("reason_code", "BROWSER_QUALIFICATION_FAILED")
    observed["browser_evidence"] = browser_report
    if browser_report.get("status") != "PASS":
        failures.append({"layer": "browser", "reason_code": str(browser_report.get("reason_code", "BROWSER_QUALIFICATION_FAILED"))})

    screenshots = browser_report.get("screenshots", [])
    if not isinstance(screenshots, list):
        screenshots = []
    failure_inventory = browser_report.get("failure_inventory", {
        "console_errors": [], "page_errors": [], "failed_requests": [], "http_errors": [],
    })
    if not isinstance(failure_inventory, dict):
        failure_inventory = {"browser_failure": "FAILURE_INVENTORY_INVALID"}
    failure_path = artifact_root / "failure-inventory.json"
    _write_json(failure_path, failure_inventory)
    failure_bytes = failure_path.read_bytes()
    artifact_records = list(screenshots)
    artifact_records.append({
        "path": "failure-inventory.json",
        "kind": "failure_inventory",
        "byte_size": len(failure_bytes),
        "sha256": _sha256(failure_bytes),
    })
    artifact_records.sort(key=lambda item: str(item.get("path", "")))

    gates = _gate_matrix(observed)

    required_sections_pass = (
        observed.get("release_status") == "PASS"
        and observed.get("configuration_status") == "PASS"
        and observed.get("postgres_status") == "PASS"
        and observed.get("migration_status") == "PASS"
        and observed.get("composition_status") == "PASS"
        and observed.get("browser_status") == "PASS"
        and controls_ok
    )
    result: dict[str, object] = {
        "schema": "org.ephi.installed-synthetic-qualification-report.v1",
        "status": "PASS" if required_sections_pass else ("BLOCKED" if browser_report.get("status") == "BLOCKED" or observed.get("postgres_status") == "BLOCKED" else "FAIL"),
        "scope": "Installed synthetic integration-kit qualification only.",
        "candidate": candidate,
        "ephi_release": observed.get("ephi_release_identity", {"status": observed.get("release_status"), "reason_code": observed.get("release_reason")}),
        "installed_distributions": installed_facts or {"status": observed.get("release_status")},
        "qualification_kit": qualification_identity or {"status": "BLOCKED", "reason_code": observed.get("release_reason")},
        "downstream": observed.get("composition_evidence", {
            "status": "NOT_RUN",
            "reason_code": "DOWNSTREAM_COMPOSITION_NOT_RUN",
        }),
        "runtime_configuration": {
            "status": observed.get("configuration_status"),
            "status_code": config_report.get("status_code"),
            "contract_sha256": config_report.get("configuration_contract", {}).get("sha256"),
            "provider_load": "NOT_PERFORMED_BY_CONFIGURATION_PREFLIGHT",
        },
        "postgresql": {
            **postgres_report,
            "binding_name": "EPHI_TEST_POSTGRES_DSN" if postgres_dsn else "NOT_CONFIGURED",
            "migration": observed.get("migration_evidence"),
        },
        "o9": {
            "status_report": browser_report.get("o9_status", {"status": "NOT_RUN"}),
            "recovery": browser_report.get("o9_recovery", {"status": "NOT_RUN", "reason_code": "POSTGRESQL_OR_FIXTURE_UNAVAILABLE"}),
        },
        "browser": browser_report,
        "negative_controls_status": "PASS" if controls_ok else "FAIL",
        "negative_controls": controls,
        "exercised_product_paths": browser_report.get("paths", {}),
        "failure_inventory": failure_inventory,
        "artifacts": artifact_records,
        "explicit_nonclaims": [
            "No source identity, byte identity, algorithm equivalence, historical-test equivalence, or historical defect-fix claim.",
            "Synthetic data does not satisfy real-family G02 or G06.",
            "The empty/synthetic Outcomes route does not satisfy real independently audited G11.",
            "Local synthetic PostgreSQL/browser evidence does not satisfy production-like G10.",
            "G12, Port Gate, release promotion, company deployment readiness, and Production are not run or claimed.",
            "No company adapter, identity binding, private source row, mapping, endpoint, credential, or TLS configuration is present.",
            "The provider is selected only by the explicit qualification command environment; installing the separate wheel does not select it.",
            "Browser state is not an authorization authority; the existing operation-time current-authorization authority remains in control.",
        ],
        "gates": gates,
    }
    report_path = Path(args.report).expanduser() if args.report else artifact_root / "qualification-report.json"
    if report_path.is_symlink() or not report_path.resolve().is_relative_to(artifact_root):
        raise ReleaseFailure("REPORT_PATH_INVALID")
    _write_json(report_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs-dir", required=True, help="Prepared EPHI release-input directory.")
    parser.add_argument("--qualification-inputs-dir", required=True, help="Prepared separate qualification payload directory.")
    parser.add_argument("--artifact-dir", required=True, help="Existing or new job-owned output directory outside the repository.")
    parser.add_argument("--report", help="Optional report path inside --artifact-dir.")
    parser.add_argument("--chromium-executable", help="Explicit qualified Chromium/Chrome executable; no browser download is attempted.")
    args = parser.parse_args(argv)
    try:
        report = _qualify(args)
    except ReleaseFailure as exc:
        report = {
            "schema": "org.ephi.installed-synthetic-qualification-report.v1",
            "status": "BLOCKED",
            "reason_code": exc.reason_code,
            "gates": _gate_matrix({
                "release_status": "BLOCKED",
                "release_reason": exc.reason_code,
                "browser_status": "BLOCKED",
                "browser_reason": exc.reason_code,
            }),
            "explicit_nonclaims": [
                "No production readiness, real-family G02/G06, production-like G10, real audited G11, G12, Port Gate, release promotion, or Production claim.",
            ],
        }
    except Exception:
        report = {
            "schema": "org.ephi.installed-synthetic-qualification-report.v1",
            "status": "FAIL",
            "reason_code": "QUALIFICATION_COMMAND_FAILED",
            "gates": _gate_matrix({
                "release_status": "FAIL",
                "release_reason": "QUALIFICATION_COMMAND_FAILED",
                "browser_status": "NOT_RUN",
                "browser_reason": "QUALIFICATION_COMMAND_FAILED",
            }),
            "explicit_nonclaims": [
                "No production readiness, real-family G02/G06, production-like G10, real audited G11, G12, Port Gate, release promotion, or Production claim.",
            ],
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
