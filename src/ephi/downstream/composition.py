"""Explicit composition of public downstream providers with EPHI authorities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json

from ephi.application import (
    AccessScope,
    ArtifactService,
    AttentionQueryService,
    ComparableCaseHistoryQueryService,
    CurrentAuthorizationAuthority,
    DecisionLoopCommandService,
    DecisionSnapshotHandoffService,
    EpisodeBriefQueryService,
    EpisodeInvestigationQueryService,
    EpisodeWorkflowCommandService,
    FamilyCenterService,
    MetrologySourceBinding,
    NextCheckPlannerService,
    Principal,
    RcaAnalysisService,
    RcaMaterializationCoordinator,
    RcaMaterializationView,
    ProviderIdentity,
    QualificationWorkspaceIdentity,
    DeliveryChannelAdapter,
    SourceSnapshotIngressService,
)
from ephi.config import RuntimeSettings
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter
from ephi.value import OutcomesService
from ephi.value.repository import OutcomeAggregateRepository
from ephi.application.transactions import VersionedAggregateCommandExecutor

from .contracts import (
    BoundedMetrologyObserver,
    DownstreamFailure,
    DownstreamReasonCode,
    PolicyConfiguration,
    PolicySchemaMetadata,
    ProviderBundle,
    ProviderCategory,
    RuntimeCapabilities,
)
from .validation import validate_provider_bundle


@dataclass(frozen=True, slots=True)
class DownstreamComposition:
    """Existing EPHI authorities plus validated downstream adapter bindings."""

    adapter: PostgreSQLReferenceTransactionAdapter
    runtime_settings: RuntimeSettings
    current_authorization: CurrentAuthorizationAuthority
    principal_provider: Callable[[], Principal]
    scope_provider: Callable[[], AccessScope]
    attention: AttentionQueryService
    episode_briefs: EpisodeBriefQueryService
    episode_investigations: EpisodeInvestigationQueryService
    rca_materializations: RcaMaterializationCoordinator
    workflow: EpisodeWorkflowCommandService
    decision_loop: DecisionLoopCommandService
    planner: NextCheckPlannerService
    handoff: DecisionSnapshotHandoffService
    artifact_service: ArtifactService
    source_ingress: SourceSnapshotIngressService
    source_observer: BoundedMetrologyObserver
    source_binding: MetrologySourceBinding
    policy_configuration: PolicyConfiguration
    policy_schema_metadata: PolicySchemaMetadata
    runtime_capabilities: RuntimeCapabilities
    provider_abi_id: str
    provider_abi_version: str
    provider_contract_identities: tuple[ProviderIdentity, ...]
    policy_configuration_identity: str
    family_center: FamilyCenterService
    notification_channel: DeliveryChannelAdapter
    outcomes: OutcomesService

    def family_workspace_identity(self, family_id: str, target: object) -> QualificationWorkspaceIdentity:
        """Build one workspace key from the exact composed U1/O4 identities."""

        from .contracts import FamilyQualificationTarget

        if not isinstance(target, FamilyQualificationTarget):
            raise ValueError("Family Center target must be configured in U1 PolicyConfiguration")
        family = next((item for item in self.policy_configuration.family_contexts if item.family_id == family_id), None)
        if family is None or target not in family.qualification_targets:
            raise ValueError("Family Center target is not present in current U1 family configuration")
        context = next((item for item in family.contexts if item.target_identity == target.context_target_identity), None)
        if context is None:
            raise ValueError("Family Center context identity is unavailable")
        if self.source_binding.family_id != family_id or self.source_binding.capability_id != target.capability_id:
            raise ValueError("exact U1 source provider binding does not match this family/capability")
        stage_policies = (
            ("REPLAY", hashlib.sha256(f"{self.policy_configuration_identity}:replay".encode()).hexdigest()),
            ("GOLDEN", self.policy_configuration.check_catalog.identity),
            ("SHADOW", hashlib.sha256(f"{self.policy_configuration_identity}:shadow".encode()).hexdigest()),
            ("QUALIFY", _recovery_identity(self.policy_configuration.recovery_policy)),
        )
        return QualificationWorkspaceIdentity(
            scope=self.scope_provider(),
            family_id=family.family_id,
            family_context_version=family.version,
            target_identity=context.target_identity,
            context_identity=context.context_identity or "UNBOUND_CONTEXT",
            unit_identity=context.unit_identity or "UNBOUND_UNIT",
            characteristic_identity=context.characteristic_identity or "UNBOUND_CHARACTERISTIC",
            capability_id=target.capability_id,
            product_id=target.product_id,
            release_id=target.release_id,
            provider_abi_id=self.provider_abi_id,
            provider_abi_version=self.provider_abi_version,
            provider_contracts=self.provider_contract_identities,
            policy_schema_id=self.policy_schema_metadata.schema_id,
            policy_schema_version=self.policy_schema_metadata.version,
            policy_configuration_version=self.policy_schema_metadata.configuration_version,
            policy_configuration_identity=self.policy_configuration_identity,
            source_binding=self.source_binding,
            runtime_environment_class=self.runtime_capabilities.target_environment_class,
            postgresql_major_version=self.runtime_capabilities.postgresql_major_version,
            runtime_contract_version=self.runtime_capabilities.postgresql_contract_version,
            stage_policy_identities=stage_policies,
            required_stages=target.required_stages,
            not_applicable_stages=target.not_applicable_stages,
            independent_judgment_stages=target.independent_judgment_stages,
            synthetic_fixture=target.synthetic_fixture,
        )

    def process_rca_materialization(self, scope: AccessScope, owner: str) -> RcaMaterializationView | None:
        """Run one queued bounded RCA job through the existing worker authority."""

        principal = self.principal_provider()
        return self.rca_materializations.process_one(
            principal,
            scope,
            owner,
            load_current_facts=lambda current_principal, query: self.episode_investigations.load_current_rca_facts(
                current_principal, query
            ),
        )

    def close(self) -> None:
        try:
            self.adapter.close()
        except Exception:
            raise DownstreamFailure(
                DownstreamReasonCode.COMPOSITION_FAIL_CLOSED,
                categories=(ProviderCategory.RUNTIME.value,),
            ) from None


def compose_downstream(
    bundle: object,
    *,
    runtime_settings: RuntimeSettings | None = None,
) -> DownstreamComposition:
    """Validate first, then compose the existing PostgreSQL backed services.

    There is no browser authority or alternative persistence path here. The
    runtime provider supplies only the secret-bearing connection input; all
    command, read, source, artifact catalog, worker, workflow and handoff state
    stays in the existing PostgreSQL authorities.
    """

    validated = validate_provider_bundle(bundle)
    try:
        settings = runtime_settings or RuntimeSettings.from_environment()
    except Exception:
        raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.RUNTIME.value,)) from None
    if not isinstance(settings, RuntimeSettings):
        raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.RUNTIME.value,))
    identity = validated.identity.implementation
    source = validated.source.implementation
    artifacts = validated.artifacts.implementation
    notifications = validated.notifications.implementation
    policy_provider = validated.policy.implementation
    runtime_provider = validated.runtime.implementation

    metadata = validated.runtime.public_metadata
    if metadata is None or metadata.target_environment_class != settings.environment.lower():
        raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.RUNTIME.value,))
    adapter: PostgreSQLReferenceTransactionAdapter | None = None
    try:
        try:
            principal = identity.resolve_principal()
            scope = identity.resolve_scope()
        except Exception:
            raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.IDENTITY.value,)) from None
        if not isinstance(principal, Principal) or not isinstance(scope, AccessScope) or not principal.grants_scope(scope):
            raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.IDENTITY.value,))
        current_authorization = CurrentAuthorizationAuthority(identity.resolve_current_principal)

        try:
            described = source.observer.describe()
        except Exception:
            raise DownstreamFailure(DownstreamReasonCode.SOURCE_BINDING_MISMATCH, categories=(ProviderCategory.SOURCE.value,)) from None
        if described != source.expected_binding:
            raise DownstreamFailure(DownstreamReasonCode.SOURCE_BINDING_MISMATCH, categories=(ProviderCategory.SOURCE.value,))

        try:
            adapter = runtime_provider.open_postgresql()
        except Exception:
            raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.RUNTIME.value,)) from None
        if type(adapter) is not PostgreSQLReferenceTransactionAdapter:
            try:
                close = getattr(adapter, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            adapter = None
            raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.RUNTIME.value,))
        try:
            server_major = int(adapter.server_version().split(".", 1)[0])
            if server_major != metadata.postgresql_major_version:
                raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.RUNTIME.value,))
        except DownstreamFailure:
            raise
        except Exception:
            raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.RUNTIME.value,)) from None

        try:
            blob_store = artifacts.blob_store()
            catalog = artifacts.catalog_for(adapter)
            artifact_service = ArtifactService(blob_store, catalog, current_authorization)
        except Exception:
            raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.ARTIFACTS.value,)) from None

        read_store = adapter.read_store()
        attention = AttentionQueryService(adapter.o3_store(), read_store, current_authorization)
        episode_briefs = EpisodeBriefQueryService(read_store, current_authorization)
        workflow = EpisodeWorkflowCommandService(adapter, current_authorization)
        decision_loop = DecisionLoopCommandService(adapter, current_authorization)
        planner = NextCheckPlannerService(decision_loop)
        policy_configuration = policy_provider.configuration
        if not isinstance(policy_configuration, PolicyConfiguration):
            raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.POLICY.value,))
        comparable_history = ComparableCaseHistoryQueryService(read_store, read_store, current_authorization)
        rca_service = RcaAnalysisService(current_authorization)
        rca_materializations = RcaMaterializationCoordinator(
            adapter.worker_store(), adapter, artifact_service, current_authorization, rca_service
        )
        episode_investigations = EpisodeInvestigationQueryService(
            episode_briefs,
            decision_loop,
            planner,
            current_authorization,
            policy_configuration.planner_policy,
            policy_configuration.check_catalog,
            rca_service,
            comparable_history,
            rca_materializations,
        )
        handoff = DecisionSnapshotHandoffService(
            adapter,
            current_authorization,
            worker=adapter.worker_store(),
            recipients=notifications.recipients,
        )
        source_ingress = SourceSnapshotIngressService(adapter.source_store(), artifact_service)
        outcomes = OutcomesService(
            OutcomeAggregateRepository(adapter),
            VersionedAggregateCommandExecutor(adapter, current_authorization),
            current_authorization,
        )
        policy_metadata = validated.policy.public_metadata
        runtime_capabilities = validated.runtime.public_metadata
        if not isinstance(policy_metadata, PolicySchemaMetadata) or not isinstance(runtime_capabilities, RuntimeCapabilities):
            raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.POLICY.value, ProviderCategory.RUNTIME.value))
        provider_contract_identities = tuple(
            ProviderIdentity(category.value, binding.contract.contract_id, binding.contract.version)
            for category in ProviderCategory
            if (binding := validated.binding(category)) is not None
        )
        policy_configuration_identity = _policy_configuration_identity(policy_configuration)
        family_center = FamilyCenterService(
            adapter,
            current_authorization,
            adapter.source_store(),
            artifact_service,
            adapter.worker_store(),
        )
        return DownstreamComposition(
            adapter=adapter,
            runtime_settings=settings,
            current_authorization=current_authorization,
            principal_provider=identity.resolve_principal,
            scope_provider=identity.resolve_scope,
            attention=attention,
            episode_briefs=episode_briefs,
            episode_investigations=episode_investigations,
            rca_materializations=rca_materializations,
            workflow=workflow,
            decision_loop=decision_loop,
            planner=planner,
            handoff=handoff,
            artifact_service=artifact_service,
            source_ingress=source_ingress,
            source_observer=source.observer,
            source_binding=source.expected_binding,
            policy_configuration=policy_configuration,
            policy_schema_metadata=policy_metadata,
            runtime_capabilities=runtime_capabilities,
            provider_abi_id=validated.abi_id,
            provider_abi_version=validated.abi_version,
            provider_contract_identities=provider_contract_identities,
            policy_configuration_identity=policy_configuration_identity,
            family_center=family_center,
            notification_channel=notifications.channel,
            outcomes=outcomes,
        )
    except DownstreamFailure:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
        raise
    except Exception:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
        raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED) from None


def _recovery_identity(policy: object) -> str:
    payload = {
        "policy_id": policy.policy_id,
        "confidence_floor": format(policy.confidence_floor, ".17g"),
        "minimum_eligible_independent_samples": policy.minimum_eligible_independent_samples,
        "expected_context": policy.expected_context,
        "expected_characteristic": policy.expected_characteristic,
        "expected_unit": policy.expected_unit,
        "affirmative_outcome": policy.affirmative_outcome.value,
        "require_reference_valid": policy.require_reference_valid,
        "require_capability_valid": policy.require_capability_valid,
        "max_observation_age_microseconds": int(policy.max_observation_age.total_seconds() * 1_000_000),
        "max_availability_delay_microseconds": int(policy.max_availability_delay.total_seconds() * 1_000_000),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _policy_configuration_identity(policy: PolicyConfiguration) -> str:
    payload = {
        "schema_id": policy.schema_id,
        "version": policy.version,
        "check_catalog_identity": policy.check_catalog.identity,
        "planner_policy_identity": policy.planner_policy.identity,
        "recovery_policy_identity": _recovery_identity(policy.recovery_policy),
        "family_contexts": [
            {
                "family_id": family.family_id,
                "version": family.version,
                "contexts": [context.as_dict() for context in family.contexts],
            }
            for family in policy.family_contexts
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


__all__ = ["DownstreamComposition", "compose_downstream"]
