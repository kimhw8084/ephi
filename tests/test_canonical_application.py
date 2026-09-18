"""Offline tests for the new canonical EPHI application boundary."""

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi import RuntimeConfig, create_application  # noqa: E402


class CanonicalApplicationTests(unittest.TestCase):
    def test_identity_and_self_check_are_deterministic(self):
        application = create_application(RuntimeConfig(environment="test", host="localhost", port=8081))
        first = application.self_check()
        second = application.self_check()
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "PASS")
        self.assertEqual(first["application"]["source_root"], "src/ephi")
        self.assertEqual(first["checks"]["behavioral_features"], "NOT_IMPLEMENTED")

    def test_runtime_config_rejects_invalid_port(self):
        with self.assertRaisesRegex(ValueError, "port"):
            RuntimeConfig(port=0)
        with self.assertRaisesRegex(ValueError, "EPHI_PORT"):
            RuntimeConfig.from_environment({"EPHI_PORT": "not-an-int"})

    def test_module_entrypoint_is_offline_and_deterministic(self):
        environment = {
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        first = subprocess.run(
            [sys.executable, "-m", "ephi", "--self-check"],
            cwd=ROOT,
            env={**__import__("os").environ, **environment},
            capture_output=True,
            text=True,
            check=False,
        )
        second = subprocess.run(
            [sys.executable, "-m", "ephi", "--self-check"],
            cwd=ROOT,
            env={**__import__("os").environ, **environment},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(first.stdout)["application"]["application_id"], "ephi")

    def test_application_has_no_forbidden_framework_authority(self):
        source_root = ROOT / "src" / "ephi"
        source = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.py"))
        self.assertNotIn("nicegui.ui", source)
        self.assertNotIn("nicegui_base.integrations.nicegui_", source)


if __name__ == "__main__":
    unittest.main()

