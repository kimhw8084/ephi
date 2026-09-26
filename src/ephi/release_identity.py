"""Deterministic, secret-safe EPHI release/install identity and preflight."""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import importlib.metadata as metadata
import json
import platform
from pathlib import Path, PurePosixPath
import re
import sys
import sysconfig
import tomllib
from typing import Any
import zipfile

from ephi.application.operations import migration_schema_identity
from ephi.downstream.contracts import (
    ABI_ID,
    ABI_VERSION,
    MANIFEST_SCHEMA,
    PROVIDER_CONTRACT_VERSION,
    REQUIRED_CATEGORIES,
)
from ephi.identity import ApplicationIdentity


RELEASE_SCHEMA = "org.ephi.release-install.v1"
LOCK_INDEX_SCHEMA = "org.ephi.release-lock-index.v1"
INSTALL_INPUTS_SCHEMA = "org.ephi.install-inputs.v1"
PREFLIGHT_SCHEMA = "org.ephi.release-preflight.v1"
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_LOCK_REQUIREMENT = re.compile(
    r'^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s;\\]+)(?:;\s*(.*?))?\s*\\?$'
)
_DIST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WHEEL_PATH = re.compile(r"^wheelhouse/[A-Za-z0-9_.+-]+\.whl$")


class ReleaseFailure(Exception):
    """Fixed reason code only; raw metadata and environment values are omitted."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def canonical_json_bytes(value: object) -> bytes:
    """One UTF-8 JSON spelling for release identities and transfer indexes."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ReleaseFailure("NON_CANONICAL_RELEASE_VALUE") from exc


def _document_bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normal_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _safe_json(path: Path, reason: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseFailure(reason) from exc
    if not isinstance(value, dict):
        raise ReleaseFailure(reason)
    return value, raw


def _read_lock_records(path: Path) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseFailure("LOCK_INPUT_MISSING_OR_TAMPERED") from exc
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    hash_count = 0
    for line in lines:
        match = _LOCK_REQUIREMENT.match(line)
        if match:
            if current is not None:
                if hash_count < 1:
                    raise ReleaseFailure("LOCK_INPUT_MISSING_OR_TAMPERED")
                records.append(current)
            package, version, marker = match.groups()
            current = {"name": _normal_name(package), "version": version}
            if marker:
                if marker not in {'sys_platform == "win32"', 'sys_platform != "win32"'}:
                    raise ReleaseFailure("LOCK_INPUT_MISSING_OR_TAMPERED")
                current["marker"] = marker
            hash_count = 0
        elif line.lstrip().startswith("--hash=sha256:"):
            if current is None or not re.fullmatch(r"\s*--hash=sha256:[0-9a-f]{64}\s*(?:\\)?", line):
                raise ReleaseFailure("LOCK_INPUT_MISSING_OR_TAMPERED")
            hash_count += 1
        elif "--index-url" in line or "--extra-index-url" in line or "--trusted-host" in line:
            raise ReleaseFailure("LOCK_INPUT_MISSING_OR_TAMPERED")
    if current is not None:
        if hash_count < 1:
            raise ReleaseFailure("LOCK_INPUT_MISSING_OR_TAMPERED")
        records.append(current)
    if not records or len({item["name"] for item in records}) != len(records):
        raise ReleaseFailure("LOCK_INPUT_MISSING_OR_TAMPERED")
    return records


def _lock_asset(root: Path, name: str) -> dict[str, object]:
    path = root / "src" / "ephi" / "release_locks" / name
    records = _read_lock_records(path)
    return {
        "path": f"release_locks/{name}",
        "sha256": _sha256_bytes(path.read_bytes()),
        "distributions": records,
    }


def _supported_interpreter_keys(requires: str) -> list[str]:
    match = re.fullmatch(r">=3\.(\d+),<3\.(\d+)", requires)
    if match is None:
        raise ReleaseFailure("PYTHON_INSTALL_CONTRACT_INVALID")
    lower, upper = (int(value) for value in match.groups())
    if lower >= upper:
        raise ReleaseFailure("PYTHON_INSTALL_CONTRACT_INVALID")
    return [f"3.{minor}" for minor in range(lower, upper)]


def _lock_index(root: Path) -> tuple[dict[str, Any], bytes]:
    path = root / "src" / "ephi" / "release_locks" / "index.json"
    index, raw = _safe_json(path, "LOCK_INDEX_MISSING_OR_TAMPERED")
    if raw != _document_bytes(index) or index.get("schema") != LOCK_INDEX_SCHEMA:
        raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
    if index.get("python_requires") != ">=3.11,<3.14":
        raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
    return index, raw


def _validate_lock_index(
    root: Path,
    index: dict[str, Any],
    expected_python: str,
    *,
    package_root: Path | None = None,
) -> None:
    expected_keys = _supported_interpreter_keys(expected_python)
    interpreters = index.get("interpreters")
    if not isinstance(interpreters, dict) or list(interpreters) != expected_keys:
        raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
    assets: list[dict[str, object]] = []
    for key in ("installer", "build"):
        item = index.get(key)
        if not isinstance(item, dict):
            raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
        assets.append(item)
    for entry in interpreters.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("runtime"), dict):
            raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
        optional = entry.get("optional")
        if not isinstance(optional, dict) or not isinstance(optional.get("postgres"), dict):
            raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
        assets.extend((entry["runtime"], optional["postgres"]))
    for entry in assets:
        path_value = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(path_value, str) or not isinstance(digest, str) or not _HEX_64.fullmatch(digest):
            raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
        rel = PurePosixPath(path_value)
        if not rel.parts or rel.is_absolute() or rel.parts[0] != "release_locks" or ".." in rel.parts:
            raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
        path = (package_root or root / "src" / "ephi") / rel
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ReleaseFailure("LOCK_INPUT_MISSING_OR_TAMPERED") from exc
        if _sha256_bytes(raw) != digest:
            raise ReleaseFailure("LOCK_INPUT_MISSING_OR_TAMPERED")
        actual_records = _read_lock_records(path)
        if actual_records != entry.get("distributions"):
            raise ReleaseFailure("LOCK_INPUT_MISSING_OR_TAMPERED")


def _requirements_from_project(project: dict[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
    dependencies = project.get("dependencies")
    optional = project.get("optional-dependencies")
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise ReleaseFailure("PACKAGE_METADATA_INCONSISTENT")
    if not isinstance(optional, dict):
        raise ReleaseFailure("PACKAGE_METADATA_INCONSISTENT")
    optional_copy: dict[str, list[str]] = {}
    for key, values in optional.items():
        if not isinstance(key, str) or not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ReleaseFailure("PACKAGE_METADATA_INCONSISTENT")
        optional_copy[key] = sorted(values)
    return sorted(dependencies), optional_copy


def _compatibility_lane(root: Path, install_minors: list[str]) -> list[str]:
    path = root / ".github" / "workflows" / "package.yml"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReleaseFailure("PACKAGE_METADATA_INCONSISTENT") from exc
    match = re.search(r"(?m)^\s*python:\s*\[([^]]+)\]", text)
    if match is None:
        raise ReleaseFailure("PACKAGE_METADATA_INCONSISTENT")
    versions = re.findall(r"['\"](3\.\d+)['\"]", match.group(1))
    if not versions or len(versions) != len(set(versions)):
        raise ReleaseFailure("PACKAGE_METADATA_INCONSISTENT")
    return sorted(set(versions) - set(install_minors), key=lambda item: tuple(map(int, item.split("."))))


def _capability_claims() -> dict[str, object]:
    return {
        "supported": [
            {
                "id": "declared-python-install-range",
                "status": "SUPPORTED_CONTRACT",
                "authority": "pyproject.toml",
            },
            {
                "id": "org.ephi.downstream-abi-v1",
                "status": "SUPPORTED_CONTRACT",
                "authority": "ephi.downstream.contracts",
            },
            {
                "id": "synthetic-downstream-conformance",
                "status": "SUPPORTED_PROOF_PATH",
                "authority": "examples/synthetic_downstream",
            },
            {
                "id": "release-install-preflight",
                "status": "IMPLEMENTED_CANDIDATE_QUALIFICATION_ONLY",
                "authority": "ephi-release-preflight",
            },
        ],
        "not_yet_qualified": [
            {"id": "real-family-g02-g06", "status": "NOT_RUN"},
            {"id": "company-identity-and-tls", "status": "NOT_RUN"},
            {"id": "production-like-capacity-g10", "status": "NOT_RUN"},
            {"id": "production-rpo-rto", "status": "NOT_RUN"},
            {"id": "g12", "status": "NOT_RUN"},
            {"id": "port-gate", "status": "NOT_RUN"},
            {"id": "company-deployment-readiness", "status": "NOT_RUN"},
            {"id": "release-promotion", "status": "NOT_CLAIMED"},
            {"id": "production", "status": "NOT_CLAIMED"},
        ],
    }


def _package_source_files(root: Path) -> list[dict[str, object]]:
    package_root = root / "src" / "ephi"
    entries: list[dict[str, object]] = []
    for path in sorted(package_root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.relative_to(package_root).as_posix() == "release_inventory.json":
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ReleaseFailure("PACKAGE_SOURCE_IDENTITY_INVALID") from exc
        entries.append({
            "path": path.relative_to(root).as_posix(),
            "byte_size": len(raw),
            "sha256": _sha256_bytes(raw),
        })
    if not entries:
        raise ReleaseFailure("PACKAGE_SOURCE_IDENTITY_INVALID")
    return entries


def build_release_inventory(repo_root: str | Path) -> dict[str, object]:
    """Build the release inventory only from existing package authorities."""

    root = Path(repo_root)
    try:
        project_data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        runtime, _ = _safe_json(root / "environment" / "nicegui_base_runtime.json", "BASE_RUNTIME_CONTRACT_INVALID")
        project = project_data["project"]
        project_name = project["name"]
        project_version = project["version"]
        requires_python = project["requires-python"]
        scripts = project_data.get("project", {}).get("scripts", {})
        build_system = project_data["build-system"]
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseFailure("PACKAGE_METADATA_INCONSISTENT") from exc
    if (
        not all(isinstance(value, str) for value in (project_name, project_version, requires_python))
        or not isinstance(scripts, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in scripts.items())
    ):
        raise ReleaseFailure("PACKAGE_METADATA_INCONSISTENT")

    identity = ApplicationIdentity()
    if identity.distribution != project_name or identity.version != project_version:
        raise ReleaseFailure("APPLICATION_IDENTITY_MISMATCH")
    direct_requirements, optional_requirements = _requirements_from_project(project)

    try:
        framework = runtime["framework"]
        runtime_dependencies = runtime["dependencies"]
        base_dependency = next(item for item in direct_requirements if item.startswith("nicegui-base @ "))
        nicegui_dependency = next(item for item in direct_requirements if item.startswith("nicegui=="))
        runtime_nicegui_dependency = next(item for item in runtime_dependencies if item.startswith("nicegui=="))
        env_requirements = (root / runtime["requirements_file"]).read_text(encoding="utf-8").splitlines()
        env_requirements = sorted(line.strip() for line in env_requirements if line.strip() and not line.lstrip().startswith("#"))
    except (KeyError, TypeError, OSError, UnicodeError, StopIteration) as exc:
        raise ReleaseFailure("BASE_RUNTIME_CONTRACT_INVALID") from exc
    if (
        runtime.get("python_requires") != requires_python
        or framework.get("distribution") != "nicegui-base"
        or framework.get("source_requirement") != base_dependency
        or base_dependency not in env_requirements
        or not isinstance(runtime_nicegui_dependency, str)
        or nicegui_dependency != runtime_nicegui_dependency
        or nicegui_dependency not in env_requirements
    ):
        raise ReleaseFailure("BASE_RUNTIME_CONTRACT_INVALID")
    nicegui_version = nicegui_dependency.partition("==")[2]

    python_minors = _supported_interpreter_keys(requires_python)
    index, index_bytes = _lock_index(root)
    if index.get("python_requires") != requires_python:
        raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
    _validate_lock_index(root, index, requires_python)
    for minor in python_minors:
        runtime_lock = index["interpreters"][minor]["runtime"]["distributions"]
        if not any(item["name"] == "nicegui" and item["version"] == nicegui_version for item in runtime_lock):
            raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
        postgres_lock = index["interpreters"][minor]["optional"]["postgres"]["distributions"]
        for requirement in optional_requirements.get("postgres", []):
            match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^;]+)", requirement)
            if match is None or not any(
                item["name"] == _normal_name(match.group(1)) and item["version"] == match.group(2)
                for item in postgres_lock
            ):
                raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")

    build_requires = project_data.get("build-system", {}).get("requires")
    build_locked = index.get("build", {}).get("distributions")
    if not isinstance(build_requires, list) or not isinstance(build_locked, list):
        raise ReleaseFailure("BUILD_CONTRACT_INVALID")
    locked_build = sorted(f"{item['name']}=={item['version']}" for item in build_locked)
    if sorted(build_requires) != locked_build:
        raise ReleaseFailure("BUILD_CONTRACT_INVALID")

    try:
        migration_identity = migration_schema_identity(root / "migrations")
    except (OSError, ValueError) as exc:
        raise ReleaseFailure("MIGRATION_IDENTITY_INVALID") from exc

    inventory: dict[str, object] = {
        "schema": RELEASE_SCHEMA,
        "release": {
            "distribution": project_name,
            "version": project_version,
            "application_identity": identity.as_dict(),
            "source_authority": identity.source_authority,
            "source_root": "src/ephi",
            "source_files": _package_source_files(root),
            "entry_points": dict(sorted(scripts.items())),
        },
        "python": {
            "requires_python": requires_python,
            "install_supported_interpreters": python_minors,
            "repository_compatibility_only": _compatibility_lane(root, python_minors),
        },
        "dependencies": {
            "direct": direct_requirements,
            "optional": optional_requirements,
            "lock_index": {
                "path": "release_locks/index.json",
                "sha256": _sha256_bytes(index_bytes),
                "schema": index["schema"],
            },
            "base": {
                "distribution": framework["distribution"],
                "repository": framework["repository"],
                "commit": framework["commit"],
                "framework_version": framework["version"],
                "nicegui_version": nicegui_version,
                "source_requirement": framework["source_requirement"],
            },
            "build_lock": index["build"],
            "installer_lock": index["installer"],
        },
        "build_system": {
            "requires": sorted(build_requires),
            "backend": build_system.get("build-backend"),
        },
        "downstream_abi": {
            "id": ABI_ID,
            "version": ABI_VERSION,
            "provider_contract_version": PROVIDER_CONTRACT_VERSION,
            "manifest_schema": MANIFEST_SCHEMA,
            "required_categories": list(REQUIRED_CATEGORIES),
        },
        "migrations": migration_identity,
        "capabilities": _capability_claims(),
    }
    inventory["release_identity_sha256"] = _sha256_bytes(canonical_json_bytes(inventory))
    return inventory


def verify_inventory_document(value: dict[str, Any], raw: bytes) -> dict[str, Any]:
    if raw != _document_bytes(value) or value.get("schema") != RELEASE_SCHEMA:
        raise ReleaseFailure("RELEASE_INVENTORY_INVALID")
    identity = value.get("release_identity_sha256")
    if not isinstance(identity, str) or not _HEX_64.fullmatch(identity):
        raise ReleaseFailure("RELEASE_INVENTORY_INVALID")
    body = dict(value)
    body.pop("release_identity_sha256", None)
    if _sha256_bytes(canonical_json_bytes(body)) != identity:
        raise ReleaseFailure("RELEASE_INVENTORY_INVALID")
    return value


def _installed_inventory() -> dict[str, Any]:
    path = Path(__file__).with_name("release_inventory.json")
    value, raw = _safe_json(path, "RELEASE_INVENTORY_INVALID")
    return verify_inventory_document(value, raw)


def _installed_lock_root() -> Path:
    return Path(__file__).with_name("release_locks")


def _verify_installed_lock_assets(inventory: dict[str, Any]) -> dict[str, Any]:
    dependencies = inventory.get("dependencies")
    lock_identity = dependencies.get("lock_index") if isinstance(dependencies, dict) else None
    if not isinstance(lock_identity, dict) or lock_identity.get("path") != "release_locks/index.json":
        raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
    root = _installed_lock_root().parent
    index, raw = _safe_json(root / "release_locks" / "index.json", "LOCK_INDEX_MISSING_OR_TAMPERED")
    if raw != _document_bytes(index) or _sha256_bytes(raw) != lock_identity.get("sha256"):
        raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
    if index.get("schema") != LOCK_INDEX_SCHEMA or index.get("python_requires") != inventory["python"]["requires_python"]:
        raise ReleaseFailure("LOCK_INDEX_MISSING_OR_TAMPERED")
    _validate_lock_index(root, index, inventory["python"]["requires_python"], package_root=root)
    return index


def _migration_directory() -> Path:
    source_root = Path(__file__).resolve().parents[2]
    source_migrations = source_root / "migrations"
    if source_migrations.is_dir():
        return source_migrations
    installed_migrations = Path(sysconfig.get_path("data")) / "share" / "ephi" / "migrations"
    if installed_migrations.is_dir():
        return installed_migrations
    raise ReleaseFailure("MIGRATION_IDENTITY_MISMATCH")


def _verify_migrations(inventory: dict[str, Any]) -> None:
    try:
        current = migration_schema_identity(_migration_directory())
    except (OSError, ValueError) as exc:
        raise ReleaseFailure("MIGRATION_IDENTITY_MISMATCH") from exc
    if current != inventory.get("migrations"):
        raise ReleaseFailure("MIGRATION_IDENTITY_MISMATCH")


def _verify_abi(inventory: dict[str, Any]) -> None:
    current = {
        "id": ABI_ID,
        "version": ABI_VERSION,
        "provider_contract_version": PROVIDER_CONTRACT_VERSION,
        "manifest_schema": MANIFEST_SCHEMA,
        "required_categories": list(REQUIRED_CATEGORIES),
    }
    if current != inventory.get("downstream_abi"):
        raise ReleaseFailure("DOWNSTREAM_ABI_IDENTITY_MISMATCH")


def _supported_minor(value: str, supported: list[str]) -> str:
    if platform.python_implementation() != "CPython":
        raise ReleaseFailure("UNSUPPORTED_PYTHON_IMPLEMENTATION")
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    if minor not in supported or value != minor:
        raise ReleaseFailure("UNSUPPORTED_PYTHON_VERSION")
    return minor


def _req_signature(value: str) -> str:
    return "".join(value.casefold().split()).replace("'", "").replace('"', "")


def _python_requires_signature(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, str):
        return None
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not re.fullmatch(r"(?:<=|>=|==|!=|~=|<|>)[0-9][A-Za-z0-9.+-]*", part) for part in parts):
        return None
    return tuple(sorted(parts))


def _wheel_metadata(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and len(PurePosixPath(name).parts) == 2
            ]
            if len(names) != 1:
                raise ReleaseFailure("INSTALL_INPUTS_INVALID")
            message = Parser().parsestr(archive.read(names[0]).decode("utf-8"))
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
        raise ReleaseFailure("INSTALL_INPUTS_INVALID") from exc
    name, version = message.get("Name"), message.get("Version")
    if not isinstance(name, str) or not isinstance(version, str) or not _DIST_NAME.fullmatch(name):
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")
    return _normal_name(name), version


def _direct_url_hash(distribution: metadata.Distribution) -> str | None:
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    archive = parsed.get("archive_info") if isinstance(parsed, dict) else None
    hashes = archive.get("hashes") if isinstance(archive, dict) else None
    digest = hashes.get("sha256") if isinstance(hashes, dict) else None
    if not isinstance(digest, str) and isinstance(archive, dict):
        old_hash = archive.get("hash")
        if isinstance(old_hash, str) and old_hash.startswith("sha256="):
            digest = old_hash.partition("=")[2]
    return digest if isinstance(digest, str) and _HEX_64.fullmatch(digest) else None


def _verify_installed_package_files(app: metadata.Distribution, inventory: dict[str, Any]) -> None:
    release = inventory.get("release")
    files = release.get("source_files") if isinstance(release, dict) else None
    if not isinstance(files, list):
        raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH")
    expected: dict[str, tuple[str, int]] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH")
        source_path = PurePosixPath(item["path"])
        if source_path.parts[:2] != ("src", "ephi") or ".." in source_path.parts:
            raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH")
        rel = PurePosixPath(*source_path.parts[1:]).as_posix()
        digest, size = item.get("sha256"), item.get("byte_size")
        if not isinstance(digest, str) or not _HEX_64.fullmatch(digest) or isinstance(size, bool) or not isinstance(size, int):
            raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH")
        expected[rel] = (digest, size)
    try:
        packaged = {
            PurePosixPath(str(item)).as_posix()
            for item in (app.files or ())
            if PurePosixPath(str(item)).parts[:1] == ("ephi",)
            and PurePosixPath(str(item)).as_posix() != "ephi/release_inventory.json"
            and "__pycache__" not in PurePosixPath(str(item)).parts
            and PurePosixPath(str(item)).suffix != ".pyc"
        }
    except (OSError, TypeError, ValueError) as exc:
        raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH") from exc
    if packaged != set(expected):
        raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH")
    for relative, (expected_digest, expected_size) in expected.items():
        path = Path(app.locate_file(relative))
        try:
            if path.is_symlink() or not path.is_file():
                raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH")
            raw = path.read_bytes()
        except OSError as exc:
            raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH") from exc
        if len(raw) != expected_size or _sha256_bytes(raw) != expected_digest:
            raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH")
    expected_entry_points = release.get("entry_points")
    actual_entry_points = {
        item.name: item.value
        for item in (app.entry_points or ())
        if item.group == "console_scripts"
    }
    if not isinstance(expected_entry_points, dict) or actual_entry_points != expected_entry_points:
        raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH")


def _input_identity(inputs_dir: Path, inventory: dict[str, Any], minor: str, lock_index: dict[str, Any]) -> dict[str, Any]:
    path = inputs_dir / "install_inputs.json"
    value, raw = _safe_json(path, "INSTALL_INPUTS_INVALID")
    if raw != _document_bytes(value) or value.get("schema") != INSTALL_INPUTS_SCHEMA:
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")
    if set(value) != {
        "schema", "release_identity_sha256", "source", "runtime", "locks", "artifacts", "install_inputs_sha256"
    }:
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")
    identity = value.get("install_inputs_sha256")
    if not isinstance(identity, str) or not _HEX_64.fullmatch(identity):
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")
    body = dict(value)
    body.pop("install_inputs_sha256", None)
    if _sha256_bytes(canonical_json_bytes(body)) != identity:
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")
    if value.get("release_identity_sha256") != inventory.get("release_identity_sha256"):
        raise ReleaseFailure("INSTALL_INPUTS_RELEASE_MISMATCH")
    runtime = value.get("runtime")
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {"python_minor", "implementation", "platform"}
        or runtime.get("python_minor") != minor
    ):
        raise ReleaseFailure("INSTALL_INPUTS_RUNTIME_MISMATCH")
    if runtime.get("implementation") != "CPython" or runtime.get("platform") != sysconfig.get_platform():
        raise ReleaseFailure("INSTALL_INPUTS_RUNTIME_MISMATCH")
    source = value.get("source")
    release = inventory.get("release")
    if (
        not isinstance(source, dict)
        or set(source) != {"repository", "commit", "tree"}
        or not isinstance(release, dict)
        or source.get("repository") != release.get("source_authority")
        or not isinstance(source.get("commit"), str)
        or not _HEX_40.fullmatch(source["commit"])
        or not isinstance(source.get("tree"), str)
        or not _HEX_40.fullmatch(source["tree"])
    ):
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")

    identities = value.get("locks")
    selected = lock_index.get("interpreters", {}).get(minor)
    if (
        not isinstance(identities, dict)
        or set(identities) != {"runtime", "postgres", "installer", "build"}
        or not isinstance(selected, dict)
    ):
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")
    expected_locks = {
        "runtime": selected["runtime"],
        "postgres": selected["optional"]["postgres"],
        "installer": lock_index["installer"],
        "build": lock_index["build"],
    }
    lock_root = _installed_lock_root()
    expected_lock_files: set[str] = set()
    for label, expected in expected_locks.items():
        actual = identities.get(label)
        if (
            not isinstance(actual, dict)
            or set(actual) != {"path", "file", "sha256"}
            or actual.get("sha256") != expected.get("sha256")
            or actual.get("path") != expected.get("path")
        ):
            raise ReleaseFailure("INSTALL_INPUTS_INVALID")
        transfer_path = actual.get("file")
        if not isinstance(transfer_path, str) or not re.fullmatch(r"locks/[A-Za-z0-9_.-]+\.txt", transfer_path):
            raise ReleaseFailure("INSTALL_INPUTS_INVALID")
        expected_lock_files.add(transfer_path.partition("/")[2])
        try:
            transferred = (inputs_dir / transfer_path).read_bytes()
            canonical = (lock_root.parent / expected["path"]).read_bytes()
        except OSError as exc:
            raise ReleaseFailure("INSTALL_INPUTS_TAMPERED") from exc
        if transferred != canonical or _sha256_bytes(transferred) != expected["sha256"]:
            raise ReleaseFailure("INSTALL_INPUTS_TAMPERED")

    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")
    seen_files: set[str] = set()
    seen_distributions: dict[str, str] = {}
    seen_kinds: dict[str, str] = {}
    artifact_order: list[str] = []
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {
            "kind", "distribution", "version", "file", "sha256", "byte_size"
        }:
            raise ReleaseFailure("INSTALL_INPUTS_INVALID")
        relative = item.get("file")
        digest = item.get("sha256")
        size = item.get("byte_size")
        name = item.get("distribution")
        version = item.get("version")
        if (
            not isinstance(relative, str)
            or not _WHEEL_PATH.fullmatch(relative)
            or relative in seen_files
            or not isinstance(digest, str)
            or not _HEX_64.fullmatch(digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or not isinstance(name, str)
            or not _DIST_NAME.fullmatch(name)
            or not isinstance(version, str)
            or item.get("kind") not in {"application", "base", "installer", "runtime", "postgres"}
        ):
            raise ReleaseFailure("INSTALL_INPUTS_INVALID")
        seen_files.add(relative)
        normalized_name = _normal_name(name)
        if normalized_name in seen_distributions:
            raise ReleaseFailure("INSTALL_INPUTS_INVALID")
        seen_distributions[normalized_name] = version
        seen_kinds[normalized_name] = item["kind"]
        artifact_order.append(normalized_name)
        artifact_path = inputs_dir / relative
        try:
            if artifact_path.is_symlink() or not artifact_path.is_file():
                raise ReleaseFailure("INSTALL_INPUTS_TAMPERED")
            content = artifact_path.read_bytes()
        except OSError as exc:
            raise ReleaseFailure("INSTALL_INPUTS_TAMPERED") from exc
        if len(content) != size or _sha256_bytes(content) != digest:
            raise ReleaseFailure("INSTALL_INPUTS_TAMPERED")
        wheel_name, wheel_version = _wheel_metadata(artifact_path)
        if wheel_name != normalized_name or wheel_version != version:
            raise ReleaseFailure("INSTALL_INPUTS_TAMPERED")

    wheel_dir = inputs_dir / "wheelhouse"
    try:
        root_entries = {item.name for item in inputs_dir.iterdir()}
        children = list(wheel_dir.iterdir())
        lock_dir = inputs_dir / "locks"
        if root_entries != {"install_inputs.json", "wheelhouse", "locks"} or any(
            not item.is_file() or item.is_symlink() or item.suffix != ".whl" for item in children
        ):
            raise ReleaseFailure("INSTALL_INPUTS_TAMPERED")
        if {item.name for item in lock_dir.iterdir()} != expected_lock_files or any(
            not item.is_file() or item.is_symlink() for item in lock_dir.iterdir()
        ):
            raise ReleaseFailure("INSTALL_INPUTS_TAMPERED")
        actual_files = {f"wheelhouse/{item.name}" for item in children}
    except ReleaseFailure:
        raise
    except OSError as exc:
        raise ReleaseFailure("INSTALL_INPUTS_TAMPERED") from exc
    if actual_files != seen_files:
        raise ReleaseFailure("INSTALL_INPUTS_TAMPERED")
    if artifact_order != sorted(artifact_order):
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")

    expected_packages: dict[str, str] = {}
    runtime_entry = selected["runtime"]["distributions"]
    for item in runtime_entry:
        marker = item.get("marker")
        if marker == 'sys_platform == "win32"' and sys.platform != "win32":
            continue
        if marker == 'sys_platform != "win32"' and sys.platform == "win32":
            continue
        expected_packages[item["name"]] = item["version"]
    runtime_names = set(expected_packages)
    for item in selected["optional"]["postgres"]["distributions"]:
        expected_packages[item["name"]] = item["version"]
    expected_kinds = {name: "runtime" for name in runtime_names}
    optional_names = {item["name"] for item in selected["optional"]["postgres"]["distributions"]}
    for name in optional_names - runtime_names:
        expected_kinds[name] = "postgres"
    expected_packages["pip"] = lock_index["installer"]["distributions"][0]["version"]
    expected_kinds["pip"] = "installer"
    release = inventory["release"]
    base = inventory["dependencies"]["base"]
    expected_packages[_normal_name(str(release["distribution"]))] = str(release["version"])
    expected_packages[_normal_name(str(base["distribution"]))] = str(base["framework_version"])
    expected_kinds[_normal_name(str(release["distribution"]))] = "application"
    expected_kinds[_normal_name(str(base["distribution"]))] = "base"
    if seen_distributions != expected_packages:
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")
    if seen_kinds != expected_kinds:
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")

    return value


def _verify_installed_subject(inventory: dict[str, Any], lock_index: dict[str, Any], inputs: dict[str, Any], minor: str) -> None:
    try:
        app = metadata.distribution("ephi")
        base_dist = metadata.distribution("nicegui-base")
        nicegui = metadata.distribution("nicegui")
        pip = metadata.distribution("pip")
    except metadata.PackageNotFoundError as exc:
        raise ReleaseFailure("INSTALLED_DISTRIBUTION_MISSING") from exc
    release = inventory["release"]
    dependencies = inventory["dependencies"]
    base_contract = dependencies["base"]
    if app.version != release["version"] or _normal_name(app.metadata.get("Name", "")) != release["distribution"]:
        raise ReleaseFailure("APPLICATION_RELEASE_IDENTITY_MISMATCH")
    if _python_requires_signature(app.metadata.get("Requires-Python")) != _python_requires_signature(inventory["python"]["requires_python"]):
        raise ReleaseFailure("PYTHON_INSTALL_CONTRACT_INVALID")
    _verify_installed_package_files(app, inventory)
    if base_dist.version != base_contract["framework_version"] or nicegui.version != base_contract["nicegui_version"]:
        raise ReleaseFailure("DIRECT_DEPENDENCY_MISMATCH")
    if _python_requires_signature(base_dist.metadata.get("Requires-Python")) != _python_requires_signature(inventory["python"]["requires_python"]):
        raise ReleaseFailure("DIRECT_DEPENDENCY_MISMATCH")
    if pip.version != lock_index["installer"]["distributions"][0]["version"]:
        raise ReleaseFailure("INSTALLER_IDENTITY_MISMATCH")

    expected_requires = [
        _req_signature(item)
        for item in dependencies["direct"]
    ]
    for extra, values in dependencies["optional"].items():
        expected_requires.extend(_req_signature(f'{item}; extra == "{extra}"') for item in values)
    actual_requires = sorted(_req_signature(item) for item in (app.requires or []))
    if sorted(expected_requires) != actual_requires:
        raise ReleaseFailure("DIRECT_DEPENDENCY_MISMATCH")
    base_requires = [item for item in (base_dist.requires or []) if "extra==" not in _req_signature(item)]
    if [_req_signature(item) for item in base_requires] != [_req_signature("nicegui==" + str(base_contract["nicegui_version"]))]:
        raise ReleaseFailure("DIRECT_DEPENDENCY_MISMATCH")

    input_artifacts = {item["distribution"]: item for item in inputs["artifacts"]}
    app_artifact = input_artifacts.get(release["distribution"])
    base_artifact = input_artifacts.get(base_contract["distribution"])
    if not isinstance(app_artifact, dict) or not isinstance(base_artifact, dict):
        raise ReleaseFailure("INSTALL_INPUTS_INVALID")
    if _direct_url_hash(app) != app_artifact.get("sha256") or _direct_url_hash(base_dist) != base_artifact.get("sha256"):
        raise ReleaseFailure("INSTALLED_ARTIFACT_MISMATCH")

    selected = lock_index["interpreters"][minor]
    runtime_expected: dict[str, str] = {}
    for item in selected["runtime"]["distributions"]:
        marker = item.get("marker")
        if marker == 'sys_platform == "win32"' and sys.platform != "win32":
            continue
        if marker == 'sys_platform != "win32"' and sys.platform == "win32":
            continue
        runtime_expected[item["name"]] = item["version"]
    optional_expected = {item["name"]: item["version"] for item in selected["optional"]["postgres"]["distributions"]}

    actual: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        version = dist.version
        if isinstance(name, str) and isinstance(version, str):
            actual[_normal_name(name)] = version
    allowed = dict(runtime_expected)
    allowed.update({"ephi": str(release["version"]), "nicegui-base": str(base_contract["framework_version"]), "pip": pip.version})
    optional_only = set(optional_expected) - set(runtime_expected)
    optional_present = set(actual) & optional_only
    if optional_present:
        if optional_present != optional_only:
            raise ReleaseFailure("LOCKED_DEPENDENCY_SET_MISMATCH")
        for name, version in optional_expected.items():
            if name in allowed and allowed[name] != version:
                raise ReleaseFailure("LOCKED_DEPENDENCY_SET_MISMATCH")
            allowed[name] = version
    if actual != allowed:
        raise ReleaseFailure("LOCKED_DEPENDENCY_SET_MISMATCH")
    for name, version in runtime_expected.items():
        if actual.get(name) != version:
            raise ReleaseFailure("LOCKED_DEPENDENCY_SET_MISMATCH")

def release_preflight(inputs_dir: str | Path) -> dict[str, Any]:
    """Validate the installed package against its immutable release inputs."""

    inventory = _installed_inventory()
    lock_index = _verify_installed_lock_assets(inventory)
    _verify_migrations(inventory)
    _verify_abi(inventory)
    supported = inventory.get("python", {}).get("install_supported_interpreters")
    if not isinstance(supported, list) or not all(isinstance(item, str) for item in supported):
        raise ReleaseFailure("RELEASE_INVENTORY_INVALID")
    minor = _supported_minor(f"{sys.version_info.major}.{sys.version_info.minor}", supported)
    inputs = _input_identity(Path(inputs_dir), inventory, minor, lock_index)
    _verify_installed_subject(inventory, lock_index, inputs, minor)

    claims = inventory["capabilities"]
    report = {
        "schema": PREFLIGHT_SCHEMA,
        "status": "PASS",
        "reason_code": "RELEASE_PREFLIGHT_PASS",
        "release_identity_sha256": inventory["release_identity_sha256"],
        "install_inputs_sha256": inputs["install_inputs_sha256"],
        "candidate_source": {
            "commit": inputs["source"]["commit"],
            "tree": inputs["source"]["tree"],
        },
        "application": {
            "distribution": inventory["release"]["distribution"],
            "version": inventory["release"]["version"],
        },
        "python_requires": inventory["python"]["requires_python"],
        "runtime": {
            "python_minor": minor,
            "install_supported": True,
        },
        "dependencies": inventory["dependencies"],
        "downstream_abi": inventory["downstream_abi"],
        "migrations": {
            "count": inventory["migrations"]["migration_count"],
            "identity_sha256": inventory["migrations"]["identity_sha256"],
        },
        "checks": {
            "release_inventory": "PASS",
            "immutable_install_inputs": "PASS",
            "dependency_and_base_identity": "PASS",
            "migration_identity": "PASS",
            "downstream_abi_identity": "PASS",
        },
        "provider_composition": {
            "authority": "ephi-downstream-preflight",
            "status": "NOT_RUN",
        },
        "capabilities": claims,
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate EPHI release/install identity before downstream provider composition.")
    parser.add_argument("--inputs-dir", required=True, help="Prepared immutable install-input bundle directory.")
    parser.add_argument("--json", action="store_true", help="Emit the secret-safe JSON report.")
    args = parser.parse_args(argv)
    try:
        report = release_preflight(args.inputs_dir)
    except ReleaseFailure as exc:
        report = {
            "schema": PREFLIGHT_SCHEMA,
            "status": "FAIL",
            "reason_code": exc.reason_code,
        }
    except Exception:
        report = {
            "schema": PREFLIGHT_SCHEMA,
            "status": "FAIL",
            "reason_code": "PREFLIGHT_INTERNAL_FAILURE",
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "INSTALL_INPUTS_SCHEMA",
    "LOCK_INDEX_SCHEMA",
    "PREFLIGHT_SCHEMA",
    "RELEASE_SCHEMA",
    "ReleaseFailure",
    "build_release_inventory",
    "canonical_json_bytes",
    "main",
    "release_preflight",
    "verify_inventory_document",
]
