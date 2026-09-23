"""Deterministic source-boundary check for the supported synthetic package."""

from __future__ import annotations

import ast
from pathlib import Path


_ALLOWED_EPHI_IMPORTS = {
    "ephi",
    "ephi.application",
    "ephi.config",
    "ephi.downstream",
    "ephi.infrastructure",
    "ephi.recovery",
}
_FORBIDDEN_NAMES = {"monkeypatch", "patch", "patch.object", "setattr"}


def check_synthetic_boundary(root: Path | None = None) -> dict[str, object]:
    """Check imports and fixture placement; this is not an audit of arbitrary code."""

    repository = root or Path(__file__).resolve().parents[3]
    fixture = repository / "examples" / "synthetic_downstream"
    files = tuple(sorted(fixture.rglob("*.py"))) if fixture.is_dir() else ()
    forbidden_imports: set[str] = set()
    forbidden_constructs: set[str] = set()
    syntax_errors: list[str] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        except (OSError, SyntaxError):
            syntax_errors.append(path.relative_to(repository).as_posix())
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                modules = []
            for module in modules:
                if module == "ephi" or module.startswith("ephi."):
                    if module not in _ALLOWED_EPHI_IMPORTS:
                        forbidden_imports.add(module)
            if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
                forbidden_constructs.add(node.id)
            if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
                forbidden_constructs.add(node.attr)

    fixture_paths = [path.relative_to(repository).as_posix() for path in files]
    structurally_outside = fixture.resolve().is_relative_to(repository.resolve()) and not fixture.resolve().is_relative_to(
        (repository / "src" / "ephi").resolve()
    ) and not fixture.resolve().is_relative_to((repository / "migrations").resolve())
    passed = bool(files) and not forbidden_imports and not forbidden_constructs and not syntax_errors and structurally_outside
    return {
        "status": "PASS" if passed else ("NOT_RUN" if not files else "FAIL"),
        "public_imports": "PASS" if not forbidden_imports and not syntax_errors and files else "FAIL",
        "core_edit_boundary": "PASS" if structurally_outside and files else "FAIL",
        "fixture_files": fixture_paths,
        "forbidden_imports": sorted(forbidden_imports),
        "forbidden_constructs": sorted(forbidden_constructs),
        "syntax_errors": sorted(syntax_errors),
        "scope": "Supported synthetic example only; not a cryptographic proof about arbitrary private packages.",
    }


__all__ = ["check_synthetic_boundary"]
