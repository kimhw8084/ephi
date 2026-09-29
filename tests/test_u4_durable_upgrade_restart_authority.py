import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import u4_n1_durable_upgrade_restart as qualification  # noqa: E402


class U4DurableUpgradeAuthorityTests(unittest.TestCase):
    def test_u4_2_reuses_the_frozen_u4_1_release_authority(self):
        authority, release_authority = qualification._authorities()
        self.assertEqual(authority["schema"], "org.ephi.u4-n1-durable-upgrade-restart-authority.v1")
        self.assertEqual(
            authority["qualification"]["release_authority"],
            "environment/u4_n1_provider_compatibility_authority.json",
        )
        self.assertEqual(authority["expected_migration_count"], 11)
        serialized = json.dumps(authority, sort_keys=True)
        for release in release_authority["releases"].values():
            self.assertNotIn(release["integrated_commit"], serialized)
            self.assertNotIn(release["integrated_tree"], serialized)
            self.assertNotIn(release["release_identity_sha256"], serialized)
        self.assertNotIn(release_authority["migrations"]["identity_sha256"], serialized)
        self.assertNotIn(release_authority["provider_package"]["compatibility_wheel_sha256"], serialized)

    def test_fixture_authority_has_the_bounded_workflow_read_artifact_scope(self):
        authority = qualification._read_json(
            qualification.AUTHORITY_PATH,
            "U4_2_AUTHORITY_INVALID",
        )
        fixture = authority["fixture"]
        self.assertEqual(fixture["aggregate_type"], "episode_workflow")
        self.assertEqual(fixture["initial_workflow_state"], {"work_state": "OPEN", "owner": None})
        self.assertEqual(fixture["command"]["command_type"], "ClaimEpisode")
        self.assertEqual(fixture["command"]["expected_workflow_version"], 0)
        self.assertEqual(fixture["read"]["entity_type"], "episode")
        self.assertTrue(fixture["artifact"]["content_utf8"].endswith("\n"))
        self.assertEqual(
            set(authority["retained_identity_tables"]),
            {
                "aggregate_state",
                "command_receipt",
                "audit_event",
                "outbox_event",
                "read_revision",
                "read_head",
                "artifact_catalog",
            },
        )
        self.assertEqual(authority["permitted_operational_differences"], [])


if __name__ == "__main__":
    unittest.main()
