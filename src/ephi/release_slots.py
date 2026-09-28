"""Fail-closed same-release installed slot selection and rollback metadata."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows runtime
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX runtime
    msvcrt = None  # type: ignore[assignment]

from ephi.release_identity import canonical_json_bytes


STATE_SCHEMA = "org.ephi.release-slot-selection.v1"
REPORT_SCHEMA = "org.ephi.release-slot-report.v1"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SLOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_CHANGE_ID = re.compile(r"^(?:CHG-[0-9]{1,12}|U[0-9](?:\.[0-9]{1,3})?)$")
_REASONS = {
    "RELEASE_SLOT_INITIALIZED",
    "RELEASE_SLOT_REGISTERED",
    "RELEASE_SLOT_VERIFIED",
    "RELEASE_SLOT_SELECTED",
    "RELEASE_SLOT_ROLLED_BACK",
    "RELEASE_SLOT_READ",
    "INVALID_ARGUMENTS",
    "STATE_PATH_INVALID",
    "STATE_MISSING",
    "STATE_MALFORMED_OR_TAMPERED",
    "STATE_SCHEMA_UNSUPPORTED",
    "STATE_WRITE_FAILED",
    "EXPECTED_GENERATION_MISMATCH",
    "SLOT_ID_INVALID",
    "SLOT_ROOT_INVALID",
    "SLOT_NOT_REGISTERED",
    "SLOT_ALREADY_REGISTERED",
    "SLOT_ALREADY_CURRENT",
    "NO_PREVIOUS_SLOT",
    "SLOT_PREFLIGHT_FAILED",
    "SLOT_IDENTITY_MISMATCH",
    "CROSS_RELEASE_COMPATIBILITY_NOT_QUALIFIED",
    "ATOMIC_REPLACEMENT_INJECTED",
}


class ReleaseSlotFailure(Exception):
    """A fixed reason code without paths, metadata, or subprocess output."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code if reason_code in _REASONS else "STATE_WRITE_FAILED"
        super().__init__(self.reason_code)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _slot_id(value: str) -> str:
    if not isinstance(value, str) or not _SLOT_ID.fullmatch(value):
        raise ReleaseSlotFailure("SLOT_ID_INVALID")
    return value


def _metadata(label: str | None, change_id: str | None) -> dict[str, str | None]:
    if label is not None and (not isinstance(label, str) or not _LABEL.fullmatch(label)):
        raise ReleaseSlotFailure("INVALID_ARGUMENTS")
    if change_id is not None and (not isinstance(change_id, str) or not _CHANGE_ID.fullmatch(change_id)):
        raise ReleaseSlotFailure("INVALID_ARGUMENTS")
    return {"label": label, "change_id": change_id}


def _safe_absolute_path(value: str | os.PathLike[str], reason: str, *, must_exist: bool) -> Path:
    raw = os.fspath(value)
    candidate = Path(raw)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ReleaseSlotFailure(reason)
    try:
        absolute = Path(os.path.abspath(candidate))
        parent = absolute if must_exist and absolute.is_dir() else absolute.parent
        resolved_parent = parent.resolve(strict=True)
        if parent != resolved_parent:
            raise ReleaseSlotFailure(reason)
        resolved = absolute.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ReleaseSlotFailure(reason) from exc
    if must_exist:
        if resolved != absolute or not absolute.exists() or absolute.is_symlink():
            raise ReleaseSlotFailure(reason)
    else:
        if resolved != absolute:
            raise ReleaseSlotFailure(reason)
        if absolute.exists() and absolute.is_symlink():
            raise ReleaseSlotFailure(reason)
    return absolute


def _state_path(value: str | os.PathLike[str], *, allow_missing: bool) -> Path:
    del allow_missing
    path = _safe_absolute_path(value, "STATE_PATH_INVALID", must_exist=False)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ReleaseSlotFailure("STATE_PATH_INVALID")
    if not path.parent.is_dir():
        raise ReleaseSlotFailure("STATE_PATH_INVALID")
    return path


def _slot_environment(slot_root: str | os.PathLike[str]) -> tuple[Path, Path, Path]:
    root = _safe_absolute_path(slot_root, "SLOT_ROOT_INVALID", must_exist=True)
    if not root.is_dir():
        raise ReleaseSlotFailure("SLOT_ROOT_INVALID")
    binary_dir = root / ("Scripts" if os.name == "nt" else "bin")
    python = binary_dir / ("python.exe" if os.name == "nt" else "python")
    if not python.is_file():
        raise ReleaseSlotFailure("SLOT_ROOT_INVALID")
    preflight = binary_dir / ("ephi-release-preflight.exe" if os.name == "nt" else "ephi-release-preflight")
    if not preflight.is_file() or preflight.is_symlink():
        raise ReleaseSlotFailure("SLOT_ROOT_INVALID")
    try:
        preflight.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ReleaseSlotFailure("SLOT_ROOT_INVALID") from exc
    if python.is_symlink():
        # Standard venvs may link their interpreter to the system runtime.
        # The isolated probe below requires EPHI itself to resolve inside this
        # venv before the release preflight can run.
        return root, python
    try:
        python.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ReleaseSlotFailure("SLOT_ROOT_INVALID") from exc
    return root, python, preflight


def _isolated_environment() -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _run_preflight(slot_root: str | os.PathLike[str], inputs_dir: str | os.PathLike[str]) -> dict[str, str]:
    root, python, preflight = _slot_environment(slot_root)
    inputs = _safe_absolute_path(inputs_dir, "SLOT_PREFLIGHT_FAILED", must_exist=True)
    if not inputs.is_dir():
        raise ReleaseSlotFailure("SLOT_PREFLIGHT_FAILED")
    probe = (
        "import importlib.metadata as m,json,pathlib,sys;"
        "r=pathlib.Path(sys.argv[1]).resolve();"
        "p=pathlib.Path(m.distribution('ephi').locate_file('ephi/release_identity.py')).resolve();"
        "q=pathlib.Path(__import__('ephi').__file__).resolve();"
        "ok=pathlib.Path(sys.prefix).resolve()==r and p.is_relative_to(r) and q.is_relative_to(r);"
        "print(json.dumps({'installed':bool(ok)},separators=(',',':')))"
    )
    cwd = Path(tempfile.gettempdir()).resolve()
    env = _isolated_environment()
    try:
        check = subprocess.run(
            [str(python), "-I", "-c", probe, str(root)],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if check.returncode != 0 or json.loads(check.stdout).get("installed") is not True:
            raise ReleaseSlotFailure("SLOT_PREFLIGHT_FAILED")
        result = subprocess.run(
            [str(preflight), "--inputs-dir", str(inputs), "--json"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        report = json.loads(result.stdout)
    except ReleaseSlotFailure:
        raise
    except Exception as exc:
        raise ReleaseSlotFailure("SLOT_PREFLIGHT_FAILED") from exc
    if (
        result.returncode != 0
        or not isinstance(report, dict)
        or report.get("status") != "PASS"
        or report.get("reason_code") != "RELEASE_PREFLIGHT_PASS"
        or not isinstance(report.get("release_identity_sha256"), str)
        or not _HEX_64.fullmatch(report["release_identity_sha256"])
        or not isinstance(report.get("install_inputs_sha256"), str)
        or not _HEX_64.fullmatch(report["install_inputs_sha256"])
    ):
        raise ReleaseSlotFailure("SLOT_PREFLIGHT_FAILED")
    source = report.get("candidate_source")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("commit"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", source["commit"])
        or not isinstance(source.get("tree"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", source["tree"])
    ):
        raise ReleaseSlotFailure("SLOT_PREFLIGHT_FAILED")
    return {
        "release_identity_sha256": report["release_identity_sha256"],
        "install_inputs_sha256": report["install_inputs_sha256"],
        "candidate_commit": source["commit"],
        "candidate_tree": source["tree"],
    }


def _slot_record(identity: dict[str, str], metadata: dict[str, str | None]) -> dict[str, object]:
    return {
        "release_identity_sha256": identity["release_identity_sha256"],
        "install_inputs_sha256": identity["install_inputs_sha256"],
        "verification_state": "PASS",
        "verification_reason_code": "SLOT_PREFLIGHT_PASS",
        "metadata": metadata,
    }


def _state_body(
    generation: int,
    current: str | None,
    previous: str | None,
    slots: dict[str, object],
    transition_type: str,
    transition_identity: str,
) -> dict[str, object]:
    return {
        "schema": STATE_SCHEMA,
        "generation": generation,
        "current_slot_id": current,
        "previous_slot_id": previous,
        "slots": dict(sorted(slots.items())),
        "last_transition_type": transition_type,
        "last_transition_identity": transition_identity,
    }


def _finalize_state(body: dict[str, object]) -> dict[str, object]:
    return {**body, "state_sha256": _digest(body)}


def _transition_identity(
    generation: int,
    transition_type: str,
    current: str | None,
    previous: str | None,
    slot_id: str | None,
    release_identity: str | None,
) -> str:
    return _digest(
        {
            "generation": generation,
            "transition_type": transition_type,
            "current_slot_id": current,
            "previous_slot_id": previous,
            "slot_id": slot_id,
            "release_identity_sha256": release_identity,
        }
    )


def _validate_state(value: object, raw: bytes) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
    try:
        canonical = canonical_json_bytes(value) + b"\n"
    except Exception as exc:
        raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED") from exc
    if raw != canonical:
        raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
    expected = {
        "schema",
        "generation",
        "current_slot_id",
        "previous_slot_id",
        "slots",
        "last_transition_type",
        "last_transition_identity",
        "state_sha256",
    }
    if set(value) != expected:
        raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
    if value["schema"] != STATE_SCHEMA:
        raise ReleaseSlotFailure("STATE_SCHEMA_UNSUPPORTED")
    body = {key: item for key, item in value.items() if key != "state_sha256"}
    try:
        actual_digest = _digest(body)
    except Exception as exc:
        raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED") from exc
    if not isinstance(value["state_sha256"], str) or actual_digest != value["state_sha256"]:
        raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
    generation = value["generation"]
    slots = value["slots"]
    current = value["current_slot_id"]
    previous = value["previous_slot_id"]
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or generation > (2**63 - 1)
        or not isinstance(slots, dict)
        or not 1 <= len(slots) <= 128
        or value["last_transition_type"] not in {"INITIALIZE", "REGISTER", "VERIFY", "SELECT", "ROLLBACK"}
        or not isinstance(value["last_transition_identity"], str)
        or not _HEX_64.fullmatch(value["last_transition_identity"])
    ):
        raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
    if not isinstance(current, str) or current not in slots:
        raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
    if previous is not None and (not isinstance(previous, str) or previous not in slots or previous == current):
        raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
    for key, record in slots.items():
        if not isinstance(key, str) or not _SLOT_ID.fullmatch(key) or not isinstance(record, dict):
            raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
        if set(record) != {
            "release_identity_sha256",
            "install_inputs_sha256",
            "verification_state",
            "verification_reason_code",
            "metadata",
        }:
            raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
        for digest_name in ("release_identity_sha256", "install_inputs_sha256"):
            digest = record[digest_name]
            if digest is not None and (not isinstance(digest, str) or not _HEX_64.fullmatch(digest)):
                raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
        if (
            record["verification_state"] != "PASS"
            or record["verification_reason_code"] != "SLOT_PREFLIGHT_PASS"
            or not isinstance(record["metadata"], dict)
            or set(record["metadata"]) != {"label", "change_id"}
        ):
            raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
        _metadata(record["metadata"]["label"], record["metadata"]["change_id"])
    return value


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ReleaseSlotFailure("STATE_MISSING")
    if path.is_symlink() or not path.is_file():
        raise ReleaseSlotFailure("STATE_PATH_INVALID")
    try:
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
        value = json.loads(raw)
    except ReleaseSlotFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED") from exc
    return _validate_state(value, raw)


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise ReleaseSlotFailure("STATE_PATH_INVALID")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        if os.name == "nt":
            if msvcrt is None:
                raise OSError
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            if fcntl is None:
                raise OSError
            fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as exc:
        raise ReleaseSlotFailure("STATE_WRITE_FAILED") from exc
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _atomic_write(
    path: Path,
    state: dict[str, object],
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    raw = canonical_json_bytes(state) + b"\n"
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:  # pragma: no cover - Windows runtime
            os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except ReleaseSlotFailure:
        raise
    except Exception as exc:
        raise ReleaseSlotFailure("STATE_WRITE_FAILED") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _transition(
    path_value: str | os.PathLike[str],
    expected_generation: int,
    transition_type: str,
    *,
    slot_id: str | None = None,
    slot_root: str | os.PathLike[str] | None = None,
    inputs_dir: str | os.PathLike[str] | None = None,
    metadata: dict[str, str | None] | None = None,
    before_replace: Callable[[], None] | None = None,
) -> dict[str, Any]:
    path = _state_path(path_value, allow_missing=False)
    if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 0:
        raise ReleaseSlotFailure("INVALID_ARGUMENTS")
    with _state_lock(path):
        state = _read_state(path)
        if state["generation"] != expected_generation:
            raise ReleaseSlotFailure("EXPECTED_GENERATION_MISMATCH")
        if expected_generation >= 2**63 - 1:
            raise ReleaseSlotFailure("INVALID_ARGUMENTS")
        slots = dict(state["slots"])
        current = state["current_slot_id"]
        previous = state["previous_slot_id"]
        selected_id = _slot_id(slot_id) if slot_id is not None else None
        if transition_type in {"REGISTER", "VERIFY", "SELECT", "ROLLBACK"}:
            if slot_root is None or inputs_dir is None:
                raise ReleaseSlotFailure("INVALID_ARGUMENTS")
            if transition_type == "ROLLBACK":
                if previous is None:
                    raise ReleaseSlotFailure("NO_PREVIOUS_SLOT")
                selected_id = previous
            elif selected_id is None:
                raise ReleaseSlotFailure("INVALID_ARGUMENTS")
            if transition_type == "REGISTER":
                if selected_id in slots:
                    raise ReleaseSlotFailure("SLOT_ALREADY_REGISTERED")
                if len(slots) >= 128:
                    raise ReleaseSlotFailure("INVALID_ARGUMENTS")
            elif selected_id not in slots:
                raise ReleaseSlotFailure("SLOT_NOT_REGISTERED")
            if transition_type == "SELECT" and selected_id == current:
                raise ReleaseSlotFailure("SLOT_ALREADY_CURRENT")
            if transition_type == "ROLLBACK":
                assert selected_id is not None
            record = slots.get(selected_id)
            current_record = slots.get(current) if current is not None else None
            if transition_type in {"SELECT", "ROLLBACK"} and current_record is not None:
                if record["release_identity_sha256"] != current_record["release_identity_sha256"]:
                    raise ReleaseSlotFailure("CROSS_RELEASE_COMPATIBILITY_NOT_QUALIFIED")
            identity = _run_preflight(slot_root, inputs_dir)
            if record is not None and (
                record["release_identity_sha256"] != identity["release_identity_sha256"]
                or record["install_inputs_sha256"] not in (None, identity["install_inputs_sha256"])
            ):
                raise ReleaseSlotFailure("SLOT_IDENTITY_MISMATCH")
            if transition_type == "REGISTER":
                slots[selected_id] = _slot_record(
                    identity,
                    metadata if metadata is not None else {"label": None, "change_id": None},
                )
            elif transition_type == "VERIFY":
                slots[selected_id] = _slot_record(identity, record["metadata"])
            if transition_type in {"SELECT", "ROLLBACK"}:
                previous, current = current, selected_id
            new_generation = expected_generation + 1
            release_identity = identity["release_identity_sha256"]
        else:
            raise ReleaseSlotFailure("INVALID_ARGUMENTS")
        transition_id = _transition_identity(
            new_generation,
            transition_type,
            current,
            previous,
            selected_id,
            release_identity,
        )
        body = _state_body(new_generation, current, previous, slots, transition_type, transition_id)
        updated = _finalize_state(body)
        _atomic_write(path, updated, before_replace=before_replace)
        return updated


def initialize(
    state_file: str | os.PathLike[str],
    slot_id: str,
    slot_root: str | os.PathLike[str],
    inputs_dir: str | os.PathLike[str],
    *,
    label: str | None = None,
    change_id: str | None = None,
) -> dict[str, Any]:
    path = _state_path(state_file, allow_missing=True)
    selected_id = _slot_id(slot_id)
    selected_metadata = _metadata(label, change_id)
    with _state_lock(path):
        if path.exists() or path.is_symlink():
            raise ReleaseSlotFailure("STATE_MALFORMED_OR_TAMPERED")
        identity = _run_preflight(slot_root, inputs_dir)
        slots = {selected_id: _slot_record(identity, selected_metadata)}
        transition_id = _transition_identity(0, "INITIALIZE", selected_id, None, selected_id, identity["release_identity_sha256"])
        state = _finalize_state(_state_body(0, selected_id, None, slots, "INITIALIZE", transition_id))
        _atomic_write(path, state)
        return state


def register(
    state_file: str | os.PathLike[str],
    expected_generation: int,
    slot_id: str,
    slot_root: str | os.PathLike[str],
    inputs_dir: str | os.PathLike[str],
    *,
    label: str | None = None,
    change_id: str | None = None,
    before_replace: Callable[[], None] | None = None,
) -> dict[str, Any]:
    return _transition(
        state_file,
        expected_generation,
        "REGISTER",
        slot_id=slot_id,
        slot_root=slot_root,
        inputs_dir=inputs_dir,
        metadata=_metadata(label, change_id),
        before_replace=before_replace,
    )


def verify(
    state_file: str | os.PathLike[str],
    expected_generation: int,
    slot_id: str,
    slot_root: str | os.PathLike[str],
    inputs_dir: str | os.PathLike[str],
    *,
    before_replace: Callable[[], None] | None = None,
) -> dict[str, Any]:
    return _transition(
        state_file,
        expected_generation,
        "VERIFY",
        slot_id=slot_id,
        slot_root=slot_root,
        inputs_dir=inputs_dir,
        before_replace=before_replace,
    )


def select(
    state_file: str | os.PathLike[str],
    expected_generation: int,
    slot_id: str,
    slot_root: str | os.PathLike[str],
    inputs_dir: str | os.PathLike[str],
    *,
    before_replace: Callable[[], None] | None = None,
) -> dict[str, Any]:
    return _transition(
        state_file,
        expected_generation,
        "SELECT",
        slot_id=slot_id,
        slot_root=slot_root,
        inputs_dir=inputs_dir,
        before_replace=before_replace,
    )


def rollback(
    state_file: str | os.PathLike[str],
    expected_generation: int,
    slot_root: str | os.PathLike[str],
    inputs_dir: str | os.PathLike[str],
    *,
    before_replace: Callable[[], None] | None = None,
) -> dict[str, Any]:
    return _transition(
        state_file,
        expected_generation,
        "ROLLBACK",
        slot_root=slot_root,
        inputs_dir=inputs_dir,
        before_replace=before_replace,
    )


def read_selection(state_file: str | os.PathLike[str]) -> dict[str, Any]:
    path = _state_path(state_file, allow_missing=False)
    with _state_lock(path):
        state = _read_state(path)
    return state


def _report(state: dict[str, Any], reason: str) -> dict[str, object]:
    return {
        "schema": REPORT_SCHEMA,
        "status": "PASS",
        "reason_code": reason,
        "generation": state["generation"],
        "current_slot_id": state["current_slot_id"],
        "previous_slot_id": state["previous_slot_id"],
        "slots": state["slots"],
        "last_transition_type": state["last_transition_type"],
        "last_transition_identity": state["last_transition_identity"],
        "state_sha256": state["state_sha256"],
    }


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ReleaseSlotFailure("INVALID_ARGUMENTS")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read_parser = commands.add_parser("read")
    read_parser.add_argument("--state-file", required=True)
    init_parser = commands.add_parser("init")
    init_parser.add_argument("--state-file", required=True)
    init_parser.add_argument("--slot-id", required=True)
    init_parser.add_argument("--slot-root", required=True)
    init_parser.add_argument("--inputs-dir", required=True)
    init_parser.add_argument("--label")
    init_parser.add_argument("--change-id")
    for name in ("register", "verify", "select"):
        command = commands.add_parser(name)
        command.add_argument("--state-file", required=True)
        command.add_argument("--expected-generation", type=int, required=True)
        command.add_argument("--slot-id", required=True)
        command.add_argument("--slot-root", required=True)
        command.add_argument("--inputs-dir", required=True)
        if name == "register":
            command.add_argument("--label")
            command.add_argument("--change-id")
    rollback_parser = commands.add_parser("rollback")
    rollback_parser.add_argument("--state-file", required=True)
    rollback_parser.add_argument("--expected-generation", type=int, required=True)
    rollback_parser.add_argument("--slot-root", required=True)
    rollback_parser.add_argument("--inputs-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
        if args.command == "read":
            print(json.dumps(_report(read_selection(args.state_file), "RELEASE_SLOT_READ"), sort_keys=True, separators=(",", ":")))
            return 0
        if args.command == "init":
            state = initialize(
                args.state_file,
                args.slot_id,
                args.slot_root,
                args.inputs_dir,
                label=args.label,
                change_id=args.change_id,
            )
            reason = "RELEASE_SLOT_INITIALIZED"
        elif args.command == "register":
            state = register(
                args.state_file,
                args.expected_generation,
                args.slot_id,
                args.slot_root,
                args.inputs_dir,
                label=args.label,
                change_id=args.change_id,
            )
            reason = "RELEASE_SLOT_REGISTERED"
        elif args.command == "verify":
            state = verify(args.state_file, args.expected_generation, args.slot_id, args.slot_root, args.inputs_dir)
            reason = "RELEASE_SLOT_VERIFIED"
        elif args.command == "select":
            state = select(args.state_file, args.expected_generation, args.slot_id, args.slot_root, args.inputs_dir)
            reason = "RELEASE_SLOT_SELECTED"
        else:
            state = rollback(args.state_file, args.expected_generation, args.slot_root, args.inputs_dir)
            reason = "RELEASE_SLOT_ROLLED_BACK"
        print(json.dumps(_report(state, reason), sort_keys=True, separators=(",", ":")))
        return 0
    except ReleaseSlotFailure as exc:
        report = {"schema": REPORT_SCHEMA, "status": "FAIL", "reason_code": exc.reason_code}
    except Exception:
        report = {"schema": REPORT_SCHEMA, "status": "FAIL", "reason_code": "STATE_WRITE_FAILED"}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
