"""Synthetic CHG-234 Asset 360 fixture; every identity and fact is fictional."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any

from ephi.application import (
    CommandContext,
    EPISODE_WORKFLOW_AGGREGATE_TYPE,
    HistoricalSourceIdentity,
    MetrologyObservation,
    RevisionVector,
    SourceSnapshotDraft,
    SourceSnapshotStatus,
    canonical_json,
)
from .flagship import (
    CHARACTERISTIC_ID,
    CONTEXT_ID,
    FAMILY_ID,
    SYNTHETIC_DISCLAIMER,
    TARGET_ID,
    UNIT_ID,
    comparable_profile,
    flagship_investigation_payload,
)


UTC = timezone.utc
PRIMARY_ASSET = "synthetic-cd-asset-primary"
PEER_ASSET = "synthetic-cd-asset-peer"
INCOMPATIBLE_ASSET = "synthetic-cd-asset-incompatible"
SYNTHETIC_ASSET_IDS = (PRIMARY_ASSET, PEER_ASSET, INCOMPATIBLE_ASSET)
PRIMARY_EPISODES = (
    "synthetic-cd-primary-episode-01",
    "synthetic-cd-primary-episode-02",
    "synthetic-cd-primary-episode-03",
)
COMPATIBLE_PEER_EPISODE = "synthetic-cd-peer-episode-01"
INCOMPATIBLE_EPISODE = "synthetic-cd-incompatible-episode-01"
FIXTURE_DISCLAIMER = f"{SYNTHETIC_DISCLAIMER} CHG-234 Asset 360 fixture."


def _replace_exact(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_exact(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_exact(item, replacements) for key, item in value.items()}
    return value


def _profile_payload(
    episode_id: str,
    revision_vector: RevisionVector,
    cycle_id: str,
    source: HistoricalSourceIdentity,
    *,
    asset_id: str,
    onset_at: datetime,
    headline: str,
    context_id: str = CONTEXT_ID,
    characteristic_id: str = CHARACTERISTIC_ID,
    unit: str = UNIT_ID,
) -> dict[str, Any]:
    payload = flagship_investigation_payload(
        episode_id=episode_id,
        revision_vector=revision_vector,
        cycle_id=cycle_id,
        source=source,
        comparables=comparable_profile(
            source,
            feature_values={"asset": asset_id, "episode": episode_id, "context": context_id},
            case_identity=episode_id,
            limitations=("SYNTHETIC_DEMONSTRATION",),
            context_identity=context_id,
        ),
    )
    replacements = {
        TARGET_ID: "synthetic-cd-asset-target",
        CONTEXT_ID: context_id,
        CHARACTERISTIC_ID: characteristic_id,
        UNIT_ID: unit,
    }
    payload = _replace_exact(payload, replacements)
    assert isinstance(payload, dict)
    target = payload["target"]
    target["target_identity"] = "synthetic-cd-asset-target"
    target["asset_identity"] = asset_id
    payload["change"]["headline"] = headline
    payload["change"]["description"] = "Synthetic longitudinal engineering Episode; observational fixture only."
    payload["change"]["magnitude"] = "synthetic demonstration value"
    payload["change"]["onset_at"] = onset_at
    return payload


def _seed_episode(
    composition: Any,
    source: HistoricalSourceIdentity,
    episode_id: str,
    asset_id: str,
    revision_id: str,
    known_at: datetime,
    *,
    headline: str,
    onset_at: datetime,
    context_id: str = CONTEXT_ID,
    characteristic_id: str = CHARACTERISTIC_ID,
    unit: str = UNIT_ID,
) -> dict[str, object]:
    adapter = composition.adapter
    principal = composition.principal_provider()
    scope = composition.scope_provider()
    existing_head = adapter.connection.execute(
        "SELECT head_version FROM read_head WHERE scope_key = %s AND entity_type = 'episode' AND entity_id = %s",
        (scope.canonical_key, episode_id),
    ).fetchone()
    if existing_head is None:
        initial = {"work_state": "OPEN", "owner": None}
        adapter.seed_aggregate(scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, episode_id, initial, version=0)
        before = RevisionVector(f"synthetic-analysis-{episode_id}-v1", None, None, 0, None, f"synthetic-manifest-{episode_id}")
        initialized = composition.decision_loop.initialize_decision_loop(
            CommandContext(f"synthetic-asset-init-{episode_id}", principal, scope, 0, before),
            episode_id,
            cycle_id=f"synthetic-cycle-{episode_id}-1",
        )
        analysis_revision = before.analysis_revision
        manifest_identity = before.qualification_manifest_id
        workflow_version = initialized.aggregate_version
    else:
        aggregate = adapter.get_aggregate(scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, episode_id)
        prior_revision = adapter.read_store().get_current_head(scope, "episode", episode_id)
        stored_revision = adapter.read_store().get_read_revision(prior_revision.revision_id)
        analysis_revision = stored_revision.revision_vector.analysis_revision
        manifest_identity = stored_revision.revision_vector.qualification_manifest_id
        workflow_version = aggregate.version
    vector = RevisionVector(analysis_revision, None, None, workflow_version, None, manifest_identity)
    profile_payload = _profile_payload(
        episode_id,
        vector,
        f"synthetic-cycle-{episode_id}-1",
        source,
        asset_id=asset_id,
        onset_at=onset_at,
        headline=headline,
        context_id=context_id,
        characteristic_id=characteristic_id,
        unit=unit,
    )
    analytical = {"episode_id": episode_id, "title": headline, "investigation_profile": profile_payload}
    historical_workflow = adapter.get_aggregate(scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, episode_id).state
    connection = adapter.connection
    connection.execute(
        """
        INSERT INTO read_revision(
            revision_id, scope_key, entity_type, entity_id, revision_vector_json, payload_json,
            known_at, published_at, workflow_aggregate_type, workflow_aggregate_id,
            workflow_version, workflow_state_json
        ) VALUES (%s, %s, 'episode', %s, %s::jsonb, %s::jsonb, %s, %s,
                  'episode_workflow', %s, 1, %s::jsonb)
        """,
        (
            revision_id, scope.canonical_key, episode_id,
            canonical_json(vector.as_dict()),
            canonical_json(analytical),
            known_at, known_at, episode_id,
            canonical_json(historical_workflow),
        ),
    )
    if existing_head is None:
        connection.execute(
            "INSERT INTO read_head(scope_key, entity_type, entity_id, revision_id, head_version, published_at) VALUES (%s, 'episode', %s, %s, 1, %s)",
            (scope.canonical_key, episode_id, revision_id, known_at),
        )
    else:
        connection.execute(
            "UPDATE read_head SET revision_id = %s, head_version = head_version + 1, published_at = %s "
            "WHERE scope_key = %s AND entity_type = 'episode' AND entity_id = %s AND head_version = %s",
            (revision_id, known_at, scope.canonical_key, episode_id, existing_head["head_version"]),
        )
    return {"episode_id": episode_id, "revision_id": revision_id, "known_at": known_at.isoformat(), "workflow_version": 1}


def seed_asset_360_fixture(
    composition: Any,
    *,
    with_ready_source: bool = True,
    source_capability_case: str = "READY",
) -> dict[str, Any]:
    """Seed exact synthetic Episodes and one bounded O4 capability snapshot."""

    adapter = composition.adapter
    scope = composition.scope_provider()
    principal = composition.principal_provider()
    binding = composition.source_binding
    source_capability_case = source_capability_case.upper()
    if not with_ready_source and source_capability_case == "READY":
        source_capability_case = "UNAVAILABLE"
    if source_capability_case not in {"READY", "STALE", "PARTIAL", "UNAVAILABLE"}:
        raise ValueError("source_capability_case must be READY, STALE, PARTIAL, or UNAVAILABLE")
    if binding.family_id != FAMILY_ID or binding.scope != scope:
        raise ValueError("synthetic Asset 360 fixture requires its exact flagship U1/O4 scope and family")
    adapter.connection.execute(
        "TRUNCATE handoff_delivery_attempt, handoff_delivery_status, handoff_intent, decision_snapshot, "
        "source_capability, source_snapshot, artifact_catalog, o3_attention_projection, query_snapshot_row, "
        "query_snapshot, read_head, read_revision, applied_effect, job, outbox_event, audit_event, "
        "command_receipt, aggregate_state CASCADE"
    )
    anchor_text = os.environ.get("EPHI_SYNTHETIC_ASSET_OBSERVATION_ANCHOR")
    now = datetime.fromisoformat(anchor_text.replace("Z", "+00:00")) if anchor_text else datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("synthetic Asset fixture anchor must be timezone-aware")
    now = now.astimezone(UTC).replace(microsecond=0)
    source_event = now - timedelta(seconds=30)
    source_available = now - timedelta(seconds=20)
    source_observation = MetrologyObservation(
        "synthetic-asset-source-manifest-row", PRIMARY_ASSET, None, None, CONTEXT_ID,
        CHARACTERISTIC_ID, UNIT_ID, 50.1, source_event, source_available,
        comparable_population_id=binding.comparable_population_id,
    )
    artifact = composition.artifact_service.write_and_register(
        principal,
        scope,
        b'{"synthetic":true,"fixture":"chg-234-asset-360-source-manifest","production":false}',
        media_type="application/json",
        logical_purpose="synthetic-chg-234-source-manifest",
        required_write_capability="synthetic.artifact.write",
    )
    status = {
        "READY": SourceSnapshotStatus.PUBLISHED,
        "STALE": SourceSnapshotStatus.PUBLISHED,
        "PARTIAL": SourceSnapshotStatus.PARTIAL,
        "UNAVAILABLE": SourceSnapshotStatus.INSUFFICIENT,
    }[source_capability_case]
    source_record, _capability = composition.source_ingress.publish(
        principal,
        SourceSnapshotDraft(
            binding,
            "synthetic-asset-360-partition",
            f"synthetic-asset-360-{source_capability_case.lower()}-v1",
            source_event,
            source_event,
            source_available,
            artifact.metadata.reference,
            () if source_capability_case == "UNAVAILABLE" else (source_observation,),
            status,
        ),
        freshness_age_seconds=1 if source_capability_case == "STALE" else 3600,
    )
    source_identity = HistoricalSourceIdentity(
        source_record.snapshot_id,
        source_record.source_revision,
        source_record.manifest_hash,
        source_record.artifact_reference.content.sha256,
    )
    onset = datetime(2025, 1, 15, 14, 32, tzinfo=UTC)
    primary_facts = []
    episode_specs = (
        (PRIMARY_EPISODES[0], "synthetic-cd-primary-revision-01", datetime(2025, 1, 18, tzinfo=UTC), onset + timedelta(days=1), "Synthetic mean CD excursion · first Episode"),
        (PRIMARY_EPISODES[1], "synthetic-cd-primary-revision-02", datetime(2025, 1, 25, tzinfo=UTC), onset + timedelta(days=8), "Synthetic mean CD excursion · second Episode"),
        (PRIMARY_EPISODES[2], "synthetic-cd-primary-revision-03", datetime(2025, 2, 12, tzinfo=UTC), onset + timedelta(days=25), "Synthetic mean CD excursion · third Episode"),
    )
    for episode_id, revision_id, known_at, event_onset, headline in episode_specs:
        primary_facts.append(_seed_episode(
            composition, source_identity, episode_id, PRIMARY_ASSET, revision_id, known_at,
            headline=headline, onset_at=event_onset,
        ))
    primary_facts.append(_seed_episode(
        composition, source_identity, PRIMARY_EPISODES[-1], PRIMARY_ASSET,
        "synthetic-cd-primary-revision-03b", datetime(2025, 2, 13, tzinfo=UTC),
        headline="Synthetic mean CD excursion · third Episode revision",
        onset_at=onset + timedelta(days=27),
    ))
    peer_fact = _seed_episode(
        composition, source_identity, COMPATIBLE_PEER_EPISODE, PEER_ASSET,
        "synthetic-cd-peer-revision-01", datetime(2025, 2, 14, tzinfo=UTC),
        headline="Synthetic compatible peer · reference trajectory", onset_at=onset + timedelta(days=27),
    )
    incompatible_fact = _seed_episode(
        composition, source_identity, INCOMPATIBLE_EPISODE, INCOMPATIBLE_ASSET,
        "synthetic-cd-incompatible-revision-01", datetime(2025, 2, 15, tzinfo=UTC),
        headline="Synthetic incompatible context · compare must block", onset_at=onset + timedelta(days=28),
        context_id="synthetic-recipe-r48", characteristic_id="Edge CD", unit="um",
    )

    # A future-known immutable revision is present in a separate Episode so a
    # requested earlier cutoff can prove that it is excluded.
    future_episode = "synthetic-cd-primary-future-episode"
    future_revision = "synthetic-cd-primary-future-revision"
    future_known = now + timedelta(days=1)
    future_fact = _seed_episode(
        composition, source_identity, future_episode, PRIMARY_ASSET, future_revision, future_known,
        headline="Synthetic future-known Episode · excluded by earlier cutoff", onset_at=onset + timedelta(days=29),
    )

    # Record one observational O5 action and one recovery plan via the
    # existing unified workflow authority. Their database commit time is
    # retained independently from requested/authorized/observed action times.
    episode_id = PRIMARY_EPISODES[-1]
    current = adapter.get_aggregate(scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, episode_id)
    viewed = RevisionVector(f"synthetic-analysis-{episode_id}-v1", None, None, current.version, None, f"synthetic-manifest-{episode_id}")
    action_at = now - timedelta(days=2)
    action = composition.decision_loop.record_external_action(
        CommandContext("synthetic-asset-action-record", principal, scope, current.version, viewed),
        episode_id,
        "synthetic-asset-observational-action-01",
        action_type="APPROVED_WORK_REQUEST",
        external_system="synthetic-maintenance-system",
        work_request_id="synthetic-work-request-01",
        reconciliation_state="UNKNOWN",
        requested_at=action_at,
        authorized_at=action_at + timedelta(hours=1),
    )
    recovery_context = RevisionVector(f"synthetic-analysis-{episode_id}-v1", None, None, action.aggregate_version, None, f"synthetic-manifest-{episode_id}")
    recovery = composition.decision_loop.create_recovery_plan(
        CommandContext("synthetic-asset-recovery-plan", principal, scope, action.aggregate_version, recovery_context),
        episode_id,
        "synthetic-asset-observational-recovery-01",
        policy=composition.policy_configuration.recovery_policy,
        policy_version=composition.policy_configuration.version,
        prior_action_id="synthetic-asset-observational-action-01",
        context_identity=CONTEXT_ID,
        characteristic_identity=CHARACTERISTIC_ID,
        unit_identity=UNIT_ID,
    )
    return {
        "synthetic": True,
        "production": False,
        "disclaimer": FIXTURE_DISCLAIMER,
        "scope": scope.as_dict(),
        "source_binding_identity": source_record.binding.as_dict(),
        "source_snapshot_id": source_record.snapshot_id,
        "source_capability_state": _capability.state.value,
        "source_capability_case": source_capability_case,
        "primary_asset_id": PRIMARY_ASSET,
        "peer_asset_id": PEER_ASSET,
        "incompatible_asset_id": INCOMPATIBLE_ASSET,
        "primary_episodes": primary_facts,
        "peer_episode": peer_fact,
        "incompatible_episode": incompatible_fact,
        "future_episode": {**future_fact, "known_at": future_known.isoformat()},
        "action_version": action.aggregate_version,
        "recovery_version": recovery.aggregate_version,
        "source_ready_at": now.isoformat(),
    }


__all__ = [
    "COMPATIBLE_PEER_EPISODE", "FIXTURE_DISCLAIMER", "INCOMPATIBLE_ASSET", "INCOMPATIBLE_EPISODE",
    "PEER_ASSET", "PRIMARY_ASSET", "PRIMARY_EPISODES", "SYNTHETIC_ASSET_IDS", "seed_asset_360_fixture",
]
