"""Real PostgreSQL 18 O6 planner reads over the durable O5 Episode aggregate."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import (  # noqa: E402
    CHECK_EXECUTE_CAPABILITY,
    CHECK_REQUEST_CAPABILITY,
    DECISION_LOOP_CREATE_CAPABILITY,
    DECISION_LOOP_READ_CAPABILITY,
    EPISODE_WORKFLOW_AGGREGATE_TYPE,
    AccessScope,
    CapabilityFact,
    CapabilityRequirement,
    CapabilityState,
    CheckExecutionMode,
    CheckTemplate,
    CheckTemplateCatalog,
    CommandContext,
    DecisionDeadlineFact,
    DecisionLoopCommandService,
    DeadlineState,
    DisruptionClass,
    EffortBand,
    EvidenceDependenceGroup,
    MutableCurrentAuthorizationAuthority,
    NextCheckPlannerService,
    PairDiscrimination,
    PairWeight,
    PlannerPolicy,
    PlannerReadFacts,
    Principal,
    QualificationFact,
    QualificationState,
    RevisionVector,
    TargetContext,
    TurnaroundFact,
    TurnaroundState,
    UnresolvedHypothesisPair,
)
from ephi.infrastructure import PostgreSQLReferenceTransactionAdapter  # noqa: E402


DSN = os.environ.get("EPHI_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(DSN, "EPHI_TEST_POSTGRES_DSN is not set; PostgreSQL integration is NOT_RUN")
class O6PlannerPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.adapter = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(self.adapter.close)
        self.scope = AccessScope("o6-pg-planner-scope", site_id="site-test", family_id="family-test")
        self.principal = Principal(
            "o6-pg-planner-engineer",
            (
                CHECK_EXECUTE_CAPABILITY,
                CHECK_REQUEST_CAPABILITY,
                DECISION_LOOP_CREATE_CAPABILITY,
                DECISION_LOOP_READ_CAPABILITY,
                "ephi.check.approve",
            ),
            (self.scope,),
            1,
            1,
        )
        self.authorization = MutableCurrentAuthorizationAuthority(self.principal)
        self.adapter.connection.execute(
            "DELETE FROM outbox_event WHERE scope_key = %s", (self.scope.canonical_key,)
        )
        self.adapter.connection.execute(
            "DELETE FROM audit_event WHERE scope_key = %s", (self.scope.canonical_key,)
        )
        self.adapter.connection.execute(
            "DELETE FROM command_receipt WHERE scope_key = %s", (self.scope.canonical_key,)
        )
        self.adapter.connection.execute(
            "DELETE FROM aggregate_state WHERE scope_key = %s", (self.scope.canonical_key,)
        )
        self.adapter.seed_aggregate(
            self.scope,
            EPISODE_WORKFLOW_AGGREGATE_TYPE,
            "episode-o6-pg",
            {"owner": None, "work_state": "OPEN"},
        )
        self.decision_loop = DecisionLoopCommandService(self.adapter, self.authorization)
        initialized = self.decision_loop.initialize_decision_loop(
            self.context("initialize", 0), "episode-o6-pg"
        )
        current = self.decision_loop.get_decision_loop(self.principal, self.scope, "episode-o6-pg")
        self.target = TargetContext("asset-o6-pg", "context-o6-pg", "mm", "characteristic-o6-pg")
        requested = self.decision_loop.request_check(
            self.context("request-human", current.aggregate_version),
            "episode-o6-pg",
            "started-human-check",
            template_id="human-check",
            template_version="v1",
            execution_mode=CheckExecutionMode.REQUEST_HUMAN_MEASUREMENT,
            target_context=self.target.as_dict(),
        )
        self.decision_loop.start_check(
            self.context("start-human", requested.aggregate_version),
            "episode-o6-pg",
            "started-human-check",
        )
        self.before_snapshot = self.decision_loop.get_decision_loop(self.principal, self.scope, "episode-o6-pg")
        self.assertGreater(self.before_snapshot.aggregate_version, initialized.aggregate_version)
        self.facts = self._facts(self.before_snapshot)
        self.policy = PlannerPolicy("pg-policy", "v1", (PairWeight("pair-pg", Decimal("1")),))
        self.catalog = CheckTemplateCatalog(
            "pg-catalog",
            "v1",
            (self._template("read-check", "evidence-pg"), self._template("human-check", "evidence-human", mode=CheckExecutionMode.REQUEST_HUMAN_MEASUREMENT)),
        )

    def context(self, command_id, expected):
        return CommandContext(
            command_id,
            self.principal,
            self.scope,
            expected,
            RevisionVector("analysis-pg", "exposure-pg", "priority-pg", expected, None, "qualification-pg"),
        )

    def _facts(self, snapshot):
        now = datetime.now(timezone.utc)
        return PlannerReadFacts(
            snapshot.episode_id,
            snapshot.active_cycle_id,
            snapshot.aggregate_version,
            snapshot.revision_vector,
            now,
            "family-test",
            "asset",
            self.target,
            # The test pair is a synthetic fixture, not a real-family assertion.
            (UnresolvedHypothesisPair("pair-pg", "h-a", "h-b"),),
            (),
            (EvidenceDependenceGroup("group-pg", "independent-pg", ("pair-pg",)),),
            (CapabilityFact("measurement-pg", CapabilityState.AVAILABLE, "cap-source-pg", now - timedelta(seconds=1), "qual-cap-pg", "context-o6-pg", now + timedelta(days=1)),),
            (
                QualificationFact("qual-cap-pg", QualificationState.QUALIFIED, "qualification-pg-1", now + timedelta(days=1)),
                QualificationFact("qual-template-pg", QualificationState.QUALIFIED, "qualification-pg-2", now + timedelta(days=1)),
            ),
            (),
            (),
            DecisionDeadlineFact(DeadlineState.UNKNOWN, "deadline-source-pg", None, "not supported in fixture"),
            (TurnaroundFact("turnaround-source-pg", TurnaroundState.SUPPORTED, 30, now + timedelta(days=1)),),
        )

    @staticmethod
    def _template(template_id, group_id, *, mode=CheckExecutionMode.READ_EXISTING):
        return CheckTemplate(
            template_id,
            "v1",
            f"Synthetic {template_id}",
            ("family-test",),
            ("asset",),
            ("context-o6-pg",),
            ("mm",),
            False,
            False,
            (CapabilityRequirement("measurement-pg", 3600),),
            (PairDiscrimination("pair-pg", Decimal("1")),),
            (),
            (),
            (),
            (group_id,),
            EffortBand.LOW,
            "effort-source-pg",
            "turnaround-source-pg",
            DisruptionClass.LOW,
            "ephi.check.approve" if mode == CheckExecutionMode.REQUEST_HUMAN_MEASUREMENT else None,
            mode,
            "result-schema-pg",
            "interpretation-schema-pg",
            ("evidence-linked",),
            "qual-template-pg",
        )

    def test_postgresql_18_restart_reproduces_plan_without_workflow_mutation(self):
        self.assertTrue(self.adapter.server_version().startswith("18."), self.adapter.server_version())
        first_service = NextCheckPlannerService(self.decision_loop)
        first = first_service.plan(
            self.principal,
            self.scope,
            "episode-o6-pg",
            expected_workflow_version=self.before_snapshot.aggregate_version,
            viewed_revisions=self.before_snapshot.revision_vector,
            facts=self.facts,
            policy=self.policy,
            catalog=self.catalog,
        )
        self.assertEqual([item.template_id for item in first.recommendations], ["read-check"])
        self.assertEqual(first.excluded_checks[0].reasons[0].value, "CHECK_IN_PROGRESS")
        durable_before = self.adapter.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-o6-pg")
        self.assertEqual(durable_before.state, self.before_snapshot.state)

        self.adapter.close()
        restarted = PostgreSQLReferenceTransactionAdapter(DSN)
        self.addCleanup(restarted.close)
        decision_loop = DecisionLoopCommandService(restarted, self.authorization)
        durable_after = decision_loop.get_decision_loop(self.principal, self.scope, "episode-o6-pg")
        self.assertEqual(durable_after.state, self.before_snapshot.state)
        second = NextCheckPlannerService(decision_loop).plan(
            self.principal,
            self.scope,
            "episode-o6-pg",
            expected_workflow_version=self.before_snapshot.aggregate_version,
            viewed_revisions=self.before_snapshot.revision_vector,
            facts=self.facts,
            policy=self.policy,
            catalog=self.catalog,
        )
        self.assertEqual(second.plan_identity, first.plan_identity)
        self.assertEqual(second.as_dict(), first.as_dict())
        durable_final = restarted.get_aggregate(self.scope, EPISODE_WORKFLOW_AGGREGATE_TYPE, "episode-o6-pg")
        self.assertEqual(durable_final.state, durable_before.state)
        self.assertEqual(durable_final.version, durable_before.version)


if __name__ == "__main__":
    unittest.main()
