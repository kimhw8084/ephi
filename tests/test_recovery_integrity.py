from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.recovery import (
    IntegrityAttribution,
    ObservationOutcome,
    RecoveryEpisode,
    RecoveryEvaluator,
    RecoveryPolicy,
    RecoveryReasonCode,
    RecoveryService,
    RecoveryState,
    RecoveryValidationError,
    RecoveryObservation,
    Severity,
)


NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


class RecoveryIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.policy = RecoveryPolicy.deterministic_w0_regression()
        self.service = RecoveryService(self.policy)
        self.episode = self.service.create_episode("episode-1")

    def observation(self, *, index=1, **overrides):
        observed_at = NOW - timedelta(minutes=10 + index)
        values = {
            "observation_id": f"observation-{index}",
            "episode_id": self.episode.episode_id,
            "sampling_identity": f"sample-{index}",
            "event_at": observed_at - timedelta(minutes=1),
            "observed_at": observed_at,
            "available_at": observed_at + timedelta(minutes=1),
            "context": self.policy.expected_context,
            "characteristic": self.policy.expected_characteristic,
            "unit": self.policy.expected_unit,
            "severity": Severity.OBSERVE,
            "outcome": ObservationOutcome.HEALTHY,
            "confidence": 0.95,
            "leading_hypothesis": "NO_KNOWN_INTEGRITY_ISSUE",
            "integrity_attribution": IntegrityAttribution.NONE,
            "reference_valid": True,
            "capability_valid": True,
        }
        values.update(overrides)
        return RecoveryObservation(**values)

    def evaluate(self, observation):
        return RecoveryEvaluator(self.policy).evaluate(observation, evaluated_at=NOW)

    def submit(self, observation):
        return self.service.submit_observation(observation, evaluated_at=NOW)

    def test_exact_five_low_confidence_pipeline_suspect_observations_stay_active(self):
        for index in range(1, 6):
            assessment = self.submit(
                self.observation(
                    index=index,
                    outcome=ObservationOutcome.UNKNOWN,
                    confidence=0.0,
                    leading_hypothesis="DATA_PIPELINE_OR_SCHEMA_CHANGE",
                    integrity_attribution=IntegrityAttribution.DATA_PIPELINE_OR_SCHEMA_CHANGE,
                )
            )
            self.assertNotEqual(assessment.episode.state, RecoveryState.RESOLVED)
        episode = self.service.get_episode(self.episode.episode_id)
        self.assertEqual(episode.assessment_count, 5)
        self.assertEqual(episode.state, RecoveryState.ACTIVE)
        self.assertEqual(episode.eligible_independent_count, 0)

    def test_low_confidence_is_ineligible_even_with_observe_severity(self):
        result = self.evaluate(self.observation(confidence=0.0))
        self.assertFalse(result.eligible)
        self.assertIn(RecoveryReasonCode.LOW_CONFIDENCE, result.reason_codes)
        self.assertFalse(result.criterion_results["confidence"].passed)

    def test_pipeline_schema_or_measurement_integrity_suspect_is_ineligible(self):
        result = self.evaluate(
            self.observation(
                integrity_attribution=IntegrityAttribution.DATA_PIPELINE_OR_SCHEMA_CHANGE,
                leading_hypothesis="DATA_PIPELINE_OR_SCHEMA_CHANGE",
            )
        )
        self.assertFalse(result.eligible)
        self.assertIn(RecoveryReasonCode.PIPELINE_OR_SCHEMA_SUSPECT, result.reason_codes)
        self.assertFalse(result.criterion_results["integrity_attribution"].passed)

    def test_stale_and_unavailable_observations_are_explicitly_ineligible(self):
        stale_observed = NOW - timedelta(days=2)
        stale = self.evaluate(
            self.observation(
                observed_at=stale_observed,
                event_at=stale_observed - timedelta(minutes=1),
                available_at=stale_observed + timedelta(minutes=1),
            )
        )
        unavailable = self.evaluate(self.observation(available_at=None))
        self.assertIn(RecoveryReasonCode.STALE_OBSERVATION, stale.reason_codes)
        self.assertIn(RecoveryReasonCode.OBSERVATION_UNAVAILABLE, unavailable.reason_codes)

    def test_context_characteristic_and_unit_mismatch_each_fail_closed(self):
        for field, code in (
            ("context", RecoveryReasonCode.CONTEXT_MISMATCH),
            ("characteristic", RecoveryReasonCode.CHARACTERISTIC_MISMATCH),
            ("unit", RecoveryReasonCode.UNIT_MISMATCH),
        ):
            with self.subTest(field=field):
                result = self.evaluate(self.observation(**{field: "wrong"}))
                self.assertFalse(result.eligible)
                self.assertIn(code, result.reason_codes)

    def test_invalid_reference_and_capability_each_fail_closed(self):
        invalid_reference = self.evaluate(self.observation(reference_valid=False))
        invalid_capability = self.evaluate(self.observation(capability_valid=False))
        self.assertIn(RecoveryReasonCode.INVALID_REFERENCE, invalid_reference.reason_codes)
        self.assertIn(RecoveryReasonCode.INVALID_CAPABILITY, invalid_capability.reason_codes)

    def test_duplicate_sampling_identity_does_not_inflate_independent_count(self):
        first = self.submit(self.observation(index=1))
        duplicate = self.submit(self.observation(index=2, sampling_identity="sample-1"))
        self.assertTrue(first.eligibility.eligible)
        self.assertFalse(duplicate.eligibility.eligible)
        self.assertIn(RecoveryReasonCode.DUPLICATE_SAMPLING_IDENTITY, duplicate.eligibility.reason_codes)
        self.assertEqual(duplicate.episode.eligible_independent_count, 1)

    def test_qualified_positive_control_resolves_only_at_required_count(self):
        first = self.submit(self.observation(index=1))
        second = self.submit(self.observation(index=2))
        third = self.submit(self.observation(index=3))
        self.assertEqual(first.episode.state, RecoveryState.RECOVERING)
        self.assertEqual(second.episode.state, RecoveryState.RECOVERING)
        self.assertEqual(third.episode.state, RecoveryState.RESOLVED)
        self.assertEqual(third.episode.eligible_independent_count, self.policy.minimum_eligible_independent_samples)

    def test_qualified_contradictory_evidence_resets_recovery_to_active(self):
        for index in range(1, 4):
            self.submit(self.observation(index=index))
        contradictory = self.submit(
            self.observation(
                index=4,
                severity=Severity.ALERT,
                outcome=ObservationOutcome.ABNORMAL,
            )
        )
        self.assertTrue(contradictory.eligibility.contradictory)
        self.assertIn(RecoveryReasonCode.CONTRADICTORY_OUTCOME, contradictory.eligibility.reason_codes)
        self.assertEqual(contradictory.episode.state, RecoveryState.ACTIVE)
        self.assertEqual(contradictory.episode.eligible_independent_count, 0)

    def test_malformed_nonfinite_and_out_of_range_inputs_fail_closed(self):
        with self.assertRaises(RecoveryValidationError):
            self.observation(observation_id=" ")
        with self.assertRaises(RecoveryValidationError):
            self.observation(event_at=datetime(2026, 1, 10, 12, 0))
        with self.assertRaises(RecoveryValidationError):
            self.observation(severity="OBSERVE")
        with self.assertRaises(RecoveryValidationError):
            RecoveryEpisode("episode-2", state="ACTIVE")
        for confidence in (math.nan, math.inf, -0.01, 1.01):
            with self.subTest(confidence=confidence):
                with self.assertRaises(RecoveryValidationError):
                    self.observation(confidence=confidence)
        missing = RecoveryEvaluator(self.policy).evaluate(None, evaluated_at=NOW)
        self.assertEqual(missing.reason_codes, (RecoveryReasonCode.MISSING_OBSERVATION,))


if __name__ == "__main__":
    unittest.main()
