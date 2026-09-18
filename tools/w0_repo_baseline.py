#!/usr/bin/env python3
"""Run the canonical Git-native EPHI W0 repository baseline.

This tool intentionally uses only the checked-out repository, its Git state,
the declared package metadata, and deterministic local commands.  The
historical source-archive utilities are not part of this execution path.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "artifacts" / "w0-repo-baseline.json"
TEST_TIMEOUT_SECONDS = 300
EXPECTED_DEPENDENCIES = [
    "nicegui-base @ git+https://github.com/kimhw8084/nicegui-base.git@000298562d6bcbf6df304edbd41b98b30fe4bfcf",
    "nicegui==3.15.0",
]
EXPECTED_PACKAGE_FILES = (
    "src/ephi/__init__.py",
    "src/ephi/__main__.py",
    "src/ephi/application.py",
    "src/ephi/config.py",
)
FORBIDDEN_APPLICATION_IMPORTS = (
    "nicegui.ui",
    "nicegui_base.integrations.nicegui_",
)


class RepoBaselineError(ValueError):
    """A repository identity or integrity condition that must fail closed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise RepoBaselineError("JSON_DUPLICATE_KEY", f"duplicate JSON key in {path.name}: {key}")
                result[key] = value
            return result

        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except FileNotFoundError as exc:
        raise RepoBaselineError("SPEC_MISSING", f"required repository specification is missing: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepoBaselineError("SPEC_INVALID", f"repository specification is not valid JSON: {exc}") from exc


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepoBaselineError("PACKAGE_DEFINITION_MISSING", f"package definition is missing: {path}") from exc
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RepoBaselineError("PACKAGE_DEFINITION_INVALID", f"package definition is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise RepoBaselineError("PACKAGE_DEFINITION_INVALID", "package definition must be a TOML table")
    return value


def _safe_relative(root: Path, value: str, field: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise RepoBaselineError("PATH_IDENTITY_MISMATCH", f"{field} must be repository-relative")
    path = root.joinpath(*relative.parts)
    if not path.resolve(strict=False).is_relative_to(root.resolve()):
        raise RepoBaselineError("PATH_IDENTITY_MISMATCH", f"{field} escapes the repository")
    return path


def _digest(path: Path) -> dict[str, int | str]:
    raw = path.read_bytes()
    return {"bytes": len(raw), "sha256": _sha256_bytes(raw)}


def _package_files(root: Path) -> list[Path]:
    ignored_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "artifacts", "build"}
    ignored_files = {".DS_Store"}

    def walk(directory: Path):
        for path in sorted(directory.iterdir()):
            if path.name in ignored_dirs or path.name in ignored_files or path.name.endswith(".egg-info"):
                continue
            if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise RepoBaselineError("PACKAGE_SYMLINK", f"symlink is not a package file: {path.relative_to(root)}")
            if path.is_dir():
                yield from walk(path)
            elif path.is_file():
                yield path

    return sorted(walk(root))


def validate_manifest_integrity(root: Path) -> dict[str, Any]:
    """Validate current manifest membership and hashes without historical parsing."""

    manifest = _read_json(root / "manifest.json")
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("files"), list):
        raise RepoBaselineError("MANIFEST_INVALID", "manifest files must be an array")
    declared = [item.get("path") for item in manifest["files"] if isinstance(item, Mapping)]
    if len(declared) != len(manifest["files"]) or declared != sorted(set(declared)):
        raise RepoBaselineError("MANIFEST_INVALID", "manifest paths must be unique and sorted")
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in _package_files(root)
        if path != root / "manifest.json"
    }
    if set(declared) != actual_paths:
        raise RepoBaselineError("MANIFEST_MEMBERSHIP_MISMATCH", "manifest membership differs from the current package")
    for item in manifest["files"]:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise RepoBaselineError("MANIFEST_INVALID", "manifest entry is incomplete")
        path = _safe_relative(root, item["path"], "manifest path")
        if not path.is_file() or path.is_symlink():
            raise RepoBaselineError("MANIFEST_FILE_MISSING", f"manifest file is missing: {item['path']}")
        observed = _digest(path)
        if observed != {key: item.get(key) for key in ("bytes", "sha256")}:
            raise RepoBaselineError("MANIFEST_HASH_MISMATCH", f"manifest hash/size mismatch: {item['path']}")
    return {"status": "PASS", "files": len(declared)}


def _source_version(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise RepoBaselineError("PACKAGE_SOURCE_INVALID", f"cannot parse package identity: {exc}") from exc
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__" and isinstance(node.value, ast.Constant):
                    if isinstance(node.value.value, str):
                        return node.value.value
    raise RepoBaselineError("PACKAGE_IDENTITY_MISSING", "src/ephi/__init__.py must declare __version__")


def validate_repository_contract(root: Path = REPOSITORY_ROOT) -> dict[str, Any]:
    """Validate declared package, dependency, source and authority identity."""

    root = Path(root).resolve()
    spec = _read_json(root / "environment" / "w0_repo_baseline.json")
    if not isinstance(spec, Mapping) or spec.get("schema_version") != 1 or spec.get("change") != "CHG-111" or spec.get("wave") != "W0":
        raise RepoBaselineError("SPEC_IDENTITY_MISMATCH", "W0 repository specification is not CHG-111/W0 schema 1")
    if spec.get("mode") != "CANONICAL_REPOSITORY":
        raise RepoBaselineError("SPEC_IDENTITY_MISMATCH", "W0 repository specification must be canonical-repository mode")

    project = _read_toml(root / "pyproject.toml").get("project")
    if not isinstance(project, Mapping):
        raise RepoBaselineError("PACKAGE_DEFINITION_INVALID", "pyproject.toml lacks a project table")
    expected_project = {
        "name": "ephi",
        "version": "0.1.0",
        "requires-python": ">=3.11,<3.14",
    }
    for field, expected in expected_project.items():
        if project.get(field) != expected:
            raise RepoBaselineError("PACKAGE_IDENTITY_MISMATCH", f"project {field} does not match the canonical identity")
    dependencies = project.get("dependencies")
    if dependencies != EXPECTED_DEPENDENCIES:
        raise RepoBaselineError("DEPENDENCY_IDENTITY_MISMATCH", "declared framework dependencies do not match the exact W0 pins")
    scripts = project.get("scripts")
    if not isinstance(scripts, Mapping) or scripts.get("ephi") != "ephi.__main__:main":
        raise RepoBaselineError("ENTRYPOINT_IDENTITY_MISMATCH", "the ephi console entrypoint is not canonical")
    build_system = _read_toml(root / "pyproject.toml").get("build-system")
    if not isinstance(build_system, Mapping) or build_system.get("build-backend") != "setuptools.build_meta":
        raise RepoBaselineError("PACKAGE_DEFINITION_INVALID", "the package must use a standard setuptools build backend")

    package_spec = spec.get("package")
    if not isinstance(package_spec, Mapping) or package_spec.get("source_root") != "src/ephi":
        raise RepoBaselineError("PACKAGE_IDENTITY_MISMATCH", "canonical source root is not src/ephi")
    for relative in EXPECTED_PACKAGE_FILES:
        path = _safe_relative(root, relative, "canonical package file")
        if path.is_symlink() or not path.is_file():
            raise RepoBaselineError("PACKAGE_FILE_MISSING", f"canonical package file is missing: {relative}")
    if _source_version(root / "src/ephi/__init__.py") != "0.1.0":
        raise RepoBaselineError("PACKAGE_IDENTITY_MISMATCH", "package source version differs from pyproject identity")
    for path in sorted((root / "src/ephi").rglob("*.py")):
        content = path.read_text(encoding="utf-8")
        if any(token in content for token in FORBIDDEN_APPLICATION_IMPORTS):
            raise RepoBaselineError("FRAMEWORK_AUTHORITY_VIOLATION", f"private/direct framework authority in {path.relative_to(root)}")
    return {
        "status": "PASS",
        "project": {field: project[field] for field in (*expected_project, "dependencies")},
        "entrypoint": "ephi.__main__:main",
        "source_root": "src/ephi",
        "framework": {
            "distribution": "nicegui-base",
            "version": "3.0.0a8",
            "commit": "000298562d6bcbf6df304edbd41b98b30fe4bfcf",
            "nicegui": "3.15.0",
        },
    }


def _git(root: Path, arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepoBaselineError("GIT_UNAVAILABLE", f"Git identity command failed: {exc}") from exc
    if completed.returncode != 0:
        raise RepoBaselineError("GIT_IDENTITY_INVALID", f"Git identity command failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def git_identity(root: Path = REPOSITORY_ROOT) -> dict[str, Any]:
    """Return immutable HEAD/tree facts plus the current worktree state."""

    head = _git(root, ["rev-parse", "HEAD"])
    tree = _git(root, ["rev-parse", "HEAD^{tree}"])
    if not re.fullmatch(r"[0-9a-f]{40}", head) or not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise RepoBaselineError("GIT_IDENTITY_INVALID", "Git HEAD/tree identities are not SHA-1 object IDs")
    status = _git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    return {
        "head": head,
        "head_tree": tree,
        "worktree": "CLEAN" if not status else "DIRTY",
        "status_lines": status.splitlines(),
        "status_sha256": _sha256_bytes(status.encode("utf-8")),
    }


def _python_command(root: Path, arguments: list[str]) -> dict[str, Any]:
    environment = {
        "PYTHONPATH": str(root / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    import os

    merged = os.environ.copy()
    merged.update(environment)
    command = [sys.executable, *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=merged,
            capture_output=True,
            check=False,
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        return {
            "status": "FAIL",
            "command": command,
            "returncode": None,
            "timeout": True,
            "stdout": {"bytes": len(stdout), "sha256": _sha256_bytes(stdout)},
            "stderr": {"bytes": len(stderr), "sha256": _sha256_bytes(stderr)},
        }
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "command": command,
        "returncode": completed.returncode,
        "stdout": {"bytes": len(completed.stdout), "sha256": _sha256_bytes(completed.stdout)},
        "stderr": {"bytes": len(completed.stderr), "sha256": _sha256_bytes(completed.stderr)},
    }


def _entrypoint_identity(root: Path) -> dict[str, Any]:
    command = [
        "-c",
        "import ephi; print(ephi.APPLICATION_ID + ':' + ephi.__version__)",
    ]
    result = _python_command(root, command)
    if result["status"] != "PASS":
        raise RepoBaselineError("IMPORT_FAILED", "canonical ephi package import failed")
    expected = b"ephi:0.1.0\n"
    # The command result stores hashes only; run the tiny import again to keep
    # the recorded baseline free of environment-specific stdout.
    import os

    merged = os.environ.copy()
    merged.update({"PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
    completed = subprocess.run(
        [sys.executable, *command], cwd=root, env=merged, capture_output=True, check=False, timeout=30
    )
    if completed.stdout != expected:
        raise RepoBaselineError("IMPORT_IDENTITY_MISMATCH", "canonical package import identity differs")
    return result


def _deterministic_entry_check(root: Path) -> dict[str, Any]:
    first = _python_command(root, ["-m", "ephi", "--self-check"])
    second = _python_command(root, ["-m", "ephi", "--self-check"])
    if first["status"] != "PASS" or second["status"] != "PASS":
        raise RepoBaselineError("ENTRYPOINT_FAILED", "canonical self-check entrypoint failed")
    if first["stdout"] != second["stdout"]:
        raise RepoBaselineError("ENTRYPOINT_NONDETERMINISTIC", "canonical self-check output is not deterministic")
    return {"status": "PASS", "first": first, "second": second}


def _canonical_test_run(root: Path) -> dict[str, Any]:
    result = _python_command(root, ["-m", "unittest", "discover", "-s", "tests", "-v"])
    result["justification"] = "canonical repository tests; no owner-supplied artifact"
    return result


def _new_result(root: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "change": "CHG-111",
        "wave": "W0",
        "mode": "CANONICAL_REPOSITORY",
        "status": "BASELINE_NOT_RUN",
        "reason": None,
        "message": None,
        "git": None,
        "contract": None,
        "manifest": None,
        "package_import": None,
        "entrypoint": None,
        "canonical_tests": None,
        "historical_test_result": {
            "status": "REFERENCE_ONLY",
            "passed": 275,
            "skipped": 1,
            "source": "evidence/pytest.log",
            "current_execution": False,
        },
    }


def _blocked(result: dict[str, Any], error: RepoBaselineError) -> dict[str, Any]:
    result["status"] = "BASELINE_BLOCKED"
    result["reason"] = error.code
    result["message"] = str(error)
    return result


def run_baseline(
    root: Path = REPOSITORY_ROOT,
    output_path: Path | None = None,
    *,
    execute_tests: bool = True,
) -> dict[str, Any]:
    """Run the canonical baseline; identity failures stop before tests."""

    root = Path(root).resolve()
    result = _new_result(root)
    try:
        result["git"] = git_identity(root)
        result["contract"] = validate_repository_contract(root)
        result["manifest"] = validate_manifest_integrity(root)
        result["package_import"] = _entrypoint_identity(root)
        result["entrypoint"] = _deterministic_entry_check(root)
    except RepoBaselineError as exc:
        result = _blocked(result, exc)
        if output_path is not None:
            write_result(output_path, result)
        return result

    if execute_tests:
        result["canonical_tests"] = _canonical_test_run(root)
        if result["canonical_tests"]["status"] != "PASS":
            result["status"] = "FAIL"
            result["reason"] = "CANONICAL_TESTS_FAILED"
            result["message"] = "canonical repository tests failed"
            if output_path is not None:
                write_result(output_path, result)
            return result
    result["status"] = "PASS"
    result["reason"] = "CANONICAL_REPOSITORY_BASELINE_PASS"
    result["message"] = "Git/package/import/entrypoint identity and canonical repository tests passed"
    if output_path is not None:
        write_result(output_path, result)
    return result


def write_result(path: Path, result: Mapping[str, Any]) -> None:
    path = Path(path).expanduser()
    evidence_root = REPOSITORY_ROOT / "evidence"
    if path.resolve(strict=False).is_relative_to(evidence_root.resolve()):
        raise RepoBaselineError("PROTECTED_PATH", "baseline output must not be written inside preserved evidence")
    if path.is_symlink():
        raise RepoBaselineError("OUTPUT_SYMLINK", "baseline output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT, help="canonical repository root")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="ignored machine-readable result")
    args = parser.parse_args(argv)
    result = run_baseline(args.root, args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return {"PASS": 0, "FAIL": 5, "BASELINE_BLOCKED": 4}.get(result["status"], 4)


if __name__ == "__main__":
    raise SystemExit(main())
