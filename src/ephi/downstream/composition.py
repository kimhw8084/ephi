"""Explicit composition of public downstream providers with EPHI authorities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ephi.application import (
    AccessScope,
    ArtifactService,
    AttentionQueryService,
    CurrentAuthorizationAuthority,
    DecisionLoopCommandService,
    DecisionSnapshotHandoffService,
    EpisodeBriefQueryService,
    EpisodeWorkflowCommandService,
    MetrologySourceBinding,
    NextCheckPlannerService,
    Principal,
    DeliveryChannelAdapter,
    SourceSnapshotIngressService,
)
from ephi.config import RuntimeSettings
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter

from .contracts import (
    BoundedMetrologyObserver,
    DownstreamFailure,
    DownstreamReasonCode,
    PolicyConfiguration,
    ProviderBundle,
    ProviderCategory,
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
    workflow: EpisodeWorkflowCommandService
    decision_loop: DecisionLoopCommandService
    planner: NextCheckPlannerService
    handoff: DecisionSnapshotHandoffService
    artifact_service: ArtifactService
    source_ingress: SourceSnapshotIngressService
    source_observer: BoundedMetrologyObserver
    source_binding: MetrologySourceBinding
    policy_configuration: PolicyConfiguration
    notification_channel: DeliveryChannelAdapter

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
        handoff = DecisionSnapshotHandoffService(
            adapter,
            current_authorization,
            worker=adapter.worker_store(),
            recipients=notifications.recipients,
        )
        source_ingress = SourceSnapshotIngressService(adapter.source_store(), artifact_service)
        policy_configuration = policy_provider.configuration
        # This second, inexpensive type check keeps property-based provider
        # implementations from changing policy identity after preflight.
        if not isinstance(policy_configuration, PolicyConfiguration):
            raise DownstreamFailure(DownstreamReasonCode.COMPOSITION_FAIL_CLOSED, categories=(ProviderCategory.POLICY.value,))

        return DownstreamComposition(
            adapter=adapter,
            runtime_settings=settings,
            current_authorization=current_authorization,
            principal_provider=identity.resolve_principal,
            scope_provider=identity.resolve_scope,
            attention=attention,
            episode_briefs=episode_briefs,
            workflow=workflow,
            decision_loop=decision_loop,
            planner=planner,
            handoff=handoff,
            artifact_service=artifact_service,
            source_ingress=source_ingress,
            source_observer=source.observer,
            source_binding=source.expected_binding,
            policy_configuration=policy_configuration,
            notification_channel=notifications.channel,
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


__all__ = ["DownstreamComposition", "compose_downstream"]
