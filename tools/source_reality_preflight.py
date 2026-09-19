#!/usr/bin/env python3
"""Secret-safe CHG-144/O4.1 real-source binding preflight."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.application.source_reality import preflight_source_reality  # noqa: E402


def main() -> int:
    print(json.dumps(preflight_source_reality(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
