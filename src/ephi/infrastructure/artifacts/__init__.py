"""Immutable artifact infrastructure adapters.

The filesystem adapter is reference/integration evidence only.  The catalog
and blob ports are deliberately separate so an approved company immutable
object store can replace it without changing scoped application contracts.
"""

from .catalog import PostgreSQLArtifactCatalog, SQLiteArtifactCatalog
from .filesystem import (
    DEFAULT_REFERENCE_MAX_ARTIFACT_SIZE,
    FileArtifactBlobStore,
    FilesystemArtifactBlobStore,
)

__all__ = [
    "DEFAULT_REFERENCE_MAX_ARTIFACT_SIZE",
    "FileArtifactBlobStore",
    "FilesystemArtifactBlobStore",
    "PostgreSQLArtifactCatalog",
    "SQLiteArtifactCatalog",
]
