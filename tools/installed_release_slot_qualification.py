#!/usr/bin/env python3
"""Qualify two exact-candidate installed slots against PostgreSQL 18."""

from __future__ import annotations

import atexit
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row

import ephi
from ephi.application.operations import canonical_sha256, verify_artifact_inventory
from ephi.o9_operations import _artifact_inventory, _table_inventory
from ephi.release_slots import (
    ReleaseSlotFailure,
    _finalize_state,
    _slot_location_identity,
    _state_body,
    _transition_identity,
    read_selection,
    select,
)
from ephi.infrastructure.postgresql import validate_required_schema


REPORT_SCHEMA = "org.ephi.installed-release-slot-rehearsal.v1"


class QualificationFailure(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise QualificationFailure("INVALID_ARGUMENTS")


def _drop_qualification_database(admin_dsn: str, database: str) -> None:
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    except Exception:
        # The report contains no connection details; PostgreSQL cleanup is
        # best-effort if the service itself has stopped.
        pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise QualificationFailure(reason)


def _read_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise QualificationFailure(reason) from exc
    if not isinstance(value, dict):
        raise QualificationFailure(reason)
    return value


def _clean_environment(*, dsn: str | None = None, path_prefix: Path | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    path_value = environment.get("PATH", "")
    if path_prefix is not None:
        path_value = str(path_prefix) + os.pathsep + path_value
    environment["PATH"] = path_value
    if dsn is not None:
        environment["EPHI_POSTGRES_DSN"] = dsn
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _entry(root: Path, name: str) -> Path:
    binary_dir = root / ("Scripts" if os.name == "nt" else "bin")
    executable = binary_dir / (name + (".exe" if os.name == "nt" else ""))
    if not executable.is_file() or (name != "python" and executable.is_symlink()):
        raise QualificationFailure("INSTALLED_ENTRYPOINT_MISSING")
    return executable


def _invoke(
    executable: Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    expected_reason: str | None = None,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(executable), *arguments],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        report = json.loads(result.stdout)
    except Exception as exc:
        raise QualificationFailure("INSTALLED_COMMAND_OUTPUT_INVALID") from exc
    if not isinstance(report, dict) or report.get("schema") != "org.ephi.release-slot-report.v1":
        raise QualificationFailure("INSTALLED_COMMAND_OUTPUT_INVALID")
    if expected_reason is not None:
        if result.returncode == 0 or report.get("status") != "FAIL" or report.get("reason_code") != expected_reason:
            raise QualificationFailure("NEGATIVE_CONTROL_FAILED")
    elif result.returncode != 0 or report.get("status") != "PASS":
        raise QualificationFailure("INSTALLED_SLOT_COMMAND_FAILED")
    return report


def _environment_identity(root: Path, repository: Path, cwd: Path) -> dict[str, object]:
    python = _entry(root, "python")
    probe = (
        "import importlib.metadata as m,json,pathlib,sys;"
        "r=pathlib.Path(sys.argv[1]).resolve();"
        "c=pathlib.Path(sys.argv[2]).resolve();"
        "p=pathlib.Path(m.distribution('ephi').locate_file('ephi/release_identity.py')).resolve();"
        "q=pathlib.Path(__import__('ephi').__file__).resolve();"
        "s=[pathlib.Path(x).resolve() for x in sys.path if x];"
        "print(json.dumps({'prefix':pathlib.Path(sys.prefix).resolve()==r,"
        "'distribution':p.is_relative_to(r),'module':q.is_relative_to(r),"
        "'checkout_source':any(x==c or x.is_relative_to(c) for x in s)},separators=(',',':')))"
    )
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", probe, str(root.resolve()), str(repository.resolve() / "src")],
            cwd=cwd,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        report = json.loads(result.stdout)
    except Exception as exc:
        raise QualificationFailure("INSTALLED_IMPORT_IDENTITY_FAILED") from exc
    if result.returncode != 0 or not isinstance(report, dict):
        raise QualificationFailure("INSTALLED_IMPORT_IDENTITY_FAILED")
    if not (
        report.get("prefix") is True
        and report.get("distribution") is True
        and report.get("module") is True
        and report.get("checkout_source") is False
    ):
        raise QualificationFailure("INSTALLED_IMPORT_IDENTITY_FAILED")
    location_id = _slot_location_identity(root)
    return {
        "slot_location_id": location_id,
        "imports_from_installed_distribution": True,
        "checkout_source_on_import_path": False,
    }


def _preflight(root: Path, inputs: Path, cwd: Path) -> dict[str, Any]:
    command = _entry(root, "ephi-release-preflight")
    try:
        result = subprocess.run(
            [str(command), "--inputs-dir", str(inputs), "--json"],
            cwd=cwd,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        report = json.loads(result.stdout)
    except Exception as exc:
        raise QualificationFailure("INSTALLED_RELEASE_PREFLIGHT_FAILED") from exc
    if (
        result.returncode != 0
        or not isinstance(report, dict)
        or report.get("schema") != "org.ephi.release-preflight.v1"
        or report.get("status") != "PASS"
        or report.get("reason_code") != "RELEASE_PREFLIGHT_PASS"
    ):
        raise QualificationFailure("INSTALLED_RELEASE_PREFLIGHT_FAILED")
    if report.get("provider_composition", {}).get("status") != "NOT_RUN":
        raise QualificationFailure("PROVIDER_COMPOSITION_WAS_SELECTED")
    return report


def _slot_command(root: Path) -> Path:
    return _entry(root, "ephi-release-slot")


def _slot_args(state_file: Path) -> list[str]:
    return ["--state-file", str(state_file)]


def _fingerprints(dsn: str, artifact_root: Path) -> dict[str, object]:
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            major = connection.info.server_version // 10000
            _require(major == 18, "POSTGRESQL_18_REQUIRED")
            validate_required_schema(connection)
            tables = _table_inventory(connection)
            artifacts = _artifact_inventory(connection)
    except QualificationFailure:
        raise
    except Exception as exc:
        raise QualificationFailure("DURABLE_FINGERPRINT_FAILED") from exc
    failures = verify_artifact_inventory(
        artifact_root,
        [{"sha256": item["sha256"], "byte_size": item["byte_size"]} for item in artifacts],
    )
    _require(not failures, "IMMUTABLE_ARTIFACT_VERIFICATION_FAILED")
    table_fingerprints = {
        name: {
            "row_count": int(item["row_count"]),
            "content_sha256": str(item["content_sha256"]),
        }
        for name, item in tables.items()
        if name != "_state"
    }
    return {
        "postgresql_major": major,
        "schema_state": "CURRENT",
        "critical_durable_state_sha256": tables["_state"]["content_sha256"],
        "critical_tables": table_fingerprints,
        "immutable_artifact_count": len(artifacts),
        "immutable_artifact_sha256": canonical_sha256(artifacts),
        "immutable_artifact_bytes_verified": True,
    }


def _status(root: Path, cwd: Path, dsn: str, artifact_root: Path, path_prefix: Path) -> dict[str, Any]:
    command = _entry(root, "ephi-operations")
    environment = _clean_environment(dsn=dsn, path_prefix=path_prefix)
    result = subprocess.run(
        [str(command), "status", "--json", "--artifact-root", str(artifact_root)],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    try:
        report = json.loads(result.stdout)
    except Exception as exc:
        raise QualificationFailure("O9_STATUS_OUTPUT_INVALID") from exc
    if result.returncode != 0 or not isinstance(report, dict):
        raise QualificationFailure("O9_STATUS_UNAVAILABLE")
    axes = report.get("axes")
    if not isinstance(axes, dict):
        raise QualificationFailure("O9_STATUS_CONTRACT_INVALID")
    _require(axes["process_transport"]["state"] == "READY", "O9_STATUS_UNAVAILABLE")
    _require(axes["postgres_readiness_durability"]["state"] == "READY", "O9_STATUS_POSTGRES_NOT_READY")
    _require(axes["immutable_artifact_integrity"]["state"] == "READY", "O9_STATUS_ARTIFACTS_NOT_READY")
    return {
        "schema_version": report["schema_version"],
        "process_transport": axes["process_transport"]["state"],
        "postgres_readiness_durability": axes["postgres_readiness_durability"]["state"],
        "immutable_artifact_integrity": axes["immutable_artifact_integrity"]["state"],
        "durable_worker_job_state": axes["durable_worker_job_state"]["state"],
        "source_capability_freshness": axes["source_capability_freshness"]["state"],
        "evidence_qualification_freshness": axes["evidence_qualification_freshness"]["state"],
        "read_only": True,
        "provider_auto_selected": False,
    }


def _expected_failure(
    command: Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    reason: str,
) -> dict[str, object]:
    _invoke(command, arguments, cwd=cwd, environment=environment, expected_reason=reason)
    return {"status": "PASS", "reason_code": reason}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> dict[str, object]:
    repository = Path(args.repository_root).resolve()
    slot_a = Path(args.slot_a_root).resolve(strict=True)
    slot_b = Path(args.slot_b_root).resolve(strict=True)
    inputs = Path(args.inputs_dir).resolve(strict=True)
    work = Path(args.work_dir).resolve()
    artifact_dir = Path(args.artifact_dir).resolve()
    cwd = work / "external-cwd"
    work.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(exist_ok=True)
    _require(slot_a != slot_b, "SLOT_LOCATIONS_NOT_DISTINCT")
    _require(slot_a.is_relative_to(repository) is False and slot_b.is_relative_to(repository) is False, "SLOT_INSIDE_SOURCE_CHECKOUT")
    _require(str(repository / "src") not in sys.path, "SOURCE_IMPORT_PATH_PRESENT")
    _require(re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha) is not None, "CANDIDATE_IDENTITY_INVALID")
    _require(re.fullmatch(r"[0-9a-f]{40}", args.candidate_tree) is not None, "CANDIDATE_IDENTITY_INVALID")
    install_inputs = _read_json(inputs / "install_inputs.json", "INSTALL_INPUT_IDENTITY_INVALID")
    source_facts = install_inputs.get("source")
    _require(
        isinstance(source_facts, dict)
        and source_facts.get("repository") == "kimhw8084/ephi"
        and source_facts.get("commit") == args.candidate_sha
        and source_facts.get("tree") == args.candidate_tree,
        "CANDIDATE_INPUT_MISMATCH",
    )
    release_identity = install_inputs.get("release_identity_sha256")
    install_inputs_identity = install_inputs.get("install_inputs_sha256")
    _require(isinstance(release_identity, str) and re.fullmatch(r"[0-9a-f]{64}", release_identity), "INSTALL_INPUT_IDENTITY_INVALID")
    _require(isinstance(install_inputs_identity, str) and re.fullmatch(r"[0-9a-f]{64}", install_inputs_identity), "INSTALL_INPUT_IDENTITY_INVALID")

    slot_identity = {
        "slot-a": _environment_identity(slot_a, repository, cwd),
        "slot-b": _environment_identity(slot_b, repository, cwd),
    }
    _require(slot_identity["slot-a"]["slot_location_id"] != slot_identity["slot-b"]["slot_location_id"], "SLOT_LOCATIONS_NOT_DISTINCT")
    preflight_a = _preflight(slot_a, inputs, cwd)
    preflight_b = _preflight(slot_b, inputs, cwd)
    for preflight in (preflight_a, preflight_b):
        _require(preflight["candidate_source"] == {"commit": args.candidate_sha, "tree": args.candidate_tree}, "CANDIDATE_PREFLIGHT_MISMATCH")
        _require(preflight["release_identity_sha256"] == release_identity, "RELEASE_IDENTITY_MISMATCH")
        _require(preflight["install_inputs_sha256"] == install_inputs_identity, "INSTALL_INPUT_IDENTITY_MISMATCH")

    admin_dsn = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()
    _require(bool(admin_dsn), "POSTGRESQL_18_TEST_DSN_NOT_CONFIGURED")
    from installed_o9_recovery_qualification import _create_isolated_database, _seed_fixture

    database = "ephi_o9_slot_" + hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    try:
        database_dsn = _create_isolated_database(admin_dsn, database)
    except Exception as exc:
        if isinstance(exc, QualificationFailure):
            raise
        raise QualificationFailure("POSTGRESQL_18_FIXTURE_DATABASE_UNAVAILABLE") from exc
    atexit.register(_drop_qualification_database, admin_dsn, database)
    artifact_root = work / "synthetic-artifacts"
    artifact_root.mkdir(exist_ok=True)
    try:
        _seed_fixture(database_dsn, artifact_root)
    except Exception as exc:
        raise QualificationFailure("DURABLE_FIXTURE_SEED_FAILED") from exc
    fingerprints_before = _fingerprints(database_dsn, artifact_root)

    state_file = work / "selection" / "release-selection.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    cli_a, cli_b = _slot_command(slot_a), _slot_command(slot_b)
    migration_sentinel = work / "migration-sentinel"
    migration_sentinel.mkdir(exist_ok=True)
    migration_marker = migration_sentinel / "durable-command-invoked"
    for tool_name in ("ephi-db-migrate", "pg_restore", "pg_dump"):
        tool = migration_sentinel / (tool_name + (".exe" if os.name == "nt" else ""))
        tool.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"Path({str(migration_marker)!r}).write_text('invoked', encoding='utf-8')\n",
            encoding="utf-8",
        )
        tool.chmod(0o700)
    env = _clean_environment(path_prefix=migration_sentinel)
    init = _invoke(
        cli_a,
        [
            "init",
            *_slot_args(state_file),
            "--slot-id", "slot-a",
            "--slot-root", str(slot_a),
            "--inputs-dir", str(inputs),
            "--label", "primary",
            "--change-id", "CHG-295",
        ],
        cwd=cwd,
        environment=env,
    )
    _require(init.get("reason_code") == "RELEASE_SLOT_INITIALIZED" and init.get("generation") == 0, "SLOT_INITIALIZATION_FAILED")
    _require(
        init["slots"]["slot-a"]["slot_location_id"] == slot_identity["slot-a"]["slot_location_id"],
        "SLOT_INITIAL_LOCATION_BINDING_FAILED",
    )
    registered = _invoke(
        cli_a,
        [
            "register",
            *_slot_args(state_file),
            "--expected-generation", "0",
            "--slot-id", "slot-b",
            "--slot-root", str(slot_b),
            "--inputs-dir", str(inputs),
            "--label", "standby",
            "--change-id", "CHG-295",
        ],
        cwd=cwd,
        environment=env,
    )
    _require(registered.get("generation") == 1 and registered.get("current_slot_id") == "slot-a", "SLOT_REGISTRATION_FAILED")
    _require(
        registered["slots"]["slot-b"]["slot_location_id"] == slot_identity["slot-b"]["slot_location_id"]
        and registered["slots"]["slot-a"]["slot_location_id"] != registered["slots"]["slot-b"]["slot_location_id"],
        "SLOT_REGISTRATION_LOCATION_BINDING_FAILED",
    )

    negatives: dict[str, object] = {}
    state_before_negative_controls = state_file.read_bytes()
    negatives["stale_generation"] = _expected_failure(
        cli_a,
        ["select", *_slot_args(state_file), "--expected-generation", "0", "--slot-id", "slot-b", "--slot-root", str(slot_b), "--inputs-dir", str(inputs)],
        cwd=cwd,
        environment=env,
        reason="EXPECTED_GENERATION_MISMATCH",
    )
    _require(state_file.read_bytes() == state_before_negative_controls, "NEGATIVE_CONTROL_MUTATED_STATE")

    malformed = work / "negative" / "malformed.json"
    malformed.parent.mkdir(exist_ok=True)
    malformed.write_bytes(b"{")
    negatives["malformed_state"] = _expected_failure(
        cli_a, ["read", *_slot_args(malformed)], cwd=cwd, environment=env, reason="STATE_MALFORMED_OR_TAMPERED"
    )
    tampered = work / "negative" / "tampered.json"
    tampered.write_bytes(state_before_negative_controls.replace(b'"generation":1', b'"generation":2'))
    negatives["tampered_state"] = _expected_failure(
        cli_a, ["read", *_slot_args(tampered)], cwd=cwd, environment=env, reason="STATE_MALFORMED_OR_TAMPERED"
    )

    negatives["missing_slot"] = _expected_failure(
        cli_a,
        ["verify", *_slot_args(state_file), "--expected-generation", "1", "--slot-id", "slot-b", "--slot-root", str(work / "missing-slot"), "--inputs-dir", str(inputs)],
        cwd=cwd,
        environment=env,
        reason="SLOT_ROOT_INVALID",
    )
    _require(state_file.read_bytes() == state_before_negative_controls, "NEGATIVE_CONTROL_MUTATED_STATE")

    path_traversal = str(slot_b / ".." / slot_a.name)
    negatives["path_traversal"] = _expected_failure(
        cli_a,
        ["verify", *_slot_args(state_file), "--expected-generation", "1", "--slot-id", "slot-b", "--slot-root", path_traversal, "--inputs-dir", str(inputs)],
        cwd=cwd,
        environment=env,
        reason="SLOT_ROOT_INVALID",
    )
    escaped = work / "negative" / "slot-escape"
    escaped.parent.mkdir(exist_ok=True)
    escaped.symlink_to(slot_b, target_is_directory=True)
    negatives["symlink_escape"] = _expected_failure(
        cli_a,
        ["verify", *_slot_args(state_file), "--expected-generation", "1", "--slot-id", "slot-b", "--slot-root", str(escaped), "--inputs-dir", str(inputs)],
        cwd=cwd,
        environment=env,
        reason="SLOT_ROOT_INVALID",
    )

    wrong_inputs = work / "negative" / "missing-inputs"
    negatives["slot_preflight_failure"] = _expected_failure(
        cli_a,
        ["verify", *_slot_args(state_file), "--expected-generation", "1", "--slot-id", "slot-b", "--slot-root", str(slot_b), "--inputs-dir", str(wrong_inputs)],
        cwd=cwd,
        environment=env,
        reason="SLOT_PREFLIGHT_FAILED",
    )
    _require(state_file.read_bytes() == state_before_negative_controls, "NEGATIVE_CONTROL_MUTATED_STATE")

    negatives["select_swapped_slot_root"] = _expected_failure(
        cli_a,
        ["select", *_slot_args(state_file), "--expected-generation", "1", "--slot-id", "slot-b", "--slot-root", str(slot_a), "--inputs-dir", str(inputs)],
        cwd=cwd,
        environment=env,
        reason="SLOT_LOCATION_MISMATCH",
    )
    _require(state_file.read_bytes() == state_before_negative_controls, "SWAPPED_SELECT_ROOT_MUTATED_STATE")

    # Resolve the candidate slot's installed package without exposing its path.
    probe = subprocess.run(
        [str(_entry(slot_b, "python")), "-I", "-c", "import pathlib,ephi;print(pathlib.Path(ephi.__file__).resolve().parent)"],
        cwd=cwd,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    _require(probe.returncode == 0, "INSTALLED_IMPORT_IDENTITY_FAILED")
    release_module_root = Path(probe.stdout.strip())
    inventory_file = release_module_root / "release_inventory.json"
    original_inventory = inventory_file.read_bytes()
    try:
        inventory_file.write_bytes(original_inventory + b" ")
        negatives["tampered_slot"] = _expected_failure(
            cli_a,
            ["verify", *_slot_args(state_file), "--expected-generation", "1", "--slot-id", "slot-b", "--slot-root", str(slot_b), "--inputs-dir", str(inputs)],
            cwd=cwd,
            environment=env,
            reason="SLOT_PREFLIGHT_FAILED",
        )
    finally:
        inventory_file.write_bytes(original_inventory)
    _require(state_file.read_bytes() == state_before_negative_controls, "NEGATIVE_CONTROL_MUTATED_STATE")

    fake_cross_release = work / "negative" / "cross-release.json"
    valid_state = read_selection(state_file)
    forged_slots = dict(valid_state["slots"])
    forged_slots["slot-b"] = dict(forged_slots["slot-b"], release_identity_sha256="f" * 64)
    forged_transition_identity = _transition_identity(
        valid_state["generation"],
        valid_state["last_transition_type"],
        valid_state["current_slot_id"],
        valid_state["previous_slot_id"],
        valid_state["last_transition_slot_id"],
        forged_slots[valid_state["last_transition_slot_id"]]["release_identity_sha256"],
        valid_state["last_transition_slot_location_id"],
    )
    body = _state_body(
        valid_state["generation"],
        valid_state["current_slot_id"],
        valid_state["previous_slot_id"],
        forged_slots,
        valid_state["last_transition_type"],
        forged_transition_identity,
        valid_state["last_transition_slot_id"],
        valid_state["last_transition_slot_location_id"],
    )
    fake_cross_release.write_bytes(json.dumps(_finalize_state(body), sort_keys=True, separators=(",", ":")).encode() + b"\n")
    cross_before = fake_cross_release.read_bytes()
    negatives["different_release_identity"] = _expected_failure(
        cli_a,
        ["select", *_slot_args(fake_cross_release), "--expected-generation", "1", "--slot-id", "slot-b", "--slot-root", str(slot_b), "--inputs-dir", str(inputs)],
        cwd=cwd,
        environment=env,
        reason="CROSS_RELEASE_COMPATIBILITY_NOT_QUALIFIED",
    )
    _require(fake_cross_release.read_bytes() == cross_before, "NEGATIVE_CONTROL_MUTATED_STATE")
    negatives["different_release_identity"]["qualification_state"] = "NOT_QUALIFIED"

    before_replace = state_file.read_bytes()
    try:
        select(
            state_file,
            1,
            "slot-b",
            slot_b,
            inputs,
            before_replace=lambda: (_ for _ in ()).throw(ReleaseSlotFailure("ATOMIC_REPLACEMENT_INJECTED")),
        )
        raise QualificationFailure("ATOMICITY_FAULT_NOT_INJECTED")
    except ReleaseSlotFailure as exc:
        _require(exc.reason_code == "ATOMIC_REPLACEMENT_INJECTED", "ATOMICITY_FAULT_NOT_INJECTED")
    _require(state_file.read_bytes() == before_replace, "ATOMICITY_OLD_STATE_CHANGED")
    old_state = read_selection(state_file)
    _require(old_state["generation"] == 1 and old_state["current_slot_id"] == "slot-a", "ATOMICITY_OLD_STATE_UNREADABLE")
    atomicity = {
        "fault_injected_immediately_before_replace": True,
        "prior_state_byte_identical_and_readable": True,
        "state_generation_after_fault": old_state["generation"],
    }

    fingerprints_before = _fingerprints(database_dsn, artifact_root)
    select_report = _invoke(
        cli_a,
        ["select", *_slot_args(state_file), "--expected-generation", "1", "--slot-id", "slot-b", "--slot-root", str(slot_b), "--inputs-dir", str(inputs)],
        cwd=cwd,
        environment=env,
    )
    _require(select_report.get("generation") == 2 and select_report.get("current_slot_id") == "slot-b" and select_report.get("previous_slot_id") == "slot-a", "SLOT_SELECTION_TRANSITION_FAILED")
    _require(
        select_report.get("last_transition_slot_id") == "slot-b"
        and select_report.get("last_transition_slot_location_id") == slot_identity["slot-b"]["slot_location_id"],
        "SLOT_SELECTION_LOCATION_BINDING_FAILED",
    )
    selected_state = _invoke(cli_b, ["read", *_slot_args(state_file)], cwd=cwd, environment=env)
    _require(selected_state.get("state_sha256") == select_report.get("state_sha256"), "ATOMIC_REPLACEMENT_STATE_INVALID")
    observed_selected = read_selection(state_file)
    _require(observed_selected["state_sha256"] == select_report.get("state_sha256"), "ATOMIC_REPLACEMENT_STATE_INVALID")
    _require(state_file.read_bytes() == json.dumps(observed_selected, sort_keys=True, separators=(",", ":")).encode() + b"\n", "ATOMIC_REPLACEMENT_STATE_INVALID")
    _require([path.name for path in state_file.parent.glob("*.json")] == [state_file.name], "ATOMIC_REPLACEMENT_STATE_COUNT_INVALID")
    _require(state_file.read_bytes().endswith(b"\n"), "ATOMIC_REPLACEMENT_STATE_INVALID")
    _require(len(list(state_file.parent.glob(state_file.name + ".*.tmp"))) == 0, "ATOMIC_REPLACEMENT_TEMP_REMAINS")
    fingerprints_selected = _fingerprints(database_dsn, artifact_root)
    _require(fingerprints_selected == fingerprints_before, "DURABLE_STATE_CHANGED_ON_SELECT")
    status = _status(slot_b, cwd, database_dsn, artifact_root, migration_sentinel)

    selected_before_wrong_rollback = state_file.read_bytes()
    negatives["rollback_swapped_slot_root"] = _expected_failure(
        cli_b,
        ["rollback", *_slot_args(state_file), "--expected-generation", "2", "--slot-root", str(slot_b), "--inputs-dir", str(inputs)],
        cwd=cwd,
        environment=env,
        reason="SLOT_LOCATION_MISMATCH",
    )
    _require(state_file.read_bytes() == selected_before_wrong_rollback, "SWAPPED_ROLLBACK_ROOT_MUTATED_STATE")

    rollback_report = _invoke(
        cli_b,
        ["rollback", *_slot_args(state_file), "--expected-generation", "2", "--slot-root", str(slot_a), "--inputs-dir", str(inputs)],
        cwd=cwd,
        environment=env,
    )
    _require(rollback_report.get("generation") == 3 and rollback_report.get("current_slot_id") == "slot-a" and rollback_report.get("previous_slot_id") == "slot-b", "SLOT_ROLLBACK_TRANSITION_FAILED")
    _require(
        rollback_report.get("last_transition_slot_id") == "slot-a"
        and rollback_report.get("last_transition_slot_location_id") == slot_identity["slot-a"]["slot_location_id"],
        "SLOT_ROLLBACK_LOCATION_BINDING_FAILED",
    )
    rolled_back_state = _invoke(cli_a, ["read", *_slot_args(state_file)], cwd=cwd, environment=env)
    _require(rolled_back_state.get("state_sha256") == rollback_report.get("state_sha256"), "ATOMIC_REPLACEMENT_STATE_INVALID")
    observed_rollback = read_selection(state_file)
    _require(observed_rollback["state_sha256"] == rollback_report.get("state_sha256"), "ATOMIC_REPLACEMENT_STATE_INVALID")
    _require(state_file.read_bytes() == json.dumps(observed_rollback, sort_keys=True, separators=(",", ":")).encode() + b"\n", "ATOMIC_REPLACEMENT_STATE_INVALID")
    _require([path.name for path in state_file.parent.glob("*.json")] == [state_file.name], "ATOMIC_REPLACEMENT_STATE_COUNT_INVALID")
    _require(len(list(state_file.parent.glob(state_file.name + ".*.tmp"))) == 0, "ATOMIC_REPLACEMENT_TEMP_REMAINS")
    fingerprints_rolled_back = _fingerprints(database_dsn, artifact_root)
    _require(fingerprints_rolled_back == fingerprints_before, "DURABLE_STATE_CHANGED_ON_ROLLBACK")
    status_after_rollback = _status(slot_a, cwd, database_dsn, artifact_root, migration_sentinel)
    _require(not migration_marker.exists(), "MIGRATION_OR_RESTORE_COMMAND_INVOKED_BY_SLOT_OPERATION")

    transitions = [
        {
            "type": state["last_transition_type"],
            "generation": state["generation"],
            "identity": state["last_transition_identity"],
            "current_slot_id": state["current_slot_id"],
            "previous_slot_id": state["previous_slot_id"],
            "slot_id": state["last_transition_slot_id"],
            "slot_location_id": state["last_transition_slot_location_id"],
        }
        for state in (init, registered, select_report, rollback_report)
    ]
    expected_transition_slots = ("slot-a", "slot-b", "slot-b", "slot-a")
    for transition, expected_slot_id in zip(transitions, expected_transition_slots, strict=True):
        _require(
            transition["slot_id"] == expected_slot_id
            and transition["slot_location_id"] == slot_identity[expected_slot_id]["slot_location_id"],
            "TRANSITION_LOCATION_BINDING_FAILED",
        )
    slots = {
        slot_id: {
            "release_identity_sha256": release_identity,
            "install_inputs_sha256": install_inputs_identity,
            "candidate_commit": args.candidate_sha,
            "candidate_tree": args.candidate_tree,
            **slot_identity[slot_id],
        }
        for slot_id in ("slot-a", "slot-b")
    }
    report = {
        "schema": REPORT_SCHEMA,
        "status": "PASS",
        "reason_code": "INSTALLED_SAME_RELEASE_SLOT_REHEARSAL_PASS",
        "candidate": {"commit": args.candidate_sha, "tree": args.candidate_tree},
        "release_identity_sha256": release_identity,
        "slot_identities": slots,
        "preflights": {
            "slot-a": {"status": "PASS", "provider_composition": "NOT_RUN"},
            "slot-b": {"status": "PASS", "provider_composition": "NOT_RUN"},
        },
        "transitions": transitions,
        "durable_fingerprints": {
            "before_selection": fingerprints_before,
            "after_selection": fingerprints_selected,
            "after_rollback": fingerprints_rolled_back,
            "all_equal": fingerprints_before == fingerprints_selected == fingerprints_rolled_back,
        },
        "operations_status_after_selection": status,
        "operations_status_after_rollback": status_after_rollback,
        "negative_controls": negatives,
        "atomicity": {
            **atomicity,
            "successful_replace_observed_complete_states": 2,
            "one_valid_state_observed_after_each_replace": True,
            "final_state_generation": rolled_back_state["generation"],
        },
        "selection_effects": {
            "schema_fixture_initialized_before_fingerprints": True,
            "migration_command_invocations_during_slot_operations": 0,
            "restore_invocations_during_slot_operations": 0,
            "traffic_switched": False,
            "provider_auto_selected": False,
            "product_durable_state_mutated": False,
        },
        "nonclaims": [
            "CROSS_RELEASE_UPGRADE_ROLLBACK_COMPATIBILITY_NOT_QUALIFIED",
            "N_MINUS_1_TO_N_COMPATIBILITY_NOT_RUN",
            "SCHEMA_DOWNGRADE_NOT_SUPPORTED",
            "SCIENTIFIC_MODEL_ROLLBACK_NOT_RUN",
            "REAL_TRAFFIC_CUTOVER_NOT_RUN",
            "G10_NOT_ESTABLISHED",
            "G11_NOT_ESTABLISHED",
            "G12_NOT_SUPPORTED",
            "PORT_GATE_NOT_SUPPORTED",
            "RELEASE_PROMOTION_NOT_SUPPORTED",
            "PRODUCTION_NOT_SUPPORTED",
        ],
    }
    _require(report["durable_fingerprints"]["all_equal"] is True, "DURABLE_STATE_CHANGED")
    _write_json(artifact_dir / "release-slot-negative-controls.json", negatives)
    _write_json(artifact_dir / "release-slot-state-summary.json", rolled_back_state)
    _write_json(artifact_dir / "release-slot-rehearsal.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = _SafeParser(description=__doc__)
    parser.add_argument("--slot-a-root", required=True)
    parser.add_argument("--slot-b-root", required=True)
    parser.add_argument("--inputs-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--candidate-tree", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    artifact_dir: Path | None = None
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
        artifact_dir = Path(args.artifact_dir)
        report = run(args)
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    except QualificationFailure as exc:
        report = {
            "schema": REPORT_SCHEMA,
            "status": "FAIL",
            "reason_code": exc.reason_code,
            "nonclaims": ["CROSS_RELEASE_UPGRADE_ROLLBACK_COMPATIBILITY_NOT_QUALIFIED"],
        }
    except Exception:
        report = {
            "schema": REPORT_SCHEMA,
            "status": "FAIL",
            "reason_code": "RELEASE_SLOT_QUALIFICATION_FAILED",
            "nonclaims": ["CROSS_RELEASE_UPGRADE_ROLLBACK_COMPATIBILITY_NOT_QUALIFIED"],
        }
    if artifact_dir is not None:
        try:
            _write_json(artifact_dir / "release-slot-rehearsal.json", report)
        except Exception:
            pass
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
