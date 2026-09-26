#!/usr/bin/env python3
"""Generate or verify the canonical EPHI release/install inventory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ephi.release_identity import ReleaseFailure, build_release_inventory, canonical_json_bytes, verify_inventory_document  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Write the generated canonical inventory.")
    mode.add_argument("--check", action="store_true", help="Check the committed inventory against its authorities.")
    args = parser.parse_args(argv)
    path = SRC / "ephi" / "release_inventory.json"
    try:
        inventory = build_release_inventory(ROOT)
        rendered = canonical_json_bytes(inventory) + b"\n"
        if args.write:
            path.write_bytes(rendered)
            status = "WRITTEN"
        else:
            committed = json.loads(path.read_bytes())
            verify_inventory_document(committed, path.read_bytes())
            if canonical_json_bytes(committed) + b"\n" != rendered:
                raise ReleaseFailure("RELEASE_INVENTORY_STALE")
            status = "PASS"
        print(json.dumps({
            "status": status,
            "release_identity_sha256": inventory["release_identity_sha256"],
            "migration_identity_sha256": inventory["migrations"]["identity_sha256"],
        }, sort_keys=True, separators=(",", ":")))
        return 0
    except (ReleaseFailure, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "FAIL", "reason_code": "RELEASE_INVENTORY_INVALID"}, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
