# Canonical Application Recipes

These recipes define **construction order**, not application-specific business logic.

## Recipe 1 — Filters + chart + records + detail

**Use:** `DataExplorerPage`.

1. Define typed URL/filter state if the analytical context should be shareable.
2. Put analytical controls in `FilterBar`.
3. Load data through a service/repository; wrap rapid changes with `Debouncer`/`CancelableTask`/`StaleResponseGuard` where appropriate.
4. Put high-level metrics in the metrics slot.
5. Use a registered chart wrapper in the primary/secondary slots.
6. Use `DataTable` or `ServerDataTable` in the data slot.
7. Use `DetailDrawer` for quick entity inspection.
8. Use `CrossFilterEngine` / `LinkedAnalysisController` for chart↔table filtering.
9. Provide loading, empty, error and stale/refresh states.
10. Run NiceGUI Base validation.

## Recipe 2 — Large server-side table

1. Use `ServerDataTable` and `TableQuery`/`TableResult` contracts.
2. Keep SQL/query construction inside the repository/service.
3. Map NiceGUI Base filter/sort/pagination state into the repository query.
4. Return only the requested page plus total/continuation metadata required by the table contract.
5. Persist user-owned table layout through table state/preferences, not browser hacks.
6. Use cancellation/stale-response protection when filters can change rapidly.

## Recipe 3 — Create/edit workflow

- Short contextual edit: `FormDrawer`.
- Very short focused decision: `FormDialog`.
- Complex multi-section workflow: dedicated route/page or `WizardPage`.
- Use `Form`, `FormField`, validation helpers and `DirtyStateGuard`.
- Use `AsyncAction` for save/submit and standardized success/error feedback.

## Recipe 4 — Monitoring page

1. Use `MonitoringPage`.
2. Lead with overall status/alerts, then KPIs/trends/affected records.
3. Use `AutoRefreshController` rather than raw timers.
4. Preserve the last valid data when a transient refresh fails if business semantics allow.
5. Show freshness/stale indicators.

## Recipe 5 — Master/detail entity explorer

1. Use `MasterDetailPage` / `MasterDetailLayout`.
2. Keep selection state explicit and URL-addressable if shareable.
3. Master region uses list/tree/table depending on the entity set.
4. Detail region uses `EntityHeader`, property/status primitives, tabs/sections, charts/tables as required.
5. On phone, transform detail into a full-screen contextual surface.

## Recipe 6 — Engineering affected-vs-control comparison

1. Use `ComparisonPage` or an engineering composition inside `DataExplorerPage`.
2. Construct affected/control population summaries through engineering analytics helpers.
3. Use common bin boundaries for distribution comparison.
4. Keep spec limits distinct from control limits.
5. Treat commonality/enrichment as evidence, not proof of cause.
6. Keep contradictory evidence visible.
7. Do not display confidence as a percentage unless explicitly calibrated.

## Recipe 7 — Protected application page

1. Resolve identity through the configured authentication adapter.
2. Apply `AccessPolicy` through the Phase 10 HTTP/runtime guard **before protected content or data access**.
3. Never rely on hidden/disabled UI as authorization.
4. Use permission-aware actions for discoverability, while server checks remain authoritative.
5. Redact secrets from logs and use correlation IDs for supportability.

## Recipe 8 — Search-as-you-type

1. Use `SearchInput`.
2. Apply `Debouncer` to reduce work.
3. Use `CancelableTask` for latest-request-wins semantics if backend work is cancellable.
4. Use `StaleResponseGuard` so old results cannot overwrite newer results.
5. Render explicit loading/no-results/error states.

## Recipe 9 — New icon/domain visual

1. Search `Icons.*`, `ICON_REGISTRY` and aliases first.
2. If the concept truly has no canonical asset, add a project-authored SVG through the Visual Asset System—not application code.
3. Add manifest/provenance, semantic key, alias if necessary, validator coverage and catalog entry.

## Recipe 10 — Legitimate framework escape hatch

1. Demonstrate the registered framework cannot satisfy the requirement.
2. Keep the extension isolated behind an application-specific component/adapter.
3. Reuse NiceGUI Base tokens, lifecycle, accessibility and error/state primitives.
4. Add an explicit validator exemption only for the exact line/rule required.
5. If the behavior is likely to recur, promote it into the framework instead of copying it across apps.

## Recipe 11 — Semiconductor application starter

1. Run `nicegui-base recipes "<engineering intent>"` before composing a repeated SPC/FDC/wafer/yield/RCA workspace from scratch.
2. Select one of the eight registered Wave 62 recipes and generate it with `nicegui-base create ... --template analysis-workspace --recipe <recipe>`.
3. Keep `services/data_source.py` as the provider boundary; replace the fixture with the approved `DataSource` adapter rather than adding provider code to the page.
4. Let strict recipe assembly fail explicitly when required source semantics are missing; use field overrides only when they are semantically correct.
5. Reuse the recipe's one `AnalysisContext`, `SelectionBus`, manufacturing filters and `WorkspaceController`; do not fork state for a panel.
6. Customize the generated application through declared panels, services and provider adapters. Promote reusable changes back into the recipe registry instead of copying them across apps.


## Wave 63 semiconductor runtime rule

When a task maps to a registered semiconductor recipe, prefer the governed Wave 63 variant suggested by agent context, connect data through `SemiconductorSourceAdapter`, inspect `RecipeOnboardingReport`, and keep app-specific panel/filter/binding changes in `RecipeCustomization`. Never bypass an ambiguous binding by guessing the first same-role column.


## Wave 64 production hardening

Before treating a semiconductor recipe as production-ready, run bounded adapter conformance (`run_semiconductor_adapter_conformance`) and runtime pushdown diagnostics (`runtime.performance_report()`). Use `runtime.onboarding_view()` for explicit binding setup and `runtime.capture_preset()` / `restore_preset()` for safe saved reviews. Missing installed NiceGUI/server/browser/company-adapter/human-baseline execution remains PENDING, never inferred from source tests. See `SEMICONDUCTOR_PRODUCTION_RUNTIME.md`.

## Wave 65 semiconductor provider/RC rule

For a semiconductor recipe using non-fixture data, keep the recipe/runtime authorities and implement the source through `SemiconductorProviderAdapterBase`. Use `provider-check` with bounded fixtures during development, then the production conformance profile and `provider-rc` benchmark on representative target data before promotion. Render guided setup through `SemiconductorSetupWizard`; do not hide unresolved bindings, population guardrails, pushdown failures, or missing target/browser/human evidence. A runnable development workflow is not release certification.
