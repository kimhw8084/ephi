"""Canonical repository-native EPHI application package."""

from .app import application_identity, self_check
from .config import RuntimeSettings
from .identity import ApplicationIdentity

__version__ = ApplicationIdentity.version

__all__ = [
    "ApplicationIdentity",
    "RuntimeSettings",
    "application_identity",
    "self_check",
]
