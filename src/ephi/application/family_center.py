"""Durable Family Center qualification workspaces on the existing O2 authority.

This module stores only typed identity, state and immutable evidence references.
Artifact bytes, jobs, source reality, authorization and command receipts remain
with their existing authorities.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import re
from typing import Any

from .artifacts import ArtifactService, ScopedArtifactReference
from .context import AccessScope, CommandContext, CurrentAuthorizationAuthority, Principal
from .errors import (
    AggregateNotFoundError,
    ArtifactError,
    AuthorizationDeniedError,
    ValidationFailureError,
    VersionConflictError,
)
from .source_ingress import (
    SOURCE_READ_CAPABILITY,
    MetrologySourceBinding,
    SourceCapabilityRecord,
    SourceCapabilityState,
    SourceSnapshotRecord,
    SourceSnapshotStatus,
)
from .storage import AggregateSnapshot, CommandStorage
from .transactions import CommandResult, VersionedAggregateCommandExecutor


FAMILY_CENTER_READ = "ephi.family_qualification.read"
FAMILY_CENTER_WRITE = "ephi.family_qualification.evidence.write"
FAMILY_CENTER_JUDGE = "ephi.family_qualification.judge"
FAMILY_CENTER_POLICY = "ephi.family_qualification.policy"
FAMILY_CENTER_PROMOTE = "ephi.family_qualification.promote"
FAMILY_CENTER_ARTIFACT_READ = "ephi.family_qualification.artifact.read"
AGGREGATE_TYPE = "family_qualification_workspace"
STAGE_ORDER = (
    "DISCOVER_MAP",
    "DATA_REALITY",
    "REPLAY",
    "GOLDEN",
    "SHADOW",
    "QUALIFY",
    "PROMOTE",
)
GATE_STAGES = STAGE_ORDER[:-1]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GateState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical identity")
    if len(value.encode("utf-8")) > 512:
        raise ValidationFailureError(f"{field} is too long")
    return value


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _time(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailureError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _future_gate_evidence_reason(item: Mapping[str, Any], now: datetime) -> str | None:
    for field in ("known_at", "published_at"):
        value = item.get(field)
        if value is None:
            continue
        try:
            timestamp = _time(datetime.fromisoformat(value), field)
        except (TypeError, ValueError, ValidationFailureError):
            return "INVALID_EVIDENCE_TIME"
        if timestamp > now:
            return "FUTURE_EVIDENCE_TIME"
    return None


def _gate_state(value: object) -> GateState:
    try:
        return GateState(value)
    except (TypeError, ValueError) as exc:
        raise ValidationFailureError("qualification gate state is unsupported") from exc


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    category: str
    contract_id: str
    version: str

    def __post_init__(self) -> None:
        for field in ("category", "contract_id", "version"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))

    def as_dict(self) -> dict[str, str]:
        return {"category": self.category, "contract_id": self.contract_id, "version": self.version}


@dataclass(frozen=True, slots=True)
class QualificationWorkspaceIdentity:
    """Exact, deterministic key for one family/capability/release workspace."""

    scope: AccessScope
    family_id: str
    family_context_version: str
    target_identity: str
    context_identity: str
    unit_identity: str
    characteristic_identity: str
    capability_id: str
    product_id: str
    release_id: str
    provider_abi_id: str
    provider_abi_version: str
    provider_contracts: tuple[ProviderIdentity, ...]
    policy_schema_id: str
    policy_schema_version: str
    policy_configuration_version: str
    policy_configuration_identity: str
    source_binding: MetrologySourceBinding
    runtime_environment_class: str
    postgresql_major_version: int
    runtime_contract_version: str
    stage_policy_identities: tuple[tuple[str, str], ...]
    required_stages: tuple[str, ...] = GATE_STAGES
    not_applicable_stages: tuple[str, ...] = ()
    independent_judgment_stages: tuple[str, ...] = ()
    synthetic_fixture: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AccessScope) or not isinstance(self.source_binding, MetrologySourceBinding):
            raise TypeError("workspace identity requires exact scope and O4 source binding values")
        for field in (
            "family_id", "family_context_version", "target_identity", "context_identity", "unit_identity",
            "characteristic_identity", "capability_id", "product_id", "release_id", "provider_abi_id",
            "provider_abi_version", "policy_schema_id", "policy_schema_version", "policy_configuration_version",
            "runtime_environment_class", "runtime_contract_version",
        ):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        for field in ("policy_configuration_identity",):
            value = getattr(self, field)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValidationFailureError(f"{field} must be a canonical SHA-256")
        if isinstance(self.postgresql_major_version, bool) or not isinstance(self.postgresql_major_version, int) or self.postgresql_major_version < 1:
            raise ValidationFailureError("postgresql_major_version must be positive")
        if self.scope != self.source_binding.scope:
            raise ValidationFailureError("workspace scope must equal the exact O4 source binding scope")
        if self.family_id != self.source_binding.family_id or self.capability_id != self.source_binding.capability_id:
            raise ValidationFailureError("workspace family/capability must equal the exact O4 source binding")
        contracts = tuple(sorted(self.provider_contracts, key=lambda item: item.category))
        if not contracts or any(not isinstance(item, ProviderIdentity) for item in contracts):
            raise ValidationFailureError("provider contract identities are required")
        if len({item.category for item in contracts}) != len(contracts):
            raise ValidationFailureError("provider contract categories must be unique")
        object.__setattr__(self, "provider_contracts", contracts)
        policies = tuple(sorted(self.stage_policy_identities))
        if any(stage not in GATE_STAGES or not isinstance(identity, str) or not identity for stage, identity in policies):
            raise ValidationFailureError("stage policy identities are invalid")
        if len({stage for stage, _ in policies}) != len(policies):
            raise ValidationFailureError("stage policy identities must be unique")
        object.__setattr__(self, "stage_policy_identities", policies)
        for field in ("required_stages", "not_applicable_stages", "independent_judgment_stages"):
            values = getattr(self, field)
            if not isinstance(values, (tuple, list, frozenset)):
                raise ValidationFailureError(f"{field} must be a stage sequence")
            if any(value not in GATE_STAGES for value in values):
                raise ValidationFailureError(f"{field} contains an unsupported stage")
            normalized = tuple(sorted(set(values), key=STAGE_ORDER.index))
            if len(normalized) != len(values):
                raise ValidationFailureError(f"{field} contains duplicate stages")
            object.__setattr__(self, field, normalized)
        if not {"DISCOVER_MAP", "DATA_REALITY"} <= set(self.required_stages):
            raise ValidationFailureError("Discover/Map and Data Reality are required qualification gates")
        if "DATA_REALITY" in self.not_applicable_stages:
            raise ValidationFailureError("Data Reality cannot be NOT_APPLICABLE")
        if "DATA_REALITY" in self.independent_judgment_stages:
            raise ValidationFailureError("Data Reality is derived from O4 and cannot use independent evidence judgment")
        if set(self.not_applicable_stages) & set(self.independent_judgment_stages):
            raise ValidationFailureError("a NOT_APPLICABLE stage cannot also require evidence judgment")
        if not set(self.not_applicable_stages) <= set(self.required_stages):
            raise ValidationFailureError("NOT_APPLICABLE must be explicitly allowed for a required stage")
        if not set(self.independent_judgment_stages) <= set(self.required_stages):
            raise ValidationFailureError("independent judgment must be configured for a required stage")
        if not isinstance(self.synthetic_fixture, bool):
            raise ValidationFailureError("synthetic_fixture must be a boolean")

    @property
    def source_binding_identity(self) -> str:
        return _hash(self.source_binding.as_dict())

    @property
    def identity(self) -> str:
        return _hash(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.as_dict(),
            "family_id": self.family_id,
            "family_context_version": self.family_context_version,
            "target_identity": self.target_identity,
            "context_identity": self.context_identity,
            "unit_identity": self.unit_identity,
            "characteristic_identity": self.characteristic_identity,
            "capability_id": self.capability_id,
            "product_id": self.product_id,
            "release_id": self.release_id,
            "provider_abi": {"id": self.provider_abi_id, "version": self.provider_abi_version},
            "provider_contracts": [item.as_dict() for item in self.provider_contracts],
            "policy": {
                "schema_id": self.policy_schema_id,
                "schema_version": self.policy_schema_version,
                "configuration_version": self.policy_configuration_version,
                "configuration_identity": self.policy_configuration_identity,
            },
            "source_binding": self.source_binding.as_dict(),
            "runtime": {
                "environment_class": self.runtime_environment_class,
                "postgresql_major_version": self.postgresql_major_version,
                "contract_version": self.runtime_contract_version,
            },
            "stage_policy_identities": {stage: value for stage, value in self.stage_policy_identities},
            "required_stages": list(self.required_stages),
            "not_applicable_stages": list(self.not_applicable_stages),
            "independent_judgment_stages": list(self.independent_judgment_stages),
            "synthetic_fixture": self.synthetic_fixture,
        }

    def dependency_fingerprint(self, stage_id: str) -> str:
        """Hash only the declared dependencies for one stage."""

        if stage_id not in GATE_STAGES:
            raise ValidationFailureError("stage is not a qualification gate")
        position = GATE_STAGES.index(stage_id)
        value: dict[str, object] = {
            "scope": self.scope.as_dict(),
            "family_id": self.family_id,
            "family_context_version": self.family_context_version,
            "target_identity": self.target_identity,
            "context_identity": self.context_identity,
            "unit_identity": self.unit_identity,
            "characteristic_identity": self.characteristic_identity,
            "capability_id": self.capability_id,
            "source_binding": self.source_binding.as_dict(),
            "provider_abi": [self.provider_abi_id, self.provider_abi_version],
            "provider_contracts": [item.as_dict() for item in self.provider_contracts],
            "policy_schema": [self.policy_schema_id, self.policy_schema_version, self.policy_configuration_version],
            "gate_requirements": {
                "required": list(self.required_stages),
                "not_applicable": list(self.not_applicable_stages),
                "independent_judgment": list(self.independent_judgment_stages),
                "synthetic_fixture": self.synthetic_fixture,
            },
        }
        if position >= GATE_STAGES.index("REPLAY"):
            value["target_release"] = [self.product_id, self.release_id]
        if position >= GATE_STAGES.index("DATA_REALITY"):
            value["runtime"] = [self.runtime_environment_class, self.postgresql_major_version, self.runtime_contract_version]
        if stage_id in {"REPLAY", "GOLDEN", "SHADOW", "QUALIFY"}:
            value["stage_policy_identity"] = dict(self.stage_policy_identities).get(stage_id, "UNBOUND_POLICY")
        if stage_id in {"GOLDEN", "SHADOW", "QUALIFY"}:
            previous = GATE_STAGES[:position]
            value["prior_stage_policy_identities"] = {
                item: dict(self.stage_policy_identities).get(item, "UNBOUND_POLICY")
                for item in previous if item in {"REPLAY", "GOLDEN", "SHADOW", "QUALIFY"}
            }
        return _hash(value)


@dataclass(frozen=True, slots=True)
class MappingFacts:
    provider_id: str
    source_id: str
    schema_id: str
    mapping_version: str
    mapping_hash: str
    canonical_roles: tuple[str, ...]
    unit: str
    timestamp_semantics: str
    availability_semantics: str
    reference_population_id: str | None
    comparable_population_id: str | None


@dataclass(frozen=True, slots=True)
class SourceReality:
    capability_state: str
    state: str
    reason_code: str
    checked_at: datetime
    freshness_age_seconds: int
    source_age_seconds: int | None = None
    latest_snapshot_id: str | None = None
    latest_manifest_hash: str | None = None
    latest_available_at: datetime | None = None
    latest_event_at: datetime | None = None
    artifact_reference: ScopedArtifactReference | None = None
    reality_identity: str = ""


@dataclass(frozen=True, slots=True)
class GateView:
    stage_id: str
    state: GateState
    revision_id: str | None = None
    revision: int | None = None
    policy_basis_id: str | None = None
    policy_basis_version: str | None = None
    evidence_sha256: str | None = None
    evidence_byte_size: int | None = None
    evidence_status: str = "NONE"
    known_at: datetime | None = None
    expires_at: datetime | None = None
    invalidation_reason: str | None = None
    judgment_identity: str | None = None
    job_id: str | None = None
    job_status: str | None = None
    evidence_author: str | None = None
    engine_identity: str | None = None
    input_identities: tuple[str, ...] = ()
    published_at: datetime | None = None
    stage_policy_identity: str | None = None
    requalification_policy_id: str | None = None
    requalification_policy_version: str | None = None
    judgment_basis_id: str | None = None
    judgment_basis_version: str | None = None
    source_reality_identity: str | None = None


@dataclass(frozen=True, slots=True)
class PromotionView:
    promotion_id: str
    state: str
    workspace_revision: int
    gate_identity_set: tuple[str, ...]
    promoted_by: str
    promoted_at: datetime
    invalidation_reason: str | None = None
    label: str = "GENERIC FAMILY CENTER QUALIFICATION — NOT G12 / NOT PRODUCTION APPROVAL"


@dataclass(frozen=True, slots=True)
class FamilyCenterMutationResult:
    """Safe projection of an O2 command result without aggregate internals."""

    status: str
    result_identity: str
    workspace_id: str
    aggregate_version: int
    qualification_kind: str = "FAMILY_CENTER_QUALIFICATION"
    g12_production_approval: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceView:
    workspace_id: str
    workspace_identity_id: str
    family_id: str
    family_context_version: str
    capability_id: str
    product_id: str
    release_id: str
    context_identity: str
    unit_identity: str
    version: int
    gates: tuple[GateView, ...]
    promotion_ready: bool
    promotion_blockers: tuple[str, ...]
    promotions: tuple[PromotionView, ...]
    source_reality: SourceReality
    mapping_facts: MappingFacts
    current_identity_match: bool
    synthetic: bool = False
    label: str = "GENERIC FAMILY/CAPABILITY QUALIFICATION — NOT G12 / NOT PRODUCTION APPROVAL"


def _artifact_dict(reference: ScopedArtifactReference | None) -> dict[str, object] | None:
    if reference is None:
        return None
    return {
        "scope": reference.scope.as_dict(),
        "sha256": reference.content.sha256,
        "byte_size": reference.content.byte_size,
    }


def _artifact_from(value: object) -> ScopedArtifactReference | None:
    if value is None:
        return None
    from .artifacts import ArtifactContentIdentity

    if not isinstance(value, Mapping):
        raise ValidationFailureError("stored evidence artifact identity is invalid")
    raw_scope = value.get("scope")
    if not isinstance(raw_scope, Mapping):
        raise ValidationFailureError("stored evidence scope identity is invalid")
    scope = AccessScope(
        raw_scope["scope_id"], raw_scope.get("site_id"), raw_scope.get("area_id"),
        raw_scope.get("family_id"), tuple(raw_scope.get("project_ids", ())),
    )
    return ScopedArtifactReference(scope, ArtifactContentIdentity(value["sha256"], value["byte_size"]))


def _source_record_identity(
    record: SourceSnapshotRecord | None,
    capability: SourceCapabilityRecord,
    effective_state: str,
) -> str:
    return _hash({
        "binding": capability.binding.as_dict(),
        "state": capability.state.value,
        "effective_state": effective_state,
        "freshness_age_seconds": capability.freshness_age_seconds,
        "latest_snapshot_id": capability.latest_snapshot_id,
        "latest_available_at": capability.latest_available_at.isoformat() if capability.latest_available_at else None,
        "snapshot_manifest_hash": record.manifest_hash if record else None,
        "snapshot_status": record.status.value if record else None,
        "artifact_sha256": record.artifact_reference.content.sha256 if record else None,
    })


class FamilyCenterService:
    """Family qualification control plane backed by O2/O4/O8/O2 artifacts/jobs."""

    def __init__(
        self,
        store: CommandStorage,
        current_authorization: CurrentAuthorizationAuthority,
        source_repository: object,
        artifact_service: ArtifactService,
        worker_store: object | None = None,
        *,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(current_authorization, CurrentAuthorizationAuthority):
            raise TypeError("Family Center requires CurrentAuthorizationAuthority")
        if not isinstance(artifact_service, ArtifactService):
            raise TypeError("Family Center requires the existing ArtifactService")
        if not hasattr(source_repository, "get_capability") or not hasattr(source_repository, "get_snapshot"):
            raise TypeError("Family Center requires the existing O4 source repository")
        self.store = store
        self.current_authorization = current_authorization
        self.source_repository = source_repository
        self.artifact_service = artifact_service
        self.worker_store = worker_store
        self.clock = clock
        self.commands = VersionedAggregateCommandExecutor(store, current_authorization)

    def ensure_workspace(self, context: CommandContext, identity: QualificationWorkspaceIdentity) -> WorkspaceView:
        self._authorize(context.principal, context.scope, FAMILY_CENTER_READ)
        self._validate_context_scope(context, identity)
        existing = self._read_aggregate(context.principal, context.scope, identity.identity)
        if existing is None:
            initial = {
                "schema": "org.ephi.family-center.workspace.v1",
                "workspace_id": identity.identity,
                "workspace_identity": identity.as_dict(),
                "gate_revisions": [],
                "promotion_records": [],
            }
            try:
                self.commands.execute(
                    context,
                    command_type="OpenFamilyQualificationWorkspace",
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=identity.identity,
                    payload={"workspace_identity_id": identity.identity},
                    required_capability=FAMILY_CENTER_WRITE,
                    create_if_missing=True,
                    initial_state=initial,
                    effect=lambda current, _payload: current,
                )
            except VersionConflictError:
                # A concurrent opener can create the exact deterministic key.
                pass
        return self.get_workspace(context.principal, context.scope, identity.identity, current_identity=identity)

    def get_workspace(
        self,
        principal: Principal,
        scope: AccessScope,
        workspace_id: str,
        *,
        current_identity: QualificationWorkspaceIdentity | None = None,
    ) -> WorkspaceView:
        self._authorize(principal, scope, FAMILY_CENTER_READ)
        workspace_id = _identity(workspace_id, "workspace_id")
        aggregate = self._read_aggregate(principal, scope, workspace_id)
        if aggregate is None:
            raise AggregateNotFoundError("qualification workspace is unavailable in the requested scope")
        state = aggregate.state
        stored_identity = state.get("workspace_identity")
        if not isinstance(stored_identity, Mapping):
            raise ValidationFailureError("stored qualification workspace identity is invalid")
        identity = current_identity or self._identity_from_stored(stored_identity)
        if identity.scope != scope:
            raise AuthorizationDeniedError("current authorization does not permit this qualification workspace")
        reality = self.current_source_reality(principal, scope, identity)
        return self._view(aggregate, identity, reality, principal)

    def record_gate_evidence(
        self,
        context: CommandContext,
        identity: QualificationWorkspaceIdentity,
        *,
        stage_id: str,
        state: GateState | str,
        policy_basis_id: str,
        policy_basis_version: str,
        engine_identity: str,
        input_identities: Sequence[str] = (),
        artifact_reference: ScopedArtifactReference | None = None,
        known_at: datetime | None = None,
        published_at: datetime | None = None,
        expires_at: datetime | None = None,
        requalification_policy_id: str,
        requalification_policy_version: str,
        job_id: str | None = None,
        reason_code: str | None = None,
    ) -> FamilyCenterMutationResult:
        stage = self._stage(stage_id)
        result_state = _gate_state(state)
        if stage == "DATA_REALITY":
            raise ValidationFailureError("Data Reality must be derived from current O4 facts")
        if stage == "DISCOVER_MAP" and result_state not in {GateState.PASS, GateState.PENDING, GateState.FAIL, GateState.BLOCKED}:
            raise ValidationFailureError("Discover/Map state is not valid")
        if result_state in {GateState.NOT_STARTED, GateState.STALE, GateState.EXPIRED}:
            raise ValidationFailureError("STALE, EXPIRED and NOT_STARTED are derived read states")
        if result_state is GateState.PASS and stage in identity.independent_judgment_stages:
            raise ValidationFailureError("this stage requires a separate independent judgment")
        if result_state is GateState.NOT_APPLICABLE:
            if stage not in identity.not_applicable_stages:
                raise ValidationFailureError("policy does not authorize NOT_APPLICABLE for this stage")
            required_capability = FAMILY_CENTER_POLICY
        else:
            required_capability = FAMILY_CENTER_WRITE
        context.require_existing_aggregate_version()
        self._validate_context_scope(context, identity)
        if result_state is GateState.NOT_APPLICABLE and not policy_basis_id:
            raise ValidationFailureError("NOT_APPLICABLE requires a versioned policy basis")
        server_now = _time(self.clock(), "now")
        captured_known = _time(known_at if known_at is not None else server_now, "known_at")
        captured_published = _time(published_at if published_at is not None else server_now, "published_at")
        payload = {
            "workspace_identity_id": identity.identity,
            "stage_id": stage,
            "state": result_state.value,
            "policy_basis_id": _identity(policy_basis_id, "policy_basis_id"),
            "policy_basis_version": _identity(policy_basis_version, "policy_basis_version"),
            "engine_identity": _identity(engine_identity, "engine_identity"),
            "input_identities": list(input_identities),
            "artifact_reference": _artifact_dict(artifact_reference),
            "known_at": captured_known.isoformat(),
            "published_at": captured_published.isoformat(),
            "expires_at": _time(expires_at, "expires_at").isoformat() if expires_at is not None else None,
            "requalification_policy_id": _identity(requalification_policy_id, "requalification_policy_id"),
            "requalification_policy_version": _identity(requalification_policy_version, "requalification_policy_version"),
            "job_id": _identity(job_id, "job_id") if job_id else None,
            "reason_code": _identity(reason_code, "reason_code") if reason_code else None,
        }

        def build_evidence(aggregate: AggregateSnapshot, request: Mapping[str, Any]) -> Mapping[str, object]:
            if datetime.fromisoformat(str(request["known_at"])) > server_now:
                raise ValidationFailureError("known_at cannot be later than Family Center server time")
            if datetime.fromisoformat(str(request["published_at"])) > server_now:
                raise ValidationFailureError("published_at cannot be later than Family Center server time")
            artifact = _artifact_from(request.get("artifact_reference"))
            if result_state in {GateState.PENDING, GateState.PASS, GateState.NOT_APPLICABLE}:
                self._require_predecessors_ready(context.principal, aggregate.state, identity, stage)
            if result_state is GateState.PASS and artifact is None:
                raise ValidationFailureError("PASS evidence requires an immutable scoped artifact reference")
            if artifact is not None:
                self._verify_artifact(context.principal, identity.scope, artifact)
            source_reality_id = None
            reality = None
            if GATE_STAGES.index(stage) >= GATE_STAGES.index("DATA_REALITY"):
                reality = self.current_source_reality(context.principal, context.scope, identity)
                source_reality_id = reality.reality_identity
            if stage in {"REPLAY", "GOLDEN", "SHADOW", "QUALIFY"} and result_state in {
                GateState.PENDING, GateState.PASS, GateState.NOT_APPLICABLE,
            }:
                if reality is None or reality.state != "READY" or reality.latest_snapshot_id is None:
                    raise ValidationFailureError(f"{stage} requires current O4 source Data Reality")
                required_inputs = {reality.latest_snapshot_id}
                for prior_stage in ("REPLAY", "GOLDEN", "SHADOW"):
                    if prior_stage not in identity.required_stages or GATE_STAGES.index(prior_stage) >= GATE_STAGES.index(stage):
                        continue
                    prior = self._latest_gate(aggregate.state, prior_stage)
                    if prior is None or not prior.get("revision_id"):
                        raise ValidationFailureError(f"{stage} requires current {prior_stage} evidence identity")
                    required_inputs.add(str(prior["revision_id"]))
                if not required_inputs <= set(request["input_identities"]):
                    raise ValidationFailureError(f"{stage} evidence must bind the current source and applicable replay/golden/shadow inputs")
            return self._evidence_payload(
                aggregate,
                identity,
                stage,
                result_state,
                context.principal.subject,
                str(request["policy_basis_id"]),
                str(request["policy_basis_version"]),
                str(request["engine_identity"]),
                tuple(request["input_identities"]),
                artifact,
                datetime.fromisoformat(str(request["known_at"])),
                datetime.fromisoformat(str(request["published_at"])),
                datetime.fromisoformat(str(request["expires_at"])) if request.get("expires_at") else None,
                str(request["requalification_policy_id"]),
                str(request["requalification_policy_version"]),
                request.get("job_id"),
                request.get("reason_code"),
                source_reality_id,
                server_now=server_now,
                policy_authorized=result_state is GateState.NOT_APPLICABLE,
                judgment_identity=None,
            )

        return self._append_gate(context, identity, payload, required_capability, build_evidence)

    def record_current_data_reality(
        self,
        context: CommandContext,
        identity: QualificationWorkspaceIdentity,
        *,
        requalification_policy_id: str = "o4.source-capability-freshness",
        requalification_policy_version: str = "1.0.0",
    ) -> FamilyCenterMutationResult:
        context.require_existing_aggregate_version()
        self._validate_context_scope(context, identity)
        server_now = _time(self.clock(), "now")
        payload = {
            "workspace_identity_id": identity.identity,
            "stage_id": "DATA_REALITY",
            "requalification_policy_id": _identity(requalification_policy_id, "requalification_policy_id"),
            "requalification_policy_version": _identity(requalification_policy_version, "requalification_policy_version"),
        }

        def build_evidence(aggregate: AggregateSnapshot, request: Mapping[str, Any]) -> Mapping[str, object]:
            self._require_predecessors_ready(context.principal, aggregate.state, identity, "DATA_REALITY")
            reality = self.current_source_reality(context.principal, context.scope, identity)
            result_state = GateState.PASS if reality.state == "READY" and reality.artifact_reference is not None else (
                GateState.STALE if reality.state == "STALE" else GateState.BLOCKED
            )
            if result_state is GateState.PASS:
                self._verify_artifact(context.principal, identity.scope, reality.artifact_reference)
            known_at = reality.checked_at
            published_at = server_now
            expires_at = (
                reality.latest_available_at + timedelta(seconds=reality.freshness_age_seconds)
                if reality.latest_available_at and result_state is GateState.PASS else None
            )
            return self._evidence_payload(
                aggregate,
                identity,
                "DATA_REALITY",
                result_state,
                "ephi.o4.source-reality",
                "o4.source-capability",
                identity.source_binding.mapping_version,
                "o4.immutable-source-snapshot",
                tuple(item for item in (reality.latest_snapshot_id, reality.latest_manifest_hash) if item),
                reality.artifact_reference if result_state is GateState.PASS else None,
                known_at,
                published_at,
                expires_at,
                str(request["requalification_policy_id"]),
                str(request["requalification_policy_version"]),
                None,
                None if result_state is GateState.PASS else reality.reason_code,
                reality.reality_identity,
                server_now=server_now,
                policy_authorized=False,
                judgment_identity=None,
            )

        return self._append_gate(context, identity, payload, FAMILY_CENTER_WRITE, build_evidence)

    def adjudicate_gate(
        self,
        context: CommandContext,
        identity: QualificationWorkspaceIdentity,
        *,
        stage_id: str,
        expected_evidence_revision_id: str,
        decision: GateState | str,
        judgment_basis_id: str,
        judgment_basis_version: str,
        reason_code: str,
    ) -> FamilyCenterMutationResult:
        stage = self._stage(stage_id)
        result_state = _gate_state(decision)
        if result_state not in {GateState.PASS, GateState.FAIL, GateState.BLOCKED}:
            raise ValidationFailureError("judgment must be PASS, FAIL or BLOCKED")
        context.require_existing_aggregate_version()
        if stage not in identity.independent_judgment_stages:
            raise ValidationFailureError("independent judgment is not configured for this stage")
        self._validate_context_scope(context, identity)
        payload = {
            "workspace_identity_id": identity.identity,
            "stage_id": stage,
            "expected_evidence_revision_id": _identity(expected_evidence_revision_id, "expected_evidence_revision_id"),
            "decision": result_state.value,
            "judgment_basis_id": _identity(judgment_basis_id, "judgment_basis_id"),
            "judgment_basis_version": _identity(judgment_basis_version, "judgment_basis_version"),
            "reason_code": _identity(reason_code, "reason_code"),
        }

        def build_evidence(aggregate: AggregateSnapshot, request: Mapping[str, Any]) -> Mapping[str, object]:
            prior = self._latest_gate(aggregate.state, stage)
            if prior is None or prior.get("revision_id") != request["expected_evidence_revision_id"]:
                raise VersionConflictError(identity.identity, context.expected_workflow_version, aggregate.version)
            if prior.get("dependency_fingerprint") != identity.dependency_fingerprint(stage):
                raise VersionConflictError(identity.identity, context.expected_workflow_version, aggregate.version)
            if prior.get("state") not in {GateState.PENDING.value, GateState.FAIL.value, GateState.BLOCKED.value}:
                raise ValidationFailureError("only an unresolved, failed or blocked revision can be independently judged")
            if prior.get("created_by") == context.principal.subject:
                raise AuthorizationDeniedError("independent judgment requires a different subject from the evidence author")
            server_now = _time(self.clock(), "now")
            temporal_reason = _future_gate_evidence_reason(prior, server_now)
            if temporal_reason:
                raise ValidationFailureError(f"independent judgment requires current evidence: {temporal_reason}")
            expiry = datetime.fromisoformat(prior["expires_at"]) if prior.get("expires_at") else None
            if expiry is None or expiry <= server_now:
                raise ValidationFailureError("independent judgment requires current, unexpired evidence")
            reality_id = None
            if GATE_STAGES.index(stage) >= GATE_STAGES.index("DATA_REALITY"):
                reality_id = self.current_source_reality(context.principal, context.scope, identity).reality_identity
                if prior.get("source_reality_identity") != reality_id:
                    raise VersionConflictError(identity.identity, context.expected_workflow_version, aggregate.version)
            artifact = _artifact_from(prior.get("artifact_reference"))
            if result_state is GateState.PASS:
                if artifact is None:
                    raise ValidationFailureError("independently judged PASS requires the original immutable artifact")
                self._verify_artifact(context.principal, identity.scope, artifact)
                self._require_predecessors_ready(context.principal, aggregate.state, identity, stage)
            evidence = dict(prior)
            evidence.update({
                "revision": 1 + sum(1 for item in aggregate.state.get("gate_revisions", ()) if item.get("stage_id") == stage),
                "workspace_version": aggregate.version + 1,
                "created_by": prior["created_by"],
                "judgment_identity": context.principal.subject,
                "judgment_basis_id": request["judgment_basis_id"],
                "judgment_basis_version": request["judgment_basis_version"],
                "state": result_state.value,
                "reason_code": request["reason_code"],
                "known_at": server_now.isoformat(),
                "published_at": server_now.isoformat(),
                "previous_revision_id": prior["revision_id"],
                "source_reality_identity": reality_id,
                "revision_sequence": 1 + len(aggregate.state.get("gate_revisions", ())),
                "dependency_fingerprint": identity.dependency_fingerprint(stage),
            })
            evidence.pop("revision_id", None)
            evidence["revision_id"] = _hash(evidence)
            return evidence

        return self._append_gate(context, identity, payload, FAMILY_CENTER_JUDGE, build_evidence)

    def promote(
        self,
        context: CommandContext,
        identity: QualificationWorkspaceIdentity,
        *,
        gate_identity_set: Sequence[str],
        source_reality_identity: str,
    ) -> FamilyCenterMutationResult:
        context.require_existing_aggregate_version()
        self._validate_context_scope(context, identity)
        evidence_set = tuple(_identity(item, "gate_identity") for item in gate_identity_set)
        if len(evidence_set) != len(identity.required_stages) or len(set(evidence_set)) != len(evidence_set):
            raise ValidationFailureError("promotion requires one current evidence identity for every required gate")
        requested_reality_id = _identity(source_reality_identity, "source_reality_identity")
        payload = {
            "workspace_identity_id": identity.identity,
            "workspace_revision": context.expected_workflow_version,
            "gate_identity_set": list(evidence_set),
            "source_reality_identity": requested_reality_id,
            "promotion_kind": "FAMILY_CENTER_QUALIFICATION",
            "g12_production_approval": False,
        }

        def append(current: Mapping[str, Any], _payload: Mapping[str, Any]) -> Mapping[str, Any]:
            if current.get("workspace_id") != identity.identity:
                raise ValidationFailureError("workspace identity changed before promotion")
            aggregate = AggregateSnapshot(
                context.scope.canonical_key,
                AGGREGATE_TYPE,
                identity.identity,
                context.expected_workflow_version,
                dict(current),
            )
            reality = self.current_source_reality(context.principal, context.scope, identity)
            view = self._view(aggregate, identity, reality, context.principal)
            if not view.promotion_ready:
                temporal_blockers = tuple(
                    blocker for blocker in view.promotion_blockers
                    if blocker.endswith(":FUTURE_EVIDENCE_TIME")
                )
                if temporal_blockers:
                    raise ValidationFailureError(
                        "promotion is blocked by future-dated qualification evidence: "
                        + ", ".join(temporal_blockers)
                    )
                raise ValidationFailureError("promotion is blocked by current qualification gates")
            if reality.reality_identity != requested_reality_id:
                raise ValidationFailureError("O4 Data Reality changed before promotion")
            current_revisions = tuple(
                item.get("revision_id") for stage in identity.required_stages
                if (item := self._latest_gate(current, stage)) is not None
            )
            if current_revisions != evidence_set:
                raise ValidationFailureError("current gate evidence changed before promotion")
            promotion = {
                "workspace_identity_id": identity.identity,
                "workspace_revision": context.expected_workflow_version,
                "workspace_resulting_revision": context.expected_workflow_version + 1,
                "gate_identity_set": list(evidence_set),
                "promoted_by": context.principal.subject,
                "promoted_at": _time(self.clock(), "promoted_at").isoformat(),
                "source_reality_identity": reality.reality_identity,
                "promotion_kind": "FAMILY_CENTER_QUALIFICATION",
                "g12_production_approval": False,
            }
            promotion["promotion_id"] = _hash(promotion)
            result = dict(current)
            records = list(current.get("promotion_records", ()))
            records.append(promotion)
            result["promotion_records"] = records
            return result

        result = self.commands.execute(
            context,
            command_type="PromoteFamilyQualification",
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=identity.identity,
            payload=payload,
            required_capability=FAMILY_CENTER_PROMOTE,
            effect=append,
        )
        return self._mutation_result(result)

    def current_source_reality(
        self,
        principal: Principal,
        scope: AccessScope,
        identity: QualificationWorkspaceIdentity,
    ) -> SourceReality:
        self._authorize(principal, scope, FAMILY_CENTER_READ)
        try:
            self._authorize(principal, scope, SOURCE_READ_CAPABILITY)
            capability = self.source_repository.get_capability(principal, identity.source_binding)
            if not isinstance(capability, SourceCapabilityRecord) or capability.binding != identity.source_binding:
                raise ValidationFailureError("O4 returned a source capability for a different binding")
            snapshot: SourceSnapshotRecord | None = None
            if capability.latest_snapshot_id is not None:
                snapshot = self.source_repository.get_snapshot(principal, scope, capability.latest_snapshot_id)
                if snapshot.binding != identity.source_binding or snapshot.snapshot_id != capability.latest_snapshot_id:
                    raise ValidationFailureError("O4 returned a source snapshot for a different binding")
            now = _time(self.clock(), "now")
            source_times = (
                capability.checked_at,
                capability.latest_available_at,
                capability.latest_event_at,
                snapshot.event_start if snapshot else None,
                snapshot.event_end if snapshot else None,
                snapshot.available_cutoff if snapshot else None,
                snapshot.ingested_at if snapshot else None,
                snapshot.published_at if snapshot else None,
                snapshot.created_at if snapshot else None,
            )
            future_source_time = any(
                timestamp is not None and _time(timestamp, "source_time") > now
                for timestamp in source_times
            )
            age = (
                max(0, int((now - capability.latest_available_at).total_seconds()))
                if capability.latest_available_at and not future_source_time else None
            )
            fresh = age is not None and age <= capability.freshness_age_seconds
            published = snapshot is not None and snapshot.status is SourceSnapshotStatus.PUBLISHED
            ready = not future_source_time and capability.state is SourceCapabilityState.READY and fresh and published
            state = "BLOCKED" if future_source_time else "READY" if ready else (
                "STALE" if capability.state is SourceCapabilityState.STALE or (capability.state is SourceCapabilityState.READY and not fresh)
                else "UNAVAILABLE" if capability.state is SourceCapabilityState.UNAVAILABLE
                else "PARTIAL" if capability.state is SourceCapabilityState.PARTIAL
                else "INSUFFICIENT" if capability.state is SourceCapabilityState.INSUFFICIENT
                else "BLOCKED"
            )
            reason = "FUTURE_SOURCE_REALITY_TIME" if future_source_time else capability.reason if capability.reason in {
                "CAPABILITY_RECORD_MISSING", "PARTIAL_SNAPSHOT", "NO_SUFFICIENT_SOURCE_ROWS",
                "SOURCE_QUARANTINED", "SOURCE_AVAILABILITY_EXCEEDS_FRESHNESS_LIMIT", "FRESH_PUBLISHED_SNAPSHOT",
            } else "SOURCE_STATE_UNAVAILABLE"
            reference = snapshot.artifact_reference if snapshot is not None else None
            if ready and reference is not None:
                try:
                    self._verify_artifact(principal, scope, reference)
                except ArtifactError:
                    state = "UNAVAILABLE"
                    reason = "SOURCE_MANIFEST_ARTIFACT_UNAVAILABLE"
            reality_id = _source_record_identity(snapshot, capability, state)
            return SourceReality(
                capability.state.value,
                state,
                reason,
                capability.checked_at,
                capability.freshness_age_seconds,
                age,
                capability.latest_snapshot_id,
                snapshot.manifest_hash if snapshot else None,
                capability.latest_available_at,
                capability.latest_event_at,
                reference,
                reality_id,
            )
        except AuthorizationDeniedError:
            now = _time(self.clock(), "checked_at")
            return SourceReality(
                SourceCapabilityState.UNAVAILABLE.value,
                "UNAVAILABLE",
                "SOURCE_AUTHORIZATION_REQUIRED",
                now,
                0,
                None,
                reality_identity=_hash({"binding": identity.source_binding_identity, "state": "SOURCE_AUTHORIZATION_REQUIRED"}),
            )
        except Exception:
            now = _time(self.clock(), "checked_at")
            return SourceReality(
                SourceCapabilityState.UNAVAILABLE.value,
                "UNAVAILABLE",
                "SOURCE_AUTHORITY_UNAVAILABLE",
                now,
                0,
                None,
                reality_identity=_hash({"binding": identity.source_binding_identity, "state": "UNAVAILABLE"}),
            )

    @staticmethod
    def mapping_facts(identity: QualificationWorkspaceIdentity) -> MappingFacts:
        binding = identity.source_binding
        return MappingFacts(
            binding.provider_id,
            binding.source_id,
            binding.schema_id,
            binding.mapping_version,
            binding.mapping_hash,
            tuple(binding.required_identifiers),
            binding.unit,
            "event_at is the metrology event time; values are timezone-aware UTC instants",
            "source_available_at records when the source made the observation available",
            binding.reference_population_id,
            binding.comparable_population_id,
        )

    def inspect_jobs(self, principal: Principal, scope: AccessScope, job_ids: Sequence[str]) -> dict[str, tuple[str, str | None]]:
        self._authorize(principal, scope, FAMILY_CENTER_READ)
        if self.worker_store is None:
            return {job_id: ("UNAVAILABLE", None) for job_id in job_ids}
        result: dict[str, tuple[str, str | None]] = {}
        for job_id in tuple(dict.fromkeys(job_ids))[:20]:
            try:
                rows = self.worker_store.inspect(scope, job_id=job_id, limit=1)
                if not rows:
                    result[job_id] = ("NOT_FOUND", None)
                else:
                    row = rows[0]
                    result[job_id] = (row.status, row.last_failure_code)
            except Exception:
                result[job_id] = ("UNAVAILABLE", None)
        return result

    def _evidence_payload(
        self,
        aggregate: AggregateSnapshot,
        identity: QualificationWorkspaceIdentity,
        stage: str,
        state: GateState,
        created_by: str,
        policy_basis_id: str,
        policy_basis_version: str,
        engine_identity: str,
        input_identities: Sequence[str],
        artifact_reference: ScopedArtifactReference | None,
        known_at: datetime,
        published_at: datetime,
        expires_at: datetime | None,
        requalification_policy_id: str,
        requalification_policy_version: str,
        job_id: str | None,
        reason_code: str | None,
        source_reality_identity: str | None,
        *,
        server_now: datetime,
        policy_authorized: bool,
        judgment_identity: str | None,
    ) -> dict[str, object]:
        known = _time(known_at, "known_at")
        published = _time(published_at, "published_at")
        expires = _time(expires_at, "expires_at") if expires_at is not None else None
        now = _time(server_now, "now")
        if known > now:
            raise ValidationFailureError("known_at cannot be later than Family Center server time")
        if published > now:
            raise ValidationFailureError("published_at cannot be later than Family Center server time")
        if published < known:
            raise ValidationFailureError("published_at cannot precede known_at")
        if expires is not None and expires <= published:
            raise ValidationFailureError("expires_at must follow published_at")
        if state in {GateState.PENDING, GateState.PASS} and expires is None:
            raise ValidationFailureError("PENDING and PASS evidence require an explicit expiration/requalification boundary")
        if state is GateState.NOT_APPLICABLE and expires is None:
            raise ValidationFailureError("NOT_APPLICABLE evidence requires a requalification boundary")
        if state is GateState.PASS and artifact_reference is None:
            raise ValidationFailureError("PASS evidence requires an immutable artifact reference")
        if state is GateState.NOT_APPLICABLE and not policy_authorized:
            raise ValidationFailureError("NOT_APPLICABLE requires an explicit policy authorization")
        inputs = tuple(sorted({_identity(item, "input_identity") for item in input_identities}))
        if len(inputs) != len(input_identities):
            raise ValidationFailureError("input identities must be unique")
        revision_sequence = 1 + sum(1 for item in aggregate.state.get("gate_revisions", ()) if item.get("stage_id") == stage)
        value: dict[str, object] = {
            "workspace_identity_id": identity.identity,
            "workspace_version": aggregate.version + 1,
            "stage_id": stage,
            "revision": revision_sequence,
            "state": state.value,
            "policy_basis_id": _identity(policy_basis_id, "policy_basis_id"),
            "policy_basis_version": _identity(policy_basis_version, "policy_basis_version"),
            "policy_identity": dict(identity.stage_policy_identities).get(stage, "UNBOUND_POLICY"),
            "engine_identity": _identity(engine_identity, "engine_identity"),
            "input_identities": list(inputs),
            "artifact_reference": _artifact_dict(artifact_reference),
            "artifact_sha256": artifact_reference.content.sha256 if artifact_reference else None,
            "artifact_byte_size": artifact_reference.content.byte_size if artifact_reference else None,
            "known_at": known.isoformat(),
            "published_at": published.isoformat(),
            "expires_at": expires.isoformat() if expires else None,
            "requalification_policy_id": _identity(requalification_policy_id, "requalification_policy_id"),
            "requalification_policy_version": _identity(requalification_policy_version, "requalification_policy_version"),
            "source_reality_identity": source_reality_identity,
            "created_by": _identity(created_by, "created_by"),
            "judgment_identity": judgment_identity,
            "policy_authorized": policy_authorized,
            "job_id": _identity(job_id, "job_id") if job_id else None,
            "reason_code": _identity(reason_code, "reason_code") if reason_code else None,
            "dependency_fingerprint": identity.dependency_fingerprint(stage),
            "previous_revision_id": self._latest_gate(aggregate.state, stage).get("revision_id") if self._latest_gate(aggregate.state, stage) else None,
        }
        value["revision_sequence"] = 1 + len(aggregate.state.get("gate_revisions", ()))
        value["revision_id"] = _hash(value)
        return value

    def _append_gate(
        self,
        context: CommandContext,
        identity: QualificationWorkspaceIdentity,
        payload: Mapping[str, object],
        required_capability: str,
        build_evidence,
    ) -> FamilyCenterMutationResult:
        def append(current: Mapping[str, Any], request: Mapping[str, Any]) -> Mapping[str, Any]:
            if current.get("workspace_id") != identity.identity:
                raise ValidationFailureError("workspace identity changed before evidence append")
            aggregate = AggregateSnapshot(
                context.scope.canonical_key,
                AGGREGATE_TYPE,
                identity.identity,
                context.expected_workflow_version or 0,
                dict(current),
            )
            evidence = build_evidence(aggregate, request)
            revisions = list(current.get("gate_revisions", ()))
            revisions.append(dict(evidence))
            next_state = dict(current)
            next_state["gate_revisions"] = revisions
            return next_state

        result = self.commands.execute(
            context,
            command_type="RecordFamilyQualificationGateEvidence",
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=identity.identity,
            payload=dict(payload),
            required_capability=required_capability,
            effect=append,
        )
        return self._mutation_result(result)

    @staticmethod
    def _mutation_result(result: CommandResult) -> FamilyCenterMutationResult:
        return FamilyCenterMutationResult(
            result.status,
            result.result_identity,
            result.aggregate_id,
            result.aggregate_version,
        )

    def _view(
        self,
        aggregate: AggregateSnapshot,
        current_identity: QualificationWorkspaceIdentity,
        reality: SourceReality,
        principal: Principal,
    ) -> WorkspaceView:
        now = _time(self.clock(), "now")
        stored = aggregate.state.get("workspace_identity")
        stored_identity = self._identity_from_stored(stored)
        identity_match = stored_identity.identity == current_identity.identity
        revisions = aggregate.state.get("gate_revisions", ())
        if not isinstance(revisions, (tuple, list)):
            raise ValidationFailureError("stored gate revisions are invalid")
        latest = {stage: self._latest_gate(aggregate.state, stage) for stage in GATE_STAGES}
        jobs = self.inspect_jobs(
            principal, current_identity.scope,
            tuple(item["job_id"] for item in latest.values() if item and item.get("job_id")),
        ) if self.worker_store is not None else {}
        gate_views: list[GateView] = []
        for stage in GATE_STAGES:
            item = latest[stage]
            if item is None:
                state = GateState.BLOCKED if stage == "DATA_REALITY" and reality.state != "READY" else GateState.NOT_STARTED
                gate_views.append(GateView(stage, state, invalidation_reason=(reality.reason_code if state is GateState.BLOCKED else None)))
                continue
            reason = None
            state = _gate_state(item["state"])
            if item.get("dependency_fingerprint") != current_identity.dependency_fingerprint(stage):
                state, reason = GateState.STALE, "DEPENDENCY_IDENTITY_CHANGED"
            if stage == "DATA_REALITY" and item.get("source_reality_identity") != reality.reality_identity:
                state, reason = (GateState.BLOCKED if reality.state in {"UNAVAILABLE", "PARTIAL", "INSUFFICIENT", "BLOCKED"} else GateState.STALE), reality.reason_code if reality.state != "READY" else "SOURCE_SNAPSHOT_CHANGED"
            if stage != "DISCOVER_MAP" and GATE_STAGES.index(stage) > GATE_STAGES.index("DATA_REALITY"):
                data_reality = latest.get("DATA_REALITY")
                if data_reality is None or item.get("source_reality_identity") != reality.reality_identity:
                    state, reason = GateState.STALE, "DATA_REALITY_DEPENDENCY_CHANGED"
            expiry = datetime.fromisoformat(item["expires_at"]) if item.get("expires_at") else None
            if state in {GateState.PENDING, GateState.PASS, GateState.NOT_APPLICABLE} and expiry is not None and expiry <= now:
                state, reason = GateState.EXPIRED, "EVIDENCE_EXPIRED"
            artifact = _artifact_from(item.get("artifact_reference"))
            artifact_status = "NONE"
            if artifact is not None:
                artifact_status = "AVAILABLE"
                try:
                    self._verify_artifact(principal, current_identity.scope, artifact)
                except AuthorizationDeniedError:
                    artifact_status = "UNAVAILABLE"
                    if state is GateState.PASS:
                        state, reason = GateState.BLOCKED, "EVIDENCE_READ_AUTHORIZATION_REQUIRED"
                except ArtifactError:
                    artifact_status = "UNAVAILABLE"
                    if state is GateState.PASS:
                        state, reason = GateState.BLOCKED, "EVIDENCE_ARTIFACT_UNAVAILABLE"
            temporal_reason = _future_gate_evidence_reason(item, now)
            if temporal_reason:
                state, reason = GateState.BLOCKED, temporal_reason
            job_id = item.get("job_id")
            job_status, _failure_code = jobs.get(job_id, (None, None)) if job_id else (None, None)
            gate_views.append(GateView(
                stage,
                state,
                item.get("revision_id"),
                item.get("revision"),
                item.get("policy_basis_id"),
                item.get("policy_basis_version"),
                item.get("artifact_sha256"),
                item.get("artifact_byte_size"),
                artifact_status,
                datetime.fromisoformat(item["known_at"]) if item.get("known_at") else None,
                expiry,
                reason or item.get("reason_code"),
                item.get("judgment_identity"),
                job_id,
                job_status,
                item.get("created_by"),
                item.get("engine_identity"),
                tuple(item.get("input_identities", ())),
                datetime.fromisoformat(item["published_at"]) if item.get("published_at") else None,
                item.get("policy_identity"),
                item.get("requalification_policy_id"),
                item.get("requalification_policy_version"),
                item.get("judgment_basis_id"),
                item.get("judgment_basis_version"),
                item.get("source_reality_identity"),
            ))
        by_stage = {item.stage_id: item for item in gate_views}
        blockers = []
        if not identity_match:
            blockers.append("WORKSPACE_IDENTITY_CHANGED")
        for stage in current_identity.required_stages:
            gate = by_stage[stage]
            if gate.state is GateState.NOT_APPLICABLE:
                stored_item = latest.get(stage)
                if stage in current_identity.not_applicable_stages and stored_item and stored_item.get("policy_authorized"):
                    continue
                blockers.append(f"{stage}:NOT_APPLICABLE_NOT_POLICY_AUTHORIZED")
            elif gate.state is not GateState.PASS:
                temporal_reason = _future_gate_evidence_reason(latest[stage], now) if latest[stage] else None
                blockers.append(f"{stage}:{temporal_reason or gate.state.value}")
        if reality.state != "READY":
            blockers.append(f"DATA_REALITY:{reality.capability_state}/{reality.state}")
        ready = identity_match and not blockers
        promotion_views = self._promotion_views(aggregate.state, current_identity, by_stage, reality, aggregate.version)
        return WorkspaceView(
            aggregate.aggregate_id,
            stored_identity.identity,
            stored_identity.family_id,
            stored_identity.family_context_version,
            stored_identity.capability_id,
            stored_identity.product_id,
            stored_identity.release_id,
            stored_identity.context_identity,
            stored_identity.unit_identity,
            aggregate.version,
            tuple(gate_views),
            ready,
            tuple(dict.fromkeys(blockers)),
            promotion_views,
            reality,
            self.mapping_facts(stored_identity),
            identity_match,
            stored_identity.synthetic_fixture,
        )

    def _promotion_views(
        self,
        state: Mapping[str, Any],
        identity: QualificationWorkspaceIdentity,
        gates: Mapping[str, GateView],
        reality: SourceReality,
        current_workspace_revision: int,
    ) -> tuple[PromotionView, ...]:
        result = []
        for record in state.get("promotion_records", ()):
            current_gate_ids = tuple(gates[stage].revision_id for stage in identity.required_stages)
            expected = tuple(record.get("gate_identity_set", ()))
            current = (
                record.get("workspace_identity_id") == identity.identity
                and expected == current_gate_ids
                and record.get("source_reality_identity") == reality.reality_identity
                and reality.state == "READY"
                and record.get("workspace_resulting_revision") == current_workspace_revision
            )
            result.append(PromotionView(
                record["promotion_id"],
                "CURRENT" if current else "STALE",
                record["workspace_revision"],
                expected,
                record["promoted_by"],
                datetime.fromisoformat(record["promoted_at"]),
                None if current else "DEPENDENCY_OR_GATE_IDENTITY_CHANGED",
            ))
        return tuple(result)

    def _require_predecessors_ready(
        self,
        principal: Principal,
        state: Mapping[str, Any],
        identity: QualificationWorkspaceIdentity,
        stage: str,
    ) -> None:
        position = GATE_STAGES.index(stage)
        now = _time(self.clock(), "now")
        for predecessor in GATE_STAGES[:position]:
            if predecessor not in identity.required_stages:
                continue
            item = self._latest_gate(state, predecessor)
            temporal_reason = _future_gate_evidence_reason(item, now) if item is not None else None
            if temporal_reason:
                raise ValidationFailureError(f"{stage} is blocked by {temporal_reason} predecessor evidence: {predecessor}")
            if item is None or item.get("state") not in {GateState.PASS.value, GateState.NOT_APPLICABLE.value}:
                raise ValidationFailureError(f"{stage} is blocked by current gate {predecessor}")
            if item.get("state") == GateState.NOT_APPLICABLE.value and not (
                predecessor in identity.not_applicable_stages and item.get("policy_authorized")
            ):
                raise ValidationFailureError(f"{stage} is blocked by unauthorized NOT_APPLICABLE gate {predecessor}")
            if item.get("dependency_fingerprint") != identity.dependency_fingerprint(predecessor):
                raise ValidationFailureError(f"{stage} is blocked by stale gate {predecessor}")
            expiry = datetime.fromisoformat(item["expires_at"]) if item.get("expires_at") else None
            if expiry is not None and expiry <= now:
                raise ValidationFailureError(f"{stage} is blocked by expired gate {predecessor}")
        if position > GATE_STAGES.index("DATA_REALITY"):
            item = self._latest_gate(state, "DATA_REALITY")
            reality_id = self.current_source_reality(
                principal, identity.scope, identity
            ).reality_identity
            if item is None or item.get("state") != GateState.PASS.value or item.get("source_reality_identity") != reality_id:
                raise ValidationFailureError(f"{stage} is blocked by current O4 Data Reality")

    def _verify_artifact(self, principal: Principal, scope: AccessScope, reference: ScopedArtifactReference) -> None:
        if not isinstance(reference, ScopedArtifactReference) or reference.scope != scope:
            raise ValidationFailureError("evidence artifact must be an immutable reference in the exact scope")
        self.artifact_service.verify_publish_preconditions(
            principal,
            (reference,),
            FAMILY_CENTER_ARTIFACT_READ,
        )

    def _read_aggregate(self, principal: Principal, scope: AccessScope, workspace_id: str) -> AggregateSnapshot | None:
        self._authorize(principal, scope, FAMILY_CENTER_READ)
        with self.store.command_transaction() as transaction:
            return transaction.get_aggregate(scope.canonical_key, AGGREGATE_TYPE, workspace_id)

    def _authorize(self, principal: Principal, scope: AccessScope, capability: str) -> None:
        self.current_authorization.authorize(principal, scope, capability)

    @staticmethod
    def _validate_context_scope(context: CommandContext, identity: QualificationWorkspaceIdentity) -> None:
        if context.scope != identity.scope:
            raise AuthorizationDeniedError("current authorization does not permit this qualification workspace")

    @staticmethod
    def _stage(stage_id: str) -> str:
        stage = _identity(stage_id, "stage_id")
        if stage not in GATE_STAGES:
            raise ValidationFailureError("stage ID is not a writable qualification gate")
        return stage

    @staticmethod
    def _latest_gate(state: Mapping[str, Any], stage_id: str) -> Mapping[str, Any] | None:
        rows = state.get("gate_revisions", ())
        matches = [item for item in rows if item.get("stage_id") == stage_id]
        return matches[-1] if matches else None

    @staticmethod
    def _identity_from_stored(value: Mapping[str, Any]) -> QualificationWorkspaceIdentity:
        """Stored identity reconstruction is intentionally read-only and strict."""

        source = value.get("source_binding")
        if not isinstance(source, Mapping):
            raise ValidationFailureError("stored O4 source binding is invalid")
        scope_data = source.get("scope_key")
        try:
            decoded_scope = json.loads(scope_data) if isinstance(scope_data, str) else source["scope"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValidationFailureError("stored source scope is invalid") from exc
        if not isinstance(decoded_scope, Mapping):
            raise ValidationFailureError("stored source scope is invalid")
        scope = AccessScope(
            decoded_scope["scope_id"], decoded_scope.get("site_id"), decoded_scope.get("area_id"),
            decoded_scope.get("family_id"), tuple(decoded_scope.get("project_ids", ())),
        )
        binding = MetrologySourceBinding(
            scope,
            source["source_id"], source["provider_id"], source["family_id"], source["capability_id"],
            source["adapter_id"], source["schema_id"], source["mapping_version"], source["mapping_hash"],
            source["unit"], source.get("reference_population_id"), source.get("comparable_population_id"),
            tuple(source.get("required_identifiers", ())),
        )
        contracts = tuple(ProviderIdentity(item["category"], item["contract_id"], item["version"]) for item in value["provider_contracts"])
        policy = value["policy"]
        runtime = value["runtime"]
        stages = value.get("stage_policy_identities", {})
        return QualificationWorkspaceIdentity(
            scope=scope,
            family_id=value["family_id"],
            family_context_version=value["family_context_version"],
            target_identity=value["target_identity"],
            context_identity=value["context_identity"],
            unit_identity=value["unit_identity"],
            characteristic_identity=value["characteristic_identity"],
            capability_id=value["capability_id"],
            product_id=value["product_id"],
            release_id=value["release_id"],
            provider_abi_id=value["provider_abi"]["id"],
            provider_abi_version=value["provider_abi"]["version"],
            provider_contracts=contracts,
            policy_schema_id=policy["schema_id"],
            policy_schema_version=policy["schema_version"],
            policy_configuration_version=policy["configuration_version"],
            policy_configuration_identity=policy["configuration_identity"],
            source_binding=binding,
            runtime_environment_class=runtime["environment_class"],
            postgresql_major_version=runtime["postgresql_major_version"],
            runtime_contract_version=runtime["contract_version"],
            stage_policy_identities=tuple(sorted(stages.items())),
            required_stages=tuple(value["required_stages"]),
            not_applicable_stages=tuple(value["not_applicable_stages"]),
            independent_judgment_stages=tuple(value["independent_judgment_stages"]),
            synthetic_fixture=bool(value.get("synthetic_fixture", False)),
        )


__all__ = [
    "AGGREGATE_TYPE",
    "FAMILY_CENTER_ARTIFACT_READ",
    "FAMILY_CENTER_JUDGE",
    "FAMILY_CENTER_POLICY",
    "FAMILY_CENTER_PROMOTE",
    "FAMILY_CENTER_READ",
    "FAMILY_CENTER_WRITE",
    "FamilyCenterMutationResult",
    "FamilyCenterService",
    "GateState",
    "GateView",
    "MappingFacts",
    "PromotionView",
    "ProviderIdentity",
    "QualificationWorkspaceIdentity",
    "SourceReality",
    "STAGE_ORDER",
    "WorkspaceView",
]
