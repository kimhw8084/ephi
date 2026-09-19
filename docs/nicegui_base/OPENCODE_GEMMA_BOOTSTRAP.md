# OpenCode / Gemma Bootstrap Prompt

Use the following as the first instruction for a coding agent working in an application built on NiceGUI Base.

> Treat NiceGUI Base as the application platform and `nicegui_base/` as stable framework code unless the task explicitly requests framework development. Read `AGENTS.md` first. Before coding, run `nicegui-base agent-context "<current task>"` and inspect the recommended golden example plus `.nicegui_base/framework_catalog.json`. Use only real NiceGUI Base public APIs; do not invent symbols or parameters. Keep page modules limited to composition and event delegation, with business logic in services and data access in repositories. Do not import NiceGUI directly, create raw AG Grid/ECharts, add arbitrary CSS/geometry/colors, or create parallel state/filter/layout/overlay systems. Use v3 `ApplicationRuntime`/`WorkspaceRuntime`, `DataSession`, workspace layout, and semantic visualization layers when they remove duplicate ownership. Preserve existing UI/UX unless the task explicitly improves it. After meaningful changes run `nicegui-base agent-check .`, application tests, and browser/runtime checks for visual work. Do not declare completion with validator warnings, failing tests, or unreviewed visual changes.

For a new workspace, bootstrap once with:

```bash
nicegui-base agent-init .
```

For every substantive task:

```bash
nicegui-base agent-context "<task>"
nicegui-base agent-check .
```


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
