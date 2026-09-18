"""Offline contract checks for the generic CHG-129 read substrate."""

from datetime import datetime, timezone
import sys
import unittest


sys.path.insert(0, "src")

from ephi.application import (  # noqa: E402
    AccessScope,
    CursorPageToken,
    QueryCursorValidationError,
    ReadRevisionDraft,
    ReadRevisionIdentity,
    RevisionVector,
    RetainedQuerySnapshot,
    VersionedReadRow,
    canonical_query_identity,
    snapshot_token_binding,
)
from ephi.infrastructure import AggregateSnapshot  # noqa: E402


class ReadContractTests(unittest.TestCase):
    def setUp(self):
        self.scope = AccessScope("scope-1", site_id="site-1")
        self.vector = RevisionVector("analysis-1", "exposure-1", "priority-1", 4, None, "manifest-1")
        self.aggregate = AggregateSnapshot(self.scope.canonical_key, "workflow", "entity-1", 4, {"state": "open"})

    def test_revision_contract_requires_explicit_vector_and_matching_workflow_version(self):
        identity = ReadRevisionIdentity("revision-1", self.scope, "fixture", "entity-1")
        with self.assertRaises(TypeError):
            RevisionVector("analysis-1", None, None, None, None, "manifest-1")
        with self.assertRaises(Exception):
            ReadRevisionDraft(identity, self.vector, {"value": 1}, AggregateSnapshot(self.scope.canonical_key, "workflow", "entity-1", 3, {}))
        draft = ReadRevisionDraft(identity, self.vector, {"value": 1}, self.aggregate)
        self.assertEqual(draft.revision_vector.workflow_version, 4)

    def test_query_identity_and_cursor_are_deterministic_and_tamper_evident(self):
        query = {"query": "fixture", "filters": {"state": "OPEN"}, "sort": [{"field": "row_id", "direction": "asc"}]}
        normalized, query_hash = canonical_query_identity({"sort": query["sort"], "filters": query["filters"], "query": query["query"]})
        self.assertEqual(normalized, query)
        snapshot = RetainedQuerySnapshot(
            "snapshot-1",
            query_hash,
            self.scope,
            "subject-1",
            9,
            "read.fixture",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
            2,
            snapshot_token_binding("snapshot-1", query_hash, self.scope, "subject-1", 9, "read.fixture"),
        )
        token = CursorPageToken.create(snapshot, 2)
        self.assertEqual(CursorPageToken.decode(token.encode()), token)
        tampered = CursorPageToken(token.snapshot_id, token.query_identity_hash, token.next_ordinal + 1, token.integrity)
        with self.assertRaises(QueryCursorValidationError):
            tampered.verify(snapshot)

    def test_versioned_rows_reject_missing_or_unsafe_identity(self):
        with self.assertRaises(Exception):
            VersionedReadRow("", 1, {"value": 1})
        with self.assertRaises(Exception):
            VersionedReadRow("row-1", True, {"value": 1})
        with self.assertRaises(Exception):
            VersionedReadRow("row-1", 1, {"access_token": "not-domain-data"})


if __name__ == "__main__":
    unittest.main()
