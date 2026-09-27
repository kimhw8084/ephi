"""One installed-aware authority for EPHI's numbered SQL migration resources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import json
import sysconfig
from typing import Any

from ephi.application.operations import migration_schema_identity


class MigrationResourceError(ValueError):
    """Migration resources are absent or cannot be used safely."""


@dataclass(frozen=True, slots=True)
class MigrationResources:
    directory: Path
    paths: tuple[Path, ...]
    identity: dict[str, Any]


def _source_root() -> Path | None:
    package_file = Path(__file__).resolve()
    if len(package_file.parents) < 3:
        return None
    candidate = package_file.parents[2]
    if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "ephi").is_dir():
        return candidate
    return None


def resolve_migration_resources(*, source_root: str | Path | None = None) -> MigrationResources:
    """Resolve and validate the exact source or installed migration set.

    In a repository checkout the committed ``migrations`` directory is the
    authority. In a normal wheel installation, setuptools installs that same
    directory under ``sysconfig``'s data prefix. No result is cached, so a
    missing, unreadable, or changed resource set fails closed at each use.
    """

    verify_release_inventory = source_root is None
    if source_root is not None:
        directory = Path(source_root) / "migrations"
        inventory_path = None
    else:
        checkout = _source_root()
        if checkout is not None:
            directory = checkout / "migrations"
            inventory_path = checkout / "src" / "ephi" / "release_inventory.json"
        else:
            data_root = sysconfig.get_path("data")
            if not data_root:
                raise MigrationResourceError("migration resources are unavailable")
            directory = Path(data_root) / "share" / "ephi" / "migrations"
            inventory_path = Path(__file__).resolve().with_name("release_inventory.json")

    try:
        identity = migration_schema_identity(directory)
        if verify_release_inventory:
            if inventory_path is None:
                raise ValueError("migration inventory is unavailable")
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            expected = inventory.get("migrations") if isinstance(inventory, dict) else None
            if expected != identity:
                raise ValueError("migration resources differ from the installed release identity")
        records = identity["files"]
        if not isinstance(records, list) or not records:
            raise ValueError("migration file set is empty")
        paths: list[Path] = []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                raise ValueError("migration identity record is invalid")
            relative = PurePosixPath(record["path"])
            if relative.parts[:1] != ("migrations",) or len(relative.parts) != 2:
                raise ValueError("migration identity path is invalid")
            path = directory / relative.name
            if not path.is_file() or path.is_symlink():
                raise ValueError("migration resource is unavailable")
            paths.append(path)
        return MigrationResources(directory, tuple(paths), identity)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        if isinstance(exc, MigrationResourceError):
            raise
        raise MigrationResourceError("migration resources are unavailable") from exc
