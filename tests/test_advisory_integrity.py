"""Offline F02/F03 domain and checkpoint regressions."""

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.advisory import (
    AdvisoryService,
    CheckpointError,
    CheckpointIdentityError,
    CheckpointSchemaError,
    EngineeringWorkState,
    TechnicalEpisodeState,
    WorkflowVersionConflict,
    restore_checkpoint,
)
from tools.w0_integrity_regressions import run_canonical_integrity_regressions


class AdvisoryIntegrityTests(unittest.TestCase):
    def test_f03_resolved_technical_episode_with_investigating_work_stays_visible(self):
        service = AdvisoryService()
        episode = service.create_episode("episode-f03")
        episode = service.transition_engineering_work_state(
            episode.episode_id,
            EngineeringWorkState.INVESTIGATING,
            expected_workflow_version=episode.workflow_version,
        )
        episode = service.transition_technical_state(
            episode.episode_id,
            TechnicalEpisodeState.RESOLVED,
            expected_workflow_version=episode.workflow_version,
        )

        attention = service.query_attention()

        self.assertEqual([row.episode_id for row in attention], ["episode-f03"])
        self.assertEqual(attention[0].technical_state, TechnicalEpisodeState.RESOLVED)
        self.assertEqual(attention[0].engineering_work_state, EngineeringWorkState.INVESTIGATING)
        self.assertTrue(attention[0].visible_in_attention)

    def test_explicit_terminal_engineering_disposition_removes_work_from_attention(self):
        service = AdvisoryService()
        episode = service.create_episode("episode-terminal")
        episode = service.transition_engineering_work_state(
            episode.episode_id,
            EngineeringWorkState.INVESTIGATING,
            expected_workflow_version=episode.workflow_version,
        )
        service.dispose_engineering_work(
            episode.episode_id,
            expected_workflow_version=episode.workflow_version,
            disposition=EngineeringWorkState.RESOLVED,
        )

        self.assertEqual(service.query_attention(), [])

    def test_stale_workflow_version_is_rejected_without_state_change(self):
        service = AdvisoryService()
        episode = service.create_episode("episode-conflict")
        updated = service.transition_engineering_work_state(
            episode.episode_id,
            EngineeringWorkState.INVESTIGATING,
            expected_workflow_version=0,
        )
        before_stale_attempt = service.get_episode(episode.episode_id)

        with self.assertRaises(WorkflowVersionConflict) as context:
            service.transition_technical_state(
                episode.episode_id,
                TechnicalEpisodeState.RESOLVED,
                expected_workflow_version=0,
            )

        self.assertEqual(context.exception.code, "WORKFLOW_VERSION_CONFLICT")
        self.assertEqual(context.exception.current_workflow_version, updated.workflow_version)
        self.assertEqual(service.get_episode(episode.episode_id), before_stale_attempt)

    def test_checkpoint_round_trip_preserves_authoritative_state(self):
        service = AdvisoryService()
        episode = service.create_episode("episode-checkpoint")
        episode = service.transition_engineering_work_state(
            episode.episode_id,
            EngineeringWorkState.INVESTIGATING,
            expected_workflow_version=0,
        )
        episode = service.transition_technical_state(
            episode.episode_id,
            TechnicalEpisodeState.RESOLVED,
            expected_workflow_version=1,
        )
        checkpoint = service.save_checkpoint(episode.episode_id)

        restored = AdvisoryService().restore_checkpoint(
            checkpoint,
            expected_episode_id=episode.episode_id,
        )

        self.assertEqual(restored.episode_id, episode.episode_id)
        self.assertEqual(restored.technical_state, episode.technical_state)
        self.assertEqual(restored.engineering_work_state, episode.engineering_work_state)
        self.assertEqual(restored.workflow_version, episode.workflow_version)
        self.assertEqual(restored.revision_id, episode.revision_id)
        self.assertEqual(json.loads(checkpoint)["workflow_version"], 2)

    def test_source_only_restore_is_rejected_as_insufficient_authority(self):
        partial = {
            "checkpoint_schema": "ephi.advisory.checkpoint",
            "checkpoint_version": 1,
            "episode_id": "episode-source-only",
            "technical_state": "ACTIVE",
        }

        with self.assertRaises(CheckpointError) as context:
            restore_checkpoint(partial)

        self.assertEqual(context.exception.code, "INCOMPLETE_AUTHORITY")

    def test_checkpoint_schema_version_and_state_fail_closed(self):
        service = AdvisoryService()
        checkpoint = json.loads(service.save_checkpoint(service.create_episode("episode-schema").episode_id))

        unsupported = dict(checkpoint, checkpoint_version=2)
        with self.assertRaises(CheckpointSchemaError) as context:
            restore_checkpoint(unsupported)
        self.assertEqual(context.exception.code, "UNSUPPORTED_VERSION")

        unsupported_schema = dict(checkpoint, checkpoint_schema="ephi.advisory.v2")
        with self.assertRaises(CheckpointSchemaError) as context:
            restore_checkpoint(unsupported_schema)
        self.assertEqual(context.exception.code, "UNSUPPORTED_SCHEMA")

        invalid_state = dict(checkpoint, engineering_work_state="UNKNOWN")
        with self.assertRaises(CheckpointError) as context:
            restore_checkpoint(invalid_state)
        self.assertEqual(context.exception.code, "INVALID_STATE")

        invalid_version = dict(checkpoint, workflow_version=-1)
        with self.assertRaises(CheckpointError) as context:
            restore_checkpoint(invalid_version)
        self.assertEqual(context.exception.code, "INVALID_WORKFLOW_VERSION")

    def test_malformed_and_identity_mismatched_checkpoint_fail_closed(self):
        with self.assertRaises(CheckpointError) as context:
            restore_checkpoint("{not-json")
        self.assertEqual(context.exception.code, "MALFORMED_PAYLOAD")

        service = AdvisoryService()
        episode = service.create_episode("episode-identity")
        with self.assertRaises(CheckpointIdentityError) as context:
            restore_checkpoint(
                service.save_checkpoint(episode.episode_id),
                expected_episode_id="different-episode",
            )
        self.assertEqual(context.exception.code, "IDENTITY_MISMATCH")

    def test_canonical_runner_executes_f02_f03_f04_f05(self):
        result = run_canonical_integrity_regressions()

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["current_execution"]["status"], "PASS")
        findings = {item["id"]: item for item in result["findings"]}
        self.assertEqual(findings["F02"]["status"], "PASS")
        self.assertEqual(findings["F02"]["source_only_result"], "SOURCE_ONLY_INSUFFICIENT")
        self.assertEqual(findings["F02"]["checkpoint_restore_result"], "CHECKPOINT_RESTORE_PASS")
        self.assertEqual(findings["F03"]["status"], "PASS")
        self.assertEqual(findings["F04"]["status"], "PASS")
        self.assertEqual(findings["F05"]["status"], "PASS")
        self.assertEqual(findings["F05"]["observed"]["assessment_count"], 5)
        self.assertNotEqual(findings["F05"]["observed"]["actual_episode_state"], "RESOLVED")


if __name__ == "__main__":
    unittest.main()
