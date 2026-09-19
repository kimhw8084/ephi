"""Installed NiceGUI Base DataSource contract evidence for CHG-134."""

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from nicegui_base import Comparison, ComparisonOperator, Query, QuerySort, SortDirection, TextMatch  # noqa: E402
except ModuleNotFoundError:  # The ordinary repository suite is intentionally framework-independent.
    NICEGUI_BASE_AVAILABLE = False
else:
    NICEGUI_BASE_AVAILABLE = True

from ephi.application import (  # noqa: E402
    AccessScope,
    AttentionPage,
    AttentionRow,
    AuthorizationDeniedError,
    EpisodeBrief,
    Principal,
    RevisionVector,
    StorageFailureError,
    ValidationFailureError,
)

if NICEGUI_BASE_AVAILABLE:
    from ephi.ui.provider import EphiReadDataSource  # noqa: E402
    from ephi.ui.app import _DevelopmentIdentityProvider, _stable_command_id  # noqa: E402


class _ProviderService:
    def __init__(self):
        self.calls = []
        self.health_calls = []
        self.health_error = None

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

    def check_source(self, principal, scope):
        self.health_calls.append((principal, scope))
        if self.health_error is not None:
            raise self.health_error


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

    def test_health_requires_the_real_source_probe_and_current_authority(self):
        healthy = asyncio.run(self.source.health())
        self.assertEqual(healthy.status.value, "healthy")
        self.assertEqual(healthy.metadata["source_check"], "passed")
        self.service.health_error = StorageFailureError("source unavailable")
        unavailable = asyncio.run(self.source.health())
        self.assertEqual(unavailable.status.value, "unavailable")
        self.assertFalse(unavailable.healthy)
        self.assertEqual(unavailable.metadata["source_check"], "failed")
        self.assertEqual(len(self.service.health_calls), 2)

    def test_retained_page_continuation_re_resolves_principal(self):
        current = [self.principal]
        current_scope = [self.scope]

        class PagingService(_ProviderService):
            def list_attention(inner_self, principal, scope, **kwargs):
                inner_self.calls.append((principal, scope, kwargs))
                if len(inner_self.calls) == 1:
                    current_scope[0] = AccessScope("provider-scope-revoked")
                    current[0] = Principal("provider-user", (), (current_scope[0],), 2, 2)
                    return AttentionPage(
                        (AttentionRow("episode-1", {"priority": "P1"}, 1),),
                        2,
                        "snapshot-1",
                        "cursor-1",
                        kwargs["filters"],
                        kwargs["order"],
                    )
                raise AuthorizationDeniedError("revoked while loading the retained continuation")

        service = PagingService()
        source = EphiReadDataSource(service, lambda: current[0], lambda: current_scope[0])
        with self.assertRaises(AuthorizationDeniedError):
            self.query_source(source, Query(offset=1, limit=1))
        self.assertEqual(len(service.calls), 2)
        self.assertEqual(service.calls[1][0], current[0])
        self.assertEqual(service.calls[1][1], current_scope[0])

    def test_development_identity_and_command_identity_are_current_and_replayable(self):
        with mock.patch.dict(
            os.environ,
            {
                "EPHI_ENV": "test",
                "EPHI_DEV_SCOPE_ID": "provider-scope",
                "EPHI_DEV_IDENTITY_SUBJECT": "provider-user",
                "EPHI_DEV_IDENTITY_CAPABILITIES": "ephi.attention.read,ephi.episode.claim",
                "EPHI_DEV_AUTH_SESSION_REVISION": "1",
                "EPHI_DEV_SECURITY_REVISION": "10",
            },
            clear=False,
        ):
            provider = _DevelopmentIdentityProvider()
            first = provider.principal()
            os.environ["EPHI_DEV_IDENTITY_CAPABILITIES"] = "ephi.attention.read"
            os.environ["EPHI_DEV_SECURITY_REVISION"] = "11"
            revoked = provider.principal()
        self.assertTrue(first.has_capability("ephi.episode.claim"))
        self.assertFalse(revoked.has_capability("ephi.episode.claim"))
        self.assertEqual(revoked.security_revision, 11)

        now = datetime.now(timezone.utc)
        vector = RevisionVector("analysis-1", None, None, 0, None, "manifest-1")
        brief = EpisodeBrief("episode-1", "read-1", now, now, {}, {"work_state": "OPEN"}, vector, {})
        first_id = _stable_command_id("ClaimEpisode", brief, self.scope, "provider-user")
        replay_id = _stable_command_id("ClaimEpisode", brief, self.scope, "provider-user")
        changed_brief = EpisodeBrief(
            "episode-1",
            "read-1",
            now,
            now,
            {},
            {"work_state": "CLAIMED"},
            RevisionVector("analysis-1", None, None, 1, None, "manifest-1"),
            {},
        )
        changed_id = _stable_command_id(
            "ClaimEpisode",
            changed_brief,
            self.scope,
            "provider-user",
        )
        self.assertEqual(first_id, replay_id)
        self.assertNotEqual(first_id, changed_id)

    @staticmethod
    def query_source(source, query):
        return asyncio.run(source.query(query))
