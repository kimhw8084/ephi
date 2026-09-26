"""Canonical authorized EPHI platform Operations destination."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from typing import Any

from nicegui_base import (
    AnalysisWorkspacePage,
    AppShell,
    Card,
    LayoutSlot,
    NavigationModel,
    StateKind,
    StateView,
    StateViewSpec,
    StatusBadge,
    StatusIntent,
)

from ephi.application.errors import AuthorizationDeniedError, VersionConflictError
from ephi.application.operations import OperationsCockpitSnapshot, OperationsQueryService
from ephi.application.worker import MAX_WORKER_JOB_TYPE_LENGTH, WORKER_JOB_STATUSES


_CSS = """
.ephi-operations, .ephi-operations * { box-sizing: border-box; min-width: 0; }
.ephi-operations { display: grid; gap: var(--cui-space-3); width: 100%; max-width: 100%; overflow-wrap: anywhere; }
.ephi-operations-title { margin: 0; font-size: clamp(1.65rem, 3vw, 2.45rem); line-height: 1.12; overflow-wrap: anywhere; }
.ephi-operations-note, .ephi-operations-status, .ephi-operations-truth {
  border: 1px solid var(--cui-border-subtle); border-radius: var(--cui-radius-md);
  background: var(--cui-surface-secondary); padding: var(--cui-space-3); overflow-wrap: anywhere;
}
.ephi-operations-status { min-height: 2.8rem; }
.ephi-operations-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--cui-space-3); }
.ephi-operations-axis { height: 100%; }
.ephi-operations-axis h2, .ephi-operations-section-title { margin: 0 0 var(--cui-space-2); font-size: 1.1rem; line-height: 1.35; overflow-wrap: anywhere; }
.ephi-operations-axis p { margin: var(--cui-space-1) 0; overflow-wrap: anywhere; }
.ephi-operations-facts { display: grid; gap: var(--cui-space-2); grid-template-columns: repeat(auto-fit, minmax(min(100%, 12rem), 1fr)); }
.ephi-operations-fact { padding: var(--cui-space-2); border: 1px solid var(--cui-border-subtle); border-radius: var(--cui-radius-sm); }
.ephi-operations-fact dt { color: var(--cui-text-secondary); font-size: .85rem; }
.ephi-operations-fact dd { margin: .2rem 0 0; overflow-wrap: anywhere; }
.ephi-operations-links, .ephi-operations-actions { display: flex; flex-wrap: wrap; gap: var(--cui-space-2); align-items: center; }
.ephi-operations-filter-controls { display: grid; grid-template-columns: minmax(12rem, 1fr) minmax(16rem, 2fr); gap: var(--cui-space-2); width: 100%; max-width: 52rem; }
.ephi-operations-filter-note { margin: 0; color: var(--cui-text-secondary); overflow-wrap: anywhere; }
.ephi-operations-table-wrap { width: 100%; max-width: 100%; overflow-x: auto; }
.ephi-operations-table { width: 100%; table-layout: fixed; border-collapse: collapse; }
.ephi-operations-table th, .ephi-operations-table td { padding: .65rem .5rem; border-bottom: 1px solid var(--cui-border-subtle); text-align: left; vertical-align: top; overflow-wrap: anywhere; }
.ephi-operations-table th { color: var(--cui-text-secondary); font-weight: 600; }
.ephi-operations-table details, .ephi-operations-job-card details { overflow-wrap: anywhere; }
.ephi-operations-table summary, .ephi-operations-job-card summary { cursor: pointer; min-height: 44px; display: flex; align-items: center; overflow-wrap: anywhere; }
.ephi-operations-job-cards { display: none; gap: var(--cui-space-2); }
.ephi-operations-job-card { border: 1px solid var(--cui-border-subtle); border-radius: var(--cui-radius-md); padding: var(--cui-space-2); }
.ephi-operations-job-card dl { display: grid; grid-template-columns: minmax(6.5rem, 38%) minmax(0, 1fr); gap: .4rem .7rem; margin: var(--cui-space-2) 0 0; }
.ephi-operations-job-card dt { color: var(--cui-text-secondary); }
.ephi-operations-job-card dd { margin: 0; overflow-wrap: anywhere; }
.ephi-operations-expiry { font-variant-numeric: tabular-nums; }
.ephi-operations button, .ephi-operations .cui-button, .ephi-operations .q-btn { min-height: 44px; }
.ephi-operations button:focus-visible, .ephi-operations a:focus-visible,
.ephi-operations summary:focus-visible, .ephi-operations .cui-button:focus-visible,
.ephi-operations .q-btn:focus-visible { outline: 3px solid #005ea8 !important; outline-offset: 2px !important; box-shadow: 0 0 0 3px #005ea8 !important; }
.ephi-operations .is-disabled-control { opacity: .68; }
@media (max-width: 760px) {
  .ephi-operations-grid { grid-template-columns: minmax(0, 1fr); }
  .ephi-operations-filter-controls { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 600px) {
  .ephi-operations-table-wrap { display: none; }
  .ephi-operations-job-cards { display: grid; }
}
@media (prefers-reduced-motion: reduce) { .ephi-operations *, .ephi-operations *::before { scroll-behavior: auto !important; animation: none !important; transition: none !important; } }
@media (forced-colors: active) { .ephi-operations-note, .ephi-operations-status, .ephi-operations-truth, .ephi-operations-fact, .ephi-operations-job-card { border: 1px solid CanvasText; } }
"""


_AXIS_LABELS = {
    "process_transport": "Process and transport",
    "postgres_readiness_durability": "PostgreSQL readiness and durability",
    "immutable_artifact_integrity": "Immutable artifact integrity",
    "source_capability_freshness": "Source capability and freshness",
    "durable_worker_job_state": "Durable worker and job state",
    "evidence_qualification_freshness": "Evidence and qualification freshness",
}
_INTENTS = {
    "READY": StatusIntent.SUCCESS,
    "CURRENT": StatusIntent.SUCCESS,
    "PARTIAL": StatusIntent.WARNING,
    "DEGRADED": StatusIntent.WARNING,
    "STALE": StatusIntent.WARNING,
    "PENDING": StatusIntent.WARNING,
    "NOT_QUALIFIED": StatusIntent.WARNING,
    "EXPIRED": StatusIntent.WARNING,
    "BLOCKED": StatusIntent.DANGER,
    "UNAVAILABLE": StatusIntent.DANGER,
    "ERROR": StatusIntent.DANGER,
}


def _text(value: object, fallback: str = "—") -> str:
    return fallback if value is None or value == "" else str(value)


def _axis_age(axis: dict[str, Any]) -> str:
    facts = axis.get("facts", {})
    seconds = facts.get("age_seconds") if isinstance(facts, dict) else None
    if not isinstance(seconds, int):
        return "Age unavailable"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _badge(value: str) -> None:
    StatusBadge(value, intent=_INTENTS.get(value, StatusIntent.NEUTRAL))


def _tag_text(ui: Any, tag: str, value: object, *, props: str | None = None) -> None:
    with ui.element(tag) as element:
        if props:
            element.props(props)
        ui.label(_text(value))


def _facts(ui: Any, values: tuple[tuple[str, object], ...]) -> None:
    with ui.element("dl").classes("ephi-operations-facts"):
        for label, value in values:
            with ui.element("div").classes("ephi-operations-fact"):
                _tag_text(ui, "dt", label)
                _tag_text(ui, "dd", value)


def _render_axis(ui: Any, name: str, axis: dict[str, Any]) -> None:
    anchor = f"operations-{name.replace('_', '-')}-details"
    with Card() as card:
        card.element.classes("ephi-operations-axis")
        with ui.element("div").classes("ephi-operations-links"):
            ui.label(_AXIS_LABELS[name]).classes("text-subtitle1")
            _badge(str(axis.get("state", "UNAVAILABLE")))
        ui.label(_text(axis.get("reason"), "AUTHORITY_STATE_UNAVAILABLE"))
        ui.label(f"Age · {_axis_age(axis)}").classes("text-caption")
        ui.link("View details", f"#{anchor}")


def _job_details(ui: Any, job: dict[str, Any]) -> None:
    labels = (
        ("Job ID", "job_id"), ("Type", "job_type"), ("Status", "status"),
        ("Priority", "priority"), ("Created", "created_at"), ("Available", "available_at"),
        ("Updated", "updated_at"), ("Attempts", "attempts"), ("Maximum attempts", "max_attempts"),
        ("Lease epoch", "lease_epoch"), ("Lease expiry", "lease_expires_at"),
        ("Lease classification", "lease_state"), ("Owner", "owner_reference"),
        ("Failure code", "failure_code"), ("Failure reason", "failure_reason"),
        ("Committed local-effect receipt", "committed_local_effect_receipt"),
    )
    with ui.element("dl"):
        for label, key in labels:
            _tag_text(ui, "dt", label)
            value = job.get(key)
            if key == "committed_local_effect_receipt":
                value = "Yes" if value is True else "No" if value is False else "Not provable"
            _tag_text(ui, "dd", value)


def _render_jobs(ui: Any, data: dict[str, Any]) -> None:
    jobs = data.get("jobs", [])
    if not jobs:
        ui.label("No job records are available in this authorized scope.")
        return
    if data.get("jobs_truncated"):
        ui.label("Showing the first 100 jobs in deterministic queue order. More jobs are available.").classes("ephi-operations-truth")
    with ui.element("div").classes("ephi-operations-table-wrap"):
        with ui.element("table").classes("ephi-operations-table").props('aria-label="Bounded durable worker jobs"'):
            with ui.element("thead"):
                with ui.element("tr"):
                    for label in ("Job and detail", "Status", "Priority", "Attempts", "Lease"):
                        _tag_text(ui, "th", label, props='scope="col"')
            with ui.element("tbody"):
                for job in jobs:
                    with ui.element("tr"):
                        with ui.element("td"):
                            with ui.element("details"):
                                _tag_text(ui, "summary", f"{job['job_id']} · {job['job_type']}")
                                _job_details(ui, job)
                        _tag_text(ui, "td", f"{job['status']} · {job['lease_state']}")
                        _tag_text(ui, "td", job["priority"])
                        _tag_text(ui, "td", f"{job['attempts']} / {job['max_attempts']}")
                        _tag_text(ui, "td", job["lease_expires_at"])
    with ui.element("div").classes("ephi-operations-job-cards"):
        for job in jobs:
            with ui.element("article").classes("ephi-operations-job-card"):
                with ui.element("details"):
                    _tag_text(ui, "summary", f"{job['job_id']} · {job['job_type']} · {job['status']}")
                    _job_details(ui, job)


def _render_snapshot(ui: Any, container: Any, snapshot: OperationsCockpitSnapshot) -> None:
    data = snapshot.as_dict()
    health = data["health"]["axes"]
    with container:
        with Card() as process_card:
            process_card.element.props('id="operations-process-transport-details"')
            ui.label("Process and transport detail").classes("ephi-operations-section-title")
            ui.label("READY means only that this application and Operations query path responded.")
            ui.label("It does not establish source, worker, artifact, qualification, or manufacturing/tool health.")

        postgres = health["postgres_readiness_durability"]
        with Card() as postgres_card:
            postgres_card.element.props('id="operations-postgres-readiness-durability-details"')
            ui.label("PostgreSQL readiness and durability detail").classes("ephi-operations-section-title")
            _facts(ui, (
                ("PostgreSQL server version", postgres["facts"].get("server_version")),
                ("Critical schema tables present", postgres["facts"].get("critical_table_count")),
                ("Critical schema tables required", postgres["facts"].get("required_table_count")),
                ("Critical schema tables missing", postgres["facts"].get("missing_table_count")),
                ("Declared migration files", postgres["facts"].get("migration_file_count")),
                ("Migration manifest identity", str(postgres["facts"].get("migration_manifest_sha256", "—"))[:16]),
                ("Migration ledger", postgres["facts"].get("migration_ledger_state")),
            ))
            ui.label("Schema/query readiness does not prove production durability or an SLO.").classes("ephi-operations-truth")

        with ui.element("section").classes("ephi-operations-grid").props('aria-label="Six independent platform health axes"'):
            for name in (
                "process_transport",
                "postgres_readiness_durability",
                "immutable_artifact_integrity",
                "source_capability_freshness",
                "durable_worker_job_state",
                "evidence_qualification_freshness",
            ):
                _render_axis(ui, name, health[name])

        source = data["source"]
        with Card() as source_card:
            source_card.element.props('id="operations-source-capability-freshness-details"')
            ui.label("Source freshness detail").classes("ephi-operations-section-title")
            _badge(str(source["state"]))
            ui.label(source["reason"])
            _facts(ui, (
                ("Source", source["source_id"]), ("Family", source["family_id"]),
                ("Capability", source["capability_id"]), ("Mapping version", source["mapping_version"]),
                ("Mapping identity", str(source["mapping_hash"] or "—")[:16]),
                ("Source revision", source["source_revision"]), ("Partition", source["source_partition"]),
                ("Last available cutoff", source["last_available_cutoff"]),
                ("Freshness age", f"{source['freshness_age_seconds']}s" if source["freshness_age_seconds"] is not None else "—"),
                ("Freshness limit", f"{source['freshness_limit_seconds']}s" if source["freshness_limit_seconds"] is not None else "—"),
            ))

        with Card() as worker_card:
            worker_card.element.props('id="operations-durable-worker-job-state-details"')
            ui.label("Durable worker queue and jobs").classes("ephi-operations-section-title")
            ui.label("Raw job payloads are withheld. Failure diagnostics and owner references are bounded and redacted.")
            filters = data["worker_filters"]
            status_filter = ", ".join(filters["statuses"]) if filters["statuses"] else "All statuses"
            type_filter = filters["job_type"] or "All job types"
            ui.label(f"Worker detail filters · {status_filter} · {type_filter}").classes("ephi-operations-truth")
            ui.label("Filters narrow these detail rows only. Worker health counts cover the full authorized scope.")
            _render_jobs(ui, data)

        artifacts = data["artifacts"]
        with Card() as artifact_card:
            artifact_card.element.props('id="operations-immutable-artifact-integrity-details"')
            ui.label("Artifact integrity detail").classes("ephi-operations-section-title")
            _facts(ui, (
                ("Catalog references inspected", artifacts["inspected_count"]),
                ("Missing bytes", artifacts["missing_count"]),
                ("Corrupt bytes", artifacts["corrupt_count"]),
                ("Other verification unavailable", artifacts["other_unavailable_count"]),
                ("Bound reached", "Yes" if artifacts["truncated"] else "No"),
            ))
            for reason, count in artifacts["safe_reasons"].items():
                ui.label(f"{reason}: {count}")

        qualification = data["qualification"]
        with Card() as qualification_card:
            qualification_card.element.props('id="operations-evidence-qualification-freshness-details"')
            ui.label("Qualification and evidence freshness").classes("ephi-operations-section-title")
            with ui.element("div").classes("ephi-operations-links"):
                _badge(str(qualification["state"]))
                if qualification["family_center_href"]:
                    ui.link("Open exact Family Center workspace", qualification["family_center_href"])
            ui.label(qualification["reason"])
            _facts(ui, (
                ("Family", qualification["family_id"]),
                ("Capability", qualification["capability_id"]),
                ("Release", qualification["release_id"]),
            ))
            if qualification["gates"]:
                with ui.element("div").classes("ephi-operations-grid").props('aria-label="Qualification evidence states and expirations"'):
                    for gate in qualification["gates"]:
                        with Card():
                            with ui.element("div").classes("ephi-operations-links"):
                                ui.label(gate["stage"])
                                _badge(str(gate["state"]))
                            ui.label(_text(gate["reason"]))
                            ui.label(f"Expires · {_text(gate['expires_at'])}").classes("ephi-operations-expiry")
            else:
                ui.label("No qualified evidence rows are available for this exact workspace.")

        backup = data["backup_restore"]
        with Card() as backup_card:
            backup_card.element.props('id="operations-backup-restore-details"')
            ui.label("Backup and restore status").classes("ephi-operations-section-title")
            _facts(ui, (
                ("Generic/local O9.1 rehearsal contract", backup["local_o91_rehearsal"]),
                ("Target backup evidence", backup["target_backup_evidence"]),
                ("Production RPO/RTO", backup["production_rpo_rto"]),
            ))
            ui.label("The O9.1 local rehearsal is an operator-run CLI/runbook capability. No target backup evidence is bound to this runtime.")
            ui.label("Restore execution is unavailable from browser callbacks.").classes("ephi-operations-truth")

        with Card() as controls_card:
            controls_card.element.props('id="operations-operator-controls-details"')
            ui.label("Operator controls").classes("ephi-operations-section-title")
            ui.label("No audited OperationsControl capability is bound. Consequential requests remain disabled.")
            controls = (
                ("Retry eligible job", "Would request an auditable retry through the owning worker authority. No retry request authority is bound."),
                ("Pause source/family processing", "Would pause approved source or family processing at its owner. No pause authority is bound."),
                ("Request projection repair", "Would create an auditable projection-repair request. No repair request authority is bound."),
                ("Start approved restore rehearsal", "Would request an isolated approved rehearsal. Browser restore execution is unavailable."),
            )
            for label, impact in controls:
                with ui.element("div").classes("ephi-operations-fact"):
                    ui.button(label).props("disabled aria-disabled=true").classes("is-disabled-control")
                    ui.label(impact)


def _error_state(error: BaseException) -> tuple[StateKind, str, str]:
    if isinstance(error, AuthorizationDeniedError):
        return StateKind.PERMISSION, "Operations permission required", "Current authorization does not permit this Operations request."
    if isinstance(error, VersionConflictError):
        return StateKind.ERROR, "Operations view changed", "A concurrent revision conflict prevented this read. Refresh to read current truth."
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return StateKind.OFFLINE, "Operations data offline", "The last coherent result is retained. Reconnect and refresh to read current truth."
    return StateKind.ERROR, "Operations data unavailable", "The authorized Operations query failed. Raw exception text is withheld."


async def build_operations_page(composition: Any, navigation: NavigationModel) -> None:
    """Render the authorized platform cockpit using the pinned public Base shell."""

    from nicegui import ui

    try:
        principal = composition.principal_provider()
        user_name = principal.subject
    except Exception:
        principal = None
        user_name = "Unavailable"
    with AppShell(
        "EPHI",
        navigation,
        active_route="/ephi/operations",
        environment=os.environ.get("EPHI_ENV", "development"),
        subtitle="Operations · platform",
        user_name=user_name,
        user_role="Platform operator",
        debugger=False,
    ):
        ui.add_css(_CSS, shared=True)
        ui.query("main").classes("ephi-operations").props('role="region" aria-label="EPHI platform Operations"')
        with AnalysisWorkspacePage("", None) as page:
            with page.slot(LayoutSlot.HEADER, sticky=True):
                with ui.element("h1").classes("ephi-operations-title"):
                    ui.label("EPHI platform operations")
                ui.label("Application, PostgreSQL, artifact, source, worker and qualification status.").classes("ephi-operations-note")
                ui.label("This page does not report manufacturing equipment or tool health.").classes("ephi-operations-truth")
            with page.slot(LayoutSlot.PRIMARY):
                with ui.element("div").classes("ephi-operations-status").props('role="status" aria-live="polite"') as status:
                    ui.label("Loading operational observations…")
                with ui.element("div").classes("ephi-operations-filter-controls").props('aria-label="Worker detail filters"'):
                    worker_status_filter = ui.select(
                        {status: status for status in WORKER_JOB_STATUSES},
                        value=None,
                        label="Worker status",
                        clearable=True,
                    )
                    worker_job_type_filter = ui.input("Exact worker job type", value="").props(
                        f'maxlength="{MAX_WORKER_JOB_TYPE_LENGTH}" autocomplete="off"'
                    )
                ui.label("Worker filters apply when you refresh. They do not change the six health axes.").classes("ephi-operations-filter-note")
                with ui.element("div").classes("ephi-operations-actions"):
                    refresh_button = ui.button("Refresh")
                container = ui.column().classes("ephi-operations")
                last_snapshot: OperationsCockpitSnapshot | None = None
                request_number = 0

                async def refresh(_event: Any = None) -> None:
                    nonlocal last_snapshot, request_number
                    request_number += 1
                    current_request = request_number
                    status.clear()
                    with status:
                        ui.label(
                            "Refreshing; showing the last coherent result."
                            if last_snapshot is not None
                            else "Loading operational observations…"
                        )
                    await asyncio.sleep(0)
                    try:
                        service: OperationsQueryService | None = getattr(composition, "operations", None)
                        if service is None or principal is None:
                            raise AuthorizationDeniedError("Operations query authority is unavailable")
                        selected_status = worker_status_filter.value
                        worker_statuses = (selected_status,) if selected_status else None
                        selected_job_type = worker_job_type_filter.value or None
                        result = service.read(
                            principal,
                            composition.scope_provider(),
                            worker_statuses=worker_statuses,
                            worker_job_type=selected_job_type,
                        )
                    except Exception as error:
                        if current_request != request_number:
                            return
                        kind, title, message = _error_state(error)
                        status.clear()
                        with status:
                            StateView(StateViewSpec(kind, title, message))
                            if kind is StateKind.OFFLINE:
                                ui.label("Reconnecting is pending the next refresh request.")
                        return
                    if current_request != request_number:
                        return
                    last_snapshot = result
                    status.clear()
                    with status:
                        ui.label(f"Observation refreshed at {result.observed_at}. Each authority axis is reported separately below.")
                    container.clear()
                    _render_snapshot(ui, container, result)

                refresh_button.on("click", refresh)
                ui.timer(0.05, refresh, once=True)


__all__ = ["build_operations_page"]
