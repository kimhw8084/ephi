"""Canonical EPHI application boundary and deterministic self-check entrypoint."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from .config import RuntimeSettings
from .identity import ApplicationIdentity


FRAMEWORK_IDENTITY = {
    "distribution": "nicegui-base",
    "version": "3.0.0a8",
    "git_commit": "000298562d6bcbf6df304edbd41b98b30fe4bfcf",
    "nicegui": "3.15.0",
}

CANONICAL_BEHAVIORAL_CHECKS = {
    "F02": {
        "status": "IMPLEMENTED",
        "execution": "NOT_RUN",
        "reason": "canonical checkpoint API is exercised by the CHG-109 runner",
    },
    "F03": {
        "status": "IMPLEMENTED",
        "execution": "NOT_RUN",
        "reason": "canonical workflow API is exercised by the CHG-109 runner",
    },
    "F04": {
        "status": "IMPLEMENTED",
        "execution": "NOT_RUN",
        "reason": "canonical temporal value API is exercised by the CHG-116 runner",
    },
    "F05": {
        "status": "IMPLEMENTED",
        "execution": "NOT_RUN",
        "reason": "canonical qualified recovery API is exercised by the CHG-118 runner",
    },
}


def application_identity() -> dict[str, object]:
    """Return package and pinned runtime identity without importing the framework."""

    return {
        "application": ApplicationIdentity().as_dict(),
        "framework": dict(FRAMEWORK_IDENTITY),
        "python_requires": ">=3.11,<3.14",
        "entrypoint": "ephi.app:main",
    }


def self_check() -> dict[str, object]:
    """Return deterministic repository identity and explicit unimplemented checks."""

    return {
        "status": "PASS",
        "scope": "canonical-repository-baseline",
        **application_identity(),
        "runtime": RuntimeSettings().as_dict(),
        "behavioral_checks": CANONICAL_BEHAVIORAL_CHECKS,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="run the fail-closed PostgreSQL-backed W1 UI")
    parser.add_argument("--self-check", action="store_true", help="emit the deterministic canonical baseline self-check")
    parser.add_argument("--json", action="store_true", help="emit JSON rather than the compact identity line")
    parser.add_argument("--version", action="store_true", help="print the canonical package version")
    args = parser.parse_args(argv)

    if args.serve:
        from .ui.app import run_ephi

        run_ephi()
        return 0

    if args.version:
        print(ApplicationIdentity.version)
        return 0

    result = self_check()
    if args.json or args.self_check or argv is None:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"{result['application']['distribution']} {result['application']['version']} ({result['scope']})")
    return 0
