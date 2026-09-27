#!/usr/bin/env python3
"""Qualify installed-wheel O9 recovery against deterministic PostgreSQL data."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from uuid import uuid4

import ephi
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from ephi.application import (
    AccessScope,
    ArtifactService,
    AttentionQueryService,
    CommandContext,
    EpisodeBriefQueryService,
    EpisodeWorkflowCommandService,
    MetrologyObservation,
    MetrologySourceBinding,
    MutableCurrentAuthorizationAuthority,
    Principal,
    RevisionVector,
    SourceSnapshotDraft,
    SourceSnapshotIngressService,
    SourceSnapshotStatus,
    VersionedAggregateCommandExecutor,
    WorkerLease,
    WorkerLeaseConfig,
)
from ephi.application.operations import artifact_blob_path, canonical_sha256, safe_identity_hash, verify_artifact_inventory
from ephi.infrastructure import FileArtifactBlobStore, PostgreSQLReferenceTransactionAdapter, PostgreSQLSourceSnapshotStore
from ephi.infrastructure.artifacts import PostgreSQLArtifactCatalog
from ephi.infrastructure.postgresql import validate_required_schema
from ephi.infrastructure.postgresql_worker import PostgreSQLWorkerStore
from ephi.migration_resources import resolve_migration_resources
from ephi.release_identity import installed_release_identity


_SENTINELS = (
    "o9-private-source-identifier-sentinel",
    "o9-private-material-identifier-sentinel",
    "o9-private-raw-row-sentinel",
    "o9-private-exception-sentinel",
    "o9-private-path-sentinel",
    "o9-private-user-sentinel",
    "o9-private-password-sentinel",
    "o9-private-host-sentinel.invalid",
    "o9-private-database-sentinel",
    "o9-private-dsn-component-sentinel",
)


class QualificationFailure(RuntimeError):
    def __init__(self, stage: str):
        super().__init__(stage)
        self.stage = stage


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise QualificationFailure("INVALID_ARGUMENTS")


def _require(condition: bool, stage: str) -> None:
    if not condition:
        raise QualificationFailure(stage)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _principal(scope: AccessScope) -> Principal:
    return Principal(
        _SENTINELS[0],
        (
            "ephi.attention.read",
            "ephi.episode.read",
            "ephi.episode.claim",
            "ephi.episode.acknowledge",
            "ephi.source.read",
            "ephi.source.ingest",
            "ephi.source.artifact.read",
            "ephi.source.artifact.write",
            "o9.fixture.write",
        ),
        (scope,),
        1,
        1,
    )


def _workflow_context(scope: AccessScope, principal: Principal, command_id: str, expected: int) -> CommandContext:
    return CommandContext(
        command_id,
        principal,
        scope,
        expected,
        RevisionVector("o9-analysis-1", "o9-exposure-1", "o9-priority-1", expected, None, "o9-manifest-1"),
        "Installed O9 synthetic recovery fixture",
    )


def _create_isolated_database(admin_dsn: str, database: str) -> str:
    if not re.fullmatch(r"ephi_o9_[a-z0-9_]{8,40}", database):
        raise QualificationFailure("DATABASE_FIXTURE_ID_INVALID")
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            server_version = int(connection.execute("SELECT current_setting('server_version_num')").fetchone()[0])
            if server_version // 10000 != 18:
                raise QualificationFailure("POSTGRESQL_18_REQUIRED")
            connection.execute(f'CREATE DATABASE "{database}" TEMPLATE template0')
    except QualificationFailure:
        raise
    except Exception:
        raise QualificationFailure("POSTGRESQL_FIXTURE_DATABASE_UNAVAILABLE") from None
    return make_conninfo(admin_dsn, dbname=database, application_name=_SENTINELS[9])


def _seed_fixture(dsn: str, artifact_root: Path) -> dict[str, Any]:
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        adapter.apply_migrations()
        scope = AccessScope(
            "o9-private-scope-identifier-sentinel",
            site_id="synthetic-site",
            area_id="synthetic-area",
            family_id="synthetic-fixture-family",
        )
        principal = _principal(scope)
        authorization = MutableCurrentAuthorizationAuthority(principal)
        artifact_service = ArtifactService(
            FileArtifactBlobStore(artifact_root),
            PostgreSQLArtifactCatalog(adapter),
            authorization,
        )
        source_content = (
            "o9-private-material-identifier-sentinel\n"
            "synthetic immutable source manifest; no authentic material data\n"
        ).encode()
        source_artifact = artifact_service.write_and_register(
            principal,
            scope,
            source_content,
            media_type="application/octet-stream",
            logical_purpose="synthetic-o9-source-manifest",
            required_write_capability="ephi.source.artifact.write",
        )

        adapter.seed_aggregate(scope, "episode_workflow", "episode-private-sentinel", {"work_state": "OPEN", "owner": None}, version=0)
        adapter.seed_aggregate(scope, "fixture", "effect-state-sentinel", {"effect_count": 0, "seed": "synthetic"}, version=0)
        adapter.seed_aggregate(scope, "fixture", "postcutoff-state-sentinel", {"effect_count": 0, "seed": "synthetic"}, version=0)
        adapter.seed_attention_projection(
            scope,
            "episode-private-sentinel",
            {
                "title": _SENTINELS[1],
                "asset_id": "synthetic-asset",
                "priority": "P2",
                "severity": "HIGH",
                "technical_state": "READY",
                "source_state": "READY",
                "deadline": "2026-09-30T23:59:00Z",
                "age": "1",
            },
        )
        workflow = adapter.get_aggregate(scope, "episode_workflow", "episode-private-sentinel")
        adapter.publish_current_revision(
            scope,
            "episode",
            "episode-private-sentinel",
            "private-read-revision-sentinel",
            RevisionVector("o9-analysis-1", "o9-exposure-1", "o9-priority-1", 0, None, "o9-manifest-1"),
            {"title": "synthetic O9 episode", "analytical_revision": "o9-analysis-1", "capability_state": {"source": "READY"}},
            workflow,
        )
        retained = AttentionQueryService(adapter.o3_store(), adapter.read_store(), authorization).list_attention(
            principal, scope, page_size=1
        )
        workflow_service = EpisodeWorkflowCommandService(adapter, authorization)
        claim = workflow_service.claim_episode(_workflow_context(scope, principal, "private-claim-command-sentinel", 0), "episode-private-sentinel")
        acknowledged = workflow_service.acknowledge_episode(
            _workflow_context(scope, principal, "private-ack-command-sentinel", 1), "episode-private-sentinel"
        )

        worker = PostgreSQLWorkerStore(
            adapter,
            config=WorkerLeaseConfig(lease_duration=timedelta(seconds=1), heartbeat_interval=timedelta(milliseconds=100)),
        )
        effect_job = worker.enqueue(scope, "SyntheticO9LocalEffect", "private-effect-job-sentinel", {"value": "synthetic"})
        effect_lease = worker.claim(scope, "private-effect-worker-sentinel")
        _require(effect_lease is not None, "WORKER_EFFECT_FIXTURE_UNAVAILABLE")
        effect_receipt = worker.commit_local_effect(
            effect_lease.lease,
            "private-effect-key-sentinel",
            {"value": "synthetic"},
            aggregate_type="fixture",
            aggregate_id="effect-state-sentinel",
        )
        worker.complete(effect_lease.lease)
        stale_job = worker.enqueue(scope, "SyntheticO9StaleEffect", "private-stale-job-sentinel", {"value": "synthetic"})
        stale_lease = worker.claim(scope, "private-stale-worker-sentinel")
        _require(stale_lease is not None, "WORKER_LEASE_FIXTURE_UNAVAILABLE")

        source_now = _now()
        binding = MetrologySourceBinding(
            scope,
            _SENTINELS[0],
            "synthetic-provider",
            "synthetic-fixture-family",
            "synthetic-fixture-capability",
            "synthetic.adapter",
            "synthetic-schema-v1",
            "synthetic-mapping-v1",
            "a" * 64,
            "mm",
        )
        observation = MetrologyObservation(
            "private-source-row-sentinel",
            "synthetic-asset",
            "synthetic-tool",
            "synthetic-head",
            "synthetic-context",
            "synthetic-characteristic",
            "mm",
            1.0,
            source_now - timedelta(minutes=5),
            source_now - timedelta(minutes=1),
        )
        source_draft = SourceSnapshotDraft(
            binding,
            "private-source-partition-sentinel",
            "private-source-revision-sentinel",
            observation.event_at,
            observation.event_at,
            observation.source_available_at,
            source_artifact.metadata.reference,
            (observation,),
            SourceSnapshotStatus.PUBLISHED,
        )
        source_record, capability = SourceSnapshotIngressService(
            PostgreSQLSourceSnapshotStore(adapter),
            artifact_service,
            clock=lambda: source_now,
        ).publish(principal, source_draft, freshness_age_seconds=3600)
        return {
            "scope": scope,
            "principal": principal,
            "retained_snapshot_id": retained.snapshot_id,
            "stale_lease": stale_lease.lease,
            "fixture_ids": {
                "claim": safe_identity_hash(claim.result_identity),
                "acknowledge": safe_identity_hash(acknowledged.result_identity),
                "effect": safe_identity_hash(effect_receipt.result_identity),
                "effect_job": safe_identity_hash(effect_job.job_id),
                "stale_job": safe_identity_hash(stale_job.job_id),
                "source_snapshot": safe_identity_hash(source_record.snapshot_id),
                "source_capability": capability.state.value,
            },
        }
    finally:
        adapter.close()


def _accept_post_cutoff(dsn: str, scope: AccessScope, principal: Principal) -> None:
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        VersionedAggregateCommandExecutor(adapter, MutableCurrentAuthorizationAuthority(principal)).execute(
            _workflow_context(scope, principal, "private-postcutoff-command-sentinel", 0),
            command_type="SyntheticPostCutoffCommand",
            aggregate_type="fixture",
            aggregate_id="postcutoff-state-sentinel",
            payload={"accepted_after_backup_cutoff": True},
            required_capability="o9.fixture.write",
        )
    finally:
        adapter.close()


def _inventory(dsn: str) -> dict[str, object]:
    from ephi.o9_operations import _table_inventory

    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        validate_required_schema(connection)
        return _table_inventory(connection)


def _file_inventory(root: Path) -> list[dict[str, object]]:
    records = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append({"relative_sha256": safe_identity_hash(path.relative_to(root).as_posix()), "sha256": digest, "byte_size": path.stat().st_size})
    return records


def _target_connection(admin_dsn: str, database: str) -> str:
    return make_conninfo(admin_dsn, dbname=database)


def _verify_application_restart(
    dsn: str,
    scope: AccessScope,
    principal: Principal,
    retained_snapshot_id: str,
    stale_lease: WorkerLease,
) -> dict[str, object]:
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        authorization = MutableCurrentAuthorizationAuthority(principal)
        workflow = EpisodeWorkflowCommandService(adapter, authorization)
        claim = workflow.claim_episode(_workflow_context(scope, principal, "private-claim-command-sentinel", 0), "episode-private-sentinel")
        acknowledged = workflow.acknowledge_episode(
            _workflow_context(scope, principal, "private-ack-command-sentinel", 1), "episode-private-sentinel"
        )
        attention = AttentionQueryService(adapter.o3_store(), adapter.read_store(), authorization)
        page = attention.list_attention(principal, scope, page_size=1, snapshot_id=retained_snapshot_id)
        brief = EpisodeBriefQueryService(adapter.read_store(), authorization).get_episode_brief(
            principal, scope, "episode-private-sentinel"
        )
        _require(page.snapshot_id == retained_snapshot_id and len(page.rows) == 1, "RETAINED_READ_RESTART_MISMATCH")
        _require(brief.workflow["work_state"] == "ACKNOWLEDGED", "ACKNOWLEDGED_WORKFLOW_RESTART_MISMATCH")
        _require(
            adapter.connection.execute("SELECT COUNT(*) AS count FROM command_receipt").fetchone()["count"] == 2,
            "ACKNOWLEDGED_RECEIPT_REPLAY_CREATED_DUPLICATE",
        )
        worker = adapter.worker_store(
            config=WorkerLeaseConfig(lease_duration=timedelta(seconds=1), heartbeat_interval=timedelta(milliseconds=100))
        )
        adapter.connection.execute(
            "UPDATE job SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE job_id = %s",
            (stale_lease.job_id,),
        )
        takeover = worker.claim(scope, "private-recovered-worker-sentinel")
        _require(takeover is not None and takeover.lease.epoch > stale_lease.epoch, "WORKER_FENCING_EPOCH_NOT_ADVANCED")
        stale_rejected = False
        try:
            worker.commit_local_effect(
                stale_lease,
                "private-recovery-effect-sentinel",
                {"value": "synthetic"},
                aggregate_type="fixture",
                aggregate_id="effect-state-sentinel",
            )
        except Exception:
            stale_rejected = True
        _require(stale_rejected, "STALE_WORKER_LEASE_WAS_ACCEPTED")
        first = worker.commit_local_effect(
            takeover.lease,
            "private-recovery-effect-sentinel",
            {"value": "synthetic"},
            aggregate_type="fixture",
            aggregate_id="effect-state-sentinel",
        )
        replay = worker.commit_local_effect(
            takeover.lease,
            "private-recovery-effect-sentinel",
            {"value": "synthetic"},
            aggregate_type="fixture",
            aggregate_id="effect-state-sentinel",
        )
        worker.complete(takeover.lease)
        _require(first == replay, "LOCAL_EFFECT_REPLAY_IDENTITY_CHANGED")
        count = adapter.connection.execute(
            "SELECT COUNT(*) AS count FROM applied_effect WHERE job_id = %s AND effect_key = %s",
            (takeover.job_id, "private-recovery-effect-sentinel"),
        ).fetchone()["count"]
        _require(count == 1, "LOCAL_EFFECT_WAS_APPLIED_MORE_THAN_ONCE")
        return {
            "acknowledged_receipt_replay": "VERIFIED",
            "retained_read_restart": "VERIFIED",
            "worker_lease_fencing": "VERIFIED",
            "local_effect_idempotency": "VERIFIED",
        }
    finally:
        adapter.close()


def _run_cli(
    cli: Path,
    cwd: Path,
    args: list[str],
    *,
    dsn: str | None = None,
    admin_dsn: str | None = None,
    extra_path: Path | None = None,
    extra_environment: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "EPHI_POSTGRES_DSN", "EPHI_POSTGRES_ADMIN_DSN", "EPHI_TEST_POSTGRES_DSN"):
        environment.pop(key, None)
    if dsn:
        environment["EPHI_POSTGRES_DSN"] = dsn
    if admin_dsn:
        environment["EPHI_POSTGRES_ADMIN_DSN"] = admin_dsn
    if extra_environment:
        environment.update(extra_environment)
    if extra_path is not None:
        environment["PATH"] = str(extra_path) + os.pathsep + environment.get("PATH", "")
    result = subprocess.run([str(cli), *args], cwd=cwd, env=environment, capture_output=True, text=True, timeout=300)
    return result.returncode, result.stdout, result.stderr


def _assert_no_private_values(
    *outputs: str,
    stage: str,
    private_values: tuple[str, ...] = (),
) -> None:
    forbidden_values = tuple(value for value in (*_SENTINELS, *private_values) if value)
    for output in outputs:
        if output:
            _require(all(value not in output for value in forbidden_values), stage)


def _copy_backup(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination / "backup_manifest.json"


def run(args: argparse.Namespace) -> dict[str, object]:
    package_path = Path(ephi.__file__).resolve()
    _require(not any(part == "src" for part in package_path.parts), "PACKAGE_IMPORT_PATH_IS_SOURCE_TREE")
    _require(str(Path(args.repository_root).resolve() / "src") not in sys.path, "SOURCE_IMPORT_PATH_PRESENT")
    try:
        installed_extras = metadata.distribution("ephi").metadata.get_all("Provides-Extra") or []
        psycopg_version = metadata.version("psycopg")
    except metadata.PackageNotFoundError:
        raise QualificationFailure("POSTGRES_EXTRA_NOT_INSTALLED") from None
    _require("postgres" in installed_extras and psycopg_version == "3.3.6", "POSTGRES_EXTRA_NOT_INSTALLED")

    work = Path(args.work_dir).resolve()
    if work.exists() and any(work.iterdir()):
        raise QualificationFailure("QUALIFICATION_WORK_DIRECTORY_NOT_EMPTY")
    work.mkdir(parents=True, exist_ok=True)
    execution_dir = work / "non-repository-working-directory"
    execution_dir.mkdir()
    private_root = work / _SENTINELS[4]
    source_artifacts = private_root / "active-artifacts"
    backup_root = private_root / "backup-bundle"
    restored_artifacts = private_root / "restored-artifacts"
    source_artifacts.mkdir(parents=True)
    backup_root.mkdir(parents=True)

    admin_dsn = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()
    _require(bool(admin_dsn), "POSTGRESQL_18_TEST_DSN_NOT_CONFIGURED")
    for tool in ("pg_dump", "pg_restore"):
        executable = shutil.which(tool)
        _require(executable is not None, "POSTGRESQL_18_NATIVE_TOOLING_UNAVAILABLE")
        try:
            version = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=30)
        except Exception:
            raise QualificationFailure("POSTGRESQL_18_NATIVE_TOOLING_UNAVAILABLE") from None
        version_match = re.search(r"PostgreSQL\)?\s+(\d+)(?:\.(\d+))?", version.stdout or version.stderr)
        _require(version.returncode == 0 and version_match is not None and int(version_match.group(1)) == 18, "POSTGRESQL_18_NATIVE_TOOLING_UNAVAILABLE")
    source_database = "ephi_o9_src_" + uuid4().hex[:12]
    target_database = "ephi_o9_restore_" + uuid4().hex[:12]
    source_dsn = _create_isolated_database(admin_dsn, source_database)
    phase = "FIXTURE_SEED"
    cli = Path(args.venv_bin).resolve() / ("ephi-operations.exe" if os.name == "nt" else "ephi-operations")
    try:
        fixture = _seed_fixture(source_dsn, source_artifacts)
        source_before_backup = _inventory(source_dsn)
        active_artifacts_before_backup = _file_inventory(source_artifacts)

        fake_native = private_root / "fake-native-tools"
        fake_native.mkdir()
        dump_capture_path = private_root / "pg-dump-native-argv.txt"
        dump_tool = fake_native / "pg_dump_o9_capture"
        dump_tool.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if '--version' not in sys.argv:\n"
            "    capture = os.environ.get('O9_ARGV_CAPTURE', '')\n"
            "    if capture:\n"
            "        open(capture, 'w', encoding='utf-8').write(repr(['pg_dump_o9_capture', *sys.argv[1:]]))\n"
            "os.execvp('pg_dump', ['pg_dump', *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        dump_tool.chmod(0o700)

        phase = "BACKUP_CREATE"
        rc, stdout, stderr = _run_cli(
            cli,
            execution_dir,
            [
                "backup-create",
                "--artifact-root",
                str(source_artifacts),
                "--output-dir",
                str(backup_root),
                "--pg-dump-command",
                dump_tool.name,
                "--json",
            ],
            dsn=source_dsn,
            extra_path=fake_native,
            extra_environment={"O9_ARGV_CAPTURE": str(dump_capture_path)},
        )
        _require(rc == 0, "INSTALLED_BACKUP_CREATE_FAILED")
        _require(not stderr, "INSTALLED_BACKUP_CREATE_WROTE_STDERR")
        _assert_no_private_values(
            stdout,
            stderr,
            stage="BACKUP_CREATE_OUTPUT_LEAK",
            private_values=(str(source_dsn), str(source_artifacts), str(backup_root)),
        )
        dump_argv = dump_capture_path.read_text(encoding="utf-8") if dump_capture_path.exists() else ""
        _require(bool(dump_argv), "PG_DUMP_ARGV_CAPTURE_MISSING")
        _assert_no_private_values(
            dump_argv,
            stage="PG_DUMP_ARGV_PRIVATE_VALUE_LEAK",
            private_values=(str(source_dsn), str(admin_dsn), source_database, str(dump_capture_path)),
        )
        manifest_path = backup_root / "backup_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        current_release = installed_release_identity()
        current_migrations = resolve_migration_resources().identity
        _require(manifest.get("schema_version") == "o9.1.backup.v2", "BACKUP_MANIFEST_VERSION_MISMATCH")
        _require(manifest.get("release_identity") == current_release, "BACKUP_RELEASE_IDENTITY_MISMATCH")
        _require(manifest.get("migration_schema_identity") == current_migrations, "BACKUP_MIGRATION_IDENTITY_MISMATCH")
        _require("repository" not in manifest and "source_sha" not in manifest and "source_tree" not in manifest, "BACKUP_MANIFEST_FABRICATED_GIT_FACTS")
        dump = manifest["dump"]
        dump_path = backup_root / "database.dump"
        dump_sha = hashlib.sha256(dump_path.read_bytes()).hexdigest()
        _require(dump_sha == dump["sha256"] and dump_path.stat().st_size == dump["byte_size"], "BACKUP_DUMP_IDENTITY_MISMATCH")
        artifact_inventory = manifest["immutable_artifacts"]["inventory"]
        _require(manifest["immutable_artifacts"]["count"] == len(artifact_inventory), "BACKUP_ARTIFACT_COUNT_MISMATCH")
        _require(not verify_artifact_inventory(backup_root / "artifacts", artifact_inventory), "BACKUP_ARTIFACT_BYTES_MISMATCH")
        manifest_text = json.dumps(manifest, sort_keys=True)
        _assert_no_private_values(manifest_text, stage="BACKUP_MANIFEST_PRIVATE_VALUE_LEAK")
        for forbidden in ("source_sha", "source_tree", "candidate_branch", "database_name", "schema_name"):
            _require(forbidden not in manifest_text, "BACKUP_MANIFEST_CONTAINS_UNBOUND_OR_PRIVATE_IDENTITY")
        _require(len(artifact_inventory) == 1, "SYNTHETIC_ARTIFACT_REFERENCE_MISSING")
        _require(source_before_backup == _inventory(source_dsn), "BACKUP_CREATE_MUTATED_SOURCE_DATABASE")
        _require(active_artifacts_before_backup == _file_inventory(source_artifacts), "BACKUP_CREATE_MUTATED_SOURCE_ARTIFACTS")

        phase = "BACKUP_VERIFY_POSITIVE"
        rc, stdout, stderr = _run_cli(cli, execution_dir, ["backup-verify", "--manifest", str(manifest_path), "--json"])
        _require(rc == 0 and not stderr, "INSTALLED_BACKUP_VERIFY_POSITIVE_FAILED")
        positive = json.loads(stdout)
        _require(positive.get("verification_state") == "VERIFIED", "INSTALLED_BACKUP_VERIFY_POSITIVE_NOT_VERIFIED")
        _assert_no_private_values(
            stdout,
            stderr,
            stage="BACKUP_VERIFY_OUTPUT_LEAK",
            private_values=(str(manifest_path), str(work)),
        )

        phase = "BACKUP_VERIFY_NEGATIVE_CONTROLS"
        corrupt_dump_manifest = _copy_backup(backup_root, private_root / "corrupt-dump")
        (corrupt_dump_manifest.parent / "database.dump").write_bytes(b"corrupt dump sentinel")
        controls: dict[str, dict[str, object]] = {}
        controls["corrupt_dump"] = {"manifest": corrupt_dump_manifest}

        for kind in ("missing_artifact", "corrupt_artifact"):
            control_manifest = _copy_backup(backup_root, private_root / kind)
            artifact_hash = artifact_inventory[0]["sha256"]
            artifact_path = artifact_blob_path(control_manifest.parent / "artifacts", artifact_hash)
            if kind == "missing_artifact":
                artifact_path.unlink()
            else:
                artifact_path.write_bytes(b"corrupt immutable artifact sentinel")
            controls[kind] = {"manifest": control_manifest}

        changed_release = _copy_backup(backup_root, private_root / "changed-release")
        changed_manifest = json.loads(changed_release.read_text(encoding="utf-8"))
        changed_manifest["release_identity"]["release_identity_sha256"] = "f" * 64
        changed_release.write_text(json.dumps(changed_manifest), encoding="utf-8")
        controls["changed_release_identity"] = {"manifest": changed_release}

        changed_migration = _copy_backup(backup_root, private_root / "changed-migration")
        changed_manifest = json.loads(changed_migration.read_text(encoding="utf-8"))
        changed_manifest["migration_schema_identity"]["identity_sha256"] = "e" * 64
        changed_migration.write_text(json.dumps(changed_manifest), encoding="utf-8")
        controls["changed_migration_identity"] = {"manifest": changed_migration}

        malformed_manifest = _copy_backup(backup_root, private_root / "malformed-manifest")
        changed_manifest = json.loads(malformed_manifest.read_text(encoding="utf-8"))
        changed_manifest["dump"]["path"] = "../escaped"
        malformed_manifest.write_text(json.dumps(changed_manifest), encoding="utf-8")
        controls["malformed_manifest"] = {"manifest": malformed_manifest}

        unsupported_manifest = _copy_backup(backup_root, private_root / "unsupported-manifest")
        changed_manifest = json.loads(unsupported_manifest.read_text(encoding="utf-8"))
        changed_manifest["schema_version"] = "o9.1.backup.v99"
        unsupported_manifest.write_text(json.dumps(changed_manifest), encoding="utf-8")
        controls["unsupported_manifest"] = {"manifest": unsupported_manifest}

        fault_tool = fake_native / "pg_restore_o9_fault"
        fault_tool.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "capture = os.environ.get('O9_ARGV_CAPTURE', '')\n"
            "if capture:\n"
            "    open(capture, 'w', encoding='utf-8').write(repr(['pg_restore_o9_fault', *sys.argv[1:]]))\n"
            "if '--version' in sys.argv:\n"
            "    print('pg_restore (PostgreSQL) 18.6 o9-private-exception-sentinel')\n"
            "    raise SystemExit(0)\n"
            "sys.stdout.write('o9-private-raw-row-sentinel')\n"
            "sys.stderr.write('o9-private-password-sentinel o9-private-path-sentinel')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        fault_tool.chmod(0o700)
        fault_manifest = _copy_backup(backup_root, private_root / "native-tool-failure")
        capture_path = private_root / "native-argv-capture.txt"
        fault_env = dict(os.environ)
        fault_env["O9_ARGV_CAPTURE"] = str(capture_path)
        original_env = os.environ.copy()
        os.environ.update(fault_env)
        try:
            rc, stdout, stderr = _run_cli(
                cli,
                execution_dir,
                ["backup-verify", "--manifest", str(fault_manifest), "--pg-restore-command", fault_tool.name, "--json"],
                extra_path=fake_native,
            )
        finally:
            os.environ.clear()
            os.environ.update(original_env)
        _require(rc == 2, "NATIVE_TOOL_FAILURE_CONTROL_DID_NOT_FAIL")
        argv_capture = capture_path.read_text(encoding="utf-8") if capture_path.exists() else ""
        _assert_no_private_values(stdout, stderr, argv_capture, stage="NATIVE_TOOL_PRIVATE_VALUE_LEAK")
        for private_value in ("ephi_o9_src_", "private-user", "private-host", "private-database", str(fault_manifest), str(dump_path)):
            _require(private_value not in argv_capture, "NATIVE_TOOL_ARGV_CONTAINS_PRIVATE_VALUE")
        _require("--list" in argv_capture and "service=" not in argv_capture, "NATIVE_TOOL_ARGV_SHAPE_INVALID")
        controls["native_tool_exception"] = {
            "manifest": fault_manifest,
            "pg_restore_command": fault_tool.name,
        }

        incompatible_tool = fake_native / "pg_restore_o9_17"
        incompatible_tool.write_text(
            "#!/usr/bin/env python3\n"
            "print('pg_restore (PostgreSQL) 17.11')\n",
            encoding="utf-8",
        )
        incompatible_tool.chmod(0o700)
        incompatible_manifest = _copy_backup(backup_root, private_root / "incompatible-native-major")
        rc, incompatible_stdout, incompatible_stderr = _run_cli(
            cli,
            execution_dir,
            ["backup-verify", "--manifest", str(incompatible_manifest), "--pg-restore-command", incompatible_tool.name, "--json"],
            extra_path=fake_native,
        )
        _require(rc == 2, "INCOMPATIBLE_NATIVE_MAJOR_CONTROL_DID_NOT_FAIL")
        _assert_no_private_values(incompatible_stdout, incompatible_stderr, stage="INCOMPATIBLE_NATIVE_MAJOR_LEAK")
        controls["incompatible_native_major"] = {
            "manifest": incompatible_manifest,
            "pg_restore_command": incompatible_tool.name,
        }

        capture_tool = fake_native / "pg_restore_o9_capture"
        capture_tool.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "capture = os.environ.get('O9_ARGV_CAPTURE', '')\n"
            "if capture:\n"
            "    open(capture, 'w', encoding='utf-8').write(repr(['pg_restore_o9_capture', *sys.argv[1:]]))\n"
            "os.execvp('pg_restore', ['pg_restore', *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        capture_tool.chmod(0o700)

        for label, item in controls.items():
            control_args = ["backup-verify", "--manifest", str(item["manifest"]), "--json"]
            if "pg_restore_command" in item:
                control_args.extend(["--pg-restore-command", item["pg_restore_command"]])
            rc, control_stdout, control_stderr = _run_cli(
                cli,
                execution_dir,
                control_args,
                extra_path=fake_native if "pg_restore_command" in item else None,
            )
            _require(rc == 2, "BACKUP_VERIFY_NEGATIVE_CONTROL_PASSED")
            _assert_no_private_values(control_stdout, control_stderr, stage="BACKUP_VERIFY_FAILURE_DISCLOSED_PRIVATE_VALUE")

        phase = "POST_CUTOFF_RECONCILIATION"
        _accept_post_cutoff(source_dsn, fixture["scope"], fixture["principal"])
        source_snapshot = _inventory(source_dsn)
        source_files = _file_inventory(source_artifacts)
        rc, stdout, stderr = _run_cli(
            cli,
            execution_dir,
            ["reconcile", "--manifest", str(manifest_path), "--json"],
            dsn=source_dsn,
        )
        _require(rc == 0 and not stderr, "INSTALLED_RECONCILE_FAILED")
        reconciliation = json.loads(stdout)
        _require(reconciliation["post_cutoff_delta_count"] > 0, "POST_CUTOFF_DELTA_OMITTED")
        _require(reconciliation["required_controlled_recovery_action"] is True, "POST_CUTOFF_CONTROLLED_ACTION_NOT_REQUIRED")
        classes = {row["table"]: row["classification"] for row in reconciliation["post_cutoff_deltas"]}
        _require(
            classes.get("command_receipt") == "CONSEQUENTIAL_OR_EXTERNAL_REQUIRES_CONTROLLED_HANDLING",
            "POST_CUTOFF_CONSEQUENTIAL_DELTA_MISCLASSIFIED",
        )
        _assert_no_private_values(stdout, stderr, stage="RECONCILIATION_OUTPUT_LEAK")

        phase = "RESTORE_REHEARSAL"
        restore_artifact_target = private_root / "isolated-restore-root"
        restore_argv_path = private_root / "restore-native-argv.txt"
        original_env = os.environ.copy()
        os.environ["O9_ARGV_CAPTURE"] = str(restore_argv_path)
        try:
            rc, stdout, stderr = _run_cli(
                cli,
                execution_dir,
                [
                    "restore-rehearsal",
                    "--manifest",
                    str(manifest_path),
                    "--target-database",
                    target_database,
                    "--target-artifact-root",
                    str(restore_artifact_target),
                    "--pg-restore-command",
                    capture_tool.name,
                    "--json",
                ],
                dsn=source_dsn,
                admin_dsn=admin_dsn,
                extra_path=fake_native,
            )
        finally:
            os.environ.clear()
            os.environ.update(original_env)
        _require(rc == 0 and not stderr, "INSTALLED_RESTORE_REHEARSAL_FAILED")
        restore_report = json.loads(stdout)
        _require(restore_report["verification_state"] == "VERIFIED", "RESTORE_REHEARSAL_NOT_VERIFIED")
        _require(restore_report["restore_target"]["isolated"] is True, "RESTORE_TARGET_NOT_ISOLATED")
        _require(restore_report["restore_target"]["traffic_switched"] is False, "RESTORE_SWITCHED_TRAFFIC")
        _require(restore_report["durable_state"]["state_sha256"] == manifest["durable_state"]["state_sha256"], "RESTORED_STATE_HASH_MISMATCH")
        _require(
            not verify_artifact_inventory(restore_artifact_target, artifact_inventory),
            "RESTORED_ARTIFACT_IDENTITY_MISMATCH",
        )
        target_dsn = _target_connection(admin_dsn, target_database)
        restored_inventory = _inventory(target_dsn)
        _require(restored_inventory == manifest["durable_state"]["tables"], "RESTORED_DURABLE_STATE_DIFFERS")
        target_files = _file_inventory(restore_artifact_target)
        _require(
            sorted((item["sha256"], item["byte_size"]) for item in target_files)
            == sorted((item["sha256"], item["byte_size"]) for item in artifact_inventory),
            "RESTORED_ARTIFACT_BUNDLE_DIFFERS",
        )
        _require(source_snapshot == _inventory(source_dsn), "RESTORE_REHEARSAL_MUTATED_SOURCE_DATABASE")
        _require(source_files == _file_inventory(source_artifacts), "RESTORE_REHEARSAL_MUTATED_SOURCE_ARTIFACTS")
        _assert_no_private_values(
            stdout,
            stderr,
            stage="RESTORE_OUTPUT_LEAK",
            private_values=(str(source_dsn), str(admin_dsn), str(work)),
        )
        restore_argv = restore_argv_path.read_text(encoding="utf-8") if restore_argv_path.exists() else ""
        _require("--dbname=service=o9_restore" in restore_argv, "RESTORE_NATIVE_SERVICE_ALIAS_MISSING")
        _assert_no_private_values(
            restore_argv,
            stage="RESTORE_NATIVE_ARGV_PRIVATE_VALUE_LEAK",
            private_values=(str(source_dsn), str(admin_dsn), source_database, target_database),
        )
        for private_value in (str(manifest_path), str(dump_path), str(source_artifacts), str(restore_artifact_target)):
            _require(private_value not in restore_argv, "RESTORE_NATIVE_ARGV_CONTAINS_PRIVATE_PATH")

        phase = "APPLICATION_RESTART_FENCING"
        restart_proof = _verify_application_restart(
            target_dsn,
            fixture["scope"],
            fixture["principal"],
            fixture["retained_snapshot_id"],
            fixture["stale_lease"],
        )
        restored_receipt_ids = {
            row["identity_hash"]
            for row in restored_inventory["command_receipt"]["row_versions"]
        }
        _require(len(restored_receipt_ids) == 2, "ACKNOWLEDGED_COMMAND_RECEIPTS_NOT_RESTORED")

        phase = "SECRET_SENTINEL_CONTROLS"
        components = (
            _SENTINELS[5],
            _SENTINELS[6],
            _SENTINELS[7],
            _SENTINELS[8],
        )
        sentinel_dsn = (
            f"postgresql://{components[0]}:{components[1]}@{components[2]}:5432/{components[3]}"
        )
        rc, stdout, stderr = _run_cli(
            cli,
            execution_dir,
            ["backup-create", "--artifact-root", str(source_artifacts), "--output-dir", str(private_root / "secret-failure"), "--json"],
            dsn=sentinel_dsn,
        )
        _require(rc == 2, "INVALID_DSN_SENTINEL_CONTROL_DID_NOT_FAIL")
        _assert_no_private_values(
            stdout,
            stderr,
            stage="DSN_OR_PATH_SENTINEL_LEAK",
            private_values=(sentinel_dsn, str(private_root)),
        )

        release_identity = current_release["release_identity_sha256"]
        migration_identity = current_migrations["identity_sha256"]
        return {
            "schema_version": "org.ephi.installed-o9-recovery-qualification.v1",
            "status": "PASS",
            "candidate": {"commit": args.candidate_sha, "tree": args.candidate_tree},
            "installation": {"release_identity_sha256": release_identity, "postgres_extra_installed": True},
            "migration_identity_sha256": migration_identity,
            "backup": {
                "dump_sha256": dump["sha256"],
                "dump_byte_size": dump["byte_size"],
                "artifact_count": len(artifact_inventory),
                "durable_state_sha256": manifest["durable_state"]["state_sha256"],
            },
            "restore": {
                "durable_state_sha256": restore_report["durable_state"]["state_sha256"],
                "artifact_count": restore_report["immutable_artifacts"]["count"],
                "traffic_switched": restore_report["restore_target"]["traffic_switched"],
                "source_database_and_artifacts_unchanged": True,
            },
            "post_cutoff_reconciliation": {
                "delta_count": reconciliation["post_cutoff_delta_count"],
                "command_receipt_classification": classes["command_receipt"],
                "consequential_actions_replayed": False,
            },
            "restart_and_fencing": restart_proof,
            "negative_controls": sorted(controls),
            "secret_sentinels_absent": True,
            "non_repository_execution": True,
        }
    except QualificationFailure:
        raise
    except Exception:
        raise QualificationFailure(phase) from None
    finally:
        try:
            with psycopg.connect(admin_dsn, autocommit=True) as connection:
                for database in (target_database, source_database):
                    connection.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = _SafeParser(description=__doc__)
    parser.add_argument("--venv-bin", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--candidate-tree", required=True)
    try:
        args = parser.parse_args(argv)
    except QualificationFailure:
        print(json.dumps({"status": "FAIL", "reason_code": "INVALID_ARGUMENTS"}, sort_keys=True))
        return 2
    if not re.fullmatch(r"[0-9a-f]{40,64}", args.candidate_sha) or not re.fullmatch(r"[0-9a-f]{40,64}", args.candidate_tree):
        print(json.dumps({"status": "FAIL", "reason_code": "CANDIDATE_IDENTITY_INVALID"}, sort_keys=True))
        return 2
    try:
        report = run(args)
    except QualificationFailure as exc:
        print(json.dumps({"status": "FAIL", "reason_code": exc.stage}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "FAIL", "reason_code": "QUALIFICATION_INFRASTRUCTURE_OR_FIXTURE_FAILURE"}, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
