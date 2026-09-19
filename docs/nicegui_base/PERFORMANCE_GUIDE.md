# Performance & Convenience Guide

- Measure before optimizing. Use `PerformanceMonitor` for hot paths.
- Use `AnalyticalDataController` for rapidly changing analytical filters/search. It combines debounce, cancellation, single-flight caching, stale-data preservation and lifecycle state.
- Use `TableQueryEngine` for repeated interactions over a stable in-memory table. Do not use it when rows change every interaction.
- Use `AsyncSingleFlightCache` only for deterministic, authorization-safe reads. Never cache mutations.
- Use `LazyResource` for expensive tabs/drawers users may never open.
- Use `ConcurrencyGate` for bounded I/O fan-out.
- Use `RetryPolicy` only for idempotent/transient operations.
- Use `run_blocking` when a synchronous library must run from an async UI path. Prefer native async APIs where available.
- Persist complete user analytical workspaces with `WorkspacePreferenceService`.
- Use `ApplicationServices` rather than constructing the standard service set repeatedly.
