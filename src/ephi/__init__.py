"""Canonical EPHI application boundary.

This package is a new repository-native implementation foundation.  It does
not claim identity, byte identity, algorithm equivalence, or historical-test
equivalence with any earlier application artifact.
"""

from .application import ApplicationIdentity, EphiApplication, create_application
from .config import RuntimeConfig

__version__ = "0.1.0"
PYTHON_REQUIRES = ">=3.11,<3.14"
APPLICATION_ID = "ephi"

__all__ = [
    "APPLICATION_ID",
    "ApplicationIdentity",
    "EphiApplication",
    "PYTHON_REQUIRES",
    "RuntimeConfig",
    "__version__",
    "create_application",
]

