# Visual Resource Guide

## Normal application usage
- Prefer `Icons.<NAME>` for icon keys.
- Use `SvgIcon(Icons.REFRESH, label="Refresh data")` for a standalone accessible icon.
- Higher-level NiceGUI Base components should receive semantic icon keys rather than raw SVG or external URLs.
- Use `Illustrations.<NAME>` for framework state artwork.
- Search with `search_icons("wafer")` when the required vocabulary is unknown.

## AI rules
1. Never download an icon or image to satisfy a normal UI need.
2. Never use emoji for controls.
3. Never embed app-authored SVG when an existing semantic asset is available.
4. Inspect `Icons`, `ICON_REGISTRY`, or `ICON_CATALOG.md` before requesting a new icon.
5. Treat icon keys as stable public API.
6. New domain icons must follow the 24x24 / 1.75 rounded-line grammar and enter the manifest.
7. Run `validate_visual_package()` after adding/changing assets.

## Runtime behavior
All runtime SVG is read from the installed Python package and validated for forbidden remote/script constructs. The NiceGUI adapter renders the validated SVG inline so `currentColor` inherits the component's semantic theme color.
