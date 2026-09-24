#!/usr/bin/env python3
"""CHG-205 synthetic PostgreSQL-backed Outcomes browser qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tools.o8_browser_qualification import _fixture_module, _wait_for_port  # noqa: E402
from ephi.application.context import AccessScope, CommandContext, CurrentAuthorizationAuthority, Principal, RevisionVector  # noqa: E402
from ephi.application.episodes import EPISODE_WORKFLOW_AGGREGATE_TYPE  # noqa: E402
from ephi.application.transactions import VersionedAggregateCommandExecutor  # noqa: E402
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter  # noqa: E402
from ephi.value import (  # noqa: E402
    ClaimAttribution,
    EvidenceMaturity,
    EventPeriod,
    OutcomesService,
    ReviewDecision,
)
from ephi.value.service import (  # noqa: E402
    ESTIMATED_OPPORTUNITY,
    OBSERVED_OUTCOME,
    OPERATING_COST,
    VALIDATED_BENEFIT,
)
from examples.synthetic_downstream.outcomes import SYNTHETIC_OUTCOME_FACTS  # noqa: E402


UTC = timezone.utc
SCOPE = AccessScope("o7-browser-scope", site_id="browser-site", area_id="browser-area", family_id="o8-browser-family")
CAPABILITIES = (
    "ephi.attention.read,ephi.episode.read,ephi.episode.claim,ephi.episode.acknowledge,"
    "value.submit,value.read,value.validate"
)
INPUT_ROOTS = (ROOT / "src/ephi", ROOT / "migrations", ROOT / "tests", ROOT / "examples/synthetic_downstream")


def _sha256(value: bytes | str) -> str:
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


class _DemoClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int = 2) -> datetime:
        self.value += timedelta(seconds=seconds)
        return self.value


def _seed_database(dsn: str) -> dict[str, object]:
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        version = adapter.server_version()
        adapter.connection.execute(
            "TRUNCATE source_capability, source_snapshot, artifact_catalog, query_snapshot_row, query_snapshot, "
            "read_head, read_revision, outbox_event, audit_event, command_receipt, aggregate_state, "
            "o3_attention_projection CASCADE"
        )
        adapter.seed_aggregate(SCOPE, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-o7-browser", {"work_state": "OPEN", "owner": None}, version=0)
        adapter.seed_attention_projection(
            SCOPE,
            "episode-o7-browser",
            {
                "title": "Synthetic Outcomes browser evidence",
                "asset_id": "synthetic-outcomes-asset",
                "priority": "P2",
                "severity": "MEDIUM",
                "technical_state": "READY",
                "source_state": "READY",
                "deadline": "2026-10-15T12:00:00Z",
                "age": "1",
            },
        )
        workflow = adapter.get_aggregate(SCOPE, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-o7-browser")
        adapter.publish_current_revision(
            SCOPE,
            "episode",
            "episode-o7-browser",
            "read-o7-browser",
            RevisionVector("analysis-o7-browser", None, None, 0, None, "manifest-o7-browser"),
            {"title": "Synthetic Outcomes browser evidence", "capability_state": {"source": "READY"}},
            workflow,
        )
        claimant = Principal("synthetic-outcomes-claimant", ("value.submit", "value.read"), (SCOPE,), 1, 1)
        reviewer = Principal("o7-browser-reviewer", ("value.read", "value.validate"), (SCOPE,), 1, 1)
        current = {claimant.subject: claimant, reviewer.subject: reviewer}
        authorization = CurrentAuthorizationAuthority(lambda subject: current[subject])
        start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=6)
        clock = _DemoClock(start)
        service = OutcomesService(
            __import__("ephi.value.repository", fromlist=["OutcomeAggregateRepository"]).OutcomeAggregateRepository(adapter),
            VersionedAggregateCommandExecutor(adapter, authorization),
            authorization,
            clock=clock,
        )
        latest_known: datetime | None = None
        state_facts: dict[str, object] = {}

        def submit(
            event_key: str,
            category: str,
            amount: str,
            currency: str,
            maturity: EvidenceMaturity,
            *,
            evidence_ids: tuple[str, ...],
            attribution: ClaimAttribution = ClaimAttribution(),
            event_at: datetime | None = None,
        ) -> tuple[str, str, int]:
            nonlocal latest_known
            clock.advance()
            command_id = "o7-demo-submit-" + hashlib.sha256(event_key.encode("utf-8")).hexdigest()
            result = service.submit_claim(
                CommandContext(command_id, claimant, SCOPE, 0),
                economic_event_key=event_key,
                category=category,
                amount=amount,
                currency=currency,
                event_at=event_at or (start - timedelta(days=7)),
                owner="synthetic-value-owner",
                evidence_ids=evidence_ids,
                cost_model_identity="synthetic-demo-cost-model-v1",
                maturity=maturity,
                attribution=attribution,
                coverage_numerator=7,
                coverage_denominator=10,
            )
            latest_known = clock.value
            entry_id = result.state["value_entries"][0]["entry_id"]
            return result.state["group_id"], entry_id, result.aggregate_version

        # This claim is reviewed later. Its intermediate cutoff remains a
        # reproducible pending result even after the later approval commits.
        late_group, late_entry, late_version = submit(
            "synthetic-late-approval-browser", VALIDATED_BENEFIT, "6.00", "USD",
            EvidenceMaturity.OBSERVED, evidence_ids=("synthetic-late-approval-evidence",),
        )
        early_cutoff = clock.advance()
        clock.advance(60)
        late_review = service.review_value(
            CommandContext("o7-demo-late-review", reviewer, SCOPE, late_version),
            group_id=late_group,
            value_entry_id=late_entry,
            decision=ReviewDecision.APPROVED,
            knowledge_cutoff=early_cutoff,
            rationale="Synthetic evidence and model checked after the saved cutoff.",
        )
        state_facts["later_approval"] = {
            "group_id": late_group,
            "value_entry_id": late_entry,
            "early_cutoff": early_cutoff.isoformat(),
            "review_known_at": clock.value.isoformat(),
            "review_aggregate_version": late_review.aggregate_version,
        }

        seeded_groups: dict[str, tuple[str, str, int]] = {}
        for fact in SYNTHETIC_OUTCOME_FACTS:
            attribution = ClaimAttribution(
                episode_ids=tuple(fact.get("episode_ids", ())),
                decision_ids=tuple(fact.get("decision_ids", ())),
                action_ids=tuple(fact.get("action_ids", ())),
                contributor_ids=tuple(fact.get("contributor_ids", ())),
            )
            seeded_groups[str(fact["economic_event_key"])] = submit(
                str(fact["economic_event_key"]),
                str(fact["category"]),
                str(fact["amount"]),
                str(fact["currency"]),
                EvidenceMaturity(str(fact["maturity"])),
                evidence_ids=tuple(fact["evidence_ids"]),
                attribution=attribution,
            )

        validated_group, validated_entry, validated_version = seeded_groups["synthetic-reviewed-benefit"]
        clock.advance()
        validated = service.review_value(
            CommandContext("o7-demo-validated-review", reviewer, SCOPE, validated_version),
            group_id=validated_group,
            value_entry_id=validated_entry,
            decision=ReviewDecision.APPROVED,
            knowledge_cutoff=clock.value,
            rationale="Synthetic benefit evidence and cost-model binding reviewed.",
        )

        unvalidated_group, unvalidated_entry, unvalidated_version = submit(
            "synthetic-browser-review-action", VALIDATED_BENEFIT, "6.00", "USD",
            EvidenceMaturity.OBSERVED, evidence_ids=("synthetic-review-action-evidence",),
        )
        state_facts["review_action"] = {"group_id": unvalidated_group, "value_entry_id": unvalidated_entry}

        root_event_at = start - timedelta(days=8)
        correction_group, original_entry, correction_version = submit(
            "synthetic-period-moving-correction", VALIDATED_BENEFIT, "10.00", "USD",
            EvidenceMaturity.OBSERVED, evidence_ids=("synthetic-correction-original",), event_at=root_event_at,
        )
        cutoff_before_correction = clock.value
        clock.advance(10)
        corrected_event_at = root_event_at + timedelta(days=2)
        correction = service.record_value_revision(
            CommandContext("o7-demo-period-correction", claimant, SCOPE, correction_version),
            group_id=correction_group,
            category=VALIDATED_BENEFIT,
            amount="20.00",
            currency="USD",
            event_at=corrected_event_at,
            owner="synthetic-value-owner",
            evidence_ids=("synthetic-correction-revised",),
            cost_model_identity="synthetic-demo-cost-model-v1",
            maturity=EvidenceMaturity.OBSERVED,
            supersedes=original_entry,
        )
        query_period = EventPeriod(start - timedelta(days=30), datetime.now(UTC) + timedelta(minutes=2))
        before = service.query(reviewer, SCOPE, event_period=query_period, knowledge_cutoff=cutoff_before_correction)
        after = service.query(reviewer, SCOPE, event_period=query_period, knowledge_cutoff=clock.value)
        early_late_review = service.query(
            reviewer, SCOPE, event_period=query_period, knowledge_cutoff=early_cutoff,
        )
        state_facts["period_moving_correction"] = {
            "group_id": correction_group,
            "original_entry": original_entry,
            "corrected_entry": correction.state["value_entries"][-1]["entry_id"],
            "cutoff_before_correction": cutoff_before_correction.isoformat(),
            "before_value": str(next(row.value.amount for row in before.rows if row.group_id == correction_group)),
            "after_value": str(next(row.value.amount for row in after.rows if row.group_id == correction_group)),
            "restated": after.restated,
        }
        state_facts["later_approval"]["state_at_earlier_cutoff"] = next(
            row.state for row in early_late_review.rows if row.group_id == late_group
        )
        state_facts["later_approval"]["state_after_approval"] = next(
            row.state for row in after.rows if row.group_id == late_group
        )
        state_facts["independent_review"] = {
            "group_id": validated_group,
            "review_entry": validated_entry,
            "state": next(row.state for row in after.rows if row.group_id == validated_group),
            "aggregate_version": validated.aggregate_version,
        }
        state_facts["deduplicated_attribution"] = {
            "episode_ids": list(next(row.claim.attribution.episode_ids for row in after.rows if row.economic_event_key == "synthetic-shared-event-once")),
            "one_group_count": sum(row.economic_event_key == "synthetic-shared-event-once" for row in after.rows),
        }
        state_facts["currency_summaries"] = {item.currency: str(item.estimated_opportunity) for item in after.summaries}
        return {
            "postgres_version": version,
            "scope_id": SCOPE.scope_id,
            "episode_id": "episode-o7-browser",
            "browser_review_event": "synthetic-browser-review-action",
            "browser_review_entry": unvalidated_entry,
            "state_facts": state_facts,
            "command_receipt_count": int(adapter.connection.execute("SELECT COUNT(*) AS count FROM command_receipt WHERE aggregate_type = 'outcome_claim_group'").fetchone()["count"]),
            "audit_event_count": int(adapter.connection.execute("SELECT COUNT(*) AS count FROM audit_event WHERE aggregate_type = 'outcome_claim_group'").fetchone()["count"]),
            "outbox_event_count": int(adapter.connection.execute("SELECT COUNT(*) AS count FROM outbox_event WHERE aggregate_type = 'outcome_claim_group'").fetchone()["count"]),
        }
    finally:
        adapter.close()


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _path_only(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        return parsed.path
    except Exception:
        return "<unavailable>"


def _browser_view(base: str, *, width: int, height: int, artifacts: Path, review: bool) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    events: dict[str, list[object]] = {"console_errors": [], "page_errors": [], "request_failures": [], "http_failures": []}
    request_inventory: list[dict[str, object]] = []
    response_inventory: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, reduced_motion="reduce")
        page = context.new_page()
        page.on("console", lambda item: events["console_errors"].append({"type": item.type, "text_digest": _sha256(item.text)}) if item.type == "error" else None)
        page.on("pageerror", lambda error: events["page_errors"].append({"type": type(error).__name__, "message_digest": _sha256(str(error))}))
        page.on("request", lambda request: request_inventory.append({"method": request.method, "path": _path_only(request.url)}))
        page.on("requestfailed", lambda request: events["request_failures"].append({"method": request.method, "path": _path_only(request.url)}))
        page.on("response", lambda response: response_inventory.append({"method": response.request.method, "status": response.status, "path": _path_only(response.url)}))
        page.on("response", lambda response: events["http_failures"].append({"status": response.status, "path": _path_only(response.url)}) if response.status >= 400 else None)
        page.goto(base + "/ephi/outcomes", wait_until="domcontentloaded", timeout=30000)
        page.get_by_role("heading", name="Outcomes").wait_for(timeout=30000)
        try:
            page.get_by_text("Estimated opportunity", exact=True).first.wait_for(timeout=5000)
        except Exception as exc:
            page.screenshot(path=str(artifacts / f"outcomes-{width}x{height}-startup-error.png"), full_page=True)
            raise RuntimeError(f"OUTCOMES_RENDER_FAILED:{width}x{height}; startup screenshot captured") from exc
        page.wait_for_timeout(500)
        body = page.locator("body").inner_text()
        required = (
            "Observed operational outcome",
            "Validated benefit / net cost",
            "PENDING",
            "OBSERVED_NOT_VALIDATED",
            "VALIDATED",
            "ZERO",
            "NEGATIVE",
            "RESTATED",
            "synthetic-period-moving-correction",
            "synthetic-shared-event-once",
        )
        missing = [item for item in required if item not in body]
        page.screenshot(path=str(artifacts / f"outcomes-{width}x{height}.png"), full_page=True)
        initial_geometry = page.evaluate(
            """() => {
                const w = window.innerWidth;
                const scrollWidth = document.documentElement.scrollWidth;
                const controls = document.querySelector('.ephi-outcomes-controls');
                const action = [...document.querySelectorAll('button')].find(item => item.getAttribute('aria-label') === 'Refresh Outcomes');
                const rect = node => node ? (() => { const r = node.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; })() : null;
                return {viewport_width:w, document_scroll_width:scrollWidth, no_horizontal_overflow:scrollWidth <= w + 1, controls:rect(controls), refresh_action:rect(action)};
            }"""
        )
        keyboard = {"target_reached": False, "focus_visible": False}
        refresh = page.get_by_role("button", name="Refresh Outcomes")
        for tab_count in range(1, 81):
            page.keyboard.press("Tab")
            if refresh.evaluate("element => element === document.activeElement"):
                focus = refresh.evaluate(
                    "element => { const style = getComputedStyle(element); return {outline: style.outlineStyle, width: style.outlineWidth, focusVisible: element.matches(':focus-visible')}; }"
                )
                keyboard = {"target_reached": True, "tab_count": tab_count, "focus_visible": bool(focus["focusVisible"] or focus["width"] != "0px" or focus["outline"] != "none")}
                break

        action = {"available": False, "reachable": False, "approved": False, "earlier_cutoff_excluded": False}
        drilldown = page.get_by_label("Claim drilldown")
        review_label = "synthetic-browser-review-action · Benefit · 6.00 USD"
        drilldown.click()
        page.get_by_text(review_label, exact=True).click()
        page.get_by_role("button", name="Review evidence and sign off").wait_for(timeout=15000)
        action["available"] = True
        button = page.get_by_role("button", name="Review evidence and sign off")
        button.scroll_into_view_if_needed()
        bounds = button.bounding_box()
        action["reachable"] = bool(
            bounds and bounds["height"] >= 44 and bounds["x"] >= 0
            and bounds["x"] + bounds["width"] <= width and bounds["y"] >= 0
            and bounds["y"] + bounds["height"] <= height
        )
        if review:
            cutoff_field = page.get_by_label("Knowledge cutoff (UTC)")
            earlier_cutoff = cutoff_field.input_value()
            button.click()
            page.get_by_role("dialog").get_by_role("button", name="Approve value").click()
            page.get_by_text("Review evidence and sign off", exact=True).wait_for(timeout=15000)
            page.wait_for_timeout(500)
            drilldown = page.get_by_label("Claim drilldown")
            drilldown.click()
            page.get_by_text(review_label, exact=True).click()
            pending_text = "Review state: pending; no qualifying independent sign-off exists for this exact value revision at this cutoff."
            page.get_by_text(pending_text, exact=True).wait_for(timeout=15000)
            action["earlier_cutoff_excluded"] = True
            cutoff_time = datetime.fromisoformat(earlier_cutoff).replace(tzinfo=UTC) + timedelta(minutes=1)
            deadline = time.monotonic() + 65
            while datetime.now(UTC) < cutoff_time and time.monotonic() < deadline:
                page.wait_for_timeout(250)
            if datetime.now(UTC) >= cutoff_time:
                cutoff_field.fill(cutoff_time.strftime("%Y-%m-%dT%H:%M"))
                page.get_by_role("button", name="Refresh Outcomes").click()
                page.get_by_text(f"AS_KNOWN through {cutoff_time.isoformat()}", exact=False).wait_for(timeout=15000)
                page.wait_for_timeout(500)
                drilldown = page.get_by_label("Claim drilldown")
                drilldown.click()
                page.get_by_text(review_label, exact=True).click()
                page.get_by_text("Independent review APPROVED", exact=False).wait_for(timeout=15000)
                action["approved"] = True
            page.screenshot(path=str(artifacts / f"outcomes-{width}x{height}-reviewed.png"))

        geometry = initial_geometry
        action_occlusion = None
        signoff = page.get_by_role("button", name="Review evidence and sign off")
        if signoff.count():
            signoff.scroll_into_view_if_needed()
            action_occlusion = signoff.evaluate(
                "element => { const r=element.getBoundingClientRect(); const x=r.left+r.width/2; const y=r.top+r.height/2; const top=document.elementFromPoint(x,y); return {within_viewport:r.top>=0 && r.bottom<=innerHeight && r.left>=0 && r.right<=innerWidth, unoccluded:Boolean(top && (top===element || element.contains(top))), height:r.height}; }"
            )
        snapshot = page.locator("body").aria_snapshot()
        url_path = _path_only(page.url)
        page.close()
        context.close()
        browser.close()
    return {
        "viewport": {"width": width, "height": height},
        "route": url_path,
        "status": "PASS" if not missing and geometry["no_horizontal_overflow"] and keyboard["target_reached"] and keyboard["focus_visible"] and not any(events.values()) and action["available"] and action["reachable"] and (not review or action["approved"] and action["earlier_cutoff_excluded"]) else "FAIL",
        "required_rendered_states": {name: name not in missing for name in required},
        "missing_rendered_states": missing,
        "keyboard_focus": keyboard,
        "review_action": action,
        "action_occlusion": action_occlusion,
        "geometry": geometry,
        "accessibility_snapshot_digest": _sha256(snapshot),
        "events": events,
        "request_inventory": {"requests": request_inventory, "responses": response_inventory, "query_strings_recorded": False, "headers_or_bodies_recorded": False},
    }


def qualify(dsn: str, output: Path, artifacts: Path) -> dict[str, object]:
    output = output.resolve()
    artifacts = artifacts.resolve()
    seed = _seed_database(dsn)
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ephi-o7-browser-") as fixture_root:
        _fixture_module(Path(fixture_root) / "ephi_browser_source_fixture.py", SCOPE)
        port = _port()
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), fixture_root)),
                "EPHI_ENV": "test",
                "EPHI_HOST": "127.0.0.1",
                "EPHI_PORT": str(port),
                "EPHI_ALLOWED_BROWSER_ORIGINS": f"http://127.0.0.1:{port}",
                "NICEGUI_BASE_ROOT_PATH": "",
                "NICEGUI_BASE_PROXY_ENABLED": "false",
                "NICEGUI_BASE_TRUSTED_PROXIES": "127.0.0.1,::1",
                "EPHI_POSTGRES_DSN": dsn,
                "NICEGUI_BASE_STORAGE_SECRET": "o7-outcomes-session-secret-not-recorded",
                "NICEGUI_STORAGE_PATH": str(Path(fixture_root) / "nicegui-storage"),
                "EPHI_DEV_SCOPE_ID": SCOPE.scope_id,
                "EPHI_DEV_SITE_ID": SCOPE.site_id or "browser-site",
                "EPHI_DEV_AREA_ID": SCOPE.area_id or "browser-area",
                "EPHI_DEV_FAMILY_ID": SCOPE.family_id or "o8-browser-family",
                "EPHI_DEV_IDENTITY_SUBJECT": "o7-browser-reviewer",
                "EPHI_DEV_IDENTITY_CAPABILITIES": CAPABILITIES,
                "EPHI_DEV_AUTH_SESSION_REVISION": "1",
                "EPHI_DEV_SECURITY_REVISION": "1",
                "EPHI_METROLOGY_SOURCE_ADAPTER": "ephi_browser_source_fixture:factory",
                "EPHI_METROLOGY_SOURCE_ID": "o8-browser-source",
                "EPHI_METROLOGY_PROVIDER_ID": "o8-browser-provider",
                "EPHI_METROLOGY_FAMILY_ID": SCOPE.family_id or "o8-browser-family",
                "EPHI_METROLOGY_CAPABILITY_ID": "o8-browser-capability",
                "EPHI_METROLOGY_SCOPE_ID": SCOPE.scope_id,
                "EPHI_METROLOGY_SITE_ID": SCOPE.site_id or "browser-site",
                "EPHI_METROLOGY_AREA_ID": SCOPE.area_id or "browser-area",
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
            base = f"http://127.0.0.1:{port}"
            desktop = _browser_view(base, width=1440, height=900, artifacts=artifacts, review=True)
            mobile = _browser_view(base, width=390, height=844, artifacts=artifacts, review=False)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    screenshots = [
        {"path": item.relative_to(ROOT).as_posix(), "bytes": item.stat().st_size, "sha256": _sha256(item.read_bytes())}
        for item in sorted(artifacts.glob("*.png"))
    ]
    report = {
        "schema_version": 1,
        "project": "ephi",
        "change": "CHG-205",
        "scope": "U2.2 / O7.1 generic synthetic Outcomes and independent value review",
        "base_commit": "8eb33a205f2f589b0f1510cd00fbe45dd78094e9",
        "candidate_input_digest": _candidate_digest(),
        "synthetic_only": True,
        "postgres": {"version": seed["postgres_version"], "restart_preservation": "tests.test_outcomes_postgresql", "version_18_qualification": "NOT_RUN_LOCAL_POSTGRES_VERSION_17" if not str(seed["postgres_version"]).startswith("18.") else "PASS"},
        "synthetic_fixture_facts": seed,
        "browser": {"desktop_1440x900": desktop, "phone_390x844": mobile},
        "screenshots": screenshots,
        "security": {"secrets_recorded": False, "raw_browser_requests_recorded": False, "authorization_headers_recorded": False, "cookies_recorded": False},
        "status": "PASS" if desktop["status"] == "PASS" and mobile["status"] == "PASS" else "FAIL",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("EPHI_TEST_POSTGRES_DSN", ""))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("EPHI_TEST_POSTGRES_DSN is required")
    report = qualify(args.dsn, args.output, args.artifacts)
    print(json.dumps({"status": report["status"], "output": str(args.output), "postgres": report["postgres"]["version"]}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
