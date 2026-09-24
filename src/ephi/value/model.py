"""Immutable value records and strict temporal/monetary validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import re
from typing import Any


_DECIMAL_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class ValueValidationError(ValueError):
    """A value record, timestamp or query failed closed validation."""


class SupersessionError(ValueValidationError):
    """A value revision violates append-only supersession rules."""


class UnknownPredecessorError(SupersessionError):
    """A successor referred to a value entry that was not already stored."""


class SupersessionConflictError(SupersessionError):
    """A successor would branch a chain or cross its immutable identity."""


class MixedCurrencyError(ValueError):
    """An aggregation would sum values from more than one currency."""


class EvidenceMaturity(str, Enum):
    """Evidence state declared for one immutable value revision."""

    PENDING = "PENDING"
    OBSERVED = "OBSERVED"
    REJECTED = "REJECTED"
    CENSORED = "CENSORED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ReviewDecision(str, Enum):
    """Independent reviewer transition bound to one exact value revision."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ValueRevisionKind(str, Enum):
    """Whether a value revision contributes an amount or explicitly voids one."""

    VALUE = "VALUE"
    VOID = "VOID"


def _optional_identity(value: object, field: str) -> str | None:
    if value is None:
        return None
    return validate_identity(value, field)


def _unique_identities(values: object, field: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list, set, frozenset)):
        raise ValueValidationError(f"{field} must be a collection of identities")
    return tuple(sorted({validate_identity(value, field) for value in values}))


@dataclass(frozen=True, slots=True)
class ClaimGroupIdentity:
    """One deduplicated economic event within an authorized scope."""

    scope: str
    economic_event_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", validate_identity(self.scope, "scope"))
        object.__setattr__(
            self,
            "economic_event_key",
            validate_identity(self.economic_event_key, "economic_event_key"),
        )

    @property
    def group_id(self) -> str:
        raw = f"ephi-outcome-group-v1\0{self.scope}\0{self.economic_event_key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ClaimAttribution:
    """Deduplicated links to operational records; links never allocate value."""

    episode_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    contributor_ids: tuple[str, ...] = ()
    material_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("episode_ids", "decision_ids", "action_ids", "contributor_ids", "material_scope"):
            object.__setattr__(self, field, _unique_identities(getattr(self, field), field))

    def as_dict(self) -> dict[str, object]:
        return {
            "episode_ids": list(self.episode_ids),
            "decision_ids": list(self.decision_ids),
            "action_ids": list(self.action_ids),
            "contributor_ids": list(self.contributor_ids),
            "material_scope": list(self.material_scope),
        }


@dataclass(frozen=True, slots=True)
class ClaimRevision:
    """Append-only owner, evidence, attribution and coverage snapshot."""

    claim_revision_id: str
    group_id: str
    value_entry_id: str
    claimant: str
    owner: str
    event_at: datetime
    known_at: datetime
    evidence_ids: tuple[str, ...]
    attribution: ClaimAttribution = ClaimAttribution()
    coverage_numerator: int | None = None
    coverage_denominator: int | None = None
    supersedes: str | None = None

    def __post_init__(self) -> None:
        for field in ("claim_revision_id", "group_id", "value_entry_id", "claimant", "owner"):
            object.__setattr__(self, field, validate_identity(getattr(self, field), field))
        object.__setattr__(self, "event_at", validate_timestamp(self.event_at, "event_at"))
        object.__setattr__(self, "known_at", validate_timestamp(self.known_at, "known_at"))
        object.__setattr__(self, "evidence_ids", _unique_identities(self.evidence_ids, "evidence_id"))
        if not isinstance(self.attribution, ClaimAttribution):
            raise ValueValidationError("attribution must be a ClaimAttribution")
        for field in ("coverage_numerator", "coverage_denominator"):
            value = getattr(self, field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueValidationError(f"{field} must be a non-negative integer or None")
        if (self.coverage_numerator is None) != (self.coverage_denominator is None):
            raise ValueValidationError("coverage numerator and denominator must be supplied together")
        if self.coverage_numerator is not None and self.coverage_numerator > self.coverage_denominator:
            raise ValueValidationError("coverage numerator cannot exceed its denominator")
        if self.supersedes is not None:
            object.__setattr__(self, "supersedes", validate_identity(self.supersedes, "supersedes"))

    def as_dict(self) -> dict[str, object]:
        return {
            "claim_revision_id": self.claim_revision_id,
            "group_id": self.group_id,
            "value_entry_id": self.value_entry_id,
            "claimant": self.claimant,
            "owner": self.owner,
            "event_at": self.event_at.isoformat(),
            "known_at": self.known_at.isoformat(),
            "evidence_ids": list(self.evidence_ids),
            "attribution": self.attribution.as_dict(),
            "coverage_numerator": self.coverage_numerator,
            "coverage_denominator": self.coverage_denominator,
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ClaimRevision":
        if not isinstance(value, dict):
            raise ValueValidationError("serialized claim revision must be an object")
        try:
            attribution = value.get("attribution", {})
            if not isinstance(attribution, dict):
                raise ValueValidationError("serialized attribution must be an object")
            return cls(
                claim_revision_id=value["claim_revision_id"],
                group_id=value["group_id"],
                value_entry_id=value["value_entry_id"],
                claimant=value["claimant"],
                owner=value["owner"],
                event_at=datetime.fromisoformat(value["event_at"].replace("Z", "+00:00")),
                known_at=datetime.fromisoformat(value["known_at"].replace("Z", "+00:00")),
                evidence_ids=tuple(value.get("evidence_ids", ())),
                attribution=ClaimAttribution(**attribution),
                coverage_numerator=value.get("coverage_numerator"),
                coverage_denominator=value.get("coverage_denominator"),
                supersedes=value.get("supersedes"),
            )
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise ValueValidationError("serialized claim revision is incomplete") from exc


@dataclass(frozen=True, slots=True)
class ReviewRevision:
    """Immutable independent review with exact science/evidence/cutoff binding."""

    review_id: str
    group_id: str
    value_entry_id: str
    claim_revision_id: str
    evidence_identity: str
    cost_model_identity: str
    rate_policy_identity: str | None
    knowledge_cutoff: datetime
    reviewer: str
    decision: ReviewDecision
    known_at: datetime
    rationale: str
    supersedes: str | None = None

    def __post_init__(self) -> None:
        for field in ("review_id", "group_id", "value_entry_id", "claim_revision_id", "evidence_identity", "cost_model_identity", "reviewer", "rationale"):
            object.__setattr__(self, field, validate_identity(getattr(self, field), field))
        object.__setattr__(self, "rate_policy_identity", _optional_identity(self.rate_policy_identity, "rate_policy_identity"))
        object.__setattr__(self, "knowledge_cutoff", validate_timestamp(self.knowledge_cutoff, "knowledge_cutoff"))
        object.__setattr__(self, "known_at", validate_timestamp(self.known_at, "known_at"))
        if self.known_at < self.knowledge_cutoff:
            raise ValueValidationError("review known_at cannot precede its knowledge cutoff")
        if not isinstance(self.decision, ReviewDecision):
            raise ValueValidationError("decision must be a ReviewDecision")
        if self.supersedes is not None:
            object.__setattr__(self, "supersedes", validate_identity(self.supersedes, "supersedes"))

    def as_dict(self) -> dict[str, object]:
        return {
            "review_id": self.review_id,
            "group_id": self.group_id,
            "value_entry_id": self.value_entry_id,
            "claim_revision_id": self.claim_revision_id,
            "evidence_identity": self.evidence_identity,
            "cost_model_identity": self.cost_model_identity,
            "rate_policy_identity": self.rate_policy_identity,
            "knowledge_cutoff": self.knowledge_cutoff.isoformat(),
            "reviewer": self.reviewer,
            "decision": self.decision.value,
            "known_at": self.known_at.isoformat(),
            "rationale": self.rationale,
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReviewRevision":
        if not isinstance(value, dict):
            raise ValueValidationError("serialized review revision must be an object")
        try:
            return cls(
                review_id=value["review_id"],
                group_id=value["group_id"],
                value_entry_id=value["value_entry_id"],
                claim_revision_id=value["claim_revision_id"],
                evidence_identity=value["evidence_identity"],
                cost_model_identity=value["cost_model_identity"],
                rate_policy_identity=value.get("rate_policy_identity"),
                knowledge_cutoff=datetime.fromisoformat(value["knowledge_cutoff"].replace("Z", "+00:00")),
                reviewer=value["reviewer"],
                decision=ReviewDecision(value["decision"]),
                known_at=datetime.fromisoformat(value["known_at"].replace("Z", "+00:00")),
                rationale=value["rationale"],
                supersedes=value.get("supersedes"),
            )
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise ValueValidationError("serialized review revision is incomplete") from exc


def validate_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueValidationError(f"{field} must be a non-empty string")
    return value


def validate_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueValidationError(f"{field} must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{field} must be a timezone-aware datetime")
    return value


def parse_amount(value: object) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueValidationError("amount must not be a float")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value):
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueValidationError("amount is not a valid decimal string") from exc
    else:
        raise ValueValidationError("amount must be a Decimal, integer, or decimal string")
    if not amount.is_finite():
        raise ValueValidationError("amount must be finite")
    return amount


def validate_currency(value: object) -> str:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise ValueValidationError("currency must be an uppercase three-letter code")
    return value


@dataclass(frozen=True, slots=True)
class EventPeriod:
    """A half-open event period used after temporal leaf selection."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = validate_timestamp(self.start, "event period start")
        end = validate_timestamp(self.end, "event period end")
        if start >= end:
            raise ValueError("event period start must be before end")

    def contains(self, event_at: datetime) -> bool:
        event_at = validate_timestamp(event_at, "event_at")
        return self.start <= event_at < self.end


@dataclass(frozen=True, slots=True)
class ValueEntry:
    """An immutable append-only monetary revision.

    ``known_at`` is the immutable server-recorded knowledge time and is the
    only time used to decide whether a revision can appear in a report.
    ``event_at`` is the event/effective instant used for period filtering.
    """

    entry_id: str
    scope: str
    group_id: str
    category: str
    amount: Decimal | None
    currency: str
    event_at: datetime
    known_at: datetime
    supersedes: str | None = None
    claim_revision_id: str | None = None
    evidence_identity: str | None = None
    cost_model_identity: str | None = None
    rate_policy_identity: str | None = None
    maturity: EvidenceMaturity = EvidenceMaturity.OBSERVED
    revision_kind: ValueRevisionKind = ValueRevisionKind.VALUE

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_id", validate_identity(self.entry_id, "entry_id"))
        object.__setattr__(self, "scope", validate_identity(self.scope, "scope"))
        object.__setattr__(self, "group_id", validate_identity(self.group_id, "group_id"))
        object.__setattr__(self, "category", validate_identity(self.category, "category"))
        if not isinstance(self.revision_kind, ValueRevisionKind):
            try:
                object.__setattr__(self, "revision_kind", ValueRevisionKind(self.revision_kind))
            except (TypeError, ValueError) as exc:
                raise ValueValidationError("revision_kind must be VALUE or VOID") from exc
        if self.revision_kind is ValueRevisionKind.VOID:
            if self.amount is not None:
                raise ValueValidationError("a void revision must not carry a monetary amount")
        else:
            if self.amount is None:
                raise ValueValidationError("a value revision requires a monetary amount")
            object.__setattr__(self, "amount", parse_amount(self.amount))
        object.__setattr__(self, "currency", validate_currency(self.currency))
        object.__setattr__(self, "event_at", validate_timestamp(self.event_at, "event_at"))
        object.__setattr__(self, "known_at", validate_timestamp(self.known_at, "known_at"))
        if self.supersedes is not None:
            object.__setattr__(self, "supersedes", validate_identity(self.supersedes, "supersedes"))
        for field in ("claim_revision_id", "evidence_identity", "cost_model_identity"):
            object.__setattr__(self, field, _optional_identity(getattr(self, field), field))
        object.__setattr__(self, "rate_policy_identity", _optional_identity(self.rate_policy_identity, "rate_policy_identity"))
        if not isinstance(self.maturity, EvidenceMaturity):
            try:
                object.__setattr__(self, "maturity", EvidenceMaturity(self.maturity))
            except (TypeError, ValueError) as exc:
                raise ValueValidationError("maturity must be a supported EvidenceMaturity") from exc

    @property
    def identity_key(self) -> tuple[str, str, str, str]:
        """The dimensions that a successor must preserve."""

        return (self.scope, self.group_id, self.category, self.currency)

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-ready data with monetary values represented as strings."""

        return {
            "entry_id": self.entry_id,
            "scope": self.scope,
            "group_id": self.group_id,
            "category": self.category,
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "event_at": self.event_at.isoformat(),
            "known_at": self.known_at.isoformat(),
            "supersedes": self.supersedes,
            "claim_revision_id": self.claim_revision_id,
            "evidence_identity": self.evidence_identity,
            "cost_model_identity": self.cost_model_identity,
            "rate_policy_identity": self.rate_policy_identity,
            "maturity": self.maturity.value,
            "revision_kind": self.revision_kind.value,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, value: object) -> "ValueEntry":
        if not isinstance(value, dict):
            raise ValueValidationError("serialized value entry must be an object")
        try:
            return cls(
                entry_id=value["entry_id"],
                scope=value["scope"],
                group_id=value["group_id"],
                category=value["category"],
                amount=value.get("amount"),
                currency=value["currency"],
                event_at=datetime.fromisoformat(value["event_at"].replace("Z", "+00:00")),
                known_at=datetime.fromisoformat(value["known_at"].replace("Z", "+00:00")),
                supersedes=value.get("supersedes"),
                claim_revision_id=value.get("claim_revision_id"),
                evidence_identity=value.get("evidence_identity"),
                cost_model_identity=value.get("cost_model_identity"),
                rate_policy_identity=value.get("rate_policy_identity"),
                maturity=value.get("maturity", EvidenceMaturity.OBSERVED.value),
                revision_kind=value.get("revision_kind", ValueRevisionKind.VALUE.value),
            )
        except (KeyError, AttributeError, TypeError) as exc:
            raise ValueValidationError("serialized value entry is incomplete") from exc


def decimal_json_default(value: object) -> str:
    """JSON encoder hook that makes Decimal monetary values decimal strings."""

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot be serialized")
        return str(value)
    if isinstance(value, datetime):
        return validate_timestamp(value, "timestamp").isoformat()
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")
