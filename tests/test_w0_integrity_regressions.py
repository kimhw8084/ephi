"""Offline regressions for the CHG-109 source-bound W0 integrity gate."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tools.source_preflight import (
    EXPECTED_ARCHIVE_FILENAME,
    EXPECTED_ARCHIVE_SHA256,
    EXPECTED_SOURCE_ROOT,
)
from tools.w0_integrity_regressions import (
    CONTRACT_PATH,
    EXPECTED_FINDING_IDS,
    evaluate_f02,
    evaluate_finding,
    run_integrity_regressions,
    validate_contract,
    verify_historical_evidence,
)


class W0IntegrityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.findings = {finding["id"]: finding for finding in self.contract["findings"]}
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_preflight(self, **overrides):
        record = {
            "schema_version": 1,
            "status": "SOURCE_REQUIRED",
            "source": {
                "required_filename": EXPECTED_ARCHIVE_FILENAME,
                "required_sha256": EXPECTED_ARCHIVE_SHA256,
                "required_source_root": EXPECTED_SOURCE_ROOT,
                "archive_path": str(self.root / EXPECTED_ARCHIVE_FILENAME),
                "archive_present": False,
                "archive_sha256": None,
                "verified": False,
            },
            "staging": {
                "status": "NOT_RUN",
                "requested_path": str(self.root / "source-staging"),
                "source_root": None,
                "member_count": None,
            },
            "baseline": {"status": "NOT_RUN"},
        }
        for section, values in overrides.items():
            record[section].update(values)
        path = self.root / "source-preflight.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_contract_matches_existing_source_identity_and_historical_record(self):
        validate_contract(self.contract)
        observations = verify_historical_evidence(self.contract)
        self.assertEqual({row["id"] for row in observations}, set(EXPECTED_FINDING_IDS))
        self.assertEqual(self.contract["historical_evidence"]["status"], "REFERENCE_ONLY")
        self.assertEqual(self.contract["current_execution"]["status"], "NOT_RUN")

    def test_contract_validation_rejects_identity_or_mixed_current_claim(self):
        wrong_identity = deepcopy(self.contract)
        wrong_identity["source_identity"]["required_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source identity"):
            validate_contract(wrong_identity)

        mixed = deepcopy(self.contract)
        mixed["historical_evidence"]["observations"][0]["status"] = "PASS"
        with self.assertRaisesRegex(ValueError, "historical"):
            validate_contract(mixed)

    def test_exact_source_gating_blocks_before_any_current_execution(self):
        preflight = self.write_preflight(
            source={"archive_sha256": "0" * 64, "archive_present": True, "verified": True},
            staging={"status": "STAGED"},
        )
        record = json.loads(preflight.read_text(encoding="utf-8"))
        record["status"] = "SOURCE_STAGED"
        preflight.write_text(json.dumps(record), encoding="utf-8")
        result = run_integrity_regressions(preflight)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "SOURCE_IDENTITY_MISMATCH")
        self.assertEqual(result["current_execution"]["status"], "NOT_RUN")
        self.assertFalse(result["current_execution"]["tests_executed"])
        self.assertTrue(all(item["status"] == "NOT_RUN" for item in result["findings"]))

    def test_missing_preflight_is_deterministically_blocked_and_separate(self):
        missing = self.root / "missing-source-preflight.json"
        first = run_integrity_regressions(missing)
        second = run_integrity_regressions(missing)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "BLOCKED")
        self.assertEqual(first["reason"], "PREFLIGHT_MISSING")
        self.assertEqual(first["historical_evidence"]["status"], "REFERENCE_ONLY")
        self.assertFalse(first["historical_evidence"]["current_execution"])
        self.assertEqual(first["current_execution"]["status"], "NOT_RUN")

    def test_f03_evaluator_requires_attention_visibility(self):
        finding = self.findings["F03"]
        visible = {
            "id": "F03",
            "scenario": finding["scenario_identity"],
            "workflow": "INVESTIGATING",
            "visible_in_attention": True,
        }
        hidden = dict(visible, visible_in_attention=False)
        self.assertEqual(evaluate_finding(finding, visible)["status"], "PASS")
        self.assertEqual(evaluate_finding(finding, hidden)["status"], "FAIL")

    def test_f04_evaluator_requires_exact_historical_cost(self):
        finding = self.findings["F04"]
        base = {"id": "F04", "scenario": finding["scenario_identity"]}
        self.assertEqual(evaluate_finding(finding, dict(base, actual_operating_cost=10))["status"], "PASS")
        for value in (0, 20, 10.01):
            with self.subTest(value=value):
                self.assertEqual(evaluate_finding(finding, dict(base, actual_operating_cost=value))["status"], "FAIL")

    def test_f05_evaluator_rejects_resolved_data_unreliable_sequence(self):
        finding = self.findings["F05"]
        base = {
            "id": "F05",
            "scenario": finding["scenario_identity"],
            "assessment_count": 5,
        }
        self.assertEqual(evaluate_finding(finding, dict(base, actual_episode_state="RECOVERING"))["status"], "PASS")
        self.assertEqual(evaluate_finding(finding, dict(base, actual_episode_state="RESOLVED"))["status"], "FAIL")

    def test_f02_distinguishes_source_only_boundary_from_restore_result(self):
        finding = self.findings["F02"]
        source_only = {
            "id": "F02",
            "scenario": finding["scenario_identity"],
            "outcome": "KeyError",
        }
        passed = evaluate_f02(finding, source_only, "CHECKPOINT_RESTORE_PASS")
        self.assertEqual(passed["status"], "PASS")
        self.assertEqual(passed["source_only_result"], "SOURCE_ONLY_INSUFFICIENT")
        self.assertEqual(passed["checkpoint_restore_result"], "CHECKPOINT_RESTORE_PASS")

        not_run = evaluate_f02(finding, source_only, "NOT_RUN")
        self.assertEqual(not_run["status"], "NOT_RUN")
        self.assertEqual(not_run["source_only_result"], "SOURCE_ONLY_INSUFFICIENT")
        self.assertEqual(not_run["checkpoint_restore_result"], "NOT_RUN")

        failed = evaluate_f02(finding, source_only, "CHECKPOINT_RESTORE_FAIL")
        self.assertEqual(failed["status"], "FAIL")
        self.assertEqual(failed["checkpoint_restore_result"], "CHECKPOINT_RESTORE_FAIL")

        wrong_source_only = dict(source_only, outcome="success")
        wrong = evaluate_f02(finding, wrong_source_only, "CHECKPOINT_RESTORE_PASS")
        self.assertEqual(wrong["status"], "PASS")
        self.assertEqual(wrong["source_only_result"], "SOURCE_ONLY_INSUFFICIENT")


if __name__ == "__main__":
    unittest.main()
