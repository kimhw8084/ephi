#!/usr/bin/env python3
"""LEGACY OPTIONAL: execute a verified historical-source baseline.

The canonical W0 path is :mod:`tools.w0_repo_baseline`. This compatibility
harness remains only for historical source-preflight fixtures and is not an
application, package, or implementation gate.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Mapping

# Keep both documented direct execution and ``python -m tools.w0_baseline`` usable.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.source_preflight import (
    EXPECTED_ARCHIVE_FILENAME,
    EXPECTED_ARCHIVE_SHA256,
    EXPECTED_SOURCE_ROOT,
    HISTORICAL_TEST_RESULT,
    discover_baseline,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = REPOSITORY_ROOT / "evidence"
DEFAULT_PREFLIGHT = REPOSITORY_ROOT / "artifacts" / "source-preflight.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "artifacts" / "w0-baseline.json"
TEST_TIMEOUT_SECONDS = 300


class BaselineError(ValueError):
    """A source or execution condition that must stop W0."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _new_result(preflight_path: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "BASELINE_NOT_RUN",
        "reason": None,
        "message": None,
        "preflight": {
            "path": str(preflight_path),
            "status": "NOT_READ",
            "record_sha256": None,
        },
        "source_identity": {
            "required_filename": EXPECTED_ARCHIVE_FILENAME,
            "required_sha256": EXPECTED_ARCHIVE_SHA256,
            "required_source_root": EXPECTED_SOURCE_ROOT,
            "verified_by": "tools/source_preflight.py",
            "archive_bytes_reverified_by_harness": False,
        },
        "inventory": {"status": "NOT_RUN"},
        "historical_test_result": deepcopy(HISTORICAL_TEST_RESULT),
        "current_run": {
            "status": "NOT_RUN",
            "tests_executed": False,
            "runner": None,
            "commands": [],
            "passed": None,
            "skipped": None,
            "failed": None,
            "errors": None,
        },
    }


def _blocked(result: dict[str, Any], error: BaselineError) -> dict[str, Any]:
    result["status"] = "BASELINE_BLOCKED"
    result["reason"] = error.code
    result["message"] = str(error)
    return result


def _resolved(path_value: Any, field: str) -> Path:
    if not isinstance(path_value, str) or not path_value:
        raise BaselineError("SOURCE_IDENTITY_MISMATCH", f"{field} must be a non-empty path")
    return Path(path_value).expanduser()


def validate_preflight_record(record: Mapping[str, Any]) -> Path:
    """Validate the integrated preflight contract and return its staged source root."""

    if record.get("status") != "SOURCE_STAGED":
        raise BaselineError(
            "PREFLIGHT_NOT_SOURCE_STAGED",
            "source preflight must report SOURCE_STAGED before baseline execution",
        )

    source = record.get("source")
    staging = record.get("staging")
    baseline = record.get("baseline")
    if not isinstance(source, Mapping) or not isinstance(staging, Mapping) or not isinstance(baseline, Mapping):
        raise BaselineError("PREFLIGHT_SCHEMA_INVALID", "SOURCE_STAGED preflight is missing source, staging or baseline data")

    exact_fields = (
        (source, "required_filename", EXPECTED_ARCHIVE_FILENAME),
        (source, "required_sha256", EXPECTED_ARCHIVE_SHA256),
        (source, "required_source_root", EXPECTED_SOURCE_ROOT),
        (source, "archive_sha256", EXPECTED_ARCHIVE_SHA256),
    )
    if any(values.get(field) != expected for values, field, expected in exact_fields):
        raise BaselineError("SOURCE_IDENTITY_MISMATCH", "preflight source identity does not match the established EPHI contract")
    archive_path = _resolved(source.get("archive_path"), "source.archive_path")
    if archive_path.name != EXPECTED_ARCHIVE_FILENAME:
        raise BaselineError("SOURCE_IDENTITY_MISMATCH", "preflight archive filename does not match the established EPHI contract")
    if source.get("archive_present") is not True or source.get("verified") is not True:
        raise BaselineError("SOURCE_NOT_VERIFIED", "preflight does not report verified archive bytes")

    if staging.get("status") != "STAGED":
        raise BaselineError("STAGING_NOT_STAGED", "preflight staging status must be STAGED")
    reported_root = _resolved(staging.get("source_root"), "staging.source_root")
    requested_stage = _resolved(staging.get("requested_path"), "staging.requested_path")
    expected_root = requested_stage / EXPECTED_SOURCE_ROOT
    if reported_root.resolve(strict=False) != expected_root.resolve(strict=False):
        raise BaselineError("SOURCE_ROOT_IDENTITY_MISMATCH", "reported source root is not the requested staged release root")
    baseline_root = _resolved(baseline.get("source_root"), "baseline.source_root")
    if baseline_root.resolve(strict=False) != reported_root.resolve(strict=False):
        raise BaselineError("SOURCE_ROOT_IDENTITY_MISMATCH", "baseline source root differs from staged source root")
    if reported_root.name != EXPECTED_SOURCE_ROOT:
        raise BaselineError("SOURCE_ROOT_IDENTITY_MISMATCH", "source root basename does not match the established EPHI contract")
    if reported_root.is_symlink() or not reported_root.exists() or not reported_root.is_dir():
        raise BaselineError("SOURCE_ROOT_MISSING", "reported staged source root does not exist as a directory")
    try:
        resolved_root = reported_root.resolve(strict=True)
    except OSError as exc:
        raise BaselineError("SOURCE_ROOT_MISSING", "reported staged source root cannot be resolved") from exc
    if resolved_root.is_relative_to(EVIDENCE_ROOT.resolve()):
        raise BaselineError("PROTECTED_SOURCE_ROOT", "staged source root must not be inside preserved evidence")
    return resolved_root


def _dependency_hashes(source_root: Path, dependency_files: list[str]) -> list[dict[str, Any]]:
    values = []
    for relative in dependency_files:
        path = source_root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file() or path.is_symlink():
            raise BaselineError("INVENTORY_CHANGED", f"discovered dependency file is not a regular file: {relative}")
        content = path.read_bytes()
        values.append({
            "path": relative,
            "bytes": len(content),
            "sha256": _sha256_bytes(content),
        })
    return values


def inventory_source(source_root: Path, preflight_baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Capture current runtime and staged-source facts without importing application code."""

    observed = discover_baseline(source_root)
    dependency_files = list(observed["dependency_files"])
    return {
        "status": "INVENTORIED",
        "source_root": source_root.as_posix(),
        "python_runtime": observed["python_runtime"],
        "python_files": observed["python_files"],
        "python_lines": observed["python_lines"],
        "test_files": observed["test_files"],
        "test_roots": observed["test_roots"],
        "dependency_files": dependency_files,
        "dependency_file_hashes": _dependency_hashes(source_root, dependency_files),
        "installed_distributions": observed["installed_distributions"],
        "preflight_baseline": deepcopy(dict(preflight_baseline)),
        "observed_baseline": observed,
    }


def _summary(runner: str, output: bytes) -> dict[str, Any]:
    text = output.decode("utf-8", errors="replace")
    values: dict[str, Any] = {
        "summary_parsed": False,
        "passed": None,
        "skipped": None,
        "failed": None,
        "errors": None,
    }
    if runner == "unittest":
        ran = list(re.finditer(r"Ran (\d+) tests?", text))
        if not ran:
            return values
        values["summary_parsed"] = True
        total = int(ran[-1].group(1))
        skipped = re.findall(r"skipped=(\d+)", text)
        failures = re.findall(r"failures=(\d+)", text)
        errors = re.findall(r"errors=(\d+)", text)
        values["skipped"] = int(skipped[-1]) if skipped else 0
        values["failed"] = int(failures[-1]) if failures else 0
        values["errors"] = int(errors[-1]) if errors else 0
        values["passed"] = total - values["skipped"] - values["failed"] - values["errors"]
        return values
    if runner == "pytest":
        labels = ("passed", "skipped", "failed", "error", "xfailed", "xpassed")
        matches = {label: list(re.finditer(rf"\b(\d+) {label}\b", text)) for label in labels}
        if not any(matches.values()):
            return values
        values["summary_parsed"] = True
        values["passed"] = int(matches["passed"][-1].group(1)) if matches["passed"] else 0
        values["skipped"] = int(matches["skipped"][-1].group(1)) if matches["skipped"] else 0
        values["failed"] = int(matches["failed"][-1].group(1)) if matches["failed"] else 0
        values["errors"] = int(matches["error"][-1].group(1)) if matches["error"] else 0
    return values


def _test_commands(source_root: Path, inventory: Mapping[str, Any]) -> tuple[str, list[tuple[list[str], str]]]:
    observed = inventory["observed_baseline"]
    runner = observed["test_execution"]["runner_discovered"]
    if runner not in {"pytest", "unittest"}:
        raise BaselineError("UNSUPPORTED_TEST_RUNNER", f"staged source runner is not supported: {runner!r}")
    if observed["test_files"] < 1:
        raise BaselineError("NO_TEST_FILES", "staged source contains no discovered test files")
    if runner == "pytest" and importlib.util.find_spec("pytest") is None:
        raise BaselineError("TEST_RUNNER_UNAVAILABLE", "staged source selects pytest but pytest is unavailable")

    roots = list(observed["test_roots"])
    if not roots:
        roots = ["."]
    commands = []
    for relative in roots:
        test_root = source_root.joinpath(*PurePosixPath(relative).parts)
        if relative != "." and (not test_root.is_dir() or test_root.is_symlink()):
            raise BaselineError("INVENTORY_CHANGED", f"discovered test root is not a directory: {relative}")
        if runner == "pytest":
            command = [sys.executable, "-m", "pytest", relative, "-q"]
        else:
            command = [sys.executable, "-m", "unittest", "discover", "-s", relative, "-v"]
        justification = f"preflight-discovered runner={runner!r}, test_root={relative!r}"
        commands.append((command, justification))
    return runner, commands


def _execute(source_root: Path, runner: str, command: list[str], justification: str) -> dict[str, Any]:
    encoded_command = shlex.join(command)
    try:
        completed = subprocess.run(
            command,
            cwd=source_root,
            capture_output=True,
            check=False,
            timeout=TEST_TIMEOUT_SECONDS,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        parsed = _summary(runner, stdout + b"\n" + stderr)
        status = "PASS" if completed.returncode == 0 else "FAIL"
        return {
            "status": status,
            "justification": justification,
            "command": encoded_command,
            "argv": command,
            "cwd": source_root.as_posix(),
            "returncode": completed.returncode,
            "timeout_seconds": TEST_TIMEOUT_SECONDS,
            **parsed,
            "stdout": {"bytes": len(stdout), "sha256": _sha256_bytes(stdout)},
            "stderr": {"bytes": len(stderr), "sha256": _sha256_bytes(stderr)},
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        return {
            "status": "FAIL",
            "justification": justification,
            "command": encoded_command,
            "argv": command,
            "cwd": source_root.as_posix(),
            "returncode": None,
            "timeout_seconds": TEST_TIMEOUT_SECONDS,
            "summary_parsed": False,
            "passed": None,
            "skipped": None,
            "failed": None,
            "errors": None,
            "timeout": True,
            "stdout": {"bytes": len(stdout), "sha256": _sha256_bytes(stdout)},
            "stderr": {"bytes": len(stderr), "sha256": _sha256_bytes(stderr)},
        }


def _total(results: list[Mapping[str, Any]], field: str) -> int | None:
    values = [item[field] for item in results]
    if any(value is None for value in values):
        return None
    return sum(values)


def run_baseline(preflight_path: Path, output_path: Path | None = None) -> dict[str, Any]:
    preflight_path = Path(preflight_path).expanduser()
    result = _new_result(preflight_path)
    try:
        raw = preflight_path.read_bytes()
    except FileNotFoundError:
        result = _blocked(result, BaselineError("PREFLIGHT_MISSING", "source-preflight result is missing"))
        if output_path is not None:
            write_result(output_path, result)
        return result
    except OSError as exc:
        result = _blocked(result, BaselineError("PREFLIGHT_READ_ERROR", str(exc)))
        if output_path is not None:
            write_result(output_path, result)
        return result

    result["preflight"]["record_sha256"] = _sha256_bytes(raw)
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        result = _blocked(result, BaselineError("PREFLIGHT_INVALID", f"source-preflight result is not valid UTF-8 JSON: {exc}"))
        if output_path is not None:
            write_result(output_path, result)
        return result
    if not isinstance(record, Mapping):
        result = _blocked(result, BaselineError("PREFLIGHT_SCHEMA_INVALID", "source-preflight result must be a JSON object"))
        if output_path is not None:
            write_result(output_path, result)
        return result
    result["preflight"]["status"] = record.get("status", "MISSING")
    result["source_identity"]["preflight_reported"] = {
        "filename": record.get("source", {}).get("required_filename") if isinstance(record.get("source"), Mapping) else None,
        "sha256": record.get("source", {}).get("archive_sha256") if isinstance(record.get("source"), Mapping) else None,
        "source_root": record.get("source", {}).get("required_source_root") if isinstance(record.get("source"), Mapping) else None,
    }
    try:
        source_root = validate_preflight_record(record)
        result["inventory"] = inventory_source(source_root, record["baseline"])
        runner, commands = _test_commands(source_root, result["inventory"])
    except BaselineError as exc:
        result = _blocked(result, exc)
        if output_path is not None:
            write_result(output_path, result)
        return result
    except (OSError, KeyError, TypeError, ValueError) as exc:
        result = _blocked(result, BaselineError("INVENTORY_ERROR", str(exc)))
        if output_path is not None:
            write_result(output_path, result)
        return result

    command_results = [_execute(source_root, runner, command, justification) for command, justification in commands]
    passed = _total(command_results, "passed")
    skipped = _total(command_results, "skipped")
    failed = _total(command_results, "failed")
    errors = _total(command_results, "errors")
    all_passed = all(item["status"] == "PASS" for item in command_results)
    result["current_run"] = {
        "status": "PASS" if all_passed else "FAIL",
        "tests_executed": True,
        "runner": runner,
        "commands": command_results,
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
    }
    result["status"] = "BASELINE_PASS" if all_passed else "BASELINE_FAILED"
    result["reason"] = "TEST_COMMANDS_PASS" if all_passed else "TEST_COMMAND_FAILED"
    result["message"] = "staged source test commands completed" if all_passed else "one or more staged source test commands failed"
    if output_path is not None:
        write_result(output_path, result)
    return result


def write_result(path: Path, result: Mapping[str, Any]) -> None:
    path = Path(path).expanduser()
    if path.resolve(strict=False).is_relative_to(EVIDENCE_ROOT.resolve()):
        raise BaselineError("PROTECTED_PATH", "baseline output must not be written inside preserved evidence")
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
    parser.add_argument("--preflight", type=Path, default=DEFAULT_PREFLIGHT, help="integrated source-preflight JSON result")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="ignored machine-readable baseline result")
    args = parser.parse_args(argv)
    try:
        result = run_baseline(args.preflight, args.output)
    except BaselineError as exc:
        result = _blocked(_new_result(args.preflight), exc)
        try:
            write_result(args.output, result)
        except BaselineError:
            pass
    print(json.dumps(result, indent=2, sort_keys=True))
    return {"BASELINE_PASS": 0, "BASELINE_FAILED": 5, "BASELINE_BLOCKED": 4}.get(result["status"], 4)


if __name__ == "__main__":
    raise SystemExit(main())
