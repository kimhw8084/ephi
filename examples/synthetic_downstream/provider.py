"""Synthetic downstream implementation using public EPHI imports only.

This package is architecture evidence. It contains no company identity,
source schema, endpoint, secret, workflow implementation, or production SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import tempfile
from pathlib import Path

from ephi import (
    AccessScope,
    Principal,
    RecoveryPolicy,
)
from ephi.application import (
    ArtifactBlobStore,
    ArtifactCatalog,
    CheckExecutionMode,
    CheckTemplate,
    CheckTemplateCatalog,
    DELIVERY_POLICY_VERSION,
    DeliveryChannelAdapter,
    DeliveryResult,
    DisruptionClass,
    EffortBand,
    MetrologyObservation,
    MetrologySourceBinding,
    PairWeight,
    PlannerPolicy,
    RecipientResolution,
    RecipientResolver,
    TargetContext,
)
from ephi.downstream import (
    ABI_ID,
    ABI_VERSION,
    ArtifactProvider,
    FamilyContextConfiguration,
    PolicyConfiguration,
    PolicySchemaMetadata,
    ProviderBinding,
    ProviderBundle,
    ProviderCategory,
    ProviderContract,
    RuntimeCapabilities,
    RuntimeProvider,
    SourceProviderBinding,
    provider_contract,
)
from ephi.infrastructure import FileArtifactBlobStore, PostgreSQLReferenceTransactionAdapter


_SCOPE_ID = "synthetic-u1-scope"
_FAMILY_ID = "synthetic-u1-family"
_SOURCE_BINDING = MetrologySourceBinding(
    AccessScope(_SCOPE_ID, site_id="synthetic-site", family_id=_FAMILY_ID),
    source_id="synthetic-source",
    provider_id="synthetic-observer",
    family_id=_FAMILY_ID,
    capability_id="synthetic-capability",
    adapter_id="examples.synthetic_downstream.provider:SyntheticObserver",
    schema_id="synthetic-canonical-observation.v1",
    mapping_version="1.0.0",
    mapping_hash="0" * 64,
    unit="um",
    required_identifiers=("asset_id", "context_id", "characteristic_id"),
)
_CAPABILITIES = frozenset(
    {
        "ephi.attention.read",
        "ephi.episode.read",
        "ephi.episode.claim",
        "ephi.episode.acknowledge",
        "ephi.source.ingest",
        "ephi.source.read",
        "ephi.source.artifact.read",
        "synthetic.artifact.write",
        "synthetic.artifact.read",
        "ephi.decision_loop.read",
        "ephi.decision_loop.create",
        "ephi.decision_loop.check.request",
        "ephi.decision_loop.check.execute",
        "ephi.decision_loop.action.record",
        "ephi.decision_loop.recovery.plan",
        "ephi.decision_loop.recovery.observe",
        "ephi.decision_loop.closure",
        "ephi.decision_loop.reopen",
        "decision_snapshot.create",
        "decision_snapshot.read",
        "handoff.create",
        "handoff.read",
        "handoff.delivery.dispatch",
        "handoff.delivery.reconcile",
    }
)


class SyntheticIdentityProvider:
    """Operation-time identity fixture; browser input is not consulted."""

    def resolve_scope(self) -> AccessScope:
        return AccessScope(
            os.environ.get("EPHI_SYNTHETIC_SCOPE_ID", _SCOPE_ID),
            site_id="synthetic-site",
            family_id=_FAMILY_ID,
        )

    def resolve_principal(self) -> Principal:
        scope = self.resolve_scope()
        subject = os.environ.get("EPHI_SYNTHETIC_SUBJECT", "synthetic-engineer")
        capabilities = tuple(
            item for item in sorted(_CAPABILITIES) if item not in set(os.environ.get("EPHI_SYNTHETIC_REVOKED_CAPABILITIES", "").split(","))
        )
        session_revision = os.environ.get("EPHI_SYNTHETIC_AUTH_SESSION_REVISION", "1")
        security_revision = os.environ.get("EPHI_SYNTHETIC_SECURITY_REVISION", "1")
        return Principal(subject, capabilities, (scope,), session_revision, security_revision)

    def resolve_current_principal(self, subject: str) -> Principal:
        # The requested subject is intentionally not substituted into current
        # authority: CurrentAuthorizationAuthority compares both identities.
        return self.resolve_principal()


class SyntheticObserver:
    """One fixed bounded observation; it exposes no manufacturing commands."""

    def __init__(self) -> None:
        self._binding = _SOURCE_BINDING

    def describe(self) -> MetrologySourceBinding:
        return self._binding

    def read_partition(self, *, start_at: object, end_at: object, limit: int) -> tuple[MetrologyObservation, ...]:
        if not isinstance(start_at, datetime) or not isinstance(end_at, datetime):
            raise ValueError("bounded interval required")
        if start_at.tzinfo is None or end_at.tzinfo is None or start_at > end_at or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("bounded interval required")
        event_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        if not start_at <= event_at <= end_at:
            return ()
        return (
            MetrologyObservation(
                source_row_id="synthetic-row-1",
                asset_id="synthetic-asset",
                tool_id=None,
                head_id=None,
                context_id="synthetic-context",
                characteristic_id="synthetic-characteristic",
                unit="um",
                value=1.25,
                event_at=event_at,
                source_available_at=event_at,
            ),
        )[:limit]


class SyntheticRecipientResolver:
    def resolve(self, scope: AccessScope, selector: str) -> RecipientResolution:
        if scope.scope_id != _SCOPE_ID or selector != "synthetic-engineer":
            raise PermissionError("synthetic recipient unavailable")
        return RecipientResolution(selector, "synthetic-engineer", "synthetic", DELIVERY_POLICY_VERSION)


class SyntheticDeliveryChannel:
    def send(self, resolution, payload, idempotency_key: str) -> DeliveryResult:
        return DeliveryResult("DELIVERED", external_reference=f"synthetic:{idempotency_key}")

    def reconcile(self, resolution, idempotency_key: str, external_reference: str | None) -> DeliveryResult:
        return DeliveryResult("DELIVERED", external_reference=external_reference or f"synthetic:{idempotency_key}")


@dataclass(frozen=True, slots=True)
class SyntheticNotifications:
    recipients: RecipientResolver
    channel: DeliveryChannelAdapter


class SyntheticArtifacts:
    def __init__(self) -> None:
        root = os.environ.get(
            "EPHI_SYNTHETIC_ARTIFACT_ROOT",
            str(Path(tempfile.gettempdir()) / "ephi-u1-synthetic-artifacts"),
        )
        self._root = root

    def blob_store(self) -> ArtifactBlobStore:
        return FileArtifactBlobStore(self._root)

    def catalog_for(self, postgresql_adapter: object) -> ArtifactCatalog:
        if type(postgresql_adapter) is not PostgreSQLReferenceTransactionAdapter:
            raise TypeError("the current PostgreSQL catalog authority is required")
        return postgresql_adapter.artifact_catalog()


class SyntheticPolicy:
    def __init__(self) -> None:
        context = TargetContext(
            target_identity="synthetic-target",
            context_identity="synthetic-context",
            unit_identity="um",
            characteristic_identity="synthetic-characteristic",
        )
        template = CheckTemplate(
            template_id="synthetic-check",
            version="1.0.0",
            title="Synthetic bounded check",
            family_ids=(_FAMILY_ID,),
            target_kinds=("asset",),
            supported_contexts=("synthetic-context",),
            supported_units=("um",),
            context_independent=False,
            unit_independent=False,
            required_capabilities=(),
            candidate_discrimination=(),
            prerequisites=(),
            redundant_with=(),
            redundancy_group_ids=(),
            evidence_group_ids=(),
            effort_band=EffortBand.LOW,
            effort_source_id="synthetic-effort",
            turnaround_source_id="synthetic-turnaround",
            disruption=DisruptionClass.NONE,
            approval_capability="synthetic.measurement.approve",
            execution_mode=CheckExecutionMode.REQUEST_HUMAN_MEASUREMENT,
            result_schema_identity="synthetic-result.v1",
            interpretation_schema_identity="synthetic-interpretation.v1",
            evidence_quality_requirements=(),
            qualification_identity="synthetic-only-not-qualified",
        )
        self._configuration = PolicyConfiguration(
            "org.ephi.policy-configuration",
            "1.0.0",
            CheckTemplateCatalog("synthetic-checks", "1.0.0", (template,)),
            PlannerPolicy("synthetic-planner", "1.0.0", (PairWeight("synthetic-pair", 1),)),
            RecoveryPolicy.deterministic_w0_regression(),
            (FamilyContextConfiguration(_FAMILY_ID, "1.0.0", (context,)),),
        )

    @property
    def configuration(self) -> PolicyConfiguration:
        return self._configuration


class SyntheticRuntime:
    def __init__(self) -> None:
        self._capabilities = RuntimeCapabilities(
            os.environ.get("EPHI_ENV", "development").lower(),
            18,
            "1.0.0",
        )

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    def open_postgresql(self) -> PostgreSQLReferenceTransactionAdapter:
        dsn = os.environ.get("EPHI_TEST_POSTGRES_DSN") or os.environ.get("EPHI_POSTGRES_DSN")
        if not dsn:
            raise RuntimeError("synthetic PostgreSQL binding is unavailable")
        return PostgreSQLReferenceTransactionAdapter(dsn)


def build_bundle() -> ProviderBundle:
    """Return the same explicit bundle shape used by private integrations."""

    identity = SyntheticIdentityProvider()
    source = SourceProviderBinding(_SOURCE_BINDING, SyntheticObserver())
    artifacts = SyntheticArtifacts()
    notifications = SyntheticNotifications(SyntheticRecipientResolver(), SyntheticDeliveryChannel())
    policy = SyntheticPolicy()
    runtime = SyntheticRuntime()
    return ProviderBundle(
        ABI_ID,
        ABI_VERSION,
        identity=ProviderBinding(provider_contract(ProviderCategory.IDENTITY), identity),
        source=ProviderBinding(provider_contract(ProviderCategory.SOURCE), source),
        artifacts=ProviderBinding(provider_contract(ProviderCategory.ARTIFACTS), artifacts),
        notifications=ProviderBinding(provider_contract(ProviderCategory.NOTIFICATIONS), notifications),
        policy=ProviderBinding(
            provider_contract(ProviderCategory.POLICY),
            policy,
            public_metadata=PolicySchemaMetadata(
                policy.configuration.schema_id,
                policy.configuration.version,
            ),
        ),
        runtime=ProviderBinding(
            provider_contract(ProviderCategory.RUNTIME),
            runtime,
            public_metadata=runtime.capabilities,
        ),
    )
