"""Composition and pages for the narrow durable Attention → Episode slice."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from collections.abc import Callable, Mapping

from nicegui_base import (
    ActionButton,
    AnalysisWorkspacePage,
    AppShell,
    ApplicationRuntime,
    ButtonIntent,
    Card,
    DataTable,
    DataSourceTable,
    DescriptionList,
    EntityHeader,
    FullScreenWorkspace,
    KeyValueItem,
    LayoutSlot,
    MasterDetailPage,
    MetricCard,
    MetricStrip,
    NavItem,
    NavSection,
    NavigationModel,
    PanelSpec,
    Selection,
    SelectionKind,
    SelectionMode,
    ServerDataTableSpec,
    StateKey,
    StateKind,
    StateNamespace,
    StateView,
    StateViewSpec,
    StatusBadge,
    StatusIntent,
    TableColumn,
    TableDensity,
    ColumnKind,
    AsyncLoader,
    StaleResponseGuard,
    WorkspaceController,
    NiceGUIRuntimeAdapter,
    RowAction,
)

from ephi.application.attention import AttentionQueryService
from ephi.application.assets import Asset360QueryService
from ephi.application.context import AccessScope, CommandContext, CurrentAuthorizationAuthority, Principal
from ephi.application.errors import AuthorizationDeniedError, VersionConflictError
from ephi.application.transactions import VersionedAggregateCommandExecutor
from ephi.application.episodes import EpisodeBrief, EpisodeBriefQueryService
from ephi.application.investigation import (
    ComponentState,
    EpisodeInvestigation,
    EpisodeInvestigationQueryService,
    InvestigationProfile,
)
from ephi.application.decision_loop import CheckExecutionMode
from ephi.application.decision_loop import CHECK_REQUEST_CAPABILITY
from ephi.application.planner import CheckTemplateCatalog
from ephi.application.workflow import EpisodeWorkflowCommandService
from ephi.application.o10 import (
    attention_result_status as _attention_result_status,
    display_value as _display_value,
    episode_action as _episode_action,
    state_for_error as _state_for_error_facts,
)
from ephi.application.source_reality import require_runtime_source_binding
from ephi.config import RuntimeSettings, downstream_entrypoint_from_environment
from ephi.downstream import DownstreamComposition, compose_downstream, load_provider_bundle
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter
from ephi.value import EventPeriod, OutcomesQueryResult, OutcomeRecord, OutcomesService, ReviewDecision
from ephi.value.repository import OutcomeAggregateRepository
from ephi.transport import (
    build_runtime_security_contract,
    install_browser_transport_stack,
    require_security_preflight,
)
from .provider import EphiReadDataSource


ORIGIN_KEY = StateKey("ephi.origin", StateNamespace.WORKSPACE, default="/attention")
EPISODE_KEY = StateKey("ephi.episode_id", StateNamespace.WORKSPACE, default=None)
FILTER_KEY = StateKey("ephi.attention.filters", StateNamespace.WORKSPACE, default={})
DRAFT_KEY = StateKey("ephi.episode.draft", StateNamespace.WORKSPACE, default={})
FOCUS_KEY = StateKey("ephi.focus_target", StateNamespace.WORKSPACE, default=None)
_O10_FOCUS_SCRIPT_INSTALLED = False


_O10_UI_CSS = """
.ephi-o10-semantic-header {
    grid-column: 1 / -1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--cui-space-1);
}
.ephi-o10-page-heading {
    margin: 0;
    color: var(--cui-text-primary);
    font-size: var(--cui-font-size-24);
    line-height: var(--cui-line-height-29);
    overflow-wrap: anywhere;
}
.ephi-o10-page-description,
.ephi-o10-scope-note,
.ephi-o10-truth-note {
    color: var(--cui-text-secondary);
    overflow-wrap: anywhere;
}
.ephi-o10-scope-note,
.ephi-o10-truth-note,
.ephi-o10-live-status {
    border: 1px solid var(--cui-border-subtle);
    border-radius: var(--cui-radius-md);
    background: var(--cui-surface-secondary);
    padding: var(--cui-space-2) var(--cui-space-3);
}
.ephi-o10-live-status {
    color: var(--cui-text-primary);
}
.ephi-o10-preview {
    min-width: 0;
}
.ephi-o10-attention-surface .cui-table-search,
.ephi-o10-attention-surface .cui-table-tool-button {
    min-height: 44px;
    height: 44px;
}
.ephi-o10-attention-surface .cui-table-tool-button {
    min-height: 44px !important;
    height: 44px !important;
    min-width: 44px !important;
}
.ephi-o10-attention-surface .cui-table-search {
    box-sizing: border-box;
}
.ephi-o10-preview .cui-button,
.ephi-o10-episode-surface .cui-button {
    min-height: 44px !important;
}
.ephi-o10-preview .cui-button {
    min-width: 44px !important;
}
.ephi-o10-episode-surface .cui-button {
    min-width: 44px !important;
}
.ephi-o10-episode-surface .ephi-investigation-heading {
    margin: 0 0 var(--cui-space-2);
    color: var(--cui-text-primary);
    font-size: var(--cui-font-size-lg);
    line-height: var(--cui-line-height-29);
    overflow-wrap: anywhere;
}
.ephi-o10-episode-surface .ephi-investigation-subheading {
    margin: var(--cui-space-2) 0 var(--cui-space-1);
    color: var(--cui-text-primary);
    font-size: 18px;
    line-height: 24px;
    overflow-wrap: anywhere;
}
.ephi-o10-preview h2 {
    margin: 0 0 var(--cui-space-2);
    color: var(--cui-text-primary);
    font-size: var(--cui-font-size-lg);
}
.ephi-o10-action-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--cui-space-2);
    align-items: center;
}
.ephi-o10-focus-target:focus,
.ephi-o10-focus-target:focus-visible,
button.ephi-o10-focus-target:focus,
button.ephi-o10-focus-target:focus-visible,
html body button.ephi-o10-focus-target:focus,
html body button.ephi-o10-focus-target:focus-visible,
.ephi-o10-preview .cui-button:focus,
.ephi-o10-preview .cui-button:focus-visible,
.ephi-o10-episode-surface .cui-button:focus,
.ephi-o10-episode-surface .cui-button:focus-visible {
    outline-style: solid !important;
    outline-width: 3px !important;
    outline-color: var(--cui-focus-ring, rgb(0 94 168)) !important;
    outline-offset: 2px !important;
    box-shadow: none !important;
}
@media (max-width: 899px) {
    .ephi-o10-action-row > * {
        min-width: min(100%, 15rem);
    }
    .cui-shell-mobile-menu.cui-icon-button {
        min-width: 44px !important;
        width: 44px !important;
    }
}
@media (prefers-reduced-motion: reduce) {
    .ephi-o10-semantic-header,
    .ephi-o10-preview,
    .ephi-o10-live-status {
        animation: none;
        transition: none;
    }
}
@media (forced-colors: active) {
    .ephi-o10-scope-note,
    .ephi-o10-truth-note,
    .ephi-o10-live-status {
        border: 1px solid CanvasText;
        color: CanvasText;
        background: Canvas;
    }
    .ephi-o10-focus-target:focus,
    .ephi-o10-focus-target:focus-visible {
        outline: 2px solid Highlight !important;
    }
}
"""

_OUTCOMES_CSS = """
.ephi-outcomes, .ephi-outcomes * { min-width: 0; }
.ephi-outcomes-page-heading { font-size: clamp(1.875rem, 4vw, 3rem) !important; line-height: 1.1 !important; white-space: nowrap; }
.ephi-outcomes-controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: var(--cui-space-3); align-items: end; }
.ephi-outcomes-summaries { width: 100%; overflow: hidden; }
.ephi-outcomes-summaries .cui-metric-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: var(--cui-space-3); }
.ephi-outcomes-table { width: 100%; overflow-x: auto; }
.ephi-outcomes-detail { overflow-wrap: anywhere; }
.ephi-outcomes-banner { border: 1px solid var(--cui-border-subtle); border-radius: var(--cui-radius-md); background: var(--cui-surface-secondary); padding: var(--cui-space-3); overflow-wrap: anywhere; }
.ephi-outcomes-action-row { display: flex; flex-wrap: wrap; gap: var(--cui-space-2); align-items: center; }
.ephi-outcomes-action-row .cui-button,
.ephi-outcomes-action-row .q-btn,
.ephi-outcomes-action-row button { min-height: 44px !important; min-width: 44px !important; }
.ephi-outcomes-focus:focus, .ephi-outcomes-focus:focus-visible { outline: 3px solid var(--cui-focus-ring, rgb(0 94 168)) !important; outline-offset: 2px !important; }
.ephi-outcomes-controls .q-field:focus-within { outline: 3px solid var(--cui-focus-ring, rgb(0 94 168)) !important; outline-offset: 2px !important; border-radius: var(--cui-radius-sm); }
@media (max-width: 600px) {
    .ephi-outcomes-controls, .ephi-outcomes-summaries .cui-metric-strip { grid-template-columns: minmax(0, 1fr); }
    .ephi-outcomes-action-row > * { min-width: min(100%, 15rem); }
}
"""


class _StaleResponseError(RuntimeError):
    """A superseded async response was intentionally discarded."""


@dataclass(slots=True)
class _DevelopmentIdentityProvider:
    """Resolve the explicit development/test identity at operation time."""

    def scope(self) -> AccessScope:
        return AccessScope(
            _required_environment("EPHI_DEV_SCOPE_ID"),
            site_id=os.environ.get("EPHI_DEV_SITE_ID") or None,
            area_id=os.environ.get("EPHI_DEV_AREA_ID") or None,
            family_id=os.environ.get("EPHI_DEV_FAMILY_ID") or None,
        )

    def principal(self) -> Principal:
        return _development_principal_from_environment(self.scope())


def _stable_command_id(action: str, brief: EpisodeBrief, scope: AccessScope, subject: str) -> str:
    """Derive one replay identity for one rendered decision action."""

    identity = {
        "action": action,
        "episode_id": brief.episode_id,
        "revision_id": brief.revision_id,
        "revision_vector": brief.revision_vector.as_dict(),
        "scope": scope.canonical_key,
        "subject": subject,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _identity(value: str, field: str) -> str:
    if not value or value != value.strip() or "\x00" in value:
        raise RuntimeError(f"missing or invalid {field} binding")
    return value


def _required_environment(name: str) -> str:
    return _identity(os.environ.get(name, ""), name)


def _int_environment(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be non-negative")
    return value


@dataclass(slots=True)
class EphiUiComposition:
    adapter: PostgreSQLReferenceTransactionAdapter
    principal_provider: Callable[[], Principal]
    scope_provider: Callable[[], AccessScope]
    current_authorization: CurrentAuthorizationAuthority
    attention: AttentionQueryService
    briefs: EpisodeBriefQueryService
    workflow: EpisodeWorkflowCommandService
    source: EphiReadDataSource
    metrology_source_adapter: object
    metrology_source_binding: object
    runtime: ApplicationRuntime
    workspace: object
    downstream: DownstreamComposition | None = None
    investigations: EpisodeInvestigationQueryService | None = None
    outcomes: OutcomesService | None = None
    asset_360: Asset360QueryService | None = None

    def close(self) -> None:
        self.adapter.close()


def _development_principal_from_environment(scope: AccessScope) -> Principal:
    environment = os.environ.get("EPHI_ENV", "development").lower()
    if environment not in {"development", "test"}:
        raise RuntimeError("production identity binding is unavailable; EPHI fails closed")
    capabilities = tuple(
        item.strip()
        for item in _required_environment("EPHI_DEV_IDENTITY_CAPABILITIES").split(",")
        if item.strip()
    )
    return Principal(
        _required_environment("EPHI_DEV_IDENTITY_SUBJECT"),
        capabilities,
        (scope,),
        _int_environment("EPHI_DEV_AUTH_SESSION_REVISION", 1),
        _int_environment("EPHI_DEV_SECURITY_REVISION", 1),
    )


def build_composition_from_environment() -> EphiUiComposition:
    """Compose through the explicit downstream ABI or the retained dev/test path."""

    downstream_entrypoint = downstream_entrypoint_from_environment()
    if downstream_entrypoint:
        downstream = compose_downstream(
            load_provider_bundle(downstream_entrypoint),
            runtime_settings=RuntimeSettings.from_environment(),
        )
        principal_provider = downstream.principal_provider
        scope_provider = downstream.scope_provider
        source = EphiReadDataSource(downstream.attention, principal_provider, scope_provider)
        asset_360 = Asset360QueryService(
            downstream.adapter.o3_store(), downstream.adapter.read_store(), downstream.current_authorization,
            downstream.source_observer, downstream.source_binding, downstream.adapter.source_store(),
        )
        runtime = ApplicationRuntime()
        runtime.data.register_source(source)
        workspace = runtime.open_workspace("ephi-attention-episode-w1")
        workspace.state.set(FILTER_KEY, {}, source="ephi.initial")
        return EphiUiComposition(
            downstream.adapter,
            principal_provider,
            scope_provider,
            downstream.current_authorization,
            downstream.attention,
            downstream.episode_briefs,
            downstream.workflow,
            source,
            downstream.source_observer,
            downstream.source_binding,
            runtime,
            workspace,
            downstream,
            downstream.episode_investigations,
            downstream.outcomes,
            asset_360,
        )
    dsn = _required_environment("EPHI_POSTGRES_DSN")
    metrology_source_adapter, metrology_source_binding = require_runtime_source_binding()
    identity = _DevelopmentIdentityProvider()
    principal_provider = identity.principal
    scope_provider = identity.scope
    current_authorization = CurrentAuthorizationAuthority.from_provider(principal_provider)
    # Validate the current binding during composition, but never retain this
    # Principal as page authority.  Every protected operation calls the bound
    # provider again.
    principal_provider()
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    o3_store = adapter.o3_store()
    attention = AttentionQueryService(o3_store, adapter.read_store(), current_authorization)
    briefs = EpisodeBriefQueryService(adapter.read_store(), current_authorization)
    workflow = EpisodeWorkflowCommandService(adapter, current_authorization)
    outcomes = OutcomesService(
        OutcomeAggregateRepository(adapter),
        VersionedAggregateCommandExecutor(adapter, current_authorization),
        current_authorization,
    )
    asset_360 = Asset360QueryService(
        adapter.o3_store(), adapter.read_store(), current_authorization,
        metrology_source_adapter, metrology_source_binding, adapter.source_store(),
    )
    source = EphiReadDataSource(attention, principal_provider, scope_provider)
    runtime = ApplicationRuntime()
    runtime.data.register_source(source)
    workspace = runtime.open_workspace("ephi-attention-episode-w1")
    workspace.state.set(FILTER_KEY, {}, source="ephi.initial")
    return EphiUiComposition(
        adapter,
        principal_provider,
        scope_provider,
        current_authorization,
        attention,
        briefs,
        workflow,
        source,
        metrology_source_adapter,
        metrology_source_binding,
        runtime,
        workspace,
        outcomes=outcomes,
        asset_360=asset_360,
    )


def _navigation() -> NavigationModel:
    return NavigationModel((NavSection("work", "Work", (
        NavItem("attention", "Attention", "/"),
        NavItem("episode", "Episode", "/episode"),
        NavItem("assets", "Assets", "/ephi/assets"),
        NavItem("outcomes", "Outcomes", "/ephi/outcomes"),
        NavItem("families", "Family Center", "/ephi/families"),
    )),))


def _intent_for_capability(state: object) -> StatusIntent:
    value = str(state).upper()
    if value in {"READY", "HEALTHY"}:
        return StatusIntent.SUCCESS
    if value in {"PARTIAL", "STALE", "INSUFFICIENT"}:
        return StatusIntent.WARNING
    if value in {"UNAVAILABLE", "ERROR", "NOT_QUALIFIED"}:
        return StatusIntent.DANGER
    return StatusIntent.NEUTRAL


def _state_for_error(error: BaseException) -> StateViewSpec:
    if isinstance(error, _StaleResponseError):
        return StateViewSpec(StateKind.ERROR, "Stale response", "A newer request is authoritative; the older response was discarded.")
    state = _state_for_error_facts(error)
    state_kind = {"permission": StateKind.PERMISSION, "offline": StateKind.OFFLINE}.get(state.kind, StateKind.ERROR)
    return StateViewSpec(state_kind, state.title, state.message, action_label=state.action_label)


def _attention_columns() -> tuple[TableColumn, ...]:
    return (
        TableColumn("episode_id", "Episode", ColumnKind.LINK, priority="high"),
        TableColumn("priority", "Priority", ColumnKind.STATUS, status_map={"P1": "danger", "P2": "warning", "P3": "info"}),
        TableColumn("title", "Issue", ColumnKind.TEXT, priority="high"),
        TableColumn("asset_id", "Asset", ColumnKind.TEXT),
        TableColumn("source_state", "Source", ColumnKind.STATUS),
        TableColumn("owner", "Owner", ColumnKind.TEXT),
        TableColumn("work_state", "Work", ColumnKind.STATUS),
        TableColumn("deadline", "Decision deadline", ColumnKind.DATETIME),
    )


def _render_attention_error(
    error: BaseException,
    *,
    on_refresh: Callable[..., object] | None = None,
) -> None:
    StateView(_state_for_error(error), on_action=on_refresh)


def _install_o10_ui_css() -> None:
    from nicegui import ui

    ui.add_css(_O10_UI_CSS, shared=True)
    # This is a focus-only browser seam for replacement renders. It never
    # selects rows or navigates routes; it waits for the requested Base action
    # to be mounted, then hands focus to that action and clears the request.
    focus_observer_script = """(() => {
            if (window.__ephiO10FocusObserver) return;
            const focusRequested = () => {
                const request = document.querySelector('[data-ephi-focus-request]');
                const marker = request?.getAttribute('data-ephi-focus-request');
                if (!marker) return;
                const target = document.querySelector(`[data-ephi-focus-target="${marker}"]`);
                if (!target || !target.isConnected) return;
                target.focus({preventScroll: true});
                if (document.activeElement === target) request.removeAttribute('data-ephi-focus-request');
            };
            window.__ephiO10FocusObserver = new MutationObserver(focusRequested);
            window.__ephiO10FocusObserver.observe(document.documentElement, {subtree: true, childList: true, attributes: true, attributeFilter: ['data-ephi-focus-target', 'data-ephi-focus-request']});
        })()"""

    global _O10_FOCUS_SCRIPT_INSTALLED
    if not _O10_FOCUS_SCRIPT_INSTALLED:
        ui.add_body_html(f"<script>{focus_observer_script}</script>", shared=True)
        _O10_FOCUS_SCRIPT_INSTALLED = True


def _schedule_focus(element: object | None, *, marker: str | None = None) -> None:
    """Move focus after a replacement render reaches the browser."""

    if element is None:
        return

    async def focus_after_render() -> None:
        await asyncio.sleep(0)
        try:
            from nicegui import ui

            element_id = getattr(element, "id", None)
            if element_id is None and marker is None:
                return
            marker_selector = "" if marker is None else f"[data-ephi-focus-target={json.dumps(marker)}]"
            exact_expression = "null" if element_id is None else f"document.getElementById({json.dumps(str(element_id))})"
            await ui.run_javascript(
                "setTimeout(() => {"
                f"const exact={exact_expression};"
                "const exactBox=exact?.getBoundingClientRect();"
                "const exactVisible=exact && exact.isConnected && exactBox && exactBox.width > 0 && exactBox.height > 0 ? exact : null;"
                f"const fallback=[...document.querySelectorAll({json.dumps(marker_selector or '[data-ephi-focus-target]')})].find(node => {{"
                "const box=node.getBoundingClientRect(); return box.width > 0 && box.height > 0;});"
                "(exactVisible || fallback)?.focus();"
                "}, 100)"
            )
        except Exception:
            return

    try:
        asyncio.get_running_loop().create_task(focus_after_render())
    except RuntimeError:
        return


def _mark_focus_target(element: object, marker: str) -> None:
    """Mark a replacement-safe focus target using the Base semantic focus token."""

    element.classes("ephi-o10-focus-target")  # type: ignore[attr-defined]
    element.props(f'data-ephi-focus-target="{marker}"')  # type: ignore[attr-defined]
    # Base's button contract intentionally suppresses the native outline. Keep
    # this scoped to the two W1 decision controls and carry the same semantic
    # focus token inline so the rendered browser proof is deterministic.
    element.style(  # type: ignore[attr-defined]
        "outline-style: solid !important; outline-width: 3px !important; "
        "outline-offset: 2px !important;"
    )


def _ensure_outcomes_action_target(button: ActionButton) -> ActionButton:
    """Keep the reviewer action and sign-off targets at least 44 CSS pixels."""

    button.element.style("min-height:44px !important; min-width:44px !important;")
    return button


def _semantic_heading(title: str, description: str, *, autofocus: bool = False) -> object:
    from nicegui import ui

    with ui.element("header").classes("ephi-o10-semantic-header"):
        heading = ui.element("h1").classes("ephi-o10-page-heading ephi-o10-focus-target")
        heading.props(f'tabindex="-1" data-ephi-focus-target="page-heading"{" autofocus" if autofocus else ""}')
        with heading:
            ui.label(title)
        ui.label(description).classes("ephi-o10-page-description")
    return heading


def _set_attention_selection(composition: EphiUiComposition, row: Mapping[str, object]) -> bool:
    episode_id = row.get("episode_id")
    if not isinstance(episode_id, str) or not episode_id:
        return False
    composition.workspace.state.set(ORIGIN_KEY, "/", source="ephi.attention")
    composition.workspace.state.set(EPISODE_KEY, episode_id, source="ephi.attention")
    composition.workspace.selections.apply(
        Selection(SelectionKind.ENTITY, episode_id, {"episode_id": episode_id}, source="attention"),
        source="ephi.attention",
    )
    return True


def _open_episode(composition: EphiUiComposition, row: Mapping[str, object]) -> None:
    """Open exactly the selected permitted row through workspace state."""

    if not _set_attention_selection(composition, row):
        return
    composition.workspace.state.set(FOCUS_KEY, "episode_heading", source="ephi.attention")
    from nicegui import ui

    ui.navigate.to("/episode")


class _AttentionPreview:
    def __init__(self, host: object, composition: EphiUiComposition) -> None:
        self.host = host
        self.composition = composition
        self.row: dict[str, object] | None = None
        self.open_button: ActionButton | None = None
        self.render_empty()

    def render_empty(self) -> None:
        from nicegui import ui

        self.row = None
        self.open_button = None
        self.host.clear()  # type: ignore[attr-defined]
        with self.host:  # type: ignore[attr-defined]
            with ui.element("aside").classes("ephi-o10-preview").props('aria-label="Selected episode preview"'):
                heading = ui.element("h2").classes("ephi-o10-focus-target").props('tabindex="-1"')
                with heading:
                    ui.label("Selected episode preview")
                StateView(
                    StateViewSpec(
                        StateKind.EMPTY,
                        "Select one permitted episode",
                        "Single-row selection shows only returned Attention facts. Use the Open episode action to continue; no double-click is required.",
                    )
                )

    async def select(self, rows: list[Mapping[str, object]]) -> None:
        if not rows or not _set_attention_selection(self.composition, rows[0]):
            self.render_empty()
            return
        self.render_row(rows[0])

    def render_row(self, row: Mapping[str, object]) -> None:
        from nicegui import ui

        self.row = dict(row)
        self.open_button = None
        self.host.clear()  # type: ignore[attr-defined]
        with self.host:  # type: ignore[attr-defined]
            with ui.element("aside").classes("ephi-o10-preview").props('aria-label="Selected episode preview"'):
                heading = ui.element("h2").classes("ephi-o10-focus-target").props('tabindex="-1"')
                with heading:
                    ui.label("Selected episode preview")
                ui.label("Facts below are limited to the selected, authorized Attention row.").classes("ephi-o10-truth-note")
                DescriptionList(
                    (
                        KeyValueItem("episode_id", "Episode ID", _display_value(row.get("episode_id"))),
                        KeyValueItem("title", "Issue / title", _display_value(row.get("title"))),
                        KeyValueItem("priority", "Priority", _display_value(row.get("priority"))),
                        KeyValueItem("source_state", "Source state", _display_value(row.get("source_state"))),
                        KeyValueItem("owner", "Owner", _display_value(row.get("owner"))),
                        KeyValueItem("work_state", "Workflow state", _display_value(row.get("work_state"))),
                        KeyValueItem("deadline", "Decision deadline", _display_value(row.get("deadline"))),
                    )
                )
                with ui.element("div").classes("ephi-o10-action-row"):
                    self.open_button = ActionButton(
                        "Open episode",
                        intent=ButtonIntent.PRIMARY,
                        on_click=lambda: _open_episode(self.composition, self.row or {}),
                    )
                    _mark_focus_target(self.open_button.element, "open-action")

        if self.composition.workspace.state.get(FOCUS_KEY) == "attention_open":
            _schedule_focus(self.open_button.element if self.open_button else None, marker="open-action")
            self.composition.workspace.state.set(FOCUS_KEY, None, source="ephi.attention.focus")


def build_attention_page(composition: EphiUiComposition) -> None:
    _install_o10_ui_css()
    workspace = composition.workspace
    with AppShell(
        "EPHI",
        _navigation(),
        active_route="/",
        environment=os.environ.get("EPHI_ENV", "development"),
        subtitle="Attention → Episode",
        user_name=composition.principal_provider().subject,
        user_role="Engineer",
        debugger=False,
    ):
        from nicegui import ui

        ui.query("main").props('role="region" aria-label="EPHI application content"')
        with MasterDetailPage("", None) as page:
            heading = _semantic_heading(
                "Attention",
                "Scoped engineering work with retained, coherent reads",
                autofocus=workspace.state.get(FOCUS_KEY) == "attention_heading",
            )
            with page.slot(LayoutSlot.FILTERS):
                ui.label("Authorized scope and source coverage").classes("ephi-o10-scope-note")
                attention_status = ui.label(_attention_result_status(total_count=None, loading=True)).classes("ephi-o10-live-status")
                attention_status.props('role="status" aria-live="polite" aria-atomic="true" tabindex="-1"')
            attention_data_slot = page.slot(LayoutSlot.DATA)
            attention_data_slot.props('tabindex="-1" aria-label="Attention result state"')
            preview: _AttentionPreview | None = None
            with attention_data_slot:
                attention_table: DataSourceTable | None = None

                async def refresh_attention() -> None:
                    if attention_table is None:
                        return
                    await attention_table.refresh(force=True)
                    _schedule_focus(attention_status)

                async def render_attention_error(error: BaseException) -> None:
                    attention_status.set_text(_state_for_error(error).title)  # type: ignore[attr-defined]
                    with attention_data_slot:
                        _render_attention_error(error, on_refresh=refresh_attention)
                    _schedule_focus(attention_data_slot)

                async def on_attention_select(rows: list[Mapping[str, object]]) -> None:
                    if preview is not None:
                        await preview.select(rows)

                async def on_attention_view_changed(snapshot: object) -> None:
                    count = getattr(snapshot, "displayed_count", None)
                    search = str(getattr(snapshot, "search", "") or "")
                    attention_status.set_text(_attention_result_status(total_count=count, search=search))  # type: ignore[attr-defined]
                    selected_id = workspace.state.get(EPISODE_KEY)
                    visible_rows = getattr(snapshot, "visible_rows", ()) or ()
                    if preview is not None and isinstance(selected_id, str) and preview.row is None:
                        for visible_row in visible_rows:
                            if isinstance(visible_row, Mapping) and visible_row.get("episode_id") == selected_id:
                                preview.render_row(visible_row)
                                break

                attention_search_generation = 0

                async def on_attention_search(event: object) -> None:
                    """Reflect Base's completed server search in the app-owned live region."""

                    nonlocal attention_search_generation
                    attention_search_generation += 1
                    generation = attention_search_generation
                    raw_value = getattr(event, "args", "")
                    if isinstance(raw_value, Mapping):
                        raw_value = raw_value.get("value", "")
                    if isinstance(raw_value, (tuple, list)):
                        raw_value = raw_value[0] if raw_value else ""
                    expected_search = str(raw_value or "")
                    attention_status.set_text(_attention_result_status(total_count=None, search=expected_search, loading=True))  # type: ignore[attr-defined]
                    for _ in range(3000):
                        if generation != attention_search_generation or attention_table is None:
                            return
                        query = getattr(attention_table, "query", None)
                        if not getattr(attention_table, "loading", True) and str(getattr(query, "search", "") or "") == expected_search:
                            count = getattr(attention_table, "total", None)
                            attention_status.set_text(_attention_result_status(total_count=count, search=expected_search))  # type: ignore[attr-defined]
                            return
                        await asyncio.sleep(0.01)
                    attention_status.set_text("Attention query completion was not confirmed; the current result is unavailable.")  # type: ignore[attr-defined]

                try:
                    from nicegui import ui

                    with ui.element("section").classes("ephi-o10-attention-surface").props('role="region" aria-label="Attention results table"'):
                        attention_table = DataSourceTable(
                            composition.source,
                            schema=composition.source.schema_definition,
                            context=workspace.analysis,
                            selections=workspace.selections,
                            row_key="episode_id",
                            title="Attention list",
                            selection=SelectionMode.SINGLE,
                            spec=ServerDataTableSpec(
                                _attention_columns(),
                                row_key="episode_id",
                                title="Attention list",
                                density=TableDensity.COMPACT,
                                selection=SelectionMode.SINGLE,
                                page_size=50,
                                page_size_options=(25, 50, 100),
                                cancel_stale_requests=True,
                                cache_pages=0,
                                empty_message="No rows in the current Attention view",
                                error_message="Attention is unavailable; last known truth is not replaced with zero.",
                            ),
                            row_actions=(
                                RowAction(
                                    "open_episode",
                                    "Open episode",
                                    icon="external-link",
                                    intent="primary",
                                    on_action=lambda row: _open_episode(composition, row),
                                ),
                            ),
                            on_select=on_attention_select,
                            on_view_changed=on_attention_view_changed,
                            on_error=render_attention_error,
                        )
                        search_input = getattr(getattr(attention_table, "toolbar", None), "search_input", None)
                        if search_input is not None:
                            search_input.on(
                                "input",
                                on_attention_search,
                                throttle=0.18,
                                leading_events=False,
                                trailing_events=True,
                                js_handler="e => emit(e.target.value)",
                            )
                except Exception as error:
                    _render_attention_error(error)
            with page.slot(LayoutSlot.DETAILS):
                from nicegui import ui

                preview_host = ui.element("div").classes("ephi-o10-preview")
                preview = _AttentionPreview(preview_host, composition)
            if workspace.state.get(FOCUS_KEY) == "attention_heading":
                _schedule_focus(heading)
                workspace.state.set(FOCUS_KEY, None, source="ephi.attention.focus")


def _select_episode(composition: EphiUiComposition, event: object) -> None:
    row = (getattr(event, "args", {}) or {}).get("data") or {}
    if isinstance(row, Mapping):
        _set_attention_selection(composition, row)


async def _load_brief(composition: EphiUiComposition, episode_id: str, guard: StaleResponseGuard) -> EpisodeBrief:
    token = guard.next()
    loader = AsyncLoader(timeout=30)
    brief = await loader.load(
        lambda: asyncio.to_thread(
            composition.briefs.get_episode_brief,
            composition.principal_provider(),
            composition.scope_provider(),
            episode_id,
        )
    )
    if brief is None or not guard.is_current(token):
        raise _StaleResponseError("stale Episode response was discarded")
    return brief


async def _load_investigation(
    composition: EphiUiComposition,
    episode_id: str,
    guard: StaleResponseGuard,
) -> tuple[EpisodeBrief, EpisodeInvestigation | None]:
    token = guard.next()
    loader = AsyncLoader(timeout=30)

    def query() -> tuple[EpisodeBrief, EpisodeInvestigation | None]:
        principal = composition.principal_provider()
        scope = composition.scope_provider()
        if composition.investigations is None:
            return composition.briefs.get_episode_brief(principal, scope, episode_id), None
        investigation = composition.investigations.get_episode_investigation(principal, scope, episode_id)
        return investigation.brief, investigation

    result = await loader.load(lambda: asyncio.to_thread(query))
    if result is None or not guard.is_current(token):
        raise _StaleResponseError("stale Episode investigation response was discarded")
    return result


def _section_heading(ui: object, text: str, *, level: int = 2) -> None:
    heading = ui.element(f"h{level}").classes(  # type: ignore[attr-defined]
        "ephi-investigation-heading" if level == 2 else "ephi-investigation-subheading"
    )
    with heading:
        ui.label(text)  # type: ignore[attr-defined]


def _reason_text(reason_codes: object) -> str:
    if not isinstance(reason_codes, (tuple, list)):
        return "No reason code supplied"
    return ", ".join(str(item) for item in reason_codes) or "No limitations recorded"


def _component_unavailable(ui: object, label: str, component: object) -> None:
    state = getattr(getattr(component, "state", None), "value", "UNAVAILABLE")
    reasons = getattr(component, "reason_codes", ())
    ui.label(f"{label}: {state.replace('_', ' ').lower()}. {_reason_text(reasons)}")  # type: ignore[attr-defined]


def _exposure_summary(profile: InvestigationProfile | None) -> str:
    if profile is None:
        return "Not represented in this profile; availability is unknown"
    unknown = next((
        item for item in profile.planner_facts.explicit_unknowns
        if "exposure" in item.fact_identity.lower() or "wip" in item.fact_identity.lower()
    ), None)
    return (
        f"Unavailable / not qualified: {unknown.reason}"
        if unknown is not None else "Not represented in this profile; availability is unknown"
    )


def _render_investigation_plan_card(
    ui: object,
    investigation: EpisodeInvestigation,
    profile: InvestigationProfile | None,
    principal: Principal,
    *,
    check_catalog: CheckTemplateCatalog | None,
    on_request_check: Callable[[object, object], object],
    action_buttons: list[ActionButton],
) -> None:
    with Card():
        _section_heading(ui, "Investigation plan")
        if investigation.planner.state is not ComponentState.READY:
            _component_unavailable(ui, "Planner", investigation.planner)
            return
        plan = investigation.planner.value
        if not hasattr(plan, "recommendations"):
            _component_unavailable(ui, "Planner", investigation.planner)
            return
        if not plan.recommendations:
            ui.label("No eligible check is available from the current curated catalog and qualified facts.")  # type: ignore[attr-defined]
        else:
            hypothesis_by_id = {item.hypothesis_identity: item for item in profile.hypotheses} if profile is not None else {}
            for recommendation in plan.recommendations:
                _section_heading(ui, f"{recommendation.rank}. {recommendation.title}", level=3)
                ui.label(f"Execution mode: {recommendation.execution_mode.replace('_', ' ').lower()}")  # type: ignore[attr-defined]
                for alternative in recommendation.alternatives_discriminated:
                    left = hypothesis_by_id.get(str(alternative["hypothesis_a_id"]))
                    right = hypothesis_by_id.get(str(alternative["hypothesis_b_id"]))
                    labels = f"{left.title if left else alternative['hypothesis_a_id']} versus {right.title if right else alternative['hypothesis_b_id']}"
                    ui.label(f"Distinguishes {labels}; curated ordinal discrimination {alternative['ordinal_discrimination']} (not probability).")  # type: ignore[attr-defined]
                if recommendation.independent_evidence_added:
                    ui.label(f"New independent evidence groups: {', '.join(recommendation.independent_evidence_added)}")  # type: ignore[attr-defined]
                catalog_templates = check_catalog.templates if check_catalog is not None else ()
                template = next((item for item in catalog_templates if item.template_id == recommendation.template_id and item.version == recommendation.template_version), None)
                mode = CheckExecutionMode(recommendation.execution_mode)
                if (
                    template is not None
                    and mode in {CheckExecutionMode.REQUEST_HUMAN_MEASUREMENT, CheckExecutionMode.REQUEST_APPROVED_EXTERNAL_WORK}
                    and principal.has_capability(CHECK_REQUEST_CAPABILITY)
                    and (template.approval_capability is None or principal.has_capability(template.approval_capability))
                ):
                    button = ActionButton("Request check", intent=ButtonIntent.SECONDARY, on_click=lambda rec=recommendation, item=template: on_request_check(rec, item))
                    if not action_buttons:
                        _mark_focus_target(button.element, "primary-action")
                    action_buttons.append(button)
                ui.label(f"Why eligible now: {'; '.join(recommendation.why_eligible_now)}")  # type: ignore[attr-defined]
                facts = recommendation.capability_and_prerequisite_facts
                if facts:
                    ui.label("Prerequisite / qualification facts: " + "; ".join(
                        ", ".join(f"{key}={value}" for key, value in sorted(item.items()) if key in {"capability_id", "prerequisite_id", "qualification_identity", "state"})
                        for item in facts
                    ))  # type: ignore[attr-defined]
                costs = recommendation.effort_turnaround_disruption
                turnaround = costs.get("turnaround", {})
                ui.label(f"Effort {costs.get('effort_band', 'unknown').lower()} · disruption {costs.get('disruption_class', 'unknown').lower()} · turnaround {turnaround.get('seconds') if turnaround.get('seconds') is not None else 'unknown'} seconds")  # type: ignore[attr-defined]
                if profile is not None:
                    deadline = profile.planner_facts.decision_deadline
                    deadline_text = deadline.deadline.isoformat() if deadline.deadline is not None else f"unknown — {deadline.unknown_reason}"
                    ui.label(f"Decision deadline {deadline_text} · source {deadline.source_identity}")  # type: ignore[attr-defined]
                not_resolved = ", ".join(recommendation.will_not_resolve) or "No additional planner-supplied limitation; this check alone does not establish either hypothesis."
                ui.label(f"Will not resolve: {not_resolved}")  # type: ignore[attr-defined]
        if plan.excluded_checks:
            ui.label(f"Excluded alternatives ({len(plan.excluded_checks)}):")  # type: ignore[attr-defined]
            for excluded in plan.excluded_checks[:20]:
                ui.label(f"{excluded.template_id} · {_reason_text(tuple(item.value for item in excluded.reasons))}")  # type: ignore[attr-defined]
            if len(plan.excluded_checks) > 20:
                ui.label(f"{len(plan.excluded_checks) - 20} additional exclusions are available in the typed planner result.")  # type: ignore[attr-defined]


def _render_evidence_card(ui: object, investigation: EpisodeInvestigation, profile: InvestigationProfile | None) -> None:
    with Card():
        _section_heading(ui, "Evidence and competing explanations")
        if profile is None:
            _component_unavailable(ui, "Evidence groups", investigation.profile)
            return
        hypothesis_by_id = {item.hypothesis_identity: item for item in profile.hypotheses}
        for pair in profile.planner_facts.unresolved_pairs:
            left = hypothesis_by_id[pair.hypothesis_a_id]
            right = hypothesis_by_id[pair.hypothesis_b_id]
            ui.label(f"Unresolved: {left.title} versus {right.title}").classes("text-subtitle1")  # type: ignore[attr-defined]
            ui.label(f"{left.summary} {right.summary}")  # type: ignore[attr-defined]
        by_dependence: dict[str, list[object]] = {}
        for group in profile.evidence_groups:
            by_dependence.setdefault(group.dependence_identity, []).append(group)
        for dependence_identity, groups in sorted(by_dependence.items()):
            titles = "; ".join(item.title for item in groups)
            ui.label(f"Evidence dependency group · {dependence_identity} · {len(groups)} related fact(s): {titles}").classes("text-subtitle2")  # type: ignore[attr-defined]
            for group in groups:
                ui.label(f"{group.polarity.replace('_', ' ').title()} — {group.summary}")  # type: ignore[attr-defined]
                ui.label(f"Source {group.source_identity} · event {group.event_at.isoformat()} · available {group.available_at.isoformat()} · qualification {group.qualification_state}")  # type: ignore[attr-defined]
                if group.limitation_codes:
                    ui.label(f"Evidence limitations: {_reason_text(group.limitation_codes)}")  # type: ignore[attr-defined]
        for unknown in profile.planner_facts.explicit_unknowns:
            ui.label(f"Unknown: {unknown.fact_identity} — {unknown.reason} (source {unknown.source_identity})")  # type: ignore[attr-defined]
        for limitation in profile.limitations:
            ui.label(f"Investigation limitation: {limitation.replace('_', ' ').lower()}")  # type: ignore[attr-defined]


def _render_comparable_cases_card(ui: object, investigation: EpisodeInvestigation) -> None:
    with Card():
        _section_heading(ui, "Comparable cases")
        if investigation.comparable_history.state is not ComponentState.READY:
            _component_unavailable(ui, "Comparable history", investigation.comparable_history)
            if investigation.comparable_history.state is ComponentState.MATERIALIZATION_REQUIRED:
                ui.label(f"Comparable history requires materialization ({_reason_text(investigation.comparable_history.reason_codes)}). No partial case ranking is shown.")  # type: ignore[attr-defined]
            return
        page = investigation.comparable_history.value
        ui.label(f"Exact-structure case comparison · {page.total_row_count} eligible case(s) · cutoff {investigation.known_at.isoformat()}")  # type: ignore[attr-defined]
        for case in page.cases:
            _section_heading(ui, f"Episode {case.episode_id} · {case.context_identity}", level=3)
            similarity = case.similarity_components
            ui.label(f"Shared exact features: {', '.join(similarity.shared_exact_feature_ids) or 'none'}")  # type: ignore[attr-defined]
            ui.label(f"Differences: {', '.join(similarity.differing_feature_ids) or 'none'} · current only: {', '.join(similarity.current_only_feature_ids) or 'none'} · case only: {', '.join(similarity.candidate_only_feature_ids) or 'none'}")  # type: ignore[attr-defined]
            ui.label(f"Family {case.family_identity} · revision {case.revision_id} · source {case.source_identity.snapshot_id} · eligibility {case.eligibility_state.value} · curation {case.curation_state.value}")  # type: ignore[attr-defined]
            if case.data_completeness_limitations:
                ui.label(f"Completeness limitations: {_reason_text(case.data_completeness_limitations)}")  # type: ignore[attr-defined]
            for claim in case.historical_claims:
                maturity = f" · outcome {claim.outcome_maturity} through {claim.outcome_cutoff.isoformat()}" if claim.outcome_maturity is not None and claim.outcome_cutoff is not None else ""
                ui.label(f"Cutoff-eligible historical {claim.claim_type.value.replace('_', ' ').lower()} claim {claim.claim_identity} · evidence {claim.evidence_identity} · curation {claim.curation_state.value} · known {claim.known_at.isoformat()} · available {claim.available_at.isoformat()}{maturity}")  # type: ignore[attr-defined]
            ui.label("Structural similarity is descriptive and does not establish the same cause.")  # type: ignore[attr-defined]
        if not any(case.historical_claims for case in page.cases):
            ui.label("No curated, cutoff-eligible historical root-cause, action or outcome claims are available in these cases.")  # type: ignore[attr-defined]
        for excluded in page.excluded_candidates:
            ui.label(f"Case excluded: {excluded.episode_id} · {_reason_text(excluded.reason_codes)}")  # type: ignore[attr-defined]


def _render_rca_card(ui: object, investigation: EpisodeInvestigation) -> None:
    with Card():
        _section_heading(ui, "Bounded RCA · observational only")
        if investigation.rca.state is ComponentState.PENDING:
            materialization = investigation.rca.value
            job_state = getattr(getattr(materialization, "state", None), "value", "PENDING")
            ui.label(f"RCA materialization {str(job_state).lower()}. No partial summary is shown while the bounded analysis runs.")  # type: ignore[attr-defined]
            return
        if investigation.rca.state not in {ComponentState.READY, ComponentState.MATERIALIZATION_REQUIRED}:
            _component_unavailable(ui, "RCA", investigation.rca)
            return
        result = investigation.rca.value
        if result.state.value == "MATERIALIZATION_REQUIRED":
            ui.label("RCA materialization is required. No partial commonality summary is shown.")  # type: ignore[attr-defined]
            return
        ui.label(f"State {result.state.value.replace('_', ' ').lower()} · control quality {result.control_quality.value.lower()}")  # type: ignore[attr-defined]
        ui.label(f"Affected cohort {result.affected_cohort_identity} · independent groups {result.affected_independent_group_count if result.affected_independent_group_count is not None else 'not computed'}")  # type: ignore[attr-defined]
        ui.label(f"Controls {', '.join(result.control_cohort_identities) or 'none'} · independent groups {result.control_independent_group_count if result.control_independent_group_count is not None else 'not computed'}")  # type: ignore[attr-defined]
        ui.label(f"Included evidence {result.included_evidence_count if result.included_evidence_count is not None else 'not computed'} · excluded {len(result.excluded_evidence)} · coverage {result.coverage.lower()}")  # type: ignore[attr-defined]
        for cohort in result.cohorts:
            ui.label(f"{cohort.role.value.title()} {cohort.cohort_identity}: {cohort.eligibility.value.lower()} · {cohort.context_identity} / {cohort.characteristic_identity} ({cohort.unit_identity}) · {cohort.interval_start.isoformat()} to {cohort.interval_end.isoformat()} · matched {', '.join(cohort.matched_dimensions) or 'none'} · source {cohort.source_identity} · qualification {cohort.qualification_identity}")  # type: ignore[attr-defined]
            if cohort.mismatches or cohort.reason_codes:
                ui.label(f"Cohort qualifications / mismatches: {_reason_text((*cohort.reason_codes, *cohort.mismatches))}")  # type: ignore[attr-defined]
        if result.associations:
            ui.label("Descriptive commonality counts (numerator / independent-group denominator):")  # type: ignore[attr-defined]
            for fact in result.associations:
                ui.label(f"{fact.factor_identity}: affected {fact.affected.numerator}/{fact.affected.denominator}; controls {fact.controls.numerator}/{fact.controls.denominator}; rate difference {fact.rate_difference_numerator}/{fact.rate_difference_denominator}.")  # type: ignore[attr-defined]
        else:
            ui.label("No commonality summary is available because controls are invalid, insufficient, mismatched or unqualified. Add qualified control evidence or run the discriminating check.")  # type: ignore[attr-defined]
        for contradiction in result.temporal_contradictions:
            ui.label(f"Temporal contradiction: {contradiction.fact_identity} · {contradiction.reason_code}")  # type: ignore[attr-defined]
        for exclusion in result.excluded_evidence:
            ui.label(f"Evidence excluded: {exclusion.identity} · {exclusion.reason_code}")  # type: ignore[attr-defined]
        ui.label(f"Interpretation: observational only. {', '.join(result.limitation_codes) or 'No additional limitations recorded'}.")  # type: ignore[attr-defined]


def _render_current_work_card(ui: object, investigation: EpisodeInvestigation) -> None:
    with Card():
        _section_heading(ui, "Current work and recovery")
        if investigation.workflow.state is not ComponentState.READY:
            _component_unavailable(ui, "O5 workflow", investigation.workflow)
            return
        snapshot = investigation.workflow.value
        checks = sorted(snapshot.check_state.values(), key=lambda item: str(item.get("check_id", "")))
        ui.label(f"Work state {snapshot.workflow_state['work_state']} · owner {snapshot.state.get('owner') or 'unassigned'} · cycle {snapshot.active_cycle_id} · workflow version {snapshot.aggregate_version}.")  # type: ignore[attr-defined]
        if checks:
            for check in checks:
                outcome = (check.get("result") or {}).get("outcome") if isinstance(check.get("result"), dict) else "not recorded"
                ui.label(f"Check {check.get('check_id')} · {check.get('template_id')} · {check.get('status')} · outcome {outcome}")  # type: ignore[attr-defined]
        else:
            ui.label("No check has been requested in the current O5 cycle.")  # type: ignore[attr-defined]
        actions = sorted(snapshot.action_state.values(), key=lambda item: str(item.get("action_id", "")))
        for action in actions:
            ui.label(f"Recorded human action {action.get('action_id')} · {action.get('action_type')} · reconciliation {action.get('reconciliation_state', 'unknown')}")  # type: ignore[attr-defined]
        if not actions:
            ui.label("No external action is recorded in this cycle.")  # type: ignore[attr-defined]
        plans = snapshot.recovery_state.get("plans", {})
        for plan_id, plan in sorted(plans.items()):
            ui.label(f"Recovery plan {plan_id} · {plan.get('state', 'unknown')} · eligible evidence count {plan.get('eligible_evidence_count', 'unavailable')}")  # type: ignore[attr-defined]
        if not plans:
            ui.label("Recovery: not started; no affirmative recovery evidence is asserted.")  # type: ignore[attr-defined]
        closure = snapshot.closure_state
        ui.label(f"Closure state {closure['cycle_status']} · closure records {len(closure['closures'])}.")  # type: ignore[attr-defined]


def _render_investigation_workspace(
    ui: object,
    investigation: EpisodeInvestigation,
    principal: Principal,
    *,
    check_catalog: CheckTemplateCatalog | None,
    on_request_check: Callable[[object, object], object],
    action_buttons: list[ActionButton],
) -> None:
    profile = investigation.profile.value if investigation.profile.state is ComponentState.READY else None
    brief = investigation.brief
    typed_profile = profile if isinstance(profile, InvestigationProfile) else None
    exposure_summary = _exposure_summary(typed_profile)
    _render_investigation_plan_card(
        ui, investigation, typed_profile, principal, check_catalog=check_catalog,
        on_request_check=on_request_check, action_buttons=action_buttons,
    )
    with Card():
        _section_heading(ui, "Current decision")
        if isinstance(profile, InvestigationProfile):
            ui.label(f"Episode identity {brief.episode_id}")  # type: ignore[attr-defined]
            ui.label(profile.change.headline).classes("text-h5")  # type: ignore[attr-defined]
            ui.label(profile.change.description)  # type: ignore[attr-defined]
            ui.label(f"Asset {profile.target.asset_identity} · family {profile.target.family_identity} · context {profile.target.context_identity} · {profile.target.characteristic_identity} ({profile.target.unit_identity})")  # type: ignore[attr-defined]
            ui.label(f"Observed onset {_display_value(profile.change.onset_at, unavailable='Unavailable / not qualified')} · magnitude {_display_value(profile.change.magnitude, unavailable='Unavailable / not qualified')}")  # type: ignore[attr-defined]
            ui.label(f"Source identity {profile.source_identity.snapshot_id} · revision {profile.source_identity.source_revision} · known at {_display_value(investigation.known_at)} · source qualification state not represented in this profile")  # type: ignore[attr-defined]
            ui.label(f"Owner {_display_value(brief.workflow.get('owner'), unavailable='Unassigned / unavailable')} · workflow state {brief.workflow.get('work_state', 'UNKNOWN')} · cycle {investigation.active_cycle_id or 'unavailable'} · version {brief.revision_vector.workflow_version}")  # type: ignore[attr-defined]
            ui.label(f"WIP / exposure: {exposure_summary}")  # type: ignore[attr-defined]
            if "SYNTHETIC_DEMONSTRATION" in profile.limitations:
                ui.label("Synthetic demonstration facts only. These IDs, values, limits and policy are not company facts.").classes("ephi-o10-truth-note")  # type: ignore[attr-defined]
        else:
            ui.label(str(brief.analytical.get("title", "Episode issue/change description unavailable")))  # type: ignore[attr-defined]
            DescriptionList((
                KeyValueItem("known_at", "Known at", _display_value(brief.known_at)),
                KeyValueItem("owner", "Owner", _display_value(brief.workflow.get("owner"), unavailable="Unassigned / unavailable")),
                KeyValueItem("work_state", "Workflow", _display_value(brief.workflow.get("work_state"))),
                KeyValueItem("capability", "Source / capability", _display_value(brief.capability_state)),
                KeyValueItem("exposure", "WIP / exposure", "Unavailable / not qualified"),
            ))
            _component_unavailable(ui, "Investigation profile", investigation.profile)
    _render_evidence_card(ui, investigation, typed_profile)
    _render_comparable_cases_card(ui, investigation)
    _render_rca_card(ui, investigation)
    _render_current_work_card(ui, investigation)


class _EpisodeView:
    def __init__(self, composition: EphiUiComposition, host: object, page_heading: object, episode_id: str, focus_request: object) -> None:
        self.composition = composition
        self.host = host
        self.page_heading = page_heading
        self.episode_id = episode_id
        self.focus_request = focus_request
        self.guard = StaleResponseGuard()
        self.primary_action: ActionButton | None = None
        self.check_actions: list[ActionButton] = []
        self.live_status: object | None = None
        self.investigation: EpisodeInvestigation | None = None

    def _clear(self) -> None:
        self.host.clear()  # type: ignore[attr-defined]

    def _mount(self, render: Callable[[], None]) -> None:
        self._clear()
        with self.host:  # type: ignore[attr-defined]
            render()

    def render_loading(self) -> None:
        def render() -> None:
            with FullScreenWorkspace():
                with Card():
                    StateView(
                        StateViewSpec(
                            StateKind.EMPTY,
                            "Loading permitted Episode brief",
                            "Episode identity and current source/workflow facts are not yet available.",
                        )
                    )

        self._mount(render)

    def render_no_selection(self) -> None:
        from nicegui import ui

        def return_to_attention() -> None:
            self.composition.workspace.state.set(FOCUS_KEY, "attention_heading", source="ephi.episode.focus")
            ui.navigate.to("/")

        def render() -> None:
            with FullScreenWorkspace():
                with Card():
                    status_target = ui.element("div").props('tabindex="-1" role="region" aria-label="Episode selection status"')
                    with status_target:
                        StateView(
                            StateViewSpec(
                                StateKind.EMPTY,
                                "No episode selected",
                                "Return to Attention and select one permitted episode. No Episode identity is inferred.",
                                secondary_action_label="Return to Attention",
                            ),
                            on_secondary_action=return_to_attention,
                        )

        self._mount(render)
        _schedule_focus(self.page_heading)

    async def load(self, *, focus_target: str | None = None) -> None:
        self.render_loading()
        try:
            brief, investigation = await _load_investigation(self.composition, self.episode_id, self.guard)
        except Exception as error:
            self.render_error(error)
            return
        self.render_brief(brief, investigation=investigation, focus_target=focus_target)

    def render_error(self, error: BaseException) -> None:
        from nicegui import ui

        spec = _state_for_error(error)

        async def retry() -> None:
            await self.load(focus_target="status")

        def return_to_attention() -> None:
            self.composition.workspace.state.set(FOCUS_KEY, "attention_heading", source="ephi.episode.focus")
            ui.navigate.to("/")

        def render() -> None:
            with FullScreenWorkspace():
                with Card():
                    status_target = ui.element("div").classes("ephi-o10-focus-target").props('tabindex="-1" role="region" aria-label="Episode status"')
                    with status_target:
                        StateView(
                            spec,
                            on_action=retry if spec.action_label else None,
                            on_secondary_action=return_to_attention,
                        )
                    if spec.action_label is None:
                        ActionButton("Return to Attention", intent=ButtonIntent.SECONDARY, on_click=return_to_attention)

        self._mount(render)
        _schedule_focus(self.page_heading if spec.kind is StateKind.PERMISSION else self.host)

    def render_brief(
        self,
        brief: EpisodeBrief,
        *,
        investigation: EpisodeInvestigation | None = None,
        focus_target: str | None = None,
        status_message: str | None = None,
    ) -> None:
        from nicegui import ui

        self.primary_action = None
        self.check_actions = []
        self.live_status = None
        self.investigation = investigation
        principal = self.composition.principal_provider()
        action = _episode_action(brief, principal)

        async def commit(action_name: str) -> None:
            if self.primary_action is not None:
                self.primary_action.element.disable()
            if self.live_status is not None:
                self.live_status.set_text(f"Submitting {action_name} through the current authorized workflow.")  # type: ignore[attr-defined]
            scope = self.composition.scope_provider()
            current_principal = self.composition.principal_provider()
            command = CommandContext(
                _stable_command_id(action_name, brief, scope, current_principal.subject),
                current_principal,
                scope,
                brief.revision_vector.workflow_version,
                brief.revision_vector,
                f"{action_name} the W1 engineer case",
            )
            try:
                if action_name == "ClaimEpisode":
                    await asyncio.to_thread(self.composition.workflow.claim_episode, command, brief.episode_id)
                else:
                    await asyncio.to_thread(self.composition.workflow.acknowledge_episode, command, brief.episode_id)
                refreshed, refreshed_investigation = await _load_investigation(self.composition, brief.episode_id, self.guard)
            except Exception as error:
                self.render_error(error)
                return
            self.render_brief(refreshed, investigation=refreshed_investigation, focus_target="primary")
            # The action render replaces the old button in-place. Complete the
            # focus hand-off in this same governed command callback so focus
            # cannot remain on the unmounted, disabled control.
            from nicegui import ui

            self.focus_request.props('data-ephi-focus-request="primary-action"')  # type: ignore[attr-defined]

        async def request_recommended_check(recommendation: object, template: object) -> None:
            if self.primary_action is not None:
                self.primary_action.element.disable()
            for button in self.check_actions:
                button.element.disable()
            if self.live_status is not None:
                self.live_status.set_text("Requesting the selected planner check through the current authorized O5 workflow.")  # type: ignore[attr-defined]
            investigation_view = self.investigation
            downstream = self.composition.downstream
            if investigation_view is None or downstream is None or investigation_view.active_cycle_id is None:
                self.render_error(RuntimeError("current downstream investigation action is unavailable"))
                return
            plan_component = investigation_view.planner
            plan = plan_component.value
            if plan_component.state is not ComponentState.READY or not hasattr(plan, "plan_identity") or recommendation not in plan.recommendations:
                await self.load(focus_target="status")
                return
            current_principal = self.composition.principal_provider()
            scope = self.composition.scope_provider()
            if (
                plan.workflow_version != brief.revision_vector.workflow_version
                or plan.viewed_revisions != brief.revision_vector
                or investigation_view.revision_id != brief.revision_id
            ):
                await self.load(focus_target="status")
                return
            check_identity = hashlib.sha256(
                f"ephi-check:{plan.plan_identity}:{investigation_view.active_cycle_id}:{recommendation.template_id}".encode("utf-8")
            ).hexdigest()
            command = CommandContext(
                _stable_command_id(f"RequestCheck:{recommendation.template_id}", brief, scope, current_principal.subject),
                current_principal,
                scope,
                brief.revision_vector.workflow_version,
                brief.revision_vector,
                "Request the current deterministic investigation-plan check",
            )
            profile_component = investigation_view.profile
            profile = profile_component.value
            if profile_component.state is not ComponentState.READY or not isinstance(profile, InvestigationProfile):
                await self.load(focus_target="status")
                return
            prerequisite_states = {
                item.prerequisite_id: item.state.value
                for item in profile.planner_facts.prerequisite_facts
            }
            try:
                downstream.decision_loop.request_check(
                    command,
                    brief.episode_id,
                    check_identity,
                    template_id=template.template_id,
                    template_version=template.version,
                    execution_mode=CheckExecutionMode(recommendation.execution_mode),
                    required_capabilities=tuple(sorted({
                        *(item.capability_id for item in template.required_capabilities),
                        *((template.approval_capability,) if template.approval_capability else ()),
                    })),
                    prerequisite_state=prerequisite_states,
                    target_context=profile.planner_facts.target_context.as_dict(),
                    cycle_id=investigation_view.active_cycle_id,
                )
                refreshed, refreshed_investigation = await _load_investigation(self.composition, brief.episode_id, self.guard)
            except Exception as error:
                # The O5 command remains the compare-and-set authority. A
                # stale plan refreshes from current server state before a new
                # recommendation action can be offered.
                if getattr(error, "code", None) in {"VERSION_CONFLICT", "COHERENT_READ_CONFLICT", "QUERY_IDENTITY_MISMATCH"}:
                    await self.load(focus_target="status")
                    return
                self.render_error(error)
                return
            message = f"Check request committed at workflow version {refreshed.revision_vector.workflow_version}."
            self.render_brief(refreshed, investigation=refreshed_investigation, focus_target="status", status_message=message)
            self.focus_request.props('data-ephi-focus-request="status"')

        def return_to_attention() -> None:
            self.composition.workspace.state.set(FOCUS_KEY, "attention_heading", source="ephi.episode.focus")
            ui.navigate.to("/")

        def render() -> None:
            with FullScreenWorkspace():
                with Card():
                    profile_value = investigation.profile.value if investigation is not None and investigation.profile.state is ComponentState.READY else None
                    headline = profile_value.change.headline if isinstance(profile_value, InvestigationProfile) else str(brief.analytical.get("title", "Decision brief"))
                    metadata = [
                        KeyValueItem("revision", "Analytical revision", brief.revision_vector.analysis_revision),
                        KeyValueItem("workflow", "Workflow version", brief.revision_vector.workflow_version),
                    ]
                    if isinstance(profile_value, InvestigationProfile):
                        metadata.append(KeyValueItem("source_identity", "Source identity", profile_value.source_identity.snapshot_id))
                        metadata.append(KeyValueItem("exposure", "WIP / exposure", _exposure_summary(profile_value)))
                        metadata.append(KeyValueItem("known_at", "Known at cutoff", _display_value(investigation.known_at)))
                    EntityHeader(
                        profile_value.target.asset_identity if isinstance(profile_value, InvestigationProfile) else brief.episode_id,
                        subtitle=headline,
                        entity_type="Episode",
                        status=str(brief.workflow.get("work_state", "UNKNOWN")),
                        metadata=tuple(metadata),
                    )
                    if not isinstance(profile_value, InvestigationProfile):
                        for key, value in sorted(brief.capability_state.items()):
                            StatusBadge(f"{key}: {_display_value(value)}", intent=_intent_for_capability(value))
                    if investigation is None:
                        DescriptionList((
                            KeyValueItem("episode_id", "Episode ID", brief.episode_id),
                            KeyValueItem("known_at", "Known at", _display_value(brief.known_at)),
                            KeyValueItem("analysis_revision", "Analytical revision", _display_value(brief.revision_vector.analysis_revision)),
                            KeyValueItem("workflow_version", "Workflow version", _display_value(brief.revision_vector.workflow_version)),
                            KeyValueItem("work_state", "Workflow state", _display_value(brief.workflow.get("work_state"))),
                            KeyValueItem("owner", "Owner", _display_value(brief.workflow.get("owner"), unavailable="Unassigned / unavailable")),
                            KeyValueItem("source_state", "Source / capability state", _display_value(brief.capability_state)),
                            KeyValueItem("eligible_action", "Current eligible action", action[0] if action else "Unavailable: current authorization or owner does not permit an action"),
                        ))
                        ui.label("Investigation profile, onset, qualified exposure, planner, comparable history and RCA are unavailable for this legacy Episode.").classes("ephi-o10-truth-note")
                    if investigation is not None:
                        catalog = self.composition.downstream.policy_configuration.check_catalog if self.composition.downstream is not None else None
                        _render_investigation_workspace(
                            ui,
                            investigation,
                            principal,
                            check_catalog=catalog,
                            on_request_check=request_recommended_check,
                            action_buttons=self.check_actions,
                        )
                    self.live_status = ui.label(status_message or f"Episode {brief.episode_id} is rendered from the current authorized coherent read.").classes("ephi-o10-live-status ephi-o10-focus-target")
                    self.live_status.props('role="status" aria-live="polite" aria-atomic="true" tabindex="-1"')
                    self.live_status.props('data-ephi-focus-target="status"')
                    with ui.element("div").classes("ephi-o10-action-row"):
                        if action is not None:
                            self.primary_action = ActionButton(action[0], intent=ButtonIntent.PRIMARY, on_click=lambda: commit(action[1]))
                            _mark_focus_target(self.primary_action.element, "primary-action")
                            if focus_target == "primary":
                                self.primary_action.element.props("autofocus")
                        else:
                            StateView(
                                StateViewSpec(
                                    StateKind.EMPTY,
                                    "No action available",
                                    "Workflow truth and current authorization do not permit Claim or Acknowledge in this session.",
                                )
                            )
                        back = ActionButton("Return to Attention", intent=ButtonIntent.SECONDARY, on_click=return_to_attention)
                        _mark_focus_target(back.element, "return-attention")

        self._mount(render)
        if focus_target == "primary" and self.primary_action is not None:
            _schedule_focus(self.primary_action.element, marker="primary-action")
        elif focus_target == "status" and self.live_status is not None:
            _schedule_focus(self.live_status, marker="status")
        else:
            _schedule_focus(self.page_heading)


def _render_brief_error(error: BaseException) -> None:
    with Card():
        StateView(_state_for_error(error))


async def build_episode_page(composition: EphiUiComposition) -> None:
    _install_o10_ui_css()
    workspace = composition.workspace
    episode_id = workspace.state.get(EPISODE_KEY)
    if not episode_id and os.environ.get("EPHI_ENV", "development").lower() == "test" and composition.downstream is not None:
        episode_id = os.environ.get("EPHI_TEST_SELECTED_EPISODE_ID")
    if not episode_id:
        # This explicit fixture binding is retained only for the existing O8
        # direct-route qualification. A selected workspace identity always wins.
        episode_id = os.environ.get("EPHI_W1_EPISODE_ID")
    with AppShell(
        "EPHI",
        _navigation(),
        active_route="/episode",
        environment=os.environ.get("EPHI_ENV", "development"),
        subtitle="Attention → Episode",
        user_name=composition.principal_provider().subject,
        user_role="Engineer",
        debugger=False,
    ):
        from nicegui import ui

        ui.query("main").props('role="region" aria-label="EPHI application content"')
        with AnalysisWorkspacePage("", None) as page:
            with page.slot(LayoutSlot.HEADER):
                heading = _semantic_heading(
                    "Episode investigation workspace",
                    "One coherent analytical revision with current evidence, plan, comparable cases and bounded RCA",
                    autofocus=not episode_id or workspace.state.get(FOCUS_KEY) == "episode_heading",
                )
                origin = workspace.state.get(ORIGIN_KEY)
                if isinstance(origin, str) and origin.startswith("/ephi/assets/") and ".." not in origin:
                    ui.link("Back to Asset 360", origin).props('aria-label="Return to originating Asset 360"')
            with page.slot(LayoutSlot.PRIMARY):
                episode_host = ui.element("div").classes("ephi-o10-episode-surface").props('tabindex="-1" role="region" aria-label="Episode rendered state"')
                if not episode_id:
                    focus_request = ui.element("div").props('data-ephi-focus-request="" aria-hidden="true"')
                    view = _EpisodeView(composition, episode_host, heading, "", focus_request)
                    view.render_no_selection()
                    return
                focus_request = ui.element("div").props('data-ephi-focus-request="" aria-hidden="true"')
                view = _EpisodeView(composition, episode_host, heading, episode_id, focus_request)
                focus_target = workspace.state.get(FOCUS_KEY)
                await view.load(focus_target=focus_target if isinstance(focus_target, str) else None)
                if focus_target == "episode_heading":
                    workspace.state.set(FOCUS_KEY, None, source="ephi.episode.focus")


def _outcomes_datetime_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def _outcomes_parse_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _outcomes_amount(value: object, currency: str) -> str:
    if value is None:
        return f"VOID · no amount ({currency})"
    return f"{value} {currency}"


async def _render_outcomes_content(composition: EphiUiComposition) -> None:
    """Render the bounded Outcomes controls, summaries and claim review panel."""

    if composition.outcomes is None:
        raise RuntimeError("Outcomes service is unavailable")
    from nicegui import ui

    ui.add_css(_OUTCOMES_CSS, shared=True)
    initial_now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    with Card():
        with ui.element("div").classes("ephi-outcomes-controls"):
            start_input = ui.input("Period start (UTC)", value=_outcomes_datetime_text(initial_now - timedelta(days=30))).props('type="datetime-local"')
            end_input = ui.input("Period end (UTC)", value=_outcomes_datetime_text(initial_now + timedelta(minutes=1))).props('type="datetime-local"')
            cutoff_input = ui.input("Knowledge cutoff (UTC)", value=_outcomes_datetime_text(initial_now)).props('type="datetime-local"')
            currency_select = ui.select({"ALL": "Separate by currency"}, value="ALL", label="Currency")
            maturity_select = ui.select(
                {
                    "ALL": "All evidence states",
                    "PENDING": "Pending",
                    "OBSERVED_NOT_VALIDATED": "Observed, not validated",
                    "VALIDATED": "Independently validated",
                    "REJECTED": "Rejected",
                    "CENSORED": "Censored",
                    "INSUFFICIENT_EVIDENCE": "Insufficient evidence",
                    "VOID": "Voided value revisions",
                    "ZERO": "Zero outcomes",
                    "NEGATIVE": "Negative outcomes",
                },
                value="ALL",
                label="Evidence maturity",
            )
            ActionButton("Refresh Outcomes", intent=ButtonIntent.PRIMARY, on_click=lambda: asyncio.create_task(load()))
        ui.label("Period and knowledge cutoff are always applied. Amounts stay in their recorded currency; no conversion policy is configured.").classes("ephi-o10-truth-note")
    content = ui.element("div").classes("ephi-outcomes")
    load_status = ui.label("Loading Outcomes…").props('role="status" aria-live="polite"')
    last_good_result: OutcomesQueryResult | None = None
    stale = False
    review_action_buttons: list[ActionButton] = []

    def show_error(error: BaseException) -> None:
        content.clear()
        with content:
            if isinstance(error, AuthorizationDeniedError):
                StateView(StateViewSpec(StateKind.PERMISSION, "Outcomes unavailable", "Current authorization does not permit this Outcomes query."))
            elif isinstance(error, VersionConflictError):
                StatusBadge("CONFLICT", intent=StatusIntent.WARNING)
                StateView(StateViewSpec(StateKind.ERROR, "Outcome changed", "A newer value revision committed. Refresh and review the current revision."))
            elif isinstance(error, ValueError):
                StateView(StateViewSpec(StateKind.ERROR, "Check the Outcomes filters", str(error)))
            else:
                StateView(StateViewSpec(StateKind.ERROR, "Outcomes could not be loaded", "The bounded request failed. Retry the query; no partial amount is shown."))

    async def load() -> None:
        nonlocal last_good_result, stale
        if last_good_result is None:
            content.clear()
            with content:
                with Card():
                    ui.label("Loading Outcomes…").props('role="status" aria-live="polite"')
                    ui.element("div").classes("ephi-o10-live-status")
        else:
            load_status.set_text("Refreshing Outcomes… the last coherent AS_KNOWN result remains visible until this query completes.")
            for button in review_action_buttons:
                button.element.disable()
        await asyncio.sleep(0)
        try:
            period = EventPeriod(
                _outcomes_parse_datetime(start_input.value, "Period start"),
                _outcomes_parse_datetime(end_input.value, "Period end"),
            )
            cutoff = _outcomes_parse_datetime(cutoff_input.value, "Knowledge cutoff")
            principal = composition.principal_provider()
            scope = composition.scope_provider()
            result = composition.outcomes.query(
                principal,
                scope,
                event_period=period,
                knowledge_cutoff=cutoff,
                currency=None if currency_select.value in (None, "ALL") else str(currency_select.value),
                maturity=None if maturity_select.value in (None, "ALL") else str(maturity_select.value),
            )
            available = {"ALL": "Separate by currency", **{item: item for item in result.currencies}}
            currency_select.options = available
            if currency_select.value not in available:
                currency_select.value = "ALL"
            render_result(result, principal)
            last_good_result = result
            stale = False
            load_status.set_text("")
        except Exception as error:
            if isinstance(error, AuthorizationDeniedError):
                last_good_result = None
                stale = False
                load_status.set_text("")
                show_error(error)
            elif last_good_result is not None:
                stale = True
                load_status.set_text("STALE: Refresh failed. The previously authorized result remains labeled with its own period and knowledge cutoff; reviewer actions are disabled until refresh succeeds.")
                for button in review_action_buttons:
                    button.element.disable()
            else:
                load_status.set_text("")
                show_error(error)

    def render_result(result: OutcomesQueryResult, principal: Principal) -> None:
        nonlocal stale
        stale = False
        review_action_buttons.clear()
        content.clear()
        with content:
            if result.state == "PARTIAL":
                StatusBadge("PARTIAL", intent=StatusIntent.WARNING)
                StateView(StateViewSpec(StateKind.ERROR, "Outcomes are partial", "The bounded query limit was reached. No partial amounts are presented; narrow the period or choose a currency."))
                return
            with ui.element("div").classes("ephi-outcomes-banner").props('role="status" aria-live="polite"'):
                ui.label(f"AS_KNOWN through {result.knowledge_cutoff.isoformat()} · event period {result.event_period.start.isoformat()} to {result.event_period.end.isoformat()}.")
            if result.restated:
                with ui.element("div").classes("ephi-outcomes-banner").props('role="status" aria-live="polite"'):
                    ui.label("RESTATED: A later-known correction changed the active value or its event period at this cutoff.")

            summaries = result.summaries
            if summaries:
                for summary in summaries:
                    with ui.element("section").classes("ephi-outcomes-summaries").props(f'aria-label="{summary.currency} outcome summaries"'):
                        with MetricStrip():
                            MetricCard("Estimated opportunity", _outcomes_amount(summary.estimated_opportunity, summary.currency), description="Estimate only; not realized savings.")
                            MetricCard("Observed operational outcome", _outcomes_amount(summary.observed_operational_outcome, summary.currency), description="Observed outcomes remain separate from validated benefit.")
                            validated_text = (
                                "No validated claims in this covered period"
                                if summary.validated_record_count == 0
                                else _outcomes_amount(summary.validated_net, summary.currency)
                            )
                            MetricCard("Validated benefit / net cost", validated_text, description=f"Benefit {summary.validated_benefit} − eligible operating cost {summary.validated_operating_cost} {summary.currency}.")
                    ui.label(
                        f"{summary.currency} coverage: {summary.coverage_group_count} of {summary.claim_group_count} claim groups include a denominator · "
                        f"pending records {summary.pending_record_count} · independently validated records {summary.validated_record_count}"
                    ).classes("ephi-o10-truth-note")
            elif result.state == "EMPTY":
                with MetricStrip():
                    MetricCard("Estimated opportunity", "—", description="No matching claims in this covered period.")
                    MetricCard("Observed operational outcome", "—", description="No matching observed outcomes.")
                    MetricCard("Validated benefit / net cost", "No validated claims in this covered period")

            if result.state == "EMPTY":
                StateView(StateViewSpec(StateKind.EMPTY, "No matching Outcomes", "No claims match the selected event period, knowledge cutoff, currency and evidence maturity. This does not mean EPHI created no value."))
            if result.excluded_count:
                ui.label(f"Excluded by the selected period, currency or evidence filter: {result.excluded_count} value revision(s). Open a claim for its correction and attribution history.").classes("ephi-o10-truth-note")

            rows = []
            by_entry: dict[str, OutcomeRecord] = {}
            for record in result.rows:
                entry_id = record.value.entry_id
                by_entry[entry_id] = record
                rows.append({
                    "entry_id": entry_id,
                    "event": f"{record.economic_event_key} · {record.value.category.replace('_', ' ').title()}",
                    "event_at": record.value.event_at.isoformat(),
                    "evidence_state": record.state,
                    "amount": f"{_outcomes_amount(record.value.amount, record.value.currency)} · {record.amount_state}",
                    "owner": record.claim.owner,
                    "validator": record.review.reviewer if record.review and record.state in {"VALIDATED", "REJECTED"} else "—",
                    "validation_time": record.review.known_at.isoformat() if record.review and record.state in {"VALIDATED", "REJECTED"} else "—",
                    "correction": "Corrected" if record.corrected else "Original",
                })
            with Card():
                ui.label("Outcome claims").classes("text-h6")
                ui.label("One economic event group contributes each value revision once. Linked Episodes, decisions and actions provide attribution only.").classes("ephi-o10-truth-note")
                with ui.element("div").classes("ephi-outcomes-table"):
                    DataTable(
                        rows=rows,
                        columns=(
                            TableColumn("event", "Economic event · value type", min_width=210, priority="high"),
                            TableColumn("evidence_state", "Evidence / review", ColumnKind.STATUS, min_width=150),
                            TableColumn("amount", "Value · amount state", min_width=150, align="right"),
                            TableColumn("correction", "Revision", ColumnKind.STATUS, min_width=115),
                        ),
                        row_key="entry_id",
                        density=TableDensity.COMPACT,
                        show_toolbar=False,
                    )
                if rows:
                    select_options = {
                        record.value.entry_id: f"{record.economic_event_key} · {record.value.category.replace('_', ' ').title()} · {_outcomes_amount(record.value.amount, record.value.currency)}"
                        for record in result.rows
                    }
                    selected_entry = ui.select(select_options, value=next(iter(select_options)), label="Claim drilldown")
                    detail = ui.element("div").classes("ephi-outcomes-detail")

                    def render_detail(_event: object = None) -> None:
                        record = by_entry.get(str(selected_entry.value))
                        detail.clear()
                        if record is None:
                            return
                        with detail:
                            with Card():
                                ui.label("Claim and correction history").classes("text-subtitle1")
                                ui.label(f"Economic event {record.economic_event_key} · group {record.group_id}")
                                ui.label(f"Value revision {record.value.entry_id} · claim revision {record.claim.claim_revision_id} · aggregate version {record.aggregate_version}")
                                ui.label(f"Event {record.value.event_at.isoformat()} · known {record.value.known_at.isoformat()} · category {record.value.category} · amount {_outcomes_amount(record.value.amount, record.value.currency)} ({record.amount_state.lower()})")
                                ui.label(f"Owner {record.claim.owner} · claimant {record.claim.claimant} · pending age {record.pending_age_seconds if record.pending_age_seconds is not None else 'not pending'} seconds")
                                ui.label(f"Evidence IDs: {', '.join(record.claim.evidence_ids) or 'none'} · evidence identity {record.value.evidence_identity or 'unavailable'}")
                                ui.label(f"Cost model {record.value.cost_model_identity or 'unavailable'} · rate policy {record.value.rate_policy_identity or 'none; no conversion applied'}")
                                ui.label(f"Linked Episodes {', '.join(record.claim.attribution.episode_ids) or 'none'} · decisions {', '.join(record.claim.attribution.decision_ids) or 'none'} · actions {', '.join(record.claim.attribution.action_ids) or 'none'}")
                                ui.label(f"Contributors {', '.join(record.claim.attribution.contributor_ids) or 'none'} · affected material scope {', '.join(record.claim.attribution.material_scope) or 'not supplied'}")
                                ui.label(f"Coverage numerator / denominator {record.claim.coverage_numerator if record.claim.coverage_numerator is not None else 'not supplied'} / {record.claim.coverage_denominator if record.claim.coverage_denominator is not None else 'not supplied'}")
                                revision_state = "active void leaf; predecessor is not restored" if record.value.revision_kind.value == "VOID" else "corrected active leaf" if record.corrected else "original active leaf"
                                ui.label(f"Correction identity: supersedes {record.value.supersedes or 'none'} · state {revision_state}")
                                ui.label("Deduplication: the economic event key identifies one group. Episode, decision, action and contributor links are deduplicated references and never multiply the group amount.")
                                ui.label("Cutoff rule: select the active value and review leaves known by the displayed cutoff, then filter by event period. Later-known corrections and approvals remain excluded from earlier AS_KNOWN results.")
                                if record.review is not None:
                                    ui.label(f"Independent review {record.review.decision.value} · validator {record.review.reviewer} · known {record.review.known_at.isoformat()} · reviewed cutoff {record.review.knowledge_cutoff.isoformat()} · rationale {record.review.rationale}")
                                elif record.state == "PENDING":
                                    ui.label("Review state: pending; no qualifying independent sign-off exists for this exact value revision at this cutoff.")

                                may_review = (
                                    principal.has_capability("value.validate")
                                    and principal.subject != record.claim.claimant
                                    and record.value.revision_kind.value == "VALUE"
                                    and record.value.category in {"benefit", "operating_cost"}
                                    and record.value.maturity.value == "OBSERVED"
                                )
                                if may_review:
                                    with ui.dialog() as review_dialog, ui.card():
                                        ui.label("Independent value review").classes("text-h6")
                                        ui.label(f"Sign-off binds value revision {record.value.entry_id}, claim revision {record.claim.claim_revision_id}, evidence {record.value.evidence_identity}, cost model {record.value.cost_model_identity}, rate policy {record.value.rate_policy_identity or 'none'}, and cutoff {result.knowledge_cutoff.isoformat()}.")
                                        rationale = ui.textarea("Reviewer rationale", value="Evidence and cost-model bindings reviewed.")
                                        review_status = ui.label("").props('role="status" aria-live="polite"')

                                        def commit_review(decision: ReviewDecision) -> None:
                                            try:
                                                reason = str(rationale.value or "").strip()
                                                if not reason:
                                                    raise ValueError("A reviewer rationale is required.")
                                                command_seed = f"{record.group_id}:{record.value.entry_id}:{decision.value}:{result.knowledge_cutoff.isoformat()}:{datetime.now(timezone.utc).isoformat()}"
                                                command_id = "outcome-review-" + hashlib.sha256(command_seed.encode("utf-8")).hexdigest()
                                                review_context = CommandContext(command_id, composition.principal_provider(), composition.scope_provider(), record.aggregate_version, reason=reason)
                                                composition.outcomes.review_value(
                                                    review_context,
                                                    group_id=record.group_id,
                                                    value_entry_id=record.value.entry_id,
                                                    decision=decision,
                                                    knowledge_cutoff=result.knowledge_cutoff,
                                                    rationale=reason,
                                                    supersedes_review_id=record.review.review_id if record.review else None,
                                                )
                                                review_dialog.close()
                                                review_status.set_text("Review recorded through the O2 command receipt. Refreshing at the selected knowledge cutoff.")
                                                asyncio.create_task(load())
                                            except VersionConflictError:
                                                load_status.set_text("CONFLICT: A newer value revision committed. Reviewer action is disabled while the current group is refreshed.")
                                                review_button.element.disable()
                                                asyncio.create_task(load())
                                                review_status.set_text("Conflict: a newer claim revision is current. Refresh and review the current revision.")
                                            except AuthorizationDeniedError:
                                                review_status.set_text("Permission denied: current reviewer authorization is required.")
                                            except Exception:
                                                review_status.set_text("Review was not recorded. Check the current revision and reviewer rationale, then retry.")

                                        with ui.element("div").classes("ephi-outcomes-action-row"):
                                            _ensure_outcomes_action_target(ActionButton("Approve value", intent=ButtonIntent.PRIMARY, on_click=lambda: commit_review(ReviewDecision.APPROVED)))
                                            _ensure_outcomes_action_target(ActionButton("Reject value", intent=ButtonIntent.DANGER, on_click=lambda: commit_review(ReviewDecision.REJECTED)))
                                    review_button = _ensure_outcomes_action_target(ActionButton("Review evidence and sign off", intent=ButtonIntent.SECONDARY, on_click=review_dialog.open))
                                    review_action_buttons.append(review_button)
                                elif record.value.category in {"benefit", "operating_cost"}:
                                    message = (
                                        "Voided revisions remain in history and cannot be reviewed."
                                        if record.value.revision_kind.value == "VOID"
                                        else "Self-validation is blocked for the claimant."
                                        if principal.subject == record.claim.claimant
                                        else "Current reviewer authorization or observed evidence is required for sign-off."
                                    )
                                    ui.label(message).classes("ephi-o10-truth-note")

                    selected_entry.on_value_change(render_detail)
                    render_detail()

    await load()


async def build_outcomes_page(composition: EphiUiComposition) -> None:
    """Canonical /ephi/outcomes destination on the pinned public Base shell."""

    principal = composition.principal_provider()
    with AppShell(
        "EPHI",
        _navigation(),
        active_route="/ephi/outcomes",
        environment=os.environ.get("EPHI_ENV", "development"),
        subtitle="Outcomes",
        user_name=principal.subject,
        user_role="Value reviewer" if principal.has_capability("value.validate") else "Engineer",
        debugger=False,
    ):
        from nicegui import ui

        ui.query("main").props('role="region" aria-label="EPHI application content"')
        with AnalysisWorkspacePage("", None) as page:
            with page.slot(LayoutSlot.HEADER):
                heading = _semantic_heading(
                    "Outcomes",
                    "Estimated opportunity, observed outcomes and independently reviewed benefit use separate value and knowledge-time rules.",
                    autofocus=False,
                )
                heading.classes("ephi-outcomes-page-heading")
                heading.style("font-size: clamp(1.875rem, 4vw, 3rem) !important; line-height: 1.1 !important; white-space: nowrap;")
            with page.slot(LayoutSlot.PRIMARY):
                await _render_outcomes_content(composition)


def build_page() -> None:
    """Base scaffold page; missing bindings render an explicit fail-closed state."""

    try:
        composition = build_composition_from_environment()
    except Exception as error:
        with AppShell(
            "EPHI",
            _navigation(),
            active_route="/",
            environment=os.environ.get("EPHI_ENV", "development"),
            subtitle="Attention → Episode",
            user_name="Unavailable",
            user_role="Unknown",
            debugger=False,
        ):
            from nicegui import ui

            ui.query("main").props('role="region" aria-label="EPHI application content"')
            _render_attention_error(error)
        return
    build_attention_page(composition)


def run_ephi() -> None:
    settings = RuntimeSettings.from_environment()
    require_security_preflight()
    policy, config = build_runtime_security_contract(settings)
    composition = build_composition_from_environment()
    runtime_adapter = NiceGUIRuntimeAdapter(config)
    from nicegui import app as nicegui_app
    from .family_center import build_family_center_index_page, build_family_center_page
    from .assets import build_asset_360_page, build_asset_index_page

    install_browser_transport_stack(nicegui_app, runtime_adapter, policy)
    runtime_adapter.run(
        root=lambda: build_attention_page(composition),
        pages={
            "/episode": lambda: build_episode_page(composition),
            "/ephi/outcomes": lambda: build_outcomes_page(composition),
            "/ephi/assets": lambda: build_asset_index_page(composition, _navigation()),
            "/ephi/assets/{asset_id}": lambda asset_id: build_asset_360_page(composition, asset_id, _navigation()),
            "/ephi/families": lambda: build_family_center_index_page(composition, _navigation()),
            "/ephi/families/{family_id}": lambda family_id: build_family_center_page(
                composition, family_id, _navigation()
            ),
        },
    )
