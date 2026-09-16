"""Exercise validation failures through the real CLI in isolated package copies."""

from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools.check_package import ROOT, package_files


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for source in package_files(ROOT):
            target = self.root / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def run_check(self, *args):
        return subprocess.run(
            [sys.executable, str(self.root / "tools/check_package.py"), *args],
            cwd=self.root, capture_output=True, text=True, check=False)

    def assert_rejected(self, expected, *args):
        result = self.run_check(*args)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)

    def test_current_package_passes_without_git_or_dependencies(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Application/runtime/browser/production qualification NOT_RUN", result.stdout)

    def test_changed_document_requires_explicit_manifest_refresh(self):
        path = self.root / "README.md"
        path.write_text(path.read_text() + "\nReviewed change.\n")
        self.assert_rejected("manifest hash/size mismatch: README.md")
        result = self.run_check("--refresh-manifest")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_file_is_rejected(self):
        (self.root / "CONTRIBUTING.md").unlink()
        self.assert_rejected("manifest file missing: CONTRIBUTING.md")

    def test_unlisted_file_is_rejected(self):
        (self.root / "extra.md").write_text("# Unreviewed file\n")
        self.assert_rejected("manifest file inventory differs")

    def test_historical_evidence_cannot_be_blessed_by_refresh(self):
        (self.root / "evidence/pytest.log").write_text("275 passed, 1 skipped\n")
        self.assert_rejected("historical evidence changed: evidence/pytest.log", "--refresh-manifest")

    def test_broken_link_is_rejected_after_refresh(self):
        path = self.root / "README.md"
        path.write_text(path.read_text() + "\n[Missing](missing.md)\n")
        self.assert_rejected("broken local link: missing.md", "--refresh-manifest")

    def test_unclosed_fence_is_rejected(self):
        path = self.root / "README.md"
        path.write_text(path.read_text() + "\n```python\nprint('unfinished')\n")
        self.assert_rejected("unclosed Markdown code fence", "--refresh-manifest")

    def test_bad_json_and_python_are_rejected(self):
        for name, content in (("broken.json", '{"a": 1, "a": 2}'),
                              ("broken.py", "def unfinished(\n")):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text(content)
                self.assert_rejected(f"invalid syntax in {name}", "--refresh-manifest")
                path.unlink()

    def test_missing_requirement_is_rejected(self):
        path = self.root / "10_Traceability_and_Decisions.md"
        path.write_text(path.read_text().replace("| R4 Planner |", "| Planner |"))
        self.assert_rejected("missing traceability identifier R4", "--refresh-manifest")

    def test_manifest_cannot_reference_parent_directory(self):
        path = self.root / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["files"][0]["path"] = "../outside.txt"
        path.write_text(json.dumps(manifest))
        self.assert_rejected("unsafe manifest path")

    def test_symlink_is_rejected_without_reading_target(self):
        (self.root / "external.txt").symlink_to(self.root.parent / "absent.txt")
        self.assert_rejected("symlink is not a package file")

    def test_secret_pattern_is_reported_without_disclosing_value(self):
        sentinel = "gh" + "p_" + "Z" * 36
        (self.root / "accidental.txt").write_text(sentinel)
        result = self.run_check("--refresh-manifest")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("possible credential/private key in accidental.txt", result.stderr)
        self.assertNotIn(sentinel, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
