"""Pinned source authority and fail-closed report checks for U4.1."""

from __future__ import annotations

import unittest

from tools import u4_n1_provider_compatibility as u4


class U4FrozenAuthorityTests(unittest.TestCase):
    def test_frozen_pair_and_claim_boundary_are_exact(self):
        authority, _ = u4._authority()
        releases = u4._release_records(authority)
        self.assertEqual(
            releases["N-1"],
            {
                "integrated_commit": "b19e8b6c5c73793a45ad039ee68f452883ef27a1",
                "integrated_tree": "3cb8562898bbbdbcdc58d2cf5877ee9f77c9ded6",
                "release_identity_sha256": "214d3ed0a612ed19fd583a252ae06989af9dcff3cbfa139a3575c75cd313b949",
                "distribution": "ephi",
                "version": "0.1.0",
            },
        )
        self.assertEqual(
            releases["N"],
            {
                "integrated_commit": "0e073216adbdd717e183575f2a4a813f9fc4c265",
                "integrated_tree": "0e54783c78fe4b4df7b022f7eccff273877f0e9e",
                "release_identity_sha256": "c5fcdc4095ec4e7fc55d58379837db9e028ce25af96c106f55928d3f8576c5ef",
                "distribution": "ephi",
                "version": "0.1.0",
            },
        )
        self.assertTrue(authority["qualification"]["not_a_semantic_release_tag"])
        self.assertTrue(authority["qualification"]["not_a_production_release"])
        self.assertEqual(authority["nicegui_base"]["commit"], "000298562d6bcbf6df304edbd41b98b30fe4bfcf")
        self.assertEqual(authority["nicegui_base"]["repository"], "https://github.com/kimhw8084/nicegui-base.git")
        self.assertEqual(authority["migrations"]["identity_sha256"], "c661c7a41eae8cd2b637778998bee77b8ebdec4a2ef78639d3ed7f69b23b1e8b")
        self.assertEqual(authority["provider_package"]["version"], "1.0.0")
        self.assertEqual(authority["public_downstream_abi"]["abi_version"], "1.0.0")
        self.assertEqual(authority["public_downstream_abi"]["provider_contract_version"], "1.0.0")

    def test_contract_report_requires_every_provider_and_safe_manifest_identity(self):
        authority, _ = u4._authority()
        categories = authority["public_downstream_abi"]["required_categories"]
        report = {
            "status_code": "CONTRACT_PASS",
            "compatibility": {"status": "PASS"},
            "safe_composition_smoke": {"status": "NOT_RUN"},
            "downstream_abi": {
                "safe_manifest_hash": "a" * 64,
                "manifest": {
                    "schema": "org.ephi.downstream.manifest.v1",
                    "abi": {"id": "org.ephi.downstream", "version": "1.0.0"},
                    "required_categories": categories,
                },
            },
            "providers": [
                {
                    "category": category,
                    "status": "COMPATIBLE",
                    "contract": {"version": "1.0.0"},
                }
                for category in categories
            ],
        }
        result = u4._compatibility_facts(report, authority, composition_expected=False)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["safe_manifest_sha256"], "a" * 64)
        report["providers"].pop()
        with self.assertRaises(u4.QualificationFailure) as caught:
            u4._compatibility_facts(report, authority, composition_expected=False)
        self.assertEqual(caught.exception.code, "DOWNSTREAM_PROVIDER_INVENTORY_MISMATCH")

    def test_provider_source_manifest_covers_all_frozen_source_files(self):
        manifest = u4._provider_source_manifest(u4.ROOT, "N-1")
        paths = [item["path"] for item in manifest["files"]]
        self.assertEqual(len(paths), 8)
        self.assertEqual(paths, sorted(paths))
        self.assertIn("examples/synthetic_downstream/README.md", paths)
        self.assertIn("examples/synthetic_downstream/provider.py", paths)
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(len(manifest["manifest_sha256"]), 64)

    def test_composition_and_abi_failures_are_not_downgraded(self):
        authority, _ = u4._authority()
        manifest = {
            "schema": "org.ephi.downstream.manifest.v1",
            "abi": {"id": "org.ephi.downstream", "version": "1.0.0"},
            "required_categories": authority["public_downstream_abi"]["required_categories"],
        }
        report = {
            "status_code": "CONTRACT_PASS",
            "compatibility": {"status": "PASS"},
            "safe_composition_smoke": {"status": "NOT_RUN"},
            "downstream_abi": {"safe_manifest_hash": "b" * 64, "manifest": manifest},
            "providers": [
                {"category": category, "status": "COMPATIBLE", "contract": {"version": "1.0.0"}}
                for category in manifest["required_categories"]
            ],
        }
        with self.assertRaises(u4.QualificationFailure) as caught:
            u4._compatibility_facts(report, authority, composition_expected=True)
        self.assertEqual(caught.exception.code, "DOWNSTREAM_COMPOSITION_FAILED")
        report["safe_composition_smoke"] = {"status": "PASS"}
        report["downstream_abi"]["manifest"]["abi"]["version"] = "9.0.0"
        with self.assertRaises(u4.QualificationFailure) as caught:
            u4._compatibility_facts(report, authority, composition_expected=True)
        self.assertEqual(caught.exception.code, "DOWNSTREAM_ABI_IDENTITY_MISMATCH")


if __name__ == "__main__":
    unittest.main()
