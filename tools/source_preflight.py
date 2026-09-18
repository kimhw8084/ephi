#!/usr/bin/env python3
"""LEGACY OPTIONAL: inspect historical source-artifact compatibility inputs.

This module is retained only for historical CHG-85/CHG-104/CHG-109 fixture
coverage. It is not part of the canonical Git-native W0 baseline and must not
be used as an application installation, package, or implementation gate.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import sys
import tempfile
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ARCHIVE_FILENAME = "ephi_v0.19.1_production_hardened(1).zip"
EXPECTED_ARCHIVE_SHA256 = "5e9ad8f63b3158adc530af69fc650aec606cbfa64896ba73840cdcf994a2b6e3"
EXPECTED_SOURCE_ROOT = "ephi_v0.19.1_production_hardened_release"
HISTORICAL_TEST_RESULT = {
    "status": "REFERENCE_ONLY",
    "source": "historical original-application audit",
    "passed": 275,
    "skipped": 1,
    "skip_reason": "optional PyArrow integration unavailable",
    "rerun_in_current_repository": False,
}
DEFAULT_ARCHIVE = REPOSITORY_ROOT / "artifacts" / "source" / EXPECTED_ARCHIVE_FILENAME
DEFAULT_STAGE = REPOSITORY_ROOT / "artifacts" / "source-staging"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "artifacts" / "source-preflight.json"


class PreflightError(ValueError):
    """A source or staging condition that must fail closed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Member:
    name: str
    relative: PurePosixPath
    is_directory: bool
    info: zipfile.ZipInfo


def sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _path_is_under(path: Path, roots: tuple[Path, ...]) -> bool:
    candidate = path.resolve(strict=False)
    return any(candidate.is_relative_to(root.resolve(strict=False)) for root in roots)


def _reject_protected_path(path: Path, protected_roots: tuple[Path, ...], label: str) -> None:
    if _path_is_under(path, protected_roots):
        raise PreflightError("PROTECTED_PATH", f"{label} is inside preserved evidence")


def _validate_member_name(name: str) -> tuple[PurePosixPath, str]:
    if not name or "\x00" in name or "\\" in name or ":" in name:
        raise PreflightError("UNSAFE_MEMBER_PATH", f"unsafe archive member path: {name!r}")
    if PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute():
        raise PreflightError("UNSAFE_MEMBER_PATH", f"unsafe archive member path: {name!r}")
    if PureWindowsPath(name).drive:
        raise PreflightError("UNSAFE_MEMBER_PATH", f"unsafe archive member path: {name!r}")
    raw_parts = name.split("/")
    if raw_parts[-1] == "":
        raw_parts.pop()
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise PreflightError("UNSAFE_MEMBER_PATH", f"unsafe archive member path: {name!r}")
    relative = PurePosixPath(*raw_parts)
    return relative, "/".join(raw_parts)


def _member_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0o177777


def validate_members(archive: zipfile.ZipFile, source_root: str) -> list[Member]:
    members: list[Member] = []
    names: set[str] = set()
    folded_names: dict[str, str] = {}
    kinds: dict[str, str] = {}
    root_prefix = source_root.rstrip("/") + "/"

    for info in archive.infolist():
        relative, normalized = _validate_member_name(info.filename)
        if normalized != source_root and not normalized.startswith(root_prefix):
            raise PreflightError("SOURCE_ROOT_MISMATCH", f"archive member is outside {source_root!r}")
        mode = _member_mode(info)
        file_type = stat.S_IFMT(mode)
        if stat.S_ISLNK(mode):
            raise PreflightError("UNSAFE_MEMBER_SYMLINK", f"symlink archive member: {info.filename!r}")
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise PreflightError("UNSAFE_MEMBER_SPECIAL_FILE", f"special archive member: {info.filename!r}")
        is_directory = info.is_dir() or info.filename.endswith("/") or file_type == stat.S_IFDIR
        if normalized == source_root and not is_directory:
            raise PreflightError("SOURCE_ROOT_NOT_DIRECTORY", f"archive root {source_root!r} is not a directory")
        if info.flag_bits & 0x1:
            raise PreflightError("ENCRYPTED_MEMBER", f"encrypted archive member: {info.filename!r}")
        if normalized in names:
            raise PreflightError("DUPLICATE_MEMBER", f"duplicate archive member: {normalized!r}")
        folded = normalized.casefold()
        if folded in folded_names:
            raise PreflightError("AMBIGUOUS_MEMBER", f"case-colliding archive member: {normalized!r}")
        names.add(normalized)
        folded_names[folded] = normalized
        kinds[normalized] = "directory" if is_directory else "file"
        members.append(Member(info.filename, relative, is_directory, info))

    if not members:
        raise PreflightError("EMPTY_ARCHIVE", "source archive contains no members")
    if source_root not in names and not any(name.startswith(root_prefix) for name in names):
        raise PreflightError("SOURCE_ROOT_MISSING", f"archive root {source_root!r} is missing")

    for normalized, kind in kinds.items():
        path = PurePosixPath(normalized)
        for parent in path.parents:
            parent_name = parent.as_posix()
            if parent_name in kinds and kinds[parent_name] == "file":
                raise PreflightError("MEMBER_CONFLICT", f"file member contains a child: {parent_name!r}")
        if kind == "file" and normalized in kinds and kinds[normalized] != "file":
            raise PreflightError("MEMBER_CONFLICT", f"member has conflicting types: {normalized!r}")
    return sorted(members, key=lambda member: (member.relative.as_posix(), not member.is_directory))


def _safe_destination(root: Path, relative: PurePosixPath) -> Path:
    destination = root.joinpath(*relative.parts)
    if not destination.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
        raise PreflightError("UNSAFE_EXTRACTION_PATH", f"member escapes staging root: {relative.as_posix()!r}")
    return destination


def _extract(archive: zipfile.ZipFile, members: list[Member], target: Path) -> None:
    for member in members:
        destination = _safe_destination(target, member.relative)
        if member.is_directory:
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(destination, flags, 0o600)
        except OSError as exc:
            raise PreflightError("UNSAFE_EXTRACTION_PATH", f"cannot create safe member: {member.name!r}") from exc
        with os.fdopen(descriptor, "wb") as output, archive.open(member.info, "r") as source:
            shutil.copyfileobj(source, output)
        mode = _member_mode(member.info) & 0o777
        if mode:
            destination.chmod(mode)


def _source_files(root: Path) -> list[Path]:
    return [
        path for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
        and not {".git", ".venv", "__pycache__", ".pytest_cache"}.intersection(path.parts)
    ]


def _python_line_count(path: Path) -> int:
    try:
        with tokenize_open(path) as stream:
            return len(stream.read().splitlines())
    except (OSError, UnicodeError):
        return 0


def tokenize_open(path: Path):
    # Local import keeps the module's dependency surface visibly standard-library-only.
    import tokenize
    return tokenize.open(path)


def discover_baseline(source_root: Path) -> dict:
    files = _source_files(source_root)
    python_files = [path for path in files if path.suffix == ".py"]
    test_files = [
        path for path in python_files
        if "tests" in path.relative_to(source_root).parts
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    ]
    dependency_names = {
        "Pipfile", "Pipfile.lock", "poetry.lock", "pyproject.toml", "requirements.txt",
        "requirements-dev.txt", "requirements-prod.txt", "setup.cfg", "setup.py",
        "tox.ini", "uv.lock", "environment.yml", "environment.yaml",
    }
    dependency_files = [path for path in files if path.name in dependency_names]
    test_roots = sorted({
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("tests")
        if path.is_dir() and not path.is_symlink()
    })
    config_names = {"pytest.ini", "tox.ini", "noxfile.py", "pyproject.toml", "setup.cfg"}
    runner = "pytest" if any(path.name in config_names for path in files) else "unittest"
    return {
        "status": "DISCOVERED",
        "source_root": source_root.as_posix(),
        "python_runtime": {
            "executable": sys.executable,
            "version": sys.version,
        },
        "python_files": len(python_files),
        "python_lines": sum(_python_line_count(path) for path in python_files),
        "test_files": len(test_files),
        "test_roots": test_roots,
        "dependency_files": [path.relative_to(source_root).as_posix() for path in dependency_files],
        "installed_distributions": sorted(
            {f"{dist.metadata['Name']}=={dist.version}" for dist in importlib.metadata.distributions()
             if dist.metadata.get("Name")},
            key=str.casefold,
        ),
        "test_execution": {
            "status": "NOT_RUN",
            "executed": False,
            "runner_discovered": runner,
            "command": None,
            "passed": None,
            "skipped": None,
        },
    }


def _result(archive_path: Path, stage_dir: Path) -> dict:
    return {
        "schema_version": 1,
        "status": None,
        "reason": None,
        "message": None,
        "source": {
            "required_filename": EXPECTED_ARCHIVE_FILENAME,
            "required_sha256": EXPECTED_ARCHIVE_SHA256,
            "required_source_root": EXPECTED_SOURCE_ROOT,
            "archive_path": str(archive_path),
            "archive_present": False,
            "archive_bytes": None,
            "archive_sha256": None,
            "verified": False,
        },
        "staging": {
            "status": "NOT_RUN",
            "requested_path": str(stage_dir),
            "source_root": None,
            "member_count": None,
        },
        "baseline": {"status": "NOT_RUN"},
        "historical_test_result": HISTORICAL_TEST_RESULT,
        "current_run": {
            "status": "NOT_RUN",
            "tests_executed": False,
            "passed": None,
            "skipped": None,
        },
    }


def _failure(result: dict, error: PreflightError) -> dict:
    result["status"] = "SOURCE_REJECTED"
    result["reason"] = error.code
    result["message"] = str(error)
    return result


def preflight(
    archive_path: Path,
    stage_dir: Path,
    *,
    expected_filename: str = EXPECTED_ARCHIVE_FILENAME,
    expected_sha256: str = EXPECTED_ARCHIVE_SHA256,
    expected_source_root: str = EXPECTED_SOURCE_ROOT,
    protected_roots: tuple[Path, ...] = (REPOSITORY_ROOT / "evidence",),
) -> dict:
    archive_path = Path(archive_path).expanduser()
    stage_dir = Path(stage_dir).expanduser()
    result = _result(archive_path, stage_dir)
    result["source"]["required_filename"] = expected_filename
    result["source"]["required_sha256"] = expected_sha256
    result["source"]["required_source_root"] = expected_source_root
    try:
        _reject_protected_path(archive_path, protected_roots, "archive path")
        _reject_protected_path(stage_dir, protected_roots, "staging path")
        if archive_path.name != expected_filename:
            raise PreflightError("SOURCE_FILENAME_MISMATCH", f"archive filename must be {expected_filename!r}")
        if archive_path.is_symlink():
            raise PreflightError("ARCHIVE_SYMLINK", "archive path must not be a symlink")
        if not archive_path.exists():
            result["status"] = "SOURCE_REQUIRED"
            result["reason"] = "ARCHIVE_MISSING"
            result["message"] = "the exact original application archive is required"
            return result
        if not archive_path.is_file():
            raise PreflightError("ARCHIVE_NOT_REGULAR_FILE", "archive path is not a regular file")
        archive_bytes, archive_hash = sha256(archive_path)
        result["source"].update({"archive_present": True, "archive_bytes": archive_bytes, "archive_sha256": archive_hash})
        if archive_hash != expected_sha256:
            raise PreflightError("SOURCE_HASH_MISMATCH", "archive SHA-256 does not match the required source identity")
        if stage_dir.exists() or stage_dir.is_symlink():
            raise PreflightError("STAGE_ALREADY_EXISTS", "staging path must be absent; no existing directory was modified")
        stage_dir.parent.mkdir(parents=True, exist_ok=True)
        if stage_dir.parent.is_symlink():
            raise PreflightError("STAGE_PARENT_SYMLINK", "staging parent must not be a symlink")
        with zipfile.ZipFile(archive_path) as archive:
            members = validate_members(archive, expected_source_root)
            temporary = Path(tempfile.mkdtemp(prefix=f".{stage_dir.name}.", dir=stage_dir.parent))
            try:
                _extract(archive, members, temporary)
                staged_source = temporary / expected_source_root
                baseline = discover_baseline(staged_source)
                os.replace(temporary, stage_dir)
                staged_source = stage_dir / expected_source_root
                baseline["source_root"] = staged_source.as_posix()
            except Exception:
                if temporary.exists():
                    shutil.rmtree(temporary)
                raise
        result["status"] = "SOURCE_STAGED"
        result["reason"] = "VERIFIED_AND_STAGED"
        result["message"] = "source archive hash and member safety checks passed"
        result["source"]["verified"] = True
        result["staging"].update({
            "status": "STAGED",
            "source_root": str(staged_source),
            "member_count": len(members),
        })
        result["baseline"] = baseline
        return result
    except Exception as exc:
        if isinstance(exc, PreflightError):
            return _failure(result, exc)
        code = "INVALID_ZIP" if isinstance(exc, zipfile.BadZipFile) else "PREFLIGHT_IO_ERROR"
        return _failure(result, PreflightError(code, str(exc)))


def write_result(path: Path, result: dict, *, protected_roots: tuple[Path, ...] = (REPOSITORY_ROOT / "evidence",)) -> None:
    path = Path(path).expanduser()
    _reject_protected_path(path, protected_roots, "result output path")
    if path.is_symlink():
        raise PreflightError("OUTPUT_SYMLINK", "result output path must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    except OSError as exc:
        raise PreflightError("OUTPUT_WRITE_ERROR", str(exc)) from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help="optional legacy historical source artifact; not used by canonical W0")
    parser.add_argument("--stage-dir", type=Path, default=DEFAULT_STAGE, help="fresh ignored directory for verified extraction")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="machine-readable result JSON")
    args = parser.parse_args(argv)
    result = preflight(args.archive, args.stage_dir)
    try:
        write_result(args.output, result)
    except PreflightError as exc:
        result = _failure(result, exc)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 4
    print(json.dumps(result, indent=2, sort_keys=True))
    return {"SOURCE_STAGED": 0, "SOURCE_REQUIRED": 2, "SOURCE_REJECTED": 3}.get(result["status"], 4)


if __name__ == "__main__":
    raise SystemExit(main())
