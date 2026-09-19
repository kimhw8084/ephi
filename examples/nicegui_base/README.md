# NiceGUI Base Golden Agent Examples

These are canonical construction examples for coding agents. Copy their **architecture and NiceGUI Base usage**, not their demo data.

- `golden_dashboard.py` — KPI/trend overview and runtime adapter.
- `golden_data_explorer.py` — one v3 `DataSession` feeding KPI, chart and table.
- `golden_crud.py` — page/service/repository separation with a governed `DataTable`.
- `golden_analysis_workspace.py` — v3 application runtime, shared dataset, workspace state and responsive panel geometry.

Rules:

1. Prefer the closest example before inventing composition.
2. Keep business-specific data/services outside `nicegui_base/`.
3. Use root-level public imports from `nicegui_base` unless a documented guide explicitly requires otherwise.
4. Run `python -m nicegui_base.validate . --warnings-as-errors` after modification.
5. Visual changes require real browser inspection; source correctness alone is not visual proof.
