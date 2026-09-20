"""Composition and pages for the narrow durable Attention → Episode slice."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
    DataSourceTable,
    DescriptionList,
    EntityHeader,
    FullScreenWorkspace,
    KeyValueItem,
    LayoutSlot,
    MasterDetailPage,
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
from ephi.application.context import AccessScope, CommandContext, CurrentAuthorizationAuthority, Principal
from ephi.application.episodes import EpisodeBrief, EpisodeBriefQueryService
from ephi.application.workflow import EpisodeWorkflowCommandService
from ephi.application.o10 import (
    attention_result_status as _attention_result_status,
    display_value as _display_value,
    episode_action as _episode_action,
    state_for_error as _state_for_error_facts,
)
from ephi.application.source_reality import require_runtime_source_binding
from ephi.config import RuntimeSettings
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter
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
    """Compose only from explicit PostgreSQL and identity bindings."""

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
    )


def _navigation() -> NavigationModel:
    return NavigationModel((NavSection("work", "Work", (NavItem("attention", "Attention", "/"), NavItem("episode", "Episode", "/episode"))),))


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


class _EpisodeView:
    def __init__(self, composition: EphiUiComposition, host: object, page_heading: object, episode_id: str, focus_request: object) -> None:
        self.composition = composition
        self.host = host
        self.page_heading = page_heading
        self.episode_id = episode_id
        self.focus_request = focus_request
        self.guard = StaleResponseGuard()
        self.primary_action: ActionButton | None = None
        self.live_status: object | None = None

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
            brief = await _load_brief(self.composition, self.episode_id, self.guard)
        except Exception as error:
            self.render_error(error)
            return
        self.render_brief(brief, focus_target=focus_target)

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

    def render_brief(self, brief: EpisodeBrief, *, focus_target: str | None = None) -> None:
        from nicegui import ui

        self.primary_action = None
        self.live_status = None
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
                refreshed = await _load_brief(self.composition, brief.episode_id, self.guard)
            except Exception as error:
                self.render_error(error)
                return
            self.render_brief(refreshed, focus_target="primary")
            # The action render replaces the old button in-place. Complete the
            # focus hand-off in this same governed command callback so focus
            # cannot remain on the unmounted, disabled control.
            from nicegui import ui

            self.focus_request.props('data-ephi-focus-request="primary-action"')  # type: ignore[attr-defined]

        def return_to_attention() -> None:
            self.composition.workspace.state.set(FOCUS_KEY, "attention_heading", source="ephi.episode.focus")
            ui.navigate.to("/")

        def render() -> None:
            with FullScreenWorkspace():
                with Card():
                    EntityHeader(
                        brief.episode_id,
                        subtitle=str(brief.analytical.get("title", "Decision brief")),
                        entity_type="Episode",
                        status=str(brief.workflow.get("work_state", "UNKNOWN")),
                        metadata=(
                            KeyValueItem("revision", "Analytical revision", brief.revision_vector.analysis_revision),
                            KeyValueItem("workflow", "Workflow version", brief.revision_vector.workflow_version),
                        ),
                    )
                    for key, value in sorted(brief.capability_state.items()):
                        StatusBadge(f"{key}: {_display_value(value)}", intent=_intent_for_capability(value))
                    DescriptionList(
                        (
                            KeyValueItem("episode_id", "Episode ID", brief.episode_id),
                            KeyValueItem("title", "Issue / title", _display_value(brief.analytical.get("title"))),
                            KeyValueItem("known_at", "Known at", _display_value(brief.known_at)),
                            KeyValueItem("analysis_revision", "Analytical revision", _display_value(brief.revision_vector.analysis_revision)),
                            KeyValueItem("workflow_version", "Workflow version", _display_value(brief.revision_vector.workflow_version)),
                            KeyValueItem("work_state", "Workflow state", _display_value(brief.workflow.get("work_state"))),
                            KeyValueItem("owner", "Owner", _display_value(brief.workflow.get("owner"), unavailable="Unassigned / unavailable")),
                            KeyValueItem("source_state", "Source / capability state", _display_value(brief.capability_state)),
                            KeyValueItem("eligible_action", "Current eligible action", action[0] if action else "Unavailable: current authorization or owner does not permit an action"),
                        )
                    )
                    ui.label("Confidence, onset, exposure, next-check, recovery and value are unavailable/not yet qualified in this W1 brief.").classes("ephi-o10-truth-note")
                    self.live_status = ui.label(
                        f"Episode {brief.episode_id} is rendered from the current authorized coherent read."
                    ).classes("ephi-o10-live-status")
                    self.live_status.props('role="status" aria-live="polite" aria-atomic="true"')
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
            _schedule_focus(self.live_status)
        else:
            _schedule_focus(self.page_heading)


def _render_brief_error(error: BaseException) -> None:
    with Card():
        StateView(_state_for_error(error))


async def build_episode_page(composition: EphiUiComposition) -> None:
    _install_o10_ui_css()
    workspace = composition.workspace
    episode_id = workspace.state.get(EPISODE_KEY)
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
        with AnalysisWorkspacePage("", None):
            heading = _semantic_heading(
                "Episode decision brief",
                "One coherent analytical/read revision with live durable workflow",
                autofocus=not episode_id or workspace.state.get(FOCUS_KEY) == "episode_heading",
            )
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

    install_browser_transport_stack(nicegui_app, runtime_adapter, policy)
    runtime_adapter.run(
        root=lambda: build_attention_page(composition),
        pages={"/episode": lambda: build_episode_page(composition)},
    )
