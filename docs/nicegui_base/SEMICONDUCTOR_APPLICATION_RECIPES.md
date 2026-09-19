# Semiconductor Application Recipe Factory — Wave 62

Wave 62 turns the Wave 59–61 platform into complete, governed semiconductor application starters. A recipe is **composition metadata**, not a new runtime. Every assembled application continues to use the existing `DataSource`, `AnalysisContext`, `SelectionBus`, `AnalysisCoordinator`, manufacturing filters, workspace controller, and `SemiconductorAnalyticalPanel` lifecycle.

## One-command workflow

Discover the best application for an engineering intent:

```bash
nicegui-base recipes "compare chambers after PM"
```

Create the recommended starter:

```bash
nicegui-base create ./pm_effect --name "PM Effect Analysis" --template analysis-workspace --recipe pm-effect-analysis
```

The generated starter contains a provider-neutral `services/data_source.py`, an executable `prepare_analysis()` composition boundary, the shared workspace page, NiceGUI Base agent materials, and behavioral starter tests. Replace the fixture `DataSource` with the approved production provider adapter; do not move provider-specific query code into the page.

## Registered recipes

| Recipe | Application | Best for | Avoid when |
|---|---|---|---|
| `spc-monitor` | `SPCMonitor` | Drift, control charts, limits, capability, wafer-linked SPC | The question is primarily multivariate FDC/tool health or RCA evidence synthesis |
| `excursion-defense-line` | `ExcursionDefenseLine` | Affected/control excursion containment, spatial delta, commonality and records | There is no defined affected/control population or excursion boundary |
| `fdc-tool-health` | `FdcToolHealth` | Sensor traces, golden envelopes, tool/chamber fingerprints and equipment events | Only scalar process measurements exist and trace/equipment context is unavailable |
| `lot-wafer-explorer` | `LotWaferExplorer` | Lot-to-wafer navigation, wafer maps, bins, defects and spatial patterns | The task is primarily time-series SPC without wafer coordinates |
| `yield-loss` | `YieldLoss` | Yield/bin Pareto, decomposition and wafer-linked loss localization | The task needs causal attribution without sufficient evidence or population definition |
| `pm-effect-analysis` | `PMEffectAnalysis` | Before/after PM comparison, chamber response, events and affected/control analysis | PM/event timing or a defensible baseline/after population cannot be defined |
| `chamber-matching` | `ChamberMatching` | Chamber comparison, fingerprint separation and spatial corroboration | There are too few comparable chambers or inconsistent recipe/operation context |
| `rca-cockpit` | `RcaCockpit` | Commonality, enrichment, genealogy, hypothesis/evidence and causal investigation flow | A simple monitoring question can be answered by a narrower recipe |

The machine-readable authority is `SEMICONDUCTOR_RECIPE_REGISTRY`; `nicegui-base recipes` is the human/agent discovery surface.

## Intelligent data binding

Each recipe declares required and optional logical fields. Assembly resolves them in this order:

1. Explicit `field_overrides` supplied by the application.
2. Canonical/known aliases declared by the recipe.
3. Compatible semantic roles from the Wave 59 `DataSchema`.

Strict assembly fails with `RecipeCompatibilityError` if required semantics cannot be resolved. Non-strict assembly may omit panels whose declared fields are unavailable, but it never invents plausible values or silently substitutes unrelated columns.

```python
assembly = await assemble_semiconductor_application(
    'spc-monitor',
    source,
    field_overrides={'measurement': 'cd_nm'},
)
```

## Shared state and interactions

A recipe declares manufacturing filters, populations, panels and cross-panel interactions, but it does not own a second state system. `assemble_semiconductor_application()` creates or accepts exactly one Wave 60 `AnalysisContext` and `SelectionBus`. `SemiconductorAnalysisContext`, dependent manufacturing filters, analytical surfaces, records/detail surfaces and workspace metadata all attach to those authorities.

When embedding a recipe in a larger application, pass the existing context and selection bus. The assembly tracks ownership and will not close externally owned authorities during teardown.

## Layout rules

Recipe panels become ordinary Wave 60 `PanelSpec` instances. Desktop geometry is defined by recipe metadata; tablet and phone geometry is derived by the existing workspace layout engine. The phone layout is full-width and collision-free. Do not add a recipe-specific CSS/grid implementation.

The generated page uses the `analysis-workspace` pattern and `NiceGUIWorkspace`. A recipe cannot be paired with another base template because that would create competing layout/state ownership.

## Scale and provider boundaries

Recipes operate on the `DataSource` contract and schema/distinct/query APIs. This keeps server-side filtering and pushdown possible for fab-scale data. The dependency-free reference calculations remain independently testable; optimized numerical or warehouse implementations can sit behind adapters without changing the application recipe contract.

Do not:

- load an entire production fab table into page memory just because the generated fixture is in-memory;
- add SQL/vendor code to `pages/home.py`;
- create local copies of manufacturing filters, populations or selections;
- bypass the registered analytical surfaces with raw ECharts/AG Grid unless a documented framework escape hatch is genuinely required;
- treat recipe recommendations as causal conclusions—the recipe chooses a workflow, not the answer.

## Agent usage

For a semiconductor task, run `nicegui-base agent-context "<task>"`. The context pack includes the recommended `analysis-workspace` starter and, when applicable, a Wave 62 recipe. Agents should inspect `.nicegui_base/framework_catalog.json` → `registries.semiconductor_recipes` before hand-assembling a page.

Representative intent mappings:

- “compare chambers after PM” → `pm-effect-analysis`
- “is chamber B drifting?” → `spc-monitor`
- “FDC sensor drift on chamber B” → `fdc-tool-health`
- “where on wafer did the process change?” → `excursion-defense-line`
- “explore all wafers in lot L123” → `lot-wafer-explorer`
- “why did yield drop and which bins dominate?” → `yield-loss`
- “match chambers and explain fingerprint separation” → `chamber-matching`
- “RCA commonality with hypotheses and evidence” → `rca-cockpit`

## Completion contract

A recipe-generated application is a production starting point, not a runtime certification. Before release, run the framework/application gates, use the approved target `DataSource` adapter, exercise representative fab-scale queries, and perform NiceGUI/browser/company-environment validation in the actual target environment. Do not promote static/source validation into a browser/runtime PASS.

## Wave 63 production-runtime continuation

For production onboarding, governed variants, provider adapters, bounded runtime refresh/records, and explicit binding diagnostics, see `SEMICONDUCTOR_RUNTIME_ONBOARDING.md`. Use `nicegui-base create ... --recipe <recipe> --variant <variant>` for a focused governed starter instead of copying/editing the recipe definition.


## Wave 64 production hardening

Before treating a semiconductor recipe as production-ready, run bounded adapter conformance (`run_semiconductor_adapter_conformance`) and runtime pushdown diagnostics (`runtime.performance_report()`). Use `runtime.onboarding_view()` for explicit binding setup and `runtime.capture_preset()` / `restore_preset()` for safe saved reviews. Missing installed NiceGUI/server/browser/company-adapter/human-baseline execution remains PENDING, never inferred from source tests. See `SEMICONDUCTOR_PRODUCTION_RUNTIME.md`.
