# Gemma/OpenCode Quick Start

## Before coding

1. Read `AGENTS.md`.
2. Read `AI_CONSTRUCTION_MANIFEST.json`.
3. Identify the dominant user task and select a page pattern from `docs/APP_PATTERNS.md`.
4. Search `FRAMEWORK_CATALOG.json` or `docs/COMPONENT_CATALOG.md` for required capabilities.
5. Inspect the real Python signature before writing the call; `docs/PUBLIC_API_INDEX.md` is a discovery index, not a substitute for source inspection.

## Normal application skeleton

```text
my_app/
├── app.py
├── app_config.py
├── pages/
├── services/
├── repositories/
├── models/
└── tests/
```

Pages compose NiceGUI Base. Services implement application logic. Repositories implement SQL/API access. Models carry typed business data.

## Runtime

Use `NiceGUIRuntimeAdapter(RuntimeConfig(...))` instead of importing NiceGUI just to call `ui.run()`.

## Visual construction

Use this order:

```text
registered Page Pattern
  → semantic Layout primitives/slots
    → registered Component/Interaction/Table/Visualization
      → application-specific data and events
```

Do not start from CSS, raw NiceGUI, AG Grid or ECharts.

## After coding

```bash
python -m nicegui_base.validate .
python -m nicegui_base.validate . --format json
python -m nicegui_base.validate . --warnings-as-errors
```

Then run application tests and a startup smoke test. A validator error is not a suggestion; fix it or document a narrow, legitimate escape hatch.

## v3 coding-agent fast path

For a fresh application workspace:

```bash
nicegui-base agent-init .
```

For each substantive task:

```bash
nicegui-base agent-context "<task>"
# implement using the recommended golden example + public API
nicegui-base agent-check .
```

`agent-context` is deliberately compact so local/smaller coding models do not need the entire framework in working context. `agent-check` fails if the generated framework guidance is stale or the application violates/warns on NiceGUI Base construction laws.

## Wave 62 semiconductor application factory

For semiconductor applications, prefer a registered complete-app recipe before hand-assembling repeated panels. Discover by intent with `nicegui-base recipes "<engineering intent>"`, then create with `nicegui-base create ./<app> --name "<App Name>" --template analysis-workspace --recipe <recipe>`. The eight registered starters are `spc-monitor`, `excursion-defense-line`, `fdc-tool-health`, `lot-wafer-explorer`, `yield-loss`, `pm-effect-analysis`, `chamber-matching`, and `rca-cockpit`. See `SEMICONDUCTOR_APPLICATION_RECIPES.md`; the machine-readable authority is `.nicegui_base/framework_catalog.json` → `registries.semiconductor_recipes`.

A recipe composes the existing `DataSource`, `AnalysisContext`, `SelectionBus`, manufacturing filters and workspace/analytical-panel lifecycle. It must not create a second query, filter, population, selection or layout system. Replace only the generated `DataSource` fixture/provider boundary for production data.

