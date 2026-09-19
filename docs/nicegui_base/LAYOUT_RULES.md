# Layout Rules

## Decision order

1. Choose a page pattern from `PATTERN_REGISTRY`.
2. Fill its semantic slots.
3. Use `Page`, `Section`, `Stack`, `Grid`, `ResponsiveGrid`, `SplitPane`, `MasterDetailLayout`, `DashboardGrid`, `StickyPanel`, `ScrollablePanel` or `FullScreenWorkspace` only when the pattern needs composition inside a slot.
4. Raw NiceGUI layout is an escape hatch.

## Canonical rules

- Page padding and section spacing are framework-owned.
- Use `ContentWidth.READING` for long-form/wizard content, `STANDARD` for normal forms/search, `WIDE` for analytics, `FULL` for workspaces.
- Use `GridPreset.METRICS` for KPI groups; do not manually set repeated column widths.
- Use `SplitPane` only when simultaneous visibility materially helps the task.
- Use `MasterDetailLayout` when selecting one entity should preserve the master list.
- Use drawers for contextual detail/filter/edit workflows; phone layouts can transform these into full-screen surfaces.
- Use sticky controls only for actions/filters that remain necessary while scrolling.
- Do not nest scrolling containers without an explicit workspace requirement.
- Do not hardcode widths/heights for standard controls, panels, tables or charts; use semantic sizes.
