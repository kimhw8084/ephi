#!/usr/bin/env python3
"""Validate the canonical Git-native EPHI W0 baseline without source artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tomllib
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "environment" / "w0_repo_baseline.json"
IGNORED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "artifacts"}
IGNORED_FILES = {".DS_Store"}
TEST_TIMEOUT_SECONDS = 300

EXPECTED = {
    "distribution": "ephi",
    "version": "0.1.0",
    "import": "ephi",
    "python_requires": ">=3.11,<3.14",
    "source_root": "src",
    "package_root": "src/ephi",
    "entrypoint": "ephi.app:main",
    "framework_distribution": "nicegui-base",
    "framework_version": "3.0.0a8",
    "framework_repository": "https://github.com/kimhw8084/nicegui-base.git",
    "framework_commit": "000298562d6bcbf6df304edbd41b98b30fe4bfcf",
    "nicegui_requirement": "nicegui==3.15.0",
}


class BaselineError(ValueError):
    """A repository identity or deterministic execution condition that fails closed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(path: Path) -> dict[str, Any]:
    value = path.read_bytes()
    return {"bytes": len(value), "sha256": _sha256_bytes(value)}


def _read_json(path: Path) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BaselineError("JSON_DUPLICATE_KEY", f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise BaselineError("JSON_NONFINITE_NUMBER", f"non-finite JSON number: {value}")

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except FileNotFoundError as exc:
        raise BaselineError("SPEC_MISSING", f"baseline specification is missing: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineError("SPEC_INVALID", f"baseline specification is not valid JSON: {exc}") from exc


def _package_files(root: Path) -> list[Path]:
    values: list[Path] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in relative.parts) or path.name in IGNORED_FILES:
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise BaselineError("PACKAGE_SYMLINK", f"package file is a symlink: {relative}")
        if path.is_file():
            values.append(path)
    return values


def _safe_relative(root: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise BaselineError("MANIFEST_INVALID", f"{field} must be a relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise BaselineError("MANIFEST_INVALID", f"{field} must stay inside the repository")
    path = root.joinpath(*relative.parts)
    if not path.resolve(strict=False).is_relative_to(root.resolve()):
        raise BaselineError("MANIFEST_INVALID", f"{field} escapes the repository")
    return path


def validate_manifest(root: Path) -> dict[str, Any]:
    """Verify current package-file identities without reading historical contents."""

    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path)
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise BaselineError("MANIFEST_INVALID", "manifest.files must be an array")
    listed: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise BaselineError("MANIFEST_INVALID", "manifest file entry must be an object")
        relative = entry.get("path")
        _safe_relative(root, relative, "manifest.files[].path")
        if not isinstance(relative, str) or relative in listed:
            raise BaselineError("MANIFEST_INVALID", "manifest file paths must be unique")
        if not isinstance(entry.get("bytes"), int) or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256"))):
            raise BaselineError("MANIFEST_INVALID", f"manifest identity is incomplete: {relative}")
        listed.append(relative)
    if listed != sorted(listed):
        raise BaselineError("MANIFEST_INVALID", "manifest file paths must be sorted")

    actual_paths = {path.relative_to(root).as_posix() for path in _package_files(root)} - {"manifest.json"}
    if set(listed) != actual_paths:
        raise BaselineError("MANIFEST_MISMATCH", "manifest inventory differs from the current Git worktree")
    for entry in entries:
        path = _safe_relative(root, entry["path"], "manifest.files[].path")
        if not path.is_file() or _digest(path) != {key: entry[key] for key in ("bytes", "sha256")}:
            raise BaselineError("MANIFEST_MISMATCH", f"manifest identity mismatch: {entry['path']}")
    return {"status": "PASS", "file_count": len(actual_paths), "manifest_sha256": _sha256_bytes(manifest_path.read_bytes())}


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    spec = _read_json(path)
    if not isinstance(spec, dict):
        raise BaselineError("SPEC_INVALID", "baseline specification must be an object")
    if spec.get("schema_version") != 1 or spec.get("change") != "CHG-111" or spec.get("wave") != "W0":
        raise BaselineError("SPEC_INVALID", "baseline specification is not CHG-111/W0 schema 1")
    repository = spec.get("repository")
    dependencies = spec.get("dependencies")
    framework = dependencies.get("framework") if isinstance(dependencies, Mapping) else None
    runtime = dependencies.get("runtime") if isinstance(dependencies, Mapping) else None
    expected_spec = {
        "distribution": repository.get("distribution") if isinstance(repository, Mapping) else None,
        "version": repository.get("version") if isinstance(repository, Mapping) else None,
        "import": repository.get("import") if isinstance(repository, Mapping) else None,
        "python_requires": repository.get("python_requires") if isinstance(repository, Mapping) else None,
        "source_root": repository.get("source_root") if isinstance(repository, Mapping) else None,
        "package_root": repository.get("package_root") if isinstance(repository, Mapping) else None,
        "entrypoint": repository.get("entrypoint") if isinstance(repository, Mapping) else None,
        "framework_distribution": framework.get("distribution") if isinstance(framework, Mapping) else None,
        "framework_version": framework.get("version") if isinstance(framework, Mapping) else None,
        "framework_repository": framework.get("repository") if isinstance(framework, Mapping) else None,
        "framework_commit": framework.get("commit") if isinstance(framework, Mapping) else None,
        "nicegui_requirement": runtime[0] if isinstance(runtime, list) and len(runtime) == 1 else None,
    }
    if expected_spec != EXPECTED:
        raise BaselineError("SPEC_IDENTITY_MISMATCH", "baseline specification does not match the pinned canonical identity")
    if spec.get("checks", {}).get("network") != "NOT_REQUIRED":
        raise BaselineError("SPEC_INVALID", "canonical baseline must not require network access")
    return spec


def validate_python_version(version_info: Sequence[int] | None = None) -> dict[str, Any]:
    version = tuple(version_info or sys.version_info[:3])
    supported = (3, 11) <= version[:2] < (3, 14)
    result = {"major": version[0], "minor": version[1], "micro": version[2], "requires": EXPECTED["python_requires"], "supported": supported}
    if not supported:
        raise BaselineError("PYTHON_UNSUPPORTED", f"Python {version[0]}.{version[1]} is outside {EXPECTED['python_requires']}")
    return result


def validate_project_metadata(root: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise BaselineError("PROJECT_INVALID", f"pyproject.toml is invalid: {exc}") from exc
    project = data.get("project")
    if not isinstance(project, Mapping):
        raise BaselineError("PROJECT_INVALID", "pyproject.toml is missing [project]")
    expected_dependencies = {
        f"{EXPECTED['framework_distribution']} @ git+{EXPECTED['framework_repository']}@{EXPECTED['framework_commit']}",
        EXPECTED["nicegui_requirement"],
    }
    actual_dependencies = set(project.get("dependencies", []))
    if project.get("name") != EXPECTED["distribution"] or project.get("version") != EXPECTED["version"]:
        raise BaselineError("PROJECT_IDENTITY_MISMATCH", "project name or version does not match the canonical identity")
    if project.get("requires-python") != EXPECTED["python_requires"]:
        raise BaselineError("PROJECT_IDENTITY_MISMATCH", "project Python requirement does not match the canonical identity")
    if actual_dependencies != expected_dependencies:
        raise BaselineError("DEPENDENCY_IDENTITY_MISMATCH", "project dependencies do not match the exact pinned framework identities")
    scripts = project.get("scripts")
    if not isinstance(scripts, Mapping) or scripts.get("ephi") != EXPECTED["entrypoint"]:
        raise BaselineError("PROJECT_IDENTITY_MISMATCH", "project entrypoint does not match the canonical identity")
    package_root = root / EXPECTED["package_root"]
    if not package_root.is_dir() or not (package_root / "__init__.py").is_file():
        raise BaselineError("PACKAGE_IDENTITY_MISMATCH", "canonical package root is missing")
    return {
        "status": "PASS",
        "distribution": project["name"],
        "version": project["version"],
        "requires_python": project["requires-python"],
        "dependencies": sorted(actual_dependencies),
        "entrypoint": scripts["ephi"],
    }


def _git_output(root: Path, args: list[str]) -> str:
    completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise BaselineError("GIT_COMMAND_FAILED", completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def validate_git_identity(identity: Mapping[str, Any], root: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", str(identity.get("commit", ""))):
        raise BaselineError("GIT_IDENTITY_INVALID", "Git HEAD is not a full commit identity")
    if not re.fullmatch(r"[0-9a-f]{40}", str(identity.get("tree", ""))):
        raise BaselineError("GIT_IDENTITY_INVALID", "Git tree is not a full tree identity")
    if Path(str(identity.get("root", ""))).resolve() != root.resolve():
        raise BaselineError("GIT_IDENTITY_INVALID", "Git root does not match the repository root")
    return dict(identity)


def collect_git_identity(root: Path = ROOT) -> dict[str, Any]:
    identity = {
        "root": _git_output(root, ["rev-parse", "--show-toplevel"]),
        "commit": _git_output(root, ["rev-parse", "HEAD"]),
        "tree": _git_output(root, ["rev-parse", "HEAD^{tree}"]),
        "status_porcelain": _git_output(root, ["status", "--porcelain=v1", "--untracked-files=all"]),
    }
    identity["worktree_clean"] = identity["status_porcelain"] == ""
    identity["status_sha256"] = _sha256_bytes(identity["status_porcelain"].encode())
    return validate_git_identity(identity, root)


def _import_identity(root: Path) -> dict[str, Any]:
    code = (
        "import json, ephi; "
        "print(json.dumps({'version': ephi.__version__, 'identity': ephi.application_identity()}, sort_keys=True))"
    )
    environment = dict(os.environ)
    source = str(root / "src")
    environment["PYTHONPATH"] = source + (":" + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    completed = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise BaselineError("IMPORT_FAILED", completed.stderr.strip() or "canonical package import failed")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BaselineError("IMPORT_IDENTITY_INVALID", "canonical package import did not emit JSON identity") from exc
    if result.get("version") != EXPECTED["version"] or result.get("identity", {}).get("application", {}).get("implementation") != "canonical-repository":
        raise BaselineError("IMPORT_IDENTITY_MISMATCH", "canonical package import identity is not expected")
    return {"status": "PASS", **result}


def _summary(output: str) -> dict[str, Any]:
    match = list(re.finditer(r"Ran (\d+) tests?", output))
    if not match:
        return {"parsed": False, "passed": None, "skipped": None, "failed": None, "errors": None}
    total = int(match[-1].group(1))
    skipped_match = list(re.finditer(r"skipped=(\d+)", output))
    failure_match = list(re.finditer(r"failures=(\d+)", output))
    error_match = list(re.finditer(r"errors=(\d+)", output))
    skipped = int(skipped_match[-1].group(1)) if skipped_match else 0
    failed = int(failure_match[-1].group(1)) if failure_match else 0
    errors = int(error_match[-1].group(1)) if error_match else 0
    return {"parsed": True, "passed": total - skipped - failed - errors, "skipped": skipped, "failed": failed, "errors": errors}


def run_tests(root: Path = ROOT) -> dict[str, Any]:
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False, timeout=TEST_TIMEOUT_SECONDS)
    combined = completed.stdout + "\n" + completed.stderr
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "command": command,
        "returncode": completed.returncode,
        "output_sha256": _sha256_bytes(combined.encode()),
        **_summary(combined),
    }


def _new_result(root: Path, spec_path: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "change": "CHG-111",
        "wave": "W0",
        "status": "BASELINE_NOT_RUN",
        "reason": None,
        "scope": "canonical-repository-only",
        "specification": {"path": str(spec_path), "status": "NOT_READ"},
        "git": {"status": "NOT_RUN"},
        "python": {"status": "NOT_RUN"},
        "package": {"status": "NOT_RUN"},
        "import": {"status": "NOT_RUN"},
        "tests": {"status": "NOT_RUN"},
        "historical_test_result": {"status": "REFERENCE_ONLY", "passed": 275, "skipped": 1, "used_by_baseline": False},
        "excluded_inputs": ["chat attachments", "source-preflight JSON", "staged-source directories", "historical source archive"],
    }


def run_baseline(root: Path = ROOT, spec_path: Path = SPEC_PATH, *, execute_tests: bool = True) -> dict[str, Any]:
    root = Path(root).resolve()
    spec_path = Path(spec_path).resolve()
    result = _new_result(root, spec_path)
    try:
        spec = load_spec(spec_path)
        result["specification"] = {"path": str(spec_path), "status": "PASS", "sha256": _sha256_bytes(spec_path.read_bytes())}
        result["git"] = {"status": "PASS", **collect_git_identity(root)}
        result["python"] = {"status": "PASS", **validate_python_version()}
        result["package"] = validate_project_metadata(root, spec)
        result["package_integrity"] = validate_manifest(root)
        result["import"] = _import_identity(root)
        if execute_tests:
            result["tests"] = run_tests(root)
        else:
            result["tests"] = {"status": "NOT_RUN", "reason": "execution disabled by caller"}
    except (BaselineError, OSError, subprocess.SubprocessError, ValueError) as exc:
        result["status"] = "BASELINE_BLOCKED"
        result["reason"] = exc.code if isinstance(exc, BaselineError) else "BASELINE_ERROR"
        result["message"] = str(exc)
        return result
    result["status"] = "BASELINE_PASS" if result["tests"]["status"] in {"PASS", "NOT_RUN"} else "BASELINE_FAILED"
    result["reason"] = "CANONICAL_REPOSITORY_CHECKS_PASS" if result["status"] == "BASELINE_PASS" else "CANONICAL_TESTS_FAILED"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--no-tests", action="store_true", help="validate identity only")
    args = parser.parse_args(argv)
    result = run_baseline(args.root, args.spec, execute_tests=not args.no_tests)
    print(json.dumps(result, indent=2, sort_keys=True))
    return {"BASELINE_PASS": 0, "BASELINE_FAILED": 5, "BASELINE_BLOCKED": 4}.get(result["status"], 4)


if __name__ == "__main__":
    raise SystemExit(main())
