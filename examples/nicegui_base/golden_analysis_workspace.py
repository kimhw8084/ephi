"""Golden v3 workspace ownership pattern for dense analytical applications."""
from nicegui_base import (
    Aggregation,
    ApplicationRuntime,
    Dataset,
    Dimension,
    Metric,
    PanelSpec,
    StateKey,
    StateNamespace,
    WorkspaceBreakpoint,
)


def build_runtime() -> tuple[ApplicationRuntime, str]:
    runtime = ApplicationRuntime()
    runtime.data.register(
        Dataset(
            'analysis',
            (
                {'id': 1, 'area': 'ETCH', 'value': 12.4},
                {'id': 2, 'area': 'ETCH', 'value': 11.9},
                {'id': 3, 'area': 'CVD', 'value': 13.1},
            ),
            row_key='id',
            dimensions=(Dimension('area'),),
            metrics=(Metric('value', aggregation=Aggregation.AVG),),
        )
    )
    workspace = runtime.open_workspace('investigation')
    workspace.open_data_session('analysis', session_id='primary')
    workspace.layout.register_panel(PanelSpec('trend', preferred_columns=8, preferred_rows=5))
    workspace.layout.register_panel(PanelSpec('records', preferred_columns=4, preferred_rows=5))
    workspace.layout.derive_breakpoint(WorkspaceBreakpoint.PHONE, source=WorkspaceBreakpoint.DESKTOP)
    selected = StateKey[str | None]('selected_record', namespace=StateNamespace.WORKSPACE, default=None)
    workspace.state.set(selected, None, source='golden-example')
    return runtime, workspace.workspace_id


__all__ = ['build_runtime']
