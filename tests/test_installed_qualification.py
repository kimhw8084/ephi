"""CHG-287/U3.6 qualification-kit scope and fail-closed contract checks."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.installed_qualification import _browser_executable, _gate_matrix  # noqa: E402


class InstalledQualificationTests(unittest.TestCase):
    def test_gate_matrix_preserves_all_and_only_governing_gate_ids(self):
        records = _gate_matrix({})
        self.assertEqual([item["gate"] for item in records], [f"G{number:02d}" for number in range(13)])
        required_fields = {
            "gate", "declared_scope", "state", "reason_code",
            "evidence_identity", "remaining_prerequisite",
        }
        self.assertTrue(all(required_fields <= set(item) for item in records))
        self.assertEqual(records[-1]["state"], "PENDING")

    def test_gate_scopes_remain_the_delivery_authorities(self):
        text = (ROOT / "09_Delivery_and_Gates.md").read_text(encoding="utf-8")
        expected = {}
        for line in text.splitlines():
            if line.startswith("| G"):
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                gate = cells[0].split(maxsplit=1)[0] if cells else ""
                if len(cells) == 3 and gate in {f"G{number:02d}" for number in range(13)}:
                    expected[gate] = cells[1]
        actual = {item["gate"]: item["declared_scope"] for item in _gate_matrix({})}
        self.assertEqual(actual, expected)

    def test_absent_explicit_browser_is_a_bounded_blocked_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing-chromium"
            executable, reason = _browser_executable(str(missing))
        self.assertIsNone(executable)
        self.assertEqual(reason, "BROWSER_EXECUTABLE_UNAVAILABLE")

    def test_provider_and_playwright_remain_separate_from_normal_ephi_dependencies(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        normal_dependencies = project["dependencies"] + [
            item for group in project["optional-dependencies"].values() for item in group
        ]
        self.assertFalse(any("synthetic-downstream" in item or "playwright" in item for item in normal_dependencies))
        self.assertEqual(
            project["scripts"]["ephi-qualify"],
            "ephi.installed_qualification:main",
        )


if __name__ == "__main__":
    unittest.main()
