# Semiconductor Recipe Production Runtime & Smart Data Onboarding — Wave 63

Wave 63 turns the Wave 62 recipe starters into a conservative production-runtime boundary without creating a second data, state, query, layout, or visualization system.

## Authorities preserved

Every Wave 63 application still uses:

- Wave 59 `DataSource` / `DataSchema` for data, schema, pushdown, provenance, health, and pagination.
- Wave 60 `AnalysisContext`, `SelectionBus`, `AnalysisCoordinator`, analytical panel states, and workspace runtime.
- Wave 61 semiconductor semantic context, dependent manufacturing filters, analytical surfaces, and engineering analytics.
- Wave 62 `SemiconductorRecipeDefinition` as the complete-app composition authority.

Wave 63 adds only onboarding, governed configuration/variants, and runtime orchestration around those authorities.

## Smart binding is conservative

Use `onboard_recipe_source()` before release or when connecting a new provider. Binding evidence is ranked in this order:

1. Explicit application override.
2. Exact logical-field name.
3. Governed recipe alias order.
4. Normalized alias.
5. Declared `DataSchema` semantic role.
6. Explainable name similarity.

`smart_binding_decisions()` never silently resolves a genuinely ambiguous semantic-role match. If two provider fields are equally plausible, the decision remains unresolved and the onboarding report tells the application to provide an explicit override. The compatibility report used by onboarding only contains fields the smart binder actually approved.

```python
report = await onboard_recipe_source('spc-monitor', source)
print(report.to_dict())
```

`RecipeOnboardingReport` includes binding decisions, alternatives, confidence evidence, required/optional gaps, source health, pushdown capabilities, available filters/panels, adapter metadata, and warnings. It is JSON serializable for setup screens, logs, CI, or agent tooling.

## Provider adapter hook

Company/vendor-specific connection logic belongs behind `SemiconductorSourceAdapter`, which returns `AdaptedSemiconductorSource` containing:

- a normal Wave 59 `DataSource`;
- optional explicit field overrides;
- provider metadata for diagnostics;
- an ownership flag controlling source teardown.

The runtime never receives raw SQL/ODBC/REST/vendor clients. That keeps recipe/page code provider-neutral and preserves pushdown.

## Governed variants and customization

`RecipeVariantDefinition` is a controlled view of an existing recipe. It may hide/reorder panels, adjust panel spans, pin filters, provide explicit field overrides, seed initial manufacturing filters, or attach metadata. It cannot introduce unknown panels, filters, fields, surfaces, or a competing runtime.

Wave 63 ships one focused variant per complete Wave 62 application:

| Recipe | Variant | Purpose |
|---|---|---|
| `spc-monitor` | `spc-monitor-fast-response` | Drift/capability/records first |
| `excursion-defense-line` | `excursion-containment` | Containment/commonality first |
| `fdc-tool-health` | `fdc-chamber-triage` | Trace/envelope/fingerprint triage |
| `lot-wafer-explorer` | `lot-wafer-spatial` | Spatial wafer review |
| `yield-loss` | `yield-loss-triage` | Pareto/decomposition triage |
| `pm-effect-analysis` | `pm-recovery` | Post-PM recovery verification |
| `chamber-matching` | `chamber-match-core` | Fingerprint/PCA/distribution matching |
| `rca-cockpit` | `rca-evidence-first` | Evidence/commonality/genealogy first |

App-specific changes use `RecipeCustomization`; do not copy a base recipe and edit it locally.

## Production runtime

Create the runtime with `create_semiconductor_recipe_runtime()`.

```python
runtime = await create_semiconductor_recipe_runtime(
    'spc-monitor',
    approved_adapter,
    variant='spc-monitor-fast-response',
    customization=RecipeCustomization(initial_filters={'product': 'P1'}),
)
probe = await runtime.refresh()
```

The runtime owns no alternative state. It assembles the selected recipe using the existing `AnalysisContext` and `SelectionBus`, exposes dependent manufacturing filter options, supports bounded context-aware record queries, propagates selection through the shared bus, tracks source ownership, and marks surfaces stale when shared context changes.

`refresh()` performs a bounded readiness probe. It does not load the full fab dataset. It standardizes:

- source unavailable/error → panel `ERROR`, runtime `BLOCKED`;
- no matching rows → panel `EMPTY`, runtime `DEGRADED`;
- valid current context → panel `READY`;
- context changed after refresh → panel `STALE` until refreshed.

Use `records()` for paginated detail access. Production providers should advertise pagination/filter pushdown; onboarding warns when the source does not.

## Generated application contract

Wave 63 recipe starters include:

- `services/data_source.py` — local governed fixture only;
- `services/data_adapter.py` — replace internals with the approved provider adapter;
- `recipe_config.py` — recipe variant, field overrides, customization, runtime policy;
- `pages/home.py` — shared recipe/workspace UI using one `AnalysisContext` and `SelectionBus`;
- `prepare_runtime()` — production runtime composition boundary;
- `prepare_analysis()` — retained Wave 62 compatibility boundary.

Example:

```bash
nicegui-base create ./spc_runtime \
  --name "SPC Runtime" \
  --recipe spc-monitor \
  --variant spc-monitor-fast-response
```

## Release boundary

Source validation cannot certify a real browser/runtime/company data path. Before stable production promotion, execute installed NiceGUI 3.15.0, real server/WebSocket, supported browser, approved production adapters, representative fab-scale queries, and human visual-baseline checks in the target environment. Historical browser evidence is not a Wave 63 runtime PASS.
