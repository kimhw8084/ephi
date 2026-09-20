"""CHG-150/O8.1 current-authorization freshness and ordering evidence."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    ArtifactBlobWriteResult,
    ArtifactContentIdentity,
    ArtifactMetadata,
    ArtifactService,
    AttentionQueryService,
    AuthorizationDeniedError,
    CommandContext,
    CurrentAuthorizationAuthority,
    EpisodeBriefQueryService,
    MutableCurrentAuthorizationAuthority,
    PageResult,
    Principal,
    QuerySnapshotExpiredError,
    RetainedQuerySnapshot,
    RetainedSnapshotRow,
    ScopedArtifactReference,
    VersionedAggregateCommandExecutor,
    VersionedReadRow,
)
from ephi.application.artifacts import ArtifactCatalogRegistration  # noqa: E402
from ephi.application.errors import ArtifactNotFoundError  # noqa: E402
from ephi.infrastructure import SQLiteReferenceTransactionAdapter  # noqa: E402


class _CountingCommandStore:
    def __init__(self, store):
        self.store = store
        self.transaction_count = 0
        self.receipt_reads = 0
        self.aggregate_reads = 0

    @contextmanager
    def command_transaction(self):
        self.transaction_count += 1
        with self.store.command_transaction() as transaction:
            owner = self

            class Unit:
                def get_command_receipt(self, *args, **kwargs):
                    owner.receipt_reads += 1
                    return transaction.get_command_receipt(*args, **kwargs)

                def get_aggregate(self, *args, **kwargs):
                    owner.aggregate_reads += 1
                    return transaction.get_aggregate(*args, **kwargs)

                def __getattr__(self, name):
                    return getattr(transaction, name)

            yield Unit()


class _CountingRows:
    def __init__(self, rows):
        self.rows = tuple(rows)
        self.fetch_calls = 0
        self.health_calls = 0

    def check_attention_source(self, principal, scope):
        self.health_calls += 1

    def fetch_attention_rows(self, principal, scope, filters, order):
        self.fetch_calls += 1
        return self.rows


class _RetainedStore:
    def __init__(self):
        self.snapshots = {}
        self.create_calls = 0
        self.page_calls = 0
        self.counter = 0

    def publish_read_revision(self, *args, **kwargs):
        raise NotImplementedError

    def read_current_bundle(self, *args, **kwargs):
        raise NotImplementedError

    def read_historical_bundle(self, *args, **kwargs):
        raise NotImplementedError

    def create_query_snapshot(self, principal, scope, query_identity, required_read_capability, rows, *, ttl_seconds=300):
        self.create_calls += 1
        self.counter += 1
        now = datetime.now(timezone.utc)
        snapshot = RetainedQuerySnapshot(
            f"snapshot-{self.counter}",
            "0" * 64,
            scope,
            principal.subject,
            principal.security_revision,
            required_read_capability,
            now,
            now + timedelta(seconds=ttl_seconds),
            len(rows),
        )
        self.snapshots[snapshot.snapshot_id] = (snapshot, tuple(rows))
        return snapshot

    def read_query_snapshot_page(self, principal, scope, snapshot_id, query_identity, required_read_capability, *, page_size=50, cursor=None):
        self.page_calls += 1
        snapshot, rows = self.snapshots[snapshot_id]
        if snapshot.subject != principal.subject:
            raise AuthorizationDeniedError("current authorization does not permit this operation")
        if snapshot.security_revision != principal.security_revision:
            raise QuerySnapshotExpiredError(reason="effective_security_revision_changed")
        start = int(cursor or 0)
        selected = rows[start:start + page_size]
        next_cursor = str(start + len(selected)) if start + len(selected) < len(rows) else None
        return PageResult(
            snapshot,
            tuple(
                RetainedSnapshotRow(snapshot_id, start + index + 1, row.row_id, row.row_version, row.payload)
                for index, row in enumerate(selected)
            ),
            next_cursor,
        )


class _CountingReadStore:
    def __init__(self):
        self.current_calls = 0
        self.historical_calls = 0

    def publish_read_revision(self, *args, **kwargs):
        raise NotImplementedError

    def read_current_bundle(self, *args, **kwargs):
        self.current_calls += 1
        raise AssertionError("protected current read should not be reached")

    def read_historical_bundle(self, *args, **kwargs):
        self.historical_calls += 1
        raise AssertionError("protected historical read should not be reached")

    def create_query_snapshot(self, *args, **kwargs):
        raise NotImplementedError

    def read_query_snapshot_page(self, *args, **kwargs):
        raise NotImplementedError


class _CountingBlobStore:
    def __init__(self):
        self.content = {}
        self.put_calls = 0
        self.verify_calls = 0
        self.read_calls = 0

    def put_bytes(self, content):
        self.put_calls += 1
        identity = ArtifactContentIdentity.from_bytes(content)
        self.content[identity] = content
        return ArtifactBlobWriteResult(identity, False)

    def verify(self, identity):
        self.verify_calls += 1
        if identity not in self.content:
            raise ArtifactNotFoundError("artifact is unavailable")

    def read(self, identity):
        self.read_calls += 1
        self.verify(identity)
        return self.content[identity]


class _CountingCatalog:
    def __init__(self):
        self.records = {}
        self.register_calls = 0
        self.get_calls = 0

    def register(self, metadata, *, object_key):
        self.register_calls += 1
        key = metadata.reference
        existing = self.records.get(key)
        if existing is not None:
            return ArtifactCatalogRegistration(existing, False)
        self.records[key] = metadata
        return ArtifactCatalogRegistration(metadata, True)

    def get(self, reference):
        self.get_calls += 1
        return self.records.get(reference)


class O8CurrentAuthorizationTests(unittest.TestCase):
    capability = "ephi.fixture.read"

    def setUp(self):
        self.scope = AccessScope("o8-scope", site_id="site-1")
        self.other_scope = AccessScope("o8-other-scope", site_id="site-2")
        self.principal = Principal("subject-1", (self.capability,), (self.scope,), 1, 10)
        self.authority = MutableCurrentAuthorizationAuthority(self.principal)

    def _principal(self, *, subject="subject-1", capabilities=None, scopes=None, auth=1, security=10):
        return Principal(
            subject,
            (self.capability,) if capabilities is None else capabilities,
            (self.scope,) if scopes is None else scopes,
            auth,
            security,
        )

    def test_authority_success_and_all_freshness_grant_failures_are_bounded(self):
        self.authority.authorize(self.principal, self.scope, self.capability)

        cases = (
            ("different subject", self._principal(subject="subject-2"), self.principal),
            ("stale auth session", self._principal(auth=1), self._principal(auth=2)),
            ("stale security", self._principal(security=10), self._principal(security=11)),
            ("scope removed", self.principal, self._principal(scopes=(), security=11)),
            ("capability removed", self.principal, self._principal(capabilities=(), security=11)),
        )
        for label, presented, current in cases:
            with self.subTest(label=label):
                self.authority.set_principal(current)
                with self.assertRaises(AuthorizationDeniedError) as raised:
                    self.authority.authorize(presented, self.scope, self.capability)
                self.assertEqual(raised.exception.code, "FORBIDDEN_ACTION")
                self.assertEqual(str(raised.exception), "current authorization does not permit this operation")

    def test_unavailable_authority_fails_closed_without_detail_and_never_upgrades(self):
        unavailable = CurrentAuthorizationAuthority(lambda _subject: (_ for _ in ()).throw(RuntimeError("secret")))
        with self.assertRaises(AuthorizationDeniedError) as raised:
            unavailable.authorize(self.principal, self.scope, self.capability)
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(str(raised.exception), "current authorization is unavailable")

        fresh_granted = self._principal(auth=2, security=11)
        self.authority.set_principal(fresh_granted)
        stale_presented = self._principal(capabilities=(), auth=1, security=10)
        with self.assertRaises(AuthorizationDeniedError):
            self.authority.authorize(stale_presented, self.scope, self.capability)

    def test_command_revocation_and_cross_scope_fail_before_receipt_or_aggregate_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteReferenceTransactionAdapter(Path(directory) / "o8.sqlite3")
            self.addCleanup(store.close)
            store.seed_aggregate(self.scope, "fixture", "aggregate-1", {"effect_count": 0})
            counted = _CountingCommandStore(store)
            executor = VersionedAggregateCommandExecutor(counted, self.authority)
            context = CommandContext("command-1", self.principal, self.scope, 0)
            executor.execute(
                context,
                command_type="FixtureCommand",
                aggregate_type="fixture",
                aggregate_id="aggregate-1",
                payload={"value": 1},
                required_capability=self.capability,
            )
            counted.receipt_reads = counted.aggregate_reads = counted.transaction_count = 0

            revoked = self._principal(capabilities=(), auth=2, security=11)
            self.authority.set_principal(revoked)
            with self.assertRaises(AuthorizationDeniedError):
                executor.execute(
                    context,
                    command_type="FixtureCommand",
                    aggregate_type="fixture",
                    aggregate_id="aggregate-1",
                    payload={"value": 1},
                    required_capability=self.capability,
                )
            self.assertEqual((counted.transaction_count, counted.receipt_reads, counted.aggregate_reads), (0, 0, 0))

            cross_scope = self._principal(scopes=(self.other_scope,), auth=3, security=12)
            self.authority.set_principal(cross_scope)
            cross_context = CommandContext("cross-scope", cross_scope, self.scope, 0)
            with self.assertRaises(AuthorizationDeniedError):
                executor.execute(
                    cross_context,
                    command_type="FixtureCommand",
                    aggregate_type="fixture",
                    aggregate_id="aggregate-1",
                    payload={"value": 1},
                    required_capability=self.capability,
                )
            self.assertEqual((counted.transaction_count, counted.receipt_reads, counted.aggregate_reads), (0, 0, 0))

    def test_episode_reads_and_attention_source_health_are_guarded_before_storage(self):
        read_store = _CountingReadStore()
        briefs = EpisodeBriefQueryService(read_store, self.authority)
        attention_rows = _CountingRows((VersionedReadRow("episode-1", 1, {"episode_id": "episode-1"}),))
        attention = AttentionQueryService(attention_rows, _RetainedStore(), self.authority)
        revoked = self._principal(capabilities=(), auth=2, security=11)
        self.authority.set_principal(revoked)

        with self.assertRaises(AuthorizationDeniedError):
            briefs.get_episode_brief(self.principal, self.scope, "episode-1")
        with self.assertRaises(AuthorizationDeniedError):
            briefs.get_episode_brief(self.principal, self.scope, "episode-1", revision_id="revision-1")
        with self.assertRaises(AuthorizationDeniedError):
            attention.list_attention(self.principal, self.scope, page_size=1)
        with self.assertRaises(AuthorizationDeniedError):
            attention.check_source(self.principal, self.scope)
        self.assertEqual((read_store.current_calls, read_store.historical_calls), (0, 0))
        self.assertEqual(attention_rows.fetch_calls, 0)
        self.assertEqual(attention_rows.health_calls, 0)

    def test_retained_snapshot_revision_and_subject_bindings_are_preserved(self):
        rows = _CountingRows((VersionedReadRow("episode-1", 1, {"episode_id": "episode-1"}),))
        retained = _RetainedStore()
        attention = AttentionQueryService(rows, retained, self.authority)
        attention_capability = "ephi.attention.read"
        attention_principal = Principal(
            "subject-1",
            (self.capability, attention_capability),
            (self.scope,),
            1,
            10,
        )
        self.authority.set_principal(attention_principal)
        first = attention.list_attention(attention_principal, self.scope, page_size=1)

        rotated = Principal("subject-1", (self.capability, attention_capability), (self.scope,), 2, 11)
        self.authority.set_principal(rotated)
        with self.assertRaises(QuerySnapshotExpiredError):
            attention.list_attention(
                rotated,
                self.scope,
                snapshot_id=first.snapshot_id,
                cursor=first.next_cursor,
                page_size=1,
            )
        renewed = attention.list_attention(rotated, self.scope, page_size=1)
        self.assertNotEqual(first.snapshot_id, renewed.snapshot_id)

        other_subject = Principal("subject-2", (self.capability, attention_capability), (self.scope,), 2, 11)
        self.authority.set_principal(other_subject)
        with self.assertRaises(AuthorizationDeniedError):
            attention.list_attention(
                other_subject,
                self.scope,
                snapshot_id=renewed.snapshot_id,
                cursor=renewed.next_cursor,
                page_size=1,
            )

    def test_artifact_metadata_bytes_and_publish_preconditions_are_guarded_first(self):
        blob = _CountingBlobStore()
        catalog = _CountingCatalog()
        service = ArtifactService(blob, catalog, self.authority)
        write_capability = "ephi.fixture.write"
        principal = Principal("subject-1", (write_capability, self.capability), (self.scope,), 1, 10)
        self.authority.set_principal(principal)
        written = service.write_and_register(
            principal,
            self.scope,
            b"protected artifact",
            media_type="application/octet-stream",
            logical_purpose="fixture",
            required_write_capability=write_capability,
        )
        reference = written.metadata.reference
        blob.verify_calls = blob.read_calls = catalog.get_calls = 0

        revoked = Principal("subject-1", (), (self.scope,), 2, 11)
        self.authority.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            service.retrieve(principal, reference, self.capability)
        with self.assertRaises(AuthorizationDeniedError):
            service.verify_publish_preconditions(principal, (reference,), self.capability)
        self.assertEqual((catalog.get_calls, blob.verify_calls, blob.read_calls), (0, 0, 0))

        other = Principal("subject-2", (self.capability,), (self.other_scope,), 3, 12)
        self.authority.set_principal(other)
        with self.assertRaises(AuthorizationDeniedError):
            service.retrieve(other, reference, self.capability)
        self.assertEqual((catalog.get_calls, blob.verify_calls, blob.read_calls), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
