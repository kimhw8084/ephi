#!/usr/bin/env python3
"""Prepare immutable EPHI wheels and dependency inputs before restricted install."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from unittest.mock import patch
import venv

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for _import_root in (str(SRC), str(ROOT)):
    if _import_root not in sys.path:
        sys.path.insert(0, _import_root)

from ephi.release_identity import (  # noqa: E402
    INSTALL_INPUTS_SCHEMA,
    ReleaseFailure,
    _normal_name,
    _sha256_bytes,
    _wheel_metadata,
    build_release_inventory,
    canonical_json_bytes,
    verify_inventory_document,
)
from ephi.config import EPHI_ENV  # noqa: E402
from ephi.downstream import preflight as downstream_preflight  # noqa: E402


PYPI_INDEX = "https://pypi.org/simple"


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise ReleaseFailure("PREPARATION_PREREQUISITE_UNAVAILABLE") from exc
    if result.returncode != 0:
        raise ReleaseFailure("PREPARATION_FAILED")
    return result.stdout.strip()


def _git_identity(env: dict[str, str]) -> tuple[str, str, int]:
    commit = _run(["git", "rev-parse", "HEAD"], cwd=ROOT, env=env)
    tree = _run(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, env=env)
    dirty = _run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, env=env)
    epoch = _run(["git", "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT, env=env)
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[0-9a-f]{40}", tree) or dirty:
        raise ReleaseFailure("SOURCE_CHECKOUT_NOT_IMMUTABLE")
    try:
        source_epoch = int(epoch)
    except ValueError as exc:
        raise ReleaseFailure("SOURCE_CHECKOUT_NOT_IMMUTABLE") from exc
    return commit, tree, source_epoch


def _clean_tool_environment(epoch: int) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PIP_", "PYTHON", "CONDA_", "UV_"))
        and key not in {"VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}
    }
    env.update({
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_INDEX_URL": PYPI_INDEX,
        "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONHASHSEED": "0",
        "SOURCE_DATE_EPOCH": str(epoch),
        "TZ": "UTC",
        "LC_ALL": "C",
    })
    return env


def _load_inventory() -> tuple[dict[str, object], dict[str, object]]:
    path = SRC / "ephi" / "release_inventory.json"
    try:
        raw = path.read_bytes()
        stored = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseFailure("RELEASE_INVENTORY_INVALID") from exc
    if not isinstance(stored, dict):
        raise ReleaseFailure("RELEASE_INVENTORY_INVALID")
    verify_inventory_document(stored, raw)
    current = build_release_inventory(ROOT)
    if canonical_json_bytes(current) + b"\n" != raw:
        raise ReleaseFailure("RELEASE_INVENTORY_STALE")
    try:
        index = json.loads((SRC / "ephi" / "release_locks" / "index.json").read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED") from exc
    if not isinstance(index, dict):
        raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
    return current, index


def _expected_downloads(index: dict[str, object], minor: str, system: str, *, include_build: bool = False) -> dict[str, str]:
    interpreters = index.get("interpreters")
    if not isinstance(interpreters, dict) or not isinstance(interpreters.get(minor), dict):
        raise ReleaseFailure("UNSUPPORTED_PYTHON_VERSION")
    entry = interpreters[minor]
    lock_assets = [entry["runtime"], entry["optional"]["postgres"], index["installer"]]
    if include_build:
        lock_assets.append(index["build"])
    expected: dict[str, str] = {}
    for asset in lock_assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("distributions"), list):
            raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
        for item in asset["distributions"]:
            if not isinstance(item, dict):
                raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
            marker = item.get("marker")
            if marker == 'sys_platform == "win32"' and system != "win32":
                continue
            if marker == 'sys_platform != "win32"' and system == "win32":
                continue
            name, version = item.get("name"), item.get("version")
            if not isinstance(name, str) or not isinstance(version, str):
                raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
            if name in expected and expected[name] != version:
                raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
            expected[name] = version
    return expected


def _download_lock(python: str, lock_path: Path, target: Path, env: dict[str, str]) -> None:
    _run([
        python,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--no-input",
        "--index-url",
        PYPI_INDEX,
        "--require-hashes",
        "--only-binary=:all:",
        "--dest",
        str(target),
        "-r",
        str(lock_path),
    ], cwd=ROOT, env=env)


def _wheel_inventory(wheel_dir: Path) -> list[tuple[Path, str, str]]:
    items: list[tuple[Path, str, str]] = []
    for path in sorted(wheel_dir.glob("*.whl"), key=lambda item: item.name):
        name, version = _wheel_metadata(path)
        items.append((path, name, version))
    return items


def _qualification_requirements() -> dict[str, str]:
    path = ROOT / "environment" / "qualification-requirements.txt"
    expected: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseFailure("QUALIFICATION_INPUTS_INVALID") from exc
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9][A-Za-z0-9.+_-]*)", value)
        if match is None:
            raise ReleaseFailure("QUALIFICATION_INPUTS_INVALID")
        name, version = match.groups()
        normalized = _normal_name(name)
        if normalized in expected:
            raise ReleaseFailure("QUALIFICATION_INPUTS_INVALID")
        expected[normalized] = version
    if set(expected) != {"greenlet", "playwright", "pyee"}:
        raise ReleaseFailure("QUALIFICATION_INPUTS_INVALID")
    return expected


def _qualification_project_identity() -> tuple[str, str]:
    try:
        project = tomllib.loads(
            (ROOT / "examples" / "synthetic_downstream" / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        name, version = project["name"], project["version"]
    except (OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise ReleaseFailure("QUALIFICATION_INPUTS_INVALID") from exc
    if not isinstance(name, str) or not isinstance(version, str):
        raise ReleaseFailure("QUALIFICATION_INPUTS_INVALID")
    if _normal_name(name) != "ephi-synthetic-downstream-qualification" or version != "1.0.0":
        raise ReleaseFailure("QUALIFICATION_INPUTS_INVALID")
    return _normal_name(name), version


def _qualification_lock(artifacts: list[dict[str, object]], minor: str) -> bytes:
    lines = [
        "# EPHI qualification-only offline inputs. Exact prepared wheel hashes follow.",
        f"# CPython {minor}; separate from ordinary EPHI runtime dependencies.",
        "",
    ]
    for item in sorted(artifacts, key=lambda record: str(record["distribution"])):
        lines.append(f'{item["distribution"]}=={item["version"]} \\')
        lines.append(f'    --hash=sha256:{item["sha256"]}')
    return ("\n".join(lines) + "\n").encode("utf-8")


def _prepare(destination: Path) -> dict[str, object]:
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    if platform.python_implementation() != "CPython":
        raise ReleaseFailure("UNSUPPORTED_PYTHON_IMPLEMENTATION")
    inventory, index = _load_inventory()
    supported = inventory["python"]["install_supported_interpreters"]
    if minor not in supported:
        raise ReleaseFailure("UNSUPPORTED_PYTHON_VERSION")
    commit, tree, source_epoch = _git_identity(_clean_tool_environment(0))
    release = inventory["release"]
    if not isinstance(release, dict):
        raise ReleaseFailure("RELEASE_INVENTORY_INVALID")

    target = destination.expanduser().resolve()
    try:
        target.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ReleaseFailure("OUTPUT_LOCATION_INVALID")
    if target.exists() or target.is_symlink():
        raise ReleaseFailure("OUTPUT_LOCATION_INVALID")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReleaseFailure("OUTPUT_LOCATION_INVALID") from exc

    env = _clean_tool_environment(source_epoch)
    suffix = minor.replace(".", "")
    lock_dir = SRC / "ephi" / "release_locks"
    with tempfile.TemporaryDirectory(prefix="ephi-u3-release-", dir=target.parent) as temporary:
        work = Path(temporary)
        downloaded = work / "downloaded"
        built = work / "built"
        staged = work / "bundle"
        download_dir = downloaded / "wheelhouse"
        build_dir = built / "wheelhouse"
        qualification_download_dir = downloaded / "qualification-wheelhouse"
        qualification_build_dir = built / "qualification-wheelhouse"
        final_wheels = staged / "wheelhouse"
        transfer_locks = staged / "locks"
        qualification_root = staged / "qualification"
        qualification_wheels = qualification_root / "wheelhouse"
        qualification_locks = qualification_root / "locks"
        for path in (
            download_dir,
            build_dir,
            qualification_download_dir,
            qualification_build_dir,
            final_wheels,
            transfer_locks,
            qualification_wheels,
            qualification_locks,
        ):
            path.mkdir(parents=True)

        selected = index["interpreters"][minor]
        lock_names = (
            selected["runtime"]["path"],
            selected["optional"]["postgres"]["path"],
            index["installer"]["path"],
            index["build"]["path"],
        )
        for relative in lock_names:
            lock = lock_dir / Path(relative).name
            _download_lock(sys.executable, lock, download_dir, env)
        downloaded_identity = {name: version for _, name, version in _wheel_inventory(download_dir)}
        if downloaded_identity != _expected_downloads(index, minor, sys.platform, include_build=True):
            raise ReleaseFailure("PREPARATION_FAILED")

        builder = work / "builder"
        try:
            venv.EnvBuilder(with_pip=False, clear=True, symlinks=os.name != "nt").create(builder)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseFailure("PREPARATION_PREREQUISITE_UNAVAILABLE") from exc
        if os.name == "nt":
            builder_python = builder / "Scripts" / "python.exe"
        else:
            builder_python = builder / "bin" / "python"
        _run([
            str(builder_python), "-m", "ensurepip", "--upgrade", "--default-pip",
        ], cwd=ROOT, env=env)
        for lock_name in ("installer-py311.txt", "build-py311.txt"):
            lock = lock_dir / lock_name
            _run([
                str(builder_python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                "--no-index", "--find-links", str(download_dir), "--require-hashes", "-r", str(lock),
            ], cwd=ROOT, env=env)

        base_contract = inventory["dependencies"]["base"]
        if not isinstance(base_contract, dict):
            raise ReleaseFailure("BASE_RUNTIME_CONTRACT_INVALID")
        base_repo = base_contract["repository"]
        base_commit = base_contract["commit"]
        base_source = work / "nicegui-base"
        _run(["git", "clone", "--filter=blob:none", "--no-checkout", "--quiet", base_repo, str(base_source)], cwd=ROOT, env=env)
        _run(["git", "-C", str(base_source), "checkout", "--quiet", "--detach", base_commit], cwd=ROOT, env=env)
        actual_base_commit = _run(["git", "-C", str(base_source), "rev-parse", "HEAD"], cwd=ROOT, env=env)
        if actual_base_commit != base_commit:
            raise ReleaseFailure("BASE_SOURCE_MISMATCH")

        base_epoch_text = _run(["git", "-C", str(base_source), "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT, env=env)
        try:
            base_epoch = int(base_epoch_text)
        except ValueError as exc:
            raise ReleaseFailure("BASE_SOURCE_MISMATCH") from exc
        base_env = dict(env, SOURCE_DATE_EPOCH=str(base_epoch))
        _run([
            str(builder_python), "-m", "pip", "wheel", "--disable-pip-version-check", "--no-input",
            "--no-index", "--no-deps", "--no-build-isolation", "--no-cache-dir", "--wheel-dir", str(build_dir),
            str(base_source),
        ], cwd=ROOT, env=base_env)
        app_source = work / "ephi-source"
        app_source.mkdir()
        shutil.copy2(ROOT / "pyproject.toml", app_source / "pyproject.toml")
        shutil.copy2(ROOT / "README.md", app_source / "README.md")
        shutil.copytree(ROOT / "src", app_source / "src")
        shutil.copytree(ROOT / "migrations", app_source / "migrations")
        _run([
            str(builder_python), "-m", "pip", "wheel", "--disable-pip-version-check", "--no-input",
            "--no-index", "--no-deps", "--no-build-isolation", "--no-cache-dir", "--wheel-dir", str(build_dir),
            str(app_source),
        ], cwd=ROOT, env=env)

        qualification_dependency_versions = _qualification_requirements()
        qualification_name, qualification_version = _qualification_project_identity()
        qualification_download_requirements = [
            f"{name}=={version}" for name, version in sorted(qualification_dependency_versions.items())
        ]
        _run([
            sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--no-input",
            "--index-url", PYPI_INDEX, "--only-binary=:all:", "--no-deps", "--dest",
            str(qualification_download_dir), *qualification_download_requirements,
        ], cwd=ROOT, env=env)
        qualification_source = work / "qualification-source"
        qualification_source.mkdir()
        fixture_source = ROOT / "examples" / "synthetic_downstream"
        shutil.copy2(fixture_source / "pyproject.toml", qualification_source / "pyproject.toml")
        shutil.copy2(fixture_source / "README.md", qualification_source / "README.md")
        for path in sorted(fixture_source.glob("*.py"), key=lambda item: item.name):
            shutil.copy2(path, qualification_source / path.name)
        _run([
            str(builder_python), "-m", "pip", "wheel", "--disable-pip-version-check", "--no-input",
            "--no-index", "--no-deps", "--no-build-isolation", "--no-cache-dir", "--wheel-dir",
            str(qualification_build_dir), str(qualification_source),
        ], cwd=ROOT, env=env)

        expected = _expected_downloads(index, minor, sys.platform)
        expected[_normal_name(str(release["distribution"]))] = str(release["version"])
        expected[_normal_name(str(base_contract["distribution"]))] = str(base_contract["framework_version"])
        candidates = _wheel_inventory(download_dir) + _wheel_inventory(build_dir)
        selected_wheels: dict[str, tuple[Path, str]] = {}
        for path, name, version in candidates:
            if name not in expected:
                continue
            if expected[name] != version or name in selected_wheels:
                raise ReleaseFailure("PREPARATION_FAILED")
            selected_wheels[name] = (path, version)
        if set(selected_wheels) != set(expected):
            raise ReleaseFailure("PREPARATION_FAILED")

        optional_only = {
            item["name"]
            for item in selected["optional"]["postgres"]["distributions"]
            if item["name"] not in {value["name"] for value in selected["runtime"]["distributions"]}
        }
        artifact_entries: list[dict[str, object]] = []
        for name in sorted(selected_wheels):
            source, version = selected_wheels[name]
            filename = source.name
            destination_file = final_wheels / filename
            shutil.copyfile(source, destination_file)
            raw = destination_file.read_bytes()
            if name == _normal_name(str(release["distribution"])):
                kind = "application"
            elif name == _normal_name(str(base_contract["distribution"])):
                kind = "base"
            elif name == "pip":
                kind = "installer"
            elif name in optional_only:
                kind = "postgres"
            else:
                kind = "runtime"
            artifact_entries.append({
                "kind": kind,
                "distribution": name,
                "version": version,
                "file": f"wheelhouse/{filename}",
                "sha256": _sha256_bytes(raw),
                "byte_size": len(raw),
            })

        qualification_expected = dict(qualification_dependency_versions)
        qualification_expected[qualification_name] = qualification_version
        qualification_candidates = _wheel_inventory(qualification_download_dir) + _wheel_inventory(qualification_build_dir)
        qualification_selected: dict[str, tuple[Path, str]] = {}
        for path, name, version in qualification_candidates:
            if name not in qualification_expected:
                continue
            if version != qualification_expected[name] or name in qualification_selected:
                raise ReleaseFailure("QUALIFICATION_INPUTS_INVALID")
            qualification_selected[name] = (path, version)
        if set(qualification_selected) != set(qualification_expected):
            raise ReleaseFailure("QUALIFICATION_INPUTS_INVALID")
        if set(qualification_selected) & set(selected_wheels):
            raise ReleaseFailure("QUALIFICATION_INPUTS_INVALID")

        qualification_artifacts: list[dict[str, object]] = []
        for name in sorted(qualification_selected):
            source, version = qualification_selected[name]
            destination_file = qualification_wheels / source.name
            shutil.copyfile(source, destination_file)
            raw = destination_file.read_bytes()
            qualification_artifacts.append({
                "kind": "provider" if name == qualification_name else "qualification_dependency",
                "distribution": name,
                "version": version,
                "file": f"wheelhouse/{source.name}",
                "sha256": _sha256_bytes(raw),
                "byte_size": len(raw),
            })

        provider_entrypoint = "examples.synthetic_downstream.provider:build_bundle"
        with patch.dict(
            os.environ,
            {EPHI_ENV: "test"},
            clear=False,
        ):
            conformance = downstream_preflight(provider_entrypoint, compose=False)
        if conformance.get("status_code") != "CONTRACT_PASS" or conformance.get("compatibility", {}).get("status") != "PASS":
            raise ReleaseFailure("QUALIFICATION_PROVIDER_INCOMPATIBLE")
        abi_report = conformance.get("downstream_abi")
        abi_manifest = abi_report.get("manifest") if isinstance(abi_report, dict) else None
        if not isinstance(abi_report, dict) or not isinstance(abi_manifest, dict):
            raise ReleaseFailure("QUALIFICATION_PROVIDER_INCOMPATIBLE")
        content_files: list[dict[str, object]] = []
        for path in sorted(fixture_source.glob("*.py"), key=lambda item: item.name):
            raw = path.read_bytes()
            content_files.append({
                "path": f"examples/synthetic_downstream/{path.name}",
                "byte_size": len(raw),
                "sha256": _sha256_bytes(raw),
            })
        qualification_lock = _qualification_lock(qualification_artifacts, minor)
        qualification_lock_path = f"qualification-py{suffix}.txt"
        (qualification_locks / qualification_lock_path).write_bytes(qualification_lock)
        qualification_inputs: dict[str, object] = {
            "schema": "org.ephi.qualification-kit-inputs.v1",
            "ephi_release_identity_sha256": inventory["release_identity_sha256"],
            "source": {"repository": release["source_authority"], "commit": commit, "tree": tree},
            "runtime": {
                "python_minor": minor,
                "implementation": platform.python_implementation(),
                "platform": sysconfig.get_platform(),
            },
            "provider": {
                "distribution": qualification_name,
                "version": qualification_version,
                "entrypoint": provider_entrypoint,
                "abi_id": abi_manifest["abi"]["id"],
                "abi_version": abi_manifest["abi"]["version"],
                "manifest_sha256": abi_report["safe_manifest_hash"],
                "artifact_sha256": next(item["sha256"] for item in qualification_artifacts if item["distribution"] == qualification_name),
                "content_files": content_files,
            },
            "requirements_sha256": _sha256_bytes((ROOT / "environment" / "qualification-requirements.txt").read_bytes()),
            "lock": {
                "file": f"locks/{qualification_lock_path}",
                "sha256": _sha256_bytes(qualification_lock),
            },
            "artifacts": qualification_artifacts,
        }
        qualification_inputs["qualification_kit_identity_sha256"] = _sha256_bytes(canonical_json_bytes(qualification_inputs))
        (qualification_root / "qualification_inputs.json").write_bytes(canonical_json_bytes(qualification_inputs) + b"\n")
        shutil.copyfile(
            ROOT / "environment" / "qualification-requirements.txt",
            qualification_root / "qualification-requirements.txt",
        )

        lock_identities = {
            "runtime": selected["runtime"],
            "postgres": selected["optional"]["postgres"],
            "installer": index["installer"],
            "build": index["build"],
        }
        for key, value in lock_identities.items():
            source_lock = lock_dir / Path(value["path"]).name
            transfer_name = Path(value["path"]).name
            shutil.copyfile(source_lock, transfer_locks / transfer_name)
        install_inputs: dict[str, object] = {
            "schema": INSTALL_INPUTS_SCHEMA,
            "release_identity_sha256": inventory["release_identity_sha256"],
            "source": {
                "repository": release["source_authority"],
                "commit": commit,
                "tree": tree,
            },
            "runtime": {
                "python_minor": minor,
                "implementation": platform.python_implementation(),
                "platform": sysconfig.get_platform(),
            },
            "locks": {
                key: {
                    "path": value["path"],
                    "file": f"locks/{Path(value['path']).name}",
                    "sha256": value["sha256"],
                }
                for key, value in lock_identities.items()
            },
            "artifacts": artifact_entries,
        }
        install_inputs["install_inputs_sha256"] = _sha256_bytes(canonical_json_bytes(install_inputs))
        staged.mkdir(exist_ok=True)
        (staged / "install_inputs.json").write_bytes(canonical_json_bytes(install_inputs) + b"\n")
        try:
            os.replace(staged, target)
        except OSError as exc:
            raise ReleaseFailure("OUTPUT_LOCATION_INVALID") from exc

    return {
        "status": "PASS",
        "reason_code": "PREPARATION_PASS",
        "release_identity_sha256": inventory["release_identity_sha256"],
        "install_inputs_sha256": install_inputs["install_inputs_sha256"],
        "candidate_source": {"commit": commit, "tree": tree},
        "python_minor": minor,
        "artifact_count": len(artifact_entries),
        "qualification_kit_identity_sha256": qualification_inputs["qualification_kit_identity_sha256"],
        "qualification_artifact_count": len(qualification_artifacts),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="New bundle directory outside the source checkout.")
    args = parser.parse_args(argv)
    try:
        report = _prepare(Path(args.output_dir))
    except ReleaseFailure as exc:
        report = {"status": "FAIL", "reason_code": exc.reason_code}
    except Exception:
        report = {"status": "FAIL", "reason_code": "PREPARATION_FAILED"}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
