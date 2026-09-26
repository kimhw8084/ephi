#!/usr/bin/env python3
"""Generate or verify the canonical EPHI runtime configuration contract."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ephi.runtime_configuration_contract import contract_document_bytes, expected_contract_is_current  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Write the generated canonical contract.")
    mode.add_argument("--check", action="store_true", help="Check the committed contract against its authorities.")
    args = parser.parse_args(argv)
    path = SRC / "ephi" / "runtime_configuration_contract.json"
    try:
        rendered = contract_document_bytes()
        if args.write:
            path.write_bytes(rendered)
        elif not expected_contract_is_current(path):
            raise ValueError
    except (OSError, ValueError, TypeError):
        print('{"reason_code":"RUNTIME_CONFIGURATION_CONTRACT_INVALID","status":"FAIL"}')
        return 2
    print('{"status":"WRITTEN"}' if args.write else '{"status":"PASS"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
