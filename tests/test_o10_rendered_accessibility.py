"""Dependency-light contracts for the CHG-156 rendered qualification seam."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application import AccessScope, EpisodeBrief, Principal, RevisionVector
from ephi.application.errors import (
    AuthorizationDeniedError,
    CoherentReadConflictError,
    QuerySnapshotExpiredError,
    StorageFailureError,
    VersionConflictError,
)
from ephi.ui.app import (
    _attention_result_status,
    _display_value,
    _episode_action,
    _state_for_error,
)
from tools.o10_rendered_accessibility import _contrast, _parse_color


class O10RenderedAccessibilityTests(unittest.TestCase):
    def test_attention_statuses_are_truthful_and_distinguish_search_empty(self) -> None:
        self.assertIn("not yet known", _attention_result_status(total_count=None, loading=True))
        self.assertIn("not a zero-risk", _attention_result_status(total_count=0, search="no-match"))
        self.assertIn("not treated as zero risk", _attention_result_status(total_count=0))
        self.assertIn("permitted Attention row", _attention_result_status(total_count=1))

    def test_preview_values_do_not_turn_unknown_into_healthy_zero(self) -> None:
        self.assertIn("Unavailable", _display_value(None))
        self.assertIn("source: READY", _display_value({"source": "READY"}))

    def test_episode_action_is_one_durable_primary_action(self) -> None:
        vector = RevisionVector("analysis-1", None, None, 0, None, "manifest-1")
        principal = Principal("engineer-1", ("ephi.episode.claim", "ephi.episode.acknowledge"), (AccessScope("scope-1"),), 1, 1)
        open_brief = EpisodeBrief("episode-1", "read-1", "now", "now", {"title": "Synthetic"}, {"work_state": "OPEN"}, vector, {"source": "READY"})
        claimed_brief = EpisodeBrief("episode-1", "read-1", "now", "now", {"title": "Synthetic"}, {"work_state": "CLAIMED", "owner": "engineer-1"}, vector, {"source": "READY"})
        other_owner = EpisodeBrief("episode-1", "read-1", "now", "now", {"title": "Synthetic"}, {"work_state": "CLAIMED", "owner": "engineer-2"}, vector, {"source": "READY"})
        self.assertEqual(_episode_action(open_brief, principal), ("Claim episode", "ClaimEpisode"))
        self.assertEqual(_episode_action(claimed_brief, principal), ("Acknowledge episode", "AcknowledgeEpisode"))
        self.assertIsNone(_episode_action(other_owner, principal))

    def test_degraded_error_mapping_is_bounded_and_distinct(self) -> None:
        self.assertEqual(_state_for_error(AuthorizationDeniedError("secret" )).title, "Permission denied")
        self.assertEqual(_state_for_error(StorageFailureError("dsn" )).title, "Source unavailable")
        self.assertEqual(_state_for_error(QuerySnapshotExpiredError()).title, "Attention snapshot expired")
        self.assertEqual(_state_for_error(CoherentReadConflictError("raw" )).title, "Stale decision read")
        self.assertEqual(_state_for_error(VersionConflictError("episode-1", 1, 2)).title, "Version conflict")
        self.assertNotIn("dsn", _state_for_error(StorageFailureError("dsn" )).message or "")

    def test_contrast_math_uses_wcag_relative_luminance(self) -> None:
        self.assertEqual(_parse_color("#fff"), (1.0, 1.0, 1.0, 1.0))
        result = _contrast("rgb(0, 0, 0)", "rgb(255, 255, 255)", 4.5)
        self.assertEqual(result["status"], "PASS")
        self.assertAlmostEqual(result["ratio"], 21.0, places=2)


if __name__ == "__main__":
    unittest.main()
