"""Canonical Family Center page composed from the pinned public NiceGUI Base."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from nicegui_base import (
    ActionButton,
    AnalysisWorkspacePage,
    AppShell,
    ButtonIntent,
    Card,
    LayoutSlot,
    NavigationModel,
    StateKind,
    StateView,
    StateViewSpec,
    StatusBadge,
    StatusIntent,
)

from ephi.application import (
    AggregateNotFoundError,
    AuthorizationDeniedError,
    CommandContext,
    FAMILY_CENTER_PROMOTE,
    FAMILY_CENTER_READ,
    GateState,
    VersionConflictError,
)


_CSS = """
.ephi-family-center, .ephi-family-center * { min-width: 0; box-sizing: border-box; }
.ephi-family-title { margin: 0; font-size: clamp(1.65rem, 3vw, 2.35rem); line-height: 1.12; overflow-wrap: anywhere; }
.ephi-family-warning, .ephi-family-truth, .ephi-family-facts, .ephi-family-blockers {
  border: 1px solid var(--cui-border-subtle); border-radius: var(--cui-radius-md);
  background: var(--cui-surface-secondary); padding: var(--cui-space-3); overflow-wrap: anywhere;
}
.ephi-family-truth { font-weight: 600; }
.ephi-family-header-grid, .ephi-family-facts-grid, .ephi-family-stepper, .ephi-family-action-row {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: var(--cui-space-2);
}
.ephi-family-step { border: 1px solid var(--cui-border-subtle); border-radius: var(--cui-radius-md); padding: var(--cui-space-2); background: var(--cui-surface-secondary); overflow-wrap: anywhere; }
.ephi-family-step strong { display: block; }
.ephi-family-step.is-current { outline: 2px solid var(--cui-focus-ring, rgb(0 94 168)); outline-offset: 1px; }
.ephi-family-table-wrap { width: 100%; overflow-x: auto; }
.ephi-family-gate-table { width: 100%; border-collapse: collapse; font-size: .9rem; }
.ephi-family-gate-table th, .ephi-family-gate-table td { padding: .65rem; border-bottom: 1px solid var(--cui-border-subtle); text-align: left; vertical-align: top; overflow-wrap: anywhere; }
.ephi-family-gate-table th { color: var(--cui-text-secondary); font-weight: 600; }
.ephi-family-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
.ephi-family-details { display: grid; gap: var(--cui-space-1); overflow-wrap: anywhere; }
.ephi-family-center button, .ephi-family-center .cui-button, .ephi-family-center .q-btn { min-height: 44px; }
.ephi-family-focus-context:focus-within { outline: 3px solid #005ea8 !important; outline-offset: 2px !important; }
.ephi-family-center button:focus, .ephi-family-center button:focus-visible,
.ephi-family-center .cui-button:focus, .ephi-family-center .cui-button:focus-visible,
.ephi-family-focus-context button:focus-visible, .ephi-family-focus-context .cui-button:focus-visible,
.ephi-family-focus:focus, .ephi-family-focus:focus-visible,
.ephi-family-page button:focus-visible, .ephi-family-page .cui-button:focus-visible,
.ephi-family-page .q-btn:focus-visible,
body:has([data-family-focus-return]) .q-btn.cui-button:focus-visible { outline: 3px solid #005ea8 !important; outline-offset: 2px !important; box-shadow: none !important; }
@media (max-width: 600px) {
  .ephi-family-header-grid, .ephi-family-facts-grid, .ephi-family-action-row { grid-template-columns: minmax(0, 1fr); }
  .ephi-family-stepper { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ephi-family-table-wrap { overflow: visible; }
  .ephi-family-gate-table { min-width: 0; }
  .ephi-family-gate-table, .ephi-family-gate-table tbody { display: block; }
  .ephi-family-gate-table thead { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
  .ephi-family-gate-table tr { display: grid; grid-template-columns: minmax(0, 1fr); margin: 0 0 var(--cui-space-2); border: 1px solid var(--cui-border-subtle); border-radius: var(--cui-radius-md); }
  .ephi-family-gate-table td { display: grid; grid-template-columns: minmax(6.2rem, 35%) minmax(0, 1fr); gap: var(--cui-space-2); border-bottom: 1px solid var(--cui-border-subtle); }
  .ephi-family-gate-table td:last-child { border-bottom: 0; }
  .ephi-family-gate-table td::before { content: attr(data-label); color: var(--cui-text-secondary); font-weight: 600; }
}
@media (min-width: 601px) { .ephi-family-gate-table { min-width: 1050px; } }
@media (max-width: 360px) { .ephi-family-stepper { grid-template-columns: minmax(0, 1fr); } }
@media (prefers-reduced-motion: reduce) { .ephi-family-center *, .ephi-family-center *::before { scroll-behavior: auto !important; animation: none !important; transition: none !important; } }
@media (forced-colors: active) { .ephi-family-step, .ephi-family-warning, .ephi-family-truth, .ephi-family-facts, .ephi-family-blockers { border: 1px solid CanvasText; } }
"""

_STAGE_LABELS = {
    "DISCOVER_MAP": "Discover / Map",
    "DATA_REALITY": "Data Reality",
    "REPLAY": "Replay",
    "GOLDEN": "Golden",
    "SHADOW": "Shadow",
    "QUALIFY": "Qualify",
    "PROMOTE": "Promote",
}


def _command_id(action: str, workspace_id: str, version: int, payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return f"family-center-{action}-{workspace_id[:16]}-{version}-{digest[:20]}"


def _stamp(value: object) -> str:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else "—"


def _intent(value: str) -> StatusIntent:
    return {
        "PASS": StatusIntent.SUCCESS,
        "READY": StatusIntent.SUCCESS,
        "NOT_APPLICABLE": StatusIntent.NEUTRAL,
        "PENDING": StatusIntent.WARNING,
        "STALE": StatusIntent.WARNING,
        "EXPIRED": StatusIntent.WARNING,
        "BLOCKED": StatusIntent.DANGER,
        "FAIL": StatusIntent.DANGER,
        "UNAVAILABLE": StatusIntent.DANGER,
    }.get(value, StatusIntent.NEUTRAL)


def build_family_center_index_page(
    composition: Any,
    navigation: NavigationModel,
) -> None:
    """Authorized family index linking to the canonical per-family route."""

    from nicegui import ui

    downstream = composition.downstream
    try:
        principal = composition.principal_provider()
        scope = composition.scope_provider()
        if downstream is None:
            raise AuthorizationDeniedError("Family Center requires the configured U1 policy authority")
        downstream.current_authorization.authorize(principal, scope, FAMILY_CENTER_READ)
        family_contexts = downstream.policy_configuration.family_contexts
    except AuthorizationDeniedError:
        family_contexts = ()
        denied = True
    except Exception:
        family_contexts = ()
        denied = False
    else:
        denied = False

    with AppShell(
        "EPHI", navigation, active_route="/ephi/families",
        environment=os.environ.get("EPHI_ENV", "development"),
        subtitle="Family Center · generic qualification", user_name=(principal.subject if "principal" in locals() else "Unavailable"),
        user_role="Family qualification reviewer", debugger=False,
    ):
        ui.add_css(_CSS, shared=True)
        ui.query("main").props('role="region" aria-label="Family Center"')
        with AnalysisWorkspacePage("Family Center", "Generic family and capability qualification") as page:
            with page.slot(LayoutSlot.HEADER):
                ui.label("Family Center").classes("ephi-family-title")
                ui.label("Generic family and capability qualification workspace.").classes("ephi-family-truth")
                ui.label("This is NOT G12 production/release promotion and NOT Production approval.").classes("ephi-family-warning")
            with page.slot(LayoutSlot.PRIMARY):
                if denied:
                    StateView(StateViewSpec(StateKind.PERMISSION, "Family Center unavailable", "Current authorization does not permit this Family Center request."))
                elif not family_contexts:
                    StateView(StateViewSpec(StateKind.EMPTY, "No configured family workspaces", "U1 has no configured Family Center family and capability targets in this runtime."))
                else:
                    for family in family_contexts:
                        with Card():
                            ui.label(f"Family {family.family_id} · context configuration {family.version}").classes("text-subtitle1")
                            if not family.qualification_targets:
                                ui.label("No capability or target release is configured for Family Center.")
                            for target in family.qualification_targets:
                                ui.link(
                                    f"{target.capability_id} · {target.product_id} · {target.release_id}",
                                    f"/ephi/families/{family.family_id}",
                                ).classes("ephi-family-focus")


def build_family_center_page(
    composition: Any,
    family_id: str,
    navigation: NavigationModel,
) -> None:
    """Render the canonical ``/ephi/families/{family_id}`` route."""

    from nicegui import ui

    downstream = composition.downstream
    selected = {"index": 0}
    workspace = {"view": None, "identity": None}
    notices = {"message": ""}

    try:
        principal = composition.principal_provider()
        scope = composition.scope_provider()
        if downstream is None:
            raise AuthorizationDeniedError("Family Center requires the configured U1 policy authority")
        downstream.current_authorization.authorize(principal, scope, FAMILY_CENTER_READ)
        family = next((item for item in downstream.policy_configuration.family_contexts if item.family_id == family_id), None)
        targets = tuple(family.qualification_targets) if family else ()
    except AuthorizationDeniedError:
        principal = None
        family = None
        targets = ()
        initial_error = "permission"
    except Exception:
        principal = None
        family = None
        targets = ()
        initial_error = "error"
    else:
        initial_error = "empty" if family is None else ""

    with AppShell(
        "EPHI", navigation, active_route="/ephi/families",
        environment=os.environ.get("EPHI_ENV", "development"),
        subtitle="Family Center · qualification", user_name=(principal.subject if principal else "Unavailable"),
        user_role="Family qualification reviewer", debugger=False,
    ):
        ui.add_css(_CSS, shared=True)
        ui.query("main").classes("ephi-family-page").props('role="region" aria-label="Family Center qualification workspace"')
        with AnalysisWorkspacePage("Family Center", "Generic family and capability qualification") as page:
            with page.slot(LayoutSlot.HEADER, sticky=True, aria_label="Persistent Family Center qualification header"):
                ui.label("Family Center").props('tabindex="-1" data-family-focus-return').classes("ephi-family-title")
                if family is not None:
                    ui.label(f"Family {family.family_id} · context configuration {family.version}").classes("text-subtitle1")
                ui.label("Generic qualification only · NOT G12 production/release promotion · NOT Production approval.").classes("ephi-family-truth")
                if targets:
                    target_options = {
                        str(index): f"{target.capability_id} · {target.product_id} · {target.release_id}"
                        for index, target in enumerate(targets)
                    }
                    target_select = ui.select(target_options, value="0", label="Capability and target release").classes("ephi-family-focus")
                    target_select.on_value_change(lambda event: select_target(str(event.value)))
                else:
                    target_select = None
                with ui.element("div").classes("ephi-family-action-row ephi-family-focus-context"):
                    ActionButton("Refresh workspace", intent=ButtonIntent.SECONDARY, on_click=lambda: refresh())
            with page.slot(LayoutSlot.PRIMARY):
                surface = ui.element("div").classes("ephi-family-center")

                def announce_and_focus() -> None:
                    try:
                        ui.run_javascript("setTimeout(() => document.querySelector('[data-family-focus-return]')?.focus(), 80)")
                    except Exception:
                        return

                def render_error(error: BaseException) -> None:
                    surface.clear()
                    with surface:
                        if isinstance(error, AuthorizationDeniedError):
                            StateView(StateViewSpec(StateKind.PERMISSION, "Family Center permission required", "Current authorization does not permit this family qualification workspace."))
                        elif isinstance(error, VersionConflictError):
                            StatusBadge("CONFLICT", intent=StatusIntent.WARNING)
                            StateView(StateViewSpec(StateKind.ERROR, "Workspace changed", "Another current revision committed. Refresh before reviewing or promoting."))
                        else:
                            StateView(StateViewSpec(StateKind.ERROR, "Workspace could not be loaded", "The bounded qualification request failed. Refresh to read current authority state."))

                def load_current() -> None:
                    if family is None or not targets or downstream is None:
                        workspace["view"] = None
                        return
                    idx = max(0, min(selected["index"], len(targets) - 1))
                    target = targets[idx]
                    identity = downstream.family_workspace_identity(family_id, target)
                    workspace["identity"] = identity
                    current_principal = composition.principal_provider()
                    current_scope = composition.scope_provider()
                    if current_scope != identity.scope:
                        raise AuthorizationDeniedError("current scope does not permit this family workspace")
                    workspace["view"] = downstream.family_center.get_workspace(
                        current_principal, current_scope, identity.identity, current_identity=identity
                    )

                def select_target(value: str) -> None:
                    try:
                        selected["index"] = int(value)
                        notices["message"] = ""
                        load_current()
                        render_workspace()
                    except AggregateNotFoundError:
                        workspace["view"] = None
                        render_workspace()
                    except Exception as error:
                        render_error(error)
                    announce_and_focus()

                def refresh() -> None:
                    try:
                        load_current()
                        notices["message"] = "Workspace refreshed from current O2/O4/O8 authorities."
                        render_workspace()
                    except AggregateNotFoundError:
                        workspace["view"] = None
                        notices["message"] = "No durable workspace exists for this configured target yet."
                        render_workspace()
                    except Exception as error:
                        render_error(error)
                    announce_and_focus()

                def open_workspace() -> None:
                    try:
                        identity = workspace["identity"]
                        assert identity is not None and downstream is not None
                        current_principal = composition.principal_provider()
                        current_scope = composition.scope_provider()
                        version = 0
                        result = downstream.family_center.ensure_workspace(
                            CommandContext(_command_id("open", identity.identity, version, identity.identity), current_principal, current_scope, version),
                            identity,
                        )
                        workspace["view"] = result
                        notices["message"] = "Durable qualification workspace opened. No gate has been promoted."
                        render_workspace()
                    except Exception as error:
                        render_error(error)
                    announce_and_focus()

                def promote() -> None:
                    try:
                        identity = workspace["identity"]
                        view = workspace["view"]
                        assert identity is not None and view is not None and downstream is not None
                        current_principal = composition.principal_provider()
                        current_scope = composition.scope_provider()
                        gate_ids = tuple(
                            next(gate.revision_id for gate in view.gates if gate.stage_id == stage)
                            for stage in identity.required_stages
                        )
                        payload = {"gates": gate_ids, "source_reality": view.source_reality.reality_identity}
                        downstream.family_center.promote(
                            CommandContext(_command_id("promote", identity.identity, view.version, payload), current_principal, current_scope, view.version),
                            identity,
                            gate_identity_set=gate_ids,
                            source_reality_identity=view.source_reality.reality_identity,
                        )
                        load_current()
                        notices["message"] = "Generic Family Center qualification promotion recorded. It does not grant G12 or Production approval."
                        render_workspace()
                    except Exception as error:
                        render_error(error)
                    announce_and_focus()

                def render_workspace() -> None:
                    surface.clear()
                    with surface:
                        if initial_error == "permission":
                            StateView(StateViewSpec(StateKind.PERMISSION, "Family Center unavailable", "Current authorization does not permit this family workspace."))
                            return
                        if initial_error == "error":
                            StateView(StateViewSpec(StateKind.ERROR, "Family configuration unavailable", "The configured U1 family authority could not be read."))
                            return
                        if family is None:
                            StateView(StateViewSpec(StateKind.EMPTY, "Family is not configured", "U1 has no configured Family Center workspace for this family identity."))
                            return
                        if not targets:
                            StateView(StateViewSpec(StateKind.EMPTY, "No qualification target configured", "This family has no configured capability and target release in U1."))
                            return
                        target = targets[selected["index"]]
                        identity = workspace["identity"]
                        ui.label(f"Capability {target.capability_id} · {target.product_id} · release {target.release_id}").classes("text-h6")
                        ui.label(f"Target context {identity.context_identity} · unit {identity.unit_identity} · provider ABI {identity.provider_abi_id} {identity.provider_abi_version}").classes("ephi-family-warning")
                        if target.synthetic_fixture:
                            ui.label("SYNTHETIC FIXTURE · NON-PRODUCTION evidence and identities").classes("ephi-family-truth")
                        if notices["message"]:
                            ui.label(notices["message"]).props('role="status" aria-live="polite"').classes("ephi-family-warning")

                        view = workspace["view"]
                        if view is None:
                            with Card():
                                StateView(StateViewSpec(StateKind.EMPTY, "Qualification workspace not started", "No durable workspace exists for this exact scope, family, context, capability, release, provider, policy, source mapping and runtime identity."))
                                openable = composition.principal_provider().has_capability("ephi.family_qualification.evidence.write")
                                ActionButton("Open qualification workspace", intent=ButtonIntent.PRIMARY, disabled=not openable, on_click=open_workspace)
                                if not openable:
                                    ui.label("Current evidence-write authorization is required to open this workspace.")
                            return

                        _render_stepper(ui, view)
                        with ui.element("div").classes("ephi-family-facts-grid"):
                            with Card():
                                ui.label("Discover / Map · sanitized U1/O4 facts").classes("text-subtitle1")
                                mapping = view.mapping_facts
                                facts = (
                                    ("Provider", mapping.provider_id), ("Source", mapping.source_id), ("Canonical schema", mapping.schema_id),
                                    ("Mapping version", mapping.mapping_version), ("Mapping hash", mapping.mapping_hash),
                                    ("Canonical roles", ", ".join(mapping.canonical_roles)), ("Unit", mapping.unit),
                                    ("Timestamp semantics", mapping.timestamp_semantics), ("Availability semantics", mapping.availability_semantics),
                                    ("Reference population", mapping.reference_population_id or "Not configured"),
                                    ("Comparable population", mapping.comparable_population_id or "Not configured"),
                                )
                                for name, value in facts:
                                    ui.label(f"{name}: {value}").classes("ephi-family-mono" if name == "Mapping hash" else "")
                            with Card():
                                ui.label("Data Reality · current O4 capability and immutable snapshot").classes("text-subtitle1")
                                reality = view.source_reality
                                StatusBadge(f"{reality.capability_state} · {reality.state}", intent=_intent(reality.state))
                                ui.label(f"Reason: {reality.reason_code}")
                                ui.label(f"Capability checked: {_stamp(reality.checked_at)} · freshness limit {reality.freshness_age_seconds}s · current source age {reality.source_age_seconds if reality.source_age_seconds is not None else 'unknown'}s")
                                ui.label(f"Snapshot: {reality.latest_snapshot_id or 'unavailable'}")
                                ui.label(f"Manifest hash: {reality.latest_manifest_hash or 'unavailable'}").classes("ephi-family-mono")
                                ui.label(f"Available at: {_stamp(reality.latest_available_at)} · latest event at: {_stamp(reality.latest_event_at)}")
                                if reality.state != "READY":
                                    ui.label("Missing or unavailable O4 facts remain unavailable; no synthetic healthy source result is substituted.").classes("ephi-family-warning")

                        with Card():
                            ui.label("Qualification gates").classes("text-h6")
                            ui.label("PASS and NOT_APPLICABLE require current evidence and policy basis. PENDING, FAIL, BLOCKED, EXPIRED, STALE and unknown states block promotion. On desktop, scroll the gate table horizontally to inspect each evidence field.")
                            _render_gate_table(ui, view.gates)

                        gate_options = {
                            gate.revision_id: f"{_STAGE_LABELS[gate.stage_id]} · {gate.state.value} · revision {gate.revision or '—'}"
                            for gate in view.gates if gate.revision_id
                        }
                        if gate_options:
                            selected_gate = ui.select(gate_options, value=next(iter(gate_options)), label="Evidence and reviewer drilldown").classes("ephi-family-focus")
                            detail = next((gate for gate in view.gates if gate.revision_id == selected_gate.value), None)
                            if detail is not None:
                                with Card():
                                    ui.label("Evidence and judgment history").classes("text-subtitle1")
                                    ui.label(f"Gate {detail.stage_id} · revision identity {detail.revision_id}").classes("ephi-family-mono")
                                    ui.label(f"Policy basis {detail.policy_basis_id or '—'} · version {detail.policy_basis_version or '—'}")
                                    ui.label(f"Artifact SHA-256 {detail.evidence_sha256 or 'none'} · bytes {detail.evidence_byte_size if detail.evidence_byte_size is not None else '—'}").classes("ephi-family-mono")
                                    ui.label(f"Evidence author {detail.evidence_author or '—'} · known at {_stamp(detail.known_at)} · published at {_stamp(detail.published_at)} · expires {_stamp(detail.expires_at)}")
                                    ui.label(f"Stage policy identity {detail.stage_policy_identity or '—'} · engine {detail.engine_identity or '—'}")
                                    ui.label(f"Applicable source/replay inputs: {', '.join(detail.input_identities) or 'none recorded'}").classes("ephi-family-mono")
                                    ui.label(f"Requalification policy {detail.requalification_policy_id or '—'} · version {detail.requalification_policy_version or '—'}")
                                    ui.label(f"Current evidence status {detail.evidence_status} · invalidation reason {detail.invalidation_reason or 'none'}")
                                    ui.label(f"Independent judgment identity {detail.judgment_identity or 'not recorded'} · basis {detail.judgment_basis_id or '—'} · version {detail.judgment_basis_version or '—'} · source-reality identity {detail.source_reality_identity or '—'}")
                                    ui.label(f"Bounded job {detail.job_id or 'none'} · job status {detail.job_status or 'not attached'}")
                                    ui.label("Artifact bytes remain in the existing artifact authority; this workspace stores and displays evidence references and hashes only.")

                        with ui.element("div").classes("ephi-family-blockers"):
                            ui.label("Promotion readiness").classes("text-subtitle1")
                            StatusBadge("READY" if view.promotion_ready else "BLOCKED", intent=StatusIntent.SUCCESS if view.promotion_ready else StatusIntent.DANGER)
                            if view.promotion_blockers:
                                for blocker in view.promotion_blockers:
                                    ui.label(f"Blocker: {blocker}")
                            can_promote = composition.principal_provider().has_capability(FAMILY_CENTER_PROMOTE)
                            if not can_promote:
                                ui.label("Blocker: CURRENT_PROMOTION_AUTHORIZATION_REQUIRED")
                            ActionButton(
                                "Promote generic qualification",
                                intent=ButtonIntent.PRIMARY,
                                disabled=not (view.promotion_ready and can_promote),
                                on_click=promote,
                            )
                            ui.label("A Family Center promotion records qualification for this exact capability and release only; it is NOT G12 and NOT Production approval.")
                            if view.promotions:
                                ui.label("Historical promotion records").classes("text-subtitle2")
                                for record in view.promotions:
                                    ui.label(f"{record.state} · {record.promotion_id} · revision {record.workspace_revision} · by {record.promoted_by} · {record.promoted_at.isoformat()} · {record.invalidation_reason or 'current exact evidence'}").classes("ephi-family-mono")

                if initial_error:
                    render_workspace()
                else:
                    try:
                        load_current()
                    except AggregateNotFoundError:
                        notices["message"] = "No durable workspace exists for this configured target yet."
                    except Exception as error:
                        render_error(error)
                        return
                    render_workspace()


def _render_stepper(ui: Any, view: Any) -> None:
    states = {gate.stage_id: gate.state for gate in view.gates}
    with ui.element("nav").classes("ephi-family-stepper").props('aria-label="Qualification stage progress"'):
        for stage in ("DISCOVER_MAP", "DATA_REALITY", "REPLAY", "GOLDEN", "SHADOW", "QUALIFY"):
            state = states.get(stage, GateState.NOT_STARTED)
            with ui.element("div").classes(f"ephi-family-step state-{state.value.lower()}"):
                ui.label(_STAGE_LABELS[stage]).classes("text-subtitle2")
                StatusBadge(state.value, intent=_intent(state.value))
        with ui.element("div").classes(f"ephi-family-step {'is-current' if view.promotion_ready else ''}"):
            ui.label(_STAGE_LABELS["PROMOTE"]).classes("text-subtitle2")
            StatusBadge("READY" if view.promotion_ready else "BLOCKED", intent=StatusIntent.SUCCESS if view.promotion_ready else StatusIntent.DANGER)


def _render_gate_table(ui: Any, gates: tuple[Any, ...]) -> None:
    columns = ("Stage", "State", "Policy basis", "Evidence SHA-256", "Known at", "Expires", "Invalidation", "Job")
    labels = columns
    with ui.element("div").classes("ephi-family-table-wrap"):
        with ui.element("table").classes("ephi-family-gate-table").props('aria-label="Qualification gate evidence"'):
            with ui.element("thead"):
                with ui.element("tr"):
                    for column in columns:
                        with ui.element("th").props('scope="col"'):
                            ui.label(column)
            with ui.element("tbody"):
                for gate in gates:
                    with ui.element("tr"):
                        cells = (
                            _STAGE_LABELS[gate.stage_id], gate.state.value,
                            f"{gate.policy_basis_id or '—'} · {gate.policy_basis_version or '—'}",
                            gate.evidence_sha256 or "—", _stamp(gate.known_at), _stamp(gate.expires_at),
                            gate.invalidation_reason or "—",
                            f"{gate.job_id or '—'} · {gate.job_status or '—'}",
                        )
                        for label, value in zip(labels, cells, strict=True):
                            with ui.element("td").props(f'data-label="{label}"'):
                                ui.label(value).classes("ephi-family-mono" if label == "Evidence SHA-256" else "")


__all__ = ["build_family_center_index_page", "build_family_center_page"]
