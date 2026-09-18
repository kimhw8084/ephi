"""Storage-neutral transaction contracts for the durable command core."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AggregateSnapshot:
    scope_key: str
    aggregate_type: str
    aggregate_id: str
    version: int
    state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredCommandReceipt:
    scope_key: str
    subject: str
    command_id: str
    payload_hash: str
    status: str
    result_identity: str
    result_json: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    auth_session_revision_json: str
    security_revision_json: str
    committed_at: str


class ReceiptAlreadyExistsError(Exception):
    """A durable unique receipt key was won by another transaction."""


class CommandUnitOfWork(Protocol):
    """The bounded local transaction used by the command executor."""

    def get_command_receipt(self, scope_key: str, subject: str, command_id: str) -> StoredCommandReceipt | None: ...

    def get_aggregate(
        self,
        scope_key: str,
        aggregate_type: str,
        aggregate_id: str,
        *,
        for_update: bool = False,
    ) -> AggregateSnapshot | None: ...

    def update_aggregate(
        self,
        scope_key: str,
        aggregate_type: str,
        aggregate_id: str,
        *,
        expected_version: int,
        next_version: int,
        state_json: str,
    ) -> int: ...

    def append_audit(
        self,
        *,
        event_id: str,
        scope_key: str,
        subject: str,
        command_id: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        event_json: str,
        auth_session_revision_json: str,
        security_revision_json: str,
        recorded_at: str,
    ) -> None: ...

    def append_outbox(
        self,
        *,
        event_id: str,
        scope_key: str,
        subject: str,
        command_id: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        payload_json: str,
        status: str,
        created_at: str,
    ) -> None: ...

    def insert_receipt(
        self,
        *,
        scope_key: str,
        subject: str,
        command_id: str,
        payload_hash: str,
        status: str,
        result_identity: str,
        result_json: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        auth_session_revision_json: str,
        security_revision_json: str,
        committed_at: str,
    ) -> None: ...


@runtime_checkable
class CommandStorage(Protocol):
    """A storage adapter that can supply one bounded command transaction."""

    def command_transaction(self) -> AbstractContextManager[CommandUnitOfWork]: ...
