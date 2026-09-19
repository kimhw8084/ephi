# Troubleshooting Guide for Gemma/OpenCode

## Import/API error

1. Do not guess the API.
2. Search `docs/COMPONENT_CATALOG.md` and the relevant registry.
3. Inspect the actual dataclass/function signature in `nicegui_base/`.
4. Check framework version/compatibility manifest.

## Validator reports raw NiceGUI

Replace the raw control/layout with the corresponding NiceGUI Base component or page/layout primitive. Use an escape-hatch annotation only when no registered primitive can meet the requirement.

## UI looks inconsistent

Search for `.style`, `.classes`, raw colors, arbitrary pixel values, direct NiceGUI imports, remote assets and literal `icon="..."`. Run strict validation.

## Table requirement seems unsupported

Inspect `TABLE_REGISTRY`; decide among local `DataTable`, server table, editable table or master-detail. Do not switch to raw AG Grid before inspecting the existing table models/adapters.

## Chart requirement seems unsupported

Inspect `VISUALIZATION_REGISTRY`. Use `PlotlyPanel` only for specialist Plotly needs, not as a shortcut around the standard visualization grammar.

## Search/filter requests race each other

Use `Debouncer`, `CancelableTask` and/or `StaleResponseGuard`; do not add manual generation counters in page code.

## Refresh temporarily fails

Use `AutoRefreshController` and preserve the prior successful view when business semantics allow; communicate stale/refresh-error state rather than blanking the page.

## Authentication behaves differently behind proxy

Check `RuntimeConfig` proxy trust, forwarded-header handling and proxy assertion secret. Do not weaken validation by trusting all inbound identity headers.

## NiceGUI runtime mismatch

Run `RuntimeDoctor`. Install the exact pinned NiceGUI version rather than modifying framework code to accommodate an untested release.

## Framework gap

Document the missing capability, isolate a custom extension behind semantic NiceGUI Base interfaces, preserve tokens/accessibility/state rules, and consider promoting reusable behavior into the framework in a later platform phase.
