# NiceGUI Base Coding-Agent Workflow

This is the operational workflow for OpenCode/Gemma-style coding agents. It intentionally reduces choice: the fastest path is the governed path.

## 0. Bootstrap the workspace once

```bash
nicegui-base agent-init .
```

This installs `AGENTS.md`, NiceGUI Base guides, the machine-readable framework catalog, and canonical golden examples without touching existing application code. Use `--overwrite` only when intentionally refreshing generated NiceGUI Base materials after a framework upgrade.

## 1. Build a task-specific context pack before coding

```bash
nicegui-base agent-context "build an equipment dashboard with KPIs, filters, trend chart and table"
```

The output identifies the dominant page pattern, construction categories, exact documents/registries to inspect, and the closest golden examples. Coding agents should use this compact pack instead of loading the entire framework into context.

## 2. Inspect before inventing

Use this order:

1. closest golden example under `examples/nicegui_base/`
2. `.nicegui_base/framework_catalog.json`
3. `docs/nicegui_base/PUBLIC_API_INDEX.md`
4. actual public Python signature/source when needed

Never guess a NiceGUI Base symbol or parameter.

## 3. Keep application architecture boring

```text
app.py / runtime
pages/              NiceGUI Base composition only
services/           business logic / orchestration
repositories/       SQL / APIs / persistence
models/             typed business models
 tests/              application behavior
```

Pages should remain small. A page callback delegates substantial work to a service or NiceGUI Base async/runtime primitive.

## 4. Use the v3 ownership layers when they remove duplicate logic

- `ApplicationRuntime` / `WorkspaceRuntime` — shared lifecycle, state, commands, undo/redo, diagnostics.
- `Dataset` / `DataSession` — one filter/query authority across table, chart and KPI surfaces.
- `WorkspaceLayoutEngine` — persistent, responsive analytical panel geometry.
- `SemanticVisualizationPlanner` — semantic intent routed into the certified visual renderer stack.

Do not migrate a proven v2 screen merely to say it uses v3. Adopt these layers when they eliminate duplicated state, filtering, lifecycle or layout behavior.

## 5. Validate continuously

```bash
nicegui-base agent-check .
```

This fails closed when the installed framework and generated agent scaffold disagree, or when static NiceGUI Base construction laws produce errors/warnings.

For machine-readable detail:

```bash
python -m nicegui_base.validate . --format json --warnings-as-errors
```

## 6. Completion contract

A coding-agent task is not complete until:

- no NiceGUI Base validator errors or warnings remain;
- application tests pass;
- startup/runtime smoke relevant to the application passes;
- browser output is reviewed for visual changes;
- no raw NiceGUI/AG Grid/ECharts/CSS path was introduced simply because it was faster to type;
- a framework extension is isolated and documented if the public vocabulary genuinely could not express the requirement.

The framework is the platform. The coding agent builds the product on top of it.


## Agent-native application factory (current release)

For a new application, start from a governed template instead of inventing repository structure:

```bash
nicegui-base create ./my_app --name "My App" --template analysis-workspace
cd my_app
nicegui-base agent-context "<current task>"
nicegui-base agent-check .
nicegui-base gate .
```

Available starter intents are `dashboard`, `data-explorer`, `crud`, `analysis-workspace`, `responsive-operations`, and `async-workflow`. Before production promotion run `nicegui-base gate . --release`; it is intentionally fail-closed unless the exact NiceGUI/browser application gate passes.

## Wave 62 semiconductor application factory

For semiconductor applications, prefer a registered complete-app recipe before hand-assembling repeated panels. Discover by intent with `nicegui-base recipes "<engineering intent>"`, then create with `nicegui-base create ./<app> --name "<App Name>" --template analysis-workspace --recipe <recipe>`. The eight registered starters are `spc-monitor`, `excursion-defense-line`, `fdc-tool-health`, `lot-wafer-explorer`, `yield-loss`, `pm-effect-analysis`, `chamber-matching`, and `rca-cockpit`. See `SEMICONDUCTOR_APPLICATION_RECIPES.md`; the machine-readable authority is `.nicegui_base/framework_catalog.json` → `registries.semiconductor_recipes`.

A recipe composes the existing `DataSource`, `AnalysisContext`, `SelectionBus`, manufacturing filters and workspace/analytical-panel lifecycle. It must not create a second query, filter, population, selection or layout system. Replace only the generated `DataSource` fixture/provider boundary for production data.

