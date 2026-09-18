"""Lazy access to the pinned framework's public authorities.

Importing :mod:`ephi` and running its repository self-check do not require an
installed environment. Framework construction is intentionally deferred until a
future application slice needs it.
"""

from __future__ import annotations


def public_runtime_authorities() -> dict[str, object]:
    """Return only public NiceGUI Base runtime authorities.

    The import is deliberately inside the boundary so offline package tests do
    not bootstrap dependencies. No private integration module is an application
    authority.
    """

    from nicegui_base import ApplicationRuntime, RuntimeConfig

    return {
        "ApplicationRuntime": ApplicationRuntime,
        "RuntimeConfig": RuntimeConfig,
    }
