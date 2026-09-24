#!/usr/bin/env python3
"""Seed and qualify the synthetic U2.1 Episode investigation through U1.

The fixture uses the actual downstream provider ABI, PostgreSQL-backed
application, and browser server. All manufacturing-like facts are fictional.
"""

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
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    COMPARABLE_PROFILE_KEY,
    CommandContext,
    HistoricalSourceIdentity,
    InvestigationProfile,
    RcaCurrentFacts,
    RcaQuery,
    RevisionVector,
    investigation_policy_identity,
)
from ephi.application.storage import AggregateSnapshot  # noqa: E402
from ephi.config import RuntimeSettings  # noqa: E402
from ephi.downstream import compose_downstream, load_provider_bundle  # noqa: E402
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402
from examples.synthetic_downstream.flagship import (  # noqa: E402
    CONTEXT_ID,
    FAMILY_ID,
    SYNTHETIC_DISCLAIMER,
    SYNTHETIC_EVENT_END,
    SYNTHETIC_EVENT_START,
    SYNTHETIC_SOURCE_AVAILABLE,
    TARGET_ID,
    comparable_profile,
    flagship_investigation_payload,
    invalid_control_payload,
)


ENTRYPOINT = "examples.synthetic_downstream.provider:build_flagship_bundle"
EPISODE_ID = "episode-synthetic-cd-sem-12"
HISTORY_A_ID = "episode-synthetic-case-peer-path-a"
HISTORY_B_ID = "episode-synthetic-case-superficial-b"
TITLE = "Synthetic CD excursion · CD-SEM 12 · Recipe R47 · +3.8 nm"
CURRENT_FEATURES = {
    "recipe-regime": "R47-synthetic",
    "trajectory-shape": "head-shift-pattern",
    "maintenance-regime": "pre-service-window",
    "tool-class": "cd-sem-class-a",
}
FORBIDDEN_MARKERS = (
    "SYNTHETIC_DSN_MARKER", "SYNTHETIC_PASSWORD_MARKER", "SYNTHETIC_TOKEN_MARKER",
    "SYNTHETIC_PRIVATE_ENDPOINT_MARKER", "SYNTHETIC_RAW_ROW_MARKER", "SYNTHETIC_PRIVATE_MAPPING_MARKER",
)


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _insert_source(adapter: PostgreSQLReferenceTransactionAdapter, scope: AccessScope, suffix: str) -> HistoricalSourceIdentity:
    snapshot_id = f"synthetic-cd-source-{suffix}"
    source_revision = f"synthetic-cd-revision-{suffix}-v1"
    manifest_hash = _sha(f"synthetic-manifest:{snapshot_id}")
    artifact_sha256 = _sha(f"synthetic-artifact:{snapshot_id}")
    now = datetime.now(timezone.utc) - timedelta(seconds=1)
    adapter.connection.execute(
        """
        INSERT INTO source_snapshot(
            snapshot_id, scope_key, source_id, provider_id, family_id, capability_id,
            adapter_id, schema_id, mapping_version, mapping_hash, unit,
            required_identifiers_json, source_partition, source_revision,
            event_start, event_end, available_cutoff,
            manifest_artifact_sha256, manifest_artifact_byte_size, manifest_artifact_object_key,
            row_count, status, manifest_hash, ingested_at, published_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            snapshot_id, scope.canonical_key, "synthetic-cd-source", "synthetic-cd-provider",
            FAMILY_ID, "synthetic-cd-measurement", "synthetic-cd-observer",
            "synthetic-cd-canonical-observation.v1", "1.0.0", _sha("synthetic-cd-mapping"), "nm",
            '["asset_id","context_id","characteristic_id"]', f"synthetic-partition-{suffix}", source_revision,
            SYNTHETIC_EVENT_START, SYNTHETIC_EVENT_END, SYNTHETIC_SOURCE_AVAILABLE,
            artifact_sha256, 512, f"sha256/{artifact_sha256}", 6, "PUBLISHED", manifest_hash, now, now,
        ),
    )
    return HistoricalSourceIdentity(snapshot_id, source_revision, manifest_hash, artifact_sha256)


def _seed_downstream_postgresql(dsn: str) -> dict[str, object]:
    bundle = load_provider_bundle(ENTRYPOINT)
    composition = compose_downstream(bundle, runtime_settings=RuntimeSettings(environment="test"))
    try:
        adapter = composition.adapter
        postgres_version = adapter.server_version()
        if not postgres_version.startswith("18."):
            raise RuntimeError("REAL_POSTGRESQL_18_REQUIRED")
        adapter.connection.execute(
            "TRUNCATE handoff_delivery_attempt, handoff_delivery_status, handoff_intent, decision_snapshot, "
            "source_capability, source_snapshot, artifact_catalog, o3_attention_projection, query_snapshot_row, "
            "query_snapshot, read_head, read_revision, applied_effect, job, outbox_event, audit_event, "
            "command_receipt, aggregate_state CASCADE"
        )
        scope = composition.scope_provider()
        if scope.family_id != FAMILY_ID:
            raise RuntimeError("FLAGSHIP_SCOPE_FAMILY_MISMATCH")
        current_source = _insert_source(adapter, scope, "affected-and-controls")
        case_a_source = _insert_source(adapter, scope, "history-a")
        case_b_source = _insert_source(adapter, scope, "history-b")

        current_fingerprint = comparable_profile(current_source, feature_values=CURRENT_FEATURES, case_identity="current")
        historical = (
            (
                HISTORY_A_ID,
                "synthetic-analysis-case-a",
                "synthetic-history-case-a-revision",
                case_a_source,
                {
                    "recipe-regime": "R47-synthetic",
                    "trajectory-shape": "head-shift-pattern",
                    "maintenance-regime": "pre-service-window",
                    "tool-class": "cd-sem-class-b",
                },
                ("ONE_TOOL_CLASS_DIFFERENCE",),
            ),
            (
                HISTORY_B_ID,
                "synthetic-analysis-case-b",
                "synthetic-history-case-b-revision",
                case_b_source,
                {
                    "recipe-regime": "R47-synthetic",
                    "trajectory-shape": "tool-wide-shift-pattern",
                    "maintenance-regime": "post-service-window",
                    "tool-class": "cd-sem-class-b",
                },
                ("TRAJECTORY_AND_MAINTENANCE_REGIME_DIFFER",),
            ),
        )
        for episode_id, analysis_id, revision_id, source, features, limitations in historical:
            candidate_profile = comparable_profile(
                source, feature_values=features, case_identity=episode_id, limitations=limitations
            )
            workflow = AggregateSnapshot(
                scope.canonical_key,
                "episode_workflow",
                episode_id,
                1,
                {"work_state": "CLOSED", "decision_loop": {"active_cycle_id": f"historical-cycle-{episode_id}"}},
            )
            adapter.publish_current_revision(
                scope, "episode", episode_id, revision_id,
                RevisionVector(analysis_id, None, None, 1, None, f"synthetic-qualification-{episode_id}"),
                {"episode_id": episode_id, "comparable_case": candidate_profile}, workflow,
            )

        adapter.seed_aggregate(
            scope, "episode_workflow", EPISODE_ID, {"work_state": "OPEN", "owner": None}, version=0,
        )
        adapter.seed_attention_projection(
            scope, EPISODE_ID,
            {
                "title": TITLE,
                "asset_id": TARGET_ID,
                "priority": "P2",
                "severity": "MEDIUM",
                "technical_state": "READY",
                "source_state": "SYNTHETIC_QUALIFIED",
                "deadline": None,
                "age": "1",
            },
        )
        before_vector = RevisionVector("synthetic-cd-analysis-v1", None, None, 0, None, "synthetic-cd-manifest-v1")
        init_context = CommandContext(
            "u2-synthetic-init-decision-loop", composition.principal_provider(), scope, 0, before_vector,
            "Initialize the synthetic U2.1 qualification Episode workflow",
        )
        composition.decision_loop.initialize_decision_loop(init_context, EPISODE_ID, cycle_id="synthetic-cd-cycle-1")
        current_workflow = adapter.get_aggregate(scope, "episode_workflow", EPISODE_ID)
        if current_workflow is None or current_workflow.version != 1:
            raise RuntimeError("FLAGSHIP_WORKFLOW_INITIALIZATION_FAILED")
        revision_vector = RevisionVector(
            "synthetic-cd-analysis-v1", None, None, current_workflow.version, None, "synthetic-cd-manifest-v1"
        )
        policy_identity = investigation_policy_identity(
            composition.policy_configuration.planner_policy,
            composition.policy_configuration.check_catalog,
        )
        profile = flagship_investigation_payload(
            episode_id=EPISODE_ID,
            revision_vector=revision_vector,
            cycle_id="synthetic-cd-cycle-1",
            source=current_source,
            comparables=current_fingerprint,
            policy_identity=policy_identity,
        )
        adapter.publish_current_revision(
            scope,
            "episode",
            EPISODE_ID,
            "synthetic-cd-analysis-revision-v1",
            revision_vector,
            {
                "episode_id": EPISODE_ID,
                "title": TITLE,
                "analytical_revision": "synthetic-cd-analysis-v1",
                "capability_state": {
                    "source": "SYNTHETIC_QUALIFIED",
                    "peer_reference": "QUALIFIED_SYNTHETIC",
                    "exposure": "UNAVAILABLE_NOT_QUALIFIED",
                },
                COMPARABLE_PROFILE_KEY: profile[COMPARABLE_PROFILE_KEY],
                "investigation_profile": profile,
            },
            current_workflow,
        )
        investigation_started = time.perf_counter()
        investigation = composition.episode_investigations.get_episode_investigation(
            composition.principal_provider(), scope, EPISODE_ID
        )
        investigation_query_ms = round((time.perf_counter() - investigation_started) * 1000, 3)
        if investigation.planner.state.value != "READY" or investigation.comparable_history.state.value != "READY" or investigation.rca.state.value != "READY":
            raise RuntimeError("FLAGSHIP_INVESTIGATION_COMPONENT_NOT_READY:" + json.dumps({
                "planner": [investigation.planner.state.value, list(investigation.planner.reason_codes)],
                "history": [investigation.comparable_history.state.value, list(investigation.comparable_history.reason_codes)],
                "rca": [investigation.rca.state.value, list(investigation.rca.reason_codes)],
            }, sort_keys=True))
        plan = investigation.planner.value
        history_page = investigation.comparable_history.value
        rca_result = investigation.rca.value

        invalid_raw = invalid_control_payload(
            episode_id=EPISODE_ID,
            revision_vector=revision_vector,
            cycle_id="synthetic-cd-cycle-1",
            source=current_source,
            comparables=current_fingerprint,
            policy_identity=policy_identity,
        )
        invalid_profile = InvestigationProfile.from_payload(invalid_raw, revision_known_at=investigation.known_at)
        invalid_dataset = invalid_profile.rca_dataset
        invalid_sources = tuple(sorted({
            current_source.snapshot_id,
            *(item.source_identity for item in invalid_dataset.evidence),
            *(item.source_identity for item in invalid_dataset.temporal_facts),
            *(item.source_identity for item in invalid_dataset.cohorts),
            "ephi.source-binding.v1:" + _sha(json.dumps(current_source.as_dict(), sort_keys=True, separators=(",", ":"))),
        }))
        invalid_query = RcaQuery(
            scope, EPISODE_ID, investigation.revision_id, investigation.revision_vector.workflow_version,
            investigation.active_cycle_id, investigation.known_at, invalid_sources,
            invalid_dataset.policy_identity, invalid_dataset.schema_identity,
        )
        invalid_facts = RcaCurrentFacts(
            invalid_query.episode_identity, invalid_query.analytical_revision_identity,
            invalid_query.workflow_version, invalid_query.active_cycle_identity, invalid_query.knowledge_cutoff,
            invalid_query.source_identities, invalid_query.policy_identity, invalid_query.schema_identity,
            invalid_dataset,
        )
        invalid_result = composition.episode_investigations.rca_service.analyze(
            composition.principal_provider(), invalid_query, load_current_facts=lambda: invalid_facts,
        )
        qualification = {
            "diagnostic_timing_ms": {"coherent_investigation_query": investigation_query_ms},
            "coherent_view": {
                "revision_identity": investigation.revision_id,
                "workflow_version": investigation.revision_vector.workflow_version,
                "active_cycle_identity": investigation.active_cycle_id,
                "known_at": investigation.known_at.isoformat(),
            },
            "planner": {
                "state": investigation.planner.state.value,
                "plan_identity": plan.plan_identity,
                "ranking_kind": plan.ranking_kind,
                "recommendations": [
                    {"rank": item.rank, "template_id": item.template_id, "title": item.title,
                     "execution_mode": item.execution_mode, "alternatives_discriminated": [
                         {key: (float(value) if key == "ordinal_discrimination" else value) for key, value in alternative.items()}
                         for alternative in item.alternatives_discriminated
                     ],
                     "will_not_resolve": list(item.will_not_resolve)}
                    for item in plan.recommendations
                ],
                "excluded_checks": [item.as_dict() for item in plan.excluded_checks],
            },
            "comparable_history": {
                "state": investigation.comparable_history.state.value,
                "query_identity": history_page.query_identity,
                "result_identity": history_page.result_identity,
                "cases": [
                    {"episode_id": case.episode_id, "similarities": list(case.similarity_components.shared_exact_feature_ids),
                     "differences": list(case.similarity_components.differing_feature_ids),
                     "current_only": list(case.similarity_components.current_only_feature_ids),
                     "candidate_only": list(case.similarity_components.candidate_only_feature_ids),
                     "limitations": list(case.data_completeness_limitations)}
                    for case in history_page.cases
                ],
            },
            "rca": {
                "query_identity": rca_result.query_identity,
                "input_identity": rca_result.input_identity,
                "result_identity": rca_result.result_identity,
                "schema_identity": rca_result.schema_identity,
                "policy_identity": rca_result.policy_identity,
                "source_identities": list(rca_result.source_identities),
                "knowledge_cutoff": rca_result.knowledge_cutoff.isoformat(),
                "state": rca_result.state.value,
                "control_quality": rca_result.control_quality.value,
                "affected_independent_group_count": rca_result.affected_independent_group_count,
                "control_independent_group_count": rca_result.control_independent_group_count,
                "associations": [item.as_dict() for item in rca_result.associations],
                "temporal_contradictions": [item.as_dict() for item in rca_result.temporal_contradictions],
                "limitations": list(rca_result.limitation_codes),
            },
                "synthetic_control_matrix": {
                "valid": {"state": rca_result.state.value, "control_quality": rca_result.control_quality.value,
                          "association_count": len(rca_result.associations)},
                "invalid": {"query_identity": invalid_result.query_identity, "input_identity": invalid_result.input_identity,
                            "result_identity": invalid_result.result_identity, "state": invalid_result.state.value,
                            "control_quality": invalid_result.control_quality.value,
                            "association_count": len(invalid_result.associations), "reason_codes": list(invalid_result.reason_codes)},
            },
        }
        return {
            "postgres_version": postgres_version,
            "scope_id": scope.scope_id,
            "scope": scope.as_dict(),
            "subject": composition.principal_provider().subject,
            "episode_id": EPISODE_ID,
            "history_episode_ids": [HISTORY_A_ID, HISTORY_B_ID],
            "workflow_state": str(current_workflow.state.get("work_state", "OPEN")),
            "active_cycle_id": "synthetic-cd-cycle-1",
            "revision_id": "synthetic-cd-analysis-revision-v1",
            "revision_vector": revision_vector.as_dict(),
            "policy_identity": policy_identity,
            "fixture_disclaimer": SYNTHETIC_DISCLAIMER,
            "source_snapshot_count": 3,
            "investigation_qualification": qualification,
        }
    finally:
        composition.close()


def _server_environment(
    dsn: str,
    port: int,
    storage_path: Path,
    artifact_root: Path,
    *,
    direct_episode_id: str | None = None,
) -> dict[str, str]:
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("EPHI_DEV_") or key.startswith("EPHI_METROLOGY_"):
            environment.pop(key, None)
    environment.update({
        "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT), environment.get("PYTHONPATH", ""))),
        "EPHI_ENV": "test",
        "EPHI_HOST": "127.0.0.1",
        "EPHI_PORT": str(port),
        "EPHI_ALLOWED_BROWSER_ORIGINS": f"http://127.0.0.1:{port}",
        "EPHI_TEST_POSTGRES_DSN": dsn,
        "EPHI_DOWNSTREAM_ENTRYPOINT": ENTRYPOINT,
        "EPHI_SYNTHETIC_SUBJECT": "synthetic-engineer",
        "EPHI_SYNTHETIC_SCOPE_ID": "synthetic-cd-investigation-scope",
        "EPHI_SYNTHETIC_ARTIFACT_ROOT": str(artifact_root),
        "NICEGUI_BASE_ROOT_PATH": "",
        "NICEGUI_BASE_PROXY_ENABLED": "false",
        "NICEGUI_BASE_TRUSTED_PROXIES": "127.0.0.1,::1",
        "NICEGUI_BASE_STORAGE_SECRET": secrets.token_urlsafe(36),
        "NICEGUI_STORAGE_PATH": str(storage_path),
    })
    if direct_episode_id is not None:
        environment["EPHI_TEST_SELECTED_EPISODE_ID"] = direct_episode_id
    return environment


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


def _positive_browser_path(dsn: str, seed: dict[str, object], artifact_dir: Path, *, mobile: bool) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    port = _port()
    base = f"http://127.0.0.1:{port}"
    events: dict[str, list[dict[str, object]]] = {
        "console_errors": [], "page_errors": [], "request_failures": [], "http_errors": [], "websockets": [],
    }
    width, height = (390, 844) if mobile else (1440, 900)
    with tempfile.TemporaryDirectory(prefix="ephi-u2-investigation-") as temporary:
        temp = Path(temporary)
        environment = _server_environment(
            dsn, port, temp / "nicegui-storage", temp / "artifacts",
            direct_episode_id=str(seed["episode_id"]) if mobile else None,
        )
        log_path = temp / "server.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [sys.executable, "-m", "ephi", "--serve"], cwd=ROOT, env=environment,
                stdout=log_file, stderr=subprocess.STDOUT, text=True,
            )
        try:
            _wait_for_process_or_port(port, process)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": width, "height": height}, device_scale_factor=1,
                    is_mobile=mobile, has_touch=mobile, reduced_motion="reduce",
                )
                page = context.new_page()
                page.on("console", lambda msg: events["console_errors"].append({"digest": _sha(msg.text)}) if msg.type == "error" else None)
                page.on("pageerror", lambda error: events["page_errors"].append({"digest": _sha(str(error))}))
                page.on("requestfailed", lambda request: events["request_failures"].append({"path": urlsplit(request.url).path, "failure_type": "request_failed"}))
                page.on("response", lambda response: events["http_errors"].append({"path": urlsplit(response.url).path, "status": response.status}) if response.status >= 400 else None)
                page.on("websocket", lambda websocket: events["websockets"].append({"path": urlsplit(websocket.url).path}))
                if mobile:
                    render_started = time.perf_counter()
                    response = page.goto(base + "/episode", wait_until="domcontentloaded", timeout=30000)
                    if response is None or response.status != 200:
                        raise RuntimeError("EPISODE_HTTP_STATUS")
                else:
                    render_started = time.perf_counter()
                    response = page.goto(base + "/", wait_until="domcontentloaded", timeout=30000)
                    if response is None or response.status != 200:
                        raise RuntimeError("ATTENTION_HTTP_STATUS")
                    page.get_by_role("heading", name="Attention", exact=True).wait_for(timeout=30000)
                    page.get_by_text(str(seed["episode_id"]), exact=True).first.wait_for(timeout=30000)
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(artifact_dir / "attention-desktop-1440x900.png"))
                    first_cell = page.locator(".cui-data-table .ag-center-cols-container .ag-row").first.locator(".ag-cell").first
                    first_cell.focus()
                    page.keyboard.press("Space")
                    page.get_by_role("button", name="Open episode").click()
                page.get_by_role("heading", name="Episode investigation workspace", exact=True).wait_for(timeout=30000)
                page.get_by_text("CD-SEM 12 · Recipe R47 · Mean CD shifted +3.8 nm starting 14:32", exact=True).first.wait_for(timeout=30000)
                page.get_by_role("heading", name="Investigation plan", exact=True).wait_for(timeout=30000)
                page.get_by_role("heading", name="Comparable cases", exact=True).wait_for(timeout=30000)
                page.get_by_role("heading", name="Bounded RCA · observational only", exact=True).wait_for(timeout=30000)
                page.get_by_role("heading", name="Current work and recovery", exact=True).wait_for(timeout=30000)
                render_ready_ms = round((time.perf_counter() - render_started) * 1000, 3)
                artifact_dir.mkdir(parents=True, exist_ok=True)
                screenshot = artifact_dir / ("episode-mobile-390x844.png" if mobile else "episode-desktop-1440x900.png")
                page.screenshot(path=str(screenshot))
                geometry = page.evaluate("""() => ({
                    viewportWidth: innerWidth,
                    documentWidth: document.documentElement.scrollWidth,
                    documentHeight: document.documentElement.scrollHeight,
                    overflowX: document.documentElement.scrollWidth > innerWidth,
                    scrollY: scrollY,
                    episodeSurface: (() => {
                        const r=document.querySelector('.ephi-o10-episode-surface')?.getBoundingClientRect();
                        return r ? {x:r.x, y:r.y, width:r.width, height:r.height} : null;
                    })(),
                    headings: [...document.querySelectorAll('h1,h2,h3')].map(x => x.innerText),
                    buttons: [...document.querySelectorAll('.ephi-o10-episode-surface button')].map(x => {
                        const r=x.getBoundingClientRect(); return {name:x.innerText, width:r.width, height:r.height, visible:r.width>0 && r.height>0};
                    }),
                    primaryZones: Object.fromEntries(['Investigation plan', 'Current decision', 'Evidence and competing explanations', 'Comparable cases', 'Bounded RCA · observational only', 'Current work and recovery'].map(title => {
                        const heading=[...document.querySelectorAll('h2,h3')].find(x => x.innerText === title);
                        const r=heading?.getBoundingClientRect();
                        return [title, r ? {top:r.top, bottom:r.bottom, visible:r.bottom > 0 && r.top < innerHeight} : null];
                    }))
                })""")
                if geometry["overflowX"]:
                    raise RuntimeError("EPISODE_HORIZONTAL_OVERFLOW")
                if geometry["documentHeight"] > 12000:
                    raise RuntimeError("EPISODE_DOCUMENT_GEOMETRY_UNBOUNDED")
                if geometry["episodeSurface"] is None or geometry["episodeSurface"]["width"] < (320 if mobile else 700):
                    raise RuntimeError("EPISODE_SURFACE_WIDTH_REGRESSION")
                if any(item["width"] < 44 or item["height"] < 44 for item in geometry["buttons"] if item["name"] and item["visible"]):
                    raise RuntimeError("EPISODE_ACTION_TARGET_BELOW_44PX")
                request_button = page.get_by_role("button", name="Request check", exact=True)
                headline = page.get_by_text("CD-SEM 12 · Recipe R47 · Mean CD shifted +3.8 nm starting 14:32", exact=True).first
                headline_box = headline.bounding_box()
                if headline_box is None or headline_box["y"] >= height:
                    raise RuntimeError("ENGINEERING_HEADLINE_NOT_VISIBLE_AT_INITIAL_VIEWPORT")
                if mobile:
                    request_button.scroll_into_view_if_needed()
                request_box = request_button.bounding_box()
                if request_box is None or request_box["height"] < 44 or request_box["width"] < 44:
                    raise RuntimeError("PLANNER_ACTION_TARGET_GEOMETRY")
                if not mobile and request_box["y"] + request_box["height"] > height:
                    raise RuntimeError("PRIMARY_PLAN_ACTION_BELOW_INITIAL_VIEWPORT")
                mobile_action_visible_after_scroll = page.evaluate("""() => {
                    const el=[...document.querySelectorAll('button')].find(x => x.innerText.trim() === 'Request check');
                    if (!el) return false;
                    const r=el.getBoundingClientRect();
                    const hit=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);
                    return r.top >= 0 && r.bottom <= innerHeight && (hit === el || el.contains(hit));
                }""") if mobile else None
                if mobile and not mobile_action_visible_after_scroll:
                    raise RuntimeError("MOBILE_PLANNER_ACTION_NOT_HIT_TESTABLE")
                if not mobile:
                    request_button.focus()
                    page.keyboard.press("Enter")
                    commit_message = page.get_by_text("Check request committed", exact=False)
                    commit_message.wait_for(timeout=30000)
                    page.get_by_text("REQUESTED", exact=False).first.wait_for(timeout=30000)
                    try:
                        page.wait_for_function(
                            "document.activeElement?.getAttribute('role') === 'status'",
                            timeout=5000,
                        )
                    except Exception as error:
                        diagnostic = page.evaluate("""() => ({
                            active: document.activeElement?.outerHTML?.slice(0, 500),
                            activeRole: document.activeElement?.getAttribute('role'),
                            statusTargets: [...document.querySelectorAll('[data-ephi-focus-target="status"]')].map(x => ({tag:x.tagName, role:x.getAttribute('role'), tabIndex:x.tabIndex, text:x.innerText, connected:x.isConnected})),
                            focusTargets: [...document.querySelectorAll('[data-ephi-focus-target]')].map(x => ({marker:x.getAttribute('data-ephi-focus-target'), tag:x.tagName, role:x.getAttribute('role'), tabIndex:x.tabIndex, text:x.innerText?.slice(0, 80)}))
                        })""")
                        raise RuntimeError(f"POST_ACTION_STATUS_FOCUS_FAILED: {json.dumps(diagnostic, sort_keys=True)}") from error
                    post_action_focus = page.evaluate("""() => ({tag: document.activeElement?.tagName, role: document.activeElement?.getAttribute('role'), text: document.activeElement?.innerText})""")
                    scope = AccessScope(
                        seed["scope"]["scope_id"], seed["scope"].get("site_id"), seed["scope"].get("area_id"),
                        seed["scope"].get("family_id"), tuple(seed["scope"].get("project_ids", ())),
                    )
                    command_identity = {
                        "action": "RequestCheck:synthetic-peer-reference-remeasure",
                        "episode_id": seed["episode_id"],
                        "revision_id": seed["revision_id"],
                        "revision_vector": seed["revision_vector"],
                        "scope": scope.canonical_key,
                        "subject": seed["subject"],
                    }
                    command_id = _sha(json.dumps(command_identity, sort_keys=True, separators=(",", ":")))
                    import psycopg

                    with psycopg.connect(dsn) as connection:
                        receipt = connection.execute(
                            "SELECT status, result_identity, aggregate_version FROM command_receipt WHERE scope_key=%s AND subject=%s AND command_id=%s",
                            (scope.canonical_key, seed["subject"], command_id),
                        ).fetchone()
                        durable_workflow = connection.execute(
                            "SELECT version, state_json FROM aggregate_state WHERE scope_key=%s AND aggregate_type='episode_workflow' AND aggregate_id=%s",
                            (scope.canonical_key, seed["episode_id"]),
                        ).fetchone()
                    if receipt is None or durable_workflow is None:
                        raise RuntimeError("O5_CHECK_RECEIPT_OR_WORKFLOW_NOT_DURABLE")
                    workflow_version, workflow_state = durable_workflow
                    cycle = next(item for item in workflow_state["decision_loop"]["cycles"] if item["cycle_id"] == seed["active_cycle_id"])
                    check_records = tuple(cycle["checks"].values())
                    request_state = next((item["status"] for item in check_records if item["template_id"] == "synthetic-peer-reference-remeasure"), None)
                    if workflow_version != receipt[2] or request_state != "REQUESTED":
                        raise RuntimeError("O5_CHECK_RECEIPT_WORKFLOW_VERSION_OR_STATE_MISMATCH")
                    o5_receipt_facts = {
                        "command_id": command_id,
                        "receipt_status": receipt[0],
                        "receipt_result_identity": receipt[1],
                        "workflow_version": workflow_version,
                        "check_template_id": "synthetic-peer-reference-remeasure",
                        "check_status": request_state,
                        "durably_committed": True,
                    }
                    page.screenshot(path=str(artifact_dir / "episode-desktop-after-check-1440x900.png"))
                    page.get_by_role("heading", name="Current work and recovery", exact=True).scroll_into_view_if_needed()
                    page.screenshot(path=str(artifact_dir / "episode-desktop-work-after-check-1440x900.png"))
                    page.get_by_role("button", name="Return to Attention", exact=True).focus()
                    page.keyboard.press("Enter")
                    page.get_by_role("heading", name="Attention", exact=True).wait_for(timeout=30000)
                else:
                    post_action_focus = None
                    o5_receipt_facts = None
                if any(events[key] for key in ("console_errors", "page_errors", "request_failures", "http_errors")):
                    raise RuntimeError("FLAGSHIP_BROWSER_EVENT_FAILURES")
                browser.close()
                return {
                    "viewport": {"width": width, "height": height},
                    "direct_selected_episode_path": "server-bound selected Episode → current downstream composition" if mobile else "Attention selected Episode → current downstream composition",
                    "launch_environment": {
                        "actual_ephi_server": True,
                        "downstream_entrypoint": environment.get("EPHI_DOWNSTREAM_ENTRYPOINT"),
                        "development_or_legacy_source_bindings_present": any(
                            key.startswith(("EPHI_DEV_", "EPHI_METROLOGY_")) for key in environment
                        ),
                        "dsn_storage_secret_and_artifact_paths": "present as process inputs; values redacted",
                    },
                    "screenshot": screenshot.name,
                    "geometry": geometry,
                    "engineering_headline_geometry": headline_box,
                    "planner_action_geometry": request_box,
                    "mobile_action_visible_after_scroll": mobile_action_visible_after_scroll,
                    "post_action_focus": post_action_focus,
                    "o5_receipt_facts": o5_receipt_facts,
                    "events": events,
                    "browser_action_flow": "PLANNER_TO_O5_REQUEST_AND_RETURN" if not mobile else "DIRECT_EPISODE_CONTENT_AND_GEOMETRY",
                    "diagnostic_timing_ms": {"application_render_ready": render_ready_ms},
                }
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("EPHI_TEST_POSTGRES_DSN", ""))
    parser.add_argument("--output", type=Path, default=ROOT / "evidence" / "u2" / "chg-189-u2.1-qualification.json")
    parser.add_argument("--screenshots", type=Path, default=ROOT / "evidence" / "u2" / "chg-189-screenshots")
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()
    if not arguments.dsn:
        parser.error("--dsn or EPHI_TEST_POSTGRES_DSN is required")
    os.environ["EPHI_ENV"] = "test"
    seed = _seed_downstream_postgresql(arguments.dsn)
    browser_results: list[dict[str, object]] = []
    if not arguments.no_browser:
        browser_results.append(_positive_browser_path(arguments.dsn, seed, arguments.screenshots, mobile=True))
        browser_results.append(_positive_browser_path(arguments.dsn, seed, arguments.screenshots, mobile=False))
    result = {
        "schema": "ephi.u2.1-qualification.v1",
        "change": "CHG-189",
        "wave": "U2.1",
        "objective": "O6.3",
        "tested_candidate_sha": _git("rev-parse", "HEAD"),
        "tested_candidate_tree": _git("rev-parse", "HEAD^{tree}"),
        "branch": _git("branch", "--show-current"),
        "base_sha": "a9702ad3f990f379f902ebe43934e0d5fe589b7e",
        "downstream_entrypoint": ENTRYPOINT,
        "fixture": seed,
        "browser": browser_results,
        "limitations": [
            "All fixture facts are synthetic demonstration data, not production science or company limits.",
            "This evidence does not establish O6/W4 completion, G10 capacity, G02/G06 real-family qualification, O5 W3 pilot completion, Port Gate or Production readiness.",
        ],
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(arguments.output), "fixture": seed, "browser_runs": len(browser_results)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
