# Static Validator Rules

Run `python -m nicegui_base.validate <app-root>` or the installed `nicegui-base-validate` command. Use `--format json` for agent-readable output and `--warnings-as-errors` for strict CI gates.

| Code | Severity | Meaning | Corrective action |
|---|---|---|---|
| AI000 | Error | Python syntax error | Fix syntax before framework validation |
| AI001 | Error | Direct NiceGUI import | Use NiceGUI Base public APIs |
| AI002 | Error | Raw AG Grid | Use `DataTable`/`ServerDataTable` |
| AI003 | Error | Raw ECharts | Use registered visualization wrappers |
| AI004 | Error | Raw NiceGUI layout | Use page patterns/layout primitives |
| AI005 | Error | Raw NiceGUI control | Use NiceGUI Base controls |
| AI006 | Error | Inline `.style()` / raw style kwarg | Use semantic parameters/tokens |
| AI007 | Warning | Raw class/utility styling | Prefer semantic APIs |
| AI008 | Warning | Literal icon string | Use `Icons.*` |
| AI009 | Error | Remote visual/runtime resource | Package resource locally |
| AI010 | Error | Direct NiceGUI storage | Use state/preferences services |
| AI011 | Warning | Hard-coded visual value | Use semantic design tokens |
| AI012 | Warning | Emoji-like UI symbol | Use canonical icon assets |
| AI013 | Warning | SQL in page/view module | Move query to repository/service |
| AI014 | Warning | Application-level CSS file | Prefer framework APIs; isolate documented extension CSS |
| AI015 | Warning | Raw `asyncio.create_task` / `ensure_future` | Use runtime-owned tasks, `AsyncAction`, cancellation primitives, or durable jobs |
| AI016 | Warning | Raw `.props(...)` renderer configuration | Prefer typed NiceGUI Base parameters; document a narrow escape hatch when necessary |
| AI017 | Warning | Direct `nicegui_base.integrations.nicegui_*` import | Import the governed public symbol from `nicegui_base` |

## Narrow exemption

Place `# nicegui-base: allow-aiNNN` immediately above an intentionally exempt line. The exemption is line-local and must represent a documented framework gap; it is not a general “disable lint” mechanism.
