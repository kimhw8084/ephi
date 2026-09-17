#!/usr/bin/env python3
"""Bootstrap and qualify the exact CHG-105 NiceGUI Base W0 environment.

This tool intentionally uses only the Python standard library.  It validates the
interpreter and installed distribution identity before importing framework code,
so a locally installed package, a floating VCS revision, or a wrong NiceGUI
version fails closed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "environment/nicegui_base_runtime.json"
BINDING_PATH = ROOT / "evidence/review/nicegui_base_binding_manifest.json"
REQUIREMENTS_PATH = ROOT / "environment/nicegui_base_requirements.txt"
DEFAULT_VENV = ROOT / "artifacts/w0-runtime/venv"
DEFAULT_ARTIFACT_DIR = ROOT / "artifacts/w0-runtime"
MIN_PYTHON = (3, 11)
MAX_PYTHON_EXCLUSIVE = (3, 14)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def python_supported(major: int, minor: int) -> bool:
    version = (major, minor)
    return MIN_PYTHON <= version < MAX_PYTHON_EXCLUSIVE


def current_python_payload() -> dict[str, Any]:
    info = sys.version_info
    return {
        "executable": str(Path(sys.executable).resolve()),
        "version": platform.python_version(),
        "major": info.major,
        "minor": info.minor,
        "micro": info.micro,
        "prefix": str(Path(sys.prefix).resolve()),
        "base_prefix": str(Path(sys.base_prefix).resolve()),
        "isolated_venv": sys.prefix != sys.base_prefix,
        "supported": python_supported(info.major, info.minor),
        "required": ">=3.11,<3.14",
    }


def distribution_direct_url(dist: metadata.Distribution) -> dict[str, Any] | None:
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_invalid_json": True}
    return value if isinstance(value, dict) else {"_invalid_shape": True}


def validate_identity_values(
    spec: dict[str, Any],
    *,
    python_major: int,
    python_minor: int,
    framework_version: str | None,
    framework_direct_url: dict[str, Any] | None,
    nicegui_version: str | None,
) -> list[dict[str, str]]:
    """Pure identity validation used by the runtime and offline unit tests."""

    expected_framework = spec["framework"]
    issues: list[dict[str, str]] = []

    if not python_supported(python_major, python_minor):
        issues.append({
            "code": "PYTHON_VERSION_OUT_OF_RANGE",
            "detail": f"Python {python_major}.{python_minor} is outside >=3.11,<3.14",
        })
    if framework_version != expected_framework["version"]:
        issues.append({
            "code": "FRAMEWORK_VERSION_MISMATCH",
            "detail": f"Expected nicegui-base {expected_framework['version']}, found {framework_version or 'missing'}",
        })
    if nicegui_version != spec["dependencies"][0].split("==", 1)[1]:
        issues.append({
            "code": "NICEGUI_VERSION_MISMATCH",
            "detail": f"Expected NiceGUI 3.15.0, found {nicegui_version or 'missing'}",
        })

    if not isinstance(framework_direct_url, dict):
        issues.append({
            "code": "FRAMEWORK_SOURCE_IDENTITY_UNAVAILABLE",
            "detail": "nicegui-base direct_url.json is absent; exact VCS provenance cannot be established",
        })
        return issues

    if framework_direct_url.get("_invalid_json") or framework_direct_url.get("_invalid_shape"):
        issues.append({
            "code": "FRAMEWORK_SOURCE_IDENTITY_INVALID",
            "detail": "nicegui-base direct_url.json is not a valid object",
        })
        return issues

    if framework_direct_url.get("url") != expected_framework["repository"]:
        issues.append({
            "code": "FRAMEWORK_REPOSITORY_MISMATCH",
            "detail": f"Expected VCS URL {expected_framework['repository']}, found {framework_direct_url.get('url')!r}",
        })
    vcs_info = framework_direct_url.get("vcs_info")
    if not isinstance(vcs_info, dict) or vcs_info.get("vcs") != "git":
        issues.append({
            "code": "FRAMEWORK_VCS_IDENTITY_MISMATCH",
            "detail": "nicegui-base direct_url.json does not identify a Git checkout",
        })
    elif vcs_info.get("commit_id") != expected_framework["commit"]:
        issues.append({
            "code": "FRAMEWORK_COMMIT_MISMATCH",
            "detail": f"Expected commit {expected_framework['commit']}, found {vcs_info.get('commit_id')!r}",
        })
    elif vcs_info.get("requested_revision") != expected_framework["commit"]:
        issues.append({
            "code": "FRAMEWORK_REQUESTED_REVISION_MISMATCH",
            "detail": f"Expected requested revision {expected_framework['commit']}, found {vcs_info.get('requested_revision')!r}",
        })
    return issues


def authority_names(binding: dict[str, Any]) -> list[str]:
    first_slice = binding["first_slice_binding"]
    names: list[str] = []
    for row in first_slice["patterns"]:
        names.append(row["public_symbol"])
    for section in ("public_symbols", "shared_state_authorities", "runtime_authorities"):
        for row in first_slice[section]:
            names.append(row["name"])
    return list(dict.fromkeys(names))


def import_public_authorities(binding: dict[str, Any]) -> dict[str, Any]:
    names = authority_names(binding)
    results: list[dict[str, Any]] = []
    for name in names:
        try:
            namespace: dict[str, Any] = {}
            exec(f"from nicegui_base import {name}", namespace, namespace)
            results.append({"name": name, "status": "PASS", "module": namespace[name].__module__})
        except Exception as exc:  # Import qualification must record the exact failure.
            results.append({
                "name": name,
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
            })
    return {
        "status": "PASS" if all(row["status"] == "PASS" for row in results) else "FAIL",
        "authority_count": len(results),
        "imports": results,
        "import_rule": "Each authority was imported from nicegui_base public exports; no direct nicegui.ui or private integration import was used.",
    }


def current_check(spec: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    python = current_python_payload()
    framework_version: str | None = None
    framework_url: dict[str, Any] | None = None
    nicegui_version: str | None = None
    framework_location: str | None = None
    nicegui_location: str | None = None
    distribution_errors: list[dict[str, str]] = []

    try:
        framework = metadata.distribution(spec["framework"]["distribution"])
        framework_version = framework.version
        framework_url = distribution_direct_url(framework)
        framework_location = str(framework.locate_file(""))
    except metadata.PackageNotFoundError:
        distribution_errors.append({"code": "FRAMEWORK_NOT_INSTALLED", "detail": "nicegui-base distribution is not installed"})
    try:
        nicegui = metadata.distribution("nicegui")
        nicegui_version = nicegui.version
        nicegui_location = str(nicegui.locate_file(""))
    except metadata.PackageNotFoundError:
        distribution_errors.append({"code": "NICEGUI_NOT_INSTALLED", "detail": "nicegui distribution is not installed"})

    issues = distribution_errors + validate_identity_values(
        spec,
        python_major=python["major"],
        python_minor=python["minor"],
        framework_version=framework_version,
        framework_direct_url=framework_url,
        nicegui_version=nicegui_version,
    )
    if not python["isolated_venv"]:
        issues.append({
            "code": "VIRTUAL_ENV_REQUIRED",
            "detail": "The installed-runtime check must run inside the bootstrap-created isolated virtual environment",
        })
    imports: dict[str, Any]
    if issues:
        imports = {
            "status": "NOT_RUN",
            "reason": "Environment identity failed closed before framework imports",
            "authority_count": len(authority_names(binding)),
            "imports": [],
        }
    else:
        try:
            root_module = importlib.import_module("nicegui_base")
            imports = import_public_authorities(binding)
            imports["root_module"] = str(Path(root_module.__file__).resolve())
        except Exception as exc:
            imports = {
                "status": "FAIL",
                "reason": f"nicegui_base root import failed: {type(exc).__name__}: {exc}",
                "authority_count": len(authority_names(binding)),
                "imports": [],
            }
            issues.append({"code": "NICEGUI_BASE_IMPORT_FAILED", "detail": imports["reason"]})

    return {
        "status": "PASS" if not issues and imports["status"] == "PASS" else "FAIL",
        "python": python,
        "framework": {
            "distribution": spec["framework"]["distribution"],
            "installed_version": framework_version,
            "expected_version": spec["framework"]["version"],
            "direct_url": framework_url,
            "location": framework_location,
        },
        "nicegui": {
            "installed_version": nicegui_version,
            "expected_version": "3.15.0",
            "location": nicegui_location,
        },
        "issues": issues,
        "public_authority_imports": imports,
        "application_authority_rule": spec["application_authority_rule"],
    }


def venv_python(venv: Path) -> Path:
    candidate = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return candidate


def venv_executable(venv: Path, name: str) -> Path:
    return venv / (f"Scripts/{name}.exe" if os.name == "nt" else f"bin/{name}")


def command_record(
    command: list[str],
    *,
    cwd: Path,
    result: subprocess.CompletedProcess[str] | None = None,
    status: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    stdout = result.stdout if result else ""
    stderr = result.stderr if result else ""
    record: dict[str, Any] = {
        "command": shlex.join(command),
        "argv": command,
        "cwd": str(cwd),
        "status": status or ("PASS" if result and result.returncode == 0 else "FAIL"),
        "exit_code": result.returncode if result else None,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_sha256": sha256_text(stdout),
        "stderr_sha256": sha256_text(stderr),
    }
    if reason:
        record["reason"] = reason
    try:
        record["parsed_stdout"] = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        record["parsed_stdout"] = None
    return record


def run_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


DISCOVERY_COMMANDS = (
    ("agent_context", ["nicegui-base", "agent-context", "EPHI Attention -> Episode", "--format", "json"]),
    ("catalog_search", ["nicegui-base", "catalog-search", "engineering attention episode workspace", "--format", "json"]),
    ("recommend_pattern", ["nicegui-base", "recommend-pattern", "EPHI engineering attention and investigation", "--format", "json"]),
    ("recommend_visualization", [
        "nicegui-base", "recommend-visualization", "episode timeline and process trend",
        "--schema", "timestamp", "--schema", "measurement", "--format", "json",
    ]),
    ("catalog_audit", ["nicegui-base", "catalog-audit", "--format", "json"]),
    ("scaffold_plan", ["nicegui-base", "scaffold-plan", "EPHI Attention -> Episode", "--format", "json"]),
)


QUALIFICATION_COMMANDS = (
    ("agent_check", ["nicegui-base", "agent-check", "."], ROOT),
    ("gate", ["nicegui-base", "gate", "."], ROOT),
    ("runtime_contract", ["nicegui-base", "runtime-contract"], ROOT),
    ("runtime_smoke", ["nicegui-base", "runtime-smoke", "--port", "0"], DEFAULT_ARTIFACT_DIR / "runtime-smoke-workspace"),
)


def run_suite(
    venv: Path,
    *,
    commands: tuple[tuple[str, list[str]], ...],
    output_path: Path,
    qualification: bool = False,
) -> dict[str, Any]:
    spec = load_json(SPEC_PATH)
    binding = load_json(BINDING_PATH)
    check_command = [str(venv_python(venv)), str(Path(__file__).resolve()), "check", "--current"]
    check_result = run_command(check_command, cwd=ROOT)
    try:
        check_report = json.loads(check_result.stdout)
    except json.JSONDecodeError:
        check_report = {"status": "FAIL", "raw_stdout": check_result.stdout, "stderr": check_result.stderr}
    report: dict[str, Any] = {
        "schema_version": 1,
        "change": "CHG-105",
        "status": "NOT_RUN" if check_report.get("status") != "PASS" else "PASS",
        "environment_check": {
            "command": shlex.join(check_command),
            "status": "PASS" if check_result.returncode == 0 else "FAIL",
            "exit_code": check_result.returncode,
            "report": check_report,
        },
        "commands": [],
    }
    if check_result.returncode != 0:
        for name, command in commands:
            report["commands"].append({
                "name": name,
                **command_record(command, cwd=ROOT, status="NOT_RUN", reason="Pinned environment check failed closed"),
            })
    else:
        for name, command in commands:
            cwd = ROOT
            if qualification:
                cwd = next(item[2] for item in QUALIFICATION_COMMANDS if item[0] == name)
                cwd.mkdir(parents=True, exist_ok=True)
            executable = venv_executable(venv, command[0])
            actual = [str(executable), *command[1:]]
            try:
                result = run_command(actual, cwd=cwd)
                record = command_record(command, cwd=cwd, result=result)
                if result.returncode != 0 and ("unrecognized arguments" in result.stderr or "invalid choice" in result.stderr):
                    record["status"] = "NOT_SUPPORTED"
                report["commands"].append({"name": name, **record})
            except FileNotFoundError as exc:
                report["commands"].append({
                    "name": name,
                    **command_record(command, cwd=cwd, status="UNAVAILABLE", reason=f"Executable unavailable: {exc}"),
                })
    statuses = [row["status"] for row in report["commands"]]
    if check_result.returncode != 0:
        report["status"] = "BLOCKED"
    elif statuses and all(status == "PASS" for status in statuses):
        report["status"] = "PASS"
    else:
        report["status"] = "FAIL"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(json_bytes(report))
    return report


def bootstrap(venv: Path) -> tuple[int, dict[str, Any]]:
    spec = load_json(SPEC_PATH)
    report: dict[str, Any] = {
        "schema_version": 1,
        "change": "CHG-105",
        "status": "BLOCKED",
        "python": current_python_payload(),
        "venv": str(venv),
        "commands": [],
    }
    if not report["python"]["supported"]:
        report["reason"] = "Bootstrap interpreter is outside >=3.11,<3.14; use python3.11, python3.12, or python3.13 explicitly."
        return 2, report
    if not REQUIREMENTS_PATH.is_file():
        report["reason"] = f"Requirements file missing: {REQUIREMENTS_PATH}"
        return 2, report

    if not venv_python(venv).exists():
        command = [sys.executable, "-m", "venv", str(venv)]
        result = run_command(command, cwd=ROOT)
        report["commands"].append(command_record(command, cwd=ROOT, result=result))
        if result.returncode != 0:
            return 1, report
    install = [
        str(venv_python(venv)), "-m", "pip", "install", "--disable-pip-version-check",
        "--no-cache-dir", "--requirement", str(REQUIREMENTS_PATH),
    ]
    result = run_command(install, cwd=ROOT)
    report["commands"].append(command_record(install, cwd=ROOT, result=result))
    if result.returncode != 0:
        return 1, report

    check_command = [str(venv_python(venv)), str(Path(__file__).resolve()), "check", "--current"]
    check_result = run_command(check_command, cwd=ROOT)
    report["commands"].append(command_record(check_command, cwd=ROOT, result=check_result))
    try:
        check = json.loads(check_result.stdout)
    except json.JSONDecodeError:
        check = {"status": "FAIL", "raw_stdout": check_result.stdout, "stderr": check_result.stderr}
    report["environment_check"] = check
    report["status"] = "PASS" if check.get("status") == "PASS" else "FAIL"
    return (0 if report["status"] == "PASS" else 1), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("bootstrap", "check", "discover", "qualify"))
    parser.add_argument("--venv", type=Path, default=DEFAULT_VENV)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--current", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.command == "bootstrap":
        code, report = bootstrap(args.venv)
        output = args.output or DEFAULT_ARTIFACT_DIR / "bootstrap.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(json_bytes(report))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return code

    if args.command == "check" and args.current:
        report = current_check(load_json(SPEC_PATH), load_json(BINDING_PATH))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["status"] == "PASS" else 1

    if args.command == "check":
        check_command = [str(venv_python(args.venv)), str(Path(__file__).resolve()), "check", "--current"]
        result = run_command(check_command, cwd=ROOT)
        if result.stdout:
            print(result.stdout, end="")
        elif result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        return result.returncode

    if args.command == "discover":
        output = args.output or DEFAULT_ARTIFACT_DIR / "discovery.json"
        report = run_suite(args.venv, commands=DISCOVERY_COMMANDS, output_path=output)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["status"] == "PASS" else 1

    output = args.output or DEFAULT_ARTIFACT_DIR / "qualification.json"
    report = run_suite(
        args.venv,
        commands=tuple((name, command) for name, command, _ in QUALIFICATION_COMMANDS),
        output_path=output,
        qualification=True,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
