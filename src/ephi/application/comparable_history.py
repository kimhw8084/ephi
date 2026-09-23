"""Bounded O6.2 comparable-case reads over immutable Episode revisions.

This module defines retrieval and exact structured-comparison contracts only.
It does not infer root cause, causal equivalence, outcome probability, or real
family qualification. Feature values are retained as digests and are never
returned to the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import re
from typing import Any, Protocol, runtime_checkable

from .context import AccessScope, CurrentAuthorizationAuthority, Principal
from .errors import (
    AuthorizationDeniedError,
    CoherentReadConflictError,
    QueryCursorValidationError,
    QueryIdentityMismatchError,
    QueryTooBroadError,
    ReadRevisionNotFoundError,
    ValidationFailureError,
)
from .hashing import canonical_json
from .read import (
    DEFAULT_SNAPSHOT_TTL_SECONDS,
    MAX_PAGE_SIZE,
    MAX_SNAPSHOT_TTL_SECONDS,
    PageResult,
    ReadRevision,
    ReadSnapshotStore,
    RetainedSnapshotRow,
    VersionedReadRow,
)


COMPARABLE_HISTORY_READ_CAPABILITY = "ephi.comparable_history.read"
COMPARABLE_PROFILE_KEY = "comparable_case"
COMPARABLE_PROFILE_SCHEMA = "o6.2.v1"
COMPARABLE_FINGERPRINT_VERSION = "exact-structured.v1"
COMPARABLE_POLICY_ID = "ephi.comparable.exact-structured"
COMPARABLE_POLICY_VERSION = "1"
MAX_COMPARABLE_CANDIDATE_LIMIT = 100
MAX_COMPARABLE_QUERY_MILLISECONDS = 500

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_.:-]{0,63}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValidationFailureError(f"{field} must be a non-empty canonical identity")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValidationFailureError(f"{field} must be a lowercase SHA-256 identity")
    return value


def _aware_datetime(value: object, field: str) -> datetime:
    if isinstance(value, Mapping) and set(value) == {"$datetime"}:
        value = value["$datetime"]
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationFailureError(f"{field} must be a timezone-aware timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailureError(f"{field} must be a timezone-aware timestamp")
    return value.astimezone(timezone.utc)


def _safe_code(value: object, field: str) -> str:
    if not isinstance(value, str) or _SAFE_CODE.fullmatch(value) is None:
        raise ValidationFailureError(f"{field} must be a bounded uppercase code")
    return value


def _opaque_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise ValidationFailureError(f"{field} must be a bounded opaque identity")
    return value


class EligibilityState(str, Enum):
    QUALIFIED = "QUALIFIED"
    LIMITED = "LIMITED"
    UNQUALIFIED = "UNQUALIFIED"


class CurationState(str, Enum):
    CURATED = "CURATED"
    LIMITED = "LIMITED"
    UNCURATED = "UNCURATED"


class HistoricalClaimType(str, Enum):
    ROOT_CAUSE = "ROOT_CAUSE"
    ACTION_SUCCESS = "ACTION_SUCCESS"
    OUTCOME = "OUTCOME"


class ComparableQueryState(str, Enum):
    READY = "READY"
    MATERIALIZATION_REQUIRED = "MATERIALIZATION_REQUIRED"


class ComparableHistoryMaterializationRequired(Exception):
    """The bounded durable history read exceeded its synchronous time cap."""


@dataclass(frozen=True, slots=True)
class HistoricalSourceIdentity:
    """Exact immutable O4 source manifest and artifact identity; no path."""

    snapshot_id: str
    source_revision: str
    manifest_hash: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        for field in ("snapshot_id", "source_revision"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        for field in ("manifest_hash", "artifact_sha256"):
            object.__setattr__(self, field, _digest(getattr(self, field), field))

    def as_dict(self) -> dict[str, str]:
        return {
            "snapshot_id": self.snapshot_id,
            "source_revision": self.source_revision,
            "manifest_hash": self.manifest_hash,
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass(frozen=True, slots=True)
class HistoricalSourceFacts:
    """Same-scope durable source facts joined from ``source_snapshot``."""

    identity: HistoricalSourceIdentity
    family_identity: str
    available_at: datetime
    known_at: datetime
    status: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, HistoricalSourceIdentity):
            raise ValidationFailureError("source facts require an immutable source identity")
        object.__setattr__(self, "family_identity", _identity(self.family_identity, "source family identity"))
        object.__setattr__(self, "available_at", _aware_datetime(self.available_at, "source available_at"))
        object.__setattr__(self, "known_at", _aware_datetime(self.known_at, "source known_at"))
        if self.status not in {"PUBLISHED", "PARTIAL", "INSUFFICIENT", "QUARANTINED"}:
            raise ValidationFailureError("source status is not a supported immutable manifest state")


@dataclass(frozen=True, slots=True)
class ComparableCaseRevisionRecord:
    """One immutable read revision and its same-scope source-manifest join."""

    revision: ReadRevision
    source_facts: HistoricalSourceFacts | None

    def __post_init__(self) -> None:
        if not isinstance(self.revision, ReadRevision):
            raise ValidationFailureError("comparable history rows require immutable read revisions")
        if self.source_facts is not None and not isinstance(self.source_facts, HistoricalSourceFacts):
            raise ValidationFailureError("source_facts must be immutable source facts or None")


@runtime_checkable
class ComparableCaseHistorySource(Protocol):
    """Bounded extension of the existing immutable Episode read authority."""

    def fetch_episode_history_window(
        self,
        principal: Principal,
        scope: AccessScope,
        current_episode_id: str,
        known_by: datetime,
        *,
        limit: int,
        required_read_capability: str,
    ) -> Sequence[ComparableCaseRevisionRecord]: ...

    def fetch_comparable_source_facts(
        self,
        principal: Principal,
        scope: AccessScope,
        identity: HistoricalSourceIdentity,
        required_read_capability: str,
    ) -> HistoricalSourceFacts | None: ...


@dataclass(frozen=True, slots=True)
class FingerprintFeature:
    """A typed feature identity plus a digest of its private exact value."""

    feature_id: str
    value_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", _opaque_id(self.feature_id, "feature_id"))
        object.__setattr__(self, "value_sha256", _digest(self.value_sha256, "feature value identity"))

    def as_dict(self) -> dict[str, str]:
        return {"feature_id": self.feature_id, "value_sha256": self.value_sha256}


@dataclass(frozen=True, slots=True)
class ExactStructuredFingerprint:
    """Versioned exact categorical comparisons; raw feature values stay private."""

    version: str
    features: tuple[FingerprintFeature, ...]

    def __post_init__(self) -> None:
        if self.version != COMPARABLE_FINGERPRINT_VERSION:
            raise ValidationFailureError("unsupported structured fingerprint version")
        if not isinstance(self.features, tuple) or not self.features or any(
            not isinstance(feature, FingerprintFeature) for feature in self.features
        ):
            raise ValidationFailureError("fingerprints require a non-empty tuple of typed features")
        ordered = tuple(sorted(self.features, key=lambda item: item.feature_id))
        if len({item.feature_id for item in ordered}) != len(ordered):
            raise ValidationFailureError("fingerprint feature identities must be unique")
        object.__setattr__(self, "features", ordered)

    @property
    def identity(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict()).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {"version": self.version, "features": [item.as_dict() for item in self.features]}


@dataclass(frozen=True, slots=True)
class HistoricalClaim:
    """A bounded historical assertion with its own evidence and curation IDs."""

    claim_type: HistoricalClaimType
    claim_identity: str
    evidence_identity: str
    curation_identity: str
    curation_state: CurationState
    known_at: datetime
    available_at: datetime
    outcome_maturity: str | None = None
    outcome_cutoff: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.claim_type, HistoricalClaimType):
            raise ValidationFailureError("historical claim type is invalid")
        for field in ("claim_identity", "evidence_identity", "curation_identity"):
            object.__setattr__(self, field, _opaque_id(getattr(self, field), field))
        if not isinstance(self.curation_state, CurationState):
            raise ValidationFailureError("historical claim curation state is invalid")
        object.__setattr__(self, "known_at", _aware_datetime(self.known_at, "claim known_at"))
        object.__setattr__(self, "available_at", _aware_datetime(self.available_at, "claim available_at"))
        if self.claim_type == HistoricalClaimType.OUTCOME:
            if self.outcome_maturity not in {"IMMATURE", "MATURE", "INDETERMINATE"} or self.outcome_cutoff is None:
                raise ValidationFailureError("outcome claims require explicit maturity and outcome cutoff")
            object.__setattr__(self, "outcome_cutoff", _aware_datetime(self.outcome_cutoff, "outcome cutoff"))
        elif self.outcome_maturity is not None or self.outcome_cutoff is not None:
            raise ValidationFailureError("only outcome claims may carry maturity or outcome cutoff")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "HistoricalClaim":
        return cls(
            HistoricalClaimType(value.get("claim_type")),
            value.get("claim_identity"),
            value.get("evidence_identity"),
            value.get("curation_identity"),
            CurationState(value.get("curation_state")),
            _aware_datetime(value.get("known_at"), "claim known_at"),
            _aware_datetime(value.get("available_at"), "claim available_at"),
            value.get("outcome_maturity"),
            None if value.get("outcome_cutoff") is None else _aware_datetime(value.get("outcome_cutoff"), "outcome cutoff"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "claim_type": self.claim_type.value,
            "claim_identity": self.claim_identity,
            "evidence_identity": self.evidence_identity,
            "curation_identity": self.curation_identity,
            "curation_state": self.curation_state.value,
            "known_at": self.known_at,
            "available_at": self.available_at,
            "outcome_maturity": self.outcome_maturity,
            "outcome_cutoff": self.outcome_cutoff,
        }


@dataclass(frozen=True, slots=True)
class ComparableCaseProfile:
    """O6.2 eligibility and fingerprint envelope embedded in one read revision."""

    family_identity: str
    context_identity: str
    source_identity: HistoricalSourceIdentity
    fingerprint: ExactStructuredFingerprint
    eligibility_state: EligibilityState
    eligibility_identity: str
    qualification_evidence_identity: str | None
    curation_state: CurationState
    curation_evidence_identity: str | None
    data_completeness_limitations: tuple[str, ...]
    claims: tuple[HistoricalClaim, ...]

    def __post_init__(self) -> None:
        for field in ("family_identity", "context_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        object.__setattr__(self, "eligibility_identity", _opaque_id(self.eligibility_identity, "eligibility_identity"))
        if not isinstance(self.source_identity, HistoricalSourceIdentity) or not isinstance(self.fingerprint, ExactStructuredFingerprint):
            raise ValidationFailureError("comparable profile requires immutable source and fingerprint identities")
        if not isinstance(self.eligibility_state, EligibilityState) or not isinstance(self.curation_state, CurationState):
            raise ValidationFailureError("comparable profile eligibility or curation state is invalid")
        for field in ("qualification_evidence_identity", "curation_evidence_identity"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _opaque_id(value, field))
        if self.eligibility_state == EligibilityState.QUALIFIED and self.qualification_evidence_identity is None:
            raise ValidationFailureError("qualified comparison requires its own evidence identity")
        if self.curation_state == CurationState.CURATED and self.curation_evidence_identity is None:
            raise ValidationFailureError("curated comparison requires its curation evidence identity")
        if not isinstance(self.data_completeness_limitations, tuple):
            raise ValidationFailureError("data completeness limitations must be a tuple")
        limitations = tuple(sorted({_safe_code(code, "data completeness limitation") for code in self.data_completeness_limitations}))
        object.__setattr__(self, "data_completeness_limitations", limitations)
        if not isinstance(self.claims, tuple) or any(not isinstance(claim, HistoricalClaim) for claim in self.claims):
            raise ValidationFailureError("claims must be a tuple of typed historical claims")

    @classmethod
    def from_revision(cls, revision: ReadRevision) -> "ComparableCaseProfile":
        raw = revision.payload.get(COMPARABLE_PROFILE_KEY)
        if not isinstance(raw, Mapping) or raw.get("schema") != COMPARABLE_PROFILE_SCHEMA:
            raise ValidationFailureError("Episode revision has no supported comparable-case profile")
        try:
            source = raw["source_identity"]
            fingerprint = raw["fingerprint"]
            if not isinstance(source, Mapping) or not isinstance(fingerprint, Mapping):
                raise TypeError
            source_identity = HistoricalSourceIdentity(
                source["snapshot_id"], source["source_revision"], source["manifest_hash"], source["artifact_sha256"]
            )
            features_raw = fingerprint["features"]
            if not isinstance(features_raw, Sequence) or isinstance(features_raw, (str, bytes)):
                raise TypeError
            features = tuple(FingerprintFeature(item["feature_id"], item["value_sha256"]) for item in features_raw)
            claims_raw = raw.get("claims", ())
            if not isinstance(claims_raw, Sequence) or isinstance(claims_raw, (str, bytes)):
                raise TypeError
            claims: list[HistoricalClaim] = []
            claims_limited = False
            for item in claims_raw:
                if isinstance(item, Mapping):
                    try:
                        claims.append(HistoricalClaim.from_dict(item))
                    except (TypeError, ValueError, ValidationFailureError):
                        # A malformed optional claim is omitted; it can never
                        # turn into an unsupported historical assertion.
                        claims_limited = True
                        continue
                else:
                    claims_limited = True
            limitations = raw.get("data_completeness_limitations", ())
            if not isinstance(limitations, Sequence) or isinstance(limitations, (str, bytes)):
                raise TypeError
            limitation_values = tuple(limitations) + (("HISTORICAL_CLAIM_OMITTED",) if claims_limited else ())
            qualification_evidence = raw.get("qualification_evidence_identity")
            curation_evidence = raw.get("curation_evidence_identity")
            return cls(
                raw["family_identity"],
                raw["context_identity"],
                source_identity,
                ExactStructuredFingerprint(fingerprint["version"], features),
                EligibilityState(raw["eligibility_state"]),
                raw["eligibility_identity"],
                qualification_evidence,
                CurationState(raw["curation_state"]),
                curation_evidence,
                limitation_values,
                tuple(claims),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailureError("comparable-case profile is incomplete or invalid") from exc

    def manifest_dict(self) -> dict[str, object]:
        """Safe identity-only representation used in deterministic result hashes."""

        return {
            "family_identity": self.family_identity,
            "context_identity": self.context_identity,
            "source_identity": self.source_identity.as_dict(),
            "fingerprint_identity": self.fingerprint.identity,
            "eligibility_state": self.eligibility_state.value,
            "eligibility_identity": self.eligibility_identity,
            "qualification_evidence_identity": self.qualification_evidence_identity,
            "curation_state": self.curation_state.value,
            "curation_evidence_identity": self.curation_evidence_identity,
            "data_completeness_limitations": list(self.data_completeness_limitations),
        }


@dataclass(frozen=True, slots=True)
class ComparableRetrievalPolicy:
    """The supported transparent exact-comparison/ranking policy identity."""

    policy_id: str = COMPARABLE_POLICY_ID
    version: str = COMPARABLE_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.policy_id != COMPARABLE_POLICY_ID or self.version not in {"1", "2"}:
            raise ValidationFailureError("unsupported comparable retrieval policy")

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "ranking_components": [
                "exact_match_count_desc",
                "compared_feature_count_desc",
                "material_difference_count_asc",
                *(["revision_known_at_desc"] if self.version == "2" else []),
                "episode_id_asc",
                "revision_id_asc",
            ],
            "limited_candidates": "include_with_visible_limitations",
            "unqualified_candidates": "exclude",
            "scalar_score": "none",
            "query_time_budget_ms": MAX_COMPARABLE_QUERY_MILLISECONDS,
        }

    @property
    def identity(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ComparableCaseQuery:
    """Complete authorized query identity for one current Episode view."""

    scope: AccessScope
    episode_id: str
    cycle_id: str
    workflow_version: int
    revision_id: str
    knowledge_cutoff: datetime
    current_source_identity: HistoricalSourceIdentity
    current_fingerprint_identity: str
    family_identity: str
    context_identity: str
    candidate_limit: int
    policy: ComparableRetrievalPolicy = ComparableRetrievalPolicy()

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AccessScope):
            raise ValidationFailureError("query scope must be an AccessScope")
        for field in ("episode_id", "cycle_id", "revision_id", "family_identity", "context_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        if isinstance(self.workflow_version, bool) or not isinstance(self.workflow_version, int) or self.workflow_version < 0:
            raise ValidationFailureError("workflow_version must be a non-negative integer")
        object.__setattr__(self, "knowledge_cutoff", _aware_datetime(self.knowledge_cutoff, "knowledge_cutoff"))
        if not isinstance(self.current_source_identity, HistoricalSourceIdentity):
            raise ValidationFailureError("query must bind an exact current immutable source identity")
        object.__setattr__(self, "current_fingerprint_identity", _digest(self.current_fingerprint_identity, "current fingerprint identity"))
        if self.scope.family_id is not None and self.scope.family_id != self.family_identity:
            raise ValidationFailureError("query family identity must match the authorized family scope")
        if isinstance(self.candidate_limit, bool) or not isinstance(self.candidate_limit, int) or not 1 <= self.candidate_limit <= MAX_COMPARABLE_CANDIDATE_LIMIT:
            raise QueryTooBroadError("candidate_limit is outside the bounded synchronous contract", limit=MAX_COMPARABLE_CANDIDATE_LIMIT)
        if not isinstance(self.policy, ComparableRetrievalPolicy):
            raise ValidationFailureError("query requires a supported versioned retrieval policy")

    def as_dict(self) -> dict[str, object]:
        return {
            "query": "o6_comparable_case_history",
            "scope": self.scope.as_dict(),
            "episode_id": self.episode_id,
            "cycle_id": self.cycle_id,
            "workflow_version": self.workflow_version,
            "revision_id": self.revision_id,
            "knowledge_cutoff": self.knowledge_cutoff,
            "current_source_identity": self.current_source_identity.as_dict(),
            "current_fingerprint_identity": self.current_fingerprint_identity,
            "family_identity": self.family_identity,
            "context_identity": self.context_identity,
            "candidate_limit": self.candidate_limit,
            "policy": self.policy.as_dict(),
            "policy_identity": self.policy.identity,
            "sort": list(self.policy.as_dict()["ranking_components"]),
        }


@dataclass(frozen=True, slots=True)
class SimilarityComponents:
    """Inspectable exact feature intersections and differences, never a score."""

    shared_exact_feature_ids: tuple[str, ...]
    differing_feature_ids: tuple[str, ...]
    current_only_feature_ids: tuple[str, ...]
    candidate_only_feature_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("shared_exact_feature_ids", "differing_feature_ids", "current_only_feature_ids", "candidate_only_feature_ids"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or tuple(sorted(set(values))) != values:
                raise ValidationFailureError(f"{field} must be a sorted unique tuple")

    @property
    def exact_match_count(self) -> int:
        return len(self.shared_exact_feature_ids)

    @property
    def compared_feature_count(self) -> int:
        return len(self.shared_exact_feature_ids) + len(self.differing_feature_ids)

    @property
    def material_difference_count(self) -> int:
        return len(self.differing_feature_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "interpretation": "exact structured feature matches; descriptive only",
            "shared_exact_feature_ids": list(self.shared_exact_feature_ids),
            "differing_feature_ids": list(self.differing_feature_ids),
            "current_only_feature_ids": list(self.current_only_feature_ids),
            "candidate_only_feature_ids": list(self.candidate_only_feature_ids),
            "exact_match_count": self.exact_match_count,
            "compared_feature_count": self.compared_feature_count,
            "material_difference_count": self.material_difference_count,
        }


@dataclass(frozen=True, slots=True)
class ComparableCase:
    episode_id: str
    revision_id: str
    revision_vector_identity: str
    source_identity: HistoricalSourceIdentity
    family_identity: str
    context_identity: str
    eligibility_state: EligibilityState
    eligibility_identity: str
    qualification_evidence_identity: str | None
    curation_state: CurationState
    curation_evidence_identity: str | None
    revision_known_at: datetime
    revision_published_at: datetime
    source_available_at: datetime
    source_known_at: datetime
    knowledge_cutoff: datetime
    similarity_components: SimilarityComponents
    tie_break_identity: str
    data_completeness_limitations: tuple[str, ...]
    historical_claims: tuple[HistoricalClaim, ...]

    def __post_init__(self) -> None:
        for field in ("episode_id", "revision_id", "revision_vector_identity", "family_identity", "context_identity", "eligibility_identity"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        for field in ("qualification_evidence_identity", "curation_evidence_identity"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _opaque_id(value, field))
        if not isinstance(self.source_identity, HistoricalSourceIdentity):
            raise ValidationFailureError("result requires an exact immutable source identity")
        if not isinstance(self.eligibility_state, EligibilityState) or not isinstance(self.curation_state, CurationState):
            raise ValidationFailureError("result eligibility and curation states must be typed")
        if not isinstance(self.similarity_components, SimilarityComponents):
            raise ValidationFailureError("result requires exact similarity components")
        object.__setattr__(self, "tie_break_identity", _digest(self.tie_break_identity, "tie_break_identity"))
        for field in ("revision_known_at", "revision_published_at", "source_available_at", "source_known_at", "knowledge_cutoff"):
            object.__setattr__(self, field, _aware_datetime(getattr(self, field), field))
        limitations = tuple(sorted({_safe_code(code, "data completeness limitation") for code in self.data_completeness_limitations}))
        object.__setattr__(self, "data_completeness_limitations", limitations)
        if not isinstance(self.historical_claims, tuple) or any(not isinstance(item, HistoricalClaim) for item in self.historical_claims):
            raise ValidationFailureError("historical_claims must contain typed claims")

    def as_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "revision_id": self.revision_id,
            "revision_vector_identity": self.revision_vector_identity,
            "source_identity": self.source_identity.as_dict(),
            "family_identity": self.family_identity,
            "context_identity": self.context_identity,
            "eligibility_state": self.eligibility_state.value,
            "eligibility_identity": self.eligibility_identity,
            "qualification_evidence_identity": self.qualification_evidence_identity,
            "curation_state": self.curation_state.value,
            "curation_evidence_identity": self.curation_evidence_identity,
            "revision_known_at": self.revision_known_at,
            "revision_published_at": self.revision_published_at,
            "source_available_at": self.source_available_at,
            "source_known_at": self.source_known_at,
            "knowledge_cutoff": self.knowledge_cutoff,
            "similarity_components": self.similarity_components.as_dict(),
            "tie_break_identity": self.tie_break_identity,
            "data_completeness_limitations": list(self.data_completeness_limitations),
            "historical_claims": [claim.as_dict() for claim in self.historical_claims],
        }


@dataclass(frozen=True, slots=True)
class ExcludedComparableCandidate:
    episode_id: str
    revision_id: str
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _identity(self.episode_id, "episode_id"))
        object.__setattr__(self, "revision_id", _identity(self.revision_id, "revision_id"))
        codes = tuple(sorted({_safe_code(code, "candidate exclusion reason") for code in self.reason_codes}))
        if not codes:
            raise ValidationFailureError("excluded candidate requires a bounded reason code")
        object.__setattr__(self, "reason_codes", codes)

    def as_dict(self) -> dict[str, object]:
        return {"episode_id": self.episode_id, "revision_id": self.revision_id, "reason_codes": list(self.reason_codes)}


@dataclass(frozen=True, slots=True)
class ComparableCasesPage:
    state: ComparableQueryState
    query_identity: str
    result_identity: str | None
    cases: tuple[ComparableCase, ...]
    excluded_candidates: tuple[ExcludedComparableCandidate, ...]
    total_row_count: int
    snapshot_id: str | None
    next_cursor: str | None
    materialization_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ComparableQueryState):
            raise ValidationFailureError("query result state is invalid")
        object.__setattr__(self, "query_identity", _digest(self.query_identity, "query_identity"))
        if self.result_identity is not None:
            object.__setattr__(self, "result_identity", _digest(self.result_identity, "result_identity"))
        if not isinstance(self.cases, tuple) or any(not isinstance(item, ComparableCase) for item in self.cases):
            raise ValidationFailureError("cases must be a tuple of typed comparable cases")
        if not isinstance(self.excluded_candidates, tuple) or any(not isinstance(item, ExcludedComparableCandidate) for item in self.excluded_candidates):
            raise ValidationFailureError("excluded_candidates must be typed")
        if isinstance(self.total_row_count, bool) or not isinstance(self.total_row_count, int) or self.total_row_count < 0:
            raise ValidationFailureError("total_row_count must be non-negative")
        if self.state == ComparableQueryState.MATERIALIZATION_REQUIRED:
            if self.result_identity is not None or self.cases or self.excluded_candidates or self.total_row_count or self.snapshot_id or self.next_cursor:
                raise ValidationFailureError("materialization-required results cannot disclose a partial population")
            if self.materialization_reason not in {"CANDIDATE_LIMIT_EXCEEDED", "QUERY_TIME_BUDGET_EXCEEDED"}:
                raise ValidationFailureError("materialization-required result needs an explicit bounded reason")
        elif self.result_identity is None or self.snapshot_id is None or self.materialization_reason is not None:
            raise ValidationFailureError("ready result requires identity and retained snapshot metadata")


def _revision_vector_identity(revision: ReadRevision) -> str:
    return hashlib.sha256(canonical_json(revision.revision_vector.as_dict()).encode("utf-8")).hexdigest()


def _fingerprint_components(current: ExactStructuredFingerprint, candidate: ExactStructuredFingerprint) -> SimilarityComponents:
    current_values = {feature.feature_id: feature.value_sha256 for feature in current.features}
    candidate_values = {feature.feature_id: feature.value_sha256 for feature in candidate.features}
    common = current_values.keys() & candidate_values.keys()
    return SimilarityComponents(
        tuple(sorted(feature_id for feature_id in common if current_values[feature_id] == candidate_values[feature_id])),
        tuple(sorted(feature_id for feature_id in common if current_values[feature_id] != candidate_values[feature_id])),
        tuple(sorted(current_values.keys() - candidate_values.keys())),
        tuple(sorted(candidate_values.keys() - current_values.keys())),
    )


def _tie_break_identity(episode_id: str, revision_id: str) -> str:
    return hashlib.sha256(canonical_json({"episode_id": episode_id, "revision_id": revision_id}).encode("utf-8")).hexdigest()


def _invalid_profile_reason(revision: ReadRevision) -> str:
    raw = revision.payload.get(COMPARABLE_PROFILE_KEY)
    if not isinstance(raw, Mapping) or raw.get("schema") != COMPARABLE_PROFILE_SCHEMA:
        return "MISSING_COMPARISON_PROFILE"
    source = raw.get("source_identity")
    if not isinstance(source, Mapping) or not all(
        source.get(field) for field in ("snapshot_id", "source_revision", "manifest_hash", "artifact_sha256")
    ):
        return "MISSING_IMMUTABLE_SOURCE_IDENTITY"
    fingerprint = raw.get("fingerprint")
    if not isinstance(fingerprint, Mapping) or not fingerprint.get("features"):
        return "MISSING_STRUCTURED_FINGERPRINT"
    if not raw.get("eligibility_identity"):
        return "MISSING_QUALIFICATION_IDENTITY"
    if raw.get("eligibility_state") == EligibilityState.QUALIFIED.value and not raw.get("qualification_evidence_identity"):
        return "MISSING_QUALIFICATION_EVIDENCE"
    if raw.get("curation_state") == CurationState.CURATED.value and not raw.get("curation_evidence_identity"):
        return "MISSING_CURATION_EVIDENCE"
    return "INVALID_COMPARISON_PROFILE"


def _case_from_retained(row: RetainedSnapshotRow) -> ComparableCase:
    payload = dict(row.payload)
    try:
        source = payload["source_identity"]
        components = payload["similarity_components"]
        claims = payload["historical_claims"]
        return ComparableCase(
            payload["episode_id"],
            payload["revision_id"],
            payload["revision_vector_identity"],
            HistoricalSourceIdentity(source["snapshot_id"], source["source_revision"], source["manifest_hash"], source["artifact_sha256"]),
            payload["family_identity"],
            payload["context_identity"],
            EligibilityState(payload["eligibility_state"]),
            payload["eligibility_identity"],
            payload.get("qualification_evidence_identity"),
            CurationState(payload["curation_state"]),
            payload.get("curation_evidence_identity"),
            _aware_datetime(payload["revision_known_at"], "revision_known_at"),
            _aware_datetime(payload["revision_published_at"], "revision_published_at"),
            _aware_datetime(payload["source_available_at"], "source_available_at"),
            _aware_datetime(payload["source_known_at"], "source_known_at"),
            _aware_datetime(payload["knowledge_cutoff"], "knowledge_cutoff"),
            SimilarityComponents(
                tuple(components["shared_exact_feature_ids"]),
                tuple(components["differing_feature_ids"]),
                tuple(components["current_only_feature_ids"]),
                tuple(components["candidate_only_feature_ids"]),
            ),
            payload["tie_break_identity"],
            tuple(payload["data_completeness_limitations"]),
            tuple(HistoricalClaim.from_dict(claim) for claim in claims),
        )
    except (KeyError, TypeError, ValueError, ValidationFailureError) as exc:
        raise QueryIdentityMismatchError("retained comparable-case content is invalid") from exc


def _materialization_page(query_identity: str, reason: str = "CANDIDATE_LIMIT_EXCEEDED") -> ComparableCasesPage:
    return ComparableCasesPage(
        ComparableQueryState.MATERIALIZATION_REQUIRED,
        query_identity,
        None,
        tuple(),
        tuple(),
        0,
        None,
        None,
        reason,
    )


class ComparableCaseHistoryQueryService:
    """Read and retain a deterministic, bounded exact-comparison result."""

    def __init__(
        self,
        history_source: ComparableCaseHistorySource,
        read_store: ReadSnapshotStore,
        current_authorization: CurrentAuthorizationAuthority,
    ) -> None:
        if not isinstance(history_source, ComparableCaseHistorySource):
            raise TypeError("history_source must implement the bounded Episode history read contract")
        if not isinstance(read_store, ReadSnapshotStore):
            raise TypeError("read_store must implement the existing retained-query authority")
        if not isinstance(current_authorization, CurrentAuthorizationAuthority):
            raise TypeError("current_authorization must be a CurrentAuthorizationAuthority")
        self.history_source = history_source
        self.read_store = read_store
        self.current_authorization = current_authorization

    def retrieve(
        self,
        principal: Principal,
        query: ComparableCaseQuery,
        *,
        page_size: int = 50,
        snapshot_id: str | None = None,
        cursor: str | None = None,
        snapshot_ttl_seconds: int = DEFAULT_SNAPSHOT_TTL_SECONDS,
    ) -> ComparableCasesPage:
        if not isinstance(query, ComparableCaseQuery):
            raise ValidationFailureError("retrieve requires a typed ComparableCaseQuery")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise QueryTooBroadError("page_size is outside the bounded retained-query contract", limit=MAX_PAGE_SIZE)
        if isinstance(snapshot_ttl_seconds, bool) or not isinstance(snapshot_ttl_seconds, int) or not 1 <= snapshot_ttl_seconds <= MAX_SNAPSHOT_TTL_SECONDS:
            raise ValidationFailureError("snapshot_ttl_seconds is outside the retained-query contract")
        if snapshot_id is None and cursor is not None:
            raise QueryCursorValidationError("a cursor requires its retained query snapshot")

        # Authorization precedes current or historical Episode/source reads,
        # candidate lookup, retained snapshot lookup, and every disclosure.
        self.current_authorization.authorize(principal, query.scope, COMPARABLE_HISTORY_READ_CAPABILITY)
        query_body, query_identity = self._query_identity(query)
        if snapshot_id is None:
            ready = self._build_result(principal, query, query_identity, query_body, snapshot_ttl_seconds)
            if ready.state == ComparableQueryState.MATERIALIZATION_REQUIRED:
                self.current_authorization.authorize(principal, query.scope, COMPARABLE_HISTORY_READ_CAPABILITY)
                return ready
            snapshot_id = ready.snapshot_id
            if snapshot_id is None:  # pragma: no cover - guarded by result validation
                raise QueryIdentityMismatchError("ready comparable result has no retained snapshot")
            page: PageResult = self.read_store.read_query_snapshot_page(
                principal,
                query.scope,
                snapshot_id,
                query_body,
                COMPARABLE_HISTORY_READ_CAPABILITY,
                page_size=page_size,
            )
            self.current_authorization.authorize(principal, query.scope, COMPARABLE_HISTORY_READ_CAPABILITY)
            cases = tuple(_case_from_retained(row) for row in page.rows)
            return ComparableCasesPage(
                ComparableQueryState.READY,
                query_identity,
                ready.result_identity,
                cases,
                ready.excluded_candidates,
                page.total_row_count,
                page.snapshot_id,
                page.next_cursor,
            )

        # The caller must resubmit the complete typed query on continuation.
        # The retained authority compares its complete scope/filter/cutoff,
        # versioned policy and ordering hash and revalidates snapshot access.
        page = self.read_store.read_query_snapshot_page(
            principal,
            query.scope,
            snapshot_id,
            query_body,
            COMPARABLE_HISTORY_READ_CAPABILITY,
            page_size=page_size,
            cursor=cursor,
        )
        first_page = self.read_store.read_query_snapshot_page(
            principal,
            query.scope,
            snapshot_id,
            query_body,
            COMPARABLE_HISTORY_READ_CAPABILITY,
            page_size=1,
        )
        self.current_authorization.authorize(principal, query.scope, COMPARABLE_HISTORY_READ_CAPABILITY)
        if not first_page.rows:
            raise QueryIdentityMismatchError("retained comparable result has no identity-bearing member")
        metadata = first_page.rows[0].payload.get("_o6_query_metadata")
        if not isinstance(metadata, Mapping) or metadata.get("query_identity") != query_identity:
            raise QueryIdentityMismatchError("retained comparable result metadata does not match the query")
        cases = tuple(_case_from_retained(row) for row in page.rows)
        raw_exclusions = metadata.get("excluded_candidates", ())
        exclusions = tuple(
            ExcludedComparableCandidate(item["episode_id"], item["revision_id"], tuple(item["reason_codes"]))
            for item in raw_exclusions
        )
        return ComparableCasesPage(
            ComparableQueryState.READY,
            query_identity,
            metadata["result_identity"],
            cases,
            exclusions,
            page.total_row_count,
            page.snapshot_id,
            page.next_cursor,
        )

    @staticmethod
    def _query_identity(query: ComparableCaseQuery) -> tuple[dict[str, object], str]:
        body = query.as_dict()
        return body, hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()

    def _build_result(
        self,
        principal: Principal,
        query: ComparableCaseQuery,
        query_identity: str,
        query_body: Mapping[str, object],
        snapshot_ttl_seconds: int,
    ) -> ComparableCasesPage:
        current = self.read_store.read_current_bundle(
            principal,
            query.scope,
            "episode",
            query.episode_id,
            COMPARABLE_HISTORY_READ_CAPABILITY,
        )
        revision = current.read_revision
        workflow = current.workflow_aggregate
        if revision.revision_id != query.revision_id or revision.entity_id != query.episode_id or revision.entity_type != "episode":
            raise CoherentReadConflictError("comparable query is not bound to the current Episode read revision")
        if workflow.aggregate_type != "episode_workflow" or workflow.aggregate_id != query.episode_id:
            raise CoherentReadConflictError("comparable query Episode and workflow identities disagree")
        if current.revision_vector.workflow_version != query.workflow_version:
            raise CoherentReadConflictError("comparable query workflow revision is stale")
        decision_loop = workflow.state.get("decision_loop")
        if not isinstance(decision_loop, Mapping) or decision_loop.get("active_cycle_id") != query.cycle_id:
            raise CoherentReadConflictError("comparable query cycle is not the active Episode cycle")
        try:
            current_profile = ComparableCaseProfile.from_revision(revision)
        except ValidationFailureError as exc:
            raise CoherentReadConflictError("current Episode has no complete comparable-case profile") from exc
        if current_profile.family_identity != query.family_identity or current_profile.context_identity != query.context_identity:
            raise QueryIdentityMismatchError("query family/context does not match the current Episode revision")
        if current_profile.eligibility_state != EligibilityState.QUALIFIED:
            raise CoherentReadConflictError("current Episode is not qualified as a comparable-case query target")
        if revision.known_at > query.knowledge_cutoff or revision.published_at > query.knowledge_cutoff:
            raise QueryIdentityMismatchError("current Episode revision was not known by the requested cutoff")
        if current_profile.source_identity != query.current_source_identity:
            raise QueryIdentityMismatchError("query current source identity does not match the immutable Episode revision")
        if current_profile.fingerprint.identity != query.current_fingerprint_identity:
            raise QueryIdentityMismatchError("query current fingerprint identity does not match the immutable Episode revision")
        current_source = self.history_source.fetch_comparable_source_facts(
            principal, query.scope, current_profile.source_identity, COMPARABLE_HISTORY_READ_CAPABILITY
        )
        if current_source is None or current_source.identity != current_profile.source_identity:
            raise ReadRevisionNotFoundError("current comparable source manifest is unavailable in the requested scope")
        if current_source.family_identity != query.family_identity:
            raise CoherentReadConflictError("current source manifest family does not match the comparable query")
        if current_source.status != "PUBLISHED":
            raise CoherentReadConflictError("current comparable source manifest is not available for comparison")
        if current_source.available_at > query.knowledge_cutoff or current_source.known_at > query.knowledge_cutoff:
            raise QueryIdentityMismatchError("current source identity was not available by the requested cutoff")

        try:
            fetched_candidates = tuple(self.history_source.fetch_episode_history_window(
                    principal,
                    query.scope,
                    query.episode_id,
                    query.knowledge_cutoff,
                    limit=query.candidate_limit + 1,
                    required_read_capability=COMPARABLE_HISTORY_READ_CAPABILITY,
                )
            )
        except ComparableHistoryMaterializationRequired:
            self.current_authorization.authorize(principal, query.scope, COMPARABLE_HISTORY_READ_CAPABILITY)
            return _materialization_page(query_identity, "QUERY_TIME_BUDGET_EXCEEDED")
        # Enforce scope, self and temporal eligibility before the bounded
        # population decision. The PostgreSQL source applies source available
        # time in SQL before LIMIT too, so future evidence cannot cause an
        # overflow state or displace an as-of-cutoff candidate.
        raw_candidates = tuple(
            record for record in fetched_candidates
            if record.revision.scope.canonical_key == query.scope.canonical_key
            and record.revision.entity_type == "episode"
            and record.revision.entity_id != query.episode_id
            and record.revision.known_at <= query.knowledge_cutoff
            and record.revision.published_at <= query.knowledge_cutoff
            and (record.source_facts is None or (
                record.source_facts.available_at <= query.knowledge_cutoff
                and record.source_facts.known_at <= query.knowledge_cutoff
            ))
        )
        # The indexed storage read returns at most limit+1 rows. Crossing the
        # cap never drops rows or returns a partial ranking.
        if len(raw_candidates) > query.candidate_limit:
            return _materialization_page(query_identity)

        eligible, exclusions, population = self._qualify_candidates(query, current_profile, raw_candidates)
        eligible.sort(key=lambda item: (item[0].episode_id, item[0].revision_id))
        if query.policy.version == "2":
            eligible.sort(key=lambda item: item[0].revision_known_at, reverse=True)
        eligible.sort(key=lambda item: (
            -item[0].similarity_components.exact_match_count,
            -item[0].similarity_components.compared_feature_count,
            item[0].similarity_components.material_difference_count,
        ))
        excluded_wire = [item.as_dict() for item in exclusions]
        result_identity = hashlib.sha256(canonical_json({
            "query": query_body,
            "population": population,
            "ordered_results": [item[0].as_dict() for item in eligible],
            "excluded_candidates": excluded_wire,
        }).encode("utf-8")).hexdigest()
        metadata = {"query_identity": query_identity, "result_identity": result_identity, "excluded_candidates": excluded_wire}
        retained_rows = []
        for index, (case, _profile) in enumerate(eligible):
            payload = case.as_dict()
            if index == 0:
                payload["_o6_query_metadata"] = metadata
            retained_rows.append(VersionedReadRow(f"{case.episode_id}:{case.revision_id}", case.revision_id, payload))

        # Recheck current authorization and the Episode view before retaining
        # candidate contents, then the retained-read authority checks again on
        # page disclosure. The current Episode may have advanced meanwhile.
        self.current_authorization.authorize(principal, query.scope, COMPARABLE_HISTORY_READ_CAPABILITY)
        latest = self.read_store.read_current_bundle(
            principal,
            query.scope,
            "episode",
            query.episode_id,
            COMPARABLE_HISTORY_READ_CAPABILITY,
        )
        if latest.read_revision.revision_id != query.revision_id or latest.revision_vector.workflow_version != query.workflow_version:
            raise CoherentReadConflictError("Episode revision advanced during comparable history retrieval")
        self.current_authorization.authorize(principal, query.scope, COMPARABLE_HISTORY_READ_CAPABILITY)
        snapshot = self.read_store.create_query_snapshot(
            principal,
            query.scope,
            query_body,
            COMPARABLE_HISTORY_READ_CAPABILITY,
            retained_rows,
            ttl_seconds=snapshot_ttl_seconds,
        )
        # Exclusion metadata is returned with the first response and retained
        # in its first immutable member for later authorized continuation.
        return ComparableCasesPage(
            ComparableQueryState.READY,
            query_identity,
            result_identity,
            tuple(item[0] for item in eligible[:50]),
            tuple(exclusions),
            snapshot.total_row_count,
            snapshot.snapshot_id,
            None,
        )

    def _qualify_candidates(
        self,
        query: ComparableCaseQuery,
        current_profile: ComparableCaseProfile,
        records: tuple[ComparableCaseRevisionRecord, ...],
    ) -> tuple[list[tuple[ComparableCase, ComparableCaseProfile]], list[ExcludedComparableCandidate], list[object]]:
        # Keep the latest as-known immutable revision for each other Episode.
        latest_by_episode: dict[str, ComparableCaseRevisionRecord] = {}
        for record in sorted(records, key=lambda item: (item.revision.entity_id, item.revision.revision_id)):
            revision = record.revision
            # The source adapter scopes in SQL. Defensively drop a malformed
            # cross-scope adapter row without exposing its identity or count.
            if revision.scope.canonical_key != query.scope.canonical_key:
                continue
            if revision.entity_type != "episode" or revision.entity_id == query.episode_id:
                continue
            if revision.known_at > query.knowledge_cutoff or revision.published_at > query.knowledge_cutoff:
                continue
            previous = latest_by_episode.get(revision.entity_id)
            if previous is None:
                latest_by_episode[revision.entity_id] = record
            else:
                prior = previous.revision
                if (revision.known_at, revision.published_at) > (prior.known_at, prior.published_at) or (
                    (revision.known_at, revision.published_at) == (prior.known_at, prior.published_at)
                    and revision.revision_id < prior.revision_id
                ):
                    latest_by_episode[revision.entity_id] = record

        eligible: list[tuple[ComparableCase, ComparableCaseProfile]] = []
        exclusions: list[ExcludedComparableCandidate] = []
        population: list[object] = []
        for episode_id, record in sorted(latest_by_episode.items()):
            revision = record.revision
            reason_codes: list[str] = []
            profile: ComparableCaseProfile | None = None
            try:
                profile = ComparableCaseProfile.from_revision(revision)
            except ValidationFailureError:
                reason_codes.append(_invalid_profile_reason(revision))
            source_facts = record.source_facts
            if profile is not None:
                if profile.family_identity != query.family_identity:
                    reason_codes.append("FAMILY_MISMATCH")
                if profile.context_identity != query.context_identity:
                    reason_codes.append("CONTEXT_MISMATCH")
                if profile.eligibility_state == EligibilityState.UNQUALIFIED:
                    reason_codes.append("UNQUALIFIED_FOR_COMPARISON")
                if profile.eligibility_state == EligibilityState.QUALIFIED and profile.qualification_evidence_identity is None:
                    reason_codes.append("MISSING_QUALIFICATION_EVIDENCE")
                if source_facts is None:
                    reason_codes.append("MISSING_IMMUTABLE_SOURCE_RECORD")
                elif source_facts.identity != profile.source_identity:
                    reason_codes.append("SOURCE_IDENTITY_MISMATCH")
                elif source_facts.family_identity != query.family_identity:
                    reason_codes.append("SOURCE_FAMILY_MISMATCH")
                elif source_facts.available_at > query.knowledge_cutoff or source_facts.known_at > query.knowledge_cutoff:
                    reason_codes.append("SOURCE_NOT_AVAILABLE_BY_CUTOFF")
                elif source_facts.status not in {"PUBLISHED", "PARTIAL"}:
                    reason_codes.append("SOURCE_NOT_QUALIFIED_FOR_COMPARISON")
            else:
                if source_facts is None:
                    reason_codes.append("MISSING_IMMUTABLE_SOURCE_RECORD")
            profile_identity = profile.manifest_dict() if profile is not None else None
            population.append({
                "episode_id": episode_id,
                "revision_id": revision.revision_id,
                "revision_vector": revision.revision_vector.as_dict(),
                "known_at": revision.known_at,
                "published_at": revision.published_at,
                "payload_sha256": hashlib.sha256(canonical_json(revision.payload).encode("utf-8")).hexdigest(),
                "profile": profile_identity,
                "source_facts": None if source_facts is None else {
                    "identity": source_facts.identity.as_dict(),
                    "family_identity": source_facts.family_identity,
                    "available_at": source_facts.available_at,
                    "known_at": source_facts.known_at,
                    "status": source_facts.status,
                },
                "reason_codes": sorted(set(reason_codes)),
            })
            if reason_codes:
                exclusions.append(ExcludedComparableCandidate(episode_id, revision.revision_id, tuple(reason_codes)))
                continue
            if profile is None or source_facts is None:  # pragma: no cover - reason list guards this
                continue
            components = _fingerprint_components(current_profile.fingerprint, profile.fingerprint)
            limitations = list(profile.data_completeness_limitations)
            if profile.eligibility_state == EligibilityState.LIMITED:
                limitations.append("COMPARISON_QUALIFICATION_LIMITED")
            if profile.curation_state != CurationState.CURATED:
                limitations.append("HISTORICAL_CURATION_LIMITED")
            if source_facts.status == "PARTIAL":
                limitations.append("SOURCE_MANIFEST_PARTIAL")
            claims = tuple(
                claim for claim in profile.claims
                if claim.curation_state == CurationState.CURATED
                and claim.known_at <= query.knowledge_cutoff
                and claim.available_at <= query.knowledge_cutoff
                and (claim.claim_type != HistoricalClaimType.OUTCOME or claim.outcome_cutoff <= query.knowledge_cutoff)
            )
            if any(
                claim.known_at <= query.knowledge_cutoff
                and claim.available_at <= query.knowledge_cutoff
                and claim.curation_state != CurationState.CURATED
                for claim in profile.claims
            ):
                limitations.append("HISTORICAL_CLAIM_CURATION_LIMITED")
            case = ComparableCase(
                episode_id,
                revision.revision_id,
                _revision_vector_identity(revision),
                profile.source_identity,
                profile.family_identity,
                profile.context_identity,
                profile.eligibility_state,
                profile.eligibility_identity,
                profile.qualification_evidence_identity,
                profile.curation_state,
                profile.curation_evidence_identity,
                revision.known_at,
                revision.published_at,
                source_facts.available_at,
                source_facts.known_at,
                query.knowledge_cutoff,
                components,
                _tie_break_identity(episode_id, revision.revision_id),
                tuple(sorted(set(limitations))),
                claims,
            )
            eligible.append((case, profile))
        exclusions.sort(key=lambda item: (item.episode_id, item.revision_id, item.reason_codes))
        return eligible, exclusions, population


__all__ = [
    "COMPARABLE_HISTORY_READ_CAPABILITY",
    "COMPARABLE_PROFILE_KEY",
    "COMPARABLE_PROFILE_SCHEMA",
    "COMPARABLE_FINGERPRINT_VERSION",
    "COMPARABLE_POLICY_ID",
    "COMPARABLE_POLICY_VERSION",
    "MAX_COMPARABLE_CANDIDATE_LIMIT",
    "MAX_COMPARABLE_QUERY_MILLISECONDS",
    "ComparableHistoryMaterializationRequired",
    "EligibilityState",
    "CurationState",
    "HistoricalClaimType",
    "ComparableQueryState",
    "HistoricalSourceIdentity",
    "HistoricalSourceFacts",
    "ComparableCaseRevisionRecord",
    "ComparableCaseHistorySource",
    "FingerprintFeature",
    "ExactStructuredFingerprint",
    "HistoricalClaim",
    "ComparableCaseProfile",
    "ComparableRetrievalPolicy",
    "ComparableCaseQuery",
    "SimilarityComponents",
    "ComparableCase",
    "ExcludedComparableCandidate",
    "ComparableCasesPage",
    "ComparableCaseHistoryQueryService",
]
