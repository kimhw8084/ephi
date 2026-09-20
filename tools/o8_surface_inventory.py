#!/usr/bin/env python3
"""Deterministic source/runtime inventory for the current EPHI candidate.

The absence result is based on AST imports/calls and the registered page route
shape. It deliberately does not treat documentation or test text as product
surface evidence.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "ephi"

SURFACES = {
    "user_upload": {
        "symbols": {"upload", "fileupload", "file_upload", "upload_file", "upload_route"},
        "description": "user upload component/route",
    },
    "markdown_or_raw_html": {
        "symbols": {"markdown", "markdownviewer", "render_markdown", "html", "rawhtml", "render_html"},
        "description": "arbitrary Markdown/raw HTML rendering/editor accepting user content",
    },
    "csv_or_spreadsheet_export": {
        "symbols": {"csv", "tocsv", "to_csv", "spreadsheet", "export", "exportcsv", "export_csv"},
        "description": "CSV/spreadsheet export endpoint/action",
    },
    "ai_or_llm": {
        "symbols": {"ai", "llm", "openai", "chatcompletion", "completion", "prompt", "generate_text"},
        "description": "AI/LLM input/output feature",
    },
}


def _name(value: ast.AST) -> str | None:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _symbol(value: str) -> str:
    return value.lower().replace("-", "_")


def _source_files() -> list[Path]:
    return sorted(path for path in SOURCE_ROOT.rglob("*.py") if path.is_file())


def inventory_source() -> dict[str, Any]:
    findings = {key: [] for key in SURFACES}
    imports: list[str] = []
    calls: list[str] = []
    routes: list[str] = []
    parsed_files = 0
    for path in _source_files():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        parsed_files += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
                    candidate = _symbol(alias.name.rsplit(".", 1)[-1])
                    for key, definition in SURFACES.items():
                        if candidate in definition["symbols"]:
                            findings[key].append({"kind": "import", "file": relative, "line": node.lineno, "symbol": alias.name})
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imports.append(alias.name)
                    candidate = _symbol(alias.name)
                    for key, definition in SURFACES.items():
                        if candidate in definition["symbols"]:
                            findings[key].append({"kind": "import", "file": relative, "line": node.lineno, "symbol": alias.name})
            elif isinstance(node, ast.Call):
                candidate_name = _name(node.func)
                if candidate_name is None:
                    continue
                candidate = _symbol(candidate_name)
                calls.append(candidate_name)
                for key, definition in SURFACES.items():
                    if candidate in definition["symbols"]:
                        findings[key].append({"kind": "call", "file": relative, "line": node.lineno, "symbol": candidate_name})
            elif isinstance(node, ast.Dict):
                for key_node in node.keys:
                    if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str) and key_node.value.startswith("/"):
                        routes.append(key_node.value)

    # The root page is the root argument to NiceGUIRuntimeAdapter.run; the
    # mapping above captures the secondary page routes.
    registered_routes = sorted({"/", *routes})
    return {
        "scope": "src/ephi application source only; documentation and tests excluded",
        "parsed_python_file_count": parsed_files,
        "registered_application_routes": registered_routes,
        "component_imports": sorted(set(imports)),
        "call_names": sorted(set(calls)),
        "surfaces": {
            key: {
                "present": bool(findings[key]),
                "description": definition["description"],
                "evidence": findings[key],
            }
            for key, definition in SURFACES.items()
        },
        "qualification_required_if_introduced": True,
        "qualification_statement": "Absence is scoped release evidence only; qualification becomes mandatory if any listed surface is later introduced.",
    }


def runtime_route_inventory(app: Any) -> dict[str, Any]:
    """Inspect the live NiceGUI/Starlette route table without payloads."""

    routes: list[dict[str, Any]] = []
    for route in getattr(app, "routes", ()):
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            continue
        methods = sorted(str(method) for method in (getattr(route, "methods", None) or ()))
        routes.append({"path": path, "methods": methods, "name": str(getattr(route, "name", ""))})
    return {"status": "PASS", "routes": sorted(routes, key=lambda item: (item["path"], item["name"]))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = inventory_source()
    print(json.dumps(payload, sort_keys=True, indent=2) if args.json else json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
