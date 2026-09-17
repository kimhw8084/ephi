"""Synthetic archive regressions for the W0 source-restoration preflight."""

from pathlib import Path
import hashlib
import json
import stat
import tempfile
import unittest
import zipfile

from tools.source_preflight import HISTORICAL_TEST_RESULT, preflight, write_result


EXPECTED_FILENAME = "fixture-source.zip"
SOURCE_ROOT = "fixture_source_release"


class SourcePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / EXPECTED_FILENAME
        self.stage = self.root / "stage"

    def make_archive(self, members):
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members:
                archive.writestr(name, content)
        return hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def run_preflight(self, expected_hash=None, **kwargs):
        return preflight(
            self.archive,
            self.stage,
            expected_filename=EXPECTED_FILENAME,
            expected_sha256=expected_hash or "0" * 64,
            expected_source_root=SOURCE_ROOT,
            protected_roots=(self.root / "evidence",),
            **kwargs,
        )

    def test_missing_archive_returns_explicit_source_required(self):
        result = self.run_preflight()
        self.assertEqual(result["status"], "SOURCE_REQUIRED")
        self.assertEqual(result["reason"], "ARCHIVE_MISSING")
        self.assertFalse(result["source"]["verified"])
        self.assertEqual(result["historical_test_result"], HISTORICAL_TEST_RESULT)
        self.assertEqual(result["current_run"]["status"], "NOT_RUN")

    def test_wrong_hash_is_rejected_before_extraction(self):
        actual_hash = self.make_archive([(f"{SOURCE_ROOT}/src/module.py", "print('fixture')\n")])
        result = self.run_preflight(expected_hash="f" * 64)
        self.assertEqual(result["status"], "SOURCE_REJECTED")
        self.assertEqual(result["reason"], "SOURCE_HASH_MISMATCH")
        self.assertEqual(result["source"]["archive_sha256"], actual_hash)
        self.assertFalse(self.stage.exists())

    def test_path_traversal_and_absolute_members_are_rejected(self):
        for unsafe_name in (f"{SOURCE_ROOT}/../../escaped.py", "/absolute.py"):
            with self.subTest(unsafe_name=unsafe_name):
                actual_hash = self.make_archive([(unsafe_name, "not extracted\n")])
                result = self.run_preflight(expected_hash=actual_hash)
                self.assertEqual(result["status"], "SOURCE_REJECTED")
                self.assertEqual(result["reason"], "UNSAFE_MEMBER_PATH")
                self.assertFalse(self.stage.exists())

    def test_symlink_member_is_rejected(self):
        with zipfile.ZipFile(self.archive, "w") as archive:
            link = zipfile.ZipInfo(f"{SOURCE_ROOT}/src/link.py")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "module.py")
        actual_hash = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        result = self.run_preflight(expected_hash=actual_hash)
        self.assertEqual(result["reason"], "UNSAFE_MEMBER_SYMLINK")
        self.assertFalse(self.stage.exists())

    def test_safe_archive_is_staged_and_baseline_is_discovered(self):
        actual_hash = self.make_archive([
            (f"{SOURCE_ROOT}/", ""),
            (f"{SOURCE_ROOT}/src/module.py", "print('fixture')\n"),
            (f"{SOURCE_ROOT}/tests/test_module.py", "def test_fixture():\n    assert True\n"),
            (f"{SOURCE_ROOT}/requirements.txt", "fixture-dependency==1\n"),
        ])
        result = self.run_preflight(expected_hash=actual_hash)
        self.assertEqual(result["status"], "SOURCE_STAGED")
        self.assertTrue(result["source"]["verified"])
        self.assertEqual(result["staging"]["member_count"], 4)
        self.assertEqual(result["baseline"]["python_files"], 2)
        self.assertEqual(result["baseline"]["test_files"], 1)
        self.assertEqual(result["baseline"]["dependency_files"], ["requirements.txt"])
        self.assertEqual(result["baseline"]["test_execution"]["status"], "NOT_RUN")
        self.assertEqual(result["historical_test_result"]["passed"], 275)
        self.assertEqual(result["historical_test_result"]["skipped"], 1)
        expected_source_path = self.stage / SOURCE_ROOT
        reported_staging_path = Path(result["staging"]["source_root"])
        reported_baseline_path = Path(result["baseline"]["source_root"])
        self.assertEqual(reported_staging_path, expected_source_path)
        self.assertEqual(reported_baseline_path, expected_source_path)
        self.assertTrue(reported_staging_path.exists())
        self.assertEqual((reported_staging_path / "src/module.py").read_text(), "print('fixture')\n")

    def test_preserved_evidence_path_is_rejected_without_writes(self):
        evidence = self.root / "evidence"
        evidence.mkdir()
        sentinel = evidence / "sentinel.txt"
        sentinel.write_bytes(b"preserve me")
        actual_hash = self.make_archive([(f"{SOURCE_ROOT}/src/module.py", "print('fixture')\n")])
        result = preflight(
            self.archive,
            evidence / "staged",
            expected_filename=EXPECTED_FILENAME,
            expected_sha256=actual_hash,
            expected_source_root=SOURCE_ROOT,
            protected_roots=(evidence,),
        )
        self.assertEqual(result["reason"], "PROTECTED_PATH")
        self.assertEqual(sentinel.read_bytes(), b"preserve me")
        self.assertFalse((evidence / "staged").exists())

    def test_result_json_is_atomic_and_machine_readable(self):
        result = self.run_preflight()
        output = self.root / "artifacts" / "preflight.json"
        write_result(output, result)
        self.assertEqual(json.loads(output.read_text())["status"], "SOURCE_REQUIRED")
        self.assertEqual(list(output.parent.glob(".preflight.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
