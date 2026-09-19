# Anti-Patterns

| Anti-pattern | Why it is rejected | Correct direction |
|---|---|---|
| `from nicegui import ui` throughout app pages | Bypasses platform consistency and validation | Import NiceGUI Base public APIs |
| `ui.aggrid(...)` | Recreates table behavior/theme/state | `DataTable` / `ServerDataTable` |
| `ui.echart(...)` | Recreates chart grammar/theme | Registered visualization wrappers |
| `.style('padding: 13px; color: #...')` | Breaks tokens and dark mode | Semantic component/layout parameters |
| Tailwind/Quasar utility strings per page | Makes AI-generated layouts inconsistent | Framework classes/APIs only |
| Emoji for delete/warning/settings | OS-dependent, inconsistent icon language | `Icons.DELETE`, `Icons.WARNING`, etc. |
| Remote icon/font/image CDN | Breaks offline/certified deployment | Packaged visual assets |
| SQL inside `pages/*.py` | Couples presentation and data access | Repository/service layer |
| Giant button callback with query + transform + rendering | Hard to test/cancel/reuse | Service + `AsyncAction` / controller |
| Manual `app.storage.user` writes | Couples app logic to NiceGUI internals | `PreferenceService` / state abstractions |
| One modal containing a full workflow | Poor context/navigation/accessibility | Drawer or dedicated route/page |
| Every section inside a decorative card | Visual noise | `Section`/`Panel`, card only for meaningful containment |
| Commonality rank labeled “root cause probability” | Unsupported causal claim | Evidence/commonality ranking + explicit confidence semantics |
| Trusted identity header without proxy trust/assertion | Spoofable authentication | Phase 10 header-auth contract |

| `ui.notify(...)`, `ui.menu_item(...)`, raw `ui.icon(...)` | Reintroduces stock NiceGUI/Quasar visual language | Company toast/menu/SVG adapters |
| Attaching a Company class to an unthemed complex Quasar widget | Leaves internal stock anatomy visible | Use the approved normalization layer or Company-owned renderer |
| Styling `th/td` while rendering AG Grid | Does not theme AG Grid runtime DOM | Theme `.ag-*` anatomy under `.cui-data-table` |
