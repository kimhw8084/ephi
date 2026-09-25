"""CHG-252 Operations query, authorization and secret-safety regressions."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    AccessScope,
    ArtifactContentIdentity,
    ArtifactScopeVerification,
    GateState,
    MetrologySourceBinding,
    MutableCurrentAuthorizationAuthority,
    OperationsQueryService,
    Principal,
    ProviderIdentity,
    QualificationWorkspaceIdentity,
    SourceCapabilityRecord,
    SourceCapabilityState,
    SourceSnapshotRecord,
    SourceSnapshotStatus,
    ScopedArtifactReference,
)
from ephi.application.operations import PostgreSQLHealthFacts, WorkerHealthFacts  # noqa: E402
from ephi.application.worker import JobRecord  # noqa: E402


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
CAPABILITIES = (
    "ephi.operations.read",
    "ephi.source.read",
    "ephi.artifact.read",
    "ephi.family_qualification.read",
)


def _binding(scope: AccessScope) -> MetrologySourceBinding:
    return MetrologySourceBinding(
        scope, "fixture-source", "fixture-provider", "fixture-family", "fixture-capability",
        "fixture.adapter", "fixture.schema.v1", "mapping-v1", "a" * 64, "um",
    )


def _snapshot(binding: MetrologySourceBinding, *, status=SourceSnapshotStatus.PUBLISHED, available_at=None):
    cutoff = available_at or NOW - timedelta(minutes=2)
    return SourceSnapshotRecord(
        "fixture-snapshot", binding, "partition-1", "revision-1", cutoff - timedelta(minutes=2),
        cutoff, cutoff, ScopedArtifactReference(
            binding.scope,
            ArtifactContentIdentity("b" * 64, 10),
        ),
        1, status, "c" * 64, cutoff, cutoff, cutoff, freshness_age_seconds=3600,
    )


class _Postgres:
    def __init__(self):
        self.calls = 0

    def operations_health_facts(self):
        self.calls += 1
        return PostgreSQLHealthFacts("18.6", 19, 19, 0, 12, "d" * 64, "NOT_BOUND_NO_MIGRATION_LEDGER", NOW)


class _Workers:
    def __init__(self, jobs=(), *, effect_ids=()):
        self.jobs = tuple(jobs)
        self.effect_ids = set(effect_ids)
        self.calls = 0
        self.facts_calls = 0

    def __getattr__(self, _name):
        # The query service depends on inspect plus two additive inspection
        # methods; WorkerJobPort's other commands are deliberately unused.
        return lambda *_args, **_kwargs: None

    def inspect(self, scope, *, job_id=None, statuses=None, limit=100):
        self.calls += 1
        rows = [item for item in self.jobs if item.scope_key == scope.canonical_key]
        if job_id is not None:
            rows = [item for item in rows if item.job_id == job_id]
        if statuses is not None:
            rows = [item for item in rows if item.status in statuses]
        rows.sort(key=lambda item: (-item.priority, item.available_at, item.created_at, item.job_id))
        return tuple(rows[:limit])

    def operations_health_facts(self, scope):
        self.facts_calls += 1
        rows = [item for item in self.jobs if item.scope_key == scope.canonical_key]
        return WorkerHealthFacts(
            len(rows),
            sum(item.status == "FAILED" for item in rows),
            sum(item.status == "DEAD_LETTER" for item in rows),
            sum(item.status == "RUNNING" for item in rows),
            sum(item.status == "RUNNING" and (item.lease_expires_at is None or item.lease_expires_at <= NOW) for item in rows),
            NOW,
        )

    def has_committed_local_effect(self, scope, job_id):
        return job_id in self.effect_ids


class _Source:
    def __init__(self, binding, capability=None, snapshot=None):
        self.binding = binding
        self.capability = capability
        self.snapshot = snapshot
        self.capability_calls = 0
        self.snapshot_calls = 0

    def get_capability(self, principal, binding):
        self.capability_calls += 1
        assert principal.grants_scope(binding.scope)
        if self.capability is not None:
            return self.capability
        return SourceCapabilityRecord(
            binding, SourceCapabilityState.UNAVAILABLE, None, None, None, NOW, 3600,
            "CAPABILITY_RECORD_MISSING",
        )

    def get_snapshot(self, principal, scope, snapshot_id):
        self.snapshot_calls += 1
        assert principal.grants_scope(scope)
        return self.snapshot


class _Artifacts:
    def __init__(self, result=None):
        self.result = result or ArtifactScopeVerification(0, 0, 0, 0, False, ())
        self.calls = 0

    def inspect_scope(self, principal, scope, capability, *, limit):
        self.calls += 1
        assert capability == "ephi.artifact.read"
        assert limit <= 500
        return self.result


class _FamilyCenter:
    def __init__(self, view=None):
        self.view = view
        self.calls = 0

    def get_workspace(self, principal, scope, workspace_id, *, current_identity=None):
        self.calls += 1
        if self.view is None:
            from ephi.application import AggregateNotFoundError

            raise AggregateNotFoundError("private workspace identity")
        return self.view


def _identity(binding):
    stages = ("REPLAY", "GOLDEN", "SHADOW", "QUALIFY")
    return QualificationWorkspaceIdentity(
        binding.scope, binding.family_id, "1.0.0", "fixture-target", "fixture-context", "um",
        "fixture-characteristic", binding.capability_id, "fixture-product", "fixture-release",
        "org.ephi.downstream", "1.0.0",
        tuple(ProviderIdentity(category, f"fixture.{category}", "1.0.0") for category in ("artifacts", "identity", "notifications", "policy", "runtime", "source")),
        "fixture-policy", "1.0.0", "1.0.0", "e" * 64, binding, "test", 18, "1.0.0",
        tuple((stage, "f" * 64) for stage in stages), synthetic_fixture=True,
    )


def _job(scope, job_id, status="QUEUED", *, priority=0, expiry=None, message=None, code=None):
    return JobRecord(
        job_id, scope.canonical_key, "fixture.work", "semantic", "0" * 64,
        {"private_row": "SERIAL-992", "dsn": "postgresql://user:secret@host/db"},
        status, priority, NOW, 1, 3, "private-worker-owner" if status == "RUNNING" else None,
        4 if status == "RUNNING" else 0, expiry, code, message, NOW if message else None,
        NOW - timedelta(minutes=1), NOW,
    )


class OperationsApplicationTests(unittest.TestCase):
    def setUp(self):
        self.scope = AccessScope("fixture-scope", site_id="fixture-site", family_id="fixture-family")
        self.principal = Principal("fixture-operator", CAPABILITIES, (self.scope,), 3, 8)
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.binding = _binding(self.scope)
        self.postgres = _Postgres()
        self.workers = _Workers()
        self.source = _Source(self.binding)
        self.artifacts = _Artifacts()
        self.family = _FamilyCenter()
        self.service = self._service()

    def _service(self, **changes):
        arguments = {
            "current_authorization": self.authorization,
            "postgres": self.postgres,
            "worker_jobs": self.workers,
            "source_repository": self.source,
            "source_binding": self.binding,
            "artifact_service": self.artifacts,
            "family_center": self.family,
            "qualification_workspace_provider": lambda: (self.binding.family_id, _identity(self.binding)),
        }
        arguments.update(changes)
        return OperationsQueryService(**arguments)

    def test_o8_denial_precedes_every_protected_existence_or_count_read(self):
        self.authorization.set_principal(replace(self.principal, capabilities=()))
        with self.assertRaises(Exception):
            self.service.read(self.principal, self.scope)
        self.assertEqual(self.postgres.calls, 0)
        self.assertEqual(self.workers.calls, 0)
        self.assertEqual(self.workers.facts_calls, 0)
        self.assertEqual(self.source.capability_calls, 0)
        self.assertEqual(self.artifacts.calls, 0)
        self.assertEqual(self.family.calls, 0)

    def test_specific_o8_denial_precedes_source_artifact_and_evidence_reads(self):
        capabilities = ("ephi.operations.read",)
        self.authorization.set_principal(replace(self.principal, capabilities=capabilities))
        result = self.service.read(self.principal, self.scope).as_dict()
        axes = result["health"]["axes"]
        self.assertEqual(axes["source_capability_freshness"]["reason"], "SOURCE_AUTHORIZATION_REQUIRED")
        self.assertEqual(axes["immutable_artifact_integrity"]["reason"], "ARTIFACT_AUTHORIZATION_REQUIRED")
        self.assertEqual(axes["evidence_qualification_freshness"]["reason"], "QUALIFICATION_AUTHORIZATION_REQUIRED")
        self.assertEqual(self.source.capability_calls, 0)
        self.assertEqual(self.artifacts.calls, 0)
        self.assertEqual(self.family.calls, 0)

    def test_six_axes_remain_independent_and_have_no_global_health_field(self):
        cutoff = NOW - timedelta(hours=2)
        cap = SourceCapabilityRecord(
            self.binding, SourceCapabilityState.READY, "fixture-snapshot", cutoff, cutoff,
            NOW, 3600, "FRESH_PUBLISHED_SNAPSHOT", "partition-1", "revision-1",
        )
        self.source = _Source(self.binding, cap, _snapshot(self.binding, available_at=cutoff))
        self.service = self._service(source_repository=self.source)
        result = self.service.read(self.principal, self.scope).as_dict()
        axes = result["health"]["axes"]
        self.assertEqual(set(axes), {
            "process_transport", "postgres_readiness_durability", "immutable_artifact_integrity",
            "source_capability_freshness", "durable_worker_job_state", "evidence_qualification_freshness",
        })
        self.assertEqual(axes["process_transport"]["state"], "READY")
        self.assertEqual(axes["postgres_readiness_durability"]["state"], "READY")
        self.assertEqual(axes["source_capability_freshness"]["state"], "STALE")
        self.assertNotIn("healthy", json.dumps(result).lower())
        self.assertNotIn("overall", json.dumps(result).lower())

    def test_postgres_ready_and_failed_or_dead_letter_jobs_remain_separate(self):
        for status in ("FAILED", "DEAD_LETTER"):
            with self.subTest(status=status):
                self.workers = _Workers((_job(self.scope, f"job-{status.lower()}", status),))
                self.service = self._service(worker_jobs=self.workers)
                axes = self.service.read(self.principal, self.scope).health.as_dict()["axes"]
                self.assertEqual(axes["postgres_readiness_durability"]["state"], "READY")
                self.assertEqual(axes["durable_worker_job_state"]["state"], "ERROR")

    def test_source_ready_partial_stale_unavailable_and_snapshot_truth_are_preserved(self):
        cases = (
            (SourceCapabilityState.READY, SourceSnapshotStatus.PUBLISHED, NOW - timedelta(minutes=2), "READY"),
            (SourceCapabilityState.PARTIAL, SourceSnapshotStatus.PARTIAL, NOW - timedelta(minutes=2), "PARTIAL"),
            (SourceCapabilityState.STALE, SourceSnapshotStatus.PUBLISHED, NOW - timedelta(hours=2), "STALE"),
            (SourceCapabilityState.UNAVAILABLE, None, None, "UNAVAILABLE"),
        )
        for source_state, snapshot_state, cutoff, expected in cases:
            with self.subTest(source_state=source_state, snapshot_state=snapshot_state):
                snapshot_id = "fixture-snapshot" if snapshot_state else None
                capability = SourceCapabilityRecord(
                    self.binding, source_state, snapshot_id, cutoff, cutoff, NOW, 3600,
                    "FRESH_PUBLISHED_SNAPSHOT" if source_state is SourceCapabilityState.READY else source_state.value,
                    "partition-1" if snapshot_id else None, "revision-1" if snapshot_id else None,
                )
                snapshot = _snapshot(self.binding, status=snapshot_state, available_at=cutoff) if snapshot_state else None
                self.source = _Source(self.binding, capability, snapshot)
                self.service = self._service(source_repository=self.source)
                axis = self.service.read(self.principal, self.scope).health.as_dict()["axes"]["source_capability_freshness"]
                self.assertEqual(axis["state"], expected)
        mismatch = _snapshot(self.binding, available_at=NOW - timedelta(minutes=3))
        fresh_capability = SourceCapabilityRecord(
            self.binding, SourceCapabilityState.READY, "fixture-snapshot", NOW - timedelta(minutes=2),
            NOW - timedelta(minutes=2), NOW, 3600, "FRESH_PUBLISHED_SNAPSHOT", "partition-1", "revision-1",
        )
        self.source = _Source(self.binding, fresh_capability, mismatch)
        self.service = self._service(source_repository=self.source)
        self.assertEqual(
            self.service.read(self.principal, self.scope).health.as_dict()["axes"]["source_capability_freshness"]["state"],
            "ERROR",
        )
        unsafe_reason = replace(fresh_capability, state=SourceCapabilityState.STALE, reason="password=hunter2")
        self.source = _Source(self.binding, unsafe_reason, _snapshot(self.binding, available_at=NOW - timedelta(minutes=2)))
        self.service = self._service(source_repository=self.source)
        safe_source = self.service.read(self.principal, self.scope)
        self.assertEqual(safe_source.source.reason, "SOURCE_STATUS_STALE")
        self.assertNotIn("hunter2", json.dumps(safe_source.as_dict()))

    def test_artifact_failures_only_change_the_artifact_axis(self):
        baseline = self.service.read(self.principal, self.scope).health.as_dict()["axes"]
        for summary in (
            ArtifactScopeVerification(2, 1, 0, 0, False, (("MISSING_ARTIFACT_BYTES", 1),)),
            ArtifactScopeVerification(2, 0, 1, 0, False, (("CORRUPT_ARTIFACT_BYTES", 1),)),
        ):
            self.artifacts = _Artifacts(summary)
            self.service = self._service(artifact_service=self.artifacts)
            current = self.service.read(self.principal, self.scope).health.as_dict()["axes"]
            self.assertEqual(current["immutable_artifact_integrity"]["state"], "ERROR")
            for name in set(baseline) - {"immutable_artifact_integrity"}:
                self.assertEqual(current[name], baseline[name])

    def test_worker_list_is_bounded_deterministic_and_never_serializes_payload_or_raw_failure(self):
        jobs = tuple(
            _job(
                self.scope,
                f"job-{index:03d}",
                "FAILED" if index == 2 else "DEAD_LETTER" if index == 3 else "QUEUED",
                priority=index % 4,
                message="serial=SERIAL-992 password=hunter2 path /private/customer/source-row.csv" if index == 2 else None,
                code="PASSWORD=hunter2" if index == 2 else None,
            )
            for index in range(105)
        )
        jobs = (*jobs, replace(
            _job(self.scope, "password=hunter2", "QUEUED", priority=100),
            job_type="/private/customer/password",
        ))
        self.workers = _Workers(jobs, effect_ids={"job-004"})
        self.service = self._service(worker_jobs=self.workers)
        first = self.service.read(self.principal, self.scope).as_dict()
        restarted_store = _Workers(jobs, effect_ids={"job-004"})
        restarted = self._service(worker_jobs=restarted_store).read(self.principal, self.scope).as_dict()
        self.assertEqual(len(first["jobs"]), 100)
        self.assertTrue(first["jobs_truncated"])
        self.assertEqual(first["jobs"], restarted["jobs"])
        self.assertEqual(first["jobs"][0]["committed_local_effect_receipt"], False)
        receipt = next(item for item in first["jobs"] if item["job_id"] == "job-004")
        self.assertTrue(receipt["committed_local_effect_receipt"])
        encoded = json.dumps(first, sort_keys=True)
        for private in ("SERIAL-992", "hunter2", "postgresql://", "/private/customer", "private-worker-owner", "private_row", '"payload"'):
            self.assertNotIn(private, encoded)
        failed = next(item for item in first["jobs"] if item["job_id"] == "job-002")
        self.assertEqual(failed["failure_code"], "UNCLASSIFIED_FAILURE")
        self.assertEqual(failed["failure_reason"], "Failure detail withheld; review the owning worker authority.")
        redacted_identity = next(item for item in first["jobs"] if item["job_type"] == "UNCLASSIFIED_JOB_TYPE")
        self.assertRegex(redacted_identity["job_id"], r"^job-[0-9a-f]{12}$")
        self.assertNotIn("hunter2", encoded)

    def test_lease_expiry_classification_uses_worker_database_time_fact(self):
        self.workers = _Workers((
            _job(self.scope, "expired", "RUNNING", expiry=NOW - timedelta(seconds=1)),
            _job(self.scope, "active", "RUNNING", expiry=NOW + timedelta(seconds=20)),
            _job(self.scope, "stale", "RUNNING", expiry=None),
        ))
        self.service = self._service(worker_jobs=self.workers)
        result = self.service.read(self.principal, self.scope)
        self.assertEqual(result.health.as_dict()["axes"]["durable_worker_job_state"]["state"], "STALE")
        states = {item.job_id: item.lease_state for item in result.jobs}
        self.assertEqual(states, {"expired": "EXPIRED", "active": "ACTIVE", "stale": "STALE"})

    def test_qualification_pending_expired_current_and_unbound_states_are_truthful(self):
        identity = _identity(self.binding)
        pending = SimpleNamespace(
            stage_id="REPLAY", state=GateState.PENDING, invalidation_reason="PENDING_REPLAY",
            expires_at=NOW + timedelta(days=1),
        )
        view = SimpleNamespace(
            gates=(pending,), promotions=(), promotion_ready=False, family_id=self.binding.family_id,
            capability_id=self.binding.capability_id, release_id=identity.release_id, synthetic=True,
        )
        self.family = _FamilyCenter(view)
        self.service = self._service(family_center=self.family, qualification_workspace_provider=lambda: (self.binding.family_id, identity))
        result = self.service.read(self.principal, self.scope)
        self.assertEqual(result.qualification.state, "PENDING")
        self.assertEqual(result.health.as_dict()["axes"]["evidence_qualification_freshness"]["state"], "PENDING")
        self.assertIsNotNone(result.qualification.gates[0].expires_at)
        self.assertEqual(result.qualification.gates[0].reason, "PENDING_REPLAY")

        current_view = SimpleNamespace(
            gates=(SimpleNamespace(stage_id="REPLAY", state=GateState.PASS, invalidation_reason=None, expires_at=NOW + timedelta(days=1)),),
            promotions=(SimpleNamespace(state="CURRENT"),), promotion_ready=True,
            family_id=self.binding.family_id, capability_id=self.binding.capability_id,
            release_id=identity.release_id, synthetic=True,
        )
        self.family = _FamilyCenter(current_view)
        self.service = self._service(family_center=self.family, qualification_workspace_provider=lambda: (self.binding.family_id, identity))
        current = self.service.read(self.principal, self.scope)
        self.assertEqual(current.qualification.state, "CURRENT")
        self.assertEqual(current.health.as_dict()["axes"]["evidence_qualification_freshness"]["state"], "CURRENT")

        expired_view = SimpleNamespace(
            gates=(SimpleNamespace(stage_id="REPLAY", state=GateState.EXPIRED, invalidation_reason="EVIDENCE_EXPIRED", expires_at=NOW - timedelta(seconds=1)),),
            promotions=(SimpleNamespace(state="CURRENT"),), promotion_ready=False,
            family_id=self.binding.family_id, capability_id=self.binding.capability_id,
            release_id=identity.release_id, synthetic=True,
        )
        self.family = _FamilyCenter(expired_view)
        self.service = self._service(family_center=self.family, qualification_workspace_provider=lambda: (self.binding.family_id, identity))
        self.assertEqual(self.service.read(self.principal, self.scope).qualification.state, "EXPIRED")

        unbound = self._service(family_center=None, qualification_workspace_provider=None).read(self.principal, self.scope)
        self.assertEqual(unbound.qualification.state, "NOT_QUALIFIED")
        self.assertEqual(unbound.qualification.reason, "QUALIFICATION_AUTHORITY_NOT_BOUND")

    def test_unstarted_workspace_and_backup_rpo_rto_are_explicitly_unqualified(self):
        identity = _identity(self.binding)
        self.family = _FamilyCenter()
        self.service = self._service(family_center=self.family, qualification_workspace_provider=lambda: (self.binding.family_id, identity))
        result = self.service.read(self.principal, self.scope)
        self.assertEqual(result.qualification.state, "NOT_QUALIFIED")
        self.assertEqual(result.qualification.reason, "QUALIFICATION_WORKSPACE_NOT_STARTED")
        self.assertEqual(result.backup_restore.production_rpo_rto, "NOT_ESTABLISHED")
        self.assertEqual(result.backup_restore.local_o91_rehearsal, "AVAILABLE_IN_CLI_CONTRACT")
        self.assertEqual(result.backup_restore.target_backup_evidence, "NOT_BOUND")

    def test_browser_page_cannot_invoke_cli_restore_or_worker_mutations(self):
        page = (ROOT / "src/ephi/ui/operations.py").read_text(encoding="utf-8")
        self.assertNotIn("tools.o9_operations", page)
        self.assertNotIn("subprocess", page)
        self.assertNotIn("pg_dump", page)
        self.assertNotIn("pg_restore", page)
        self.assertNotIn("EPHI_POSTGRES_DSN", page)
        for mutation in (".enqueue(", ".claim(", ".heartbeat(", ".complete(", ".fail(", ".defer(", ".cancel(", ".commit_local_effect("):
            self.assertNotIn(mutation, page)


if __name__ == "__main__":
    unittest.main()
