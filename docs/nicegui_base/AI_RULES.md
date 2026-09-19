# AI Construction Rules

These rules are authoritative for Gemma/OpenCode-generated applications.

## Architecture

- **AI001 — Framework first.** Use NiceGUI Base public APIs before NiceGUI or Quasar APIs.
- **AI002 — Stable internals.** Do not edit `nicegui_base/` unless explicitly tasked with framework development.
- **AI003 — Layer separation.** UI/page modules compose UI and call services; repositories own SQL/data access.
- **AI004 — Small callbacks.** Event handlers should delegate substantial work to services/async primitives.
- **AI005 — Typed APIs.** Prefer typed enums/dataclasses over stringly-typed configuration.
- **AI006 — No invented APIs.** Inspect source/registry/catalog before naming a class, property or enum.

## Page and layout

- **L01 — Pattern first.** Select a registered page pattern before composing a page manually.
- **L02 — Semantic slots.** Fill pattern slots such as filters, metrics, primary, data and details.
- **L03 — Semantic geometry.** Use `GridPreset`, `PanelSize`, `Gap`, `ContentWidth`, `SplitPane`, etc.; do not specify arbitrary geometry.
- **L04 — Framework spacing.** Never set page/section gaps using arbitrary pixels.
- **L05 — Responsive ownership.** Do not hand-build desktop/mobile duplicates; approved layouts own transformations.
- **L06 — Contextual detail.** Use `DetailDrawer` for quick contextual inspection and a dedicated route for extensive workflows.
- **L07 — Dialog restraint.** Use dialogs for short focused decisions; do not place entire applications in modal dialogs.
- **L08 — Card restraint.** Do not nest decorative cards or convert every section into a card.

## Components and visual language

- **C01 — Semantic controls.** Use NiceGUI Base buttons, inputs, surfaces, status components and forms.
- **C02 — No raw colors.** Do not place arbitrary hex/RGB/HSL colors in app code.
- **C03 — No arbitrary CSS.** App-level `.style()` and utility-class visual construction are prohibited by default.
- **C04 — No arbitrary icons.** Use `Icons.*`; never download icons or use emoji as controls.
- **C05 — State completeness.** Data-backed regions require loading, ready, empty and error behavior.
- **C06 — Accessibility.** Icon-only actions require accessible labels; state is never communicated by color alone.
- **C07 — Zero stock visual leakage.** NiceGUI/Quasar may supply runtime behavior only; every visible complex widget must be Company-rendered or covered by the approved normalization layer.
- **C08 — Company overlays.** Do not use stock notify/menu/tooltip/icon presentation paths; route through NiceGUI Base adapters.
- **C09 — Grid ownership.** AG Grid must live inside the Company grid theme and may not expose its default visual language.

## Tables

- **T01 — DataTable first.** Use `DataTable`, `ServerDataTable`, `EditableTable` or `MasterDetailTable`.
- **T02 — No raw AG Grid.** Do not call `ui.aggrid` in application code.
- **T03 — Large data.** Use server query contracts for datasets unsuitable for client materialization.
- **T04 — Persistence.** Use table-state/preset mechanisms for user-owned columns/density/sorts.
- **T05 — Editing.** Editing requires validation and explicit error feedback.

## Visualization

- **V01 — Registered grammar.** Choose a registered visualization wrapper.
- **V02 — No raw ECharts styling.** Theme, palettes, axes and standard toolbars are framework-owned.
- **V03 — Linked analysis.** Use `CrossFilterEngine` / `LinkedAnalysisController` rather than manually coupling widgets.
- **V04 — Limits.** Use `SpecLimits` and engineering composition helpers for limits/targets.
- **V05 — Stable series identity.** Do not assign arbitrary series colors.

## State, async and convenience

- **S01 — No direct storage.** Use state/preference services instead of `app.storage` directly.
- **S02 — Duplicate work.** Use `AsyncAction` for user-triggered async work.
- **S03 — Rapid queries.** Use cancellation/debounce/stale-response primitives for search/filter workloads.
- **S04 — Refresh.** Use `AutoRefreshController`; preserve existing data on transient refresh failures when appropriate.
- **S05 — URL semantics.** Use typed URL state for shareable analytical context.

## Engineering semantics

- **E01 — Spec vs control.** LSL/USL/target are not LCL/UCL/centerline.
- **E02 — Commonality.** Shared routing/exposure is evidence, not automatic causality.
- **E03 — Contradictions.** Do not hide contradictory evidence by default.
- **E04 — Confidence.** Do not display a percentage unless it is explicitly calibrated as a probability.
- **E05 — Zero denominators.** Do not fabricate infinite ratios when control exposure is zero.

## Security/runtime

- **R01 — Fail closed.** Protected content requires server-side authorization before rendering/data access.
- **R02 — Proxy identity.** Trust identity headers only through the configured proxy trust/assertion contract.
- **R03 — Secrets.** Runtime secrets come from secure configuration/environment and are redacted from logs.
- **R04 — Uploads.** Use `UploadPolicy`; do not accept arbitrary active content by default.
- **R05 — Deployment.** One Uvicorn worker per NiceGUI process. Scale through multiple instances with session affinity and shared persistence when required.

## Completion

Run `python -m nicegui_base.validate <app-root>`. Errors must be fixed. Warnings should be fixed unless a documented escape hatch is justified. For stricter gates use `--warnings-as-errors`.
## Production-completion laws (v1.2)

- **P01 — Accessibility is structural.** Preserve framework labels, `aria-*` relationships, focus behavior, skip navigation and non-color state cues.
- **P02 — Runtime controls are framework-owned.** Use NiceGUI Base table toolbar, density, columns, selection, dialogs, state actions and chart toolbars rather than rebuilding them.
- **P03 — Content registry first.** Use registered metrics, property/detail displays, tree/viewers, workflow, comparison, command palette and activity components before custom presentation.
- **P04 — Durable work boundary.** Use `DurableJobAdapter` when work must survive a process restart; `InProcessJobAdapter` is only for non-durable work.
- **P05 — Sanitized/local content.** Markdown remains sanitized by default and remote images/resources are disabled unless an approved company policy explicitly permits them.
- **P06 — Current NiceGUI contracts.** Do not bypass NiceGUI Base adapters with old AG Grid/NiceGUI calling conventions.


## Coding-agent operations (v3)

- **A01 — Task context first.** Run `nicegui-base agent-context "<task>"` before substantive coding and inspect the selected golden example/catalog entries.
- **A02 — Versioned scaffold.** `nicegui-base agent-check .` must confirm the generated agent materials match the installed NiceGUI Base version.
- **A03 — Golden paths.** Prefer `examples/nicegui_base/` canonical examples before constructing a novel page architecture.
- **A04 — Owned tasks.** Raw `asyncio.create_task`/`ensure_future` is prohibited by default; use runtime-owned tasks, async primitives, or durable jobs.
- **A05 — Public renderer boundary.** Do not import `nicegui_base.integrations.nicegui_*` directly from application code.
- **A06 — Typed renderer contract.** Do not use raw `.props(...)` as a routine customization mechanism; request/extend a typed NiceGUI Base capability when needed.
