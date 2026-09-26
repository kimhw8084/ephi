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
import venv

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

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
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
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
        final_wheels = staged / "wheelhouse"
        transfer_locks = staged / "locks"
        for path in (download_dir, build_dir, final_wheels, transfer_locks):
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
            venv.EnvBuilder(with_pip=True, clear=True).create(builder)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseFailure("PREPARATION_PREREQUISITE_UNAVAILABLE") from exc
        if os.name == "nt":
            builder_python = builder / "Scripts" / "python.exe"
        else:
            builder_python = builder / "bin" / "python"
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
        _run([
            str(builder_python), "-m", "pip", "wheel", "--disable-pip-version-check", "--no-input",
            "--no-index", "--no-deps", "--no-build-isolation", "--no-cache-dir", "--wheel-dir", str(build_dir),
            str(ROOT),
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
                    "file": f"locks/{key}-{Path(value['path']).name}",
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
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="New bundle directory outside the source checkout.")
    args = parser.parse_args(argv)
    try:
        report = _prepare(Path(args.output_dir))
    except ReleaseFailure as exc:
        report = {"status": "FAIL", "reason_code": exc.reason_code}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        report = {"status": "FAIL", "reason_code": "PREPARATION_FAILED"}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
