"""Restart-safe RCA materialization over EPHI's worker and artifact ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import json

from .artifacts import (
    ArtifactContentIdentity,
    ArtifactService,
    ScopedArtifactReference,
)
from .context import AccessScope, CurrentAuthorizationAuthority, Principal
from .errors import CoherentReadConflictError, NoEligibleJobError, ValidationFailureError
from .hashing import canonical_json
from .rca import (
    RCA_READ_CAPABILITY,
    RcaAnalysisService,
    RcaCurrentFacts,
    RcaQuery,
    RcaResult,
    RcaState,
)
from .worker import JobRecord, WorkerJobPort


RCA_MATERIALIZATION_JOB_TYPE = "ephi.rca.observational.materialize.v1"
RCA_MATERIALIZATION_AGGREGATE_TYPE = "rca_materialization"
RCA_MATERIALIZATION_WRITE_CAPABILITY = "ephi.rca.materialize.write"
_EFFECT_KEY = "publish-immutable-rca-result-v1"


class RcaMaterializationState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class RcaMaterializationView:
    query_identity: str
    state: RcaMaterializationState
    job_identity: str | None
    result_identity: str | None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query_identity, str) or not self.query_identity:
            raise ValidationFailureError("RCA materialization query identity is required")
        if not isinstance(self.state, RcaMaterializationState):
            raise ValidationFailureError("RCA materialization state is unsupported")
        if self.job_identity is not None and (not isinstance(self.job_identity, str) or not self.job_identity):
            raise ValidationFailureError("RCA materialization job identity is invalid")
        if self.result_identity is not None and (not isinstance(self.result_identity, str) or not self.result_identity):
            raise ValidationFailureError("RCA materialization result identity is invalid")
        if self.reason_code is not None and (not self.reason_code.isupper() or len(self.reason_code) > 64):
            raise ValidationFailureError("RCA materialization reason code is invalid")

    def as_dict(self) -> dict[str, str | None]:
        return {
            "query_identity": self.query_identity,
            "state": self.state.value,
            "job_identity": self.job_identity,
            "result_identity": self.result_identity,
            "reason_code": self.reason_code,
        }


class RcaMaterializationCoordinator:
    """Queue bounded expensive RCA and publish exact results as immutable artifacts.

    The generic aggregate row stores only status and artifact identity. Worker
    fencing and applied-effect idempotency protect that pointer; artifact bytes
    are content-addressed and retain the exact Episode revision identity.
    """

    def __init__(
        self,
        worker: WorkerJobPort,
        aggregate_store: object,
        artifacts: ArtifactService,
        current_authorization: CurrentAuthorizationAuthority,
        analysis: RcaAnalysisService,
    ) -> None:
        if not isinstance(worker, WorkerJobPort):
            raise TypeError("RCA materialization requires the existing durable worker port")
        if not all(
            callable(getattr(aggregate_store, method, None)) for method in ("seed_aggregate", "get_aggregate")
        ):
            raise TypeError("RCA materialization requires the existing generic aggregate store")
        if not isinstance(artifacts, ArtifactService):
            raise TypeError("RCA materialization requires the existing artifact service")
        if not isinstance(current_authorization, CurrentAuthorizationAuthority) or not isinstance(analysis, RcaAnalysisService):
            raise TypeError("RCA materialization requires current authorization and RCA authorities")
        self.worker = worker
        self.aggregate_store = aggregate_store
        self.artifacts = artifacts
        self.current_authorization = current_authorization
        self.analysis = analysis

    @staticmethod
    def _aggregate_id(query: RcaQuery) -> str:
        return query.identity

    def _authorize(self, principal: Principal, query: RcaQuery) -> None:
        self.current_authorization.authorize(principal, query.scope, RCA_READ_CAPABILITY)

    def _aggregate(self, principal: Principal, query: RcaQuery):
        self._authorize(principal, query)
        aggregate = self.aggregate_store.get_aggregate(
            query.scope, RCA_MATERIALIZATION_AGGREGATE_TYPE, self._aggregate_id(query)
        )
        if aggregate is None:
            return None
        state = aggregate.state
        if state.get("query_identity") != query.identity or state.get("revision_identity") != query.analytical_revision_identity:
            raise CoherentReadConflictError("RCA materialization record does not match the exact requested input")
        return aggregate

    def _job(self, principal: Principal, query: RcaQuery, job_identity: str) -> JobRecord | None:
        self._authorize(principal, query)
        rows = self.worker.inspect(query.scope, job_id=job_identity, limit=1)
        if not rows:
            return None
        job = rows[0]
        if job.scope_key != query.scope.canonical_key or job.job_type != RCA_MATERIALIZATION_JOB_TYPE or job.semantic_key != query.identity:
            raise CoherentReadConflictError("RCA materialization job identity does not match its authorized query")
        return job

    def status(self, principal: Principal, query: RcaQuery) -> RcaMaterializationView | None:
        aggregate = self._aggregate(principal, query)
        if aggregate is None:
            return None
        state = aggregate.state
        job_identity = state.get("job_identity")
        status = RcaMaterializationState(state["state"])
        job = self._job(principal, query, job_identity) if isinstance(job_identity, str) else None
        if status in {RcaMaterializationState.PENDING, RcaMaterializationState.RUNNING} and job is not None:
            if job.status in {"QUEUED", "PENDING"}:
                status = RcaMaterializationState.PENDING
            elif job.status == "RUNNING":
                status = RcaMaterializationState.RUNNING
            elif job.status in {"FAILED", "DEAD_LETTER", "CANCELED"}:
                status = RcaMaterializationState.STALE if job.last_failure_code == "RCA_STALE" else RcaMaterializationState.FAILED
        return RcaMaterializationView(
            query.identity, status, job_identity,
            state.get("result_identity") if isinstance(state.get("result_identity"), str) else None,
            state.get("reason_code") if isinstance(state.get("reason_code"), str) else None,
        )

    def enqueue(self, principal: Principal, query: RcaQuery) -> RcaMaterializationView:
        self._authorize(principal, query)
        self.current_authorization.authorize(principal, query.scope, RCA_MATERIALIZATION_WRITE_CAPABILITY)
        existing = self.status(principal, query)
        if existing is not None:
            return existing
        job = self.worker.enqueue(
            query.scope,
            RCA_MATERIALIZATION_JOB_TYPE,
            query.identity,
            {"query": query.as_dict()},
            priority=0,
            max_attempts=3,
        )
        # An item becomes claimable in the existing queue before its generic
        # status aggregate is inserted. A concurrently claiming worker can only
        # retry a missing target; no partial result can be published.
        try:
            self.aggregate_store.seed_aggregate(
                query.scope,
                RCA_MATERIALIZATION_AGGREGATE_TYPE,
                self._aggregate_id(query),
                {
                    "query_identity": query.identity,
                    "episode_identity": query.episode_identity,
                    "revision_identity": query.analytical_revision_identity,
                    "workflow_version": query.workflow_version,
                    "active_cycle_identity": query.active_cycle_identity,
                    "knowledge_cutoff": query.knowledge_cutoff,
                    "source_identities": list(query.source_identities),
                    "policy_identity": query.policy_identity,
                    "schema_identity": query.schema_identity,
                    "job_identity": job.job_id,
                    "state": "PENDING",
                },
            )
        except Exception:
            aggregate = self._aggregate(principal, query)
            if aggregate is None or aggregate.state.get("job_identity") != job.job_id:
                raise
        return RcaMaterializationView(query.identity, RcaMaterializationState.PENDING, job.job_id, None)

    def read_result(
        self,
        principal: Principal,
        query: RcaQuery,
        *,
        load_current_facts: Callable[[], RcaCurrentFacts],
    ) -> RcaResult | None:
        self._authorize(principal, query)
        aggregate = self._aggregate(principal, query)
        if aggregate is None or aggregate.state.get("state") != RcaMaterializationState.READY.value:
            return None
        current = load_current_facts()
        self.analysis.validate_current_facts(query, current)
        state = aggregate.state
        reference = ScopedArtifactReference(
            query.scope,
            ArtifactContentIdentity(state.get("artifact_sha256"), state.get("artifact_byte_size")),
        )
        verified = self.artifacts.retrieve(principal, reference, RCA_READ_CAPABILITY)
        if verified.metadata.producing_job_id != state.get("job_identity") or verified.metadata.revision_id != query.analytical_revision_identity:
            raise CoherentReadConflictError("materialized RCA artifact is bound to a different job or Episode revision")
        try:
            document = json.loads(verified.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailureError("materialized RCA artifact is malformed") from exc
        if not isinstance(document, Mapping):
            raise ValidationFailureError("materialized RCA result must be a typed object")
        result = RcaResult.from_dict(document)
        if (
            result.query_identity != query.identity
            or result.result_identity != state.get("result_identity")
            or result.episode_identity != query.episode_identity
            or result.analytical_revision_identity != query.analytical_revision_identity
            or result.workflow_version != query.workflow_version
            or result.active_cycle_identity != query.active_cycle_identity
            or result.knowledge_cutoff != query.knowledge_cutoff
            or result.source_identities != query.source_identities
            or result.policy_identity != query.policy_identity
            or result.schema_identity != query.schema_identity
        ):
            raise CoherentReadConflictError("materialized RCA result does not match the exact authorized query")
        return result

    def _commit_status(
        self,
        job: JobRecord,
        query: RcaQuery,
        *,
        state: RcaMaterializationState,
        reason_code: str | None = None,
        result: RcaResult | None = None,
        artifact: tuple[str, int] | None = None,
    ) -> None:
        values: dict[str, object] = {
            "query_identity": query.identity,
            "state": state.value,
            "reason_code": reason_code,
            "result_identity": result.result_identity if result is not None else None,
            "input_identity": result.input_identity if result is not None else None,
            "artifact_sha256": artifact[0] if artifact is not None else None,
            "artifact_byte_size": artifact[1] if artifact is not None else None,
            "job_identity": job.job_id,
        }

        def mutation(current: Mapping[str, object], payload: Mapping[str, object]) -> Mapping[str, object]:
            if current.get("query_identity") != query.identity or current.get("revision_identity") != query.analytical_revision_identity:
                raise CoherentReadConflictError("RCA materialization record changed before its fenced result commit")
            return {**current, **payload}

        self.worker.commit_local_effect(
            job.lease,
            _EFFECT_KEY,
            values,
            aggregate_type=RCA_MATERIALIZATION_AGGREGATE_TYPE,
            aggregate_id=query.identity,
            mutation=mutation,
        )

    def process_one(
        self,
        principal: Principal,
        scope: AccessScope,
        owner: str,
        *,
        load_current_facts: Callable[[Principal, RcaQuery], RcaCurrentFacts],
    ) -> RcaMaterializationView | None:
        # Queue status/existence is protected by the exact scope authorization.
        self.current_authorization.authorize(principal, scope, RCA_READ_CAPABILITY)
        job = self.worker.claim(scope, owner, job_type=RCA_MATERIALIZATION_JOB_TYPE)
        if job is None:
            return None
        try:
            payload = job.payload.get("query")
            if not isinstance(payload, Mapping):
                raise ValidationFailureError("RCA materialization job payload is malformed")
            query = RcaQuery.from_dict(payload)
            if query.scope != scope or query.identity != job.semantic_key:
                raise CoherentReadConflictError("RCA materialization job does not match its queue scope or semantic identity")
            self._authorize(principal, query)
            facts = load_current_facts(principal, query)
            result = self.analysis.analyze_materialized(principal, query, load_current_facts=lambda: facts)
            if result.state == RcaState.MATERIALIZATION_REQUIRED:
                raise ValidationFailureError("RCA facts exceed the durable materialization input contract")
            current_after = load_current_facts(principal, query)
            self.analysis.validate_current_facts(query, current_after)
            content = canonical_json(result.as_dict()).encode("utf-8")
            artifact = self.artifacts.write_and_register(
                principal,
                scope,
                content,
                media_type="application/json",
                logical_purpose="ephi.rca.observational.v1",
                required_write_capability=RCA_MATERIALIZATION_WRITE_CAPABILITY,
                producing_job_id=job.job_id,
                revision_id=query.analytical_revision_identity,
            )
            reference = artifact.metadata.reference
            self._commit_status(
                job, query, state=RcaMaterializationState.READY, result=result,
                artifact=(reference.content.sha256, reference.content.byte_size),
            )
            self.worker.complete(job.lease)
            return RcaMaterializationView(query.identity, RcaMaterializationState.READY, job.job_id, result.result_identity)
        except CoherentReadConflictError:
            try:
                if "query" in locals() and isinstance(query, RcaQuery):
                    self._commit_status(job, query, state=RcaMaterializationState.STALE, reason_code="EXACT_VIEW_ADVANCED")
                self.worker.fail(job.lease, retryable=False, error_code="RCA_STALE", error_message="RCA inputs no longer match the requested Episode view")
            except Exception:
                pass
            if "query" in locals() and isinstance(query, RcaQuery):
                return RcaMaterializationView(query.identity, RcaMaterializationState.STALE, job.job_id, None, "EXACT_VIEW_ADVANCED")
            return None
        except Exception:
            try:
                self.worker.fail(job.lease, retryable=True, error_code="RCA_MATERIALIZATION_FAILED", error_message="RCA materialization could not be completed")
            except Exception:
                pass
            if "query" in locals() and isinstance(query, RcaQuery):
                return RcaMaterializationView(query.identity, RcaMaterializationState.FAILED, job.job_id, None, "MATERIALIZATION_FAILED")
            return None


__all__ = [
    "RCA_MATERIALIZATION_AGGREGATE_TYPE", "RCA_MATERIALIZATION_JOB_TYPE",
    "RCA_MATERIALIZATION_WRITE_CAPABILITY", "RcaMaterializationCoordinator",
    "RcaMaterializationState", "RcaMaterializationView",
]
