"""CHG-258/U3.1 release/install identity and secret-safety regressions."""

from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application.operations import migration_schema_identity  # noqa: E402
from ephi.downstream.contracts import (  # noqa: E402
    ABI_ID,
    ABI_VERSION,
    MANIFEST_SCHEMA,
    PROVIDER_CONTRACT_VERSION,
    REQUIRED_CATEGORIES,
)
from ephi.release_identity import (  # noqa: E402
    ReleaseFailure,
    _lock_index,
    _supported_minor,
    _verify_abi,
    _verify_migrations,
    _validate_lock_index,
    build_release_inventory,
    canonical_json_bytes,
    main,
    verify_inventory_document,
)
from tools.o9_operations import _migration_identity  # noqa: E402


class ReleaseInventoryTests(unittest.TestCase):
    def test_inventory_is_canonical_stable_and_bound_to_owner_facts(self):
        first = build_release_inventory(ROOT)
        with patch.dict(os.environ, {"EPHI_POSTGRES_DSN": "postgresql://user:password@private.invalid/ephi"}):
            second = build_release_inventory(ROOT)
        self.assertEqual(first, second)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(first["release"]["distribution"], "ephi")
        self.assertEqual(first["release"]["version"], "0.1.0")
        self.assertEqual(first["python"]["requires_python"], ">=3.11,<3.14")
        self.assertEqual(first["python"]["install_supported_interpreters"], ["3.11", "3.12", "3.13"])
        self.assertEqual(first["python"]["repository_compatibility_only"], ["3.14"])
        self.assertEqual(first["dependencies"]["base"]["commit"], "000298562d6bcbf6df304edbd41b98b30fe4bfcf")
        self.assertEqual(first["dependencies"]["base"]["framework_version"], "3.0.0a8")
        self.assertEqual(first["dependencies"]["base"]["nicegui_version"], "3.15.0")
        self.assertEqual(first["downstream_abi"], {
            "id": ABI_ID,
            "version": ABI_VERSION,
            "provider_contract_version": PROVIDER_CONTRACT_VERSION,
            "manifest_schema": MANIFEST_SCHEMA,
            "required_categories": list(REQUIRED_CATEGORIES),
        })
        self.assertEqual(first["migrations"], _migration_identity(ROOT))
        self.assertEqual(first["migrations"]["migration_count"], 11)

    def test_inventory_digest_rejects_changed_fact_and_committed_bytes_are_current(self):
        inventory = build_release_inventory(ROOT)
        raw = canonical_json_bytes(inventory) + b"\n"
        verify_inventory_document(inventory, raw)
        changed = copy.deepcopy(inventory)
        changed["release"]["version"] = "9.9.9"
        with self.assertRaises(ReleaseFailure) as caught:
            verify_inventory_document(changed, canonical_json_bytes(changed) + b"\n")
        self.assertEqual(caught.exception.reason_code, "RELEASE_INVENTORY_INVALID")
        committed = ROOT / "src/ephi/release_inventory.json"
        self.assertEqual(committed.read_bytes(), raw)

    def test_project_identity_mismatch_fails_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
            shutil.copytree(ROOT / "environment", root / "environment")
            shutil.copytree(ROOT / ".github", root / ".github")
            shutil.copytree(ROOT / "migrations", root / "migrations")
            shutil.copytree(ROOT / "src/ephi", root / "src/ephi")
            pyproject = root / "pyproject.toml"
            pyproject.write_text(pyproject.read_text().replace('"nicegui==3.15.0"', '"nicegui==3.14.0"'), encoding="utf-8")
            with self.assertRaises(ReleaseFailure) as caught:
                build_release_inventory(root)
            self.assertEqual(caught.exception.reason_code, "BASE_RUNTIME_CONTRACT_INVALID")

    def test_lock_index_rejects_tampered_immutable_lock(self):
        index, _ = _lock_index(ROOT)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = ROOT / "src/ephi/release_locks"
            target = root / "src/ephi/release_locks"
            shutil.copytree(source, target)
            with (target / "runtime-py311.txt").open("a", encoding="utf-8") as stream:
                stream.write("# modified input\n")
            with self.assertRaises(ReleaseFailure) as caught:
                _validate_lock_index(root, index, ">=3.11,<3.14")
            self.assertEqual(caught.exception.reason_code, "LOCK_INPUT_MISSING_OR_TAMPERED")

    def test_shared_migration_identity_detects_added_removed_and_modified_inputs(self):
        expected = migration_schema_identity(ROOT / "migrations")
        self.assertEqual(expected, _migration_identity(ROOT))
        originals = sorted((ROOT / "migrations").glob("*.sql"))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "migrations"
            root.mkdir()
            for path in originals:
                shutil.copyfile(path, root / path.name)
            self.assertEqual(migration_schema_identity(root), expected)
            (root / "012_u3_not_a_product_migration.sql").write_text("-- fixture\n", encoding="utf-8")
            self.assertNotEqual(migration_schema_identity(root), expected)
            (root / "012_u3_not_a_product_migration.sql").unlink()
            removed = originals.pop()
            (root / removed.name).unlink()
            self.assertNotEqual(migration_schema_identity(root), expected)
            shutil.copyfile(removed, root / removed.name)
            first = next(root.glob("*.sql"))
            first.write_bytes(first.read_bytes() + b"\n-- modified fixture\n")
            self.assertNotEqual(migration_schema_identity(root), expected)

    def test_preflight_migration_and_abi_mismatches_fail_closed(self):
        inventory = build_release_inventory(ROOT)
        changed_migrations = copy.deepcopy(inventory)
        changed_migrations["migrations"]["identity_sha256"] = "f" * 64
        with self.assertRaises(ReleaseFailure) as migration_error:
            _verify_migrations(changed_migrations)
        self.assertEqual(migration_error.exception.reason_code, "MIGRATION_IDENTITY_MISMATCH")

        changed_abi = copy.deepcopy(inventory)
        changed_abi["downstream_abi"]["version"] = "2.0.0"
        with self.assertRaises(ReleaseFailure) as abi_error:
            _verify_abi(changed_abi)
        self.assertEqual(abi_error.exception.reason_code, "DOWNSTREAM_ABI_IDENTITY_MISMATCH")

    def test_python_314_is_not_accepted_as_install_support(self):
        with self.assertRaises(ReleaseFailure) as caught:
            _supported_minor("3.14", ["3.11", "3.12", "3.13"])
        self.assertEqual(caught.exception.reason_code, "UNSUPPORTED_PYTHON_VERSION")

    def test_preflight_failure_projection_does_not_echo_private_input_or_environment(self):
        private_path = "/tmp/postgresql://private-user:private-password@private.endpoint/ephi"
        output = io.StringIO()
        with patch.dict(os.environ, {
            "EPHI_POSTGRES_DSN": "postgresql://private-user:private-password@private.endpoint/ephi",
            "EPHI_DOWNSTREAM_ENTRYPOINT": "private.mapping:source_rows",
        }), redirect_stdout(output):
            result = main(["--inputs-dir", private_path, "--json"])
        self.assertEqual(result, 2)
        report = json.loads(output.getvalue())
        self.assertEqual(set(report), {"schema", "status", "reason_code"})
        self.assertEqual(report["reason_code"], "INSTALL_INPUTS_INVALID")
        rendered = output.getvalue()
        for private_value in ("private-user", "private-password", "private.endpoint", "source_rows"):
            self.assertNotIn(private_value, rendered)


if __name__ == "__main__":
    unittest.main()
