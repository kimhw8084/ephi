"""CHG-174 O6.2 bounded comparable-case retrieval regressions."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    COMPARABLE_HISTORY_READ_CAPABILITY,
    COMPARABLE_PROFILE_KEY,
    COMPARABLE_PROFILE_SCHEMA,
    ComparableCaseHistoryQueryService,
    ComparableCaseQuery,
    ComparableCaseRevisionRecord,
    ComparableHistoryMaterializationRequired,
    ComparableQueryState,
    ComparableRetrievalPolicy,
    CurationState,
    EligibilityState,
    ExactStructuredFingerprint,
    FingerprintFeature,
    HistoricalClaimType,
    HistoricalSourceFacts,
    HistoricalSourceIdentity,
    Principal,
    ReadRevision,
    ReadRevisionIdentity,
    RevisionVector,
    AccessScope,
    CurrentReadBundle,
    CurrentReadHead,
    HistoricalReadBundle,
    PageResult,
    ReadSnapshotStore,
    RetainedQuerySnapshot,
    RetainedSnapshotRow,
    VersionedReadRow,
    canonical_query_identity,
    CursorPageToken,
    MutableCurrentAuthorizationAuthority,
    QueryIdentityMismatchError,
    AuthorizationDeniedError,
    CoherentReadConflictError,
)
from ephi.application.storage import AggregateSnapshot  # noqa: E402


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _ReadStore(ReadSnapshotStore):
    def __init__(self):
        self.current = {}
        self.snapshots = {}
        self.snapshot_counter = 0

    def add_revision(self, scope, episode_id, revision_id, profile, *, known_at, workflow_version=3, active_cycle="cycle-1", current=False):
        workflow = AggregateSnapshot(
            scope.canonical_key,
            "episode_workflow",
            episode_id,
            workflow_version,
            {"decision_loop": {"active_cycle_id": active_cycle}},
        )
        vector = RevisionVector(f"analysis-{revision_id}", None, None, workflow_version, None, f"qualification-{revision_id}")
        revision = ReadRevision(
            ReadRevisionIdentity(revision_id, scope, "episode", episode_id),
            vector,
            {"episode_id": episode_id, COMPARABLE_PROFILE_KEY: profile},
            known_at,
            known_at,
            workflow,
        )
        if current:
            self.current[(scope.canonical_key, episode_id)] = revision
        return revision

    def publish_read_revision(self, revision, *, expected_head_version=None, expected_revision_id=None):
        raise NotImplementedError

    def read_current_bundle(self, principal, scope, entity_type, entity_id, required_read_capability):
        revision = self.current.get((scope.canonical_key, entity_id))
        if revision is None or entity_type != "episode":
            raise CoherentReadConflictError("current Episode unavailable")
        return CurrentReadBundle(revision, revision.workflow_aggregate, revision.revision_vector)

    def read_historical_bundle(self, principal, scope, revision_id, required_read_capability):
        revision = next(item for item in self.current.values() if item.revision_id == revision_id)
        return HistoricalReadBundle(revision, revision.workflow_aggregate, revision.revision_vector)

    def create_query_snapshot(self, principal, scope, query_identity, required_read_capability, rows, *, ttl_seconds=300):
        self.snapshot_counter += 1
        _normalized, query_hash = canonical_query_identity(query_identity)
        snapshot_id = f"snapshot-{self.snapshot_counter}"
        created_at = datetime.now(timezone.utc)
        snapshot = RetainedQuerySnapshot(
            snapshot_id,
            query_hash,
            scope,
            principal.subject,
            principal.security_revision,
            required_read_capability,
            created_at,
            created_at + timedelta(seconds=ttl_seconds),
            len(rows),
        )
        self.snapshots[snapshot_id] = (snapshot, tuple(rows))
        return snapshot

    def read_query_snapshot_page(self, principal, scope, snapshot_id, query_identity, required_read_capability, *, page_size=50, cursor=None):
        snapshot, rows = self.snapshots[snapshot_id]
        _normalized, query_hash = canonical_query_identity(query_identity)
        if query_hash != snapshot.query_identity_hash:
            raise QueryIdentityMismatchError("query identity mismatch")
        if scope != snapshot.scope or principal.subject != snapshot.subject or not principal.grants_scope(scope):
            raise AuthorizationDeniedError("snapshot is not authorized")
        if not principal.has_capability(required_read_capability) or principal.security_revision != snapshot.security_revision:
            raise AuthorizationDeniedError("snapshot authorization changed")
        binding = _sha("test retained snapshot binding")
        if cursor is None:
            start = 0
        else:
            token = CursorPageToken.decode(cursor)
            token.verify(snapshot, server_binding=binding)
            start = token.next_ordinal - 1
        selected = rows[start : start + page_size]
        retained = tuple(
            RetainedSnapshotRow(snapshot_id, start + offset + 1, row.row_id, row.row_version, row.payload)
            for offset, row in enumerate(selected)
        )
        next_cursor = None
        if start + len(selected) < len(rows):
            next_cursor = CursorPageToken.create(snapshot, start + len(selected) + 1, server_binding=binding).encode()
        return PageResult(snapshot, retained, next_cursor)


class _HistorySource:
    def __init__(self, *, inject_cross_scope=()):
        self.records = []
        self.sources = {}
        self.inject_cross_scope = list(inject_cross_scope)
        self.requested_limits = []
        self.require_materialization = False

    def add(self, revision, source_facts):
        self.records.append(ComparableCaseRevisionRecord(revision, source_facts))
        if source_facts is not None:
            self.sources[(revision.scope.canonical_key, source_facts.identity.snapshot_id)] = source_facts

    def fetch_episode_history_window(self, principal, scope, current_episode_id, known_by, *, limit, required_read_capability):
        self.requested_limits.append(limit)
        if self.require_materialization:
            raise ComparableHistoryMaterializationRequired
        eligible = [
            record for record in self.records
            if record.revision.scope.canonical_key == scope.canonical_key
            and record.revision.entity_id != current_episode_id
            and record.revision.entity_type == "episode"
            and record.revision.known_at <= known_by
            and record.revision.published_at <= known_by
            and (record.source_facts is None or (
                record.source_facts.available_at <= known_by and record.source_facts.known_at <= known_by
            ))
        ]
        eligible.sort(key=lambda item: item.revision.known_at, reverse=True)
        return tuple(eligible[:limit] + self.inject_cross_scope)

    def fetch_comparable_source_facts(self, principal, scope, identity, required_read_capability):
        return self.sources.get((scope.canonical_key, identity.snapshot_id))


def _profile(
    *,
    source_id: str,
    features: dict[str, str],
    family="family-A",
    context="context-A",
    eligibility="QUALIFIED",
    curation="CURATED",
    claims=(),
    limitations=(),
):
    return {
        "schema": COMPARABLE_PROFILE_SCHEMA,
        "family_identity": family,
        "context_identity": context,
        "source_identity": {
            "snapshot_id": source_id,
            "source_revision": f"source-revision-{source_id}",
            "manifest_hash": _sha(f"manifest-{source_id}"),
            "artifact_sha256": _sha(f"artifact-{source_id}"),
        },
        "fingerprint": {
            "version": "exact-structured.v1",
            "features": [
                {"feature_id": feature_id, "value_sha256": _sha(value)}
                for feature_id, value in features.items()
            ],
        },
        "eligibility_state": eligibility,
        "eligibility_identity": f"eligibility-{source_id}",
        "qualification_evidence_identity": f"qualification-evidence-{source_id}" if eligibility == "QUALIFIED" else None,
        "curation_state": curation,
        "curation_evidence_identity": f"curation-evidence-{source_id}" if curation == "CURATED" else None,
        "data_completeness_limitations": list(limitations),
        "claims": list(claims),
    }


class ComparableHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scope = AccessScope("o6.2-scope", site_id="site-A", family_id="family-A")
        self.other_scope = AccessScope("other-scope", site_id="site-B", family_id="family-A")
        self.principal = Principal(
            "history-engineer",
            (COMPARABLE_HISTORY_READ_CAPABILITY,),
            (self.scope,),
            1,
            1,
        )
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.read_store = _ReadStore()
        self.history = _HistorySource()
        self.service = ComparableCaseHistoryQueryService(self.history, self.read_store, self.authorization)
        self.now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        self.current_profile = _profile(
            source_id="source-current",
            features={"regime": "recipe-a", "geometry": "shape-x", "material_class": "alloy-z"},
        )
        self.current_revision = self.read_store.add_revision(
            self.scope,
            "episode-current",
            "current-revision-1",
            self.current_profile,
            known_at=self.now - timedelta(hours=2),
            workflow_version=3,
            current=True,
        )
        self.current_source = self._source(self.current_profile, available_at=self.now - timedelta(hours=3))
        self.history.sources[(self.scope.canonical_key, self.current_source.identity.snapshot_id)] = self.current_source
        self.query = self._query()

    def _source(self, profile, *, available_at=None, known_at=None, status="PUBLISHED"):
        raw = profile["source_identity"]
        return HistoricalSourceFacts(
            HistoricalSourceIdentity(raw["snapshot_id"], raw["source_revision"], raw["manifest_hash"], raw["artifact_sha256"]),
            profile["family_identity"],
            available_at or self.now - timedelta(days=1),
            known_at or self.now - timedelta(days=1) + timedelta(minutes=1),
            status,
        )

    def _candidate(self, episode_id, revision_id, *, profile=None, known_at=None, scope=None, status="PUBLISHED", source_available_at=None, source_family=None):
        scope = scope or self.scope
        profile = profile or _profile(
            source_id=f"source-{episode_id}",
            features={"regime": "recipe-a", "geometry": "shape-x", "tool_family": "tool-7"},
        )
        revision = self.read_store.add_revision(
            scope,
            episode_id,
            revision_id,
            profile,
            known_at=known_at or self.now - timedelta(days=1),
            workflow_version=1,
        )
        source = self._source(profile, available_at=source_available_at, status=status) if scope == self.scope else None
        if source is not None and source_family is not None:
            source = replace(source, family_identity=source_family)
        self.history.add(revision, source)
        return revision

    def _query(self, *, cutoff=None, limit=100, policy=None):
        fingerprint = ExactStructuredFingerprint(
            "exact-structured.v1",
            tuple(FingerprintFeature(feature_id, _sha(value)) for feature_id, value in {
                "regime": "recipe-a",
                "geometry": "shape-x",
                "material_class": "alloy-z",
            }.items()),
        )
        return ComparableCaseQuery(
            self.scope,
            "episode-current",
            "cycle-1",
            3,
            "current-revision-1",
            cutoff or self.now,
            self.current_source.identity,
            fingerprint.identity,
            "family-A",
            "context-A",
            limit,
            policy or ComparableRetrievalPolicy(),
        )

    def test_exact_comparable_results_are_deterministic_and_ties_use_stable_identity(self):
        self._candidate("episode-b", "revision-b")
        self._candidate("episode-a", "revision-a", known_at=self.now - timedelta(days=2))
        first = self.service.retrieve(self.principal, self.query)
        second = self.service.retrieve(self.principal, self.query)
        self.assertEqual(first.result_identity, second.result_identity)
        self.assertEqual([item.episode_id for item in first.cases], ["episode-a", "episode-b"])
        self.assertEqual([item.revision_id for item in first.cases], ["revision-a", "revision-b"])
        self.assertEqual(first.cases[0].similarity_components.shared_exact_feature_ids, ("geometry", "regime"))
        self.assertEqual(first.cases[0].similarity_components.candidate_only_feature_ids, ("tool_family",))
        self.assertEqual(first.cases[0].similarity_components.current_only_feature_ids, ("material_class",))
        self.assertEqual(first.cases[0].similarity_components.differing_feature_ids, ())
        self.assertTrue(first.cases[0].tie_break_identity)
        recency = self.service.retrieve(
            self.principal,
            replace(self.query, policy=ComparableRetrievalPolicy(version="2")),
        )
        self.assertEqual([item.episode_id for item in recency.cases], ["episode-b", "episode-a"])

    def test_current_episode_is_never_returned_and_cross_scope_rows_do_not_affect_bound(self):
        self._candidate("episode-a", "revision-a")
        cross_scope_profile = _profile(source_id="foreign", features={"regime": "recipe-a"})
        cross_scope_revision = self.read_store.add_revision(
            self.other_scope,
            "foreign-episode",
            "foreign-revision",
            cross_scope_profile,
            known_at=self.now - timedelta(days=2),
            workflow_version=1,
        )
        self.history.inject_cross_scope.append(ComparableCaseRevisionRecord(cross_scope_revision, None))
        query = self._query(limit=1)
        page = self.service.retrieve(self.principal, query)
        self.assertEqual(page.state, ComparableQueryState.READY)
        self.assertEqual([item.episode_id for item in page.cases], ["episode-a"])
        disclosed = str([item.as_dict() for item in page.excluded_candidates])
        self.assertNotIn("foreign-episode", disclosed)
        self.assertNotIn("episode-current", [item.episode_id for item in page.cases])

    def test_temporal_family_context_and_unqualified_candidates_are_filtered_before_ranking(self):
        self._candidate("future-source", "future-revision", source_available_at=self.now + timedelta(hours=1))
        self._candidate("wrong-family", "wrong-family-r", profile=_profile(source_id="wrong-family-source", features={"regime": "recipe-a"}, family="family-B"))
        self._candidate("wrong-context", "wrong-context-r", profile=_profile(source_id="wrong-context-source", features={"regime": "recipe-a"}, context="context-B"))
        self._candidate("wrong-source-family", "wrong-source-family-r", source_family="family-B")
        self._candidate("unqualified", "unqualified-r", profile=_profile(source_id="unqualified-source", features={"regime": "recipe-a"}, eligibility="UNQUALIFIED"))
        page = self.service.retrieve(self.principal, self.query)
        self.assertEqual(page.cases, ())
        exclusions = {item.episode_id: item.reason_codes for item in page.excluded_candidates}
        self.assertNotIn("future-source", exclusions)
        self.assertEqual(exclusions["wrong-family"], ("FAMILY_MISMATCH", "SOURCE_FAMILY_MISMATCH"))
        self.assertEqual(exclusions["wrong-context"], ("CONTEXT_MISMATCH",))
        self.assertEqual(exclusions["wrong-source-family"], ("SOURCE_FAMILY_MISMATCH",))
        self.assertEqual(exclusions["unqualified"], ("UNQUALIFIED_FOR_COMPARISON",))
        repeated = self.service.retrieve(self.principal, self.query)
        self.assertEqual(page.result_identity, repeated.result_identity)
        self.assertEqual(page.excluded_candidates, repeated.excluded_candidates)

    def test_missing_historical_source_is_explicit_and_excluded(self):
        profile = _profile(source_id="source-missing", features={"regime": "recipe-a"})
        revision = self.read_store.add_revision(
            self.scope,
            "missing-source-case",
            "missing-source-revision",
            profile,
            known_at=self.now - timedelta(days=1),
            workflow_version=1,
        )
        self.history.add(revision, None)
        page = self.service.retrieve(self.principal, self.query)
        self.assertEqual(page.cases, ())
        self.assertEqual(page.excluded_candidates[0].reason_codes, ("MISSING_IMMUTABLE_SOURCE_RECORD",))

    def test_missing_qualification_identity_is_explicit_and_excluded(self):
        profile = _profile(source_id="missing-qualification", features={"regime": "recipe-a"})
        profile["eligibility_identity"] = None
        self._candidate("missing-qualification-case", "missing-qualification-revision", profile=profile)
        page = self.service.retrieve(self.principal, self.query)
        self.assertEqual(page.cases, ())
        self.assertEqual(page.excluded_candidates[0].reason_codes, ("MISSING_QUALIFICATION_IDENTITY",))

    def test_limited_comparison_is_visible_and_raw_feature_values_are_never_disclosed(self):
        profile = _profile(
            source_id="limited-source",
            features={"regime": "secret-recipe-string", "material_class": "private-material-name"},
            eligibility="LIMITED",
            curation="UNCURATED",
            limitations=("SPARSE_HISTORY",),
        )
        self._candidate("limited-case", "limited-revision", profile=profile, status="PARTIAL")
        page = self.service.retrieve(self.principal, self.query)
        case = page.cases[0]
        self.assertEqual(case.eligibility_state, EligibilityState.LIMITED)
        self.assertEqual(case.curation_state, CurationState.UNCURATED)
        self.assertEqual(
            case.data_completeness_limitations,
            ("COMPARISON_QUALIFICATION_LIMITED", "HISTORICAL_CURATION_LIMITED", "SOURCE_MANIFEST_PARTIAL", "SPARSE_HISTORY"),
        )
        output = str(case.as_dict())
        self.assertNotIn("secret-recipe-string", output)
        self.assertNotIn("private-material-name", output)
        self.assertNotIn("value_sha256", output)
        self.assertNotIn("similarity_score", output)

    def test_material_differences_missing_fields_and_claim_provenance_are_explicit(self):
        before = self.now - timedelta(days=1)
        future = self.now + timedelta(hours=1)
        claims = [
            {
                "claim_type": "ROOT_CAUSE",
                "claim_identity": "claim.root-cause-17",
                "evidence_identity": "evidence.root-17",
                "curation_identity": "curation.root-17",
                "curation_state": "CURATED",
                "known_at": before,
                "available_at": before,
            },
            {
                "claim_type": "ACTION_SUCCESS",
                "claim_identity": "claim.action-11",
                "evidence_identity": "evidence.action-11",
                "curation_identity": "curation.action-11",
                "curation_state": "CURATED",
                "known_at": before,
                "available_at": before,
            },
            {
                "claim_type": "ROOT_CAUSE",
                "claim_identity": "claim.uncurated",
                "evidence_identity": "evidence.uncurated",
                "curation_identity": "curation.pending",
                "curation_state": "LIMITED",
                "known_at": before,
                "available_at": before,
            },
            {
                "claim_type": "OUTCOME",
                "claim_identity": "claim.future-outcome",
                "evidence_identity": "evidence.future-outcome",
                "curation_identity": "curation.future-outcome",
                "curation_state": "CURATED",
                "known_at": future,
                "available_at": future,
                "outcome_maturity": "MATURE",
                "outcome_cutoff": future,
            },
            {
                "claim_type": "ROOT_CAUSE",
                "claim_identity": "claim.missing-evidence",
                "curation_identity": "curation.missing-evidence",
                "curation_state": "CURATED",
                "known_at": before,
                "available_at": before,
            },
            {
                "claim_type": "ACTION_SUCCESS",
                "claim_identity": "claim.missing-curation",
                "evidence_identity": "evidence.missing-curation",
                "curation_state": "CURATED",
                "known_at": before,
                "available_at": before,
            },
            {
                "claim_type": "OUTCOME",
                "claim_identity": "claim.missing-outcome-evidence",
                "curation_identity": "curation.missing-outcome-evidence",
                "curation_state": "CURATED",
                "known_at": before,
                "available_at": before,
                "outcome_maturity": "MATURE",
                "outcome_cutoff": before,
            },
        ]
        profile = _profile(
            source_id="claims-source",
            features={"regime": "recipe-different", "sensor_class": "class-new"},
            claims=claims,
        )
        self._candidate("claims-case", "claims-revision", profile=profile)
        case = self.service.retrieve(self.principal, self.query).cases[0]
        components = case.similarity_components
        self.assertEqual(components.differing_feature_ids, ("regime",))
        self.assertEqual(components.current_only_feature_ids, ("geometry", "material_class"))
        self.assertEqual(components.candidate_only_feature_ids, ("sensor_class",))
        self.assertEqual(
            {claim.claim_type for claim in case.historical_claims},
            {HistoricalClaimType.ROOT_CAUSE, HistoricalClaimType.ACTION_SUCCESS},
        )
        self.assertTrue(all(claim.evidence_identity and claim.curation_identity for claim in case.historical_claims))
        self.assertNotIn("future-outcome", str(case.as_dict()))
        self.assertNotIn("missing-evidence", str(case.as_dict()))
        self.assertNotIn("missing-curation", str(case.as_dict()))
        self.assertNotIn("missing-outcome-evidence", str(case.as_dict()))
        self.assertIn("HISTORICAL_CLAIM_OMITTED", case.data_completeness_limitations)
        self.assertIn("HISTORICAL_CLAIM_CURATION_LIMITED", case.data_completeness_limitations)

    def test_cutoff_candidate_source_fingerprint_and_policy_identity_changes_change_result(self):
        self._candidate("case-a", "revision-a")
        baseline = self.service.retrieve(self.principal, self.query)
        later_cutoff = self.service.retrieve(self.principal, replace(self.query, knowledge_cutoff=self.now + timedelta(seconds=1)))
        self.assertNotEqual(baseline.result_identity, later_cutoff.result_identity)

        recency_policy = ComparableRetrievalPolicy(version="2")
        different_policy = self.service.retrieve(self.principal, replace(self.query, policy=recency_policy))
        self.assertNotEqual(baseline.result_identity, different_policy.result_identity)
        self.assertNotEqual(baseline.query_identity, different_policy.query_identity)

        changed_source_profile = _profile(
            source_id="source-case-a-revised",
            features={"regime": "recipe-a", "geometry": "shape-revised"},
        )
        self._candidate(
            "case-a",
            "revision-a2",
            profile=changed_source_profile,
            known_at=self.now - timedelta(hours=1),
        )
        changed_revision = self.service.retrieve(self.principal, self.query)
        self.assertNotEqual(baseline.result_identity, changed_revision.result_identity)
        self.assertEqual(changed_revision.cases[0].revision_id, "revision-a2")

        changed_profile = _profile(
            source_id="source-changed",
            features={"regime": "recipe-a", "geometry": "shape-other"},
        )
        self._candidate("case-b", "revision-b", profile=changed_profile)
        changed_population = self.service.retrieve(self.principal, self.query)
        self.assertNotEqual(baseline.result_identity, changed_population.result_identity)

    def test_current_fingerprint_and_source_revision_are_bound_into_result_identity(self):
        self._candidate("case-a", "revision-a")
        baseline = self.service.retrieve(self.principal, self.query)
        new_source_profile = _profile(
            source_id="source-current-revised",
            features={"regime": "recipe-a", "geometry": "shape-x", "material_class": "alloy-z"},
        )
        new_current_source = self._source(new_source_profile, available_at=self.now - timedelta(hours=3))
        self.history.sources[(self.scope.canonical_key, new_current_source.identity.snapshot_id)] = new_current_source
        new_revision = self.read_store.add_revision(
            self.scope,
            "episode-current",
            "current-revision-2",
            new_source_profile,
            known_at=self.now - timedelta(hours=1),
            workflow_version=3,
            current=True,
        )
        self.assertEqual(new_revision.revision_id, "current-revision-2")
        fingerprint = ExactStructuredFingerprint(
            "exact-structured.v1",
            tuple(
                FingerprintFeature(feature_id, _sha(value))
                for feature_id, value in {
                    "regime": "recipe-a",
                    "geometry": "shape-x",
                    "material_class": "alloy-z",
                }.items()
            ),
        )
        new_query = replace(
            self.query,
            revision_id="current-revision-2",
            current_source_identity=new_current_source.identity,
            current_fingerprint_identity=fingerprint.identity,
        )
        changed = self.service.retrieve(self.principal, new_query)
        self.assertNotEqual(baseline.result_identity, changed.result_identity)

    def test_retained_cursor_binds_full_query_and_rechecks_current_authorization(self):
        self._candidate("case-a", "revision-a")
        self._candidate("case-b", "revision-b", known_at=self.now - timedelta(days=2))
        first = self.service.retrieve(self.principal, self.query, page_size=1)
        self.assertIsNotNone(first.next_cursor)
        second = self.service.retrieve(
            self.principal,
            self.query,
            page_size=1,
            snapshot_id=first.snapshot_id,
            cursor=first.next_cursor,
        )
        self.assertEqual(second.result_identity, first.result_identity)
        self.assertEqual(second.cases[0].episode_id, "case-b")
        self.assertEqual(len(second.excluded_candidates), len(first.excluded_candidates))

        with self.assertRaises(QueryIdentityMismatchError):
            self.service.retrieve(
                self.principal,
                replace(self.query, knowledge_cutoff=self.now + timedelta(seconds=1)),
                snapshot_id=first.snapshot_id,
                cursor=first.next_cursor,
            )
        revoked = Principal("history-engineer", (), (self.scope,), 2, 2)
        self.authorization.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.service.retrieve(self.principal, self.query, snapshot_id=first.snapshot_id, cursor=first.next_cursor)

    def test_population_overflow_returns_explicit_materialization_state_without_partial_truth(self):
        self._candidate("case-a", "revision-a")
        self._candidate("case-b", "revision-b")
        page = self.service.retrieve(self.principal, self._query(limit=1))
        self.assertEqual(page.state, ComparableQueryState.MATERIALIZATION_REQUIRED)
        self.assertEqual(page.materialization_reason, "CANDIDATE_LIMIT_EXCEEDED")
        self.assertIsNone(page.result_identity)
        self.assertEqual(page.cases, ())
        self.assertEqual(page.excluded_candidates, ())
        self.assertIsNone(page.snapshot_id)
        self.assertEqual(self.history.requested_limits[-1], 2)

    def test_durable_query_time_bound_returns_materialization_required(self):
        self.history.require_materialization = True
        page = self.service.retrieve(self.principal, self.query)
        self.assertEqual(page.state, ComparableQueryState.MATERIALIZATION_REQUIRED)
        self.assertEqual(page.materialization_reason, "QUERY_TIME_BUDGET_EXCEEDED")
        self.assertEqual(page.cases, ())

    def test_current_revision_cycle_scope_and_authorization_are_mandatory(self):
        wrong_cycle = replace(self.query, cycle_id="cycle-old")
        with self.assertRaises(CoherentReadConflictError):
            self.service.retrieve(self.principal, wrong_cycle)
        wrong_revision = replace(self.query, revision_id="not-current")
        with self.assertRaises(CoherentReadConflictError):
            self.service.retrieve(self.principal, wrong_revision)
        other_scope_query = replace(self.query, scope=self.other_scope)
        with self.assertRaises(AuthorizationDeniedError):
            self.service.retrieve(self.principal, other_scope_query)

        read_count = len(self.history.requested_limits)
        revoked = Principal("history-engineer", (), (self.scope,), 2, 2)
        self.authorization.set_principal(revoked)
        with self.assertRaises(AuthorizationDeniedError):
            self.service.retrieve(self.principal, self.query)
        self.assertEqual(len(self.history.requested_limits), read_count)

    def test_future_available_candidate_does_not_change_as_of_result_or_population_identity(self):
        self._candidate("case-a", "revision-a")
        baseline = self.service.retrieve(self.principal, self.query)
        self._candidate(
            "future-case",
            "future-revision",
            source_available_at=self.now + timedelta(days=2),
        )
        as_of = self.service.retrieve(self.principal, self.query)
        self.assertEqual(as_of.result_identity, baseline.result_identity)
        self.assertEqual([item.episode_id for item in as_of.cases], ["case-a"])
        self.assertNotIn("future-case", str([item.as_dict() for item in as_of.excluded_candidates]))


if __name__ == "__main__":
    unittest.main()
