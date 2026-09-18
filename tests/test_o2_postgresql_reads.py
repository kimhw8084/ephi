"""CHG-129 PostgreSQL reference/integration evidence for generic reads."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    AuthorizationDeniedError,
    CoherentReadConflictError,
    CursorPageToken,
    Principal,
    QueryCursorValidationError,
    QueryIdentityMismatchError,
    QuerySnapshotExpiredError,
    QueryTooBroadError,
    ReadRevisionConflictError,
    ReadRevisionDraft,
    ReadRevisionIdentity,
    RevisionVector,
    ScopeDeniedError,
    VersionedReadRow,
    canonical_json,
    canonical_query_identity,
)
from ephi.infrastructure import AggregateSnapshot, PostgreSQLReferenceTransactionAdapter  # noqa: E402


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLReadSnapshotTests(unittest.TestCase):
    capability = "o2.fixture.read"

    def setUp(self):
        self.store = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.store.close)
        self.store.connection.execute(
            "TRUNCATE query_snapshot_row, query_snapshot, read_head, read_revision, aggregate_state"
        )
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS ephi_test_read_source (row_id TEXT PRIMARY KEY, row_version BIGINT NOT NULL, sort_key INTEGER NOT NULL, payload_json JSONB NOT NULL)"
        )
        self.store.connection.execute("TRUNCATE ephi_test_read_source")
        self.scope = AccessScope("read-scope-1", site_id="site-1", area_id="area-1")
        self.principal = Principal(
            "read-subject-1",
            (self.capability,),
            (self.scope,),
            auth_session_revision=10,
            security_revision=20,
        )
        self.workflow_v1 = self.store.seed_aggregate(
            self.scope,
            "workflow",
            "entity-1",
            {"state": "OPEN", "owner": "team-a"},
            version=1,
        )

    def vector(self, version: int, suffix: str) -> RevisionVector:
        return RevisionVector(f"analysis-{suffix}", f"exposure-{suffix}", f"priority-{suffix}", version, None, f"manifest-{suffix}")

    def publish(self, revision_id: str, version: int, suffix: str, *, expected_head_version=None, workflow=None):
        return self.store.publish_current_revision(
            self.scope,
            "fixture",
            "entity-1",
            revision_id,
            self.vector(version, suffix),
            {"revision": revision_id, "value": version},
            workflow or self.store.get_aggregate(self.scope, "workflow", "entity-1"),
            expected_head_version=expected_head_version,
        )

    def update_workflow(self, version: int, state: dict[str, object]):
        self.store.connection.execute("BEGIN")
        try:
            self.store.connection.execute(
                "UPDATE aggregate_state SET version = %s, state_json = %s::jsonb WHERE scope_key = %s AND aggregate_type = 'workflow' AND aggregate_id = 'entity-1'",
                (version, canonical_json(state), self.scope.canonical_key),
            )
            self.store.connection.commit()
        except Exception:
            self.store.connection.rollback()
            raise
        return self.store.get_aggregate(self.scope, "workflow", "entity-1")

    def draft(self, revision_id: str, version: int, suffix: str, workflow: AggregateSnapshot) -> ReadRevisionDraft:
        return ReadRevisionDraft(
            ReadRevisionIdentity(revision_id, self.scope, "fixture", "entity-1"),
            self.vector(version, suffix),
            {"revision": revision_id, "value": version},
            workflow,
        )

    def source_rows(self):
        rows = self.store.connection.execute(
            "SELECT row_id, row_version, payload_json FROM ephi_test_read_source ORDER BY sort_key ASC, row_id ASC"
        ).fetchall()
        return tuple(VersionedReadRow(row["row_id"], int(row["row_version"]), row["payload_json"]) for row in rows)

    def seed_source(self, values):
        for row_id, version, sort_key, payload in values:
            self.store.connection.execute(
                "INSERT INTO ephi_test_read_source(row_id, row_version, sort_key, payload_json) VALUES (%s, %s, %s, %s::jsonb)",
                (row_id, version, sort_key, canonical_json(payload)),
            )

    def query(self):
        return {"query": "fixture", "filters": {"state": "OPEN"}, "sort": [{"field": "sort_key", "direction": "asc"}]}

    def test_migration_is_idempotent_and_read_schema_is_generic_only(self):
        self.store.apply_migrations()
        self.assertRegex(self.store.server_version(), r"^18\.")
        names = {
            row["table_name"]
            for row in self.store.connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name IN ('read_revision', 'read_head', 'query_snapshot', 'query_snapshot_row', 'attention_projection', 'episode')"
            ).fetchall()
        }
        self.assertEqual(names, {"read_revision", "read_head", "query_snapshot", "query_snapshot_row"})

    def test_revision_reinsert_and_direct_update_fail_without_replacement(self):
        head = self.publish("r1", 1, "r1", workflow=self.workflow_v1)
        self.assertEqual(head.head_version, 1)
        with self.assertRaises(ReadRevisionConflictError):
            self.store.publish_current_revision(
                self.scope,
                "fixture",
                "entity-1",
                "r1",
                self.vector(1, "different"),
                {"revision": "tampered"},
                self.workflow_v1,
            )
        with self.assertRaises(Exception) as raised:
            self.store.connection.execute(
                "UPDATE read_revision SET payload_json = %s::jsonb WHERE revision_id = 'r1'",
                (canonical_json({"revision": "tampered-direct"}),),
            )
        self.assertEqual(getattr(raised.exception, "sqlstate", None), "55000")
        revision = self.store.get_read_revision("r1")
        self.assertEqual(revision.payload, {"revision": "r1", "value": 1})

    def test_stale_head_publisher_cannot_overwrite_newer_head(self):
        first = self.publish("r1", 1, "r1", workflow=self.workflow_v1)
        workflow_v2 = self.update_workflow(2, {"state": "ACKNOWLEDGED", "owner": "team-b"})
        second = self.publish("r2", 2, "r2", expected_head_version=first.head_version, workflow=workflow_v2)
        self.assertEqual(second.head_version, 2)
        workflow_v3 = self.update_workflow(3, {"state": "CLOSED", "owner": "team-c"})
        with self.assertRaises(ReadRevisionConflictError):
            self.publish("r3", 3, "r3", expected_head_version=first.head_version, workflow=workflow_v3)
        self.assertEqual(self.store.get_current_head(self.scope, "fixture", "entity-1").revision_id, "r2")
        self.assertIsNone(self.store.get_read_revision("r3"))

    def test_current_bundle_is_repeatable_read_coherent_across_separate_connections(self):
        first = self.publish("r1", 1, "r1", workflow=self.workflow_v1)
        reader = PostgreSQLReferenceTransactionAdapter(DSN)
        writer = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        entered = threading.Event()
        continue_read = threading.Event()
        result = []
        failures = []

        def after_revision_read():
            entered.set()
            if not continue_read.wait(10):
                raise AssertionError("reader synchronization point was not released")

        def read():
            try:
                result.append(
                    reader.read_current_bundle(
                        self.principal,
                        self.scope,
                        "fixture",
                        "entity-1",
                        self.capability,
                        _after_revision_read=after_revision_read,
                    )
                )
            except Exception as exc:  # pragma: no cover - assertion reports concurrency failures
                failures.append(exc)

        thread = threading.Thread(target=read)
        thread.start()
        self.assertTrue(entered.wait(10))
        workflow_v2 = AggregateSnapshot(self.scope.canonical_key, "workflow", "entity-1", 2, {"state": "ACKNOWLEDGED", "owner": "team-b"})
        writer.connection.execute("BEGIN")
        writer.connection.execute(
            "UPDATE aggregate_state SET version = 2, state_json = %s::jsonb WHERE scope_key = %s AND aggregate_type = 'workflow' AND aggregate_id = 'entity-1'",
            (canonical_json(workflow_v2.state), self.scope.canonical_key),
        )
        writer.publish_current_revision_in_transaction(
            writer.connection,
            self.draft("r2", 2, "r2", workflow_v2),
            expected_head_version=first.head_version,
        )
        writer.connection.commit()
        continue_read.set()
        thread.join(timeout=15)
        self.assertFalse(failures, failures)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].read_revision.revision_id, "r1")
        self.assertEqual(result[0].workflow_aggregate.version, 1)
        self.assertEqual(result[0].revision_vector.workflow_version, 1)
        newest = reader.read_current_bundle(self.principal, self.scope, "fixture", "entity-1", self.capability)
        self.assertEqual((newest.read_revision.revision_id, newest.workflow_aggregate.version), ("r2", 2))

    def test_historical_read_uses_stored_workflow_snapshot_after_current_advances(self):
        first = self.publish("r1", 1, "r1", workflow=self.workflow_v1)
        workflow_v2 = self.update_workflow(2, {"state": "ACKNOWLEDGED", "owner": "team-b"})
        self.publish("r2", 2, "r2", expected_head_version=first.head_version, workflow=workflow_v2)
        historical = self.store.read_historical_bundle(self.principal, self.scope, "r1", self.capability)
        self.assertEqual(historical.read_revision.revision_id, "r1")
        self.assertEqual(historical.workflow_aggregate.version, 1)
        self.assertEqual(historical.workflow_aggregate.state["state"], "OPEN")
        self.assertEqual(historical.revision_vector.workflow_version, 1)

    def test_retained_snapshot_pages_keep_order_rows_and_versions_after_source_mutation(self):
        self.seed_source(
            [
                ("row-a", 1, 1, {"value": "a1"}),
                ("row-b", 1, 2, {"value": "b1"}),
                ("row-c", 1, 3, {"value": "c1"}),
                ("row-d", 1, 4, {"value": "d1"}),
            ]
        )
        snapshot = self.store.create_query_snapshot(
            self.principal,
            self.scope,
            self.query(),
            self.capability,
            self.source_rows(),
            ttl_seconds=300,
        )
        with self.assertRaises(Exception) as snapshot_update:
            self.store.connection.execute(
                "UPDATE query_snapshot SET total_row_count = 1 WHERE snapshot_id = %s",
                (snapshot.snapshot_id,),
            )
        self.assertEqual(getattr(snapshot_update.exception, "sqlstate", None), "55000")
        first_page = self.store.read_query_snapshot_page(self.principal, self.scope, snapshot.snapshot_id, self.query(), self.capability, page_size=2)
        self.assertEqual([row.row_id for row in first_page.rows], ["row-a", "row-b"])
        with self.assertRaises(Exception) as member_update:
            self.store.connection.execute(
                "UPDATE query_snapshot_row SET row_id = 'tampered' WHERE snapshot_id = %s AND ordinal = 1",
                (snapshot.snapshot_id,),
            )
        self.assertEqual(getattr(member_update.exception, "sqlstate", None), "55000")
        self.store.connection.execute("TRUNCATE ephi_test_read_source")
        self.seed_source(
            [
                ("row-d", 2, 1, {"value": "d2"}),
                ("row-b", 2, 2, {"value": "b2"}),
                ("row-e", 1, 3, {"value": "e1"}),
            ]
        )
        second_page = self.store.read_query_snapshot_page(
            self.principal,
            self.scope,
            snapshot.snapshot_id,
            self.query(),
            self.capability,
            page_size=2,
            cursor=first_page.next_cursor,
        )
        self.assertEqual([(row.row_id, row.row_version, row.payload["value"]) for row in second_page.rows], [("row-c", 1, "c1"), ("row-d", 1, "d1")])
        refreshed = self.store.create_query_snapshot(self.principal, self.scope, self.query(), self.capability, self.source_rows())
        refreshed_page = self.store.read_query_snapshot_page(self.principal, self.scope, refreshed.snapshot_id, self.query(), self.capability, page_size=100)
        self.assertNotEqual(snapshot.snapshot_id, refreshed.snapshot_id)
        self.assertEqual([(row.row_id, row.row_version) for row in refreshed_page.rows], [("row-d", 2), ("row-b", 2), ("row-e", 1)])

    def test_cursor_identity_authorization_and_bounds_fail_closed(self):
        self.seed_source([(f"row-{index}", 1, index, {"value": index}) for index in range(1, 4)])
        snapshot = self.store.create_query_snapshot(self.principal, self.scope, self.query(), self.capability, self.source_rows())
        page = self.store.read_query_snapshot_page(self.principal, self.scope, snapshot.snapshot_id, self.query(), self.capability, page_size=1)
        self.assertIsNotNone(page.next_cursor)
        with self.assertRaises(QueryIdentityMismatchError):
            self.store.read_query_snapshot_page(self.principal, self.scope, snapshot.snapshot_id, {"query": "other", "filters": {}, "sort": []}, self.capability, page_size=1, cursor=page.next_cursor)
        other = self.store.create_query_snapshot(self.principal, self.scope, self.query(), self.capability, self.source_rows())
        with self.assertRaises(QueryCursorValidationError):
            self.store.read_query_snapshot_page(self.principal, self.scope, other.snapshot_id, self.query(), self.capability, page_size=1, cursor=page.next_cursor)
        decoded = CursorPageToken.decode(page.next_cursor)
        malformed = CursorPageToken(decoded.snapshot_id, decoded.query_identity_hash, 999, decoded.integrity).encode()
        with self.assertRaises(QueryCursorValidationError):
            self.store.read_query_snapshot_page(self.principal, self.scope, snapshot.snapshot_id, self.query(), self.capability, page_size=1, cursor=malformed)
        with self.assertRaises(QueryTooBroadError):
            self.store.read_query_snapshot_page(self.principal, self.scope, snapshot.snapshot_id, self.query(), self.capability, page_size=101)
        rotated_session = Principal(self.principal.subject, (self.capability,), (self.scope,), 11, 20)
        self.store.read_query_snapshot_page(rotated_session, self.scope, snapshot.snapshot_id, self.query(), self.capability, page_size=1)
        with self.assertRaises(AuthorizationDeniedError):
            self.store.read_query_snapshot_page(Principal(self.principal.subject, (), (self.scope,), 11, 20), self.scope, snapshot.snapshot_id, self.query(), self.capability, page_size=1)
        with self.assertRaises(ScopeDeniedError):
            self.store.read_query_snapshot_page(Principal(self.principal.subject, (self.capability,), (AccessScope("other"),), 11, 20), self.scope, snapshot.snapshot_id, self.query(), self.capability, page_size=1)
        with self.assertRaises(QuerySnapshotExpiredError):
            self.store.read_query_snapshot_page(Principal(self.principal.subject, (self.capability,), (self.scope,), 11, 21), self.scope, snapshot.snapshot_id, self.query(), self.capability, page_size=1)

    def test_expiry_and_missing_member_return_restart_query_without_live_fallback(self):
        self.seed_source([("row-a", 1, 1, {"value": "a"}), ("row-b", 1, 2, {"value": "b"})])
        expiring = self.store.create_query_snapshot(self.principal, self.scope, self.query(), self.capability, self.source_rows(), ttl_seconds=1)
        time.sleep(1.2)
        with self.assertRaises(QuerySnapshotExpiredError) as raised:
            self.store.read_query_snapshot_page(self.principal, self.scope, expiring.snapshot_id, self.query(), self.capability, page_size=1)
        self.assertTrue(raised.exception.details["restart_query"])
        retained = self.store.create_query_snapshot(self.principal, self.scope, self.query(), self.capability, self.source_rows())
        self.store.connection.execute("TRUNCATE query_snapshot_row")
        with self.assertRaises(QuerySnapshotExpiredError):
            self.store.read_query_snapshot_page(self.principal, self.scope, retained.snapshot_id, self.query(), self.capability, page_size=1)


if __name__ == "__main__":
    unittest.main()
