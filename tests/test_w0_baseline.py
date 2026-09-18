"""Legacy compatibility regressions for the historical-source baseline."""

from pathlib import Path
import json
import tempfile
import unittest

from tools.source_preflight import (
    EXPECTED_ARCHIVE_FILENAME,
    EXPECTED_ARCHIVE_SHA256,
    EXPECTED_SOURCE_ROOT,
    HISTORICAL_TEST_RESULT,
    discover_baseline,
)
from tools.w0_baseline import run_baseline


class W0BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / "source-staging"
        self.source = self.stage / EXPECTED_SOURCE_ROOT

    def make_source(self):
        tests = self.source / "tests"
        tests.mkdir(parents=True)
        (tests / "test_fixture.py").write_text(
            "import unittest\n\n"
            "class FixtureTests(unittest.TestCase):\n"
            "    def test_deterministic_fixture(self):\n"
            "        self.assertEqual(2 + 2, 4)\n",
            encoding="utf-8",
        )

    def make_record(self, *, source_exists=True):
        # This is a synthetic preflight-shaped fixture, never the owner archive or source evidence.
        if source_exists:
            self.make_source()
            baseline = discover_baseline(self.source)
        else:
            baseline = {
                "status": "DISCOVERED",
                "source_root": str(self.source),
                "test_execution": {"runner_discovered": "unittest"},
            }
        return {
            "schema_version": 1,
            "status": "SOURCE_STAGED",
            "source": {
                "required_filename": EXPECTED_ARCHIVE_FILENAME,
                "required_sha256": EXPECTED_ARCHIVE_SHA256,
                "required_source_root": EXPECTED_SOURCE_ROOT,
                "archive_path": str(self.root / EXPECTED_ARCHIVE_FILENAME),
                "archive_present": True,
                "archive_sha256": EXPECTED_ARCHIVE_SHA256,
                "verified": True,
            },
            "staging": {
                "status": "STAGED",
                "requested_path": str(self.stage),
                "source_root": str(self.source),
                "member_count": 2,
            },
            "baseline": baseline,
            "historical_test_result": HISTORICAL_TEST_RESULT,
            "current_run": {"status": "NOT_RUN", "tests_executed": False},
        }

    def write_record(self, record):
        path = self.root / "source-preflight.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_missing_preflight_is_blocked_without_current_execution(self):
        result = run_baseline(self.root / "missing.json")
        self.assertEqual(result["status"], "BASELINE_BLOCKED")
        self.assertEqual(result["reason"], "PREFLIGHT_MISSING")
        self.assertEqual(result["current_run"]["status"], "NOT_RUN")
        self.assertEqual(result["historical_test_result"], HISTORICAL_TEST_RESULT)

    def test_not_staged_preflight_is_blocked(self):
        record = self.make_record(source_exists=False)
        record["status"] = "SOURCE_REQUIRED"
        result = run_baseline(self.write_record(record))
        self.assertEqual(result["status"], "BASELINE_BLOCKED")
        self.assertEqual(result["reason"], "PREFLIGHT_NOT_SOURCE_STAGED")
        self.assertFalse(result["current_run"]["tests_executed"])

    def test_identity_mismatch_is_blocked_before_source_tests(self):
        record = self.make_record()
        record["source"]["archive_sha256"] = "0" * 64
        result = run_baseline(self.write_record(record))
        self.assertEqual(result["status"], "BASELINE_BLOCKED")
        self.assertEqual(result["reason"], "SOURCE_IDENTITY_MISMATCH")
        self.assertEqual(result["inventory"]["status"], "NOT_RUN")

    def test_missing_source_root_is_blocked(self):
        record = self.make_record(source_exists=False)
        result = run_baseline(self.write_record(record))
        self.assertEqual(result["status"], "BASELINE_BLOCKED")
        self.assertEqual(result["reason"], "SOURCE_ROOT_MISSING")
        self.assertEqual(result["current_run"]["status"], "NOT_RUN")

    def test_deterministic_fixture_is_inventoried_and_executed(self):
        record = self.make_record()
        output = self.root / "w0-baseline.json"
        result = run_baseline(self.write_record(record), output)
        self.assertEqual(result["status"], "BASELINE_PASS")
        self.assertEqual(result["inventory"]["python_files"], 1)
        self.assertEqual(result["inventory"]["test_files"], 1)
        self.assertEqual(result["inventory"]["test_roots"], ["tests"])
        self.assertEqual(result["current_run"]["runner"], "unittest")
        self.assertTrue(result["current_run"]["tests_executed"])
        self.assertEqual(result["current_run"]["passed"], 1)
        self.assertEqual(result["current_run"]["skipped"], 0)
        self.assertEqual(result["historical_test_result"]["status"], "REFERENCE_ONLY")
        self.assertEqual(json.loads(output.read_text())["status"], "BASELINE_PASS")


if __name__ == "__main__":
    unittest.main()
