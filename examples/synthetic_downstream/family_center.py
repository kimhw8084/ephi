"""Seed visibly synthetic Family Center fixtures through the existing authorities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any

from ephi.application import (
    CommandContext,
    GateState,
    MetrologyObservation,
    Principal,
    SourceSnapshotDraft,
)


FAMILY_ID = "synthetic-u1-family"
CAPABILITY_ID = "synthetic-capability"
PRODUCT_ID = "synthetic-product"
GREEN_RELEASE_ID = "synthetic-release-1"
SYNTHETIC_LABEL = "SYNTHETIC NON-PRODUCTION FIXTURE"
_UTC = timezone.utc


def publish_current_synthetic_source(composition: Any, *, now: datetime | None = None, revision: str = "current") -> str:
    """Publish a bounded synthetic snapshot through O4 for fixture use only."""

    principal = composition.principal_provider()
    scope = composition.scope_provider()
    binding = composition.source_binding
    current = (now or datetime.now(_UTC)).astimezone(_UTC)
    event_at = current - timedelta(seconds=15)
    available_at = current - timedelta(seconds=10)
    artifact = composition.artifact_service.write_and_register(
        principal,
        scope,
        b'{"synthetic":true,"fixture":"family-center-source-manifest","production":false}',
        media_type="application/json",
        logical_purpose="synthetic-family-center-source-manifest",
        required_write_capability="synthetic.artifact.write",
    )
    observation = MetrologyObservation(
        source_row_id=f"synthetic-family-center-row-{revision}",
        asset_id="synthetic-u1-asset",
        tool_id=None,
        head_id=None,
        context_id="synthetic-context",
        characteristic_id="synthetic-characteristic",
        unit="um",
        value=1.25,
        event_at=event_at,
        source_available_at=available_at,
    )
    source_revision = f"synthetic-family-center-{revision}"
    record, _capability = composition.source_ingress.publish(
        principal,
        SourceSnapshotDraft(
            binding,
            "synthetic-family-center-partition",
            source_revision,
            event_at,
            event_at,
            available_at,
            artifact.metadata.reference,
            (observation,),
        ),
        freshness_age_seconds=3600,
    )
    return record.snapshot_id


def seed_synthetic_workspace(composition: Any, release_id: str, mode: str) -> dict[str, object]:
    """Seed one fixture workspace using only U1/O2/O4/O8 and O2 artifacts.

    Modes are ``green``, ``ambiguous``, ``expired``, ``failed``, ``pending`` and
    ``stale``. The stale mode first records a promoted green state and then
    publishes a new O4 snapshot so historical evidence and promotion remain
    present while current readiness is invalidated.
    """

    if mode not in {"green", "ambiguous", "expired", "failed", "pending", "stale"}:
        raise ValueError("unsupported synthetic Family Center fixture mode")
    family = next(item for item in composition.policy_configuration.family_contexts if item.family_id == FAMILY_ID)
    target = next(item for item in family.qualification_targets if item.release_id == release_id)
    identity = composition.family_workspace_identity(FAMILY_ID, target)
    principal = composition.principal_provider()
    scope = composition.scope_provider()
    service = composition.family_center
    opened = service.ensure_workspace(
        CommandContext(f"synthetic-family-open-{release_id}", principal, scope, 0),
        identity,
    )
    now = datetime.now(_UTC)

    if mode == "ambiguous":
        service.record_gate_evidence(
            CommandContext(f"synthetic-family-ambiguous-{release_id}", principal, scope, opened.version),
            identity,
            stage_id="DISCOVER_MAP",
            state=GateState.BLOCKED,
            policy_basis_id="synthetic-ambiguous-role-policy",
            policy_basis_version="1.0.0",
            engine_identity="synthetic-u1-mapping-harness.v1",
            known_at=now,
            published_at=now,
            requalification_policy_id="synthetic-remap-required",
            requalification_policy_version="1.0.0",
            reason_code="AMBIGUOUS_CANONICAL_ROLE_MAPPING",
        )
        view = service.get_workspace(principal, scope, identity.identity, current_identity=identity)
        return {"mode": mode, "workspace_id": identity.identity, "version": view.version, "promotion_ready": view.promotion_ready, "synthetic": True, "production_approval": False}

    if mode == "failed":
        service.record_gate_evidence(
            CommandContext(f"synthetic-family-failed-{release_id}", principal, scope, opened.version),
            identity,
            stage_id="DISCOVER_MAP",
            state=GateState.FAIL,
            policy_basis_id="synthetic-mapping-policy",
            policy_basis_version="1.0.0",
            engine_identity="synthetic-u1-mapping-harness.v1",
            known_at=now,
            published_at=now,
            requalification_policy_id="synthetic-remap-required",
            requalification_policy_version="1.0.0",
            reason_code="CANONICAL_MAPPING_REVIEW_FAILED",
        )
        view = service.get_workspace(principal, scope, identity.identity, current_identity=identity)
        return {"mode": mode, "workspace_id": identity.identity, "version": view.version, "promotion_ready": view.promotion_ready, "synthetic": True, "production_approval": False}

    if mode == "expired":
        evidence_time = now - timedelta(hours=2)
        artifact = _artifact(composition, scope, "discover-map-expired")
        service.record_gate_evidence(
            CommandContext(f"synthetic-family-expired-{release_id}", principal, scope, opened.version),
            identity,
            stage_id="DISCOVER_MAP",
            state=GateState.PASS,
            policy_basis_id="synthetic-mapping-policy",
            policy_basis_version="1.0.0",
            engine_identity="synthetic-u1-mapping-harness.v1",
            artifact_reference=artifact,
            known_at=evidence_time - timedelta(minutes=2),
            published_at=evidence_time,
            expires_at=evidence_time + timedelta(minutes=1),
            requalification_policy_id="synthetic-mapping-expiry",
            requalification_policy_version="1.0.0",
        )
        view = service.get_workspace(principal, scope, identity.identity, current_identity=identity)
        return {"mode": mode, "workspace_id": identity.identity, "version": view.version, "promotion_ready": view.promotion_ready, "synthetic": True, "production_approval": False}

    # Green, stale, and pending fixtures share the currently published O4 truth.
    capability = composition.source_ingress.repository.get_capability(principal, identity.source_binding)
    snapshot_id = (
        capability.latest_snapshot_id
        if capability.state.value == "READY" and capability.latest_snapshot_id
        else publish_current_synthetic_source(composition, now=now, revision=f"{release_id}-initial")
    )
    view = service.get_workspace(principal, scope, identity.identity, current_identity=identity)
    service.record_gate_evidence(
        CommandContext(f"synthetic-family-map-{release_id}", principal, scope, view.version),
        identity,
        stage_id="DISCOVER_MAP",
        state=GateState.PASS,
        policy_basis_id="synthetic-canonical-role-policy",
        policy_basis_version="1.0.0",
        engine_identity="synthetic-u1-mapping-harness.v1",
        input_identities=(identity.source_binding.mapping_hash,),
        artifact_reference=_artifact(composition, scope, "discover-map-pass"),
        known_at=now,
        published_at=now,
        expires_at=now + timedelta(hours=2),
        requalification_policy_id="synthetic-mapping-change",
        requalification_policy_version="1.0.0",
    )
    view = service.get_workspace(principal, scope, identity.identity, current_identity=identity)
    service.record_current_data_reality(CommandContext(f"synthetic-family-reality-{release_id}", principal, scope, view.version), identity)
    view = service.get_workspace(principal, scope, identity.identity, current_identity=identity)
    replay_state = GateState.PENDING if mode == "pending" else GateState.PASS
    replay_job_id = None
    if mode == "pending":
        # Keep the pending fixture tied to the real O2 queue so its bounded
        # status is truthful in Family Center. This fixture job has no replay
        # handler and contains no source rows or scientific outputs.
        replay_job = composition.adapter.worker_store().enqueue(
            scope,
            "SyntheticFamilyReplayEvidence",
            f"synthetic-family-replay-{identity.identity}",
            {
                "workspace_id": identity.identity,
                "stage_id": "REPLAY",
                "fixture": "synthetic-non-production",
                "production": False,
            },
            max_attempts=1,
        )
        replay_job_id = replay_job.job_id
    service.record_gate_evidence(
        CommandContext(f"synthetic-family-replay-{release_id}", principal, scope, view.version),
        identity,
        stage_id="REPLAY",
        state=replay_state,
        policy_basis_id="synthetic-bounded-replay-policy",
        policy_basis_version="1.0.0",
        engine_identity="synthetic-replay-harness.v1",
        input_identities=(snapshot_id,),
        artifact_reference=_artifact(composition, scope, "replay-pending" if mode == "pending" else "replay-pass"),
        known_at=now,
        published_at=now,
        expires_at=now + timedelta(hours=1),
        requalification_policy_id="synthetic-source-or-release-change",
        requalification_policy_version="1.0.0",
        job_id=replay_job_id,
        reason_code="SYNTHETIC_REPLAY_PENDING" if mode == "pending" else None,
    )
    if mode == "pending":
        view = service.get_workspace(principal, scope, identity.identity, current_identity=identity)
        return {"mode": mode, "workspace_id": identity.identity, "version": view.version, "promotion_ready": view.promotion_ready, "synthetic": True, "production_approval": False}

    view = service.get_workspace(principal, scope, identity.identity, current_identity=identity)
    service.record_gate_evidence(
        CommandContext(f"synthetic-family-golden-{release_id}", principal, scope, view.version),
        identity,
        stage_id="GOLDEN",
        state=GateState.PENDING,
        policy_basis_id="synthetic-golden-comparison-policy",
        policy_basis_version="1.0.0",
        engine_identity="synthetic-golden-harness.v1",
        input_identities=_stage_inputs(view, "GOLDEN", snapshot_id),
        artifact_reference=_artifact(composition, scope, "golden-pending"),
        known_at=now,
        published_at=now,
        expires_at=now + timedelta(hours=1),
        requalification_policy_id="synthetic-golden-policy-or-source-change",
        requalification_policy_version="1.0.0",
    )
    aggregate = composition.adapter.get_aggregate(scope, "family_qualification_workspace", identity.identity)
    golden = next(row for row in aggregate.state["gate_revisions"] if row["stage_id"] == "GOLDEN")
    _as_reviewer(composition, True)
    try:
        reviewer = composition.principal_provider()
        service.adjudicate_gate(
            CommandContext(f"synthetic-family-judge-golden-{release_id}", reviewer, scope, aggregate.version),
            identity,
            stage_id="GOLDEN",
            expected_evidence_revision_id=golden["revision_id"],
            decision=GateState.PASS,
            judgment_basis_id="synthetic-independent-golden-review",
            judgment_basis_version="1.0.0",
            reason_code="INDEPENDENT_REVIEW_PASS",
        )
        view = service.get_workspace(reviewer, scope, identity.identity, current_identity=identity)
        service.record_gate_evidence(
            CommandContext(f"synthetic-family-shadow-na-{release_id}", reviewer, scope, view.version),
            identity,
            stage_id="SHADOW",
            state=GateState.NOT_APPLICABLE,
            policy_basis_id="synthetic-shadow-policy-not-applicable",
            policy_basis_version="1.0.0",
            engine_identity="synthetic-policy-authority.v1",
            input_identities=_stage_inputs(view, "SHADOW", snapshot_id),
            known_at=now,
            published_at=now,
            expires_at=now + timedelta(hours=1),
            requalification_policy_id="synthetic-shadow-policy-change",
            requalification_policy_version="1.0.0",
            reason_code="POLICY_AUTHORIZED_NOT_APPLICABLE",
        )
        view = service.get_workspace(reviewer, scope, identity.identity, current_identity=identity)
        _as_reviewer(composition, False)
        engineer = composition.principal_provider()
        service.record_gate_evidence(
            CommandContext(f"synthetic-family-qualify-{release_id}", engineer, scope, view.version),
            identity,
            stage_id="QUALIFY",
            state=GateState.PENDING,
            policy_basis_id="synthetic-family-qualification-policy",
            policy_basis_version="1.0.0",
            engine_identity="synthetic-bounded-qualification-harness.v1",
            input_identities=_stage_inputs(view, "QUALIFY", snapshot_id),
            artifact_reference=_artifact(composition, scope, "qualify-pending"),
            known_at=now,
            published_at=now,
            expires_at=now + timedelta(hours=1),
            requalification_policy_id="synthetic-release-or-policy-change",
            requalification_policy_version="1.0.0",
        )
        aggregate = composition.adapter.get_aggregate(scope, "family_qualification_workspace", identity.identity)
        qualification = next(row for row in aggregate.state["gate_revisions"] if row["stage_id"] == "QUALIFY")
        _as_reviewer(composition, True)
        reviewer = composition.principal_provider()
        service.adjudicate_gate(
            CommandContext(f"synthetic-family-judge-qualify-{release_id}", reviewer, scope, aggregate.version),
            identity,
            stage_id="QUALIFY",
            expected_evidence_revision_id=qualification["revision_id"],
            decision=GateState.PASS,
            judgment_basis_id="synthetic-independent-qualification-review",
            judgment_basis_version="1.0.0",
            reason_code="INDEPENDENT_REVIEW_PASS",
        )
        view = service.get_workspace(reviewer, scope, identity.identity, current_identity=identity)
        if not view.promotion_ready:
            raise RuntimeError("synthetic green workspace unexpectedly failed promotion readiness")
        if mode in {"green", "stale"}:
            gate_ids = tuple(next(gate.revision_id for gate in view.gates if gate.stage_id == stage) for stage in identity.required_stages)
            service.promote(
                CommandContext(f"synthetic-family-promote-{release_id}", reviewer, scope, view.version),
                identity,
                gate_identity_set=gate_ids,
                source_reality_identity=view.source_reality.reality_identity,
            )
        if mode == "stale":
            publish_current_synthetic_source(composition, revision=f"{release_id}-dependency-change")
            view = service.get_workspace(reviewer, scope, identity.identity, current_identity=identity)
        return {
            "mode": mode,
            "workspace_id": identity.identity,
            "version": view.version,
            "promotion_ready": view.promotion_ready,
            "promotion_state": view.promotions[-1].state if view.promotions else None,
            "gate_revision_count": len(composition.adapter.get_aggregate(scope, "family_qualification_workspace", identity.identity).state["gate_revisions"]),
            "synthetic": True,
            "production_approval": False,
        }
    finally:
        _as_reviewer(composition, False)


def _artifact(composition: Any, scope: Any, name: str):
    principal = composition.principal_provider()
    return composition.artifact_service.write_and_register(
        principal,
        scope,
        f'{{"synthetic":true,"fixture":"family-center","stage":"{name}","production":false}}'.encode(),
        media_type="application/json",
        logical_purpose=f"synthetic-family-center-{name}",
        required_write_capability="synthetic.artifact.write",
    ).metadata.reference


def _stage_inputs(view: Any, stage: str, snapshot_id: str) -> tuple[str, ...]:
    order = ("REPLAY", "GOLDEN", "SHADOW", "QUALIFY")
    required = {"REPLAY", "GOLDEN", "SHADOW"}
    inputs = [snapshot_id]
    for prior in order:
        if prior == stage:
            break
        if prior not in required:
            continue
        gate = next((item for item in view.gates if item.stage_id == prior), None)
        if gate is not None and gate.revision_id:
            inputs.append(gate.revision_id)
    return tuple(inputs)


def _as_reviewer(composition: Any, reviewer: bool) -> None:
    if reviewer:
        os.environ["EPHI_SYNTHETIC_SUBJECT"] = "synthetic-independent-reviewer"
    else:
        os.environ["EPHI_SYNTHETIC_SUBJECT"] = "synthetic-engineer"


__all__ = [
    "CAPABILITY_ID",
    "FAMILY_ID",
    "GREEN_RELEASE_ID",
    "PRODUCT_ID",
    "SYNTHETIC_LABEL",
    "publish_current_synthetic_source",
    "seed_synthetic_workspace",
]
