#!/usr/bin/env python3
"""Run the CHG-147 real PostgreSQL 18.x local restore rehearsal."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    ArtifactService,
    AttentionQueryService,
    CommandContext,
    EpisodeBriefQueryService,
    EpisodeWorkflowCommandService,
    MetrologyObservation,
    MetrologySourceBinding,
    Principal,
    RevisionVector,
    SourceSnapshotDraft,
    SourceSnapshotIngressService,
    SourceSnapshotStatus,
    VersionedAggregateCommandExecutor,
    WorkerLease,
    WorkerLeaseConfig,
)
from ephi.application.operations import json_bytes, safe_identity_hash  # noqa: E402
from ephi.infrastructure import (  # noqa: E402
    FileArtifactBlobStore,
    PostgreSQLReferenceTransactionAdapter,
    PostgreSQLSourceSnapshotStore,
)
from ephi.infrastructure.artifacts import PostgreSQLArtifactCatalog  # noqa: E402
from ephi.infrastructure.postgresql_worker import PostgreSQLWorkerStore  # noqa: E402
from tools.o9_operations import (  # noqa: E402
    _dsn_with_database,
    create_backup,
    operations_status,
    reconcile,
    restore_rehearsal,
    verify_backup,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _principal(scope: AccessScope) -> Principal:
    return Principal(
        "o9-rehearsal-engineer",
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
        "O9.1 local restore rehearsal",
    )


def seed_source(dsn: str, artifact_root: Path) -> dict[str, object]:
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    artifact_store = FileArtifactBlobStore(artifact_root)
    scope = AccessScope("o9-rehearsal-scope", site_id="fixture-site", area_id="fixture-area", family_id="generic-fixture-family")
    principal = _principal(scope)
    # The rehearsal database is created solely for this run. Truncation keeps a
    # rerun deterministic without touching an active/company database.
    adapter.connection.execute("TRUNCATE source_capability, source_snapshot, artifact_catalog, query_snapshot_row, query_snapshot, read_head, read_revision, applied_effect, job, outbox_event, audit_event, command_receipt, o3_attention_projection, aggregate_state CASCADE")
    artifacts = ArtifactService(artifact_store, PostgreSQLArtifactCatalog(adapter))
    source_content = b"O9 generic source manifest fixture; not authentic metrology evidence."
    source_artifact = artifacts.write_and_register(
        principal,
        scope,
        source_content,
        media_type="application/octet-stream",
        logical_purpose="o9-generic-source-manifest-fixture",
        required_write_capability="ephi.source.artifact.write",
    )
    adapter.seed_aggregate(scope, "episode_workflow", "episode-o9-1", {"work_state": "OPEN", "owner": None}, version=0)
    adapter.seed_aggregate(scope, "fixture", "worker-aggregate", {"effect_count": 0, "seed": "o9"}, version=0)
    adapter.seed_aggregate(scope, "fixture", "postcutoff-aggregate", {"effect_count": 0, "seed": "o9"}, version=0)
    adapter.seed_attention_projection(
        scope,
        "episode-o9-1",
        {
            "title": "O9 durable attention fixture",
            "asset_id": "fixture-asset",
            "priority": "P2",
            "severity": "HIGH",
            "technical_state": "READY",
            "source_state": "READY",
            "deadline": "2026-09-19T23:59:00Z",
            "age": "1",
        },
    )
    workflow = adapter.get_aggregate(scope, "episode_workflow", "episode-o9-1")
    adapter.publish_current_revision(
        scope,
        "episode",
        "episode-o9-1",
        "episode-o9-read-1",
        RevisionVector("o9-analysis-1", "o9-exposure-1", "o9-priority-1", 0, None, "o9-manifest-1"),
        {"title": "O9 durable attention fixture", "analytical_revision": "o9-analysis-1", "capability_state": {"source": "READY"}},
        workflow,
    )
    attention = AttentionQueryService(adapter.o3_store(), adapter.read_store())
    retained = attention.list_attention(principal, scope, page_size=1)
    workflow_service = EpisodeWorkflowCommandService(adapter)
    claim = workflow_service.claim_episode(_workflow_context(scope, principal, "o9-claim-1", 0), "episode-o9-1")
    acknowledge = workflow_service.acknowledge_episode(_workflow_context(scope, principal, "o9-ack-1", 1), "episode-o9-1")

    worker = PostgreSQLWorkerStore(adapter, config=WorkerLeaseConfig(lease_duration=timedelta(seconds=1), heartbeat_interval=timedelta(milliseconds=100)))
    effect_job = worker.enqueue(scope, "O9LocalEffect", "o9-effect-job", {"value": "one"})
    effect_lease = worker.claim(scope, "o9-worker-a")
    if effect_lease is None:
        raise RuntimeError("O9 effect job was not claimable")
    effect_receipt = worker.commit_local_effect(effect_lease.lease, "o9-effect-1", {"value": "one"}, aggregate_type="fixture", aggregate_id="worker-aggregate")
    worker.complete(effect_lease.lease)
    stale_job = worker.enqueue(scope, "O9StaleEffect", "o9-stale-job", {"value": "stale"})
    stale_lease = worker.claim(scope, "o9-worker-stale")
    if stale_lease is None:
        raise RuntimeError("O9 stale job was not claimable")

    source_now = _now()
    binding = MetrologySourceBinding(
        scope,
        "o9-generic-source",
        "o9-generic-provider",
        "generic-fixture-family",
        "generic-fixture-capability",
        "o9.fixture.adapter",
        "o9-fixture-schema-v1",
        "o9-fixture-mapping-v1",
        "a" * 64,
        "mm",
    )
    observation = MetrologyObservation(
        "o9-row-1",
        "o9-asset-1",
        "o9-tool-1",
        "o9-head-1",
        "o9-context-1",
        "o9-characteristic-1",
        "mm",
        1.0,
        source_now - timedelta(minutes=5),
        source_now - timedelta(minutes=1),
    )
    source_draft = SourceSnapshotDraft(
        binding,
        "o9-partition-1",
        "o9-revision-1",
        observation.event_at,
        observation.event_at,
        observation.source_available_at,
        source_artifact.metadata.reference,
        (observation,),
        SourceSnapshotStatus.PUBLISHED,
    )
    source_service = SourceSnapshotIngressService(
        PostgreSQLSourceSnapshotStore(adapter),
        artifacts,
        clock=lambda: source_now,
    )
    source_record, capability = source_service.publish(principal, source_draft, freshness_age_seconds=3600)
    fixture_executor = VersionedAggregateCommandExecutor(adapter)
    seed = {
        "scope_hash": safe_identity_hash(scope.canonical_key),
        "episode_hash": safe_identity_hash("episode-o9-1"),
        "retained_snapshot_hash": safe_identity_hash(retained.snapshot_id),
        "claim_result_hash": safe_identity_hash(claim.result_identity),
        "acknowledge_result_hash": safe_identity_hash(acknowledge.result_identity),
        "effect_result_hash": safe_identity_hash(effect_receipt.result_identity),
        "effect_job_hash": safe_identity_hash(effect_job.job_id),
        "stale_job_hash": safe_identity_hash(stale_job.job_id),
        "stale_owner_hash": safe_identity_hash(stale_lease.lease.owner),
        "stale_epoch": stale_lease.lease.epoch,
        "source_snapshot_hash": safe_identity_hash(source_record.snapshot_id),
        "source_capability_state": capability.state.value,
        "postcutoff_command_id": "o9-post-cutoff-1",
    }
    adapter.close()
    return {
        "dsn": dsn,
        "scope": scope,
        "principal": principal,
        "seed": seed,
        "retained_snapshot_id": retained.snapshot_id,
        "stale_lease": stale_lease.lease,
        "fixture_executor": fixture_executor,
    }


def accept_post_cutoff(dsn: str, scope: AccessScope, principal: Principal) -> None:
    adapter = PostgreSQLReferenceTransactionAdapter(dsn)
    try:
        executor = VersionedAggregateCommandExecutor(adapter)
        executor.execute(
            _workflow_context(scope, principal, "o9-post-cutoff-1", 0),
            command_type="O9PostCutoffLocalCommand",
            aggregate_type="fixture",
            aggregate_id="postcutoff-aggregate",
            payload={"accepted_after_backup_cutoff": True},
            required_capability="o9.fixture.write",
        )
    finally:
        adapter.close()


def verify_application_restart(target_dsn: str, scope: AccessScope, principal: Principal, retained_snapshot_id: str, stale_lease: WorkerLease) -> dict[str, object]:
    adapter = PostgreSQLReferenceTransactionAdapter(target_dsn)
    try:
        workflow = EpisodeWorkflowCommandService(adapter)
        claim_replay = workflow.claim_episode(_workflow_context(scope, principal, "o9-claim-1", 0), "episode-o9-1")
        ack_replay = workflow.acknowledge_episode(_workflow_context(scope, principal, "o9-ack-1", 1), "episode-o9-1")
        attention = AttentionQueryService(adapter.o3_store(), adapter.read_store())
        page = attention.list_attention(principal, scope, page_size=1, snapshot_id=retained_snapshot_id)
        brief = EpisodeBriefQueryService(adapter.read_store()).get_episode_brief(principal, scope, "episode-o9-1")
        worker = adapter.worker_store(config=WorkerLeaseConfig(lease_duration=timedelta(seconds=1), heartbeat_interval=timedelta(milliseconds=100)))
        adapter.connection.execute("UPDATE job SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE job_id = %s", (stale_lease.job_id,))
        takeover = worker.claim(scope, "o9-worker-recovered")
        if takeover is None or takeover.lease.epoch <= stale_lease.epoch:
            raise RuntimeError("restored stale job did not take a new fencing epoch")
        stale_rejected = False
        try:
            worker.commit_local_effect(stale_lease, "o9-stale-effect", {"value": "stale"}, aggregate_type="fixture", aggregate_id="worker-aggregate")
        except Exception as exc:
            stale_rejected = getattr(exc, "code", "") == "STALE_LEASE"
        if not stale_rejected:
            raise RuntimeError("restored stale worker/effect identity was not fenced")
        first_effect = worker.commit_local_effect(takeover.lease, "o9-stale-effect", {"value": "stale"}, aggregate_type="fixture", aggregate_id="worker-aggregate")
        replay_effect = worker.commit_local_effect(takeover.lease, "o9-stale-effect", {"value": "stale"}, aggregate_type="fixture", aggregate_id="worker-aggregate")
        worker.complete(takeover.lease)
        return {
            "read_restart": {"attention_rows": len(page.rows), "retained_snapshot_reopened": page.snapshot_id == retained_snapshot_id, "episode_workflow": brief.workflow["work_state"], "workflow_version": brief.revision_vector.workflow_version},
            "acknowledged_receipt_replay": {"claim_result_hash": safe_identity_hash(claim_replay.result_identity), "ack_result_hash": safe_identity_hash(ack_replay.result_identity), "no_new_receipt": adapter.connection.execute("SELECT COUNT(*) AS count FROM command_receipt").fetchone()["count"] == 2},
            "stale_worker_fencing": {"stale_rejected": stale_rejected, "takeover_epoch": takeover.lease.epoch, "old_epoch": stale_lease.epoch, "effect_replay_same_identity": first_effect == replay_effect, "effect_applied_once": adapter.connection.execute("SELECT COUNT(*) AS count FROM applied_effect WHERE job_id = %s AND effect_key = %s", (takeover.job_id, "o9-stale-effect")).fetchone()["count"] == 1},
        }
    finally:
        adapter.close()


def run(args: argparse.Namespace) -> dict[str, object]:
    evidence = Path(args.evidence_dir).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    artifact_root = Path(args.artifact_root).resolve()
    seed = seed_source(args.dsn, artifact_root)
    scope = seed["scope"]
    principal = seed["principal"]
    create_backup(dsn=args.dsn, artifact_root=artifact_root, output_dir=args.backup_dir)
    backup_manifest_path = Path(args.backup_dir) / "backup_manifest.json"
    accept_post_cutoff(args.dsn, scope, principal)
    reconciliation = reconcile(manifest_path=backup_manifest_path, dsn=args.dsn)
    verify = verify_backup(manifest_path=backup_manifest_path, repo_root=ROOT, backup_root=Path(args.backup_dir) / "artifacts")
    restore = restore_rehearsal(
        manifest_path=backup_manifest_path,
        source_admin_dsn=args.admin_dsn,
        target_database=args.target_database,
        target_artifact_root=args.target_artifact_root,
    )
    target_dsn = _dsn_with_database(args.admin_dsn, args.target_database)
    restart = verify_application_restart(target_dsn, scope, principal, seed["retained_snapshot_id"], seed["stale_lease"])
    health = operations_status(dsn=args.dsn, artifact_root=artifact_root)
    summary = {
        "schema_version": "o9.1.rehearsal-summary.v1",
        "operation": "CHG-147/O9.1",
        "qualification_boundary": "GENERIC_MECHANICS_ONLY_NO_AUTHENTIC_FAMILY_QUALIFICATION",
        "backup_verification": verify,
        "restore": restore,
        "application_restart_and_semantics": restart,
        "post_snapshot_reconciliation": reconciliation,
        "operations_health": health,
        "seeded_authority_identities": seed["seed"],
        "timing": {
            "scope": "LOCAL_RESTORE_REHEARSAL",
            "backup_duration_seconds": json.loads(backup_manifest_path.read_text(encoding="utf-8"))["timing"]["backup_duration_seconds"],
            "restore_duration_seconds": restore["timing"]["restore_duration_seconds"],
            "verification_duration_seconds": restore["timing"]["verification_duration_seconds"],
            "backup_cutoff_at_server": reconciliation["backup_cutoff"]["cutoff_at_server"],
            "observed_post_cutoff_delta_window_seconds": reconciliation["observed_post_cutoff_delta_window_seconds"],
        },
        "production_disaster_rpo_rto_claim": "NOT_ESTABLISHED",
        "timing_evidence_scope": "LOCAL_RESTORE_REHEARSAL",
    }
    (evidence / "backup_manifest.json").write_bytes(backup_manifest_path.read_bytes())
    (evidence / "operations_health.json").write_bytes(json_bytes(health))
    (evidence / "restore_reconciliation_report.json").write_bytes(json_bytes(summary))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--admin-dsn", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--target-artifact-root", required=True)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--evidence-dir", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(args), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "VERIFY_FAILED", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
