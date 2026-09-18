#!/usr/bin/env python3
"""Run CHG-109 integrity checks against the canonical repository boundary.

The default target is ``src/ephi``. The historical staged-source runner is
available only through the explicit ``--legacy-source`` compatibility mode.
"""

from __future__ import annotations

import argparse
import ast
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Mapping

# Keep direct execution and ``python -m tools.w0_integrity_regressions`` usable.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ephi.value import EventPeriod, InMemoryValueRepository, ValueEntry, ValueService, decimal_json_default

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = REPOSITORY_ROOT / "evidence"
CONTRACT_PATH = EVIDENCE_ROOT / "review" / "w0_integrity_regression_contract.json"
DEFAULT_PREFLIGHT = REPOSITORY_ROOT / "artifacts" / "source-preflight.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "artifacts" / "w0-integrity-regressions.json"
DEFAULT_RUNTIME_ROOT = REPOSITORY_ROOT / "artifacts" / "w0-integrity-regressions"
PROBE_TIMEOUT_SECONDS = 300
EXPECTED_FINDING_IDS = ("F03", "F02", "F04", "F05")
F02_RESULTS = {
    "SOURCE_ONLY_INSUFFICIENT",
    "CHECKPOINT_RESTORE_PASS",
    "CHECKPOINT_RESTORE_FAIL",
    "NOT_RUN",
}


class IntegrityError(ValueError):
    """A contract, evidence or source condition that must fail closed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _legacy_source_constants() -> tuple[str, str, str]:
    """Load historical fixture constants only when explicit legacy mode is used."""

    from tools.source_preflight import (
        EXPECTED_ARCHIVE_FILENAME,
        EXPECTED_ARCHIVE_SHA256,
        EXPECTED_SOURCE_ROOT,
    )

    return EXPECTED_ARCHIVE_FILENAME, EXPECTED_ARCHIVE_SHA256, EXPECTED_SOURCE_ROOT


def sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _strict_json_bytes(raw: bytes, label: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise IntegrityError("JSON_DUPLICATE_KEY", f"duplicate JSON key in {label}: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise IntegrityError("JSON_NONFINITE_NUMBER", f"non-finite JSON number in {label}: {value}")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except IntegrityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("JSON_INVALID", f"invalid JSON in {label}: {exc}") from exc


def _read_json(path: Path, label: str) -> Any:
    try:
        return _strict_json_bytes(path.read_bytes(), label)
    except FileNotFoundError as exc:
        raise IntegrityError("FILE_MISSING", f"required {label} is missing: {path}") from exc
    except OSError as exc:
        raise IntegrityError("FILE_READ_ERROR", f"cannot read {label}: {exc}") from exc


def _repo_relative(path_value: Any, field: str) -> Path:
    if not isinstance(path_value, str) or not path_value:
        raise IntegrityError("CONTRACT_INVALID", f"{field} must be a non-empty repository-relative path")
    relative = PurePosixPath(path_value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in path_value:
        raise IntegrityError("CONTRACT_INVALID", f"{field} must stay inside the repository")
    path = REPOSITORY_ROOT.joinpath(*relative.parts)
    if not path.resolve(strict=False).is_relative_to(REPOSITORY_ROOT.resolve()):
        raise IntegrityError("CONTRACT_INVALID", f"{field} escapes the repository")
    return path


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntegrityError("CONTRACT_INVALID", f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise IntegrityError("CONTRACT_INVALID", f"{field} must be an array")
    return value


def validate_contract(contract: Mapping[str, Any], *, source_bound: bool = True) -> None:
    """Validate CHG-109 structure and, optionally, its legacy source binding."""

    if contract.get("schema_version") != 1 or contract.get("change") != "CHG-109" or contract.get("wave") != "W0":
        raise IntegrityError("CONTRACT_INVALID", "contract must be schema 1 for CHG-109/W0")

    if source_bound:
        expected_filename, expected_sha256, expected_source_root = _legacy_source_constants()
        source = _mapping(contract.get("source_identity"), "source_identity")
        expected_identity = {
            "required_filename": expected_filename,
            "required_sha256": expected_sha256,
            "required_source_root": expected_source_root,
        }
        for field, expected in expected_identity.items():
            if source.get(field) != expected:
                raise IntegrityError("SOURCE_IDENTITY_MISMATCH", f"contract source identity does not match {field}")

    historical = _mapping(contract.get("historical_evidence"), "historical_evidence")
    if historical.get("status") != "REFERENCE_ONLY" or historical.get("immutable") is not True:
        raise IntegrityError("CONTRACT_INVALID", "historical evidence must remain immutable REFERENCE_ONLY")
    files = _list(historical.get("files"), "historical_evidence.files")
    expected_files = {
        "evidence/behavior_probes.py",
        "evidence/behavior_probes.json",
        "evidence/behavior_probes.log",
        "evidence/source_excerpts.md",
        "evidence/source_inventory.json",
    }
    actual_files = {item.get("path") for item in files if isinstance(item, Mapping)}
    if actual_files != expected_files or len(files) != len(expected_files):
        raise IntegrityError("CONTRACT_INVALID", "historical evidence file set is incomplete or duplicated")
    for item in files:
        item = _mapping(item, "historical_evidence.files[]")
        if not isinstance(item.get("bytes"), int) or not isinstance(item.get("sha256"), str):
            raise IntegrityError("CONTRACT_INVALID", "historical evidence file identity is incomplete")
        if len(item["sha256"]) != 64:
            raise IntegrityError("CONTRACT_INVALID", "historical evidence hash must be SHA-256")
        _repo_relative(item.get("path"), "historical_evidence.files[].path")

    observations = _list(historical.get("observations"), "historical_evidence.observations")
    if {item.get("id") for item in observations if isinstance(item, Mapping)} != set(EXPECTED_FINDING_IDS):
        raise IntegrityError("CONTRACT_INVALID", "historical observations must cover F02/F03/F04/F05 exactly")
    for item in observations:
        item = _mapping(item, "historical_evidence.observations[]")
        if item.get("status") != "REFERENCE_ONLY":
            raise IntegrityError("HISTORICAL_CURRENT_MIX", "historical observations must be marked REFERENCE_ONLY")
        observed = _mapping(item.get("observed"), "historical_evidence.observations[].observed")
        if item.get("scenario_identity") != observed.get("scenario"):
            raise IntegrityError("CONTRACT_INVALID", "historical scenario identity must match preserved observation")

    current = _mapping(contract.get("current_execution"), "current_execution")
    if current.get("status") != "ENABLED":
        raise IntegrityError("CONTRACT_INVALID", "contract current_execution must enable canonical scenarios")
    if current.get("target") != "src/ephi" or current.get("import") != "ephi":
        raise IntegrityError("CONTRACT_INVALID", "canonical current_execution target must be src/ephi/ephi")
    canonical = _mapping(contract.get("canonical_execution"), "canonical_execution")
    if canonical.get("status") != "ENABLED" or canonical.get("runner") != "tools/w0_integrity_regressions.py":
        raise IntegrityError("CONTRACT_INVALID", "canonical execution section is not enabled")
    if canonical.get("fresh_findings") != ["F02", "F03", "F04"]:
        raise IntegrityError("CONTRACT_INVALID", "canonical execution must run fresh F02/F03/F04 scenarios")
    out_of_scope = _mapping(canonical.get("out_of_scope"), "canonical_execution.out_of_scope")
    if set(out_of_scope) != {"F05"} or any(
        not isinstance(value, Mapping)
        or value.get("status") != "NOT_IMPLEMENTED"
        or value.get("execution") != "NOT_RUN"
        for value in out_of_scope.values()
    ):
        raise IntegrityError("CONTRACT_INVALID", "F05 must remain separately NOT_IMPLEMENTED/NOT_RUN")
    if source_bound:
        legacy_current = _mapping(contract.get("legacy_source_execution"), "legacy_source_execution")
        if legacy_current.get("probe_script") != "evidence/behavior_probes.py" or legacy_current.get("pythonpath") != "src:company_port/src":
            raise IntegrityError("CONTRACT_INVALID", "current probe boundary does not match the documented source boundary")
        for field in ("runtime_artifact_root", "result_output"):
            _repo_relative(legacy_current.get(field), f"legacy_source_execution.{field}")
        protected = legacy_current.get("historical_output_must_not_be_overwritten")
        if protected != ["evidence/behavior_probes.py", "evidence/behavior_probes.json", "evidence/behavior_probes.log"]:
            raise IntegrityError("CONTRACT_INVALID", "current execution does not protect historical probe files")

    qualification = _mapping(contract.get("f02_checkpoint_restore_qualification"), "f02_checkpoint_restore_qualification")
    for field in ("source_inventory", "source_excerpts", "checkpoint_source_path", "checkpoint_test_path", "checkpoint_test_symbol"):
        _repo_relative(qualification.get(field), f"f02_checkpoint_restore_qualification.{field}")
    if qualification.get("checkpoint_test_symbol") == "":
        raise IntegrityError("CONTRACT_INVALID", "F02 checkpoint test symbol must not be empty")

    findings = _list(contract.get("findings"), "findings")
    if len(findings) != len(EXPECTED_FINDING_IDS) or {item.get("id") for item in findings if isinstance(item, Mapping)} != set(EXPECTED_FINDING_IDS):
        raise IntegrityError("CONTRACT_INVALID", "findings must cover F02/F03/F04/F05 exactly")
    for finding in findings:
        finding = _mapping(finding, "findings[]")
        for field in ("scenario_identity", "historical_observation", "target_product_behavior", "evaluation_rule"):
            if field not in finding:
                raise IntegrityError("CONTRACT_INVALID", f"finding {finding.get('id')} lacks {field}")
        historical_observation = _mapping(finding["historical_observation"], "finding.historical_observation")
        if historical_observation.get("status") != "REFERENCE_ONLY" or historical_observation.get("source") != "evidence/behavior_probes.json":
            raise IntegrityError("HISTORICAL_CURRENT_MIX", f"finding {finding.get('id')} historical observation is not reference-only")
        observed = _mapping(historical_observation.get("observed"), "finding.historical_observation.observed")
        if historical_observation.get("record_id") != finding.get("id") or observed.get("id") != finding.get("id"):
            raise IntegrityError("CONTRACT_INVALID", f"finding {finding.get('id')} historical record identity is inconsistent")
        if observed.get("scenario") != finding.get("scenario_identity"):
            raise IntegrityError("CONTRACT_INVALID", f"finding {finding.get('id')} scenario identity is inconsistent")
        if not _list(finding.get("source_locators"), "finding.source_locators") or not _list(finding.get("audit_locators"), "finding.audit_locators"):
            raise IntegrityError("CONTRACT_INVALID", f"finding {finding.get('id')} lacks source/audit locators")
        for locator_group in (finding["source_locators"], finding["audit_locators"]):
            for locator in locator_group:
                locator = _mapping(locator, "finding.locator")
                if not isinstance(locator.get("path"), str) or not locator["path"]:
                    raise IntegrityError("CONTRACT_INVALID", f"finding {finding.get('id')} has an incomplete locator")
                _repo_relative(locator["path"], "finding.locator.path")
        rule = _mapping(finding["evaluation_rule"], "finding.evaluation_rule")
        if not isinstance(rule.get("type"), str) or not rule["type"]:
            raise IntegrityError("CONTRACT_INVALID", f"finding {finding.get('id')} lacks evaluation rule type")
        assertion_groups = [rule.get("assertions")] if finding.get("id") != "F02" else [rule.get("source_only_assertions")]
        for assertions in assertion_groups:
            assertions = _list(assertions, "finding.evaluation_rule.assertions")
            if not assertions:
                raise IntegrityError("CONTRACT_INVALID", f"finding {finding.get('id')} has no evaluation assertions")
            for assertion in assertions:
                assertion = _mapping(assertion, "finding.evaluation_rule.assertions[]")
                if assertion.get("operator") not in {"equals", "not_equals"} or not isinstance(assertion.get("path"), str):
                    raise IntegrityError("CONTRACT_INVALID", f"finding {finding.get('id')} has an invalid evaluation assertion")


def verify_historical_evidence(contract: Mapping[str, Any], *, source_bound: bool = True) -> list[dict[str, Any]]:
    """Verify preserved evidence identities and return observations as reference-only data."""

    validate_contract(contract, source_bound=source_bound)
    historical = _mapping(contract["historical_evidence"], "historical_evidence")
    for item in historical["files"]:
        item = _mapping(item, "historical_evidence.files[]")
        path = _repo_relative(item["path"], "historical_evidence.files[].path")
        if path.is_symlink() or not path.is_file():
            raise IntegrityError("HISTORICAL_EVIDENCE_CHANGED", f"historical evidence file is missing or symlinked: {item['path']}")
        size, digest = sha256(path)
        if size != item["bytes"] or digest != item["sha256"]:
            raise IntegrityError("HISTORICAL_EVIDENCE_CHANGED", f"historical evidence identity changed: {item['path']}")

    probe_json = _repo_relative(historical["probe_record"], "historical_evidence.probe_record")
    probe_rows = _read_json(probe_json, "historical probe record")
    if not isinstance(probe_rows, list):
        raise IntegrityError("HISTORICAL_EVIDENCE_INVALID", "historical probe record must be an array")
    if len(probe_rows) != len(EXPECTED_FINDING_IDS) or any(not isinstance(row, Mapping) for row in probe_rows):
        raise IntegrityError("HISTORICAL_EVIDENCE_CHANGED", "historical probe record has an unexpected row set")
    expected = {item["id"]: item["observed"] for item in historical["observations"]}
    actual = {row.get("id"): row for row in probe_rows if isinstance(row, Mapping)}
    if len(actual) != len(probe_rows) or actual != expected:
        raise IntegrityError("HISTORICAL_EVIDENCE_CHANGED", "historical probe record differs from the contract reference")
    probe_log = _repo_relative("evidence/behavior_probes.log", "historical probe log")
    if probe_json.read_bytes() != probe_log.read_bytes():
        raise IntegrityError("HISTORICAL_EVIDENCE_CHANGED", "historical probe JSON and log differ")
    return [dict(row) for row in probe_rows]


def _new_result(contract_path: Path, preflight_path: Path) -> dict[str, Any]:
    expected_filename, expected_sha256, expected_source_root = _legacy_source_constants()
    return {
        "schema_version": 1,
        "change": "CHG-109",
        "wave": "W0",
        "status": "BLOCKED",
        "reason": None,
        "message": None,
        "contract": {
            "path": str(contract_path),
            "status": "NOT_READ",
            "record_sha256": None,
        },
        "preflight": {
            "path": str(preflight_path),
            "status": "NOT_READ",
            "record_sha256": None,
        },
        "source_identity": {
            "required_filename": expected_filename,
            "required_sha256": expected_sha256,
            "required_source_root": expected_source_root,
            "verified_by": "tools/source_preflight.py and tools/w0_baseline.py",
        },
        "historical_evidence": {
            "status": "REFERENCE_ONLY",
            "current_execution": False,
            "observations": [],
        },
        "current_execution": {
            "status": "NOT_RUN",
            "tests_executed": False,
            "source_root": None,
            "probe": {"status": "NOT_RUN"},
            "checkpoint_restore": {"status": "NOT_RUN"},
        },
        "findings": [
            {
                "id": finding_id,
                "status": "NOT_RUN",
                "reason": "SOURCE_NOT_STAGED",
                **({"source_only_result": "NOT_RUN", "checkpoint_restore_result": "NOT_RUN"} if finding_id == "F02" else {}),
            }
            for finding_id in EXPECTED_FINDING_IDS
        ],
    }


def _blocked(result: dict[str, Any], error: IntegrityError) -> dict[str, Any]:
    result["status"] = "BLOCKED"
    result["reason"] = error.code
    result["message"] = str(error)
    result["current_execution"]["status"] = "NOT_RUN"
    return result


def _finish(result: dict[str, Any], output_path: Path | None) -> dict[str, Any]:
    if output_path is not None:
        write_result(output_path, result)
    return result


def _write_runtime_bytes(path: Path, content: bytes) -> dict[str, Any]:
    path.write_bytes(content)
    size, digest = sha256(path)
    return {"path": str(path), "bytes": size, "sha256": digest}


def _safe_runtime_dir(root: Path) -> Path:
    root = Path(root).expanduser()
    if root.resolve(strict=False).is_relative_to(EVIDENCE_ROOT.resolve()):
        raise IntegrityError("PROTECTED_PATH", "runtime artifacts must not be written inside preserved evidence")
    if root.is_symlink():
        raise IntegrityError("RUNTIME_SYMLINK", "runtime artifact root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise IntegrityError("RUNTIME_SYMLINK", "runtime artifact root must not be a symlink")
    try:
        return Path(tempfile.mkdtemp(prefix="run-", dir=root))
    except OSError as exc:
        raise IntegrityError("RUNTIME_CREATE_ERROR", f"cannot create runtime artifact directory: {exc}") from exc


def _tree_snapshot(root: Path) -> str:
    entries: list[tuple[str, str, int | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "SYMLINK", None))
        elif path.is_file():
            size, digest = sha256(path)
            entries.append((relative, digest, size))
        elif path.is_dir():
            entries.append((relative, "DIRECTORY", None))
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode("utf-8")).hexdigest()


def _command_result(command: list[str], source_root: Path, runtime_dir: Path) -> dict[str, Any]:
    stdout_path = runtime_dir / "command.stdout"
    stderr_path = runtime_dir / "command.stderr"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = "src:company_port/src"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    encoded = shlex.join(command)
    try:
        completed = subprocess.run(
            command,
            cwd=source_root,
            env=environment,
            capture_output=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        return {
            "status": "PASS" if completed.returncode == 0 else "FAIL",
            "command": encoded,
            "argv": command,
            "cwd": str(source_root),
            "returncode": completed.returncode,
            "timeout_seconds": PROBE_TIMEOUT_SECONDS,
            "stdout": _write_runtime_bytes(stdout_path, stdout),
            "stderr": _write_runtime_bytes(stderr_path, stderr),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        return {
            "status": "FAIL",
            "command": encoded,
            "argv": command,
            "cwd": str(source_root),
            "returncode": None,
            "timeout_seconds": PROBE_TIMEOUT_SECONDS,
            "timeout": True,
            "stdout": _write_runtime_bytes(stdout_path, stdout),
            "stderr": _write_runtime_bytes(stderr_path, stderr),
        }


def _path_value(row: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _assertion_result(row: Mapping[str, Any], assertion: Mapping[str, Any]) -> dict[str, Any]:
    path = assertion.get("path")
    operator = assertion.get("operator")
    expected = assertion.get("value")
    present, actual = _path_value(row, path) if isinstance(path, str) else (False, None)
    if path == "actual_operating_cost" and operator == "equals":
        try:
            actual_decimal = Decimal(actual) if not isinstance(actual, (bool, float)) else None
            expected_decimal = Decimal(expected) if not isinstance(expected, (bool, float)) else None
        except (InvalidOperation, TypeError, ValueError):
            actual_decimal = expected_decimal = None
        exact_money = (
            actual_decimal is not None
            and expected_decimal is not None
            and actual_decimal.is_finite()
            and expected_decimal.is_finite()
        )
        passed = present and exact_money and actual_decimal == expected_decimal
    else:
        passed = present and (
            actual == expected if operator == "equals" else actual != expected if operator == "not_equals" else False
        )
    return {
        "path": path,
        "operator": operator,
        "expected": expected,
        "actual": actual,
        "passed": passed,
    }


def evaluate_finding(finding: Mapping[str, Any], observation: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one fresh probe row against its contract, never against historical rows."""

    rule = _mapping(finding.get("evaluation_rule"), "finding.evaluation_rule")
    assertions = _list(rule.get("assertions"), "finding.evaluation_rule.assertions")
    checks = [_assertion_result(observation, _mapping(item, "finding.evaluation_rule.assertions[]")) for item in assertions]
    passed = all(check["passed"] for check in checks)
    return {
        "id": finding.get("id"),
        "status": "PASS" if passed else "FAIL",
        "scenario": finding.get("scenario_identity"),
        "checks": checks,
        "observed": dict(observation),
        "message": "all current assertions passed" if passed else "one or more current assertions failed",
    }


def evaluate_f02(finding: Mapping[str, Any], observation: Mapping[str, Any] | None, checkpoint_restore_result: str) -> dict[str, Any]:
    """Apply F02's source-only boundary and separate checkpoint qualification result."""

    if checkpoint_restore_result not in F02_RESULTS - {"SOURCE_ONLY_INSUFFICIENT"}:
        raise IntegrityError("F02_RESULT_INVALID", f"invalid checkpoint restore result: {checkpoint_restore_result}")
    source_only_result = "NOT_RUN"
    source_only_checks: list[dict[str, Any]] = []
    if observation is not None:
        source_evaluation = evaluate_finding(
            {
                "id": "F02",
                "scenario_identity": finding["scenario_identity"],
                "evaluation_rule": {
                    "assertions": finding["evaluation_rule"]["source_only_assertions"]
                },
            },
            observation,
        )
        source_only_checks = source_evaluation["checks"]
        if source_evaluation["status"] == "PASS":
            source_only_result = "SOURCE_ONLY_INSUFFICIENT"
        else:
            source_only_result = "FAIL"

    if source_only_result == "NOT_RUN" or checkpoint_restore_result == "NOT_RUN":
        status = "NOT_RUN"
    elif source_only_result == "SOURCE_ONLY_INSUFFICIENT" and checkpoint_restore_result == "CHECKPOINT_RESTORE_PASS":
        status = "PASS"
    else:
        status = "FAIL"
    return {
        "id": "F02",
        "status": status,
        "scenario": finding.get("scenario_identity"),
        "source_only_result": source_only_result,
        "checkpoint_restore_result": checkpoint_restore_result,
        "source_only_checks": source_only_checks,
        "message": {
            "PASS": "source-only insufficiency preserved and checkpoint restore passed",
            "FAIL": "F02 source-only boundary or checkpoint restore qualification failed",
            "NOT_RUN": "F02 checkpoint restore qualification is not run",
        }[status],
    }


def _finding_map(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(finding["id"]): finding for finding in contract["findings"]}


def _row_map(rows: list[Any]) -> dict[str, Mapping[str, Any]]:
    values = [row for row in rows if isinstance(row, Mapping) and isinstance(row.get("id"), str)]
    if len(values) != len(rows) or len({row["id"] for row in values}) != len(values):
        raise IntegrityError("CURRENT_OUTPUT_INVALID", "fresh behavior probe output contains invalid or duplicate finding IDs")
    return {row["id"]: row for row in values}


def _inventory_row(inventory: Any, path: str) -> Mapping[str, Any] | None:
    if not isinstance(inventory, list):
        return None
    for row in inventory:
        if isinstance(row, Mapping) and row.get("path") == path:
            return row
    return None


def _ast_functions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return {}
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    }


def discover_checkpoint_api(source_root: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    """Discover, hash and inspect the actual advisory checkpoint API without importing it."""

    qualification = contract["f02_checkpoint_restore_qualification"]
    inventory = _read_json(
        _repo_relative(qualification["source_inventory"], "f02 source inventory"),
        "preserved source inventory",
    )
    source_path_value = qualification["checkpoint_source_path"]
    test_path_value = qualification["checkpoint_test_path"]
    source_row = _inventory_row(inventory, source_path_value)
    test_row = _inventory_row(inventory, test_path_value)
    if source_row is None or test_row is None:
        return {"status": "NOT_RUN", "reason": "CHECKPOINT_API_NOT_IDENTIFIED"}
    source_path = source_root.joinpath(*PurePosixPath(source_path_value).parts)
    test_path = source_root.joinpath(*PurePosixPath(test_path_value).parts)
    if any(path.is_symlink() or not path.is_file() for path in (source_path, test_path)):
        return {"status": "NOT_RUN", "reason": "CHECKPOINT_SOURCE_OR_TEST_MISSING"}
    for path, row in ((source_path, source_row), (test_path, test_row)):
        size, digest = sha256(path)
        if size != row.get("bytes") or digest != row.get("sha256"):
            return {"status": "NOT_RUN", "reason": "CHECKPOINT_SOURCE_IDENTITY_MISMATCH"}

    source_functions = _ast_functions(source_path)
    symbols = [symbol.get("name") for symbol in source_row.get("symbols", []) if isinstance(symbol, Mapping)]
    save_names = [name for name in symbols if isinstance(name, str) and name.startswith("save_")]
    restore_names = [name for name in symbols if isinstance(name, str) and name.startswith("restore_")]
    if len(save_names) != 1 or len(restore_names) != 1 or any(name not in source_functions for name in (*save_names, *restore_names)):
        return {"status": "NOT_RUN", "reason": "CHECKPOINT_API_NOT_IDENTIFIED"}
    api_source = "\n".join(
        ast.unparse(source_functions[name]) for name in (save_names[0], restore_names[0])
    ).lower()
    state_fields = [term for term in ("workflow", "version", "revision") if term in api_source]
    if len(state_fields) != 3:
        return {"status": "NOT_RUN", "reason": "CHECKPOINT_STATE_FIELDS_NOT_IDENTIFIED"}

    test_functions = _ast_functions(test_path)
    test_name = qualification["checkpoint_test_symbol"]
    test_function = test_functions.get(test_name)
    if test_function is None:
        return {"status": "NOT_RUN", "reason": "CHECKPOINT_TEST_NOT_IDENTIFIED"}
    called_names = {
        node.id for node in ast.walk(test_function) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(test_function) if isinstance(node, ast.Attribute)
    }
    if not {save_names[0], restore_names[0]}.issubset(called_names):
        return {"status": "NOT_RUN", "reason": "CHECKPOINT_TEST_DOES_NOT_EXERCISE_API"}
    excerpts_path = _repo_relative(qualification["source_excerpts"], "f02 source excerpts")
    try:
        excerpts = excerpts_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {"status": "NOT_RUN", "reason": "F02_SOURCE_EXCERPTS_UNAVAILABLE"}
    if "## F02" not in excerpts or "src/ephi/advisory/service.py" not in excerpts:
        return {"status": "NOT_RUN", "reason": "F02_SOURCE_EXCERPT_NOT_IDENTIFIED"}

    def signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        return ast.unparse(node.args)

    return {
        "status": "IDENTIFIED",
        "source_locator": source_path_value,
        "test_locator": test_path_value,
        "test_symbol": test_name,
        "save_api": {"name": save_names[0], "signature": signature(source_functions[save_names[0]])},
        "restore_api": {"name": restore_names[0], "signature": signature(source_functions[restore_names[0]])},
        "basis": {
            "inventory": "evidence/source_inventory.json",
            "excerpts": "evidence/source_excerpts.md",
            "f02_excerpt_inspected": True,
            "staged_source_hashes_verified": True,
            "existing_checkpoint_test_calls_discovered_api": True,
            "state_fields_observed": state_fields,
        },
    }


def _checkpoint_runner(record: Mapping[str, Any], source_root: Path, test_path: str, test_name: str) -> tuple[list[str], str] | None:
    baseline = record.get("baseline")
    if not isinstance(baseline, Mapping):
        return None
    execution = baseline.get("test_execution")
    runner = execution.get("runner_discovered") if isinstance(execution, Mapping) else None
    if runner == "pytest":
        if importlib.util.find_spec("pytest") is None:
            return None
        return [sys.executable, "-m", "pytest", f"{test_path}::{test_name}", "-q", "-p", "no:cacheprovider"], runner
    if runner == "unittest":
        module = ".".join(PurePosixPath(test_path).with_suffix("").parts)
        return [sys.executable, "-m", "unittest", f"{module}.{test_name}", "-v"], runner
    return None


def _execute_current(
    record: Mapping[str, Any],
    source_root: Path,
    contract: Mapping[str, Any],
    runtime_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    before = _tree_snapshot(source_root)
    probe_output = runtime_dir / "behavior_probes.json"
    probe_command = [
        sys.executable,
        str(_repo_relative(contract["legacy_source_execution"]["probe_script"], "legacy_source_execution.probe_script")),
        "--output",
        str(probe_output),
    ]
    probe_command_result = _command_result(probe_command, source_root, runtime_dir)
    probe = dict(probe_command_result)
    probe["output"] = None
    if probe_output.is_file() and not probe_output.is_symlink():
        size, digest = sha256(probe_output)
        probe["output"] = {"path": str(probe_output), "bytes": size, "sha256": digest}
    if probe_command_result["status"] != "PASS" or probe["output"] is None:
        checkpoint = {"status": "NOT_RUN", "reason": "PROBE_EXECUTION_FAILED"}
        return probe, checkpoint, {"before": before, "after": _tree_snapshot(source_root)}

    api = discover_checkpoint_api(source_root, contract)
    if api.get("status") != "IDENTIFIED":
        return probe, {"status": "NOT_RUN", "reason": api.get("reason", "CHECKPOINT_API_NOT_IDENTIFIED"), "discovery": api}, {"before": before, "after": _tree_snapshot(source_root)}
    qualification = contract["f02_checkpoint_restore_qualification"]
    command = _checkpoint_runner(
        record,
        source_root,
        qualification["checkpoint_test_path"],
        qualification["checkpoint_test_symbol"],
    )
    if command is None:
        return probe, {"status": "NOT_RUN", "reason": "CHECKPOINT_TEST_RUNNER_UNAVAILABLE", "discovery": api}, {"before": before, "after": _tree_snapshot(source_root)}
    checkpoint_runtime = runtime_dir / "checkpoint"
    checkpoint_runtime.mkdir()
    checkpoint_command_result = _command_result(command[0], source_root, checkpoint_runtime)
    checkpoint_status = "CHECKPOINT_RESTORE_PASS" if checkpoint_command_result["status"] == "PASS" else "CHECKPOINT_RESTORE_FAIL"
    checkpoint = {
        "status": checkpoint_status,
        "runner": command[1],
        "discovery": api,
        "execution": checkpoint_command_result,
    }
    return probe, checkpoint, {"before": before, "after": _tree_snapshot(source_root)}


def _new_canonical_result(contract_path: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "change": "CHG-109",
        "wave": "W0",
        "status": "NOT_RUN",
        "reason": None,
        "message": None,
        "target": {
            "source_root": "src/ephi",
            "import": "ephi",
            "entrypoint": "ephi.app:main",
            "mode": "canonical-repository",
        },
        "contract": {
            "path": str(contract_path),
            "status": "NOT_READ",
            "record_sha256": None,
        },
        "historical_evidence": {
            "status": "REFERENCE_ONLY",
            "current_execution": False,
            "observations": [],
        },
        "current_execution": {
            "status": "NOT_RUN",
            "tests_executed": False,
            "target": "src/ephi",
            "application_self_check": {"status": "NOT_RUN"},
            "canonical_scenarios": {"status": "NOT_RUN"},
        },
        "findings": [
            {
                "id": finding_id,
                "status": "NOT_IMPLEMENTED",
                "execution": "NOT_RUN",
                "reason": "canonical scenario has not run",
                **({"source_only_result": "NOT_RUN", "checkpoint_restore_result": "NOT_RUN"} if finding_id == "F02" else {}),
            }
            for finding_id in EXPECTED_FINDING_IDS
        ],
    }


def _canonical_self_check() -> dict[str, Any]:
    source = REPOSITORY_ROOT / "src"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source) + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    completed = subprocess.run(
        [sys.executable, "-m", "ephi", "--self-check", "--json"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        return {"status": "FAIL", "returncode": completed.returncode, "stderr": completed.stderr[-4000:]}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "FAIL", "reason": "SELF_CHECK_INVALID_JSON"}
    return {"status": "PASS", "returncode": completed.returncode, "identity": payload}


def _canonical_f03_scenario(scenario: str) -> dict[str, Any]:
    """Execute the fresh F03 scenario against the canonical domain service."""

    from ephi.advisory import AdvisoryService, EngineeringWorkState, TechnicalEpisodeState

    service = AdvisoryService()
    episode = service.create_episode("canonical-f03-episode")
    episode = service.transition_engineering_work_state(
        episode.episode_id,
        EngineeringWorkState.INVESTIGATING,
        expected_workflow_version=episode.workflow_version,
    )
    episode = service.transition_technical_state(
        episode.episode_id,
        TechnicalEpisodeState.RESOLVED,
        expected_workflow_version=episode.workflow_version,
    )
    attention = service.query_attention()
    visible = [row for row in attention if row.episode_id == episode.episode_id]
    return {
        "id": "F03",
        "scenario": scenario,
        "technical_episode_state": episode.technical_state.value,
        "workflow": episode.engineering_work_state.value,
        "visible_in_attention": len(visible) == 1 and visible[0].visible_in_attention,
    }


def _canonical_f04_scenario(scenario: str) -> dict[str, Any]:
    """Execute the fresh F04 cutoff scenario against the canonical value API."""

    from datetime import datetime, timezone

    repository = InMemoryValueRepository()
    original = ValueEntry(
        entry_id="canonical-f04-original",
        scope="canonical-scope",
        group_id="canonical-claim-group",
        category="operating_cost",
        amount=Decimal("10"),
        currency="USD",
        event_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        known_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    repository.append(original)
    repository.append(
        ValueEntry(
            entry_id="canonical-f04-correction",
            scope=original.scope,
            group_id=original.group_id,
            category=original.category,
            amount=Decimal("20"),
            currency=original.currency,
            event_at=original.event_at,
            known_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            supersedes=original.entry_id,
        )
    )
    actual = ValueService(repository).aggregate(
        scope=original.scope,
        group_id=original.group_id,
        category=original.category,
        currency=original.currency,
        knowledge_cutoff=datetime(2026, 1, 2, tzinfo=timezone.utc),
        event_period=EventPeriod(
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        ),
    )
    return {
        "id": "F04",
        "scenario": scenario,
        "knowledge_cutoff": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "expected_operating_cost": Decimal("10"),
        "actual_operating_cost": actual,
    }


def _canonical_f02_scenario(scenario: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Execute source-only rejection and complete checkpoint round-trip independently."""

    from ephi.advisory import (
        AdvisoryService,
        CheckpointError,
        EngineeringWorkState,
        TechnicalEpisodeState,
    )

    service = AdvisoryService()
    episode = service.create_episode("canonical-f02-episode")
    episode = service.transition_engineering_work_state(
        episode.episode_id,
        EngineeringWorkState.INVESTIGATING,
        expected_workflow_version=episode.workflow_version,
    )
    episode = service.transition_technical_state(
        episode.episode_id,
        TechnicalEpisodeState.RESOLVED,
        expected_workflow_version=episode.workflow_version,
    )
    partial = {
        "checkpoint_schema": "ephi.advisory.checkpoint",
        "checkpoint_version": 1,
        "episode_id": episode.episode_id,
        "technical_state": episode.technical_state.value,
    }
    try:
        service.restore_checkpoint(partial, expected_episode_id=episode.episode_id)
    except CheckpointError as exc:
        source_only_result = "SOURCE_ONLY_INSUFFICIENT" if exc.code == "INCOMPLETE_AUTHORITY" else "FAIL"
        source_only_error_code = exc.code
    else:
        source_only_result = "FAIL"
        source_only_error_code = None

    checkpoint = service.save_checkpoint(episode.episode_id)
    restored_service = AdvisoryService()
    restored = restored_service.restore_checkpoint(checkpoint, expected_episode_id=episode.episode_id)
    round_trip_fields = restored.as_dict() == episode.as_dict()
    checkpoint_result = "CHECKPOINT_RESTORE_PASS" if round_trip_fields else "CHECKPOINT_RESTORE_FAIL"
    observation = {
        "id": "F02",
        "scenario": scenario,
        "source_only_result": source_only_result,
        "source_only_error_code": source_only_error_code,
        "round_trip_preserved": round_trip_fields,
    }
    details = {
        "status": checkpoint_result,
        "source_only_error_code": source_only_error_code,
        "serialized_checkpoint": checkpoint,
        "original": episode.as_dict(),
        "restored": restored.as_dict(),
    }
    return observation, checkpoint_result, details


def run_canonical_integrity_regressions(
    *,
    contract_path: Path = CONTRACT_PATH,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Verify historical evidence and execute the fresh canonical F02/F03/F04 slice."""

    contract_path = Path(contract_path).expanduser()
    result = _new_canonical_result(contract_path)
    try:
        contract_raw = contract_path.read_bytes()
        result["contract"]["record_sha256"] = hashlib.sha256(contract_raw).hexdigest()
        contract_value = _strict_json_bytes(contract_raw, "CHG-109 contract")
        if not isinstance(contract_value, Mapping):
            raise IntegrityError("CONTRACT_INVALID", "CHG-109 contract must be an object")
        validate_contract(contract_value, source_bound=False)
        result["contract"]["status"] = "VALID"
        result["historical_evidence"]["observations"] = verify_historical_evidence(contract_value, source_bound=False)
    except FileNotFoundError as exc:
        return _finish(_blocked(result, IntegrityError("CONTRACT_MISSING", f"CHG-109 contract is missing: {contract_path}")), output_path)
    except IntegrityError as exc:
        return _finish(_blocked(result, exc), output_path)
    except OSError as exc:
        return _finish(_blocked(result, IntegrityError("CONTRACT_READ_ERROR", str(exc))), output_path)

    try:
        result["current_execution"]["application_self_check"] = _canonical_self_check()
        if result["current_execution"]["application_self_check"]["status"] != "PASS":
            result["current_execution"]["status"] = "FAIL"
            result["status"] = "FAIL"
            result["reason"] = "CANONICAL_SELF_CHECK_FAILED"
            result["message"] = "canonical application self-check failed"
        else:
            sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
            findings = _finding_map(contract_value)
            f03_observation = _canonical_f03_scenario(findings["F03"]["scenario_identity"])
            f02_observation, f02_checkpoint_status, f02_details = _canonical_f02_scenario(
                findings["F02"]["scenario_identity"]
            )
            f04_observation = _canonical_f04_scenario(findings["F04"]["scenario_identity"])
            evaluated = [
                evaluate_finding(findings["F03"], f03_observation),
                evaluate_f02(findings["F02"], f02_observation, f02_checkpoint_status),
                evaluate_finding(findings["F04"], f04_observation),
                {
                    "id": "F05",
                    "status": "NOT_IMPLEMENTED",
                    "execution": "NOT_RUN",
                    "reason": "out of scope for CHG-116",
                },
            ]
            result["findings"] = evaluated
            result["current_execution"]["status"] = "PASS" if all(
                item["status"] == "PASS" for item in evaluated[:3]
            ) else "FAIL"
            result["current_execution"]["tests_executed"] = True
            result["current_execution"]["canonical_scenarios"] = {
                "status": result["current_execution"]["status"],
                "target": "src/ephi",
                "f02": f02_details,
                "f03": f03_observation,
                "f04": f04_observation,
            }
            result["status"] = result["current_execution"]["status"]
            result["reason"] = (
                "ALL_SCOPED_INTEGRITY_REGRESSIONS_PASS"
                if result["status"] == "PASS"
                else "REGRESSION_ASSERTION_FAILED"
            )
            result["message"] = (
                "fresh canonical F02/F03/F04 scenarios passed; F05 remains NOT_IMPLEMENTED/NOT_RUN"
                if result["status"] == "PASS"
                else "one or more fresh canonical F02/F03 assertions failed"
            )
        return _finish(result, output_path)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        return _finish(_blocked(result, IntegrityError("CANONICAL_EXECUTION_ERROR", str(exc))), output_path)


def _run_legacy_integrity_regressions(
    preflight_path: Path,
    output_path: Path | None = None,
    *,
    contract_path: Path = CONTRACT_PATH,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> dict[str, Any]:
    """Run CHG-109 only after exact source preflight and historical evidence checks pass."""

    from tools.w0_baseline import BaselineError, validate_preflight_record

    preflight_path = Path(preflight_path).expanduser()
    contract_path = Path(contract_path).expanduser()
    result = _new_result(contract_path, preflight_path)
    try:
        contract_raw = contract_path.read_bytes()
        result["contract"]["record_sha256"] = hashlib.sha256(contract_raw).hexdigest()
        contract_value = _strict_json_bytes(contract_raw, "CHG-109 contract")
        if not isinstance(contract_value, Mapping):
            raise IntegrityError("CONTRACT_INVALID", "CHG-109 contract must be an object")
        validate_contract(contract_value)
        result["contract"]["status"] = "VALID"
        result["historical_evidence"]["observations"] = verify_historical_evidence(contract_value)
    except FileNotFoundError as exc:
        return _finish(_blocked(result, IntegrityError("CONTRACT_MISSING", f"CHG-109 contract is missing: {contract_path}")), output_path)
    except IntegrityError as exc:
        return _finish(_blocked(result, exc), output_path)
    except OSError as exc:
        return _finish(_blocked(result, IntegrityError("CONTRACT_READ_ERROR", str(exc))), output_path)

    try:
        preflight_raw = preflight_path.read_bytes()
    except FileNotFoundError:
        return _finish(_blocked(result, IntegrityError("PREFLIGHT_MISSING", "source-preflight result is missing")), output_path)
    except OSError as exc:
        return _finish(_blocked(result, IntegrityError("PREFLIGHT_READ_ERROR", str(exc))), output_path)
    result["preflight"]["record_sha256"] = hashlib.sha256(preflight_raw).hexdigest()
    try:
        preflight = _strict_json_bytes(preflight_raw, "source-preflight result")
        if not isinstance(preflight, Mapping):
            raise IntegrityError("PREFLIGHT_SCHEMA_INVALID", "source-preflight result must be an object")
        result["preflight"]["status"] = preflight.get("status", "MISSING")
        source_root = validate_preflight_record(preflight)
    except BaselineError as exc:
        return _finish(_blocked(result, IntegrityError(exc.code, str(exc))), output_path)
    except IntegrityError as exc:
        return _finish(_blocked(result, exc), output_path)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return _finish(_blocked(result, IntegrityError("PREFLIGHT_SCHEMA_INVALID", str(exc))), output_path)

    result["current_execution"]["source_root"] = str(source_root)
    try:
        runtime_dir = _safe_runtime_dir(runtime_root)
        probe, checkpoint, source_snapshots = _execute_current(preflight, source_root, contract_value, runtime_dir)
        result["current_execution"]["runtime_directory"] = str(runtime_dir)
        result["current_execution"]["probe"] = probe
        result["current_execution"]["checkpoint_restore"] = checkpoint
        result["current_execution"]["tests_executed"] = probe.get("status") in {"PASS", "FAIL"}
        result["current_execution"]["source_tree_before_sha256"] = source_snapshots["before"]
        result["current_execution"]["source_tree_after_sha256"] = source_snapshots["after"]
        if source_snapshots["before"] != source_snapshots["after"]:
            result["current_execution"]["status"] = "FAIL"
            return _finish(_blocked(result, IntegrityError("STAGED_SOURCE_MODIFIED", "staged source changed during the bounded run")), output_path)
        if probe.get("status") != "PASS" or not probe.get("output"):
            result["current_execution"]["status"] = "FAIL"
            result["status"] = "FAIL"
            result["reason"] = "PROBE_EXECUTION_FAILED"
            result["message"] = "preserved behavior probe did not complete successfully"
            return _finish(result, output_path)
        fresh_rows = _read_json(Path(probe["output"]["path"]), "fresh behavior probe output")
        if not isinstance(fresh_rows, list):
            raise IntegrityError("CURRENT_OUTPUT_INVALID", "fresh behavior probe output must be an array")
        rows = _row_map(fresh_rows)
        if set(rows) != set(EXPECTED_FINDING_IDS):
            raise IntegrityError("CURRENT_OUTPUT_INVALID", "fresh behavior probe output must cover F02/F03/F04/F05 exactly")
        findings = _finding_map(contract_value)
        evaluated: list[dict[str, Any]] = []
        for finding_id in EXPECTED_FINDING_IDS:
            if finding_id == "F02":
                evaluated.append(evaluate_f02(findings[finding_id], rows[finding_id], checkpoint["status"]))
            else:
                evaluated.append(evaluate_finding(findings[finding_id], rows[finding_id]))
        result["findings"] = evaluated
        statuses = [item["status"] for item in evaluated]
        if "FAIL" in statuses:
            result["current_execution"]["status"] = "FAIL"
            result["status"] = "FAIL"
            result["reason"] = "REGRESSION_ASSERTION_FAILED"
            result["message"] = "one or more current integrity assertions failed"
        elif "NOT_RUN" in statuses:
            result["current_execution"]["status"] = "NOT_RUN"
            result["status"] = "NOT_RUN"
            result["reason"] = "F02_CHECKPOINT_RESTORE_NOT_RUN"
            result["message"] = "source probe ran, but F02 checkpoint/restore qualification did not run"
        else:
            result["current_execution"]["status"] = "PASS"
            result["status"] = "PASS"
            result["reason"] = "ALL_INTEGRITY_REGRESSIONS_PASS"
            result["message"] = "all current F02/F03/F04/F05 integrity assertions passed"
        return _finish(result, output_path)
    except IntegrityError as exc:
        return _finish(_blocked(result, exc), output_path)
    except (OSError, KeyError, TypeError, ValueError, SyntaxError) as exc:
        return _finish(_blocked(result, IntegrityError("CURRENT_EXECUTION_ERROR", str(exc))), output_path)


def run_integrity_regressions(
    preflight_path: Path | None = None,
    output_path: Path | None = None,
    *,
    contract_path: Path = CONTRACT_PATH,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    legacy_source: bool = False,
) -> dict[str, Any]:
    """Run canonical checks by default; require an explicit legacy source mode."""

    if not legacy_source:
        return run_canonical_integrity_regressions(contract_path=contract_path, output_path=output_path)
    if preflight_path is None:
        raise IntegrityError("PREFLIGHT_REQUIRED", "legacy source mode requires an explicit preflight result")
    return _run_legacy_integrity_regressions(
        preflight_path,
        output_path,
        contract_path=contract_path,
        runtime_root=runtime_root,
    )


def write_result(path: Path, result: Mapping[str, Any]) -> None:
    path = Path(path).expanduser()
    if path.resolve(strict=False).is_relative_to(EVIDENCE_ROOT.resolve()):
        raise IntegrityError("PROTECTED_PATH", "result output must not be written inside preserved evidence")
    if path.is_symlink():
        raise IntegrityError("OUTPUT_SYMLINK", "result output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(result, default=decimal_json_default, indent=2, sort_keys=True) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, default=None, help="legacy source-preflight JSON result; only with --legacy-source")
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH, help="tracked CHG-109 machine-readable contract")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="ignored machine-readable current result")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT, help="ignored root for fresh probe output")
    parser.add_argument("--legacy-source", action="store_true", help="explicitly run the retained historical staged-source compatibility path")
    args = parser.parse_args(argv)
    preflight = args.preflight if args.preflight is not None else (DEFAULT_PREFLIGHT if args.legacy_source else None)
    result = run_integrity_regressions(preflight, contract_path=args.contract, runtime_root=args.runtime_root, legacy_source=args.legacy_source)
    try:
        write_result(args.output, result)
    except IntegrityError as exc:
        result = _blocked(result, exc)
    print(json.dumps(result, default=decimal_json_default, indent=2, sort_keys=True))
    return {"PASS": 0, "FAIL": 5, "NOT_RUN": 4, "NOT_IMPLEMENTED": 4, "BLOCKED": 4}.get(result["status"], 4)


if __name__ == "__main__":
    raise SystemExit(main())
