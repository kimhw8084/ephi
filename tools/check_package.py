#!/usr/bin/env python3
"""Validate the canonical repository package and preserve historical evidence."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tomllib
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
IGNORED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "artifacts"}
IGNORED_FILES = {".DS_Store", "EPHI_1.0_Design_Pack.zip"}
STATUS = "CANONICAL_REPO_BASELINE_NOT_PRODUCTION_QUALIFIED"
REQUIRED = {
    "README.md", "AGENTS.md", "CONTRIBUTING.md", "manifest.json",
    ".gitignore", ".gitattributes", ".github/workflows/package.yml",
    "evidence/README.md", "evidence/import/original_manifest.json",
    "evidence/review/package_review.json", "evidence/review/base_reference_check.json",
    "environment/w0_repo_baseline.json", "pyproject.toml", "src/ephi/__init__.py",
    "src/ephi/config_preflight.py", "src/ephi/runtime_configuration_contract.json",
    "tools/check_package.py", "tools/generate_runtime_configuration_contract.py",
    "tools/w0_repo_baseline.py", "tests/test_package.py", "tests/test_runtime_config_preflight.py",
}
SECRET_PATTERNS = (
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{40,}\b",
    r"\bAKIA[A-Z0-9]{16}\b",
)


def digest(path: Path) -> dict:
    content = path.read_bytes()
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def package_files(root: Path) -> list[Path]:
    def walk(directory: Path):
        for path in sorted(directory.iterdir()):
            if path.name in IGNORED_DIRS or path.name in IGNORED_FILES:
                continue
            if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise ValueError(f"symlink is not a package file: {path.relative_to(root)}")
            if path.is_dir():
                yield from walk(path)
            elif path.is_file():
                yield path

    return sorted(walk(root))


def safe_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError(f"unsafe manifest path: {name!r}")
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path escapes package: {name!r}")
    return path


def read_json(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=invalid_constant)


def refresh_manifest(root: Path) -> None:
    manifest = read_json(root / "manifest.json")
    manifest["files"] = [
        {"path": path.relative_to(root).as_posix(), **digest(path)}
        for path in package_files(root) if path != root / "manifest.json"
    ]
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def markdown_errors(root: Path, path: Path) -> list[str]:
    errors = []
    fence = None
    visible = []
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            run, rest = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                fence = None
            continue
        if fence is None:
            visible.append(line)
    if fence is not None:
        errors.append("unclosed Markdown code fence")
    # The repository uses inline links. Ignore code spans and external targets.
    prose = re.sub(r"`[^`]*`", "", "\n".join(visible))
    for target in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)", prose):
        target = target.strip().strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        candidate = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
        if not candidate.is_relative_to(root.resolve()) or not candidate.exists():
            errors.append(f"broken local link: {target}")
    return errors


def historical_errors(root: Path, manifest: dict) -> list[str]:
    errors = []
    original_path = root / "evidence/import/original_manifest.json"
    original = read_json(original_path)
    if digest(original_path)["sha256"] != manifest["imported_design_pack"]["original_manifest_sha256"]:
        errors.append("original manifest identity changed")
    evidence = [entry for entry in original["files"] if entry["path"].startswith("evidence/")]
    if len(evidence) != 9:
        errors.append("historical evidence inventory must contain nine artifacts")
    for entry in evidence:
        path = safe_path(root, entry["path"])
        if not path.is_file() or digest(path) != {key: entry[key] for key in ("bytes", "sha256")}:
            errors.append(f"historical evidence changed: {entry['path']}")

    inventory = read_json(root / "evidence/source_inventory.json")
    if len(inventory) != 409 or len({row["path"] for row in inventory}) != 409:
        errors.append("source inventory must contain 409 unique records")
    for row in inventory:
        safe_path(root, row["path"])
        if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            errors.append(f"invalid source inventory hash: {row['path']}")
    for prefix, expected in (("src/ephi/", (237, 27778)),
                             ("company_port/src/", (18, 776)), ("tests/", (147, 5624))):
        rows = [row for row in inventory if row["path"].startswith(prefix)]
        if (len(rows), sum(row["lines"] for row in rows)) != expected:
            errors.append(f"historical source counts disagree: {prefix}")

    cases = list(ET.parse(root / "evidence/pytest_results.xml").getroot().iter("testcase"))
    skipped = sum(case.find("skipped") is not None for case in cases)
    failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    result = manifest["historical_source_test_result"]
    if (len(cases) - skipped - failed, skipped, failed) != (result["passed"], result["skipped"], 0):
        errors.append("historical JUnit counts disagree")
    if f"{result['passed']} passed, {result['skipped']} skipped" not in (root / "evidence/pytest.log").read_text():
        errors.append("historical pytest log counts disagree")
    environment = read_json(root / "evidence/audit_environment.json")
    if environment["zip_sha256"] != original["ephi_archive_sha256"]:
        errors.append("historical source archive identities disagree")
    if environment["explicit_routes"] != 53 or len(environment["routes"]) != 53:
        errors.append("historical API route counts disagree")
    probes = read_json(root / "evidence/behavior_probes.json")
    if len(probes) != 4 or {probe["id"] for probe in probes} != {"F02", "F03", "F04", "F05"}:
        errors.append("historical probe IDs disagree")
    if (root / "evidence/behavior_probes.json").read_bytes() != (root / "evidence/behavior_probes.log").read_bytes():
        errors.append("historical probe log and JSON disagree")
    return errors


def validate(root: Path) -> list[str]:
    errors = []
    files = package_files(root)
    names = {path.relative_to(root).as_posix() for path in files}
    for missing in sorted(REQUIRED - names):
        errors.append(f"required file missing: {missing}")
    manifest = read_json(root / "manifest.json")
    if manifest["status"] != STATUS:
        errors.append("design package must not claim implemented/qualified status")
    declared = [entry["path"] for entry in manifest["files"]]
    if declared != sorted(set(declared)):
        errors.append("manifest paths must be unique and sorted")
    if set(declared) != names - {"manifest.json"}:
        errors.append("manifest file inventory differs from package")
    for entry in manifest["files"]:
        path = safe_path(root, entry["path"])
        if not path.is_file():
            errors.append(f"manifest file missing: {entry['path']}")
        elif digest(path) != {key: entry[key] for key in ("bytes", "sha256")}:
            errors.append(f"manifest hash/size mismatch: {entry['path']}")

    for path in files:
        name = path.relative_to(root).as_posix()
        try:
            if path.suffix == ".json":
                read_json(path)
            elif path.name == "pyproject.toml":
                tomllib.loads(path.read_text(encoding="utf-8"))
            elif path.suffix == ".py":
                ast.parse(path.read_text(encoding="utf-8"), filename=name)
            elif path.suffix == ".md":
                errors.extend(f"{name}: {message}" for message in markdown_errors(root, path))
            content = path.read_bytes().decode("utf-8", errors="replace")
            if any(re.search(pattern, content) for pattern in SECRET_PATTERNS):
                errors.append(f"possible credential/private key in {name}; inspect locally")
        except (ValueError, SyntaxError) as exc:
            errors.append(f"invalid syntax in {name}: {type(exc).__name__}")

    coverage = {
        "01_Product_and_Architecture.md": [f"I{i:02}" for i in range(1, 13)],
        "02_Source_Audit.md": [f"F{i:02}" for i in range(1, 15)],
        "09_Delivery_and_Gates.md": [f"G{i:02}" for i in range(13)] + [f"W{i}" for i in range(8)],
        "10_Traceability_and_Decisions.md": [f"R{i}" for i in range(1, 9)] + [f"T{i}" for i in range(1, 5)],
    }
    chapters = [name for name in names if re.match(r"^\d\d_.*\.md$", name)]
    if len(chapters) != 14 or {name[:2] for name in chapters} != {f"{i:02}" for i in range(14)}:
        errors.append("expected numbered chapters 00 through 13")
    for name, identifiers in coverage.items():
        content = (root / name).read_text(encoding="utf-8")
        for identifier in identifiers:
            if not re.search(rf"\b{identifier}\b", content):
                errors.append(f"missing traceability identifier {identifier} in {name}")
    errors.extend(historical_errors(root, manifest))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-manifest", action="store_true", help="Record intentionally changed file hashes before checking.")
    args = parser.parse_args()
    try:
        if args.refresh_manifest:
            refresh_manifest(ROOT)
        errors = validate(ROOT)
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as exc:
        errors = [f"cannot validate package: {exc}"]
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"PASS: {len(package_files(ROOT))} package files; manifest, syntax, local links/fences, traceability and historical evidence.")
    print("Scope: canonical repository baseline present; Application/runtime/browser/production qualification NOT_RUN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
