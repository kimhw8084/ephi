"""Installed NiceGUI Base DataSource contract evidence for CHG-134."""

import asyncio
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from nicegui_base import Comparison, ComparisonOperator, Query, QuerySort, SortDirection, TextMatch  # noqa: E402
except ModuleNotFoundError:  # The ordinary repository suite is intentionally framework-independent.
    NICEGUI_BASE_AVAILABLE = False
else:
    NICEGUI_BASE_AVAILABLE = True

from ephi.application import AccessScope, AttentionPage, AttentionRow, Principal, ValidationFailureError  # noqa: E402

if NICEGUI_BASE_AVAILABLE:
    from ephi.ui.provider import EphiReadDataSource  # noqa: E402


class _ProviderService:
    def __init__(self):
        self.calls = []

    def list_attention(self, principal, scope, **kwargs):
        self.calls.append((principal, scope, kwargs))
        return AttentionPage(
            (
                AttentionRow(
                    "episode-1",
                    {"title": "Case", "priority": "P1", "owner": None, "work_state": "OPEN"},
                    1,
                ),
            ),
            1,
            "snapshot-1",
            None,
            kwargs["filters"],
            kwargs["order"],
        )


@unittest.skipUnless(NICEGUI_BASE_AVAILABLE, "NiceGUI Base venv is not installed; provider conformance is NOT_RUN")
class O3ProviderContractTests(unittest.TestCase):
    def setUp(self):
        self.scope = AccessScope("provider-scope")
        self.principal = Principal("provider-user", ("ephi.attention.read",), (self.scope,), 1, 1)
        self.service = _ProviderService()
        self.source = EphiReadDataSource(self.service, lambda: self.principal, lambda: self.scope)

    def query(self, query):
        return asyncio.run(self.source.query(query))

    def test_schema_capabilities_and_allowlisted_translation(self):
        result = self.query(
            Query(
                filter=Comparison("priority", ComparisonOperator.EQ, "P1"),
                sorts=(QuerySort("episode_id", SortDirection.ASC),),
                projection=("episode_id", "priority"),
                limit=25,
            )
        )
        self.assertEqual(result.rows, ({"episode_id": "episode-1", "priority": "P1"},))
        self.assertTrue(self.source.capabilities.filter_pushdown)
        self.assertEqual(asyncio.run(self.source.schema()).key, "episode_id")
        self.assertEqual(self.service.calls[0][2]["filters"], {"priority": "P1"})

    def test_text_search_is_allowlisted_and_unsupported_requests_fail(self):
        self.query(Query(filter=TextMatch("title", "Case")))
        with self.assertRaises(ValidationFailureError):
            self.query(Query(filter=Comparison("priority", ComparisonOperator.GT, "P1")))
        with self.assertRaises(ValidationFailureError):
            self.query(Query(sorts=(QuerySort("unsupported", SortDirection.ASC),)))
        with self.assertRaises(ValidationFailureError):
            self.query(Query(projection=("unsupported",)))
