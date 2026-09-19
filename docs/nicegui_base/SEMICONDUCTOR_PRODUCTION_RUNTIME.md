# Semiconductor Production Adapter & Runtime Hardening — Wave 64

Wave 64 hardens the Wave 63 recipe runtime for real production onboarding without adding a parallel data, state, layout, or persistence system.

## Production path

1. Adapt the approved provider behind `SemiconductorSourceAdapter`; return an existing Wave 59 `DataSource`.
2. Run `run_semiconductor_adapter_conformance()` with production policy. Probes are bounded and capability claims are verified through `QueryStats`.
3. Inspect `runtime.onboarding_view()` and resolve ambiguous/required bindings explicitly. Never choose the first same-role field silently.
4. Use `runtime.refresh()`, `runtime.records()`, and `runtime.experience_state()` for the standard ready/loading/stale/empty/partial/error contract.
5. Run `runtime.performance_report()` against representative production-like data. Filter, pagination and projection pushdown are observable requirements, not documentation claims.
6. Save review state with `runtime.capture_preset()` and restore it with `runtime.restore_preset()`. Presets reuse the Wave 60 context/selection/workspace snapshots and validate source fields before restore.
7. Assemble target evidence with `build_semiconductor_target_runtime_certification()`. Missing installed NiceGUI, real server/WebSocket, corporate browser or human visual-baseline execution remains **PENDING**.

## Adapter conformance

`AdapterConformancePolicy` controls production requirements. The default policy requires a healthy source plus filter and pagination pushdown. Projection/distinct pushdown can also be mandatory. Conformance never enumerates an entire source; every row-returning probe is bounded, and distinct probing uses an equality predicate to keep cardinality bounded.

A provider that advertises pushdown but returns `QueryStats.pushdown=False` fails conformance. A provider that does not advertise a required capability also fails. Development fixtures may use an explicitly relaxed policy, but that is not production certification.

## Onboarding UX

`build_recipe_onboarding_view()` converts the Wave 63 report into a UI-ready contract with:

- bound / ambiguous / required / optional field states;
- ranked alternatives and confidence evidence;
- explicit blocking actions;
- panel/filter availability;
- deterministic completion ratio.

`SemiconductorOnboardingPanel` and `SemiconductorRuntimeStatusPanel` render those contracts through NiceGUI Base components rather than page-local NiceGUI markup.

## Runtime presets

A `RecipeRuntimePreset` captures:

- `AnalysisContextSnapshot`;
- `SelectionSnapshot`;
- workspace layout snapshot;
- workspace interaction snapshot;
- recipe/variant identity.

Serialization delegates the nested runtime state to the existing Wave 60 `workspace_snapshot_to_dict()` / `workspace_snapshot_from_dict()` persistence contract. Restore rejects recipe/variant mismatches and source fields that do not exist in the current schema.

## Performance and scale

`probe_semiconductor_runtime_performance()` issues bounded context, pagination and filter queries. It can enforce:

- filter pushdown;
- pagination pushdown;
- projection pushdown;
- optional latency budgets;
- optional scan-amplification budgets when a provider reports `rows_scanned`.

The source suite includes a large synthetic SQLite test to demonstrate true pushdown without introducing a mandatory database dependency beyond Python's standard library.

## Target certification boundary

Source validation cannot certify the final corporate environment. The target promotion object keeps these gates distinct:

- approved company adapter conformance;
- representative fab-scale pushdown/performance;
- installed NiceGUI 3.15.0;
- real server/WebSocket lifecycle;
- supported corporate browser;
- explicit human visual-baseline approval.

A missing target gate is PENDING, never PASS.
