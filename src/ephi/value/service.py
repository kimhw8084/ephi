"""Temporal value query service for the bounded F04 implementation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Callable, Mapping

from ephi.application.context import AccessScope, CommandContext, CurrentAuthorizationAuthority, Principal
from ephi.application.errors import (
    AggregateNotFoundError,
    AuthorizationDeniedError,
    ValidationFailureError,
)
from ephi.application.transactions import CommandResult, VersionedAggregateCommandExecutor

from .model import (
    ClaimAttribution,
    ClaimGroupIdentity,
    ClaimRevision,
    EvidenceMaturity,
    EventPeriod,
    MixedCurrencyError,
    ReviewDecision,
    ReviewRevision,
    SupersessionConflictError,
    ValueEntry,
    ValueRevisionKind,
    ValueValidationError,
    validate_currency,
    validate_identity,
    validate_timestamp,
    parse_amount,
)
from .repository import InMemoryValueRepository, OutcomeAggregateRepository, ValueRepository


OUTCOME_GROUP_AGGREGATE = "outcome_claim_group"
VALUE_SUBMIT_CAPABILITY = "value.submit"
VALUE_READ_CAPABILITY = "value.read"
VALUE_VALIDATE_CAPABILITY = "value.validate"
ESTIMATED_OPPORTUNITY = "estimated_opportunity"
OBSERVED_OUTCOME = "observed_outcome"
VALIDATED_BENEFIT = "benefit"
OPERATING_COST = "operating_cost"
OUTCOME_CATEGORIES = frozenset({ESTIMATED_OPPORTUNITY, OBSERVED_OUTCOME, VALIDATED_BENEFIT, OPERATING_COST})
MAX_OUTCOME_GROUPS = 500
ALLOW_SELF_VALIDATION = False


class ValueService:
    """Application boundary for append and as-known exact value aggregation."""

    def __init__(self, repository: ValueRepository | None = None) -> None:
        self.repository = repository or InMemoryValueRepository()

    def append(self, entry: ValueEntry) -> ValueEntry:
        return self.repository.append(entry)

    def record(self, entry: ValueEntry) -> ValueEntry:
        """Named command alias for callers that record a server-known entry."""

        return self.append(entry)

    def active_leaves_as_of(self, knowledge_cutoff: datetime) -> tuple[ValueEntry, ...]:
        return self.repository.active_leaves_as_of(knowledge_cutoff)

    def aggregate(
        self,
        *,
        scope: str,
        knowledge_cutoff: datetime,
        group_id: str | None = None,
        category: str | None = None,
        currency: str | None = None,
        event_period: EventPeriod | None = None,
    ) -> Decimal:
        """Aggregate active leaves after cutoff, then event-period filtering."""

        validate_identity(scope, "scope")
        if group_id is not None:
            validate_identity(group_id, "group_id")
        if category is not None:
            validate_identity(category, "category")
        if currency is not None:
            validate_currency(currency)
        validate_timestamp(knowledge_cutoff, "knowledge_cutoff")
        if event_period is not None and not isinstance(event_period, EventPeriod):
            raise ValueValidationError("event_period must be an EventPeriod")

        selected = [
            entry
            for entry in self.repository.active_leaves_as_of(knowledge_cutoff)
            if entry.scope == scope
            and (group_id is None or entry.group_id == group_id)
            and (category is None or entry.category == category)
            and (currency is None or entry.currency == currency)
            and entry.amount is not None
        ]
        if event_period is not None:
            selected = [entry for entry in selected if event_period.contains(entry.event_at)]

        currencies = {entry.currency for entry in selected}
        if len(currencies) > 1:
            raise MixedCurrencyError("mixed-currency aggregation is unsupported")
        return _exact_sum(entry.amount for entry in selected if entry.amount is not None)

    def total(self, **kwargs: object) -> Decimal:
        """Concise alias for the exact Decimal aggregate query."""

        return self.aggregate(**kwargs)  # type: ignore[arg-type]

    def entries(self) -> Iterable[ValueEntry]:
        return self.repository.all_entries()


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    group_id: str
    economic_event_key: str
    aggregate_version: int
    value: ValueEntry
    claim: ClaimRevision
    review: ReviewRevision | None
    state: str
    amount_state: str
    corrected: bool
    pending_age_seconds: int | None


@dataclass(frozen=True, slots=True)
class OutcomeCurrencySummary:
    currency: str
    estimated_opportunity: Decimal
    observed_operational_outcome: Decimal
    validated_benefit: Decimal
    validated_operating_cost: Decimal
    validated_net: Decimal
    claim_group_count: int
    coverage_group_count: int
    pending_record_count: int
    validated_record_count: int


@dataclass(frozen=True, slots=True)
class OutcomesQueryResult:
    state: str
    rows: tuple[OutcomeRecord, ...]
    summaries: tuple[OutcomeCurrencySummary, ...]
    currencies: tuple[str, ...]
    knowledge_cutoff: datetime
    event_period: EventPeriod
    restated: bool
    excluded_count: int
    query_identity: str
    result_identity: str
    reason: str | None = None


def _server_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    return validate_timestamp(value, "server_time")


def _command_record_id(kind: str, context: CommandContext) -> str:
    raw = "\0".join((kind, context.scope.canonical_key, context.principal.subject, context.command_id))
    return f"{kind}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _evidence_identity(evidence_ids: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(list(evidence_ids), separators=(",", ":")).encode("utf-8")).hexdigest()


def _canonical_identity(kind: str, document: Mapping[str, object]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(f"ephi-outcomes-{kind}-v1\0{encoded}".encode("utf-8")).hexdigest()


def _timestamp_identity(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Add finite Decimals as integer coefficients, independent of context precision."""

    numbers = tuple(values)
    if not numbers:
        return Decimal("0")
    exponent = min(value.as_tuple().exponent for value in numbers)
    total = 0
    for value in numbers:
        parts = value.as_tuple()
        coefficient = int("".join(str(digit) for digit in parts.digits) or "0")
        if parts.sign:
            coefficient = -coefficient
        total += coefficient * (10 ** (parts.exponent - exponent))
    sign = 1 if total < 0 else 0
    digits = tuple(int(digit) for digit in str(abs(total)))
    return Decimal((sign, digits, exponent))


def _revision_rows(transaction: object, scope_key: str, group_id: str) -> tuple[ValueEntry, ...]:
    loader = getattr(transaction, "list_outcome_value_revisions", None)
    if not callable(loader):
        raise ValueValidationError("command store does not support normalized outcome value revisions")
    return tuple(ValueEntry.from_dict(item) for item in loader(scope_key, group_id))


def _append_revision(transaction: object, entry: ValueEntry) -> None:
    writer = getattr(transaction, "append_outcome_value_revision", None)
    if not callable(writer):
        raise ValueValidationError("command store does not support normalized outcome value revisions")
    writer(entry.scope, entry.group_id, entry.as_dict())


def _result_facts(rows: Iterable[OutcomeRecord]) -> list[dict[str, object]]:
    return [
        {
            "group_id": row.group_id,
            "economic_event_key": row.economic_event_key,
            "value": row.value.as_dict(),
            "claim": row.claim.as_dict(),
            "review": row.review.as_dict() if row.review is not None else None,
            "state": row.state,
            "amount_state": row.amount_state,
            "corrected": row.corrected,
            "pending_age_seconds": row.pending_age_seconds,
        }
        for row in rows
    ]


def _summary_facts(summaries: Iterable[OutcomeCurrencySummary]) -> list[dict[str, object]]:
    return [
        {
            "currency": summary.currency,
            "estimated_opportunity": str(summary.estimated_opportunity),
            "observed_operational_outcome": str(summary.observed_operational_outcome),
            "validated_benefit": str(summary.validated_benefit),
            "validated_operating_cost": str(summary.validated_operating_cost),
            "validated_net": str(summary.validated_net),
            "claim_group_count": summary.claim_group_count,
            "coverage_group_count": summary.coverage_group_count,
            "pending_record_count": summary.pending_record_count,
            "validated_record_count": summary.validated_record_count,
        }
        for summary in summaries
    ]


def _outcomes_result_identity(
    query_identity: str,
    *,
    state: str,
    rows: Iterable[OutcomeRecord],
    summaries: Iterable[OutcomeCurrencySummary],
    currencies: Iterable[str],
    restated: bool,
    excluded_count: int,
    reason: str | None = None,
) -> str:
    return _canonical_identity("result", {
        "query_identity": query_identity,
        "state": state,
        "rows": _result_facts(rows),
        "summaries": _summary_facts(summaries),
        "currencies": list(currencies),
        "restated": restated,
        "excluded_count": excluded_count,
        "reason": reason,
    })


def _as_list(state: Mapping[str, object], key: str) -> tuple[object, ...]:
    raw = state.get(key, ())
    if not isinstance(raw, list):
        raise ValueValidationError(f"stored outcome {key} must be a list")
    return tuple(raw)


def _value_entries(state: Mapping[str, object]) -> tuple[ValueEntry, ...]:
    return tuple(ValueEntry.from_dict(item) for item in _as_list(state, "value_entries"))


def _claim_revisions(state: Mapping[str, object]) -> tuple[ClaimRevision, ...]:
    return tuple(ClaimRevision.from_dict(item) for item in _as_list(state, "claim_revisions"))


def _review_revisions(state: Mapping[str, object]) -> tuple[ReviewRevision, ...]:
    return tuple(ReviewRevision.from_dict(item) for item in _as_list(state, "review_revisions"))


def _append_only_leaf(items: tuple[object, ...], *, id_field: str, supersedes_field: str, cutoff: datetime, known_field: str) -> tuple[object, ...]:
    known = [item for item in items if getattr(item, known_field) <= cutoff]
    known_ids = {getattr(item, id_field) for item in known}
    replaced = {
        getattr(item, supersedes_field)
        for item in known
        if getattr(item, supersedes_field) is not None and getattr(item, supersedes_field) in known_ids
    }
    return tuple(item for item in known if getattr(item, id_field) not in replaced)


class OutcomesService:
    """Authorized Outcomes query and O2 receipt-backed claim/review commands."""

    def __init__(
        self,
        repository: OutcomeAggregateRepository,
        commands: VersionedAggregateCommandExecutor,
        current_authorization: CurrentAuthorizationAuthority,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not callable(getattr(repository, "get_group", None)) or not callable(getattr(repository, "list_groups", None)):
            raise TypeError("repository must use the O2 outcome aggregate authority")
        if not isinstance(commands, VersionedAggregateCommandExecutor):
            raise TypeError("commands must use VersionedAggregateCommandExecutor")
        if not isinstance(current_authorization, CurrentAuthorizationAuthority):
            raise TypeError("current_authorization must be a CurrentAuthorizationAuthority")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.repository = repository
        self.commands = commands
        self.current_authorization = current_authorization
        self.clock = clock

    def submit_claim(
        self,
        context: CommandContext,
        *,
        economic_event_key: str,
        category: str,
        amount: object,
        currency: str,
        event_at: datetime,
        owner: str,
        evidence_ids: Iterable[str],
        cost_model_identity: str,
        rate_policy_identity: str | None = None,
        maturity: EvidenceMaturity = EvidenceMaturity.PENDING,
        attribution: ClaimAttribution = ClaimAttribution(),
        coverage_numerator: int | None = None,
        coverage_denominator: int | None = None,
    ) -> CommandResult:
        """Create one economic event group through O2's create/CAS command path."""

        self.current_authorization.authorize(context.principal, context.scope, VALUE_SUBMIT_CAPABILITY)
        event_key = validate_identity(economic_event_key, "economic_event_key")
        category = validate_identity(category, "category")
        if category not in OUTCOME_CATEGORIES:
            raise ValueValidationError("unsupported outcome value category")
        cost_model_identity = validate_identity(cost_model_identity, "cost_model_identity")
        if rate_policy_identity is not None:
            raise ValueValidationError("no versioned currency-conversion policy is configured")
        if context.expected_workflow_version != 0:
            raise ValidationFailureError("new outcome claim groups require expected_workflow_version=0")
        if not isinstance(attribution, ClaimAttribution):
            raise ValueValidationError("attribution must be a ClaimAttribution")
        if not isinstance(evidence_ids, Iterable) or isinstance(evidence_ids, (str, bytes)):
            raise ValueValidationError("evidence_ids must be a collection of evidence identities")
        evidence = tuple(sorted({validate_identity(item, "evidence_id") for item in evidence_ids}))
        if not evidence:
            raise ValueValidationError("outcome claims require at least one evidence identity")
        owner = validate_identity(owner, "owner")
        event_at = validate_timestamp(event_at, "event_at")
        currency = validate_currency(currency)
        amount_text = str(parse_amount(amount))
        if not isinstance(maturity, EvidenceMaturity):
            try:
                maturity = EvidenceMaturity(maturity)
            except (TypeError, ValueError) as exc:
                raise ValueValidationError("maturity must be a supported EvidenceMaturity") from exc
        identity = ClaimGroupIdentity(context.scope.canonical_key, event_key)
        entry_id = _command_record_id("value", context)
        claim_id = _command_record_id("claim", context)
        evidence_hash = _evidence_identity(evidence)
        payload = {
            "economic_event_key": event_key,
            "group_id": identity.group_id,
            "category": category,
            "amount": amount_text,
            "currency": currency,
            "event_at": event_at.isoformat(),
            "owner": owner,
            "evidence_ids": list(evidence),
            "evidence_identity": evidence_hash,
            "cost_model_identity": cost_model_identity,
            "rate_policy_identity": rate_policy_identity,
            "maturity": maturity.value,
            "attribution": attribution.as_dict(),
            "coverage_numerator": coverage_numerator,
            "coverage_denominator": coverage_denominator,
        }

        def create(transaction: object, _current: Mapping[str, object], raw: Mapping[str, object]) -> Mapping[str, object]:
            now = _server_now(self.clock)
            entry = ValueEntry(
                entry_id, context.scope.canonical_key, identity.group_id, category,
                amount_text, currency, event_at, now, None, claim_id, evidence_hash,
                cost_model_identity, rate_policy_identity, maturity,
            )
            claim = ClaimRevision(
                claim_id, identity.group_id, entry_id, context.principal.subject,
                owner, event_at, now, evidence, attribution, coverage_numerator,
                coverage_denominator,
            )
            _append_revision(transaction, entry)
            return {
                "schema_version": 1,
                "group_id": identity.group_id,
                "economic_event_key": event_key,
                "scope_key": context.scope.canonical_key,
                "created_by": context.principal.subject,
                "created_at": now.isoformat(),
                "claim_revisions": [claim.as_dict()],
                "review_revisions": [],
            }

        return self.commands.execute(
            context,
            command_type="SubmitOutcomeClaim",
            aggregate_type=OUTCOME_GROUP_AGGREGATE,
            aggregate_id=identity.group_id,
            payload=payload,
            required_capability=VALUE_SUBMIT_CAPABILITY,
            transactional_effect=create,
            create_if_missing=True,
            initial_state={"group_id": identity.group_id, "economic_event_key": event_key},
        )

    def record_value_revision(
        self,
        context: CommandContext,
        *,
        group_id: str,
        category: str,
        amount: object,
        currency: str,
        event_at: datetime,
        owner: str,
        evidence_ids: Iterable[str],
        cost_model_identity: str,
        rate_policy_identity: str | None = None,
        maturity: EvidenceMaturity = EvidenceMaturity.PENDING,
        attribution: ClaimAttribution = ClaimAttribution(),
        supersedes: str | None = None,
        coverage_numerator: int | None = None,
        coverage_denominator: int | None = None,
    ) -> CommandResult:
        """Append another category or a non-branching correction in one group."""

        self.current_authorization.authorize(context.principal, context.scope, VALUE_SUBMIT_CAPABILITY)
        group_id = validate_identity(group_id, "group_id")
        return self._append_value(
            context, group_id, category=category, amount=amount, currency=currency,
            event_at=event_at, owner=owner, evidence_ids=evidence_ids,
            cost_model_identity=cost_model_identity, rate_policy_identity=rate_policy_identity,
            maturity=maturity, attribution=attribution, supersedes=supersedes,
            coverage_numerator=coverage_numerator, coverage_denominator=coverage_denominator,
        )

    def _append_value(
        self,
        context: CommandContext,
        group_id: str,
        *,
        category: str,
        amount: object,
        currency: str,
        event_at: datetime,
        owner: str,
        evidence_ids: Iterable[str],
        cost_model_identity: str,
        rate_policy_identity: str | None,
        maturity: EvidenceMaturity,
        attribution: ClaimAttribution,
        supersedes: str | None,
        coverage_numerator: int | None,
        coverage_denominator: int | None,
    ) -> CommandResult:
        from .repository import InMemoryValueRepository

        category = validate_identity(category, "category")
        if category not in OUTCOME_CATEGORIES:
            raise ValueValidationError("unsupported outcome value category")
        cost_model_identity = validate_identity(cost_model_identity, "cost_model_identity")
        if rate_policy_identity is not None:
            raise ValueValidationError("no versioned currency-conversion policy is configured")
        if not isinstance(attribution, ClaimAttribution):
            raise ValueValidationError("attribution must be a ClaimAttribution")
        if not isinstance(evidence_ids, Iterable) or isinstance(evidence_ids, (str, bytes)):
            raise ValueValidationError("evidence_ids must be a collection of evidence identities")
        evidence = tuple(sorted({validate_identity(item, "evidence_id") for item in evidence_ids}))
        if not evidence:
            raise ValueValidationError("outcome claims require at least one evidence identity")
        owner = validate_identity(owner, "owner")
        event_at = validate_timestamp(event_at, "event_at")
        currency = validate_currency(currency)
        amount_text = str(parse_amount(amount))
        if not isinstance(maturity, EvidenceMaturity):
            try:
                maturity = EvidenceMaturity(maturity)
            except (TypeError, ValueError) as exc:
                raise ValueValidationError("maturity must be a supported EvidenceMaturity") from exc
        if supersedes is not None:
            supersedes = validate_identity(supersedes, "supersedes")
        value_id = _command_record_id("value", context)
        claim_id = _command_record_id("claim", context)
        evidence_hash = _evidence_identity(evidence)
        payload = {
            "group_id": group_id,
            "category": category,
            "amount": amount_text,
            "currency": currency,
            "event_at": event_at.isoformat(),
            "owner": owner,
            "evidence_ids": list(evidence),
            "evidence_identity": evidence_hash,
            "cost_model_identity": cost_model_identity,
            "rate_policy_identity": rate_policy_identity,
            "maturity": maturity.value,
            "attribution": attribution.as_dict(),
            "supersedes": supersedes,
            "coverage_numerator": coverage_numerator,
            "coverage_denominator": coverage_denominator,
        }

        def append(transaction: object, current: Mapping[str, object], raw: Mapping[str, object]) -> Mapping[str, object]:
            if current.get("group_id") != group_id:
                raise ValueValidationError("stored outcome aggregate identity is inconsistent")
            now = _server_now(self.clock)
            current_values = _revision_rows(transaction, context.scope.canonical_key, group_id)
            current_claims = _claim_revisions(current)
            repo = InMemoryValueRepository(current_values)
            current_leaves = repo.active_leaves_as_of(now)
            matching = tuple(item for item in current_leaves if item.category == category and item.currency == currency)
            if supersedes is None and matching:
                raise SupersessionConflictError("a value correction must supersede the active category/currency revision")
            if supersedes is not None and (len(matching) != 1 or matching[0].entry_id != supersedes):
                raise SupersessionConflictError("a correction must supersede the current active value revision")
            predecessor_claim_id = None
            if supersedes is not None:
                predecessor = next((item for item in current_values if item.entry_id == supersedes), None)
                predecessor_claim = next((item for item in current_claims if item.value_entry_id == supersedes), None)
                if predecessor is None or predecessor_claim is None:
                    raise ValueValidationError("corrected value has no immutable claim predecessor")
                predecessor_claim_id = predecessor_claim.claim_revision_id
            entry = ValueEntry(
                value_id, context.scope.canonical_key, group_id, category,
                amount_text, currency, event_at, now, supersedes, claim_id,
                evidence_hash, cost_model_identity, rate_policy_identity, maturity,
            )
            repo.append(entry)
            _append_revision(transaction, entry)
            claim_value = ClaimRevision(
                claim_id, group_id, value_id, context.principal.subject,
                owner, event_at, now, evidence, attribution, coverage_numerator,
                coverage_denominator, predecessor_claim_id,
            )
            if claim_value.supersedes is not None:
                prior = next((item for item in current_claims if item.claim_revision_id == claim_value.supersedes), None)
                if prior is None or prior.value_entry_id != supersedes or prior.known_at >= claim_value.known_at:
                    raise ValueValidationError("claim correction identity does not match its predecessor")
                if any(item.supersedes == prior.claim_revision_id for item in current_claims):
                    raise ValueValidationError("a claim revision cannot have branching successors")
            elif supersedes is not None:
                raise ValueValidationError("a corrected value requires a linked claim correction")
            next_state = dict(current)
            next_state["claim_revisions"] = [*current.get("claim_revisions", []), claim_value.as_dict()]
            return next_state

        return self.commands.execute(
            context,
            command_type="RecordOutcomeValueRevision",
            aggregate_type=OUTCOME_GROUP_AGGREGATE,
            aggregate_id=group_id,
            payload=payload,
            required_capability=VALUE_SUBMIT_CAPABILITY,
            transactional_effect=append,
        )

    def void_value_revision(
        self,
        context: CommandContext,
        *,
        group_id: str,
        value_entry_id: str,
    ) -> CommandResult:
        """Append a non-monetary revision that voids the current value leaf."""

        self.current_authorization.authorize(context.principal, context.scope, VALUE_SUBMIT_CAPABILITY)
        group_id = validate_identity(group_id, "group_id")
        value_entry_id = validate_identity(value_entry_id, "value_entry_id")
        void_id = _command_record_id("value-void", context)
        void_claim_id = _command_record_id("claim-void", context)
        payload = {"group_id": group_id, "value_entry_id": value_entry_id}

        def append_void(transaction: object, current: Mapping[str, object], _raw: Mapping[str, object]) -> Mapping[str, object]:
            if current.get("group_id") != group_id:
                raise ValueValidationError("stored outcome aggregate identity is inconsistent")
            now = _server_now(self.clock)
            values = _revision_rows(transaction, context.scope.canonical_key, group_id)
            repo = InMemoryValueRepository(values)
            target = next((item for item in values if item.entry_id == value_entry_id), None)
            current_leaf = next(
                (
                    item for item in repo.active_leaves_as_of(now)
                    if item.category == (target.category if target is not None else None)
                    and item.currency == (target.currency if target is not None else None)
                ),
                None,
            )
            if target is None:
                raise AggregateNotFoundError("outcome value revision is not available in the requested scope")
            if target.revision_kind is ValueRevisionKind.VOID or current_leaf is None or current_leaf.entry_id != target.entry_id:
                raise SupersessionConflictError("only the current active value revision can be voided")
            if target.claim_revision_id is None:
                raise ValueValidationError("void target has no immutable claim identity")
            prior_claim = next(
                (item for item in _claim_revisions(current) if item.claim_revision_id == target.claim_revision_id),
                None,
            )
            if prior_claim is None or prior_claim.known_at >= now:
                raise ValueValidationError("void target has no later-known claim predecessor")
            if any(item.supersedes == prior_claim.claim_revision_id for item in _claim_revisions(current)):
                raise ValueValidationError("a claim revision cannot have branching successors")
            void_claim = ClaimRevision(
                void_claim_id,
                group_id,
                void_id,
                context.principal.subject,
                prior_claim.owner,
                target.event_at,
                now,
                prior_claim.evidence_ids,
                prior_claim.attribution,
                prior_claim.coverage_numerator,
                prior_claim.coverage_denominator,
                prior_claim.claim_revision_id,
            )
            void = ValueEntry(
                void_id,
                target.scope,
                target.group_id,
                target.category,
                None,
                target.currency,
                target.event_at,
                now,
                target.entry_id,
                void_claim_id,
                target.evidence_identity,
                target.cost_model_identity,
                target.rate_policy_identity,
                target.maturity,
                ValueRevisionKind.VOID,
            )
            repo.append(void)
            _append_revision(transaction, void)
            next_state = dict(current)
            next_state["claim_revisions"] = [*current.get("claim_revisions", []), void_claim.as_dict()]
            return next_state

        return self.commands.execute(
            context,
            command_type="VoidOutcomeValueRevision",
            aggregate_type=OUTCOME_GROUP_AGGREGATE,
            aggregate_id=group_id,
            payload=payload,
            required_capability=VALUE_SUBMIT_CAPABILITY,
            transactional_effect=append_void,
        )

    def review_value(
        self,
        context: CommandContext,
        *,
        group_id: str,
        value_entry_id: str,
        decision: ReviewDecision,
        knowledge_cutoff: datetime,
        rationale: str,
        supersedes_review_id: str | None = None,
    ) -> CommandResult:
        """Append a separate reviewer transition bound to exact value/evidence facts."""

        self.current_authorization.authorize(context.principal, context.scope, VALUE_VALIDATE_CAPABILITY)
        group_id = validate_identity(group_id, "group_id")
        value_entry_id = validate_identity(value_entry_id, "value_entry_id")
        if not isinstance(decision, ReviewDecision):
            try:
                decision = ReviewDecision(decision)
            except (TypeError, ValueError) as exc:
                raise ValueValidationError("review decision must be APPROVED or REJECTED") from exc
        cutoff = validate_timestamp(knowledge_cutoff, "knowledge_cutoff")
        if cutoff > _server_now(self.clock):
            raise ValueValidationError("knowledge cutoff cannot be in the future")
        rationale = validate_identity(rationale, "rationale")
        if supersedes_review_id is not None:
            supersedes_review_id = validate_identity(supersedes_review_id, "supersedes_review_id")
        review_id = _command_record_id("review", context)
        payload = {
            "decision": decision.value,
            "knowledge_cutoff": cutoff.isoformat(),
            "rationale": rationale,
            "supersedes_review_id": supersedes_review_id,
            "value_entry_id": value_entry_id,
            "group_id": group_id,
        }

        def append_review(transaction: object, current: Mapping[str, object], raw: Mapping[str, object]) -> Mapping[str, object]:
            if current.get("group_id") != group_id:
                raise ValueValidationError("stored outcome aggregate identity is inconsistent")
            current_values = _revision_rows(transaction, context.scope.canonical_key, group_id)
            current_target = next((item for item in current_values if item.entry_id == value_entry_id), None)
            if current_target is None:
                raise AggregateNotFoundError("outcome value revision is not available in the requested scope")
            if current_target.revision_kind is ValueRevisionKind.VOID:
                raise SupersessionConflictError("a void revision cannot be reviewed")
            current_claim = next((item for item in _claim_revisions(current) if item.claim_revision_id == current_target.claim_revision_id), None)
            if current_claim is None:
                raise ValueValidationError("value revision has no immutable claim identity")
            if current_target not in InMemoryValueRepository(current_values).active_leaves_as_of(cutoff):
                raise SupersessionConflictError("review target is no longer active at the supplied cutoff")
            if context.principal.subject == current_claim.claimant and not ALLOW_SELF_VALIDATION:
                raise AuthorizationDeniedError("claimants cannot independently validate their own outcome")
            if current_target.category not in {VALIDATED_BENEFIT, OPERATING_COST}:
                raise ValueValidationError("only benefit and operating-cost values have an independent value sign-off")
            if decision is ReviewDecision.APPROVED and current_target.maturity is not EvidenceMaturity.OBSERVED:
                raise ValueValidationError("pending, rejected, censored or insufficient evidence cannot be approved")
            if current_target.evidence_identity is None or current_target.cost_model_identity is None:
                raise ValueValidationError("review requires evidence and cost-model identities")
            now = _server_now(self.clock)
            review = ReviewRevision(
                review_id, group_id, value_entry_id, current_claim.claim_revision_id,
                current_target.evidence_identity, current_target.cost_model_identity,
                current_target.rate_policy_identity, cutoff, context.principal.subject,
                decision, now, rationale, supersedes_review_id,
            )
            current_reviews = _review_revisions(current)
            for previous in current_reviews:
                if previous.value_entry_id == value_entry_id and previous.known_at <= now and previous.supersedes is None and supersedes_review_id is None:
                    raise SupersessionConflictError("a later review must supersede the current review")
            if supersedes_review_id is not None:
                previous = next((item for item in current_reviews if item.review_id == supersedes_review_id), None)
                if previous is None or previous.value_entry_id != value_entry_id or previous.known_at >= review.known_at:
                    raise SupersessionConflictError("review successor must preserve its target and be later-known")
                if any(item.supersedes == previous.review_id for item in current_reviews):
                    raise SupersessionConflictError("a review revision cannot have branching successors")
            next_state = dict(current)
            next_state["review_revisions"] = [*current.get("review_revisions", []), review.as_dict()]
            return next_state

        return self.commands.execute(
            context,
            command_type="ReviewOutcomeValue",
            aggregate_type=OUTCOME_GROUP_AGGREGATE,
            aggregate_id=group_id,
            payload=payload,
            required_capability=VALUE_VALIDATE_CAPABILITY,
            transactional_effect=append_review,
        )

    def query(
        self,
        principal: Principal,
        scope: AccessScope,
        *,
        event_period: EventPeriod,
        knowledge_cutoff: datetime,
        currency: str | None = None,
        maturity: str | None = None,
    ) -> OutcomesQueryResult:
        """Authorized bounded read; authorization precedes every aggregate lookup."""

        self.current_authorization.authorize(principal, scope, VALUE_READ_CAPABILITY)
        if not isinstance(event_period, EventPeriod):
            raise ValueValidationError("event_period must be an EventPeriod")
        cutoff = validate_timestamp(knowledge_cutoff, "knowledge_cutoff")
        if cutoff > _server_now(self.clock):
            raise ValueValidationError("knowledge cutoff cannot be in the future")
        if currency is not None:
            currency = validate_currency(currency)
        allowed_maturities = {"ALL", "PENDING", "OBSERVED", "OBSERVED_NOT_VALIDATED", "VALIDATED", "REJECTED", "CENSORED", "INSUFFICIENT_EVIDENCE", "ZERO", "NEGATIVE", "VOID"}
        if maturity is not None:
            maturity = validate_identity(maturity, "maturity").upper()
            if maturity not in allowed_maturities:
                raise ValueValidationError("unsupported evidence-maturity filter")
        query_identity = _canonical_identity("query", {
            "scope": scope.canonical_key,
            "event_period": {
                "start": _timestamp_identity(event_period.start),
                "end": _timestamp_identity(event_period.end),
            },
            "knowledge_cutoff": _timestamp_identity(cutoff),
            "currency": currency,
            "maturity": maturity or "ALL",
        })
        snapshots = self.repository.list_groups(scope, limit=MAX_OUTCOME_GROUPS + 1)
        if len(snapshots) > MAX_OUTCOME_GROUPS:
            reason = "OUTCOME_QUERY_LIMIT_EXCEEDED"
            result_identity = _outcomes_result_identity(
                query_identity, state="PARTIAL", rows=(), summaries=(), currencies=(),
                restated=False, excluded_count=0, reason=reason,
            )
            return OutcomesQueryResult(
                "PARTIAL", (), (), (), cutoff, event_period, False, 0,
                query_identity, result_identity, reason,
            )

        rows: list[OutcomeRecord] = []
        restated = False
        all_currencies: set[str] = set()
        excluded_count = 0
        for snapshot in snapshots:
            state = snapshot.state
            if state.get("scope_key") != scope.canonical_key:
                raise ValueValidationError("outcome aggregate scope identity is inconsistent")
            group_id = validate_identity(state.get("group_id"), "group_id")
            event_key = validate_identity(state.get("economic_event_key"), "economic_event_key")
            values = _value_entries(state)
            value_repo = InMemoryValueRepository(values)
            leaves = value_repo.active_leaves_as_of(cutoff)
            claims = _claim_revisions(state)
            known_claims = tuple(item for item in claims if item.known_at <= cutoff)
            claim_by_revision = {item.claim_revision_id: item for item in known_claims}
            reviews = _review_revisions(state)
            review_leaves = _append_only_leaf(
                reviews, id_field="review_id", supersedes_field="supersedes",
                cutoff=cutoff, known_field="known_at",
            )
            review_by_value: dict[str, ReviewRevision] = {}
            for review in review_leaves:
                if isinstance(review, ReviewRevision):
                    review_by_value[review.value_entry_id] = review
            known_values = tuple(item for item in values if item.known_at <= cutoff)
            by_id = {item.entry_id: item for item in known_values}
            for leaf in leaves:
                if leaf.group_id != group_id:
                    continue
                claim = claim_by_revision.get(leaf.claim_revision_id)
                if claim is None:
                    excluded_count += 1
                    continue
                if leaf.currency not in all_currencies:
                    all_currencies.add(leaf.currency)
                if currency is not None and leaf.currency != currency:
                    excluded_count += 1
                    continue
                chain: list[ValueEntry] = []
                cursor = leaf
                while cursor is not None:
                    chain.append(cursor)
                    cursor = by_id.get(cursor.supersedes) if cursor.supersedes else None
                chain_intersects_period = any(event_period.contains(item.event_at) for item in chain)
                if chain_intersects_period and any(
                    item.supersedes is not None
                    and item.supersedes in by_id
                    and item.known_at > by_id[item.supersedes].event_at
                    for item in chain
                ):
                    restated = True
                if not event_period.contains(leaf.event_at):
                    if chain_intersects_period and any(item.supersedes is not None for item in chain):
                        restated = True
                    excluded_count += 1
                    continue
                review = review_by_value.get(leaf.entry_id)
                if review is not None and (
                    review.claim_revision_id != claim.claim_revision_id
                    or review.evidence_identity != leaf.evidence_identity
                    or review.cost_model_identity != leaf.cost_model_identity
                    or review.rate_policy_identity != leaf.rate_policy_identity
                    or review.reviewer == claim.claimant
                ):
                    review = None
                state_label = self._state_label(leaf, review)
                amount_state = (
                    "VOID" if leaf.revision_kind is ValueRevisionKind.VOID
                    else "ZERO" if leaf.amount == 0
                    else "NEGATIVE" if leaf.amount is not None and leaf.amount < 0
                    else "POSITIVE"
                )
                if maturity is not None and maturity != "ALL":
                    matches = {
                        state_label,
                        leaf.maturity.value,
                        amount_state,
                    }
                    if maturity not in matches:
                        excluded_count += 1
                        continue
                age = max(0, int((cutoff - claim.known_at).total_seconds())) if state_label == "PENDING" else None
                rows.append(OutcomeRecord(
                    group_id, event_key, snapshot.version, leaf, claim, review,
                    state_label, amount_state, leaf.supersedes is not None, age,
                ))

        rows.sort(key=lambda row: (
            row.group_id, row.value.category, row.value.currency,
            _timestamp_identity(row.value.event_at), _timestamp_identity(row.value.known_at),
            row.value.entry_id,
        ))
        summaries = self._summaries(rows)
        state = "READY" if rows else "EMPTY"
        sorted_currencies = tuple(sorted(all_currencies))
        result_identity = _outcomes_result_identity(
            query_identity, state=state, rows=rows, summaries=summaries,
            currencies=sorted_currencies, restated=restated, excluded_count=excluded_count,
        )
        return OutcomesQueryResult(
            state, tuple(rows), summaries, sorted_currencies, cutoff, event_period,
            restated, excluded_count, query_identity, result_identity,
        )

    @staticmethod
    def _state_label(value: ValueEntry, review: ReviewRevision | None) -> str:
        if value.revision_kind is ValueRevisionKind.VOID:
            return "VOID"
        if value.maturity is EvidenceMaturity.REJECTED:
            return "REJECTED"
        if value.maturity is EvidenceMaturity.CENSORED:
            return "CENSORED"
        if value.maturity is EvidenceMaturity.INSUFFICIENT_EVIDENCE:
            return "INSUFFICIENT_EVIDENCE"
        if value.maturity is EvidenceMaturity.PENDING:
            return "PENDING"
        if review is not None and review.decision is ReviewDecision.APPROVED:
            return "VALIDATED"
        if review is not None and review.decision is ReviewDecision.REJECTED:
            return "REJECTED"
        return "OBSERVED_NOT_VALIDATED" if value.category == OBSERVED_OUTCOME else "PENDING"

    @staticmethod
    def _summaries(rows: list[OutcomeRecord]) -> tuple[OutcomeCurrencySummary, ...]:
        currencies = sorted({row.value.currency for row in rows})
        summaries = []
        for currency in currencies:
            current = [row for row in rows if row.value.currency == currency]
            contributing = [row for row in current if row.value.amount is not None]
            estimated = _exact_sum(row.value.amount for row in contributing if row.value.category == ESTIMATED_OPPORTUNITY and row.value.amount is not None)
            observed = _exact_sum(row.value.amount for row in contributing if row.value.category == OBSERVED_OUTCOME and row.value.maturity is EvidenceMaturity.OBSERVED and row.value.amount is not None)
            benefit = _exact_sum(row.value.amount for row in contributing if row.value.category == VALIDATED_BENEFIT and row.state == "VALIDATED" and row.value.amount is not None)
            cost = _exact_sum(row.value.amount for row in contributing if row.value.category == OPERATING_COST and row.state == "VALIDATED" and row.value.amount is not None)
            summaries.append(OutcomeCurrencySummary(
                currency, estimated, observed, benefit, cost, _exact_sum((benefit, -cost)),
                len({row.group_id for row in contributing}),
                len({row.group_id for row in contributing if row.claim.coverage_denominator is not None}),
                sum(row.state == "PENDING" for row in contributing),
                sum(row.state == "VALIDATED" for row in contributing),
            ))
        return tuple(summaries)
