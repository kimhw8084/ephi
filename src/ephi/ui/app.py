"""Composition and pages for the narrow durable Attention → Episode slice."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from collections.abc import Callable

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
)

from ephi.application.attention import AttentionQueryService
from ephi.application.context import AccessScope, CommandContext, CurrentAuthorizationAuthority, Principal
from ephi.application.episodes import EpisodeBrief, EpisodeBriefQueryService
from ephi.application.errors import (
    AuthorizationDeniedError,
    CommandError,
    CoherentReadConflictError,
    QuerySnapshotExpiredError,
    ScopeDeniedError,
    StorageFailureError,
    VersionConflictError,
)
from ephi.application.workflow import EpisodeWorkflowCommandService
from ephi.application.source_reality import require_runtime_source_binding
from ephi.config import RuntimeSettings
from ephi.infrastructure.postgresql import PostgreSQLReferenceTransactionAdapter
from ephi.transport import (
    BrowserTransportMiddleware,
    BrowserTransportPolicy,
    build_runtime_config,
    require_security_preflight,
)
from .provider import EphiReadDataSource


ORIGIN_KEY = StateKey("ephi.origin", StateNamespace.WORKSPACE, default="/attention")
EPISODE_KEY = StateKey("ephi.episode_id", StateNamespace.WORKSPACE, default=None)
FILTER_KEY = StateKey("ephi.attention.filters", StateNamespace.WORKSPACE, default={})
DRAFT_KEY = StateKey("ephi.episode.draft", StateNamespace.WORKSPACE, default={})


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
    if isinstance(error, (AuthorizationDeniedError, ScopeDeniedError)):
        return StateViewSpec(StateKind.PERMISSION, "Permission denied", "Current authorization does not permit this data or action.")
    if isinstance(error, QuerySnapshotExpiredError):
        return StateViewSpec(StateKind.ERROR, "Attention snapshot expired", "Refresh Attention to start a new retained query snapshot.", action_label="Refresh")
    if isinstance(error, VersionConflictError):
        return StateViewSpec(StateKind.ERROR, "Version conflict", "The workflow changed in another session. Refresh the Episode before retrying.", action_label="Refresh")
    if isinstance(error, CoherentReadConflictError):
        return StateViewSpec(StateKind.ERROR, "Stale decision read", "The analytical revision and workflow state were not one coherent read.", action_label="Refresh")
    if isinstance(error, StorageFailureError):
        return StateViewSpec(StateKind.OFFLINE, "Source unavailable", "PostgreSQL or the required EPHI source is unavailable. No healthy or empty state is inferred.", action_label="Retry")
    return StateViewSpec(StateKind.ERROR, "EPHI request failed", "The operation did not commit. Retry only after reviewing the current state.")


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


def build_attention_page(composition: EphiUiComposition) -> None:
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
        with MasterDetailPage("Attention", "Scoped engineering work with retained, coherent reads") as page:
            with page.slot(LayoutSlot.FILTERS):
                StateView(StateViewSpec(StateKind.EMPTY, "Attention is server-authorized", "Filters and order are translated to the bounded EPHI query contract.", compact=True))
            attention_data_slot = page.slot(LayoutSlot.DATA)
            with attention_data_slot:
                attention_table: DataSourceTable | None = None

                async def refresh_attention() -> None:
                    if attention_table is None:
                        return
                    await attention_table.refresh(force=True)

                async def render_attention_error(error: BaseException) -> None:
                    with attention_data_slot:
                        _render_attention_error(error, on_refresh=refresh_attention)

                try:
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
                            empty_message="No permitted Attention rows",
                            error_message="Attention is unavailable; last known truth is not replaced with zero.",
                        ),
                        on_row_double_click=lambda event: _select_episode(composition, event),
                        on_error=render_attention_error,
                    )
                except Exception as error:
                    _render_attention_error(error)
            with page.slot(LayoutSlot.DETAILS):
                StateView(StateViewSpec(StateKind.EMPTY, "Select an episode", "Double-click an Attention row to preserve its origin and open the Episode decision brief."))


def _select_episode(composition: EphiUiComposition, event: object) -> None:
    row = (getattr(event, "args", {}) or {}).get("data") or {}
    episode_id = row.get("episode_id")
    if not isinstance(episode_id, str) or not episode_id:
        return
    composition.workspace.state.set(ORIGIN_KEY, "/", source="ephi.attention")
    composition.workspace.state.set(EPISODE_KEY, episode_id, source="ephi.attention")
    composition.workspace.selections.apply(
        Selection(SelectionKind.ENTITY, episode_id, {"episode_id": episode_id}, source="attention"),
        source="ephi.attention",
    )


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


def _render_brief_actions(composition: EphiUiComposition, brief: EpisodeBrief, guard: StaleResponseGuard) -> None:
    initial_scope = composition.scope_provider()
    initial_subject = composition.principal_provider().subject
    claim_command_id = _stable_command_id("ClaimEpisode", brief, initial_scope, initial_subject)
    acknowledge_command_id = _stable_command_id("AcknowledgeEpisode", brief, initial_scope, initial_subject)

    async def commit_claim() -> None:
        principal = composition.principal_provider()
        scope = composition.scope_provider()
        command = CommandContext(
            claim_command_id,
            principal,
            scope,
            brief.revision_vector.workflow_version,
            brief.revision_vector,
            "Claim the W1 engineer case",
        )
        try:
            await asyncio.to_thread(composition.workflow.claim_episode, command, brief.episode_id)
            refreshed = await _load_brief(composition, brief.episode_id, guard)
            _render_episode_body(composition, refreshed, guard)
        except Exception as error:
            _render_brief_error(error)

    async def commit_acknowledge() -> None:
        principal = composition.principal_provider()
        scope = composition.scope_provider()
        command = CommandContext(
            acknowledge_command_id,
            principal,
            scope,
            brief.revision_vector.workflow_version,
            brief.revision_vector,
            "Acknowledge the W1 engineer case",
        )
        try:
            await asyncio.to_thread(composition.workflow.acknowledge_episode, command, brief.episode_id)
            refreshed = await _load_brief(composition, brief.episode_id, guard)
            _render_episode_body(composition, refreshed, guard)
        except Exception as error:
            _render_brief_error(error)

    principal = composition.principal_provider()
    if brief.workflow.get("work_state") == "OPEN":
        ActionButton("Claim episode", intent=ButtonIntent.PRIMARY, on_click=commit_claim)
    elif brief.workflow.get("work_state") == "CLAIMED" and brief.workflow.get("owner") == principal.subject:
        ActionButton("Acknowledge episode", intent=ButtonIntent.PRIMARY, on_click=commit_acknowledge)
    else:
        StateView(StateViewSpec(StateKind.EMPTY, "No action available", "Workflow truth is authoritative; this session cannot overwrite the current owner."))


def _render_episode_body(composition: EphiUiComposition, brief: EpisodeBrief, guard: StaleResponseGuard) -> None:
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
                StatusBadge(f"{key}: {value}", intent=_intent_for_capability(value))
            DescriptionList(
                (
                    KeyValueItem("known_at", "Known at", brief.known_at),
                    KeyValueItem("owner", "Owner", brief.workflow.get("owner") or "Unassigned"),
                    KeyValueItem("work_state", "Work state", brief.workflow.get("work_state")),
                    KeyValueItem("source_state", "Source state", brief.capability_state),
                )
            )
            _render_brief_actions(composition, brief, guard)


def _render_brief_error(error: BaseException) -> None:
    with Card():
        StateView(_state_for_error(error))


async def build_episode_page(composition: EphiUiComposition) -> None:
    workspace = composition.workspace
    episode_id = workspace.state.get(EPISODE_KEY)
    if not episode_id:
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
        with AnalysisWorkspacePage("Episode decision brief", "One coherent analytical/read revision with live durable workflow"):
            if not episode_id:
                StateView(StateViewSpec(StateKind.EMPTY, "No episode selected", "Return to Attention and select one permitted episode."))
                return
            guard = StaleResponseGuard()
            try:
                brief = await _load_brief(composition, episode_id, guard)
            except Exception as error:
                _render_brief_error(error)
                return
            _render_episode_body(composition, brief, guard)


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
            _render_attention_error(error)
        return
    build_attention_page(composition)


def run_ephi() -> None:
    settings = RuntimeSettings.from_environment()
    require_security_preflight()
    config = build_runtime_config(settings)
    base_issues = config.validate_environment()
    if base_issues:
        raise RuntimeError(f"EPHI security preflight blocked: {','.join(base_issues)}")
    policy = BrowserTransportPolicy.from_environment()
    policy.validate()
    composition = build_composition_from_environment()
    runtime_adapter = NiceGUIRuntimeAdapter(config)
    runtime_adapter.install_middleware()
    # The transport gate is added after Base middleware registration so its
    # pure-ASGI check remains outside NiceGUI routing/client creation while
    # Base SecurityHeadersMiddleware remains the outer response policy.
    from nicegui import app as nicegui_app

    nicegui_app.add_middleware(BrowserTransportMiddleware, policy=policy)
    runtime_adapter.run(
        root=lambda: build_attention_page(composition),
        pages={"/episode": lambda: build_episode_page(composition)},
    )
