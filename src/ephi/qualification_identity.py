"""Identity checks for the separately transferred synthetic qualification kit."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata as metadata
import json
from pathlib import Path, PurePosixPath
import re
import sys
import sysconfig
from typing import Any

from ephi.downstream import preflight as downstream_preflight
from ephi.release_identity import (
    ReleaseFailure,
    _normal_name,
    _sha256_bytes,
    _wheel_metadata,
    canonical_json_bytes,
)


QUALIFICATION_INPUTS_SCHEMA = "org.ephi.qualification-kit-inputs.v1"
QUALIFICATION_DISTRIBUTION = "ephi-synthetic-downstream-qualification"
QUALIFICATION_VERSION = "1.0.0"
PROVIDER_ENTRYPOINT = "examples.synthetic_downstream.provider:build_bundle"
QUALIFICATION_DEPENDENCIES = {
    "greenlet": "3.5.5",
    "playwright": "1.62.0",
    "pyee": "13.0.1",
}
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_DIST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WHEEL = re.compile(r"^wheelhouse/[A-Za-z0-9_.+-]+\.whl$")
_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+) \\$")
_HASH = re.compile(r"^\s+--hash=sha256:([0-9a-f]{64})$")


class QualificationFailure(ValueError):
    """Fixed reason only; qualification preflights never expose input values."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _safe_document(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID") from exc
    if not isinstance(document, dict) or raw != canonical_json_bytes(document) + b"\n":
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
    return document, raw


def _safe_child(root: Path, relative: str) -> Path:
    path_value = PurePosixPath(relative)
    if path_value.is_absolute() or ".." in path_value.parts or "\\" in relative:
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
    path = root.joinpath(*path_value.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED") from exc
    if path.is_symlink() or not path.is_file():
        raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED")
    return path


def _lock_records(path: Path) -> list[tuple[str, str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED") from exc
    records: list[tuple[str, str, str]] = []
    current: tuple[str, str] | None = None
    for line in lines:
        if not line or line.lstrip().startswith("#"):
            continue
        if "--index-url" in line or "--extra-index-url" in line or "--trusted-host" in line:
            raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
        match = _REQUIREMENT.fullmatch(line)
        if match:
            if current is not None:
                raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
            current = (_normal_name(match.group(1)), match.group(2))
            continue
        hash_match = _HASH.fullmatch(line)
        if hash_match is None or current is None:
            raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
        records.append((*current, hash_match.group(1)))
        current = None
    if current is not None or not records or len({item[0] for item in records}) != len(records):
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
    return records


def _distribution_content(distribution: metadata.Distribution, records: list[dict[str, object]]) -> None:
    expected = {str(item["path"]): (str(item["sha256"]), int(item["byte_size"])) for item in records}
    listed = {PurePosixPath(str(item)).as_posix() for item in (distribution.files or ())}
    listed_fixture = {
        item for item in listed
        if item.startswith("examples/synthetic_downstream/") and item.endswith(".py")
    }
    if not expected or len(expected) != len(records) or set(expected) != listed_fixture:
        raise QualificationFailure("QUALIFICATION_PROVIDER_CONTENT_MISMATCH")
    for relative, (digest, size) in expected.items():
        if not relative.startswith("examples/synthetic_downstream/") or not _HEX_64.fullmatch(digest):
            raise QualificationFailure("QUALIFICATION_PROVIDER_CONTENT_MISMATCH")
        path = Path(distribution.locate_file(relative))
        try:
            if path.is_symlink() or not path.is_file():
                raise QualificationFailure("QUALIFICATION_PROVIDER_CONTENT_MISMATCH")
            raw = path.read_bytes()
        except OSError as exc:
            raise QualificationFailure("QUALIFICATION_PROVIDER_CONTENT_MISMATCH") from exc
        if len(raw) != size or _sha256_bytes(raw) != digest:
            raise QualificationFailure("QUALIFICATION_PROVIDER_CONTENT_MISMATCH")


def qualification_input_identity(
    qualification_inputs_dir: str | Path,
    release_inputs: dict[str, Any],
) -> tuple[dict[str, object], dict[str, str]]:
    """Verify exact transferred wheels and installed provider identity, without PostgreSQL."""

    root = Path(qualification_inputs_dir).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise QualificationFailure("QUALIFICATION_INPUTS_UNAVAILABLE")
    document, _raw = _safe_document(root / "qualification_inputs.json")
    fields = {
        "schema", "ephi_release_identity_sha256", "source", "runtime", "provider",
        "requirements_sha256", "lock", "artifacts", "qualification_kit_identity_sha256",
    }
    if set(document) != fields or document.get("schema") != QUALIFICATION_INPUTS_SCHEMA:
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
    requirements_sha256 = document.get("requirements_sha256")
    if not isinstance(requirements_sha256, str) or not _HEX_64.fullmatch(requirements_sha256):
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
    requirements_path = _safe_child(root, "qualification-requirements.txt")
    try:
        requirements_raw = requirements_path.read_bytes()
    except OSError as exc:
        raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED") from exc
    if _sha256_bytes(requirements_raw) != requirements_sha256:
        raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED")
    try:
        requirement_lines = [
            line.strip() for line in requirements_raw.decode("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except UnicodeError as exc:
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID") from exc
    expected_lines = [f"{name}=={version}" for name, version in sorted(QUALIFICATION_DEPENDENCIES.items())]
    if requirement_lines != expected_lines:
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
    kit_identity = document.get("qualification_kit_identity_sha256")
    body = dict(document)
    body.pop("qualification_kit_identity_sha256", None)
    if not isinstance(kit_identity, str) or not _HEX_64.fullmatch(kit_identity):
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
    if _sha256_bytes(canonical_json_bytes(body)) != kit_identity:
        raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED")

    release_identity = release_inputs.get("release_identity_sha256")
    source = release_inputs.get("source")
    runtime = release_inputs.get("runtime")
    if (
        document.get("ephi_release_identity_sha256") != release_identity
        or document.get("source") != source
        or document.get("runtime") != runtime
        or not isinstance(source, dict)
        or not isinstance(source.get("commit"), str)
        or not _HEX_40.fullmatch(source["commit"])
        or not isinstance(source.get("tree"), str)
        or not _HEX_40.fullmatch(source["tree"])
        or not isinstance(runtime, dict)
        or runtime.get("implementation") != "CPython"
        or runtime.get("python_minor") != f"{sys.version_info.major}.{sys.version_info.minor}"
        or runtime.get("platform") != sysconfig.get_platform()
    ):
        raise QualificationFailure("QUALIFICATION_INPUTS_RELEASE_MISMATCH")

    provider = document.get("provider")
    if (
        not isinstance(provider, dict)
        or set(provider) != {
            "distribution", "version", "entrypoint", "abi_id", "abi_version",
            "manifest_sha256", "artifact_sha256", "content_files",
        }
        or provider.get("distribution") != QUALIFICATION_DISTRIBUTION
        or provider.get("version") != QUALIFICATION_VERSION
        or provider.get("entrypoint") != PROVIDER_ENTRYPOINT
        or not isinstance(provider.get("abi_id"), str)
        or not isinstance(provider.get("abi_version"), str)
        or not isinstance(provider.get("manifest_sha256"), str)
        or not _HEX_64.fullmatch(provider["manifest_sha256"])
        or not isinstance(provider.get("artifact_sha256"), str)
        or not _HEX_64.fullmatch(provider["artifact_sha256"])
        or not isinstance(provider.get("content_files"), list)
    ):
        raise QualificationFailure("QUALIFICATION_PROVIDER_IDENTITY_MISMATCH")

    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 4:
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
    names: dict[str, str] = {}
    artifact_records: list[tuple[str, str, str]] = []
    provider_artifacts = 0
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {
            "kind", "distribution", "version", "file", "sha256", "byte_size",
        }:
            raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
        name, version, file_name = item.get("distribution"), item.get("version"), item.get("file")
        digest, size, kind = item.get("sha256"), item.get("byte_size"), item.get("kind")
        if (
            not isinstance(name, str) or not _DIST.fullmatch(name)
            or not isinstance(version, str) or not isinstance(file_name, str) or not _WHEEL.fullmatch(file_name)
            or not isinstance(digest, str) or not _HEX_64.fullmatch(digest)
            or isinstance(size, bool) or not isinstance(size, int) or size < 1
            or kind not in {"provider", "qualification_dependency"}
        ):
            raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
        normalized = _normal_name(name)
        if normalized in names:
            raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
        names[normalized] = version
        artifact_path = _safe_child(root, file_name)
        raw = artifact_path.read_bytes()
        if len(raw) != size or _sha256_bytes(raw) != digest or _wheel_metadata(artifact_path) != (normalized, version):
            raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED")
        artifact_records.append((normalized, version, digest))
        if kind == "provider":
            provider_artifacts += 1
            if normalized != QUALIFICATION_DISTRIBUTION or digest != provider["artifact_sha256"]:
                raise QualificationFailure("QUALIFICATION_PROVIDER_IDENTITY_MISMATCH")
        elif normalized == QUALIFICATION_DISTRIBUTION:
            raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
    if (
        provider_artifacts != 1
        or {name: version for name, version in names.items() if name != QUALIFICATION_DISTRIBUTION}
        != QUALIFICATION_DEPENDENCIES
        or names.get(QUALIFICATION_DISTRIBUTION) != QUALIFICATION_VERSION
        or artifact_records != sorted(artifact_records)
    ):
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")

    lock = document.get("lock")
    if (
        not isinstance(lock, dict)
        or set(lock) != {"file", "sha256"}
        or not isinstance(lock.get("file"), str)
        or not re.fullmatch(r"locks/qualification-py3(?:11|12|13)\.txt", lock["file"])
        or not isinstance(lock.get("sha256"), str)
        or not _HEX_64.fullmatch(lock["sha256"])
    ):
        raise QualificationFailure("QUALIFICATION_INPUTS_INVALID")
    lock_path = _safe_child(root, lock["file"])
    lock_raw = lock_path.read_bytes()
    if _sha256_bytes(lock_raw) != lock["sha256"]:
        raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED")
    if sorted(_lock_records(lock_path)) != sorted(artifact_records):
        raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED")
    try:
        root_entries = {item.name for item in root.iterdir()}
        wheel_dir = root / "wheelhouse"
        locks_dir = root / "locks"
        if (
            root_entries != {"qualification_inputs.json", "qualification-requirements.txt", "wheelhouse", "locks"}
            or wheel_dir.is_symlink() or not wheel_dir.is_dir()
            or locks_dir.is_symlink() or not locks_dir.is_dir()
            or {item.name for item in wheel_dir.iterdir()} != {Path(str(item["file"])).name for item in artifacts}
            or {item.name for item in locks_dir.iterdir()} != {Path(lock["file"]).name}
            or any(not item.is_file() or item.is_symlink() for item in wheel_dir.iterdir())
            or any(not item.is_file() or item.is_symlink() for item in locks_dir.iterdir())
        ):
            raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED")
    except QualificationFailure:
        raise
    except OSError as exc:
        raise QualificationFailure("QUALIFICATION_INPUTS_TAMPERED") from exc

    try:
        provider_distribution = metadata.distribution(QUALIFICATION_DISTRIBUTION)
    except metadata.PackageNotFoundError as exc:
        raise QualificationFailure("QUALIFICATION_PROVIDER_NOT_INSTALLED") from exc
    if provider_distribution.version != QUALIFICATION_VERSION or provider_distribution.requires:
        raise QualificationFailure("QUALIFICATION_PROVIDER_IDENTITY_MISMATCH")
    content_files = provider.get("content_files")
    if not all(isinstance(item, dict) and set(item) == {"path", "byte_size", "sha256"} for item in content_files):
        raise QualificationFailure("QUALIFICATION_PROVIDER_CONTENT_MISMATCH")
    _distribution_content(provider_distribution, content_files)
    provider_path = Path(provider_distribution.locate_file("examples/synthetic_downstream/provider.py"))
    try:
        imported = importlib.import_module("examples.synthetic_downstream.provider")
    except Exception as exc:
        raise QualificationFailure("QUALIFICATION_PROVIDER_NOT_IMPORTABLE") from exc
    if Path(str(imported.__file__)).resolve() != provider_path.resolve():
        raise QualificationFailure("QUALIFICATION_PROVIDER_IMPORT_MISMATCH")
    module_name = PROVIDER_ENTRYPOINT.partition(":")[0]
    module = sys.modules.get(module_name)
    if module is None or Path(str(getattr(module, "__file__", ""))).resolve() != provider_path.resolve():
        raise QualificationFailure("QUALIFICATION_PROVIDER_IMPORT_MISMATCH")

    conformance = downstream_preflight(PROVIDER_ENTRYPOINT, compose=False)
    if (
        conformance.get("status_code") != "CONTRACT_PASS"
        or conformance.get("compatibility", {}).get("status") != "PASS"
        or conformance.get("downstream_abi", {}).get("id") != provider["abi_id"]
        or conformance.get("downstream_abi", {}).get("version") != provider["abi_version"]
        or conformance.get("downstream_abi", {}).get("safe_manifest_hash") != provider["manifest_sha256"]
        or conformance.get("synthetic_boundary", {}).get("status") != "PASS"
    ):
        raise QualificationFailure("QUALIFICATION_PROVIDER_CONFORMANCE_MISMATCH")

    return ({
        "schema": QUALIFICATION_INPUTS_SCHEMA,
        "qualification_kit_identity_sha256": kit_identity,
        "qualification_inputs_sha256": _sha256_bytes(canonical_json_bytes(document) + b"\n"),
        "provider": {
            "distribution": QUALIFICATION_DISTRIBUTION,
            "version": QUALIFICATION_VERSION,
            "entrypoint": PROVIDER_ENTRYPOINT,
            "artifact_sha256": provider["artifact_sha256"],
            "abi_id": provider["abi_id"],
            "abi_version": provider["abi_version"],
            "manifest_sha256": provider["manifest_sha256"],
            "content_file_count": len(content_files),
        },
        "qualification_dependencies": [
            {"distribution": name, "version": version}
            for name, version in sorted(names.items()) if name != QUALIFICATION_DISTRIBUTION
        ],
        "artifacts": [
            {
                "kind": item["kind"],
                "distribution": item["distribution"],
                "version": item["version"],
                "sha256": item["sha256"],
                "byte_size": item["byte_size"],
            }
            for item in artifacts
        ],
        "source": source,
        "release_identity_sha256": release_identity,
        "conformance": {
            "status": conformance["compatibility"]["status"],
            "reason_code": conformance["compatibility"]["reason_code"],
        },
    }, names)


__all__ = [
    "PROVIDER_ENTRYPOINT",
    "QUALIFICATION_DEPENDENCIES",
    "QUALIFICATION_DISTRIBUTION",
    "QUALIFICATION_INPUTS_SCHEMA",
    "QUALIFICATION_VERSION",
    "QualificationFailure",
    "qualification_input_identity",
]
