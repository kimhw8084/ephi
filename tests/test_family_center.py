"""Family Center qualification state uses O2/O4/O8 and existing artifacts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    ArtifactBlobWriteResult,
    ArtifactCatalogRegistration,
    ArtifactContentIdentity,
    ArtifactMetadata,
    ArtifactService,
    AuthorizationDeniedError,
    CommandContext,
    FamilyCenterService,
    GateState,
    IdempotencyConflictError,
    MetrologySourceBinding,
    MutableCurrentAuthorizationAuthority,
    Principal,
    ProviderIdentity,
    QualificationWorkspaceIdentity,
    ScopedArtifactReference,
    SourceCapabilityRecord,
    SourceCapabilityState,
    SourceSnapshotRecord,
    SourceSnapshotStatus,
    VersionConflictError,
    ValidationFailureError,
    FAMILY_CENTER_ARTIFACT_READ,
    FAMILY_CENTER_JUDGE,
    FAMILY_CENTER_POLICY,
    FAMILY_CENTER_PROMOTE,
    FAMILY_CENTER_READ,
    FAMILY_CENTER_WRITE,
    FAMILY_CENTER_STAGE_ORDER,
)
from ephi.application.artifacts import ArtifactCatalog, ArtifactBlobStore  # noqa: E402
from ephi.application.errors import ArtifactIntegrityError, SourceSnapshotNotFoundError  # noqa: E402
from ephi.infrastructure.sqlite import SQLiteReferenceTransactionAdapter  # noqa: E402


CAPABILITIES = frozenset({
    FAMILY_CENTER_READ,
    FAMILY_CENTER_WRITE,
    FAMILY_CENTER_JUDGE,
    FAMILY_CENTER_POLICY,
    FAMILY_CENTER_PROMOTE,
    FAMILY_CENTER_ARTIFACT_READ,
    "ephi.source.read",
    "synthetic.artifact.write",
})


class _Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class _BlobStore(ArtifactBlobStore):
    def __init__(self):
        self.content: dict[str, bytes] = {}

    def put_bytes(self, content: bytes) -> ArtifactBlobWriteResult:
        identity = ArtifactContentIdentity.from_bytes(content)
        reused = identity.sha256 in self.content
        self.content[identity.sha256] = content
        return ArtifactBlobWriteResult(identity, reused)

    def verify(self, identity: ArtifactContentIdentity) -> None:
        content = self.content.get(identity.sha256)
        if content is None or ArtifactContentIdentity.from_bytes(content) != identity:
            raise ArtifactIntegrityError("synthetic test blob unavailable")

    def read(self, identity: ArtifactContentIdentity) -> bytes:
        self.verify(identity)
        return self.content[identity.sha256]


class _Catalog(ArtifactCatalog):
    def __init__(self):
        self.rows: dict[tuple[str, str], ArtifactMetadata] = {}

    def register(self, metadata: ArtifactMetadata, *, object_key: str) -> ArtifactCatalogRegistration:
        key = (metadata.reference.scope_key, metadata.content.sha256)
        prior = self.rows.get(key)
        if prior is not None and prior.immutable_metadata_key() != metadata.immutable_metadata_key():
            raise ValueError("test artifact metadata conflict")
        if prior is None:
            self.rows[key] = metadata
        return ArtifactCatalogRegistration(self.rows[key], prior is None)

    def get(self, reference: ScopedArtifactReference) -> ArtifactMetadata | None:
        return self.rows.get((reference.scope_key, reference.content.sha256))


class _SourceRepository:
    def __init__(self, clock: _Clock):
        self.clock = clock
        self.capabilities: dict[str, SourceCapabilityRecord] = {}
        self.snapshots: dict[str, SourceSnapshotRecord] = {}

    @staticmethod
    def _key(binding: MetrologySourceBinding) -> str:
        return hashlib.sha256(str(binding.as_dict()).encode()).hexdigest()

    def get_capability(self, principal: Principal, binding: MetrologySourceBinding) -> SourceCapabilityRecord:
        if not principal.grants_scope(binding.scope) or not principal.has_capability("ephi.source.read"):
            raise AuthorizationDeniedError("source read denied")
        return self.capabilities.get(self._key(binding)) or SourceCapabilityRecord(
            binding,
            SourceCapabilityState.UNAVAILABLE,
            None,
            None,
            None,
            self.clock.now,
            3600,
            "CAPABILITY_RECORD_MISSING",
        )

    def get_snapshot(self, principal: Principal, scope: AccessScope, snapshot_id: str) -> SourceSnapshotRecord:
        if not principal.grants_scope(scope) or not principal.has_capability("ephi.source.read"):
            raise AuthorizationDeniedError("source read denied")
        if snapshot_id not in self.snapshots:
            raise SourceSnapshotNotFoundError()
        return self.snapshots[snapshot_id]

    def publish_ready(self, principal: Principal, binding: MetrologySourceBinding, artifact: ScopedArtifactReference) -> SourceSnapshotRecord:
        now = self.clock.now
        snapshot_id = "a" * 64
        record = SourceSnapshotRecord(
            snapshot_id,
            binding,
            "synthetic-family-partition",
            "synthetic-family-revision-1",
            now - timedelta(seconds=10),
            now - timedelta(seconds=5),
            now - timedelta(seconds=5),
            artifact,
            1,
            SourceSnapshotStatus.PUBLISHED,
            "b" * 64,
            now - timedelta(seconds=4),
            now - timedelta(seconds=3),
            now - timedelta(seconds=4),
        )
        self.snapshots[snapshot_id] = record
        self.capabilities[self._key(binding)] = SourceCapabilityRecord(
            binding,
            SourceCapabilityState.READY,
            snapshot_id,
            record.event_end,
            record.available_cutoff,
            now,
            3600,
            "FRESH_PUBLISHED_SNAPSHOT",
            record.source_partition,
            record.source_revision,
        )
        return record

    def set_state(self, binding: MetrologySourceBinding, state: SourceCapabilityState) -> None:
        existing = self.get_capability(_principal(binding.scope), binding)
        self.capabilities[self._key(binding)] = replace(
            existing,
            state=state,
            checked_at=self.clock.now,
            reason="SOURCE_AVAILABILITY_EXCEEDS_FRESHNESS_LIMIT" if state is SourceCapabilityState.STALE else "FRESH_PUBLISHED_SNAPSHOT",
        )


def _principal(scope: AccessScope, subject: str = "synthetic-engineer") -> Principal:
    return Principal(subject, CAPABILITIES, (scope,), 1, 1)


def _identity(binding: MetrologySourceBinding, *, release_id: str = "synthetic-release-1") -> QualificationWorkspaceIdentity:
    contracts = tuple(
        ProviderIdentity(category, f"org.ephi.{category}.synthetic", "1.0.0")
        for category in ("artifacts", "identity", "notifications", "policy", "runtime", "source")
    )
    policies = tuple((stage, hashlib.sha256(f"synthetic-{stage}".encode()).hexdigest()) for stage in ("REPLAY", "GOLDEN", "SHADOW", "QUALIFY"))
    return QualificationWorkspaceIdentity(
        binding.scope,
        binding.family_id,
        "1.0.0",
        "synthetic-target",
        "synthetic-context",
        binding.unit,
        "synthetic-characteristic",
        binding.capability_id,
        "synthetic-product",
        release_id,
        "org.ephi.downstream",
        "1.0.0",
        contracts,
        "org.ephi.policy-configuration",
        "1.0.0",
        "1.0.0",
        "c" * 64,
        binding,
        "test",
        18,
        "1.0.0",
        policies,
        not_applicable_stages=("SHADOW",),
        independent_judgment_stages=("GOLDEN", "QUALIFY"),
        synthetic_fixture=True,
    )


class _CountingStore:
    def __init__(self, store):
        self.store = store
        self.transactions = 0

    def command_transaction(self):
        self.transactions += 1
        return self.store.command_transaction()


class FamilyCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "family-center.sqlite3"
        self.store = SQLiteReferenceTransactionAdapter(self.path)
        self.addCleanup(self.store.close)
        self.clock = _Clock()
        self.scope = AccessScope("synthetic-family-scope", site_id="synthetic-site", family_id="synthetic-family")
        self.binding = MetrologySourceBinding(
            self.scope,
            "synthetic-source",
            "synthetic-provider",
            "synthetic-family",
            "synthetic-capability",
            "synthetic-adapter",
            "synthetic-schema.v1",
            "1.0.0",
            "1" * 64,
            "um",
            "synthetic-reference-population",
            "synthetic-comparable-population",
            ("asset_id", "context_id", "characteristic_id"),
        )
        self.identity = _identity(self.binding)
        self.engineer = _principal(self.scope)
        self.reviewer = _principal(self.scope, "synthetic-independent-reviewer")
        self.current = MutableCurrentAuthorizationAuthority(self.engineer)
        self.artifacts = ArtifactService(_BlobStore(), _Catalog(), self.current)
        self.source = _SourceRepository(self.clock)
        self.service = FamilyCenterService(self.store, self.current, self.source, self.artifacts, clock=self.clock)
        self.source_artifact = self.write_artifact("synthetic O4 manifest evidence")
        self.source_snapshot = self.source.publish_ready(self.engineer, self.binding, self.source_artifact)

    def context(self, command_id: str, version: int, principal: Principal | None = None) -> CommandContext:
        return CommandContext(command_id, principal or self.engineer, self.scope, version)

    def write_artifact(self, body: str, principal: Principal | None = None) -> ScopedArtifactReference:
        identity = self.artifacts.write_and_register(
            principal or self.current._current,
            self.scope,
            body.encode("utf-8"),
            media_type="application/json",
            logical_purpose="synthetic-family-center-evidence",
            required_write_capability="synthetic.artifact.write",
        )
        return identity.metadata.reference

    def open_workspace(self):
        return self.service.ensure_workspace(self.context("open-family-workspace", 0), self.identity)

    def _inject_gate_times(self, stage: str, **timestamps: datetime):
        row = self.store.connection.execute(
            "SELECT version, state_json FROM aggregate_state WHERE scope_key = ? AND aggregate_type = ? AND aggregate_id = ?",
            (self.scope.canonical_key, "family_qualification_workspace", self.identity.identity),
        ).fetchone()
        state = json.loads(row["state_json"])
        revision = next(item for item in reversed(state["gate_revisions"]) if item["stage_id"] == stage)
        for field, value in timestamps.items():
            revision[field] = value.isoformat()
        self.store.connection.execute(
            "UPDATE aggregate_state SET state_json = ? WHERE scope_key = ? AND aggregate_type = ? AND aggregate_id = ?",
            (json.dumps(state, sort_keys=True, separators=(",", ":")), self.scope.canonical_key,
             "family_qualification_workspace", self.identity.identity),
        )
        return revision

    def _prepare_pending_golden(self):
        view = self.open_workspace()
        self.record("DISCOVER_MAP", view.version, body="synthetic map")
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.service.record_current_data_reality(self.context("data-reality-for-future-judgment", view.version), self.identity)
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.record("REPLAY", view.version, body="synthetic replay")
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.record("GOLDEN", view.version, state=GateState.PENDING, body="synthetic golden")
        aggregate = self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity)
        golden = next(item for item in aggregate.state["gate_revisions"] if item["stage_id"] == "GOLDEN")
        return aggregate, golden

    def record(self, stage: str, version: int, *, state: GateState = GateState.PASS, body: str | None = None, command_id: str | None = None, expires_in: int = 3600, principal: Principal | None = None, policy_id: str | None = None):
        now = self.clock.now
        artifact = self.write_artifact(body or f"synthetic {stage.lower()} evidence", principal) if body is not None or state in {GateState.PASS, GateState.PENDING} else None
        inputs = [self.source_snapshot.snapshot_id] if stage in {"REPLAY", "GOLDEN", "SHADOW", "QUALIFY"} else []
        aggregate = self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity)
        prior_stages = {"GOLDEN": ("REPLAY",), "SHADOW": ("REPLAY", "GOLDEN"), "QUALIFY": ("REPLAY", "GOLDEN", "SHADOW")}.get(stage, ())
        for prior_stage in prior_stages:
            prior = next((item for item in reversed(aggregate.state["gate_revisions"]) if item["stage_id"] == prior_stage), None)
            if prior is not None:
                inputs.append(prior["revision_id"])
        return self.service.record_gate_evidence(
            self.context(command_id or f"record-{stage.lower()}-{version}", version, principal),
            self.identity,
            stage_id=stage,
            state=state,
            policy_basis_id=policy_id or f"synthetic-{stage.lower()}-policy",
            policy_basis_version="1.0.0",
            engine_identity=f"synthetic-{stage.lower()}-harness.v1",
            input_identities=tuple(inputs),
            artifact_reference=artifact,
            known_at=now,
            published_at=now,
            expires_at=now + timedelta(seconds=expires_in) if state in {GateState.PASS, GateState.PENDING, GateState.NOT_APPLICABLE} else None,
            requalification_policy_id=f"synthetic-{stage.lower()}-requalification",
            requalification_policy_version="1.0.0",
            reason_code="SYNTHETIC_MAPPING_AMBIGUOUS" if state is GateState.BLOCKED else None,
        )

    def complete_workspace(self):
        view = self.open_workspace()
        blocked_map = self.record("DISCOVER_MAP", view.version, state=GateState.BLOCKED)
        with self.assertRaises(ValidationFailureError):
            self.service.record_current_data_reality(self.context("data-reality-blocked", blocked_map.aggregate_version), self.identity)
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.record("DISCOVER_MAP", view.version, body="synthetic canonical role mapping evidence")
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.service.record_current_data_reality(self.context("data-reality-pass", view.version), self.identity)
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        pending_replay = self.record("REPLAY", view.version, state=GateState.PENDING, body="synthetic bounded replay artifact")
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.assertEqual(next(gate for gate in view.gates if gate.stage_id == "REPLAY").state, GateState.PENDING)
        self.assertFalse(view.promotion_ready)
        self.assertIn("REPLAY:PENDING", view.promotion_blockers)
        self.record("REPLAY", view.version, body="synthetic replay artifact")
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.record("GOLDEN", view.version, state=GateState.PENDING, body="synthetic golden artifact")
        aggregate = self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity)
        golden = next(item for item in aggregate.state["gate_revisions"] if item["stage_id"] == "GOLDEN")
        self.current.set_principal(self.reviewer)
        self.service.adjudicate_gate(
            self.context("judge-golden", aggregate.version, self.reviewer),
            self.identity,
            stage_id="GOLDEN",
            expected_evidence_revision_id=golden["revision_id"],
            decision=GateState.PASS,
            judgment_basis_id="synthetic-independent-golden-review",
            judgment_basis_version="1.0.0",
            reason_code="INDEPENDENT_REVIEW_PASS",
        )
        view = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity)
        self.service.record_gate_evidence(
            self.context("shadow-policy-na", view.version, self.reviewer),
            self.identity,
            stage_id="SHADOW",
            state=GateState.NOT_APPLICABLE,
            policy_basis_id="synthetic-shadow-not-applicable-policy",
            policy_basis_version="1.0.0",
            engine_identity="synthetic-policy-authority",
            input_identities=(
                self.source_snapshot.snapshot_id,
                next(gate.revision_id for gate in view.gates if gate.stage_id == "REPLAY"),
                next(gate.revision_id for gate in view.gates if gate.stage_id == "GOLDEN"),
            ),
            expires_at=self.clock.now + timedelta(seconds=3600),
            requalification_policy_id="synthetic-shadow-requalification",
            requalification_policy_version="1.0.0",
            reason_code="POLICY_AUTHORIZED_NOT_APPLICABLE",
        )
        view = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity)
        self.current.set_principal(self.engineer)
        self.record("QUALIFY", view.version, state=GateState.PENDING, body="synthetic qualification evidence")
        aggregate = self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity)
        qualification = next(item for item in aggregate.state["gate_revisions"] if item["stage_id"] == "QUALIFY")
        self.current.set_principal(self.reviewer)
        self.service.adjudicate_gate(
            self.context("judge-qualification", aggregate.version, self.reviewer),
            self.identity,
            stage_id="QUALIFY",
            expected_evidence_revision_id=qualification["revision_id"],
            decision=GateState.PASS,
            judgment_basis_id="synthetic-independent-qualification-review",
            judgment_basis_version="1.0.0",
            reason_code="INDEPENDENT_REVIEW_PASS",
        )
        view = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity)
        return view

    def test_complete_synthetic_workspace_promotion_and_historical_invalidation(self):
        view = self.complete_workspace()
        self.assertTrue(view.synthetic)
        self.assertTrue(view.promotion_ready)
        self.assertEqual([gate.stage_id for gate in view.gates], list(FAMILY_CENTER_STAGE_ORDER[:-1]))
        self.assertEqual(next(gate for gate in view.gates if gate.stage_id == "SHADOW").state, GateState.NOT_APPLICABLE)
        self.assertTrue(all(gate.evidence_sha256 for gate in view.gates if gate.state is GateState.PASS))
        gate_ids = tuple(next(gate.revision_id for gate in view.gates if gate.stage_id == stage) for stage in self.identity.required_stages)
        result = self.service.promote(
            self.context("promote-synthetic-family", view.version, self.reviewer),
            self.identity,
            gate_identity_set=gate_ids,
            source_reality_identity=view.source_reality.reality_identity,
        )
        self.assertFalse(result.g12_production_approval)
        self.assertEqual(result.qualification_kind, "FAMILY_CENTER_QUALIFICATION")
        promoted = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity)
        self.assertEqual(promoted.promotions[-1].state, "CURRENT")
        self.assertIn("NOT G12", promoted.promotions[-1].label)
        self.source.set_state(self.binding, SourceCapabilityState.STALE)
        invalidated = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity)
        self.assertFalse(invalidated.promotion_ready)
        self.assertEqual(invalidated.promotions[-1].state, "STALE")
        self.assertEqual(len(self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity).state["promotion_records"]), 1)

    def test_o2_receipt_replay_conflict_append_only_expiry_and_independent_judgment(self):
        view = self.open_workspace()
        blocked = self.record("DISCOVER_MAP", view.version, state=GateState.BLOCKED, command_id="discover-blocked")
        with self.assertRaises(ValidationFailureError):
            self.service.record_current_data_reality(self.context("reality-before-map", blocked.aggregate_version), self.identity)
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        fixed_now = self.clock.now
        pass_args = {
            "stage_id": "DISCOVER_MAP",
            "state": GateState.PASS,
            "policy_basis_id": "synthetic-map-policy",
            "policy_basis_version": "1.0.0",
            "engine_identity": "synthetic-mapping-review.v1",
            "artifact_reference": self.write_artifact("synthetic map artifact"),
            "known_at": fixed_now,
            "published_at": fixed_now,
            "expires_at": fixed_now + timedelta(seconds=5),
            "requalification_policy_id": "synthetic-map-requalify",
            "requalification_policy_version": "1.0.0",
        }
        context = self.context("map-pass-receipt", view.version)
        first = self.service.record_gate_evidence(context, self.identity, **pass_args)
        replay = self.service.record_gate_evidence(context, self.identity, **pass_args)
        self.assertEqual(first.result_identity, replay.result_identity)
        self.clock.now -= timedelta(seconds=1)
        replay_after_server_clock_rollback = self.service.record_gate_evidence(context, self.identity, **pass_args)
        self.assertEqual(first.result_identity, replay_after_server_clock_rollback.result_identity)
        self.clock.now = fixed_now
        with self.assertRaises(IdempotencyConflictError):
            self.service.record_gate_evidence(context, self.identity, **{**pass_args, "state": GateState.FAIL})
        aggregate = self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity)
        history_before = tuple(aggregate.state["gate_revisions"])
        self.assertEqual(len(history_before), 2)
        self.clock.now += timedelta(seconds=6)
        expired = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.assertEqual(next(gate for gate in expired.gates if gate.stage_id == "DISCOVER_MAP").state, GateState.EXPIRED)
        self.assertFalse(expired.promotion_ready)
        self.assertEqual(tuple(self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity).state["gate_revisions"]), history_before)

    def test_workspace_identity_and_dependency_scoped_invalidation(self):
        view = self.complete_workspace()
        self.assertTrue(view.promotion_ready)
        gate_ids = tuple(next(gate.revision_id for gate in view.gates if gate.stage_id == stage) for stage in self.identity.required_stages)
        self.service.promote(
            self.context("promote-before-release-change", view.version, self.reviewer),
            self.identity,
            gate_identity_set=gate_ids,
            source_reality_identity=view.source_reality.reality_identity,
        )
        release = replace(self.identity, release_id="synthetic-release-2")
        release_view = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity, current_identity=release)
        release_states = {gate.stage_id: gate.state for gate in release_view.gates}
        self.assertEqual(release_states["DISCOVER_MAP"], GateState.PASS)
        self.assertEqual(release_states["DATA_REALITY"], GateState.PASS)
        self.assertEqual(release_states["REPLAY"], GateState.STALE)
        self.assertEqual(release_states["QUALIFY"], GateState.STALE)
        self.assertFalse(release_view.promotion_ready)
        self.assertEqual(release_view.promotions[-1].state, "STALE")
        changed_binding = replace(self.binding, mapping_version="2.0.0", mapping_hash="d" * 64)
        mapping_identity = replace(self.identity, source_binding=changed_binding)
        mapping_view = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity, current_identity=mapping_identity)
        self.assertEqual({gate.state for gate in mapping_view.gates if gate.stage_id != "PROMOTE"}, {GateState.STALE, GateState.BLOCKED})
        policy_identity = replace(self.identity, policy_configuration_identity="e" * 64)
        policy_view = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity, current_identity=policy_identity)
        self.assertNotEqual(policy_identity.identity, self.identity.identity)
        self.assertEqual(next(gate for gate in policy_view.gates if gate.stage_id == "DISCOVER_MAP").state, GateState.PASS)
        golden_policy = replace(
            self.identity,
            stage_policy_identities=tuple(
                (stage, "f" * 64 if stage == "GOLDEN" else policy)
                for stage, policy in self.identity.stage_policy_identities
            ),
        )
        scoped_policy_view = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity, current_identity=golden_policy)
        scoped_policy_states = {gate.stage_id: gate.state for gate in scoped_policy_view.gates}
        self.assertEqual(scoped_policy_states["REPLAY"], GateState.PASS)
        self.assertEqual(scoped_policy_states["GOLDEN"], GateState.STALE)
        self.assertEqual(scoped_policy_states["SHADOW"], GateState.STALE)
        self.assertEqual(scoped_policy_states["QUALIFY"], GateState.STALE)
        contract_identity = replace(self.identity, provider_abi_version="1.1.0")
        contract_view = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity, current_identity=contract_identity)
        self.assertTrue(all(gate.state in {GateState.STALE, GateState.BLOCKED} for gate in contract_view.gates if gate.revision_id))
        self.assertNotEqual(self.identity.identity, release.identity)
        self.assertNotEqual(self.identity.identity, mapping_identity.identity)
        self.assertNotEqual(self.identity.identity, policy_identity.identity)

    def test_workspace_identity_is_deterministic_across_restart(self):
        opened = self.open_workspace()
        workspace_id = opened.workspace_id
        self.store.close()
        self.store = SQLiteReferenceTransactionAdapter(self.path)
        self.service = FamilyCenterService(self.store, self.current, self.source, self.artifacts, clock=self.clock)
        after_restart = self.service.get_workspace(self.engineer, self.scope, workspace_id)
        self.assertEqual(after_restart.workspace_id, self.identity.identity)
        self.assertEqual(after_restart.workspace_identity_id, self.identity.identity)

    def test_missing_o4_capability_blocks_data_reality(self):
        self.source.capabilities.clear()
        self.source.snapshots.clear()
        view = self.open_workspace()
        self.record("DISCOVER_MAP", view.version, body="synthetic discover mapping")
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        result = self.service.record_current_data_reality(self.context("missing-o4-reality", view.version), self.identity)
        blocked = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.assertEqual(blocked.source_reality.capability_state, "UNAVAILABLE")
        reality_gate = next(gate for gate in blocked.gates if gate.stage_id == "DATA_REALITY")
        self.assertEqual(reality_gate.state, GateState.BLOCKED)
        self.assertEqual(reality_gate.invalidation_reason, "CAPABILITY_RECORD_MISSING")
        self.assertFalse(blocked.promotion_ready)
        self.assertFalse(result.g12_production_approval)

    def test_current_authorization_precedes_workspace_existence_lookup(self):
        denied = Principal("synthetic-denied", (), (self.scope,), 1, 1)
        authority = MutableCurrentAuthorizationAuthority(denied)
        spy = _CountingStore(self.store)
        denied_service = FamilyCenterService(spy, authority, self.source, self.artifacts, clock=self.clock)
        with self.assertRaises(AuthorizationDeniedError):
            denied_service.get_workspace(denied, self.scope, "missing-workspace-id")
        self.assertEqual(spy.transactions, 0)

    def test_artifact_bearing_pass_is_required(self):
        view = self.open_workspace()
        with self.assertRaises(ValidationFailureError):
            self.service.record_gate_evidence(
                self.context("missing-artifact-pass", view.version), self.identity,
                stage_id="DISCOVER_MAP", state=GateState.PASS,
                policy_basis_id="synthetic-map", policy_basis_version="1.0.0",
                engine_identity="synthetic-map.v1", expires_at=self.clock.now + timedelta(hours=1),
                requalification_policy_id="synthetic-requalify", requalification_policy_version="1.0.0",
            )

    def test_record_gate_evidence_rejects_future_known_at(self):
        view = self.open_workspace()
        now = self.clock.now
        with self.assertRaisesRegex(ValidationFailureError, "known_at cannot be later than Family Center server time"):
            self.service.record_gate_evidence(
                self.context("future-known-at", view.version), self.identity,
                stage_id="DISCOVER_MAP", state=GateState.FAIL,
                policy_basis_id="synthetic-map", policy_basis_version="1.0.0",
                engine_identity="synthetic-map.v1", known_at=now + timedelta(seconds=1),
                published_at=now, requalification_policy_id="synthetic-requalify",
                requalification_policy_version="1.0.0",
            )
        self.assertEqual(self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity).state["gate_revisions"], [])

    def test_record_gate_evidence_rejects_future_published_at(self):
        view = self.open_workspace()
        now = self.clock.now
        with self.assertRaisesRegex(ValidationFailureError, "published_at cannot be later than Family Center server time"):
            self.service.record_gate_evidence(
                self.context("future-published-at", view.version), self.identity,
                stage_id="DISCOVER_MAP", state=GateState.FAIL,
                policy_basis_id="synthetic-map", policy_basis_version="1.0.0",
                engine_identity="synthetic-map.v1", known_at=now,
                published_at=now + timedelta(seconds=1), requalification_policy_id="synthetic-requalify",
                requalification_policy_version="1.0.0",
            )
        self.assertEqual(self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity).state["gate_revisions"], [])

    def test_record_gate_evidence_still_rejects_published_before_known(self):
        view = self.open_workspace()
        now = self.clock.now
        with self.assertRaisesRegex(ValidationFailureError, "published_at cannot precede known_at"):
            self.service.record_gate_evidence(
                self.context("published-before-known", view.version), self.identity,
                stage_id="DISCOVER_MAP", state=GateState.FAIL,
                policy_basis_id="synthetic-map", policy_basis_version="1.0.0",
                engine_identity="synthetic-map.v1", known_at=now - timedelta(seconds=1),
                published_at=now - timedelta(seconds=2), requalification_policy_id="synthetic-requalify",
                requalification_policy_version="1.0.0",
            )

    def test_future_gate_revision_blocks_view_readiness_and_promotion_with_reason(self):
        ready = self.complete_workspace()
        self.assertTrue(ready.promotion_ready)
        future_time = self.clock.now + timedelta(seconds=1)
        self._inject_gate_times("DISCOVER_MAP", known_at=future_time, published_at=future_time)

        blocked = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity)
        gate = next(item for item in blocked.gates if item.stage_id == "DISCOVER_MAP")
        self.assertEqual(gate.state, GateState.BLOCKED)
        self.assertEqual(gate.invalidation_reason, "FUTURE_EVIDENCE_TIME")
        self.assertFalse(blocked.promotion_ready)
        self.assertIn("DISCOVER_MAP:FUTURE_EVIDENCE_TIME", blocked.promotion_blockers)
        gate_ids = tuple(next(item.revision_id for item in blocked.gates if item.stage_id == stage) for stage in self.identity.required_stages)
        with self.assertRaisesRegex(ValidationFailureError, "DISCOVER_MAP:FUTURE_EVIDENCE_TIME"):
            self.service.promote(
                self.context("promote-future-gate", blocked.version, self.reviewer), self.identity,
                gate_identity_set=gate_ids, source_reality_identity=blocked.source_reality.reality_identity,
            )

    def test_future_policy_authorized_not_applicable_cannot_satisfy_required_stage(self):
        self.complete_workspace()
        future_time = self.clock.now + timedelta(seconds=1)
        self._inject_gate_times("SHADOW", known_at=self.clock.now, published_at=future_time)

        blocked = self.service.get_workspace(self.reviewer, self.scope, self.identity.identity)
        gate = next(item for item in blocked.gates if item.stage_id == "SHADOW")
        self.assertEqual(gate.state, GateState.BLOCKED)
        self.assertEqual(gate.invalidation_reason, "FUTURE_EVIDENCE_TIME")
        self.assertFalse(blocked.promotion_ready)
        self.assertIn("SHADOW:FUTURE_EVIDENCE_TIME", blocked.promotion_blockers)
        gate_ids = tuple(next(item.revision_id for item in blocked.gates if item.stage_id == stage) for stage in self.identity.required_stages)
        with self.assertRaisesRegex(ValidationFailureError, "SHADOW:FUTURE_EVIDENCE_TIME"):
            self.service.promote(
                self.context("promote-future-not-applicable", blocked.version, self.reviewer), self.identity,
                gate_identity_set=gate_ids, source_reality_identity=blocked.source_reality.reality_identity,
            )

    def test_future_predecessor_evidence_cannot_unlock_data_reality(self):
        view = self.open_workspace()
        self.record("DISCOVER_MAP", view.version, body="synthetic map")
        self._inject_gate_times("DISCOVER_MAP", known_at=self.clock.now + timedelta(seconds=1))
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        with self.assertRaisesRegex(ValidationFailureError, "FUTURE_EVIDENCE_TIME predecessor evidence: DISCOVER_MAP"):
            self.service.record_current_data_reality(self.context("future-predecessor-reality", view.version), self.identity)

    def test_independent_judgment_rejects_future_dated_prior_revision(self):
        aggregate, golden = self._prepare_pending_golden()
        future_time = self.clock.now + timedelta(seconds=1)
        self._inject_gate_times("GOLDEN", known_at=self.clock.now, published_at=future_time)
        self.current.set_principal(self.reviewer)
        with self.assertRaisesRegex(ValidationFailureError, "FUTURE_EVIDENCE_TIME"):
            self.service.adjudicate_gate(
                self.context("judge-future-golden", aggregate.version, self.reviewer), self.identity,
                stage_id="GOLDEN", expected_evidence_revision_id=golden["revision_id"],
                decision=GateState.PASS, judgment_basis_id="synthetic-independent-review",
                judgment_basis_version="1.0.0", reason_code="INDEPENDENT_REVIEW_PASS",
            )
        after = self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity)
        self.assertEqual(len(after.state["gate_revisions"]), len(aggregate.state["gate_revisions"]))

    def test_historical_gate_timestamp_is_reeligible_when_server_clock_reaches_it(self):
        view = self.open_workspace()
        revision_time = self.clock.now
        self.record("DISCOVER_MAP", view.version, body="synthetic map")
        aggregate_before = self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity)
        revision_before = next(item for item in aggregate_before.state["gate_revisions"] if item["stage_id"] == "DISCOVER_MAP")

        self.clock.now = revision_time - timedelta(seconds=1)
        behind = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        gate = next(item for item in behind.gates if item.stage_id == "DISCOVER_MAP")
        self.assertEqual(gate.state, GateState.BLOCKED)
        self.assertEqual(gate.invalidation_reason, "FUTURE_EVIDENCE_TIME")

        self.clock.now = revision_time
        reached = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        gate = next(item for item in reached.gates if item.stage_id == "DISCOVER_MAP")
        self.assertEqual(gate.state, GateState.PASS)
        self.assertEqual(gate.evidence_status, "AVAILABLE")
        aggregate_after = self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity)
        revision_after = next(item for item in aggregate_after.state["gate_revisions"] if item["stage_id"] == "DISCOVER_MAP")
        self.assertEqual(revision_after, revision_before)

    def test_future_o4_source_fact_blocks_data_reality_instead_of_appearing_fresh(self):
        capability = self.source.get_capability(self.engineer, self.binding)
        self.source.capabilities[_SourceRepository._key(self.binding)] = replace(
            capability, latest_available_at=self.clock.now + timedelta(seconds=1),
        )
        reality = self.service.current_source_reality(self.engineer, self.scope, self.identity)
        self.assertEqual(reality.state, "BLOCKED")
        self.assertEqual(reality.reason_code, "FUTURE_SOURCE_REALITY_TIME")

    def test_independent_judgment_cannot_be_self_satisfied(self):
        view = self.open_workspace()
        self.record("DISCOVER_MAP", view.version, body="synthetic map")
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.service.record_current_data_reality(self.context("data-reality-for-golden", view.version), self.identity)
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.record("REPLAY", view.version, body="synthetic replay")
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        pending = self.record("GOLDEN", view.version, state=GateState.PENDING, body="synthetic golden")
        aggregate = self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity)
        golden = next(item for item in aggregate.state["gate_revisions"] if item["stage_id"] == "GOLDEN")
        with self.assertRaises(AuthorizationDeniedError):
            self.service.adjudicate_gate(
                self.context("self-judge-golden", aggregate.version, self.engineer), self.identity,
                stage_id="GOLDEN", expected_evidence_revision_id=golden["revision_id"],
                decision=GateState.PASS, judgment_basis_id="self", judgment_basis_version="1.0.0",
                reason_code="SELF_JUDGMENT",
            )
        self.assertEqual(pending.status, "COMMITTED")

    def test_independent_judgment_cannot_turn_stale_evidence_into_current_pass(self):
        view = self.open_workspace()
        self.record("DISCOVER_MAP", view.version, body="synthetic map")
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.service.record_current_data_reality(self.context("data-reality-before-stale-judgment", view.version), self.identity)
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.record("REPLAY", view.version, body="synthetic replay")
        view = self.service.get_workspace(self.engineer, self.scope, self.identity.identity)
        self.record("GOLDEN", view.version, state=GateState.PENDING, body="synthetic golden")
        aggregate = self.store.get_aggregate(self.scope, "family_qualification_workspace", self.identity.identity)
        golden = next(item for item in aggregate.state["gate_revisions"] if item["stage_id"] == "GOLDEN")
        self.source.set_state(self.binding, SourceCapabilityState.STALE)
        self.current.set_principal(self.reviewer)
        with self.assertRaises(VersionConflictError):
            self.service.adjudicate_gate(
                self.context("judge-stale-golden", aggregate.version, self.reviewer), self.identity,
                stage_id="GOLDEN", expected_evidence_revision_id=golden["revision_id"],
                decision=GateState.PASS, judgment_basis_id="synthetic-independent-review",
                judgment_basis_version="1.0.0", reason_code="REVIEWED_AFTER_SOURCE_CHANGE",
            )


if __name__ == "__main__":
    unittest.main()
