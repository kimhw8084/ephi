"""Immutable value records and strict temporal/monetary validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
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
    amount: Decimal
    currency: str
    event_at: datetime
    known_at: datetime
    supersedes: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_id", validate_identity(self.entry_id, "entry_id"))
        object.__setattr__(self, "scope", validate_identity(self.scope, "scope"))
        object.__setattr__(self, "group_id", validate_identity(self.group_id, "group_id"))
        object.__setattr__(self, "category", validate_identity(self.category, "category"))
        object.__setattr__(self, "amount", parse_amount(self.amount))
        object.__setattr__(self, "currency", validate_currency(self.currency))
        object.__setattr__(self, "event_at", validate_timestamp(self.event_at, "event_at"))
        object.__setattr__(self, "known_at", validate_timestamp(self.known_at, "known_at"))
        if self.supersedes is not None:
            object.__setattr__(self, "supersedes", validate_identity(self.supersedes, "supersedes"))

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
            "amount": str(self.amount),
            "currency": self.currency,
            "event_at": self.event_at.isoformat(),
            "known_at": self.known_at.isoformat(),
            "supersedes": self.supersedes,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)


def decimal_json_default(value: object) -> str:
    """JSON encoder hook that makes Decimal monetary values decimal strings."""

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot be serialized")
        return str(value)
    if isinstance(value, datetime):
        return validate_timestamp(value, "timestamp").isoformat()
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")
