#!/usr/bin/env python3
"""Dedicated, non-production rendered-state harness for CHG-156 evidence.

This process is started only by the O10 qualification tool. It imports the
production error-to-StateView mapping but is not part of normal EPHI routing or
composition, so degraded-state qualification cannot weaken production policy.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    from nicegui import ui
    from ephi.application.errors import (
        AuthorizationDeniedError,
        CoherentReadConflictError,
        QuerySnapshotExpiredError,
        StorageFailureError,
        VersionConflictError,
    )
    from ephi.ui.app import _O10_UI_CSS, _render_attention_error, _state_for_error

    scenarios = {
        "attention-no-permitted-rows": ("Attention", None),
        "attention-permission": ("Attention", AuthorizationDeniedError("not recorded")),
        "attention-offline": ("Attention", StorageFailureError("not recorded")),
        "attention-stale": ("Attention", QuerySnapshotExpiredError()),
        "attention-failure": ("Attention", RuntimeError("not recorded")),
        "episode-permission": ("Episode", AuthorizationDeniedError("not recorded")),
        "episode-offline": ("Episode", StorageFailureError("not recorded")),
        "episode-coherent-conflict": ("Episode", CoherentReadConflictError("not recorded")),
        "episode-version-conflict": ("Episode", VersionConflictError("synthetic-episode", 2, 3)),
        "episode-owner-changed": ("Episode", AuthorizationDeniedError("not recorded")),
        "episode-capability-states": ("Episode", None),
    }
    if args.scenario not in scenarios:
        raise SystemExit(f"unknown scenario: {args.scenario}")
    surface, error = scenarios[args.scenario]

    ui.add_css(_O10_UI_CSS, shared=True)

    @ui.page("/")
    def state_page() -> None:
        with ui.element("main").props('aria-label="O10 degraded-state harness"'):
            heading = ui.element("h1").classes("ephi-o10-page-heading")
            with heading:
                ui.label(f"{surface} degraded-state qualification")
            ui.label("Synthetic qualification identity; no protected row or secret is rendered.").classes("ephi-o10-truth-note")
            if args.scenario == "attention-no-permitted-rows":
                with ui.element("section").props('role="status" aria-live="polite" aria-label="Attention result state"'):
                    ui.label("No permitted Attention rows")
                    ui.label("This is not a zero-risk result; unavailable data is not represented as an empty success.")
            elif args.scenario == "episode-capability-states":
                from nicegui_base import StatusBadge, StatusIntent
                from ephi.ui.app import _intent_for_capability

                with ui.element("section").props('aria-label="Episode capability state matrix"'):
                    for state in ("READY", "STALE", "PARTIAL", "INSUFFICIENT", "UNAVAILABLE"):
                        StatusBadge(f"source capability: {state}", intent=_intent_for_capability(state))
                        ui.label(f"source capability {state}: {state}").props('role="status" aria-live="polite"')
            elif surface == "Attention":
                _render_attention_error(error)
            elif args.scenario == "episode-owner-changed":
                with ui.element("section").props('role="status" aria-live="polite" aria-label="Episode action availability"'):
                    ui.label("Current owner/authorization changed; action unavailable")
                    ui.label("Workflow truth is authoritative and no Claim/Acknowledge action is offered.")
            else:
                spec = _state_for_error(error)
                with ui.element("section").props('aria-label="Episode degraded state"'):
                    ui.label(spec.title).classes("ephi-o10-live-status").props('role="status" aria-live="polite"')
                    ui.label(spec.message or "No additional detail is available.")
                    if spec.action_label:
                        ui.button(spec.action_label, on_click=lambda: None).props(f'aria-label="{spec.action_label}"')
                    ui.button("Return to Attention", on_click=lambda: None).props('aria-label="Return to Attention"')
            ui.label(f"Scenario: {args.scenario}").props('role="status" aria-live="polite"')

    ui.run(
        host="127.0.0.1",
        port=args.port,
        show=False,
        reload=False,
        title="EPHI O10 state harness",
        storage_secret=os.environ.get("NICEGUI_BASE_STORAGE_SECRET", "o10-harness-secret"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
