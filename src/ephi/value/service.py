"""Temporal value query service for the bounded F04 implementation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from .model import (
    EventPeriod,
    MixedCurrencyError,
    ValueEntry,
    ValueValidationError,
    validate_currency,
    validate_identity,
    validate_timestamp,
)
from .repository import InMemoryValueRepository, ValueRepository


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
        ]
        if event_period is not None:
            selected = [entry for entry in selected if event_period.contains(entry.event_at)]

        currencies = {entry.currency for entry in selected}
        if len(currencies) > 1:
            raise MixedCurrencyError("mixed-currency aggregation is unsupported")
        return sum((entry.amount for entry in selected), Decimal("0"))

    def total(self, **kwargs: object) -> Decimal:
        """Concise alias for the exact Decimal aggregate query."""

        return self.aggregate(**kwargs)  # type: ignore[arg-type]

    def entries(self) -> Iterable[ValueEntry]:
        return self.repository.all_entries()
