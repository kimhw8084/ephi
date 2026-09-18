"""Command-line entry boundary for the canonical EPHI baseline."""

from __future__ import annotations

import argparse
import json

from .application import create_application
from .config import RuntimeConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EPHI canonical repository baseline")
    parser.add_argument("--self-check", action="store_true", help="print deterministic identity/config checks")
    args = parser.parse_args(argv)
    # The baseline has no server launcher yet; the default command is the
    # deterministic self-check so a fresh checkout has an honest entrypoint.
    del args
    result = create_application(RuntimeConfig.from_environment()).self_check()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

