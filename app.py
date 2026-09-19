"""NiceGUI Base application scaffold entrypoint for the EPHI package."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ephi.ui.app import build_page as _build_page
from ephi.ui.app import run_ephi


def build_page() -> None:
    _build_page()


def main() -> None:
    run_ephi()


if __name__ == "__main__":
    main()
