"""Offline fail-closed tests for the canonical Git-native W0 baseline."""

from pathlib import Path
import json
import tempfile
import unittest

from tools.w0_repo_baseline import (
    EXPECTED,
    BaselineError,
    load_spec,
    run_baseline,
    validate_git_identity,
    validate_project_metadata,
)


ROOT = Path(__file__).resolve().parents[1]


class W0RepoBaselineTests(unittest.TestCase):
    def test_current_identity_passes_without_network(self):
        result = run_baseline(ROOT, execute_tests=False)
        self.assertEqual(result["status"], "BASELINE_PASS")
        self.assertEqual(result["scope"], "canonical-repository-only")
        self.assertFalse(result["historical_test_result"]["used_by_baseline"])
        self.assertEqual(result["package"]["dependencies"][1], EXPECTED["nicegui_requirement"])
        self.assertEqual(result["import"]["identity"]["application"]["implementation"], "canonical-repository")

    def test_default_baseline_source_has_no_legacy_artifact_dependency(self):
        source = (ROOT / "tools/w0_repo_baseline.py").read_text(encoding="utf-8")
        for forbidden in ("ephi_v0.19.1_production_hardened", "source-preflight.json", "source-staging", "archive_sha256"):
            self.assertNotIn(forbidden, source)

    def test_git_identity_corruption_fails_closed(self):
        identity = {
            "root": str(ROOT),
            "commit": "not-a-commit",
            "tree": "0" * 40,
        }
        with self.assertRaisesRegex(BaselineError, "full commit identity"):
            validate_git_identity(identity, ROOT)

    def test_project_dependency_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                "[project]\nname='ephi'\nversion='0.1.0'\nrequires-python='>=3.11,<3.14'\n"
                "dependencies=['nicegui==3.15.1']\n[project.scripts]\nephi='ephi.app:main'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BaselineError, "dependencies"):
                validate_project_metadata(root, load_spec())

    def test_spec_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "baseline.json"
            spec = json.loads((ROOT / "environment/w0_repo_baseline.json").read_text(encoding="utf-8"))
            spec["repository"]["version"] = "9.9.9"
            path.write_text(json.dumps(spec), encoding="utf-8")
            with self.assertRaisesRegex(BaselineError, "pinned canonical identity"):
                load_spec(path)


if __name__ == "__main__":
    unittest.main()
