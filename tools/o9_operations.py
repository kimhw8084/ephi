#!/usr/bin/env python3
"""Source-checkout compatibility entry point for the installed O9 authority."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import ephi.o9_operations as _authority

CRITICAL_TABLES = _authority.CRITICAL_TABLES
LEGACY_MANIFEST_SCHEMA = _authority.LEGACY_MANIFEST_SCHEMA
MANIFEST_SCHEMA = _authority.MANIFEST_SCHEMA
OperationsFailure = _authority.OperationsFailure
_dsn_with_database = _authority._dsn_with_database
_libpq_parameters = _authority._libpq_parameters
_migration_identity = _authority._migration_identity
_private_libpq_environment = _authority._private_libpq_environment
_run_dump = _authority._run_dump
_run_restore = _authority._run_restore


def create_backup(*args, **kwargs):
    return _authority.create_backup(*args, **kwargs)


def verify_backup(*args, **kwargs):
    return _authority.verify_backup(*args, **kwargs)


def restore_rehearsal(*args, **kwargs):
    return _authority.restore_rehearsal(*args, **kwargs)


def reconcile(*args, **kwargs):
    return _authority.reconcile(*args, **kwargs)


def operations_status(*args, **kwargs):
    return _authority.operations_status(*args, **kwargs)


def main(argv=None):
    return _authority.main(argv)

# The compatibility entry point only adds the source package to the import
# path. The installed command enters through ephi.operations_status directly.


if __name__ == "__main__":
    raise SystemExit(main())
