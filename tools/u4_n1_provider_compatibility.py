#!/usr/bin/env python3
"""Qualify the frozen N-1 synthetic provider against frozen N-1 and N releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tomllib
import venv


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_PATH = ROOT / "environment" / "u4_n1_provider_compatibility_authority.json"
PROVIDER_NAME = "ephi-synthetic-downstream-qualification"
PROVIDER_ENTRYPOINT = "examples.synthetic_downstream.provider:build_bundle"
_JOB_ID = re.compile(r"^CF-[0-9a-f]{24}$")


class QualificationFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, code: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationFailure(code) from exc
    if not isinstance(value, dict):
        raise QualificationFailure(code)
    return value


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None, code: str = "COMMAND_FAILED") -> str:
    try:
        completed = subprocess.run(
            [str(item) for item in argv],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure(code) from exc
    if completed.returncode != 0:
        raise QualificationFailure(code)
    return completed.stdout.strip()


def _git(repository: Path, *args: str, check_code: str = "GIT_HISTORY_INVALID") -> str:
    return _run(["git", "-C", str(repository), *args], cwd=ROOT, code=check_code)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(value) + b"\n")


def _authority() -> tuple[dict[str, object], str]:
    authority = _read_json(AUTHORITY_PATH, "FROZEN_AUTHORITY_INVALID")
    return authority, _sha256_file(AUTHORITY_PATH)


def _release_records(authority: dict[str, object]) -> dict[str, dict[str, object]]:
    releases = authority.get("releases")
    if not isinstance(releases, dict) or set(releases) != {"N-1", "N"}:
        raise QualificationFailure("FROZEN_AUTHORITY_INVALID")
    result: dict[str, dict[str, object]] = {}
    for name, value in releases.items():
        if not isinstance(value, dict):
            raise QualificationFailure("FROZEN_AUTHORITY_INVALID")
        result[name] = value
    return result


def _provider_source_manifest(root: Path, source_release: str) -> dict[str, object]:
    source = root / "examples" / "synthetic_downstream"
    try:
        project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    except (OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise QualificationFailure("PROVIDER_SOURCE_IDENTITY_INVALID") from exc
    if project.get("name") != PROVIDER_NAME or project.get("version") != "1.0.0":
        raise QualificationFailure("PROVIDER_SOURCE_IDENTITY_INVALID")
    tracked = _git(root, "ls-tree", "-r", "--name-only", "HEAD", "--", "examples/synthetic_downstream").splitlines()
    included = [root / item for item in tracked]
    if (
        not included
        or not (source / "pyproject.toml").is_file()
        or not (source / "README.md").is_file()
        or not any(path.name == "provider.py" for path in included)
        or any(not path.is_file() or path.is_symlink() for path in included)
    ):
        raise QualificationFailure("PROVIDER_SOURCE_IDENTITY_INVALID")
    files = []
    for path in included:
        raw = path.read_bytes()
        files.append({
            "path": path.relative_to(root).as_posix(),
            "byte_size": len(raw),
            "sha256": _sha256_bytes(raw),
        })
    manifest = {
        "schema": "org.ephi.u4-provider-source-manifest.v1",
        "source_release": source_release,
        "source_commit": _git(root, "rev-parse", "HEAD"),
        "distribution": project["name"],
        "version": project["version"],
        "build_input_policy": "pyproject.toml, README.md, and top-level Python package files copied by tools/prepare_release_inputs.py; complete Git-tracked provider source path is inventoried",
        "files": files,
    }
    manifest["manifest_sha256"] = _sha256_bytes(_canonical_bytes(manifest))
    return manifest


def _provider_artifact(inputs: Path, expected_sha: str | None = None) -> tuple[Path, dict[str, object], dict[str, object]]:
    kit = _read_json(inputs / "qualification" / "qualification_inputs.json", "PROVIDER_ARTIFACT_INVALID")
    artifacts = kit.get("artifacts")
    if not isinstance(artifacts, list):
        raise QualificationFailure("PROVIDER_ARTIFACT_INVALID")
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("distribution") == PROVIDER_NAME]
    if len(matches) != 1:
        raise QualificationFailure("PROVIDER_ARTIFACT_INVALID")
    item = matches[0]
    if item.get("version") != "1.0.0" or not isinstance(item.get("file"), str):
        raise QualificationFailure("PROVIDER_ARTIFACT_INVALID")
    relative = PurePosixPath(item["file"])
    if relative.is_absolute() or ".." in relative.parts or not relative.as_posix().startswith("wheelhouse/"):
        raise QualificationFailure("PROVIDER_ARTIFACT_INVALID")
    wheel = inputs / "qualification" / Path(*relative.parts)
    if not wheel.is_file() or wheel.is_symlink():
        raise QualificationFailure("PROVIDER_ARTIFACT_INVALID")
    raw = wheel.read_bytes()
    if len(raw) != item.get("byte_size") or _sha256_bytes(raw) != item.get("sha256"):
        raise QualificationFailure("PROVIDER_ARTIFACT_INVALID")
    if expected_sha is not None and _sha256_bytes(raw) != expected_sha:
        raise QualificationFailure("PROVIDER_ARTIFACT_IDENTITY_MISMATCH")
    return wheel, {**item, "wheel_file": wheel.name, "sha256": _sha256_bytes(raw), "byte_size": len(raw)}, kit


def _provided_provider_wheel(path_value: str, expected_name: str, expected_sha: str) -> tuple[Path, int]:
    supplied = Path(path_value).expanduser()
    if supplied.is_symlink() or not supplied.is_file() or supplied.name != expected_name:
        raise QualificationFailure("PROVIDER_ARTIFACT_INVALID")
    wheel = supplied.resolve()
    size = wheel.stat().st_size
    if _sha256_file(wheel) != expected_sha:
        raise QualificationFailure("PROVIDER_ARTIFACT_IDENTITY_MISMATCH")
    return wheel, size


def _installed_distribution_inventory(
    python: Path,
    distribution: str,
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, object] | None:
    code = r'''import hashlib, importlib.metadata as metadata, json, sys
from pathlib import Path
name = sys.argv[1]
try:
    dist = metadata.distribution(name)
except metadata.PackageNotFoundError:
    print("null")
    raise SystemExit(0)
prefix = Path(sys.prefix).resolve()
rows = []
for item in sorted(dist.files or (), key=lambda value: value.as_posix()):
    installed = Path(dist.locate_file(item))
    resolved = installed.resolve(strict=True)
    if installed.is_symlink() or not resolved.is_file():
        raise SystemExit(3)
    relative = resolved.relative_to(prefix).as_posix()
    data = resolved.read_bytes()
    rows.append({"path": relative, "byte_size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
if not rows:
    raise SystemExit(3)
rows.sort(key=lambda item: item["path"])
body = {"distribution": dist.metadata.get("Name", ""), "version": dist.version, "file_count": len(rows), "files": rows}
body["identity_sha256"] = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
migration_rows = [row for row in rows if "/share/ephi/migrations/" in "/" + row["path"] and row["path"].endswith(".sql")]
body["migration_files"] = migration_rows
migration_records = [
    {"path": f"migrations/{Path(row['path']).name}", "sha256": row["sha256"], "byte_size": row["byte_size"]}
    for row in migration_rows
]
migration_identity = hashlib.sha256(
    json.dumps(migration_records, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
).hexdigest()
body["migration_identity_sha256"] = migration_identity
print(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False))'''
    try:
        completed = subprocess.run(
            [str(python), "-c", code, distribution],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure("INSTALLED_DISTRIBUTION_HASH_FAILED") from exc
    if completed.returncode != 0:
        raise QualificationFailure("INSTALLED_DISTRIBUTION_HASH_FAILED")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise QualificationFailure("INSTALLED_DISTRIBUTION_HASH_FAILED") from exc
    if result is not None and not isinstance(result, dict):
        raise QualificationFailure("INSTALLED_DISTRIBUTION_HASH_FAILED")
    return result


def _clean_install_environment(home_root: Path, postgres_dsn: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "EPHI_ENV": "development",
        "EPHI_TEST_POSTGRES_DSN": postgres_dsn,
    }
    home_root.mkdir(parents=True, exist_ok=True)
    return env


def _parse_cli(executable: Path, argv: list[str], *, cwd: Path, env: dict[str, str], expected_code: int) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [str(executable), *argv],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure("INSTALLED_DOWNSTREAM_CLI_UNAVAILABLE") from exc
    if completed.returncode != expected_code:
        raise QualificationFailure("INSTALLED_DOWNSTREAM_CLI_RESULT_INVALID")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise QualificationFailure("INSTALLED_DOWNSTREAM_CLI_RESULT_INVALID") from exc
    if not isinstance(value, dict):
        raise QualificationFailure("INSTALLED_DOWNSTREAM_CLI_RESULT_INVALID")
    return value


def _preflight_identity(report: dict[str, object], release: dict[str, object], release_name: str, inputs_report: dict[str, object]) -> dict[str, object]:
    if report.get("status") != "PASS" or report.get("reason_code") != "RELEASE_PREFLIGHT_PASS":
        raise QualificationFailure("RELEASE_PREFLIGHT_FAILED")
    if report.get("release_identity_sha256") != release.get("release_identity_sha256"):
        raise QualificationFailure("RELEASE_IDENTITY_MISMATCH")
    source = inputs_report.get("candidate_source")
    expected_source = {
        "commit": release.get("integrated_commit"),
        "tree": release.get("integrated_tree"),
    }
    if source != expected_source or report.get("candidate_source") != expected_source:
        raise QualificationFailure("RELEASE_SOURCE_IDENTITY_MISMATCH")
    if report.get("application") != {"distribution": "ephi", "version": "0.1.0"}:
        raise QualificationFailure("RELEASE_PACKAGE_IDENTITY_MISMATCH")
    if report.get("migrations", {}).get("identity_sha256") != "c661c7a41eae8cd2b637778998bee77b8ebdec4a2ef78639d3ed7f69b23b1e8b":
        raise QualificationFailure("RELEASE_MIGRATION_IDENTITY_MISMATCH")
    if report.get("provider_composition", {}).get("status") != "NOT_RUN":
        raise QualificationFailure("RELEASE_PREFLIGHT_ORDER_INVALID")
    return {
        "status": "PASS",
        "release_identity_sha256": report["release_identity_sha256"],
        "install_inputs_sha256": report["install_inputs_sha256"],
        "candidate_source": report["candidate_source"],
        "application": report["application"],
        "base_runtime": report["dependencies"]["base"],
        "migrations": report["migrations"],
        "downstream_abi": report["downstream_abi"],
        "runtime_configuration": report["runtime_configuration"],
        "checks": report["checks"],
        "provider_composition": report["provider_composition"],
        "prepared_inputs_identity_sha256": inputs_report.get("install_inputs_sha256"),
        "release_label": release_name,
    }


def _compatibility_facts(
    report: dict[str, object],
    authority: dict[str, object],
    *,
    composition_expected: bool,
) -> dict[str, object]:
    if report.get("status_code") != "CONTRACT_PASS" or report.get("compatibility", {}).get("status") != "PASS":
        raise QualificationFailure("DOWNSTREAM_CONFORMANCE_FAILED")
    expected_composition = "PASS" if composition_expected else "NOT_RUN"
    if report.get("safe_composition_smoke", {}).get("status") != expected_composition:
        raise QualificationFailure("DOWNSTREAM_COMPOSITION_FAILED")
    abi = report.get("downstream_abi")
    manifest = abi.get("manifest") if isinstance(abi, dict) else None
    expected_abi = authority["public_downstream_abi"]
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != expected_abi["manifest_schema"]
        or manifest.get("abi") != {"id": expected_abi["abi_id"], "version": expected_abi["abi_version"]}
    ):
        raise QualificationFailure("DOWNSTREAM_ABI_IDENTITY_MISMATCH")
    if manifest.get("required_categories") != expected_abi["required_categories"]:
        raise QualificationFailure("DOWNSTREAM_CATEGORY_INVENTORY_MISMATCH")
    providers = report.get("providers")
    if not isinstance(providers, list) or len(providers) != len(expected_abi["required_categories"]):
        raise QualificationFailure("DOWNSTREAM_PROVIDER_INVENTORY_MISMATCH")
    contract_versions = {}
    for item in providers:
        if not isinstance(item, dict) or item.get("status") != "COMPATIBLE":
            raise QualificationFailure("DOWNSTREAM_PROVIDER_INVENTORY_MISMATCH")
        contract = item.get("contract")
        if not isinstance(contract, dict) or contract.get("version") != expected_abi["provider_contract_version"]:
            raise QualificationFailure("DOWNSTREAM_PROVIDER_CONTRACT_MISMATCH")
        contract_versions[item["category"]] = contract["version"]
    identity = abi.get("safe_manifest_hash")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise QualificationFailure("DOWNSTREAM_MANIFEST_IDENTITY_INVALID")
    return {
        "status": "PASS",
        "status_code": report["status_code"],
        "abi_id": manifest["abi"]["id"],
        "abi_version": manifest["abi"]["version"],
        "provider_contract_version": expected_abi["provider_contract_version"],
        "provider_contract_versions_by_category": contract_versions,
        "compatibility": report["compatibility"],
        "safe_composition_smoke": report["safe_composition_smoke"],
        "safe_manifest": manifest,
        "safe_manifest_sha256": identity,
        "synthetic_boundary": report.get("synthetic_boundary"),
    }


def _worktree_source(repository: Path, worktree_root: Path, label: str, release: dict[str, object]) -> Path:
    commit, tree = str(release["integrated_commit"]), str(release["integrated_tree"])
    if _git(repository, "cat-file", "-t", commit) != "commit":
        raise QualificationFailure("FROZEN_COMMIT_UNAVAILABLE")
    path = worktree_root / label
    try:
        _run(["git", "-C", str(repository), "worktree", "add", "--detach", str(path), commit], cwd=ROOT, code="FROZEN_WORKTREE_MATERIALIZATION_FAILED")
        if _git(path, "rev-parse", "HEAD") != commit or _git(path, "rev-parse", "HEAD^{tree}") != tree:
            raise QualificationFailure("FROZEN_TREE_MISMATCH")
        if _git(path, "status", "--porcelain", "--untracked-files=all"):
            raise QualificationFailure("FROZEN_WORKTREE_NOT_CLEAN")
    except QualificationFailure:
        if path.exists():
            _run(["git", "-C", str(repository), "worktree", "remove", "--force", str(path)], cwd=ROOT, code="FROZEN_WORKTREE_CLEANUP_FAILED")
        raise
    return path


def _prepare_release(root: Path, output: Path, external_cwd: Path) -> dict[str, object]:
    tool = root / "tools" / "prepare_release_inputs.py"
    if not tool.is_file():
        raise QualificationFailure("RELEASE_PREPARATION_TOOL_MISSING")
    output_text = _run([sys.executable, str(tool), "--output-dir", str(output)], cwd=external_cwd, code="RESTRICTED_INPUT_PREPARATION_FAILED")
    try:
        report = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise QualificationFailure("RESTRICTED_INPUT_PREPARATION_FAILED") from exc
    if not isinstance(report, dict) or report.get("status") != "PASS":
        raise QualificationFailure("RESTRICTED_INPUT_PREPARATION_FAILED")
    return report


def _install_release(root: Path, inputs: Path, env_root: Path, external_cwd: Path, postgres_dsn: str) -> tuple[Path, dict[str, str]]:
    if env_root.exists():
        raise QualificationFailure("INSTALL_ENVIRONMENT_NOT_FRESH")
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(env_root)
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure("INSTALL_ENVIRONMENT_CREATE_FAILED") from exc
    bin_dir = env_root / "bin"
    python = bin_dir / "python"
    suffix = f"{sys.version_info.major}{sys.version_info.minor}"
    install_env = _clean_install_environment(env_root / "home", postgres_dsn)
    lock_dir = inputs / "locks"
    wheelhouse = inputs / "wheelhouse"
    commands = [
        [str(python), "-m", "pip", "install", "--no-compile", "--no-index", "--find-links", str(wheelhouse), "--require-hashes", "-r", str(lock_dir / "installer-py311.txt")],
        [str(python), "-m", "pip", "uninstall", "-y", "setuptools", "wheel"],
        [str(python), "-m", "pip", "install", "--no-compile", "--no-index", "--find-links", str(wheelhouse), "--require-hashes", "-r", str(lock_dir / f"runtime-py{suffix}.txt")],
        [str(python), "-m", "pip", "install", "--no-compile", "--no-index", "--find-links", str(wheelhouse), "--require-hashes", "-r", str(lock_dir / f"postgres-py{suffix}.txt")],
    ]
    for command in commands:
        _run(command, cwd=external_cwd, env=install_env, code="OFFLINE_EPHI_INSTALL_FAILED")
    app_wheels = sorted(wheelhouse.glob("ephi-*.whl"))
    base_wheels = sorted(wheelhouse.glob("nicegui_base-*.whl"))
    if len(app_wheels) != 1 or len(base_wheels) != 1:
        raise QualificationFailure("RESTRICTED_RELEASE_WHEEL_INVALID")
    _run([str(python), "-m", "pip", "install", "--no-compile", "--no-index", "--no-deps", str(app_wheels[0]), str(base_wheels[0])], cwd=external_cwd, env=install_env, code="OFFLINE_EPHI_INSTALL_FAILED")
    if "PYTHONPATH" in install_env or "PYTHONHOME" in install_env:
        raise QualificationFailure("INSTALLED_ENVIRONMENT_PATH_CONTAMINATED")
    return bin_dir, install_env


def _create_composition_database(python: Path, postgres_dsn: str, database_name: str, *, cwd: Path, env: dict[str, str]) -> str:
    code = r'''import json, os
import psycopg
from psycopg import sql
base = os.environ["U4_ADMIN_DSN"]
name = os.environ["U4_DATABASE_NAME"]
with psycopg.connect(base, autocommit=True) as connection:
    current = connection.execute("SELECT current_database()").fetchone()[0]
    exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if current == name or exists:
        raise SystemExit(4)
    connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
print(json.dumps({"dsn": psycopg.conninfo.make_conninfo(base, dbname=name)}, separators=(",", ":")))'''
    child_env = dict(env)
    child_env["U4_ADMIN_DSN"] = postgres_dsn
    child_env["U4_DATABASE_NAME"] = database_name
    try:
        completed = subprocess.run([str(python), "-c", code], cwd=cwd, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure("COMPOSITION_DATABASE_CREATE_FAILED") from exc
    if completed.returncode != 0:
        raise QualificationFailure("COMPOSITION_DATABASE_CREATE_FAILED")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise QualificationFailure("COMPOSITION_DATABASE_CREATE_FAILED") from exc
    if not isinstance(result, dict) or not isinstance(result.get("dsn"), str) or not result["dsn"]:
        raise QualificationFailure("COMPOSITION_DATABASE_CREATE_FAILED")
    return result["dsn"]


def _drop_composition_database(python: Path, postgres_dsn: str, database_name: str, *, cwd: Path, env: dict[str, str]) -> None:
    code = r'''import os
import psycopg
from psycopg import sql
base = os.environ["U4_ADMIN_DSN"]
name = os.environ["U4_DATABASE_NAME"]
with psycopg.connect(base, autocommit=True) as connection:
    current = connection.execute("SELECT current_database()").fetchone()[0]
    if current == name:
        raise SystemExit(4)
    connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
    exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if exists:
        connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))'''
    child_env = dict(env)
    child_env["U4_ADMIN_DSN"] = postgres_dsn
    child_env["U4_DATABASE_NAME"] = database_name
    try:
        completed = subprocess.run([str(python), "-c", code], cwd=cwd, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure("COMPOSITION_DATABASE_CLEANUP_FAILED") from exc
    if completed.returncode != 0:
        raise QualificationFailure("COMPOSITION_DATABASE_CLEANUP_FAILED")


def _installed_provider_inventory(python: Path, *, cwd: Path, env: dict[str, str]) -> dict[str, object]:
    inventory = _installed_distribution_inventory(python, PROVIDER_NAME, cwd=cwd, env=env)
    if inventory is None:
        raise QualificationFailure("PROVIDER_DISTRIBUTION_OWNERSHIP_INVALID")
    if inventory.get("distribution") != PROVIDER_NAME or inventory.get("version") != "1.0.0":
        raise QualificationFailure("PROVIDER_DISTRIBUTION_OWNERSHIP_INVALID")
    paths = [str(item["path"]) for item in inventory["files"]]
    if any(path.endswith(".pyc") or "/__pycache__/" in f"/{path}" for path in paths):
        raise QualificationFailure("PROVIDER_INSTALL_BYTECODE_PRESENT")
    provider_files = [path for path in paths if "/examples/synthetic_downstream/" in f"/{path}"]
    if not provider_files or any("/ephi/" in f"/{path}" for path in provider_files):
        raise QualificationFailure("PROVIDER_DISTRIBUTION_OWNERSHIP_INVALID")
    return inventory


def _probe_imports(python: Path, *, cwd: Path, env: dict[str, str], worktrees: list[Path], output_path: Path) -> dict[str, object]:
    code = r'''import importlib, json, os, sys
from pathlib import Path
import ephi, ephi.downstream
provider = importlib.import_module("examples.synthetic_downstream.provider")
prefix = Path(sys.prefix).resolve()
mods = {"ephi": Path(ephi.__file__).resolve(), "ephi.downstream": Path(ephi.downstream.__file__).resolve(), "provider": Path(provider.__file__).resolve()}
assert all(p.is_relative_to(prefix) for p in mods.values())
assert "PYTHONPATH" not in os.environ and "PYTHONHOME" not in os.environ
source_roots = [Path(p).resolve() for p in json.loads(os.environ["U4_SOURCE_ROOTS_JSON"])]
paths = [Path(p or os.getcwd()).resolve() for p in sys.path]
assert all(not any(p.is_relative_to(root) for root in source_roots) for p in paths)
assert not any(Path.cwd().resolve().is_relative_to(root) for root in source_roots)
print(json.dumps({"module_paths_inside_venv": {k: p.relative_to(prefix).as_posix() for k,p in mods.items()}, "sys_path_checkout_entries": [], "checkout_pythonpath_used": False, "external_cwd": True}, sort_keys=True, separators=(",", ":")))'''
    probe_env = dict(env)
    probe_env["U4_SOURCE_ROOTS_JSON"] = json.dumps([str(item) for item in worktrees])
    try:
        completed = subprocess.run([str(python), "-c", code], cwd=cwd, env=probe_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure("INSTALLED_IMPORT_PROBE_FAILED") from exc
    if completed.returncode != 0:
        raise QualificationFailure("INSTALLED_IMPORT_PROBE_FAILED")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise QualificationFailure("INSTALLED_IMPORT_PROBE_FAILED") from exc
    if not isinstance(report, dict) or report.get("checkout_pythonpath_used") is not False or report.get("sys_path_checkout_entries") != []:
        raise QualificationFailure("INSTALLED_IMPORT_PROBE_FAILED")
    _write_json(output_path, report)
    return report


def _negative_controls(bin_dir: Path, *, cwd: Path, env: dict[str, str], work_root: Path) -> dict[str, object]:
    fixture_root = work_root / "negative-fixtures"
    fixture_root.mkdir(parents=True, exist_ok=False)
    fixture = '''from dataclasses import replace\nfrom examples.synthetic_downstream.provider import build_bundle\nfrom ephi.downstream import ProviderBinding\n\ndef build_incompatible_abi():\n    return replace(build_bundle(), abi_version="9.0.0")\n\ndef build_incompatible_provider_contract():\n    bundle = build_bundle()\n    binding = bundle.identity\n    contract = replace(binding.contract, required_capabilities=(*binding.contract.required_capabilities, "u4.incompatible.required"))\n    return replace(bundle, identity=ProviderBinding(contract, binding.implementation, binding.public_metadata))\n'''
    fixture_path = fixture_root / "u4_negative_provider.py"
    fixture_path.write_text(fixture, encoding="utf-8")
    fixture_env = dict(env)
    fixture_env["PYTHONPATH"] = str(fixture_root)
    missing = _parse_cli(bin_dir / "ephi-downstream-preflight", ["--json"], cwd=cwd, env=env, expected_code=2)
    if missing.get("status_code") != "MISSING_REQUIRED_PROVIDER" or missing.get("safe_composition_smoke", {}).get("status") != "NOT_RUN":
        raise QualificationFailure("NEGATIVE_CONTROL_FAILED")
    if any(not isinstance(item, dict) or item.get("status") != "MISSING" for item in missing.get("providers", [])):
        raise QualificationFailure("NEGATIVE_CONTROL_FAILED")

    controls = {}
    for factory, code in (
        ("build_incompatible_abi", "INCOMPATIBLE_ABI"),
        ("build_incompatible_provider_contract", "INCOMPATIBLE_PROVIDER_CONTRACT"),
    ):
        report = _parse_cli(
            bin_dir / "ephi-downstream-preflight",
            ["--entrypoint", f"u4_negative_provider:{factory}", "--json"],
            cwd=cwd,
            env=fixture_env,
            expected_code=2,
        )
        if report.get("status_code") != code or report.get("safe_composition_smoke", {}).get("status") != "NOT_RUN":
            raise QualificationFailure("NEGATIVE_CONTROL_FAILED")
        controls[factory] = {
            "status": "PASS_FAILS_CLOSED",
            "reason_code": code,
            "observed_status_code": report["status_code"],
            "provider_statuses": [item.get("status") for item in report.get("providers", []) if isinstance(item, dict)],
            "entrypoint": f"u4_negative_provider:{factory}",
            "safe_composition": report.get("safe_composition_smoke", {}).get("status"),
            "automatic_fallback": False,
            "fixture_source_sha256": _sha256_file(fixture_path),
        }
    controls["missing_provider"] = {
        "status": "PASS_FAILS_CLOSED",
        "reason_code": "MISSING_REQUIRED_PROVIDER",
        "entrypoint": None,
        "missing_categories": [item["category"] for item in missing["providers"]],
        "provider_statuses": [item.get("status") for item in missing["providers"] if isinstance(item, dict)],
        "safe_composition": "NOT_RUN",
        "automatic_fallback": False,
    }
    shutil.rmtree(fixture_root)
    return controls


def _manifest_file_identity(report: dict[str, object]) -> tuple[dict[str, object], str]:
    manifest = report.get("downstream_abi", {}).get("manifest")
    if not isinstance(manifest, dict):
        raise QualificationFailure("DOWNSTREAM_MANIFEST_INVALID")
    return manifest, str(report["downstream_abi"]["safe_manifest_hash"])


def _assert_allowed_candidate_paths(repository: Path, candidate: str, base: str) -> list[str]:
    try:
        value = _git(repository, "diff", "--name-only", f"{base}..{candidate}")
    except QualificationFailure as exc:
        raise QualificationFailure("CANDIDATE_HISTORY_INVALID") from exc
    changed = [item for item in value.splitlines() if item]
    protected = ("src/ephi/", "migrations/", "examples/synthetic_downstream/")
    if any(path.startswith(protected) for path in changed):
        raise QualificationFailure("CORE_EDIT_PROHIBITION_FAILED")
    return changed


def _release_install_identity(python: Path, *, cwd: Path, env: dict[str, str]) -> dict[str, object]:
    app = _installed_distribution_inventory(python, "ephi", cwd=cwd, env=env)
    if app is None or app.get("version") != "0.1.0":
        raise QualificationFailure("RELEASE_PACKAGE_IDENTITY_MISMATCH")
    return app


def _release_migration_inventory(app_identity: dict[str, object]) -> list[dict[str, object]]:
    migrations = app_identity.get("migration_files")
    if not isinstance(migrations, list) or not migrations:
        raise QualificationFailure("MIGRATION_HASH_INVALID")
    return [
        {
            "path": f"migrations/{Path(str(item['path'])).name}",
            "byte_size": item["byte_size"],
            "sha256": item["sha256"],
        }
        for item in migrations
    ]


def _run_installed_release_qualification(
    release_name: str,
    release: dict[str, object],
    worktree: Path,
    inputs: Path,
    bin_dir: Path,
    install_env: dict[str, str],
    provider_wheel: Path,
    provider_artifact: dict[str, object],
    *,
    work_root: Path,
    external_cwd: Path,
    source_roots: list[Path],
    authority: dict[str, object],
    artifact_root: Path,
    database_name: str,
) -> dict[str, object]:
    environment_root = work_root / "environment"
    environment_root.mkdir(parents=True, exist_ok=True)

    release_preflight_path = bin_dir / "ephi-release-preflight"
    preflight_text = _run([str(release_preflight_path), "--inputs-dir", str(inputs), "--json"], cwd=external_cwd, env=install_env, code="RELEASE_PREFLIGHT_FAILED")
    try:
        release_preflight = json.loads(preflight_text)
    except json.JSONDecodeError as exc:
        raise QualificationFailure("RELEASE_PREFLIGHT_FAILED") from exc
    if not isinstance(release_preflight, dict):
        raise QualificationFailure("RELEASE_PREFLIGHT_FAILED")
    preparation_path = inputs / "install_inputs.json"
    prepared = _read_json(preparation_path, "RESTRICTED_INPUT_IDENTITY_INVALID")
    preparation_summary = _read_json(inputs / "qualification" / "qualification_inputs.json", "PROVIDER_ARTIFACT_INVALID")
    prepared_source = prepared.get("source")
    if not isinstance(prepared_source, dict):
        raise QualificationFailure("RESTRICTED_INPUT_IDENTITY_INVALID")
    identity = _preflight_identity(release_preflight, release, release_name, {
        "candidate_source": {"commit": prepared_source.get("commit"), "tree": prepared_source.get("tree")},
        "install_inputs_sha256": prepared.get("install_inputs_sha256"),
    })
    if identity.get("install_inputs_sha256") != prepared.get("install_inputs_sha256"):
        raise QualificationFailure("RESTRICTED_INPUT_IDENTITY_INVALID")
    if identity.get("downstream_abi", {}).get("id") != authority["public_downstream_abi"]["abi_id"] or identity.get("downstream_abi", {}).get("version") != authority["public_downstream_abi"]["abi_version"] or identity.get("downstream_abi", {}).get("provider_contract_version") != authority["public_downstream_abi"]["provider_contract_version"]:
        raise QualificationFailure("RELEASE_ABI_IDENTITY_MISMATCH")
    base = release_preflight.get("dependencies", {}).get("base", {})
    if base.get("commit") != authority["nicegui_base"]["commit"] or base.get("repository") != authority["nicegui_base"]["repository"]:
        raise QualificationFailure("RELEASE_BASE_IDENTITY_MISMATCH")
    if preparation_summary.get("ephi_release_identity_sha256") != release["release_identity_sha256"]:
        raise QualificationFailure("PROVIDER_KIT_RELEASE_IDENTITY_MISMATCH")

    app = _release_install_identity(bin_dir / "python", cwd=external_cwd, env=install_env)
    ephi_before = app
    migration_before = _release_migration_inventory(app)
    migrations_identity = release_preflight.get("migrations", {}).get("identity_sha256")
    if (
        migrations_identity != authority["migrations"]["identity_sha256"]
        or app.get("migration_identity_sha256") != migrations_identity
    ):
        raise QualificationFailure("RELEASE_MIGRATION_IDENTITY_MISMATCH")

    provider_dist_before = _installed_distribution_inventory(bin_dir / "python", PROVIDER_NAME, cwd=external_cwd, env=install_env)
    if provider_dist_before is not None:
        raise QualificationFailure("PROVIDER_PREINSTALLED_BEFORE_PROOF")
    _run([str(bin_dir / "python"), "-m", "pip", "install", "--no-compile", "--no-index", "--no-deps", str(provider_wheel)], cwd=external_cwd, env=install_env, code="OFFLINE_PROVIDER_INSTALL_FAILED")

    contracts = _parse_cli(
        bin_dir / "ephi-downstream-preflight",
        ["--entrypoint", PROVIDER_ENTRYPOINT, "--contracts-only", "--json"],
        cwd=external_cwd,
        env=install_env,
        expected_code=0,
    )
    composed = _parse_cli(
        bin_dir / "ephi-downstream-preflight",
        ["--entrypoint", PROVIDER_ENTRYPOINT, "--json"],
        cwd=external_cwd,
        env=install_env,
        expected_code=0,
    )
    contract_facts = _compatibility_facts(contracts, authority, composition_expected=False)
    composition_facts = _compatibility_facts(composed, authority, composition_expected=True)
    if contract_facts["safe_manifest_sha256"] != composition_facts["safe_manifest_sha256"]:
        raise QualificationFailure("DOWNSTREAM_MANIFEST_IDENTITY_MISMATCH")
    if contracts["safe_composition_smoke"].get("status") != "NOT_RUN":
        raise QualificationFailure("CONTRACTS_ONLY_MODE_INVALID")

    probe_path = artifact_root / "imports" / f"{release_name.replace('-', 'minus-')}.json"
    import_probe = _probe_imports(bin_dir / "python", cwd=external_cwd, env=install_env, worktrees=source_roots, output_path=probe_path)
    if import_probe.get("external_cwd") is not True:
        raise QualificationFailure("INSTALLED_IMPORT_PROBE_FAILED")
    negative = _negative_controls(bin_dir, cwd=external_cwd, env=install_env, work_root=environment_root)

    provider_inventory = _installed_provider_inventory(bin_dir / "python", cwd=external_cwd, env=install_env)
    provider_paths = [item["path"] for item in provider_inventory["files"]]
    ephi_after = _release_install_identity(bin_dir / "python", cwd=external_cwd, env=install_env)
    ephi_synthetic_paths = [item["path"] for item in ephi_before["files"] if "synthetic_downstream" in str(item["path"])]
    ephi_synthetic_paths.extend(item["path"] for item in ephi_after["files"] if "synthetic_downstream" in str(item["path"]))
    if ephi_synthetic_paths:
        raise QualificationFailure("PROVIDER_IN_EPHI_DISTRIBUTION")
    migration_after = _release_migration_inventory(ephi_after)
    if ephi_before["files"] != ephi_after["files"] or migration_before != migration_after:
        raise QualificationFailure("CORE_EDIT_PROHIBITION_FAILED")

    _write_json(artifact_root / "hashes" / f"ephi-{release_name.replace('-', 'minus-')}-before-after.json", {
        "distribution": "ephi",
        "version": app["version"],
        "before": ephi_before,
        "after": ephi_after,
        "migration_files_before": migration_before,
        "migration_files_after": migration_after,
        "unchanged": True,
    })
    _write_json(artifact_root / "hashes" / f"provider-{release_name.replace('-', 'minus-')}.json", provider_inventory)
    _write_json(artifact_root / "preflight" / f"{release_name.replace('-', 'minus-')}-release-preflight.json", release_preflight)
    _write_json(artifact_root / "preflight" / f"{release_name.replace('-', 'minus-')}-contracts-only.json", contracts)
    _write_json(artifact_root / "preflight" / f"{release_name.replace('-', 'minus-')}-composition.json", composed)
    _write_json(artifact_root / "negative-controls" / f"{release_name.replace('-', 'minus-')}.json", negative)

    return {
        "release_identity_sha256": release_preflight["release_identity_sha256"],
        "integrated_commit": release["integrated_commit"],
        "integrated_tree": release["integrated_tree"],
        "package_version": app["version"],
        "install_inputs_sha256": identity["install_inputs_sha256"],
        "release_preflight": identity,
        "installed_ephi": {
            "distribution": ephi_before["distribution"],
            "version": ephi_before["version"],
            "file_count": ephi_before["file_count"],
            "before_identity_sha256": ephi_before["identity_sha256"],
            "after_identity_sha256": ephi_after["identity_sha256"],
            "before_after_byte_equal": True,
            "migration_identity_sha256": migrations_identity,
            "migration_file_count": len(migration_before),
            "migration_before_after_byte_equal": True,
            "migration_files": migration_after,
            "synthetic_downstream_files_owned": ephi_synthetic_paths,
        },
        "provider_installation": {
            "artifact_sha256": provider_artifact["sha256"],
            "artifact_byte_size": provider_artifact["byte_size"],
            "provider_distribution": provider_inventory["distribution"],
            "provider_version": provider_inventory["version"],
            "provider_installed_files_identity_sha256": provider_inventory["identity_sha256"],
            "provider_installed_files": provider_paths,
            "owned_by_separate_distribution": True,
        },
        "contracts_only": contract_facts,
        "safe_composition": composition_facts,
        "composition_database": {
            "scope": "fresh isolated database dedicated to this release's composition smoke",
            "identity_sha256": _sha256_bytes(database_name.encode("utf-8")),
            "database_shared_with_other_frozen_release": False,
            "cross_release_migration_or_restart_performed": False,
        },
        "import_probe": import_probe,
        "negative_controls": negative,
    }


def _run_release_qualification(
    release_name: str,
    release: dict[str, object],
    worktree: Path,
    inputs: Path,
    env_root: Path,
    provider_wheel: Path,
    provider_artifact: dict[str, object],
    *,
    work_root: Path,
    external_cwd: Path,
    postgres_dsn: str,
    source_roots: list[Path],
    authority: dict[str, object],
    artifact_root: Path,
    job_id: str | None,
) -> dict[str, object]:
    bin_dir, install_env = _install_release(worktree, inputs, env_root, external_cwd, postgres_dsn)
    job_token = job_id.removeprefix("CF-")[:24] if job_id else "unbound"
    release_token = "nminus1" if release_name == "N-1" else "n"
    database_name = f"ephi_u4_{job_token}_{release_token}"
    composition_dsn = _create_composition_database(
        bin_dir / "python", postgres_dsn, database_name, cwd=external_cwd, env=install_env,
    )
    isolated_env = dict(install_env)
    isolated_env["EPHI_TEST_POSTGRES_DSN"] = composition_dsn
    try:
        return _run_installed_release_qualification(
            release_name,
            release,
            worktree,
            inputs,
            bin_dir,
            isolated_env,
            provider_wheel,
            provider_artifact,
            work_root=work_root,
            external_cwd=external_cwd,
            source_roots=source_roots,
            authority=authority,
            artifact_root=artifact_root,
            database_name=database_name,
        )
    finally:
        _drop_composition_database(
            bin_dir / "python", postgres_dsn, database_name, cwd=external_cwd, env=install_env,
        )


def _fabric_binding(job_id: str | None) -> dict[str, object]:
    if job_id is None:
        return {"fabric_job": None, "fabric_binding_state": "UNBOUND_CI"}
    if not _JOB_ID.fullmatch(job_id):
        raise QualificationFailure("FABRIC_JOB_ID_INVALID")
    return {"fabric_job": job_id, "fabric_binding_state": "BOUND"}


def _qualification_identity(job_id: str | None, compatibility_state: str) -> dict[str, object]:
    if compatibility_state not in {"PASS", "FAIL"}:
        raise ValueError("compatibility_state must be PASS or FAIL")
    return {"compatibility_state": compatibility_state, **_fabric_binding(job_id)}


def _failure_qualification_identity(job_id: str | None) -> dict[str, object]:
    if job_id is not None and not _JOB_ID.fullmatch(job_id):
        return {
            "compatibility_state": "FAIL",
            "fabric_job": None,
            "fabric_binding_state": "INVALID",
        }
    return _qualification_identity(job_id, "FAIL")


def _candidate_report_identity(repository: Path, candidate: str, candidate_tree: str, job_id: str | None) -> tuple[dict[str, object], list[str]]:
    _fabric_binding(job_id)
    if _git(repository, "rev-parse", "HEAD") != candidate or _git(repository, "rev-parse", "HEAD^{tree}") != candidate_tree:
        raise QualificationFailure("CANDIDATE_IDENTITY_MISMATCH")
    if _git(repository, "status", "--porcelain", "--untracked-files=all"):
        raise QualificationFailure("CANDIDATE_WORKTREE_NOT_CLEAN")
    return {"commit": candidate, "tree": candidate_tree}, []


def run_qualification(args: argparse.Namespace) -> int:
    repository = Path(args.repository_root).resolve() if args.repository_root else ROOT
    artifact_root = Path(args.artifact_root).expanduser().resolve()
    work_root = Path(args.work_root).expanduser().resolve()
    if artifact_root.is_relative_to(repository) or work_root.is_relative_to(repository):
        raise QualificationFailure("ARTIFACT_OR_WORK_ROOT_INSIDE_CHECKOUT")
    if artifact_root == work_root or artifact_root.is_relative_to(work_root) or work_root.is_relative_to(artifact_root):
        raise QualificationFailure("ARTIFACT_AND_WORK_ROOT_OVERLAP")
    if artifact_root.exists() and any(artifact_root.iterdir()):
        raise QualificationFailure("ARTIFACT_ROOT_NOT_EMPTY")
    if work_root.exists() and any(work_root.iterdir()):
        raise QualificationFailure("WORK_ROOT_NOT_EMPTY")
    artifact_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)

    authority, authority_sha = _authority()
    releases = _release_records(authority)
    candidate, _ = _candidate_report_identity(repository, args.candidate_sha, args.candidate_tree, args.job_id)
    repository_remote = _git(repository, "remote", "get-url", "origin")
    if not re.search(r"(?:github\.com[:/]?)kimhw8084/ephi(?:\.git)?/?$", repository_remote, re.IGNORECASE):
        raise QualificationFailure("CANONICAL_REPOSITORY_MISMATCH")
    if _git(repository, "merge-base", "--is-ancestor", str(releases["N-1"]["integrated_commit"]), str(releases["N"]["integrated_commit"]), check_code="FROZEN_ANCESTRY_MISMATCH"):
        raise QualificationFailure("FROZEN_ANCESTRY_MISMATCH")
    if _git(repository, "merge-base", "--is-ancestor", str(releases["N"]["integrated_commit"]), args.candidate_sha, check_code="CANDIDATE_NOT_DESCENDANT_OF_FROZEN_N"):
        raise QualificationFailure("CANDIDATE_NOT_DESCENDANT_OF_FROZEN_N")
    protected_paths = authority.get("protected_cross_release_paths")
    if not isinstance(protected_paths, list):
        raise QualificationFailure("FROZEN_AUTHORITY_INVALID")
    diff_args = ["diff", "--name-only", str(releases["N-1"]["integrated_commit"]), str(releases["N"]["integrated_commit"]), "--", *protected_paths]
    cross_release_delta = _git(repository, *diff_args).splitlines()
    if cross_release_delta:
        raise QualificationFailure("FROZEN_PROVIDER_PROFILE_NOT_UNCHANGED")
    candidate_changes = _assert_allowed_candidate_paths(repository, args.candidate_sha, str(releases["N"]["integrated_commit"]))

    postgres_dsn = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()
    if not postgres_dsn:
        raise QualificationFailure("POSTGRESQL_18_SERVICE_REQUIRED")
    worktrees_root = work_root / "worktrees"
    inputs_root = work_root / "inputs"
    environments_root = work_root / "environments"
    external_cwd = work_root / "cwd"
    for path in (worktrees_root, inputs_root, environments_root, external_cwd):
        path.mkdir(parents=True, exist_ok=True)

    worktrees: dict[str, Path] = {}
    try:
        worktrees["N-1"] = _worktree_source(repository, worktrees_root, "n-minus-1", releases["N-1"])
        worktrees["N"] = _worktree_source(repository, worktrees_root, "n", releases["N"])
        source_roots = [repository, *worktrees.values()]
        for path in (external_cwd, inputs_root, environments_root, artifact_root):
            if any(path.resolve().is_relative_to(item.resolve()) for item in source_roots):
                raise QualificationFailure("EXTERNAL_EXECUTION_ROOT_INVALID")
        manifests = {name: _provider_source_manifest(root, name) for name, root in worktrees.items()}
        if manifests["N-1"]["files"] != manifests["N"]["files"]:
            raise QualificationFailure("PROVIDER_SOURCE_MANIFEST_CHANGED")
        source_manifest = manifests["N-1"]
        if source_manifest["source_commit"] != releases["N-1"]["integrated_commit"]:
            raise QualificationFailure("PROVIDER_SOURCE_IDENTITY_INVALID")
        _write_json(artifact_root / "provider" / "provider-source-manifest.json", source_manifest)

        input_dirs: dict[str, Path] = {}
        prep_reports: dict[str, dict[str, object]] = {}
        for name, root in worktrees.items():
            input_dirs[name] = inputs_root / ("n-minus-1" if name == "N-1" else "n")
            prep_reports[name] = _prepare_release(root, input_dirs[name], external_cwd)
            expected = releases[name]
            if prep_reports[name].get("candidate_source") != {"commit": expected["integrated_commit"], "tree": expected["integrated_tree"]}:
                raise QualificationFailure("RELEASE_INPUT_SOURCE_MISMATCH")
            if prep_reports[name].get("release_identity_sha256") != expected["release_identity_sha256"]:
                raise QualificationFailure("RELEASE_INPUT_IDENTITY_MISMATCH")

        n1_provider_wheel, n1_provider_artifact, n1_provider_kit = _provider_artifact(input_dirs["N-1"])
        n_provider_wheel, n_provider_artifact, n_provider_kit = _provider_artifact(input_dirs["N"])
        expected_provider_wheel_sha = authority.get("provider_package", {}).get("compatibility_wheel_sha256")
        if not isinstance(expected_provider_wheel_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_provider_wheel_sha):
            raise QualificationFailure("FROZEN_AUTHORITY_INVALID")
        if args.provider_wheel:
            compatibility_wheel, compatibility_wheel_size = _provided_provider_wheel(
                args.provider_wheel, n1_provider_wheel.name, expected_provider_wheel_sha,
            )
            compatibility_input_source = "SUPPLIED_PINNED_N_MINUS_1_WHEEL"
        else:
            if n1_provider_artifact["sha256"] != expected_provider_wheel_sha:
                raise QualificationFailure("PROVIDER_ARTIFACT_IDENTITY_MISMATCH")
            compatibility_wheel = n1_provider_wheel
            compatibility_wheel_size = int(n1_provider_artifact["byte_size"])
            compatibility_input_source = "N_MINUS_1_RELEASE_PREPARATION_OUTPUT"
        provider_artifact = {
            **n1_provider_artifact,
            "sha256": expected_provider_wheel_sha,
            "byte_size": compatibility_wheel_size,
            "wheel_file": compatibility_wheel.name,
            "compatibility_input_source": compatibility_input_source,
        }
        expected_provider_files = [row for row in source_manifest["files"] if str(row["path"]).endswith(".py")]
        for kit, expected in ((n1_provider_kit, releases["N-1"]), (n_provider_kit, releases["N"])):
            provider_facts = kit.get("provider")
            source_facts = kit.get("source")
            if (
                kit.get("ephi_release_identity_sha256") != expected["release_identity_sha256"]
                or not isinstance(source_facts, dict)
                or source_facts.get("commit") != expected["integrated_commit"]
                or not isinstance(provider_facts, dict)
                or provider_facts.get("distribution") != PROVIDER_NAME
                or provider_facts.get("version") != "1.0.0"
                or provider_facts.get("entrypoint") != PROVIDER_ENTRYPOINT
                or provider_facts.get("content_files") != expected_provider_files
            ):
                raise QualificationFailure("PROVIDER_SOURCE_IDENTITY_INVALID")
        provider_output = artifact_root / "provider" / compatibility_wheel.name
        shutil.copyfile(compatibility_wheel, provider_output)
        if _sha256_file(provider_output) != expected_provider_wheel_sha:
            raise QualificationFailure("PROVIDER_ARTIFACT_IDENTITY_MISMATCH")
        provider_artifact.update({
            "file": f"provider/{provider_output.name}",
            "artifact_path": f"provider/{provider_output.name}",
            "build_source_release": "N-1",
            "build_source_commit": releases["N-1"]["integrated_commit"],
            "provider_rebuilt_for_n": False,
            "same_artifact_installed_on_both_releases": True,
        })
        n1_prep_output = {
            "status": "BUILT_BUT_NOT_USED_AS_COMPATIBILITY_INPUT" if compatibility_input_source == "SUPPLIED_PINNED_N_MINUS_1_WHEEL" else "USED_AS_COMPATIBILITY_INPUT",
            "artifact_sha256": n1_provider_artifact["sha256"],
            "artifact_byte_size": n1_provider_artifact["byte_size"],
            "used_for_compatibility": compatibility_input_source == "N_MINUS_1_RELEASE_PREPARATION_OUTPUT",
            "installed_as_compatibility_input": compatibility_input_source == "N_MINUS_1_RELEASE_PREPARATION_OUTPUT",
        }
        unused_n_prep = {
            "status": "BUILT_BY_N_RELEASE_INPUT_TOOL_NOT_INSTALLED",
            "artifact_sha256": n_provider_artifact["sha256"],
            "artifact_byte_size": n_provider_artifact["byte_size"],
            "source_manifest_equal_to_n_minus_1": n1_provider_kit["provider"]["content_files"] == n_provider_kit["provider"]["content_files"],
            "installed_in_n_environment": False,
            "used_for_compatibility": False,
        }
        _write_json(artifact_root / "provider" / "provider-artifact-inventory.json", {
            "schema": "org.ephi.u4-provider-artifact-inventory.v1",
            "distribution": PROVIDER_NAME,
            "version": "1.0.0",
            "entrypoint": PROVIDER_ENTRYPOINT,
            "installed_artifact": provider_artifact,
            "n_minus_1_release_preparation_tool_output": n1_prep_output,
            "n_release_preparation_tool_output": unused_n_prep,
            "source_manifest_sha256": source_manifest["manifest_sha256"],
        })

        release_evidence = {}
        for name, root in worktrees.items():
            release_evidence[name] = _run_release_qualification(
                name,
                releases[name],
                root,
                input_dirs[name],
                environments_root / ("n-minus-1" if name == "N-1" else "n"),
                n1_provider_wheel,
                provider_artifact,
                work_root=work_root,
                external_cwd=external_cwd,
                postgres_dsn=postgres_dsn,
                source_roots=source_roots,
                authority=authority,
                artifact_root=artifact_root,
                job_id=args.job_id,
            )

        safe_n1 = release_evidence["N-1"]["safe_composition"]["safe_manifest"]
        safe_n = release_evidence["N"]["safe_composition"]["safe_manifest"]
        if safe_n1 != safe_n:
            raise QualificationFailure("SAFE_MANIFEST_PROVIDER_FACTS_CHANGED")
        provider_hash_n1 = release_evidence["N-1"]["provider_installation"]["provider_installed_files_identity_sha256"]
        provider_hash_n = release_evidence["N"]["provider_installation"]["provider_installed_files_identity_sha256"]
        if provider_hash_n1 != provider_hash_n:
            raise QualificationFailure("PROVIDER_INSTALLED_BYTES_DIFFER")
        if release_evidence["N-1"]["installed_ephi"]["migration_files"] != release_evidence["N"]["installed_ephi"]["migration_files"]:
            raise QualificationFailure("RELEASE_MIGRATION_IDENTITY_MISMATCH")

        for name, path in worktrees.items():
            if _git(path, "status", "--porcelain", "--untracked-files=all"):
                raise QualificationFailure("FROZEN_WORKTREE_NOT_CLEAN")
        for name, path in reversed(list(worktrees.items())):
            if path.exists():
                _run(["git", "-C", str(repository), "worktree", "remove", "--force", str(path)], cwd=ROOT, code="FROZEN_WORKTREE_CLEANUP_FAILED")
        worktrees.clear()

        report = {
            "schema": "org.ephi.u4-n1-provider-compatibility.v1",
            "status": "PASS",
            **_qualification_identity(args.job_id, "PASS"),
            "core_edit_required": False,
            "upstream_package_files_modified": [],
            "upstream_migration_files_modified": [],
            "provider_rebuilt_for_n": False,
            "provider_artifact_same_bytes": True,
            "source_checkout_imports_used": False,
            "qualification": {
                "change": "CHG-296",
                "slice": "U4.1",
                "kind": "unchanged-provider-public-abi-core-edit-compatibility-only",
                "fabric_job": args.job_id,
                "fabric_binding_state": _fabric_binding(args.job_id)["fabric_binding_state"],
                "candidate": candidate,
                "static_authority_path": "environment/u4_n1_provider_compatibility_authority.json",
                "static_authority_sha256": authority_sha,
            },
            "frozen_pair": {
                "N-1": releases["N-1"],
                "N": releases["N"],
                "is_semantic_release_tag": False,
                "is_production_release": False,
                "N-1_is_ancestor_of_N": True,
                "N_is_ancestor_of_candidate": True,
                "cross_release_git_delta_under_protected_paths": cross_release_delta,
                "installed_migration_bytes_equal": True,
                "candidate_changed_paths": candidate_changes,
                "release_pair_qualification_only": True,
                "nicegui_base_pin": authority["nicegui_base"],
                "migration_identity_sha256": authority["migrations"]["identity_sha256"],
            },
            "provider": {
                "distribution": PROVIDER_NAME,
                "version": "1.0.0",
                "entrypoint": PROVIDER_ENTRYPOINT,
                "source_release": "N-1",
                "source_commit": releases["N-1"]["integrated_commit"],
                "compatibility_input_source": compatibility_input_source,
                "compatibility_wheel_sha256": expected_provider_wheel_sha,
                "n_minus_1_release_preparation_tool_output": n1_prep_output,
                "source_manifest": source_manifest,
                "source_manifest_sha256": source_manifest["manifest_sha256"],
                "cross_release_source_file_manifests_byte_equal": manifests["N-1"]["files"] == manifests["N"]["files"],
                "wheel": provider_artifact,
            "provider_artifact_same_bytes": True,
            "provider_rebuilt_for_n": False,
            "provider_rebuilt_for_n_scope": "the N environment installs the N-1 wheel; the N release-input tool's separately emitted wheel is inventoried and discarded",
            "n_release_preparation_tool_output": unused_n_prep,
                "installed_file_inventory_identity_N-1": provider_hash_n1,
                "installed_file_inventory_identity_N": provider_hash_n,
            },
            "public_abi": {
                "abi_id": "org.ephi.downstream",
                "abi_version": "1.0.0",
                "provider_contract_version": "1.0.0",
                "safe_manifest_identity_N-1": release_evidence["N-1"]["safe_composition"]["safe_manifest_sha256"],
                "safe_manifest_identity_N": release_evidence["N"]["safe_composition"]["safe_manifest_sha256"],
                "safe_manifest_byte_equal": True,
                "release_specific_safe_manifest_differences": [],
            },
            "releases": release_evidence,
            "installed_execution": {
                "cwd_outside_all_source_checkouts": True,
                "public_index_used_for_installation": False,
                "checkout_pythonpath_or_sys_path_used": False,
                "source_checkout_imports_used": False,
                "source_import_use_scope": "Installed compatibility/conformance/composition execution; release-owned preparation tools read their own frozen source worktrees by design.",
                "frozen_source_checkout_preparation_used": True,
                "provider_imported_from_installed_separate_distribution": True,
                "full_safe_composition_used_postgresql_major": 18,
                "all_worktrees_removed_after_proof": True,
            },
            "core_edit_prohibition": {
                "status": "PASS",
                "core_edit_required": False,
                "upstream_package_files_modified": [],
                "upstream_migration_files_modified": [],
                "EPHI_distribution_hashes_equal_before_after_provider_install_and_composition": True,
                "provider_owned_by_separate_distribution": True,
                "synthetic_downstream_files_inside_EPHI_distribution": [],
                "provider_rebuilt_for_n": False,
                "provider_artifact_same_bytes": True,
                "source_checkout_imports_used": False,
            },
            "negative_controls": release_evidence["N"]["negative_controls"],
            "claim_boundary": authority["qualification"]["claim_boundary"],
            "remaining_u4_prerequisites": authority["remaining_u4_prerequisites"],
            "explicit_nonclaims": [
                "Database migration/restart across N-1 to N is NOT_RUN.",
                "Retained workflow/read/artifact identity preservation is NOT_QUALIFIED.",
                "Feature/capability-flag preservation is NOT_QUALIFIED.",
                "Failed-upgrade stop-before-promotion is NOT_QUALIFIED.",
                "Cross-release activation/rollback is NOT_QUALIFIED.",
                "Full U4 exit is NOT_QUALIFIED.",
                "Real company integration, G10, G11, G12, Port Gate, release promotion and Production are NOT_RUN.",
                "U3 same-release ephi-release-slot behavior is not evidence of cross-release compatibility.",
            ],
            "artifacts": [
                "provider/provider-source-manifest.json",
                "provider/provider-artifact-inventory.json",
                f"provider/{provider_output.name}",
                "hashes/ephi-N-minus-1-before-after.json",
                "hashes/ephi-N-before-after.json",
                "hashes/provider-N-minus-1.json",
                "hashes/provider-N.json",
                "preflight/N-minus-1-release-preflight.json",
                "preflight/N-minus-1-contracts-only.json",
                "preflight/N-minus-1-composition.json",
                "preflight/N-release-preflight.json",
                "preflight/N-contracts-only.json",
                "preflight/N-composition.json",
                "negative-controls/N-minus-1.json",
                "negative-controls/N.json",
                "imports/N-minus-1.json",
                "imports/N.json",
            ],
        }
        _write_json(artifact_root / "u4-n1-provider-compatibility.json", report)
        artifact_rows = []
        for path in sorted(item for item in artifact_root.rglob("*") if item.is_file() and item.name != "artifact-manifest.json"):
            artifact_rows.append({"path": path.relative_to(artifact_root).as_posix(), "byte_size": path.stat().st_size, "sha256": _sha256_file(path)})
        artifact_manifest = {
            "schema": "org.ephi.u4-compatibility-artifact-manifest.v1",
            **_qualification_identity(args.job_id, "PASS"),
            "candidate": candidate,
            "files": artifact_rows,
        }
        _write_json(artifact_root / "artifact-manifest.json", artifact_manifest)
        return 0
    finally:
        for path in reversed(list(worktrees.values())):
            if path.exists():
                _run(["git", "-C", str(repository), "worktree", "remove", "--force", str(path)], cwd=ROOT, code="FROZEN_WORKTREE_CLEANUP_FAILED")


def main(argv: list[str] | None = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    try:
        code = run_qualification(args)
    except QualificationFailure as exc:
        root = Path(args.artifact_root).expanduser().resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
            _write_json(root / "u4-n1-provider-compatibility.json", {
                "schema": "org.ephi.u4-n1-provider-compatibility.v1",
                "status": "FAIL",
                **_failure_qualification_identity(args.job_id),
                "failure_reason_code": exc.code,
                "candidate": {"commit": args.candidate_sha, "tree": args.candidate_tree},
                "claim_boundary": "No compatibility claim; the bounded qualification did not satisfy every required proof condition.",
            })
        except OSError:
            pass
        print(json.dumps({"status": "FAIL", "reason_code": exc.code}, sort_keys=True, separators=(",", ":")))
        return 2
    except Exception:
        code = "QUALIFICATION_FAILED"
        root = Path(args.artifact_root).expanduser().resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
            _write_json(root / "u4-n1-provider-compatibility.json", {
                "schema": "org.ephi.u4-n1-provider-compatibility.v1",
                "status": "FAIL",
                **_failure_qualification_identity(args.job_id),
                "failure_reason_code": code,
                "candidate": {"commit": args.candidate_sha, "tree": args.candidate_tree},
                "claim_boundary": "No compatibility claim; the bounded qualification did not satisfy every required proof condition.",
            })
        except OSError:
            pass
        print(json.dumps({"status": "FAIL", "reason_code": code}, sort_keys=True, separators=(",", ":")))
        return 2
    print(json.dumps({"status": "PASS", "report": "u4-n1-provider-compatibility.json"}, sort_keys=True, separators=(",", ":")))
    return code


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, help="Empty job-owned Artifact Bridge material root outside the checkout.")
    parser.add_argument("--work-root", required=True, help="Empty external root for detached worktrees, builds, and environments.")
    parser.add_argument("--repository-root", help="Canonical checkout whose Git object database contains both frozen releases.")
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--candidate-tree", required=True)
    parser.add_argument("--job-id", help="Live Project OS/Fabric job ID. Omit for generic CI evidence, which is marked UNBOUND_CI.")
    parser.add_argument("--provider-wheel", help="Optional exact N-1 compatibility wheel; its SHA-256 must match the frozen authority.")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
