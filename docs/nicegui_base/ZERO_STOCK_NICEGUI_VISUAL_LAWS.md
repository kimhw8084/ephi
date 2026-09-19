# Zero Stock NiceGUI Visual Laws — NiceGUI Base v3

NiceGUI/Quasar/AG Grid may provide runtime behavior, but NiceGUI Base owns the visible product experience. A recognizable stock widget is a visual defect and should be corrected before handoff.

## Construction law

Application code uses NiceGUI Base semantic APIs first. Complex NiceGUI widgets are permitted inside framework adapters only when their visible anatomy is normalized by NiceGUI Base styling or replaced with a Company-owned renderer. Do not fork NiceGUI or monkey-patch Quasar internals.

## Required normalization families

- Buttons: q-btn internal spacing, pseudo states, ripple/focus, icons and density.
- Fields/selects: q-field shell, native/input typography, focus/error/read-only/disabled, select popup, chips, clear/arrow affordances.
- Choices/ranges: checkbox, radio, toggle, slider/range track/thumb/ticks/focus.
- Navigation: tabs, segmented controls, expansion headers, splitter handles, drawer navigation.
- Workflow/content: stepper, tree, uploader, progress/spinner, Markdown/code/JSON/log surfaces.
- Overlays: dialog backdrop, menus, popovers, tooltip and toast. Stock q-notification must not be used.
- Data grid: actual AG Grid `.ag-*` DOM is themed; styling fictional table `th/td` markup is insufficient.

## Runtime prohibitions

Framework integrations must not use `ui.notify`, `ui.menu_item` or raw `ui.icon` for canonical NiceGUI Base. Tooltips must carry NiceGUI Base styling. Application code must never construct raw NiceGUI/Quasar visual components as a shortcut.

## Visual consistency rules

1. All visual values derive from NiceGUI Base semantic tokens.
2. Every `var(--cui-*)` reference must resolve to a declared custom property.
3. Light, dark and system themes must preserve semantic hierarchy.
4. Comfortable, compact and dense modes use the same visual grammar, only different geometry.
5. Breakpoints use the semantic phone/tablet/laptop system; component-specific breakpoints require an explicit reason.
6. SVG resources come from `Icons.*` / `Illustrations.*`; no remote icon/font dependency.
7. Status must never depend on color alone.
8. Focus-visible treatment is mandatory for interactive surfaces.
9. `prefers-reduced-motion` and `forced-colors` are first-class modes.
10. Pattern pages encode canonical layout geometry; applications should fill slots rather than redesigning page structure.

## Rendered-product check

Browser checks inspect the rendered DOM and geometry. They should fail when stock Quasar controls appear outside approved Company wrappers, AG Grid appears outside the Company grid theme, stock notifications appear, or unapproved Material icon nodes leak into canonical surfaces. Source checks and rendered-browser checks are complementary; neither should be treated as a substitute for the other.

## Product standard

The target is not “customized NiceGUI.” The target is a NiceGUI Base product whose implementation happens to use NiceGUI underneath. If a reviewer can identify a component as stock NiceGUI/Quasar from its visible appearance, the UI is not complete.
