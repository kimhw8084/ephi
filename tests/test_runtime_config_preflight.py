"""CHG-260/U3.2 runtime configuration contract and preflight evidence."""

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

from ephi.config import RuntimeSettings  # noqa: E402
from ephi.config_preflight import main, preflight  # noqa: E402
from ephi.release_identity import build_release_inventory  # noqa: E402
from ephi.runtime_configuration_contract import (  # noqa: E402
    CONFIGURATION_CONTRACT_PATH,
    build_runtime_configuration_contract,
    canonical_json_bytes,
    contract_document_bytes,
    expected_contract_is_current,
)


def _qa_values(**overrides):
    values = {
        "EPHI_ENV": "qa",
        "EPHI_HOST": "0.0.0.0",
        "EPHI_PORT": "8080",
        "EPHI_DOWNSTREAM_ENTRYPOINT": "private_provider_bundle.provider:build_bundle",
        "EPHI_ALLOWED_BROWSER_ORIGINS": "https://ephi.private.example",
        "NICEGUI_BASE_STORAGE_SECRET": "test-storage-secret",
        "NICEGUI_BASE_SECURE_SESSION_COOKIE": "true",
    }
    values.update(overrides)
    return values


def _direct_values(profile: str):
    return {
        "EPHI_ENV": profile,
        "EPHI_ALLOWED_BROWSER_ORIGINS": "http://127.0.0.1:8080",
        "NICEGUI_BASE_STORAGE_SECRET": "test-storage-secret",
        "EPHI_POSTGRES_DSN": "postgresql://test_user:test_password@127.0.0.1:5432/ephi_test",
        "EPHI_DEV_IDENTITY_SUBJECT": "test-engineer",
        "EPHI_DEV_IDENTITY_CAPABILITIES": "ephi.attention.read,ephi.episode.read",
        "EPHI_DEV_SCOPE_ID": "test-scope",
        "EPHI_METROLOGY_SOURCE_ADAPTER": "test_source.provider:build_observer",
        "EPHI_METROLOGY_SOURCE_ID": "source-1",
        "EPHI_METROLOGY_PROVIDER_ID": "provider-1",
        "EPHI_METROLOGY_FAMILY_ID": "family-1",
        "EPHI_METROLOGY_CAPABILITY_ID": "capability-1",
        "EPHI_METROLOGY_SCOPE_ID": "test-scope",
        "EPHI_METROLOGY_SCHEMA_ID": "schema-1",
        "EPHI_METROLOGY_MAPPING_VERSION": "mapping-v1",
        "EPHI_METROLOGY_MAPPING_HASH": "a" * 64,
        "EPHI_METROLOGY_UNIT": "mm",
    }


class RuntimeConfigurationContractTests(unittest.TestCase):
    def test_contract_is_canonical_versioned_and_owner_complete(self):
        contract = build_runtime_configuration_contract()
        self.assertEqual(contract["schema"], "org.ephi.runtime-configuration.v1")
        self.assertEqual(contract["version"], "1.0.0")
        self.assertTrue(expected_contract_is_current(ROOT / CONFIGURATION_CONTRACT_PATH))
        self.assertEqual((ROOT / CONFIGURATION_CONTRACT_PATH).read_bytes(), contract_document_bytes())
        variables = {item["name"]: item for item in contract["variables"]}
        for name in (
            "EPHI_ENV", "EPHI_PORT", "EPHI_DOWNSTREAM_ENTRYPOINT", "EPHI_ALLOWED_BROWSER_ORIGINS",
            "NICEGUI_BASE_STORAGE_SECRET", "NICEGUI_REDIS_URL", "EPHI_POSTGRES_DSN",
            "EPHI_METROLOGY_MAPPING_VERSION", "EPHI_TEST_POSTGRES_DSN",
        ):
            self.assertIn(name, variables)
            for field in (
                "owner", "consumer", "applicable_profiles", "semantic_type", "required", "optional",
                "requirement_mode", "required_profiles", "required_condition", "default",
                "sensitivity", "raw_value_disclosure_permitted", "authoritative_validator",
            ):
                self.assertIn(field, variables[name])
        self.assertEqual(variables["NICEGUI_BASE_STORAGE_SECRET"]["owner"], "NiceGUI Base-owned / EPHI-consumed via ephi.transport")
        self.assertFalse(variables["EPHI_POSTGRES_DSN"]["required"])
        self.assertEqual(variables["EPHI_POSTGRES_DSN"]["requirement_mode"], "conditional")
        self.assertEqual(variables["EPHI_DOWNSTREAM_ENTRYPOINT"]["required_profiles"], ["qa", "production"])
        self.assertEqual(variables["EPHI_POSTGRES_DSN"]["sensitivity"], "secret_credential_material")
        self.assertIn("O9 database operations", variables["EPHI_POSTGRES_DSN"]["consumer"])
        self.assertFalse(variables["NICEGUI_REDIS_URL"]["raw_value_disclosure_permitted"])
        self.assertFalse(variables["EPHI_DOWNSTREAM_ENTRYPOINT"]["raw_value_disclosure_permitted"])
        for item in contract["variables"]:
            if item["sensitivity"] != "public_metadata":
                self.assertFalse(item["raw_value_disclosure_permitted"], item["name"])
            if item["name"].startswith("NICEGUI_BASE_") or item["name"] == "NICEGUI_REDIS_URL":
                self.assertIn("NiceGUI Base-owned / EPHI-consumed", item["owner"])
        self.assertNotIn("EPHI_SYNTHETIC_SUBJECT", variables)
        self.assertNotIn("EPHI_SYNTHETIC_ARTIFACT_ROOT", variables)

    def test_contract_identity_is_static_across_environment_values_and_order(self):
        expected = preflight(_qa_values())
        reordered = dict(reversed(list(_qa_values(
            UNRELATED_VALUE="private unrelated sentinel",
            NICEGUI_BASE_STORAGE_SECRET="changed-secret-value",
            EPHI_POSTGRES_DSN="postgresql://other:credentials@other.private/db",
        ).items())))
        actual = preflight(reordered)
        self.assertEqual(expected, actual)
        self.assertEqual(expected["configuration_contract"], actual["configuration_contract"])

    def test_downstream_configuration_is_opaque_and_does_not_change_static_digest(self):
        base = preflight(_qa_values())
        changed = preflight(_qa_values(
            EPHI_DOWNSTREAM_ENTRYPOINT="private_another_company_bundle.provider:build_bundle",
            NICEGUI_BASE_STORAGE_SECRET="rotated-credential-value",
            NICEGUI_REDIS_URL="redis://private-user:private-password@redis.private.invalid/7",
            EPHI_POSTGRES_DSN="postgresql://user:password@db.private.invalid/private_database",
        ))
        self.assertEqual(base["configuration_contract"]["sha256"], changed["configuration_contract"]["sha256"])
        self.assertEqual(base["status_code"], "CONFIG_CONTRACT_PASS")
        self.assertEqual(changed["status_code"], "CONFIG_CONTRACT_PASS")
        missing_secret = preflight(_qa_values(NICEGUI_BASE_STORAGE_SECRET=""))
        self.assertEqual(missing_secret["status"], "FAIL")
        self.assertIn("RUNTIME_SECURITY_CONFIGURATION_INVALID", missing_secret["reason_codes"])
        self.assertEqual(base["configuration_contract"]["sha256"], missing_secret["configuration_contract"]["sha256"])

    def test_generated_configuration_contract_change_changes_release_identity(self):
        original = build_release_inventory(ROOT)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
            shutil.copytree(ROOT / "environment", root / "environment")
            shutil.copytree(ROOT / ".github", root / ".github")
            shutil.copytree(ROOT / "migrations", root / "migrations")
            shutil.copytree(ROOT / "src/ephi", root / "src/ephi")
            contract = copy.deepcopy(build_runtime_configuration_contract())
            variables = contract["variables"]
            variables.append({
                "name": "EPHI_CONFIG_CONTRACT_SOURCE_CHANGE_SENTINEL",
                "owner": "test fixture",
                "consumer": "release identity regression",
                "applicable_profiles": ["test"],
                "semantic_type": "test-only",
                "required": False,
                "required_profiles": [],
                "required_condition": None,
                "default": {"kind": "unset"},
                "sensitivity": "public_metadata",
                "raw_value_disclosure_permitted": True,
                "authoritative_validator": "test fixture",
            })
            contract.pop("contract_sha256")
            import hashlib
            contract["contract_sha256"] = hashlib.sha256(canonical_json_bytes(contract)).hexdigest()
            (root / CONFIGURATION_CONTRACT_PATH).write_bytes(contract_document_bytes(contract))
            changed = build_release_inventory(root)
        self.assertNotEqual(original["runtime_configuration"], changed["runtime_configuration"])
        self.assertNotEqual(original["release_identity_sha256"], changed["release_identity_sha256"])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
            shutil.copytree(ROOT / "environment", root / "environment")
            shutil.copytree(ROOT / ".github", root / ".github")
            shutil.copytree(ROOT / "migrations", root / "migrations")
            shutil.copytree(ROOT / "src/ephi", root / "src/ephi")
            source = root / "src/ephi/runtime_configuration_contract.py"
            source.write_bytes(source.read_bytes() + b"\n# source identity fixture\n")
            changed_source = build_release_inventory(root)
        self.assertEqual(original["runtime_configuration"], changed_source["runtime_configuration"])
        self.assertNotEqual(original["release_identity_sha256"], changed_source["release_identity_sha256"])


class ConfigurationPreflightTests(unittest.TestCase):
    def test_valid_dev_and_test_direct_paths_remain_supported(self):
        import importlib

        for profile in ("development", "test"):
            with self.subTest(profile=profile), patch.object(
                importlib, "import_module", side_effect=AssertionError("source adapter load attempted")
            ) as imported:
                report = preflight(_direct_values(profile))
            self.assertEqual(report["status_code"], "CONFIG_CONTRACT_PASS")
            self.assertEqual(report["checks"]["development_test_direct_configuration"], "PASS")
            self.assertEqual(report["checks"]["provider_composition"], "NOT_RUN")
            imported.assert_not_called()

    def test_qa_and_production_require_valid_explicit_entrypoint_without_loading_it(self):
        import importlib

        for profile in ("qa", "production"):
            values = _qa_values(EPHI_ENV=profile)
            if profile == "production":
                values["EPHI_ALLOWED_BROWSER_ORIGINS"] = "https://ephi.private.example"
            with self.subTest(profile=profile), patch.object(importlib, "import_module", side_effect=AssertionError("provider load attempted")) as imported:
                report = preflight(values)
            self.assertEqual(report["status_code"], "CONFIG_CONTRACT_PASS")
            self.assertEqual(report["checks"]["downstream_entrypoint"]["shape"], "VALID")
            self.assertEqual(report["checks"]["provider_composition"], "NOT_RUN")
            imported.assert_not_called()

    def test_qa_missing_bundle_and_legacy_fallback_fail_closed(self):
        missing = preflight(_qa_values(EPHI_DOWNSTREAM_ENTRYPOINT=""))
        self.assertIn("MISSING_DOWNSTREAM_ENTRYPOINT", missing["reason_codes"])
        for legacy_name, legacy_value in (
            ("EPHI_DEV_IDENTITY_SUBJECT", "PRIVATE_IDENTITY_SENTINEL"),
            ("EPHI_METROLOGY_MAPPING_VERSION", "PRIVATE_MAPPING_SENTINEL"),
        ):
            with self.subTest(legacy_name=legacy_name):
                report = preflight(_qa_values(EPHI_DOWNSTREAM_ENTRYPOINT="", **{legacy_name: legacy_value}))
                self.assertIn("MISSING_DOWNSTREAM_ENTRYPOINT", report["reason_codes"])
                self.assertIn("LEGACY_COMPOSITION_CONFIGURATION_FORBIDDEN", report["reason_codes"])

    def test_invalid_core_and_o8_values_fail_through_existing_authorities(self):
        for port in ("not-a-port", "0", "65536"):
            with self.subTest(port=port):
                report = preflight(_qa_values(EPHI_PORT=port))
                self.assertIn("INVALID_RUNTIME_SETTINGS", report["reason_codes"])
        cases = (
            {"NICEGUI_BASE_PROXY_ENABLED": "maybe"},
            {"EPHI_ALLOWED_BROWSER_ORIGINS": "https://private-origin-sentinel.invalid/private/path"},
            {"NICEGUI_BASE_PROXY_ENABLED": "true", "NICEGUI_BASE_TRUSTED_PROXIES": "*"},
            {"NICEGUI_BASE_SAME_SITE": "none", "NICEGUI_BASE_SECURE_SESSION_COOKIE": "false"},
            {"NICEGUI_BASE_EXPECTED_REPLICAS": "many"},
            {"NICEGUI_BASE_EXPECTED_REPLICAS": "2"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                report = preflight(_qa_values(**overrides))
                self.assertEqual(report["checks"]["runtime_security"]["status"], "FAIL")
                self.assertIn("RUNTIME_SECURITY_CONFIGURATION_INVALID", report["reason_codes"])

    def test_invalid_environment_and_entrypoint_are_bounded(self):
        invalid_env = preflight(_qa_values(EPHI_ENV="not-a-supported-profile"))
        self.assertEqual(invalid_env["profile"], "unsupported")
        self.assertIn("RUNTIME_SECURITY_CONFIGURATION_INVALID", invalid_env["reason_codes"])
        invalid_entrypoint = preflight(_qa_values(EPHI_DOWNSTREAM_ENTRYPOINT="private.provider:factory()"))
        self.assertIn("INVALID_DOWNSTREAM_ENTRYPOINT", invalid_entrypoint["reason_codes"])

    def test_adversarial_private_values_never_appear_in_json_or_static_artifacts(self):
        sentinels = (
            "PGUSER_SENTINEL", "PGPASSWORD_SENTINEL", "DBHOST_SENTINEL.private.invalid", "PGDATABASE_SENTINEL",
            "BASE_STORAGE_SECRET_SENTINEL", "REDIS_PASSWORD_SENTINEL", "redis.private.invalid",
            "BROWSER_PRIVATE_HOST_SENTINEL.invalid", "BROWSER_PRIVATE_PATH_SENTINEL",
            "TRUSTED_PROXY_PRIVATE_SENTINEL", "private_network_sentinel", "private_company_provider_package",
            "METROLOGY_SOURCE_IDENTIFIER_SENTINEL", "METROLOGY_MAPPING_IDENTIFIER_SENTINEL",
            "PRIVATE_FILESYSTEM_PATH_SENTINEL", "PRIVATE_OBJECT_PATH_SENTINEL",
        )
        values = _qa_values(
            EPHI_HOST="private-runtime-host-sentinel.invalid",
            EPHI_POSTGRES_DSN="postgresql://PGUSER_SENTINEL:PGPASSWORD_SENTINEL@DBHOST_SENTINEL.private.invalid:5432/PGDATABASE_SENTINEL",
            NICEGUI_BASE_STORAGE_SECRET="BASE_STORAGE_SECRET_SENTINEL",
            NICEGUI_REDIS_URL="redis://user:REDIS_PASSWORD_SENTINEL@redis.private.invalid/2",
            NICEGUI_BASE_EXPECTED_REPLICAS="2",
            NICEGUI_BASE_SESSION_AFFINITY_CONFIRMED="true",
            NICEGUI_BASE_PROXY_ENABLED="true",
            NICEGUI_BASE_TRUSTED_PROXIES="198.51.100.0/24,TRUSTED_PROXY_PRIVATE_SENTINEL,private_network_sentinel",
            EPHI_ALLOWED_BROWSER_ORIGINS="https://BROWSER_PRIVATE_HOST_SENTINEL.invalid/BROWSER_PRIVATE_PATH_SENTINEL",
            EPHI_DOWNSTREAM_ENTRYPOINT="private_company_provider_package.private_module:build_bundle",
            EPHI_METROLOGY_SOURCE_ID="METROLOGY_SOURCE_IDENTIFIER_SENTINEL",
            EPHI_METROLOGY_MAPPING_VERSION="METROLOGY_MAPPING_IDENTIFIER_SENTINEL",
            EPHI_TEST_POSTGRES_RESTART_DATA_DIR="/private/PRIVATE_FILESYSTEM_PATH_SENTINEL",
            EPHI_METROLOGY_COMPARABLE_POPULATION_ID="s3://private-bucket/PRIVATE_OBJECT_PATH_SENTINEL",
        )
        report = preflight(values)
        encoded = json.dumps(report, sort_keys=True)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, encoded)
        output = io.StringIO()
        with patch.dict(os.environ, values, clear=True), redirect_stdout(output):
            main(["--json"])
        for sentinel in sentinels:
            self.assertNotIn(sentinel, output.getvalue())
        for artifact in (
            ROOT / CONFIGURATION_CONTRACT_PATH,
            ROOT / "src/ephi/release_inventory.json",
        ):
            content = artifact.read_text(encoding="utf-8")
            for sentinel in sentinels:
                self.assertNotIn(sentinel, content)

    def test_entrypoint_presence_only_is_reported_and_unrelated_variables_are_ignored(self):
        one = preflight(_qa_values(UNRELATED_SETTING="UNRELATED_SENTINEL"))
        two = preflight(dict(reversed(list(_qa_values().items()))))
        self.assertEqual(one, two)
        self.assertEqual(one["checks"]["downstream_entrypoint"], {"configured": True, "shape": "VALID"})
        self.assertEqual(one["checks"]["external_connections"], "NOT_ATTEMPTED")


if __name__ == "__main__":
    unittest.main()
