"""Bounded append-only repository for temporal value revisions."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from .model import (
    SupersessionConflictError,
    SupersessionError,
    UnknownPredecessorError,
    ValueEntry,
    ValueValidationError,
    validate_timestamp,
)


class ValueRepository(Protocol):
    """Typed persistence boundary required by the value service."""

    def append(self, entry: ValueEntry) -> ValueEntry:
        ...

    def all_entries(self) -> tuple[ValueEntry, ...]:
        ...

    def known_entries(self, knowledge_cutoff: datetime) -> tuple[ValueEntry, ...]:
        ...

    def active_leaves_as_of(self, knowledge_cutoff: datetime) -> tuple[ValueEntry, ...]:
        ...


class InMemoryValueRepository:
    """Small bounded repository that never rewrites or deletes revisions."""

    def __init__(self, entries: Iterable[ValueEntry] = ()) -> None:
        self._entries: dict[str, ValueEntry] = {}
        self._successors: dict[str, str] = {}
        for entry in entries:
            self.append(entry)

    def append(self, entry: ValueEntry) -> ValueEntry:
        if not isinstance(entry, ValueEntry):
            raise TypeError("repository entries must be ValueEntry")
        if entry.supersedes == entry.entry_id:
            raise SupersessionError("an entry cannot supersede itself")
        if entry.entry_id in self._entries:
            raise ValueValidationError(f"entry already exists: {entry.entry_id}")
        if entry.supersedes is None:
            self._entries[entry.entry_id] = entry
            return entry

        predecessor = self._entries.get(entry.supersedes)
        if predecessor is None:
            raise UnknownPredecessorError(f"unknown predecessor: {entry.supersedes}")
        if predecessor.identity_key != entry.identity_key:
            raise SupersessionConflictError("successor identity does not match predecessor")
        if predecessor.known_at >= entry.known_at:
            raise SupersessionError("successor known_at must be later than its predecessor")
        if entry.supersedes in self._successors:
            raise SupersessionConflictError("a predecessor may have only one successor")
        self._assert_chain_is_acyclic(entry)
        self._entries[entry.entry_id] = entry
        self._successors[entry.supersedes] = entry.entry_id
        return entry

    def _assert_chain_is_acyclic(self, entry: ValueEntry) -> None:
        """Forward-only predecessor references make cycles structurally impossible."""

        seen = {entry.entry_id}
        predecessor_id = entry.supersedes
        while predecessor_id is not None:
            if predecessor_id in seen:
                raise SupersessionError("supersession cycle detected")
            seen.add(predecessor_id)
            predecessor = self._entries.get(predecessor_id)
            if predecessor is None:
                raise UnknownPredecessorError(f"unknown predecessor: {predecessor_id}")
            predecessor_id = predecessor.supersedes

    def get(self, entry_id: str) -> ValueEntry:
        try:
            return self._entries[entry_id]
        except KeyError as exc:
            raise KeyError(f"unknown value entry: {entry_id}") from exc

    def all_entries(self) -> tuple[ValueEntry, ...]:
        return tuple(self._entries.values())

    def known_entries(self, knowledge_cutoff: datetime) -> tuple[ValueEntry, ...]:
        cutoff = validate_timestamp(knowledge_cutoff, "knowledge_cutoff")
        return tuple(entry for entry in self._entries.values() if entry.known_at <= cutoff)

    def active_leaves_as_of(self, knowledge_cutoff: datetime) -> tuple[ValueEntry, ...]:
        """Select leaves from the known revision graph, never from present state."""

        known = self.known_entries(knowledge_cutoff)
        known_ids = {entry.entry_id for entry in known}
        superseded_known_ids = {
            entry.supersedes
            for entry in known
            if entry.supersedes is not None and entry.supersedes in known_ids
        }
        return tuple(entry for entry in known if entry.entry_id not in superseded_known_ids)
