"""CHG-134 PostgreSQL adapter lifecycle and outage/reconnect evidence."""

import os
from pathlib import Path
import sys
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    AuthorizationDeniedError,
    CommandContext,
    EpisodeBriefQueryService,
    EpisodeWorkflowCommandService,
    Principal,
    QuerySnapshotExpiredError,
    RevisionVector,
    StorageFailureError,
    VersionedAggregateCommandExecutor,
)
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class PostgreSQLAdapterLifecycleTests(unittest.TestCase):
    capability = "ephi.fixture.write"

    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.adapter.connection.execute(
            "TRUNCATE o3_attention_projection, query_snapshot_row, query_snapshot, read_head, read_revision, "
            "outbox_event, audit_event, command_receipt, aggregate_state"
        )
        self.scope = AccessScope("lifecycle-scope", site_id="site-1")
        self.principal = Principal(
            "lifecycle-user",
            (self.capability, "ephi.attention.read", "ephi.episode.read", "ephi.episode.claim"),
            (self.scope,),
            1,
            1,
        )
        self.adapter.seed_aggregate(self.scope, "fixture", "aggregate-1", {"effect_count": 0})
        self.adapter.seed_aggregate(
            self.scope,
            "episode_workflow",
            "episode-1",
            {"work_state": "OPEN", "owner": None},
        )
        self.adapter.seed_attention_projection(
            self.scope,
            "episode-1",
            {"title": "Lifecycle Attention case", "priority": "P1", "source_state": "READY"},
        )
        workflow = self.adapter.get_aggregate(self.scope, "episode_workflow", "episode-1")
        self.adapter.publish_current_revision(
            self.scope,
            "episode",
            "episode-1",
            "episode-read-1",
            RevisionVector("analysis-1", "exposure-1", "priority-1", 0, None, "manifest-1"),
            {"title": "Lifecycle Attention case", "capability_state": {"source": "READY"}},
            workflow,
        )

    def context(self, command_id: str, *, expected: int = 0, principal=None) -> CommandContext:
        return CommandContext(
            command_id,
            principal or self.principal,
            self.scope,
            expected,
            RevisionVector("analysis-1", "exposure-1", "priority-1", expected, None, "manifest-1"),
            "lifecycle regression",
        )

    def execute(self, command_id: str = "command-1", *, expected: int = 0):
        return VersionedAggregateCommandExecutor(self.adapter).execute(
            self.context(command_id, expected=expected),
            command_type="FixtureCommand",
            aggregate_type="fixture",
            aggregate_id="aggregate-1",
            payload={"value": 1},
            required_capability=self.capability,
            effect=lambda state, _payload: {**state, "effect_count": state["effect_count"] + 1},
        )

    def terminate_backend(self, adapter: PostgreSQLReferenceTransactionAdapter) -> None:
        backend_pid = adapter.connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        killer = self.adapter._psycopg.connect(DSN, autocommit=True, row_factory=self.adapter._row_factory)
        try:
            killer.execute("SELECT pg_terminate_backend(%s)", (backend_pid,)).fetchone()
        finally:
            killer.close()

    def induce_server_termination(self, adapter=None):
        adapter = adapter or self.adapter
        old_connection = adapter.connection
        self.terminate_backend(adapter)
        with self.assertRaises(Exception):
            old_connection.execute("SELECT 1")
        return old_connection

    def test_explicit_close_is_terminal(self):
        self.adapter.close()
        with self.assertRaises(StorageFailureError):
            _ = self.adapter.connection

    def test_server_terminated_connection_is_replaced_on_next_operation(self):
        original = self.induce_server_termination()
        replacement = self.adapter.connection
        self.assertIsNot(original, replacement)
        self.assertTrue(replacement.autocommit)
        self.assertIs(replacement.row_factory, self.adapter._row_factory)
        self.assertEqual(self.adapter.get_aggregate(self.scope, "fixture", "aggregate-1").state["effect_count"], 0)

    def test_failed_in_flight_transaction_is_not_retried(self):
        command = self.context("failed-in-flight")
        transaction = self.adapter.command_transaction()
        with self.assertRaises(StorageFailureError):
            with transaction as unit:
                self.assertIsNotNone(unit.get_aggregate(self.scope.canonical_key, "fixture", "aggregate-1"))
                self.terminate_backend(self.adapter)
                unit.update_aggregate(
                    self.scope.canonical_key,
                    "fixture",
                    "aggregate-1",
                    expected_version=0,
                    next_version=1,
                    state_json='{"effect_count":1}',
                )
                unit.insert_receipt(
                    scope_key=self.scope.canonical_key,
                    subject=command.principal.subject,
                    command_id=command.command_id,
                    payload_hash="not-committed",
                    status="COMMITTED",
                    result_identity="not-committed",
                    result_json="{}",
                    aggregate_type="fixture",
                    aggregate_id="aggregate-1",
                    aggregate_version=1,
                    auth_session_revision_json="1",
                    security_revision_json="1",
                    committed_at="2026-09-19T00:00:00Z",
                )
        self.assertIsNone(self.adapter.get_command_receipt(self.scope.canonical_key, command.principal.subject, command.command_id))
        self.assertEqual(self.adapter.get_aggregate(self.scope, "fixture", "aggregate-1").state["effect_count"], 0)

    def test_same_adapter_reconnects_for_o3_read_and_fresh_authorized_command(self):
        original = self.induce_server_termination()
        self.assertIsNot(original, self.adapter.connection)
        from ephi.application.attention import AttentionQueryService

        attention = AttentionQueryService(self.adapter.o3_store(), self.adapter.read_store())
        page = attention.list_attention(self.principal, self.scope, page_size=1)
        self.assertEqual([row.episode_id for row in page.rows], ["episode-1"])
        workflow = EpisodeWorkflowCommandService(self.adapter)
        committed = workflow.claim_episode(self.context("claim-1"), "episode-1")
        self.assertEqual(committed.aggregate_version, 1)
        brief = EpisodeBriefQueryService(self.adapter.read_store()).get_episode_brief(
            self.principal, self.scope, "episode-1"
        )
        self.assertEqual(brief.workflow["work_state"], "CLAIMED")

    def test_expired_retained_attention_snapshot_requires_authorized_refresh(self):
        from ephi.application.attention import AttentionQueryService

        attention = AttentionQueryService(self.adapter.o3_store(), self.adapter.read_store())
        first = attention.list_attention(self.principal, self.scope, page_size=1)
        rotated = Principal(
            self.principal.subject,
            self.principal.capabilities,
            self.principal.scope_grants,
            self.principal.auth_session_revision,
            self.principal.security_revision + 1,
        )
        with self.assertRaises(QuerySnapshotExpiredError):
            attention.list_attention(
                rotated,
                self.scope,
                snapshot_id=first.snapshot_id,
                cursor=first.next_cursor,
                page_size=1,
            )
        renewed = attention.list_attention(rotated, self.scope, page_size=1)
        self.assertNotEqual(renewed.snapshot_id, first.snapshot_id)
        self.assertEqual([row.episode_id for row in renewed.rows], ["episode-1"])
        revoked = Principal(
            rotated.subject,
            (),
            rotated.scope_grants,
            rotated.auth_session_revision,
            rotated.security_revision,
        )
        with self.assertRaises(AuthorizationDeniedError):
            attention.list_attention(revoked, self.scope, page_size=1)

    def test_concurrent_reconnect_observation_has_one_replacement_owner(self):
        original = self.induce_server_termination()
        barrier = threading.Barrier(8)
        connections = []
        failures = []

        def observe():
            try:
                barrier.wait(timeout=10)
                connections.append(self.adapter.connection)
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=observe) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(failures, failures)
        self.assertEqual(len({id(connection) for connection in connections}), 1)
        self.assertIsNot(original, connections[0])

    def test_receipt_replay_and_authorization_semantics_remain_unchanged(self):
        committed = self.execute()
        before = self.adapter.count_rows()
        self.induce_server_termination()
        replay = self.execute()
        self.assertEqual(replay, committed)
        self.assertEqual(self.adapter.count_rows(), before)
        revoked = Principal(self.principal.subject, (), (self.scope,), 2, 2)
        with self.assertRaises(AuthorizationDeniedError):
            VersionedAggregateCommandExecutor(self.adapter).execute(
                self.context("command-1", principal=revoked),
                command_type="FixtureCommand",
                aggregate_type="fixture",
                aggregate_id="aggregate-1",
                payload={"value": 1},
                required_capability=self.capability,
            )


if __name__ == "__main__":
    unittest.main()
