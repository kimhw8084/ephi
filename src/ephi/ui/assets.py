"""Canonical Assets and Asset 360 destinations on pinned NiceGUI Base."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import json
from urllib.parse import urlencode, quote
import base64
from typing import Any, Mapping

from nicegui_base import (
    ActionButton,
    AnalysisWorkspacePage,
    AppShell,
    ButtonIntent,
    Card,
    ColumnKind,
    DataTable,
    DataTableSpec,
    LayoutSlot,
    NavigationModel,
    PaginationMode,
    RowAction,
    StatusBadge,
    StatusIntent,
    TableColumn,
    TableDensity,
    TabSpec,
    Tabs,
    StateKind,
    StateView,
    StateViewSpec,
)

from ephi.application.assets import Asset360QueryService, AssetPage
from ephi.application.errors import (
    AggregateNotFoundError,
    AuthorizationDeniedError,
    CommandError,
    QuerySnapshotExpiredError,
    ValidationFailureError,
)
from ephi.application.context import AccessScope, Principal


_CSS = """
.ephi-assets, .ephi-assets * { min-width: 0; box-sizing: border-box; }
.ephi-assets .cui-pattern--analysis_workspace .cui-pattern-slot--primary { grid-column: 1 / -1; }
.ephi-assets-title { margin: 0; font-size: clamp(1.7rem, 4vw, 2.5rem); line-height: 1.12; overflow-wrap: anywhere; }
.ephi-assets-note, .ephi-assets-facts, .ephi-assets-state, .ephi-assets-card { border: 1px solid var(--cui-border-subtle); border-radius: var(--cui-radius-md); background: var(--cui-surface-secondary); padding: var(--cui-space-3); overflow-wrap: anywhere; }
.ephi-assets-controls, .ephi-assets-header-grid, .ephi-assets-coverage { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); gap: var(--cui-space-2); align-items: end; }
.ephi-assets-table-wrap { width: 100%; overflow-x: auto; }
.ephi-assets-desktop-table { display: block; }
.ephi-assets-mobile-list { display: none; }
.ephi-assets .cui-table-row-action { display: inline-flex !important; align-items: center !important; min-width: 44px !important; min-height: 44px !important; height: 44px !important; padding-block: 10px !important; margin-inline-end: 4px !important; }
.ephi-assets .ag-row { min-height: 52px !important; height: 52px !important; }
.ephi-assets button.q-btn.cui-button.cui-button--primary:focus-visible,
.ephi-assets button.q-btn.cui-button.cui-button--secondary:focus-visible { outline: 3px solid #005ea8 !important; outline-offset: 2px !important; box-shadow: 0 0 0 3px #005ea8 !important; }
.ephi-assets-time { color: var(--cui-text-secondary); font-size: .9rem; overflow-wrap: anywhere; }
.ephi-assets-timeline { display: grid; gap: var(--cui-space-3); }
.ephi-assets-episode { border-inline-start: 3px solid var(--cui-border-strong); padding-inline-start: var(--cui-space-3); }
.ephi-assets-episode h2, .ephi-assets-episode h3 { margin: 0 0 var(--cui-space-1); overflow-wrap: anywhere; }
.ephi-assets-actions { display: grid; gap: var(--cui-space-2); }
.ephi-assets-chart { width: 100%; max-width: 760px; border: 1px solid var(--cui-border-subtle); border-radius: var(--cui-radius-md); background: var(--cui-surface-primary); }
.ephi-assets-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
.ephi-assets button, .ephi-assets .cui-button, .ephi-assets .q-btn { min-height: 44px !important; }
@media (min-width: 601px) { .ephi-assets-timeline { padding-right: 8px; } }
@media (max-width: 600px) { .ephi-assets-controls, .ephi-assets-header-grid, .ephi-assets-coverage { grid-template-columns: minmax(0, 1fr); } .ephi-assets-desktop-table { display: none; } .ephi-assets-mobile-list { display: grid; gap: var(--cui-space-2); } }
@media (prefers-reduced-motion: reduce) { .ephi-assets *, .ephi-assets *::before, .ephi-assets *::after { scroll-behavior: auto !important; animation: none !important; transition: none !important; } }
@media (forced-colors: active) { .ephi-assets-note, .ephi-assets-facts, .ephi-assets-state, .ephi-assets-card { border: 1px solid CanvasText; } }
"""


def _stamp(value: object) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "—")


def _query_params() -> Mapping[str, str]:
    try:
        from nicegui import ui
        request = ui.context.client.request
        return {key: str(value) for key, value in request.query_params.items()}
    except Exception:
        return {}


def _route(path: str, values: Mapping[str, object]) -> str:
    params = {key: value for key, value in values.items() if value not in (None, "")}
    return f"{path}?{urlencode(params, doseq=True)}" if params else path


def _filters_from_params(params: Mapping[str, str]) -> dict[str, str]:
    return {field: params[field] for field in ("asset", "family", "site", "context", "characteristic") if params.get(field)}


def _page_columns() -> tuple[TableColumn, ...]:
    return (
        TableColumn("asset_id", "Asset", ColumnKind.TEXT, width=150, min_width=145, priority="high", sortable=False, filterable=False),
        TableColumn("family_context", "Family / context", ColumnKind.TEXT, width=180, min_width=170, priority="high", sortable=False, filterable=False),
        TableColumn("characteristic_unit", "Characteristic / unit", ColumnKind.TEXT, width=165, min_width=155, priority="normal", sortable=False, filterable=False),
        TableColumn("source_status_age", "Source state / age", ColumnKind.TEXT, width=145, min_width=135, priority="high", sortable=False, filterable=False),
        TableColumn("open_work_count", "Open work", ColumnKind.INTEGER, width=90, min_width=80, priority="high", sortable=False, filterable=False),
        TableColumn("latest_episode_work", "Latest Episode / work", ColumnKind.TEXT, width=265, min_width=250, priority="high", sortable=False, filterable=False),
    )


def _friendly_error(error: BaseException) -> tuple[StateKind, str, str]:
    if isinstance(error, AuthorizationDeniedError):
        return StateKind.PERMISSION, "Asset permission required", "Current authorization does not permit this Asset request."
    if isinstance(error, QuerySnapshotExpiredError):
        return StateKind.ERROR, "Asset page expired", "The retained page is no longer available. Restart the filter query."
    if isinstance(error, AggregateNotFoundError):
        return StateKind.NOT_FOUND, "Asset unavailable", "No matching qualified Episode truth is available in the current authorized scope by this cutoff."
    if isinstance(error, CommandError):
        return StateKind.ERROR, "Asset data unavailable", f"The authorized Asset query failed ({error.code}). Refresh to read current truth."
    return StateKind.ERROR, "Asset data unavailable", "The authorized Asset query failed. Refresh to read current truth."


def _shell(navigation: NavigationModel, principal: Principal, active: str, subtitle: str) -> AppShell:
    import os
    return AppShell(
        "EPHI", navigation, active_route=active, environment=os.environ.get("EPHI_ENV", "development"),
        subtitle=subtitle, user_name=principal.subject, user_role="Engineer", debugger=False,
    )


def _load_service(composition: Any) -> tuple[Asset360QueryService, Principal, AccessScope]:
    if composition.asset_360 is None:
        raise ValidationFailureError("Asset read authority is unavailable")
    return composition.asset_360, composition.principal_provider(), composition.scope_provider()


def _trail_read(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        result = json.loads(base64.urlsafe_b64decode(raw.encode("ascii") + b"=" * (-len(raw) % 4)))
        if isinstance(result, list) and all(isinstance(item, str) for item in result) and len(result) <= 100:
            return result
    except Exception:
        pass
    return []


def _trail_write(values: list[str]) -> str:
    raw = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_asset_index_page(composition: Any, navigation: NavigationModel) -> None:
    from nicegui import ui

    params = _query_params()
    filters = _filters_from_params(params)
    page_size = 25
    cursor = params.get("cursor") or None
    snapshot_id = params.get("snapshot") or None
    facts_identity = params.get("facts") or None
    trail = _trail_read(params.get("trail"))
    state: dict[str, Any] = {"page": None, "error": None}
    try:
        service, principal, scope = _load_service(composition)
        page = service.list_assets(
            principal, scope, filters=filters, page_size=page_size,
            snapshot_id=snapshot_id, cursor=cursor, facts_identity=facts_identity,
        )
        state["page"] = page
    except Exception as error:
        state["error"] = error
        try:
            principal = composition.principal_provider()
        except Exception:
            principal = Principal("Unavailable", (), (), 0)
        try:
            scope = composition.scope_provider()
        except Exception:
            scope = None

    with _shell(navigation, principal, "/ephi/assets", "Authorized Asset list"):
        ui.add_css(_CSS, shared=True)
        ui.query("main").classes("ephi-assets").props('role="region" aria-label="Authorized Asset list"')
        with AnalysisWorkspacePage("", None) as page_layout:
            with page_layout.slot(LayoutSlot.HEADER):
                ui.label("Assets").classes("ephi-assets-title")
                ui.label("Assets present in qualified EPHI Episode truth for the current authorized scope. This list does not represent company asset-master completeness.").classes("ephi-assets-note")
                if scope is not None:
                    ui.label(f"Site {scope.site_id or 'not separately identified'} · family scope {scope.family_id or 'not separately identified'} · scope {scope.scope_id}").classes("ephi-assets-time")
            with page_layout.slot(LayoutSlot.PRIMARY):
                with ui.element("div").classes("ephi-assets-controls").props('role="search" aria-label="Filter assets by canonical identity"'):
                    asset_input = ui.input("Asset identity", value=filters.get("asset", ""))
                    family_input = ui.input("Family identity", value=filters.get("family", ""))
                    site_input = ui.input("Site identity", value=filters.get("site", ""))
                    context_input = ui.input("Context identity", value=filters.get("context", ""))
                    characteristic_input = ui.input("Characteristic identity", value=filters.get("characteristic", ""))

                    def apply_filters() -> None:
                        target = {
                            "asset": asset_input.value,
                            "family": family_input.value,
                            "site": site_input.value,
                            "context": context_input.value,
                            "characteristic": characteristic_input.value,
                        }
                        ui.navigate.to(_route("/ephi/assets", target))

                    ActionButton("Apply filters", intent=ButtonIntent.PRIMARY, on_click=apply_filters)
                with ui.element("div").classes("ephi-assets-note"):
                    ui.label("Loading qualified current Episode heads…" if state["page"] is None and state["error"] is None else "")
                    if state["error"] is not None:
                        kind, title, message = _friendly_error(state["error"])
                        StateView(
                            StateViewSpec(kind, title, message, action_label="Restart query"),
                            on_action=lambda: ui.navigate.to("/ephi/assets"),
                        )
                    elif state["page"] is not None:
                        result: AssetPage = state["page"]
                        ui.label(f"{result.total_count} authorized asset(s) · retained snapshot {result.snapshot_id} · result {result.result_identity[:16]}").props('role="status" aria-live="polite"')
                        ui.label(f"Query identity {result.query_identity['facts_identity']} · sort asset ID tie-break · source state reflects O4 capability freshness.").classes("ephi-assets-time")
                        states = {str(row["source_state"]) for row in result.rows}
                        if "STALE" in states:
                            StatusBadge("SOURCE STALE", intent=StatusIntent.WARNING)
                        elif "PARTIAL" in states:
                            StatusBadge("SOURCE PARTIAL", intent=StatusIntent.WARNING)
                        elif result.rows and states <= {"UNAVAILABLE"}:
                            StatusBadge("SOURCE UNAVAILABLE", intent=StatusIntent.DANGER)
                if state["page"] is not None:
                    result = state["page"]
                    if not result.rows:
                        title = "No authorized Assets" if not filters else "No matching Assets"
                        detail = "No supported InvestigationProfile is present in current authorized Episode heads." if not filters else "No qualified current Episode head matches all exact canonical identity filters."
                        StateView(StateViewSpec(StateKind.EMPTY, title, detail))
                    else:
                        def open_asset(row: Mapping[str, Any]) -> None:
                            ui.navigate.to(f"/ephi/assets/{quote(str(row['asset_id']), safe='')}")

                        table_spec = DataTableSpec(
                            _page_columns(), row_key="asset_id", title="Qualified Assets", description="One row per exact asset identity present in current authorized Episode heads.",
                            density=TableDensity.COMPACT, pagination=PaginationMode.CLIENT, page_size=25,
                            searchable=False, column_manager=False, density_control=False, export_csv=False,
                            export_enabled=False, copy_enabled=False, refresh_enabled=False, persist_state=False,
                            empty_message="No matching Assets", error_message="Asset page unavailable",
                        )
                        with ui.element("div").classes("ephi-assets-desktop-table ephi-assets-table-wrap").props('role="region" aria-label="Asset table and accessible row actions"'):
                            DataTable(
                                result.rows, spec=table_spec, row_key="asset_id",
                                row_actions=(RowAction("open_asset", "Open", icon="external-link", intent="primary", on_action=open_asset),),
                            )
                        with ui.element("div").classes("ephi-assets-mobile-list").props('role="list" aria-label="Assets with all fields and direct actions"'):
                            for row in result.rows:
                                with Card() as asset_card:
                                    asset_card.element.props('role="listitem"')
                                    ui.label(f"Asset {row['asset_id']}").classes("text-subtitle1 ephi-assets-mono")
                                    ui.label(f"Family {row['family_identity']} · context {row['context_identity']}")
                                    ui.label(f"Characteristic {row['characteristic_identity']} · unit {row['unit_identity']}")
                                    ui.label(f"Source {row['source_state']} · age {row['source_age_display']}")
                                    ui.label(f"Open engineering work {row['open_work_count']}")
                                    ui.label(f"Latest Episode {row['latest_episode_id']} · work {row['latest_work_state']} · owner {row.get('latest_owner') or 'unassigned'}")
                                    ActionButton(
                                        "Open Asset 360", intent=ButtonIntent.PRIMARY,
                                        on_click=lambda asset_id=row["asset_id"]: ui.navigate.to(f"/ephi/assets/{quote(str(asset_id), safe='')}")
                                    )
                        page_number = len(trail) + 1
                        with ui.element("div").classes("ephi-assets-controls").props('aria-label="Asset retained page controls"'):
                            ui.label(f"Page {page_number} · {result.total_count} total")
                            if trail:
                                previous_cursor = trail[-1]
                                previous_trail = trail[:-1]
                                previous_url = _route("/ephi/assets", {
                                    **filters,
                                    "snapshot": result.snapshot_id,
                                    "cursor": None if previous_cursor == "FIRST" else previous_cursor,
                                    "facts": result.query_identity["facts_identity"],
                                    "trail": _trail_write(previous_trail) if previous_trail else None,
                                })
                                ui.link("Previous page", previous_url)
                            if result.next_cursor:
                                next_trail = [*trail, cursor or "FIRST"]
                                next_url = _route("/ephi/assets", {
                                    **filters,
                                    "snapshot": result.snapshot_id,
                                    "cursor": result.next_cursor,
                                    "facts": result.query_identity["facts_identity"],
                                    "trail": _trail_write(next_trail),
                                })
                                ui.link("Next page", next_url)
                            ui.link("Refresh current Assets", _route("/ephi/assets", filters))


def _series_svg(points: list[Mapping[str, Any]], peer: list[Mapping[str, Any]] | None, unit: str) -> str:
    width, height, margin = 760, 240, 36
    all_points = [(item, 0) for item in points] + ([(item, 1) for item in peer] if peer else [])
    values = [float(item["value"]) for item, _ in all_points]
    if not values:
        return ""
    low, high = min(values), max(values)
    if low == high:
        low -= 1
        high += 1
    event_times = [item["event_at"] for item, _ in all_points]
    start, end = min(event_times), max(event_times)
    span = max(1.0, (end - start).total_seconds())
    circles = []
    for item, is_peer in all_points:
        x = margin + ((item["event_at"] - start).total_seconds() / span) * (width - 2 * margin)
        y = height - margin - ((float(item["value"]) - low) / (high - low)) * (height - 2 * margin)
        color = "#ad3f2f" if is_peer else "#145a82"
        label = escape(f"{item['event_at'].isoformat()} · {item['value']} {unit} · {item['source_row_id']}")
        circles.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="6" fill="{color}"><title>{label}</title></circle>')
    # Points are deliberately unconnected: gaps remain visible and no values
    # are interpolated or smoothed between source observations.
    return (
        f'<svg class="ephi-assets-chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{len(points)} primary and {len(peer or [])} peer measurement observations, plotted as separate points without connecting lines">'
        f'<line x1="{margin}" y1="{height-margin}" x2="{width-margin}" y2="{height-margin}" stroke="currentColor" opacity=".45"/>'
        f'<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height-margin}" stroke="currentColor" opacity=".45"/>'
        + "".join(circles)
        + f'<text x="{margin}" y="20" font-size="12">{low:g}–{high:g} {escape(unit)}</text>'
        + f'<text x="{margin}" y="{height-8}" font-size="11">{escape(start.isoformat())}</text>'
        + f'<text x="{width-margin}" y="{height-8}" text-anchor="end" font-size="11">{escape(end.isoformat())}</text>'
        + "</svg>"
    )


def _action_summary(action: Mapping[str, Any]) -> str:
    details = (
        f"Action {action.get('action_id')} · {action.get('action_type')} · status {action.get('reconciliation_state')} · "
        f"requested {action.get('requested_at') or 'not recorded'} · authorized {action.get('authorized_at') or 'not recorded'} · "
        f"observed {action.get('observed_at') or 'not recorded'} · EPHI recorded {action.get('recorded_at') or 'not available'}"
    )
    return details


def _open_episode(composition: Any, episode_id: str, asset_url: str) -> None:
    from nicegui import ui
    from .app import EPISODE_KEY, FOCUS_KEY, ORIGIN_KEY

    composition.workspace.state.set(ORIGIN_KEY, asset_url, source="ephi.assets")
    composition.workspace.state.set(EPISODE_KEY, episode_id, source="ephi.assets")
    composition.workspace.state.set(FOCUS_KEY, "episode_heading", source="ephi.assets")
    ui.navigate.to("/episode")


def build_asset_360_page(composition: Any, asset_id: str, navigation: NavigationModel) -> None:
    from nicegui import ui

    params = _query_params()
    now = datetime.now(timezone.utc)
    try:
        cutoff = datetime.fromisoformat(params["cutoff"].replace("Z", "+00:00")) if params.get("cutoff") else now
        end = datetime.fromisoformat(params["end"].replace("Z", "+00:00")) if params.get("end") else cutoff
        start = datetime.fromisoformat(params["start"].replace("Z", "+00:00")) if params.get("start") else end - timedelta(days=30)
        peer_asset_id = params.get("peer") or None
        characteristic = params.get("characteristic") or None
        unit = params.get("unit") or None
        service, principal, scope = _load_service(composition)
        result = service.get_asset_360(
            principal, scope, asset_id, knowledge_cutoff=cutoff, window_start=start, window_end=end,
            characteristic_identity=characteristic, unit_identity=unit, peer_asset_id=peer_asset_id,
        )
        error: BaseException | None = None
    except Exception as caught:
        result = None
        error = caught
        try:
            principal = composition.principal_provider()
        except Exception:
            principal = Principal("Unavailable", (), (), 0)

    with _shell(navigation, principal, "/ephi/assets", f"Asset 360 · {asset_id}"):
        ui.add_css(_CSS, shared=True)
        ui.query("main").classes("ephi-assets").props(f'role="region" aria-label="Asset 360 {escape(asset_id)}"')
        with AnalysisWorkspacePage("", None) as page_layout:
            with page_layout.slot(LayoutSlot.HEADER, sticky=True, aria_label="Persistent Asset and context header"):
                ui.label(f"Asset 360 · {asset_id}").classes("ephi-assets-title")
                if result is not None:
                    with ui.element("div").classes("ephi-assets-header-grid"):
                        with Card():
                            ui.label(f"Family {result.asset['family_identity']} · context {result.asset['context_identity']}")
                            ui.label(f"Characteristic {result.asset['characteristic_identity']} · unit {result.asset['unit_identity']}")
                            ui.label(f"Latest Episode known {_stamp(result.asset.get('latest_episode_known_at'))} · published {_stamp(result.asset.get('latest_episode_published_at'))}").classes("ephi-assets-time")
                        with Card():
                            source_intent = StatusIntent.SUCCESS if result.source["state"] == "READY" else StatusIntent.WARNING if result.source["state"] in {"PARTIAL", "STALE"} else StatusIntent.DANGER
                            StatusBadge(f"SOURCE {result.source['state']}", intent=source_intent)
                            ui.label(f"Source age {result.source.get('age_seconds') if result.source.get('age_seconds') is not None else 'unknown'} seconds · snapshot {result.source.get('snapshot_id') or 'unavailable'}")
                            ui.label(f"O4 revision {result.source.get('source_revision') or 'unavailable'} · available {_stamp(result.source.get('latest_available_at'))}").classes("ephi-assets-mono")
                            ui.label(f"Source {result.source.get('source_id') or 'unavailable'} · provider {result.source.get('provider_id') or 'unavailable'} · capability {result.source.get('capability_id') or 'unavailable'}").classes("ephi-assets-mono")
                        with Card():
                            ui.label(f"Open engineering work {result.asset['open_work_count']}")
                            ui.label(f"Latest Episode {result.asset['latest_episode_id']} · work {result.asset['latest_work_state']} · owner {result.asset.get('latest_owner') or 'unassigned'}")
                        with Card():
                            ui.label(f"Knowledge cutoff {_stamp(result.knowledge_cutoff)}")
                            ui.label(f"Query {result.query_identity[:20]} · result {result.result_identity[:20]}").classes("ephi-assets-mono")
                else:
                    ui.label("Current asset and source state are unavailable for this request.").classes("ephi-assets-note")
            with page_layout.slot(LayoutSlot.PRIMARY):
                if error is not None:
                    kind, title, message = _friendly_error(error)
                    StateView(
                        StateViewSpec(kind, title, message, action_label="Return to Assets"),
                        on_action=lambda: ui.navigate.to("/ephi/assets"),
                    )
                    return
                assert result is not None
                with ui.element("div").classes("ephi-assets-note"):
                    ui.label("Qualified EPHI Episode coverage only. This view does not assert company asset-master completeness, causal attribution, predictive health, or production readiness.")
                with ui.element("div").classes("ephi-assets-controls").props('role="search" aria-label="Asset trend and peer comparison query"'):
                    start_input = ui.input("Window start (ISO 8601 UTC)", value=start.isoformat())
                    end_input = ui.input("Window end (ISO 8601 UTC)", value=end.isoformat())
                    cutoff_input = ui.input("Knowledge cutoff (ISO 8601 UTC)", value=cutoff.isoformat())
                    char_input = ui.input("Characteristic identity", value=result.asset["characteristic_identity"])
                    unit_input = ui.input("Unit identity", value=result.asset["unit_identity"])
                    peer_input = ui.input("Optional peer asset identity", value=peer_asset_id or "")
                    def apply_query() -> None:
                        query = {
                            "start": start_input.value, "end": end_input.value,
                            "cutoff": cutoff_input.value, "characteristic": char_input.value,
                            "unit": unit_input.value, "peer": peer_input.value,
                        }
                        ui.navigate.to(_route(f"/ephi/assets/{quote(asset_id, safe='')}", query))
                    ActionButton("Apply window and compare", intent=ButtonIntent.PRIMARY, on_click=apply_query)
                with ui.element("div").classes("ephi-assets-note"):
                    ui.label("Open work and latest state are O5 workflow facts at the requested cutoff. Recorded actions and recovery are observations; temporal order does not imply technical effect or causality.")
                    ui.label("Changes/actions are shown from the existing O5 Episode workflow authority. Immutable Episode/read history is not rewritten.")
                with Tabs((
                    TabSpec("episodes", "Episodes", lazy=False),
                    TabSpec("changes", "Changes"),
                    TabSpec("measurement", "Measurement/quality"),
                    TabSpec("material", "Material context"),
                ), value="episodes") as tabs:
                    with tabs.panel("episodes"):
                        with ui.element("div").classes("ephi-assets-timeline"):
                            if not result.episodes:
                                StateView(StateViewSpec(StateKind.EMPTY, "No Episodes by this cutoff", "No immutable Episode/read revision for this asset was known and published by the requested cutoff."))
                            for episode in result.episodes:
                                with ui.element("article").classes("ephi-assets-episode ephi-assets-card"):
                                    ui.label(f"{episode['change']['headline']}").classes("text-h6")
                                    ui.label(f"Episode {episode['episode_id']} · revision {episode['revision_id']}").classes("ephi-assets-mono")
                                    ui.label(f"Known { _stamp(episode['known_at']) } · published { _stamp(episode['published_at']) } · onset { _stamp(episode['change']['onset_at']) }").classes("ephi-assets-time")
                                    ui.label(f"{episode['change']['description']} · magnitude {episode['change'].get('magnitude') or 'not recorded'}")
                                    ui.label(f"{episode['workflow_label']}: {episode['workflow_state']} · owner {episode.get('owner') or 'unassigned'} · O5 workflow version {episode['workflow_version']}").classes("ephi-assets-facts")
                                    ActionButton(
                                        "Open Episode", intent=ButtonIntent.SECONDARY,
                                        on_click=lambda episode_id=episode["episode_id"]: _open_episode(composition, episode_id, f"/ephi/assets/{quote(asset_id, safe='')}")
                                    )
                                    for action in episode.get("actions", ()):
                                        ui.label(_action_summary(action)).classes("ephi-assets-note")
                    with tabs.panel("changes"):
                        ui.label("O5 recorded action, check, recovery, closure and reopen facts. No causal link to analytical changes is inferred.").classes("ephi-assets-note")
                        if not result.changes:
                            StateView(StateViewSpec(StateKind.EMPTY, "No recorded O5 changes/actions", "The authorized O5 workflow contains no matching action or recovery facts by this cutoff."))
                        else:
                            with ui.element("div").classes("ephi-assets-actions"):
                                for item in result.changes:
                                    with Card():
                                        ui.label(f"{item['kind']} · Episode {item['episode_id']}").classes("text-subtitle1")
                                        if item["kind"] == "ACTION":
                                            ui.label(_action_summary(item))
                                        else:
                                            ui.label(f"{item.get('recovery_plan_id') or item.get('closure_identity') or item.get('reopen_identity')} · state {item.get('state') or item.get('status') or 'recorded'} · recorded {item.get('recorded_at') or item.get('closed_at') or item.get('reopened_at') or 'not separately recorded'}")
                    with tabs.panel("measurement"):
                        ui.label(f"Exact source binding {result.measurement.get('binding_identity')} · O4 snapshot {result.source.get('snapshot_id') or 'unavailable'} · source revision {result.source.get('source_revision') or 'unavailable'}").classes("ephi-assets-mono")
                        ui.label(f"Source {result.measurement.get('source_id') or result.source.get('source_id') or 'unavailable'} · schema {result.measurement.get('schema_id') or result.source.get('schema_id') or 'unavailable'} · mapping {result.measurement.get('mapping_version') or result.source.get('mapping_version') or 'unavailable'} · hash {result.measurement.get('mapping_hash') or result.source.get('mapping_hash') or 'unavailable'}").classes("ephi-assets-mono")
                        ui.label(f"Window {_stamp(start)} through {_stamp(end)} · knowledge cutoff {_stamp(cutoff)} · snapshot published {_stamp(result.source.get('published_at'))} · {len(result.measurement['points'])} bounded point(s) · no EPHI raw-row persistence").classes("ephi-assets-time")
                        ui.label("Observation points are unconnected; missing periods remain visible gaps. No interpolation or smoothing is applied.").classes("ephi-assets-note")
                        if result.measurement["points"]:
                            if result.compare and result.compare["state"] == "READY":
                                ui.html(_series_svg(result.measurement["points"], result.compare["peer_points"], result.asset["unit_identity"]), sanitize=False)
                                ui.label(f"Primary {asset_id} · peer {result.compare['peer_asset_id']} · exact qualified population {result.compare['population_identity']} · descriptive observations only.").classes("ephi-assets-note")
                            else:
                                ui.html(_series_svg(result.measurement["points"], None, result.asset["unit_identity"]), sanitize=False)
                            with ui.element("div").classes("ephi-assets-desktop-table ephi-assets-table-wrap"):
                                DataTable(
                                    result.measurement["points"],
                                    spec=DataTableSpec((
                                        TableColumn("event_at", "Event time", ColumnKind.DATETIME, sortable=False, filterable=False),
                                        TableColumn("source_available_at", "Source available", ColumnKind.DATETIME, sortable=False, filterable=False),
                                        TableColumn("value", f"Value ({result.asset['unit_identity']})", ColumnKind.FLOAT, sortable=False, filterable=False),
                                        TableColumn("source_row_id", "Source observation identity", ColumnKind.TEXT, sortable=False, filterable=False),
                                    ), row_key="source_row_id", title="Bounded observations", pagination=PaginationMode.CLIENT, page_size=25, searchable=False, column_manager=False, density_control=False, export_enabled=False, copy_enabled=False, persist_state=False),
                                    row_key="source_row_id",
                                )
                            with ui.element("div").classes("ephi-assets-mobile-list").props('role="list" aria-label="Measurement observations with all exact identities"'):
                                for observation in result.measurement["points"]:
                                    with Card() as observation_card:
                                        observation_card.element.props('role="listitem"')
                                        ui.label(f"{observation['value']} {observation['unit']} · {observation['event_at'].isoformat()}").classes("text-subtitle1")
                                        ui.label(f"Source available {observation['source_available_at'].isoformat()}")
                                        ui.label(f"Source observation {observation['source_row_id']}").classes("ephi-assets-mono")
                        elif result.measurement["state"] == "UNAVAILABLE":
                            with Card():
                                StatusBadge("HISTORICAL MEASUREMENT UNAVAILABLE", intent=StatusIntent.DANGER)
                                ui.label(f"Exact revision read limitation: {result.measurement['limitations'][0]}").classes("ephi-assets-state")
                        else:
                            StateView(StateViewSpec(StateKind.EMPTY, "No observations in the selected window", "No exact asset/context/characteristic/unit observation was both event-time eligible and available by this knowledge cutoff."))
                        if result.compare is not None:
                            if result.compare["state"] == "READY":
                                StatusBadge("QUALIFIED DESCRIPTIVE COMPARE", intent=StatusIntent.SUCCESS)
                            else:
                                StatusBadge("COMPARE BLOCKED", intent=StatusIntent.DANGER)
                                ui.label(f"Blocked reason: {result.compare['reason']}").classes("ephi-assets-state")
                        if result.source["state"] != "READY":
                            StatusBadge(f"CAPABILITY {result.source['state']}", intent=StatusIntent.WARNING if result.source["state"] in {"STALE", "PARTIAL"} else StatusIntent.DANGER)
                    with tabs.panel("material"):
                        with Card():
                            StatusBadge("MATERIAL CONTEXT UNAVAILABLE", intent=StatusIntent.WARNING)
                            ui.label("No existing qualified EPHI source provides WIP, lot, wafer, or material context for this Asset query. No material data is fabricated.")
                with ui.element("div").classes("ephi-assets-coverage"):
                    with Card():
                        ui.label("O4 capability coverage").classes("text-subtitle1")
                        ui.label(f"Effective state {result.source['state']} · O4 recorded state {result.source.get('o4_state', 'UNAVAILABLE')} · age {result.source.get('age_seconds') if result.source.get('age_seconds') is not None else 'unknown'} seconds")
                        ui.label(f"Binding {result.source.get('binding_identity') or 'unavailable'} · snapshot {result.source.get('snapshot_id') or 'unavailable'} · manifest {result.source.get('snapshot_manifest_hash') or 'unavailable'}").classes("ephi-assets-mono")
                        ui.label(f"Source {result.source.get('source_id') or 'unavailable'} · provider {result.source.get('provider_id') or 'unavailable'} · adapter {result.source.get('adapter_id') or 'unavailable'} · capability {result.source.get('capability_id') or 'unavailable'}").classes("ephi-assets-mono")
                        ui.label(f"Schema {result.source.get('schema_id') or 'unavailable'} · mapping {result.source.get('mapping_version') or 'unavailable'} · hash {result.source.get('mapping_hash') or 'unavailable'}").classes("ephi-assets-mono")
                        ui.label(f"Reference {result.source.get('reference_population_id') or 'unqualified'} · comparable {result.source.get('comparable_population_id') or 'unqualified'}")
                        ui.label(f"Latest event { _stamp(result.source.get('latest_event_at')) } · available { _stamp(result.source.get('latest_available_at')) } · reason {result.source.get('reason') or 'none'}")
                        ui.label(f"As of knowledge cutoff {_stamp(result.knowledge_cutoff)} · snapshot published {_stamp(result.source.get('published_at'))}").classes("ephi-assets-time")
                    with Card():
                        ui.label("Query limitations").classes("text-subtitle1")
                        for limitation in result.limitations:
                            ui.label(limitation).classes("ephi-assets-time")


__all__ = ["build_asset_360_page", "build_asset_index_page"]
