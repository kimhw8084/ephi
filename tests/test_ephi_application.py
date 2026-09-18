"""Offline tests for the minimal canonical EPHI application boundary."""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi import RuntimeSettings, application_identity, self_check  # noqa: E402
from ephi.app import main  # noqa: E402


class EphiApplicationTests(unittest.TestCase):
    def test_identity_is_canonical_and_not_historical(self):
        identity = application_identity()
        self.assertEqual(identity["application"]["distribution"], "ephi")
        self.assertEqual(identity["application"]["implementation"], "canonical-repository")
        self.assertFalse(identity["application"]["historical_identity_claimed"])
        self.assertEqual(identity["framework"]["git_commit"], "000298562d6bcbf6df304edbd41b98b30fe4bfcf")

    def test_runtime_settings_are_deterministic_without_environment(self):
        self.assertEqual(RuntimeSettings.from_environment({}).as_dict(), {
            "environment": "development",
            "host": "127.0.0.1",
            "port": 8080,
            "application_name": "ephi",
        })

    def test_self_check_does_not_fabricate_integrity_passes(self):
        result = self_check()
        self.assertEqual(result["status"], "PASS")
        for finding in ("F02", "F03", "F04", "F05"):
            self.assertEqual(result["behavioral_checks"][finding]["status"], "NOT_IMPLEMENTED")
            self.assertEqual(result["behavioral_checks"][finding]["execution"], "NOT_RUN")

    def test_entry_boundary_is_offline(self):
        self.assertEqual(main(["--self-check", "--json"]), 0)


if __name__ == "__main__":
    unittest.main()
