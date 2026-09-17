"""Offline regression tests for CHG-105 environment identity fail-closed rules."""

import json
from pathlib import Path
import unittest

from tools.w0_runtime import (
    BINDING_PATH,
    SPEC_PATH,
    authority_names,
    python_supported,
    validate_identity_values,
)


class W0RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.binding = json.loads(BINDING_PATH.read_text(encoding="utf-8"))
        self.direct_url = {
            "url": self.spec["framework"]["repository"],
            "vcs_info": {
                "vcs": "git",
                "commit_id": self.spec["framework"]["commit"],
                "requested_revision": self.spec["framework"]["commit"],
            },
        }

    def identity_issues(self, **overrides):
        values = {
            "python_major": 3,
            "python_minor": 11,
            "framework_version": "3.0.0a8",
            "framework_direct_url": self.direct_url,
            "nicegui_version": "3.15.0",
        }
        values.update(overrides)
        return validate_identity_values(self.spec, **values)

    def test_supported_python_window_is_exact(self):
        self.assertTrue(python_supported(3, 11))
        self.assertTrue(python_supported(3, 12))
        self.assertTrue(python_supported(3, 13))
        self.assertFalse(python_supported(3, 10))
        self.assertFalse(python_supported(3, 14))

    def test_exact_identity_passes(self):
        self.assertEqual(self.identity_issues(), [])

    def test_python_outside_window_fails_closed(self):
        issues = self.identity_issues(python_minor=14)
        self.assertIn("PYTHON_VERSION_OUT_OF_RANGE", {issue["code"] for issue in issues})

    def test_missing_vcs_provenance_fails_closed(self):
        issues = self.identity_issues(framework_direct_url=None)
        self.assertIn("FRAMEWORK_SOURCE_IDENTITY_UNAVAILABLE", {issue["code"] for issue in issues})

    def test_wrong_commit_fails_closed(self):
        direct_url = json.loads(json.dumps(self.direct_url))
        direct_url["vcs_info"]["commit_id"] = "0" * 40
        issues = self.identity_issues(framework_direct_url=direct_url)
        self.assertIn("FRAMEWORK_COMMIT_MISMATCH", {issue["code"] for issue in issues})

    def test_wrong_nicegui_version_fails_closed(self):
        issues = self.identity_issues(nicegui_version="3.15.1")
        self.assertIn("NICEGUI_VERSION_MISMATCH", {issue["code"] for issue in issues})

    def test_binding_authorities_are_public_root_names(self):
        names = authority_names(self.binding)
        self.assertEqual(len(names), 21)
        self.assertIn("AnalysisWorkspacePage", names)
        self.assertIn("ApplicationRuntime", names)
        self.assertTrue(all(name.isidentifier() for name in names))


if __name__ == "__main__":
    unittest.main()
