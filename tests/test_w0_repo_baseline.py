"""Offline fail-closed tests for the canonical Git-native W0 baseline."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.w0_repo_baseline import (
    RepoBaselineError,
    git_identity,
    validate_manifest_integrity,
    validate_repository_contract,
)


ROOT = Path(__file__).resolve().parents[1]


class RepoBaselineTests(unittest.TestCase):
    def test_current_contract_and_manifest_are_canonical(self):
        contract = validate_repository_contract(ROOT)
        manifest = validate_manifest_integrity(ROOT)
        self.assertEqual(contract["status"], "PASS")
        self.assertEqual(manifest["status"], "PASS")

    def test_dependency_identity_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("environment/w0_repo_baseline.json", "pyproject.toml"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            for relative in ("__init__.py", "__main__.py", "application.py", "config.py"):
                source = root / "src/ephi" / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes((ROOT / "src/ephi" / relative).read_bytes())
            package = root / "pyproject.toml"
            package.write_text(
                package.read_text(encoding="utf-8").replace("nicegui==3.15.0", "nicegui==3.15.1"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RepoBaselineError, "dependencies") as context:
                validate_repository_contract(root)
            self.assertEqual(context.exception.code, "DEPENDENCY_IDENTITY_MISMATCH")

    def test_package_identity_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("environment/w0_repo_baseline.json", "pyproject.toml"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            for relative in ("__main__.py", "application.py", "config.py"):
                source = root / "src/ephi" / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes((ROOT / "src/ephi" / relative).read_bytes())
            source = root / "src/ephi/__init__.py"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                (ROOT / "src/ephi/__init__.py").read_text(encoding="utf-8").replace('"0.1.0"', '"9.9.9"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RepoBaselineError, "source version") as context:
                validate_repository_contract(root)
            self.assertEqual(context.exception.code, "PACKAGE_IDENTITY_MISMATCH")

    def test_git_identity_fails_closed_outside_a_git_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RepoBaselineError) as context:
                git_identity(Path(directory))
            self.assertIn(context.exception.code, {"GIT_IDENTITY_INVALID", "GIT_UNAVAILABLE"})

    def test_corrupt_git_object_identity_fails_closed(self):
        with patch("tools.w0_repo_baseline._git", side_effect=["not-a-commit", "not-a-tree"]):
            with self.assertRaisesRegex(RepoBaselineError, "SHA-1") as context:
                git_identity(ROOT)
            self.assertEqual(context.exception.code, "GIT_IDENTITY_INVALID")

    def test_canonical_baseline_source_has_no_legacy_archive_inputs(self):
        source = (ROOT / "tools/w0_repo_baseline.py").read_text(encoding="utf-8")
        spec = (ROOT / "environment/w0_repo_baseline.json").read_text(encoding="utf-8")
        for forbidden in ("source_preflight", "source-preflight", "source-staging", ".zip"):
            self.assertNotIn(forbidden, source)
            self.assertNotIn(forbidden, spec)


if __name__ == "__main__":
    unittest.main()
