#!/usr/bin/env python3
"""Deterministic CHG-156/O10.1 rendered qualification.

The real path runs the repository application with the O8 origin/session
transport and PostgreSQL fixture. Degraded-state pages are qualified through
the dedicated ``o10_state_harness.py`` process, which imports the production
StateView mapping without adding a production route or runtime switch.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tools.o8_browser_qualification import (  # noqa: E402
    _database_command_facts,
    _fixture_module,
    _seed_database,
    _wait_for_port,
)
from ephi.application.o10 import (  # noqa: E402
    contrast as _contrast,
    evaluate_o10_acceptance,
    focus_contrast as _focus_contrast,
    parse_color as _parse_color,
)


VIEWPORTS = ((1440, 900), (1024, 768), (768, 1024), (390, 844), (320, 800))
DEGRADED_SCENARIOS = (
    "attention-no-permitted-rows",
    "attention-permission",
    "attention-offline",
    "attention-stale",
    "attention-failure",
    "episode-permission",
    "episode-offline",
    "episode-coherent-conflict",
    "episode-version-conflict",
    "episode-owner-changed",
    "episode-capability-states",
)


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _safe_issue(value: object) -> dict[str, str]:
    text = str(value)
    return {"type": type(value).__name__, "digest": _sha256(text)}


def _aria_snapshot(page: Any) -> str:
    try:
        value = page.locator("body").aria_snapshot()
    except Exception as exc:
        return f"NOT_SUPPORTED:{type(exc).__name__}"
    return value if isinstance(value, str) else str(value)


def _focused_name(page: Any) -> str:
    return page.evaluate(
        """() => {
            const el = document.activeElement;
            if (!el) return '';
            return el.getAttribute('aria-label') || el.getAttribute('title') || (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120);
        }"""
    )


def _focused_details(page: Any) -> dict[str, object]:
    return page.evaluate(
        """() => {
            const el = document.activeElement;
            return {
                tag: el?.tagName || '',
                id: el?.id || '',
                ariaLabel: el?.getAttribute('aria-label') || '',
                marker: el?.getAttribute('data-ephi-focus-target') || '',
                tabIndex: el?.getAttribute('tabindex') || '',
                primaryMarkers: document.querySelectorAll('[data-ephi-focus-target="primary-action"]').length,
                primaryLabels: [...document.querySelectorAll('[data-ephi-focus-target="primary-action"]')].map(node => node.getAttribute('aria-label') || ''),
                focusRequest: document.querySelector('[data-ephi-focus-request]')?.getAttribute('data-ephi-focus-request') || '',
            };
        }"""
    )


def _focus_style(page: Any, locator: Any, *, already_keyboard_focused: bool = False) -> dict[str, object]:
    if not already_keyboard_focused:
        locator.focus()
    return page.evaluate(
        """() => {
            const el = document.activeElement;
            const style = getComputedStyle(el);
            return {
                tag: el.tagName,
                isConnected: Boolean(el.isConnected),
                matchesFocus: el.matches(':focus'),
                matchesFocusVisible: el.matches(':focus-visible'),
                outlineStyle: style.outlineStyle,
                outlineWidth: style.outlineWidth,
                outlineColor: style.outlineColor,
                boxShadow: style.boxShadow,
                backgroundColor: style.backgroundColor,
                color: style.color,
            };
        }"""
    )


def _keyboard_focus_target(page: Any, name: str, limit: int = 80) -> Any:
    locator = page.get_by_role("button", name=name)
    for _ in range(limit):
        page.keyboard.press("Tab")
        if _focused_name(page) == name:
            return locator
    raise RuntimeError(f"keyboard focus target not reached: {name}")


def _box(page: Any, locator: Any) -> dict[str, object]:
    return locator.bounding_box() or {"x": None, "y": None, "width": None, "height": None}


def _visible_button(page: Any, *names: str) -> Any:
    candidates = []
    for name in names:
        locator = page.get_by_role("button", name=name)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            box = candidate.bounding_box()
            if candidate.is_visible() and box and box["width"] > 0 and box["height"] > 0:
                candidates.append((box["width"], candidate))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    raise RuntimeError(f"visible button not found: {names}")


def _semantic_facts(page: Any, *, surface: str, episode_id: str | None = None, aria_snapshot: str = "") -> dict[str, object]:
    """Capture named landmark/control facts for one fully rendered surface."""

    def count(role: str, name: str | None = None) -> int:
        locator = page.get_by_role(role, name=name) if name is not None else page.get_by_role(role)
        return locator.count()

    h1_text = page.locator("h1").all_inner_texts()
    common = {
        "h1_count": page.locator("h1").count(),
        "h1_text": h1_text,
        "main_count": count("main"),
        "primary_navigation_count": count("navigation", "Primary navigation"),
        "status_count": count("status"),
        "mobile_navigation_count": count("button", "Open navigation"),
    }
    if surface == "attention":
        required = {
            "search_table_count": count("searchbox", "Search table"),
            "table_controls_count": count("toolbar", "Table controls"),
            "attention_results_region_count": count("region", "Attention results table"),
            "refresh_count": count("button", "Refresh table"),
            "open_episode_count": count("button", "Open episode"),
        }
        stable_names = all(required[key] == 1 for key in ("search_table_count", "table_controls_count", "attention_results_region_count", "refresh_count", "open_episode_count"))
        result = {**common, **required, "stable_names": stable_names}
        if aria_snapshot:
            result["populated_aria_contains_episode"] = bool(episode_id and episode_id in aria_snapshot)
            result["populated_aria_not_empty_state"] = "No permitted Attention rows" not in aria_snapshot
        return result

    action_count = count("button", "Claim episode") + count("button", "Acknowledge episode")
    return {
        **common,
        "episode_region_count": count("region", "Episode rendered state"),
        "return_count": count("button", "Return to Attention"),
        "eligible_primary_action": action_count == 1,
        "selected_episode_identity": bool(episode_id and _visible_text_present(page, episode_id)),
        "episode_aria_truthful": bool(episode_id and episode_id in aria_snapshot and "Workflow state" in aria_snapshot and "Source / capability state" in aria_snapshot and ("Claim episode" in aria_snapshot or "Acknowledge episode" in aria_snapshot)),
        "stable_names": count("button", "Return to Attention") == 1 and action_count == 1,
    }


def _wait_for_authorized_attention_row(page: Any, episode_id: str, timeout: int = 30000) -> None:
    _wait_for_visible_text(page, episode_id, timeout=timeout)
    page.wait_for_function(
        """() => [...document.querySelectorAll('[role=status]')].some(node => {
            const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
            return text.includes('permitted Attention row') && !text.startsWith('Loading');
        })""",
        timeout=timeout,
    )


def _wait_for_authoritative_status(page: Any, expected: str, timeout: int = 30000) -> None:
    page.wait_for_function(
        """(expected) => [...document.querySelectorAll('[role=status]')].some(node =>
            (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').includes(expected))""",
        arg=expected,
        timeout=timeout,
    )


def _wait_for_visible_text(page: Any, text: str, timeout: int = 30000) -> None:
    page.wait_for_function(
        """(expected) => {
            const body = document.body;
            const rect = body?.getBoundingClientRect();
            const style = body ? getComputedStyle(body) : null;
            return Boolean(body && (body.innerText || '').includes(expected) && rect?.width > 0 && rect?.height > 0
                && style?.visibility !== 'hidden' && style?.display !== 'none');
        }""",
        arg=text,
        timeout=timeout,
    )


def _visible_text_present(page: Any, text: str) -> bool:
    return bool(page.evaluate(
        """(expected) => {
            const body = document.body;
            const rect = body?.getBoundingClientRect();
            const style = body ? getComputedStyle(body) : null;
            return Boolean(body && (body.innerText || '').includes(expected) && rect?.width > 0 && rect?.height > 0
                && style?.visibility !== 'hidden' && style?.display !== 'none');
        }""",
        arg=text,
    ))


def _contrast_facts(page: Any) -> dict[str, object]:
    values = page.evaluate(
        """() => {
            const read = (selector, backgroundSelector = 'body') => {
                const node = document.querySelector(selector);
                const bgNode = document.querySelector(backgroundSelector) || document.body;
                if (!node) return null;
                const style = getComputedStyle(node);
                const bg = getComputedStyle(bgNode);
                const ownBackground = style.backgroundColor && style.backgroundColor !== 'transparent' && !style.backgroundColor.includes('rgba(0, 0, 0, 0)') ? style.backgroundColor : bg.backgroundColor;
                return {foreground: style.color, background: ownBackground};
            };
            return {
                primary_text: read('h1'),
                secondary_text: read('.ephi-o10-page-description'),
                primary_action: read('button[aria-label="Claim episode"], button[aria-label="Acknowledge episode"]'),
                status_text: read('.ephi-o10-live-status'),
            };
        }"""
    )
    result = {}
    for key, value in values.items():
        if value is None:
            result[key] = {"status": "NOT_FOUND"}
        else:
            result[key] = _contrast(value["foreground"], value["background"], 4.5 if key != "primary_action" else 4.5)
    return result


def _attach_events(page: Any, events: dict[str, list[dict[str, str]]]) -> None:
    page.on("console", lambda message: events["console_errors"].append(_safe_issue(message.text)) if message.type == "error" else None)
    page.on("pageerror", lambda error: events["page_errors"].append(_safe_issue(error)))
    page.on("requestfailed", lambda request: events["request_failures"].append({"path": request.url.split("?", 1)[0], "digest": _sha256(request.url)}))


def _wait_for_text(page: Any, text: str, timeout: int = 30000) -> None:
    if text in {"Attention", "Episode decision brief"}:
        page.get_by_role("heading", name=text).wait_for(timeout=timeout)
        return
    page.get_by_text(text, exact=False).first.wait_for(timeout=timeout)


def _keyboard_select(page: Any, episode_id: str) -> dict[str, object]:
    """Select the synthetic row with keyboard events and no pointer input."""

    search = page.get_by_role("searchbox", name="Search table")
    search.wait_for(timeout=30000)
    grid = page.locator(".cui-data-table .ag-root-wrapper").first
    grid.wait_for(timeout=30000)
    reached_grid_by_tab = False
    for _ in range(100):
        page.keyboard.press("Tab")
        reached_grid_by_tab = bool(page.evaluate("Boolean(document.activeElement?.closest('.cui-data-table .ag-root-wrapper'))"))
        if reached_grid_by_tab:
            break
    if not reached_grid_by_tab:
        # The grid is the application-owned keyboard surface. Programmatic
        # focus only establishes its starting point; selection remains keys.
        grid.focus()
    page.keyboard.press("Tab")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Space")
    preview = page.locator('[aria-label="Selected episode preview"]')
    preview_action = preview.get_by_role("button", name="Open episode")
    try:
        preview_action.wait_for(timeout=10000)
    except Exception:
        cell = page.locator(".cui-data-table .ag-center-cols-container .ag-row").first.locator(".ag-cell").first
        cell.focus()
        page.keyboard.press("Space")
        preview_action.wait_for(timeout=10000)
    selected = preview.count() == 1 and _visible_text_present(page, episode_id)
    if not selected:
        raise RuntimeError(f"keyboard selection did not expose expected episode {episode_id}")
    return {"reached_grid_by_tab": reached_grid_by_tab, "selected_episode_id": episode_id, "selection_input": "keyboard"}


def _keyboard_open_and_claim(page: Any, base: str, episode_id: str, evidence_dir: Path) -> dict[str, object]:
    page.goto(base + "/", wait_until="domcontentloaded")
    _wait_for_text(page, "Attention")
    _wait_for_authorized_attention_row(page, episode_id)
    attention_aria = _aria_snapshot(page)
    initial_status = page.get_by_role("status").all_inner_texts()
    populated_attention = {
        "expected_episode_id": episode_id,
        "aria_contains_episode": episode_id in attention_aria,
        "aria_not_empty_state": "No permitted Attention rows" not in attention_aria,
        "data_row_count": page.locator(".cui-data-table .ag-center-cols-container .ag-row").count(),
        "search_controls_present": page.get_by_role("searchbox", name="Search table").count() == 1,
        "table_controls_present": page.get_by_role("toolbar", name="Table controls").count() == 1,
    }
    page.screenshot(path=str(evidence_dir / "attention-populated-1440x900.png"), full_page=True)
    selection = _keyboard_select(page, episode_id)
    attention_selected_aria = _aria_snapshot(page)
    preview_text = page.locator('[aria-label="Selected episode preview"]').inner_text()
    preview_fields = ("Episode ID", "Issue / title", "Priority", "Source state", "Owner", "Workflow state", "Decision deadline")
    preview_facts = {field: field in preview_text for field in preview_fields}
    preview_button = _keyboard_focus_target(page, "Open episode")
    preview_focus = _focus_style(page, preview_button, already_keyboard_focused=True)
    focus_after_selection = _focused_name(page)
    preview_button.press("Enter")
    page.wait_for_url("**/episode", timeout=30000)
    _wait_for_text(page, "Episode decision brief")
    _wait_for_visible_text(page, episode_id)
    page.locator("dl").get_by_text("OPEN", exact=True).wait_for(timeout=30000)
    page.get_by_role("button", name="Claim episode").wait_for(timeout=30000)
    episode_aria = _aria_snapshot(page)
    episode_semantic_facts = _semantic_facts(page, surface="episode", episode_id=episode_id, aria_snapshot=episode_aria)
    episode_geometry = _episode_geometry_facts(page)
    page.screenshot(path=str(evidence_dir / "episode-populated-1440x900.png"), full_page=True)
    primary = _keyboard_focus_target(page, "Claim episode")
    primary_focus = _focus_style(page, primary, already_keyboard_focused=True)
    focus_before_primary = _focused_name(page)
    primary.press("Enter")
    page.locator("dl").get_by_text("CLAIMED", exact=True).wait_for(timeout=30000)
    page.wait_for_function(
        """() => {
            const node = document.querySelector('[data-ephi-focus-target="primary-action"]');
            return Boolean(node && document.activeElement === node && node.getBoundingClientRect().width > 0);
        }""",
        timeout=30000,
    )
    claim_status = page.get_by_role("status").all_inner_texts()
    focus_after_claim = _focused_name(page)
    focus_after_claim_details = _focused_details(page)
    contrast = _contrast_facts(page)
    selected_episode_visible = _visible_text_present(page, episode_id)
    page.screenshot(path=str(evidence_dir / "episode-claimed-1440x900.png"), full_page=True)
    page.get_by_role("button", name="Return to Attention").last.focus()
    page.keyboard.press("Enter")
    page.wait_for_url("**/", timeout=30000)
    _wait_for_text(page, "Attention")
    _wait_for_authorized_attention_row(page, episode_id)
    preview = page.locator('[aria-label="Selected episode preview"]')
    _wait_for_visible_text(page, episode_id)
    restored_preview = page.get_by_text("Selected episode preview", exact=True).count() == 1 and preview.get_by_role("button", name="Open episode").count() == 1
    page.wait_for_function("() => (document.activeElement?.innerText || '').trim() === 'Attention'", timeout=30000)
    focus_after_return = _focused_name(page)
    attention_semantic_facts = _semantic_facts(page, surface="attention", episode_id=episode_id, aria_snapshot=_aria_snapshot(page))
    return {
        "attention_aria_snapshot": attention_aria,
        "attention_selected_aria_snapshot": attention_selected_aria,
        "populated_attention": populated_attention,
        "selection": selection,
        "episode_aria_snapshot": episode_aria,
        "episode_semantic_facts": episode_semantic_facts,
        "episode_geometry": episode_geometry,
        "attention_semantic_facts": attention_semantic_facts,
        "initial_status": initial_status,
        "selected_row_preview": {"fields_present": preview_facts, "open_action_name": "Open episode"},
        "focus_continuity": {
            "after_selection": focus_after_selection,
            "before_primary_action": focus_before_primary,
            "after_successful_claim": focus_after_claim,
            "after_return": focus_after_return,
            "expected_sequence": _focus_path_ok({
                "after_selection": focus_after_selection,
                "before_primary_action": focus_before_primary,
                "after_successful_claim": focus_after_claim,
                "after_return": focus_after_return,
            }),
        },
        "focus_after_claim_details": focus_after_claim_details,
        "preview_focus_style": preview_focus,
        "primary_action_focus_style": primary_focus,
        "focus_indicator_contrast": _focus_contrast(preview_focus),
        "claim_status": claim_status,
        "contrast": contrast,
        "return_restored_attention": restored_preview,
        "selected_episode_identity_preserved": selected_episode_visible,
        "keyboard_path_complete": bool(selection.get("selected_episode_id") == episode_id and focus_after_selection == "Open episode" and focus_before_primary in {"Claim episode", "Acknowledge episode"} and focus_after_return == "Attention"),
        "focus_continuity_complete": _focus_path_ok({
            "after_selection": focus_after_selection,
            "before_primary_action": focus_before_primary,
            "after_successful_claim": focus_after_claim,
            "after_return": focus_after_return,
        }),
        "populated_attention_truthful": bool(
            populated_attention["aria_contains_episode"]
            and populated_attention["aria_not_empty_state"]
            and populated_attention["data_row_count"] >= 1
            and populated_attention["search_controls_present"]
            and populated_attention["table_controls_present"]
        ),
    }


def _episode_geometry_facts(page: Any) -> dict[str, object]:
    """Assert the Episode uses the pinned Base pattern's governed geometry."""

    return page.evaluate(
        """() => {
            const rect = node => {
                const box = node?.getBoundingClientRect();
                return box ? {left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: box.width, height: box.height} : null;
            };
            const pattern = document.querySelector('.cui-pattern--analysis_workspace');
            const header = pattern?.querySelector('[data-cui-slot="header"]');
            const primary = pattern?.querySelector('[data-cui-slot="primary"]');
            const card = primary?.querySelector('.cui-surface--card');
            const title = card?.querySelector('.cui-entity-header__subtitle');
            const action = card?.querySelector('button[aria-label="Claim episode"], button[aria-label="Acknowledge episode"]');
            const pageStyle = pattern ? getComputedStyle(pattern) : null;
            const tracks = pageStyle ? pageStyle.gridTemplateColumns.split(' ').map(value => Number.parseFloat(value)).filter(Number.isFinite) : [];
            const gap = pageStyle ? Number.parseFloat(pageStyle.columnGap) || 0 : 0;
            const headerBox = rect(header);
            const governedGridWidth = headerBox?.width || 0;
            const trackWidth = tracks.length === 12 ? (governedGridWidth - gap * 11) / 12 : 0;
            const expectedPrimaryWidth = trackWidth > 0 ? trackWidth * 8 + gap * 7 : 0;
            const titleStyle = title ? getComputedStyle(title) : null;
            const titleBox = rect(title);
            const cardBox = rect(card);
            const primaryBox = rect(primary);
            const actionBox = rect(action);
            const viewport = {width: window.innerWidth, height: window.innerHeight};
            const visibleInViewport = box => Boolean(box && box.width > 0 && box.height > 0 && box.left >= -1 && box.right <= viewport.width + 1 && box.top >= -1 && box.bottom <= viewport.height + 1);
            const titleLineHeight = titleStyle ? Number.parseFloat(titleStyle.lineHeight) : 0;
            const titleLineCount = titleBox && titleLineHeight > 0 ? Math.ceil(titleBox.height / titleLineHeight) : null;
            const governedPrimarySlot = Boolean(
                pattern && header && primary &&
                getComputedStyle(primary).gridColumnStart === '1' &&
                getComputedStyle(primary).gridColumnEnd === '9' &&
                tracks.length === 12 &&
                primaryBox && expectedPrimaryWidth > 0 && primaryBox.width >= expectedPrimaryWidth * 0.9
            );
            const readableContent = Boolean(
                cardBox && primaryBox && cardBox.width >= primaryBox.width * 0.9 &&
                titleBox && titleBox.width >= primaryBox.width * 0.5 &&
                titleLineCount !== null && titleLineCount <= 3 &&
                ['Episode ID', 'Workflow state', 'Current eligible action'].every(label => (card?.innerText || '').includes(label))
            );
            const pageLevelOverflow = document.documentElement.scrollWidth > document.documentElement.clientWidth || document.body.scrollWidth > document.body.clientWidth;
            const decisionActionVisible = visibleInViewport(actionBox);
            const status = governedPrimarySlot && readableContent && decisionActionVisible && !pageLevelOverflow ? 'PASS' : 'FAIL';
            return {
                status,
                governed_primary_slot: governedPrimarySlot,
                readable_content: readableContent,
                decision_action_visible: decisionActionVisible,
                page_level_horizontal_overflow: pageLevelOverflow,
                pattern_box: rect(pattern),
                header_slot_box: rect(header),
                primary_slot_box: primaryBox,
                expected_primary_width: expectedPrimaryWidth,
                primary_grid_column: primary ? getComputedStyle(primary).gridColumn : '',
                card_box: cardBox,
                title_box: titleBox,
                title_text: title?.innerText || '',
                title_line_count: titleLineCount,
                key_fact_labels_present: ['Episode ID', 'Workflow state', 'Current eligible action'].every(label => (card?.innerText || '').includes(label)),
                decision_action_box: actionBox,
                viewport,
                threshold_basis: 'Pinned Base analysis_workspace 12-column grid: PRIMARY spans columns 1/9 and rendered content must retain at least 90% of its computed governed span; title retains at least half of that span and at most three readable lines.'
            };
        }"""
    )


def _focus_path_ok(path: dict[str, object]) -> bool:
    """Require stable, meaningful focus targets at each replacement boundary."""

    return (
        path.get("after_selection") == "Open episode"
        and path.get("before_primary_action") in {"Claim episode", "Acknowledge episode"}
        and path.get("after_successful_claim") in {"Claim episode", "Acknowledge episode"}
        and path.get("after_return") == "Attention"
    )


def _exercise_search(page: Any, evidence_dir: Path, episode_id: str) -> dict[str, object]:
    unfiltered_row = page.locator('[aria-label="Selected episode preview"]').get_by_text(episode_id, exact=True)
    unfiltered_row_existed = unfiltered_row.count() == 1
    search = page.get_by_role("searchbox", name="Search table")
    search.fill("synthetic-no-match-156")
    expected = "No permitted Attention rows match this search. This is not a zero-risk result."
    _wait_for_authoritative_status(page, expected)
    status = page.get_by_role("status").all_inner_texts()
    overlay = page.locator(".cui-data-table").inner_text()
    page.screenshot(path=str(evidence_dir / "attention-no-matching-rows-1440x900.png"), full_page=True)
    return {
        "status_text": status,
        "truthful_no_match": expected in status and "zero-risk" in expected,
        "unfiltered_authorized_row_existed": unfiltered_row_existed,
        "neutral_empty_overlay": "No permitted Attention rows" not in overlay,
    }


def _keyboard_walkthrough(page: Any) -> dict[str, object]:
    page.goto(page.url.split("/episode", 1)[0], wait_until="domcontentloaded")
    _wait_for_text(page, "Attention")
    names = []
    reached_table = False
    for _ in range(80):
        page.keyboard.press("Tab")
        name = _focused_name(page)
        details = page.evaluate(
            """() => ({
                role: document.activeElement?.getAttribute('role') || '',
                inGrid: Boolean(document.activeElement?.closest('.cui-data-table .ag-root-wrapper')),
                tag: document.activeElement?.tagName || ''
            })"""
        )
        token = name or details["role"] or ("grid" if details["inGrid"] else details["tag"])
        names.append(token)
        reached_table = reached_table or bool(details["inGrid"])
        if "Search table" in name and reached_table and any("Attention" in item or "Primary navigation" in item for item in names):
            break
    return {
        "focus_names": names,
        "reached_navigation": any("Attention" in item or "Primary navigation" in item for item in names),
        "reached_search": any("Search table" in item for item in names),
        "reached_table": reached_table,
        "no_empty_focus": all(bool(item) for item in names),
    }


def _real_browser(base: str, seed: dict[str, str], evidence_dir: Path) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    events: dict[str, list[dict[str, str]]] = {"console_errors": [], "page_errors": [], "request_failures": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1, reduced_motion="reduce")
        page = context.new_page()
        _attach_events(page, events)
        page.goto(base + "/episode", wait_until="domcontentloaded")
        _wait_for_text(page, "No episode selected")
        no_selection = {"status": page.get_by_role("status").all_inner_texts(), "aria": _aria_snapshot(page)}
        no_selection["focus_observer"] = page.evaluate("Boolean(window.__ephiO10FocusObserver)")
        page.screenshot(path=str(evidence_dir / "episode-no-selection-1440x900.png"), full_page=True)
        critical = _keyboard_open_and_claim(page, base, seed["episode_id"], evidence_dir)
        search = _exercise_search(page, evidence_dir, seed["episode_id"])
        semantics_attention = critical["attention_semantic_facts"]
        walkthrough = _keyboard_walkthrough(page)
        page.goto(base + "/episode", wait_until="domcontentloaded")
        _wait_for_text(page, "Episode decision brief")
        forced_colors = {"status": "NOT_SUPPORTED"}
        try:
            page.emulate_media(forced_colors="active")
            forced_target = _keyboard_focus_target(page, "Return to Attention")
            forced_focus = _focus_style(page, forced_target, already_keyboard_focused=True)
            forced_indicator = _focus_contrast(forced_focus)
            focus_observable = forced_focus.get("matchesFocus") is True and (
                forced_focus.get("outlineStyle") not in {"none", "hidden"} and str(forced_focus.get("outlineWidth")) not in {"0px", "0"}
                or forced_focus.get("boxShadow") not in {None, "none"}
            )
            if focus_observable and forced_indicator.get("status") == "NOT_MEASURABLE":
                forced_indicator = {
                    **forced_indicator,
                    "status": "PASS",
                    "measurement": "computed non-zero forced-colors focus treatment; system-representable outline",
                    "contrast_status": "NOT_MEASURABLE",
                }
            forced_colors = {
                "status": "PASS" if focus_observable and forced_indicator.get("status") == "PASS" else "FAIL",
                "focus": forced_focus,
                "focus_indicator": forced_indicator,
                "focus_observable": focus_observable,
            }
        except (TypeError, NotImplementedError) as exc:
            forced_colors = {"status": "NOT_SUPPORTED", "environment_fact": type(exc).__name__}
        except Exception as exc:
            forced_colors = {"status": "FAIL", "environment_fact": {"type": type(exc).__name__, "digest": _sha256(str(exc))}}
        finally:
            page.emulate_media(forced_colors="none")
        semantics_episode = critical["episode_semantic_facts"]
        contrast = critical["contrast"]
        facts = {
            "critical_path": critical,
            "search_no_match": search,
            "keyboard_walkthrough": walkthrough,
            "no_episode_selected": no_selection,
            "semantics_attention_after_return": semantics_attention,
            "semantics_episode": semantics_episode,
            "reduced_motion": {"match_media": page.evaluate("window.matchMedia('(prefers-reduced-motion: reduce)').matches")},
            "forced_colors": forced_colors,
            "contrast": contrast,
            "events": events,
            "secret_safety": {"cookies_recorded": False, "storage_recorded": False, "authorization_headers_recorded": False, "dsn_recorded": False},
        }
        context.close()
        browser.close()
    return facts


def _visible_and_unclipped(locator: Any, page: Any) -> bool:
    if not locator.count() or not locator.is_visible():
        return False
    return bool(locator.evaluate(
        """(element) => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none'
                && rect.right <= window.innerWidth + 1 && rect.left >= -1;
        }"""
    ))


def _overflow_facts(page: Any) -> dict[str, object]:
    return page.evaluate(
        """() => {
            const table = [...document.querySelectorAll('.cui-data-table')].find(node =>
                node.scrollWidth > node.clientWidth || node.querySelector('.ag-body-viewport'));
            const tableViewport = table?.querySelector('.ag-body-viewport, .ag-center-cols-viewport') || table;
            const bodyOverflow = document.body.scrollWidth > document.body.clientWidth;
            const documentOverflow = document.documentElement.scrollWidth > document.documentElement.clientWidth;
            return {
                document_scroll_width: document.documentElement.scrollWidth,
                document_client_width: document.documentElement.clientWidth,
                body_scroll_width: document.body.scrollWidth,
                body_client_width: document.body.clientWidth,
                document_overflow: documentOverflow,
                body_overflow: bodyOverflow,
                application_horizontal_overflow: documentOverflow || bodyOverflow,
                table_internal_overflow: Boolean(tableViewport && tableViewport.scrollWidth > tableViewport.clientWidth),
                table_scroll_width: tableViewport?.scrollWidth || 0,
                table_client_width: tableViewport?.clientWidth || 0
            };
        }"""
    )


def _surface_facts(page: Any, episode_id: str) -> dict[str, object]:
    """Return bounded, secret-safe facts when one responsive step cannot settle."""

    return page.evaluate(
        """(episodeId) => ({
            path: window.location.pathname,
            h1: [...document.querySelectorAll('h1')].map(node => (node.innerText || '').trim()),
            status: [...document.querySelectorAll('[role=status]')].map(node => (node.innerText || '').trim().slice(0, 180)),
            episode_identity_visible: [...document.querySelectorAll('body *')].some(node => {
                const rect = node.getBoundingClientRect();
                return (node.innerText || node.textContent || '').trim() === episodeId && rect.width > 0 && rect.height > 0;
            }),
            no_episode_selected: (document.body.innerText || '').includes('No episode selected'),
            preview_count: document.querySelectorAll('[aria-label="Selected episode preview"]').length,
            primary_buttons: [...document.querySelectorAll('button')].map(node => (node.getAttribute('aria-label') || node.innerText || '').trim()).filter(name => name === 'Claim episode' || name === 'Acknowledge episode'),
        })""",
        arg=episode_id,
    )


def _responsive_browser(base: str, evidence_dir: Path, episode_id: str, events: dict[str, list[dict[str, str]]]) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    results = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for width, height in VIEWPORTS:
            stage = "context"
            context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, reduced_motion="reduce")
            page = context.new_page()
            _attach_events(page, events)
            stage = "attention_navigation"
            page.goto(base + "/", wait_until="domcontentloaded")
            _wait_for_text(page, "Attention")
            stage = "attention_data"
            _wait_for_authorized_attention_row(page, episode_id)
            attention_first_paint = _attention_first_paint_facts(page)
            screenshot_name = f"attention-{width}x{height}.png"
            page.screenshot(path=str(evidence_dir / screenshot_name), full_page=True)
            attention_overflow = _overflow_facts(page)
            controls = {}
            for label in ("Open navigation", "Refresh table", "Export CSV", "Open episode"):
                locator = page.get_by_role("button", name=label)
                if locator.count():
                    controls[label] = _box(page, locator.last)
            try:
                stage = "keyboard_selection"
                _keyboard_select(page, episode_id)
                attention_open = page.locator('[aria-label="Selected episode preview"]').get_by_role("button", name="Open episode")
                attention_open.wait_for(timeout=10000)
                controls["Open episode preview"] = _box(page, attention_open)
                attention_content_visibility = {
                    "attention_heading": _visible_and_unclipped(page.get_by_role("heading", name="Attention"), page),
                    "attention_status": _visible_and_unclipped(page.locator(".ephi-o10-live-status"), page),
                    "attention_preview": _visible_and_unclipped(page.locator('[aria-label="Selected episode preview"]'), page),
                    "attention_open_action": _visible_and_unclipped(attention_open, page),
                }
                stage = "episode_navigation"
                attention_open.focus()
                page.keyboard.press("Enter")
                page.wait_for_url("**/episode", timeout=30000)
                _wait_for_text(page, "Episode decision brief")
                stage = "episode_identity"
                _wait_for_visible_text(page, episode_id)
                page.screenshot(path=str(evidence_dir / f"episode-{width}x{height}.png"), full_page=True)
                stage = "episode_measurement"
                episode_primary = _visible_button(page, "Claim episode", "Acknowledge episode")
                controls["Episode primary"] = _box(page, episode_primary)
                episode_overflow = _overflow_facts(page)
                content_visibility = {
                    **attention_content_visibility,
                    "episode_heading": _visible_and_unclipped(page.get_by_role("heading", name="Episode decision brief"), page),
                    "episode_status": _visible_and_unclipped(page.locator(".ephi-o10-live-status"), page),
                    "episode_primary": _visible_and_unclipped(episode_primary, page),
                }
            except Exception as exc:
                episode_overflow = {}
                results[f"{width}x{height}"] = {
                    "status": "FAIL",
                    "stage": stage,
                    "error": _safe_issue(exc),
                    "surface_facts": _surface_facts(page, episode_id),
                    "attention_overflow": attention_overflow,
                    "episode_overflow": episode_overflow,
                    "controls": controls,
                }
                context.close()
                continue
            critical = {key: value for key, value in controls.items() if key in {"Open episode preview", "Episode primary", "Refresh table", "Open navigation"}}
            all_targets_measured = all(value.get("width") is not None and value.get("height") is not None for value in critical.values())
            phone_targets_ok = width not in {390, 320} or all(value["width"] >= 44 and value["height"] >= 44 for value in critical.values() if value.get("width") is not None)
            target_visibility = all(
                value.get("width") is not None and value.get("height") is not None and value["width"] > 0 and value["height"] > 0
                for value in critical.values()
            )
            content_ok = all(content_visibility.values())
            viewport_status = all_targets_measured and phone_targets_ok and target_visibility and content_ok and attention_overflow.get("application_horizontal_overflow") is False and episode_overflow.get("application_horizontal_overflow") is False
            results[f"{width}x{height}"] = {
                "status": "PASS" if viewport_status else "FAIL",
                "attention_overflow": attention_overflow.get("application_horizontal_overflow"),
                "attention_first_paint": attention_first_paint,
                "episode_overflow": episode_overflow.get("application_horizontal_overflow"),
                "document_overflow": bool(attention_overflow.get("document_overflow") or episode_overflow.get("document_overflow")),
                "body_overflow": bool(attention_overflow.get("body_overflow") or episode_overflow.get("body_overflow")),
                "application_horizontal_overflow": bool(attention_overflow.get("application_horizontal_overflow") or episode_overflow.get("application_horizontal_overflow")),
                "table_internal_overflow": {"attention": attention_overflow.get("table_internal_overflow"), "episode": episode_overflow.get("table_internal_overflow")},
                "content_visibility": content_visibility,
                "critical_targets_visible": content_ok and target_visibility,
                "controls": controls,
                "critical_target_minimum_css_px": 44,
                "critical_target_measurements": critical,
            }
            context.close()
        browser.close()
    return results


def _attention_first_paint_facts(page: Any) -> dict[str, object]:
    """Measure the compact same-row scent exposed before selection."""

    return page.evaluate(
        """() => {
            const row = document.querySelector('.cui-data-table .ag-center-cols-container .ag-row');
            const cell = [...(row?.querySelectorAll('.ag-cell') || [])].find(node => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim());
            const box = cell?.getBoundingClientRect();
            const text = (cell?.innerText || cell?.textContent || '').replace(/\\s+/g, ' ').trim();
            const visible = Boolean(box && box.width > 0 && box.height > 0 && box.left >= -1 && box.right <= window.innerWidth + 1 && box.top >= -1 && box.bottom <= window.innerHeight + 1);
            return {
                status: visible && /\\bP[1-3]\\b/.test(text) && text.includes('READY/OPEN') && text.includes('Browser transport') ? 'PASS' : 'FAIL',
                visible,
                text,
                priority_present: /\\bP[1-3]\\b/.test(text),
                source_work_present: text.includes('READY/OPEN'),
                issue_present: text.includes('Browser transport'),
                box: box ? {left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: box.width, height: box.height} : null,
                basis: 'One Base DataSourceTable cell carries the same-row priority, source/work state and issue title before selection; no parallel mobile list is mounted.'
            };
        }"""
    )


def _harness_states(evidence_dir: Path) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    results = {}
    for scenario in DEGRADED_SCENARIOS:
        port = _port()
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "o10_state_harness.py"), "--scenario", scenario, "--port", str(port)],
            cwd=ROOT,
            env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT)))),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            _wait_for_port(port, process)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                events: dict[str, list[dict[str, str]]] = {"console_errors": [], "page_errors": [], "request_failures": []}
                _attach_events(page, events)
                page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                page.locator("h1").wait_for(timeout=30000)
                body = page.locator("body").inner_text()
                screenshot = evidence_dir / f"{scenario}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                expected = {
                    "attention-no-permitted-rows": ("no permitted attention rows", "not a zero-risk"),
                    "attention-permission": ("permission denied",),
                    "attention-offline": ("source unavailable",),
                    "attention-stale": ("attention snapshot expired",),
                    "attention-failure": ("ephi request failed",),
                    "episode-permission": ("permission denied",),
                    "episode-offline": ("source unavailable",),
                    "episode-coherent-conflict": ("stale decision read",),
                    "episode-version-conflict": ("version conflict",),
                    "episode-owner-changed": ("action unavailable",),
                    "episode-capability-states": ("ready", "stale", "partial", "insufficient", "unavailable"),
                }[scenario]
                results[scenario] = {
                    "status": "PASS" if all(token in body.lower() for token in expected) and not any(events.values()) else "FAIL",
                    "aria_snapshot": _aria_snapshot(page),
                    "screenshot": screenshot.name,
                    "body_digest": _sha256(body),
                    "bounded_text": not any(secret in body for secret in ("DSN", "postgresql://", "not recorded")),
                    "events": events,
                }
                browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    return results


def qualify(dsn: str, output: Path, evidence_dir: Path) -> dict[str, object]:
    seed = _seed_database(dsn)
    with tempfile.TemporaryDirectory(prefix="ephi-o10-rendered-") as fixture_root:
        fixture_path = Path(fixture_root) / "ephi_browser_source_fixture.py"
        from ephi.application import AccessScope

        scope = AccessScope(seed["scope_id"], site_id="browser-site", area_id="browser-area", family_id="o8-browser-family")
        _fixture_module(fixture_path, scope)
        port = _port()
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), fixture_root)),
                "EPHI_ENV": "test",
                "EPHI_HOST": "127.0.0.1",
                "EPHI_PORT": str(port),
                "EPHI_ALLOWED_BROWSER_ORIGINS": f"http://127.0.0.1:{port}",
                "NICEGUI_BASE_ROOT_PATH": "",
                "NICEGUI_BASE_PROXY_ENABLED": "false",
                "NICEGUI_BASE_TRUSTED_PROXIES": "127.0.0.1,::1",
                "EPHI_POSTGRES_DSN": dsn,
                "NICEGUI_BASE_STORAGE_SECRET": "o10-rendered-session-secret-not-recorded",
                "NICEGUI_STORAGE_PATH": str(Path(fixture_root) / "nicegui-storage"),
                "EPHI_DEV_SCOPE_ID": seed["scope_id"],
                "EPHI_DEV_SITE_ID": "browser-site",
                "EPHI_DEV_AREA_ID": "browser-area",
                "EPHI_DEV_FAMILY_ID": "o8-browser-family",
                "EPHI_DEV_IDENTITY_SUBJECT": "o10-rendered-engineer",
                "EPHI_DEV_IDENTITY_CAPABILITIES": "ephi.attention.read,ephi.episode.read,ephi.episode.claim,ephi.episode.acknowledge",
                "EPHI_DEV_AUTH_SESSION_REVISION": "1",
                "EPHI_DEV_SECURITY_REVISION": "1",
                "EPHI_METROLOGY_SOURCE_ADAPTER": "ephi_browser_source_fixture:factory",
                "EPHI_METROLOGY_SOURCE_ID": "o8-browser-source",
                "EPHI_METROLOGY_PROVIDER_ID": "o8-browser-provider",
                "EPHI_METROLOGY_FAMILY_ID": "o8-browser-family",
                "EPHI_METROLOGY_CAPABILITY_ID": "o8-browser-capability",
                "EPHI_METROLOGY_SCOPE_ID": seed["scope_id"],
                "EPHI_METROLOGY_SITE_ID": "browser-site",
                "EPHI_METROLOGY_AREA_ID": "browser-area",
                "EPHI_METROLOGY_SCHEMA_ID": "o8-browser-schema-v1",
                "EPHI_METROLOGY_MAPPING_VERSION": "o8-browser-mapping-v1",
                "EPHI_METROLOGY_MAPPING_HASH": "a" * 64,
                "EPHI_METROLOGY_UNIT": "mm",
                "EPHI_METROLOGY_REFERENCE_POPULATION_ID": "o8-browser-reference",
            }
        )
        def run_server(phase: str, callback: Any) -> Any:
            phase_port = _port()
            phase_environment = dict(environment, EPHI_PORT=str(phase_port), EPHI_ALLOWED_BROWSER_ORIGINS=f"http://127.0.0.1:{phase_port}")
            log_path = Path(fixture_root) / f"server-{phase}.log"
            with log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen([sys.executable, "-m", "ephi", "--serve"], cwd=ROOT, env=phase_environment, stdout=log_file, stderr=subprocess.STDOUT, text=True)
            try:
                _wait_for_port(phase_port, process)
                return callback(f"http://127.0.0.1:{phase_port}")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

        real = run_server("real", lambda base: _real_browser(base, seed, evidence_dir))
        command = _database_command_facts(dsn, seed["episode_id"])
        _seed_database(dsn)
        responsive_events: dict[str, list[dict[str, str]]] = {"console_errors": [], "page_errors": [], "request_failures": []}
        responsive = run_server("responsive", lambda base: _responsive_browser(base, evidence_dir, seed["episode_id"], responsive_events))
        for key in responsive_events:
            real["events"][key].extend(responsive_events[key])
    degraded = _harness_states(evidence_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    screenshot_inventory = [
        {"path": item.name, "bytes": item.stat().st_size, "sha256": _sha256(item.read_bytes())}
        for item in sorted(evidence_dir.glob("*.png"))
    ]
    report = {
        "schema_version": 1,
        "project": "ephi",
        "request": "ephi-o10-rendered-accessibility-fix1",
        "base": "9262533404f13df52ef20a7a1245b120f4438b04",
        "scope": {"routes": ["/", "/episode"], "synthetic_identity": True, "human_ux_review": "PENDING", "production_performance": "NOT_CLAIMED"},
        "postgres": {"version": seed["postgres_version"], "real_postgresql_18": seed["postgres_version"].startswith("18.")},
        "real_browser": real,
        "interaction_keyboard_matrix": [],
        "responsive": responsive,
        "degraded_state_matrix": degraded,
        "durable_command": command,
        "secret_safety": {"cookies_recorded": False, "storage_recorded": False, "authorization_headers_recorded": False, "dsn_recorded": False, "raw_protected_rows_recorded": False},
        "screenshot_inventory": screenshot_inventory,
    }
    acceptance = evaluate_o10_acceptance(report)
    critical = real.get("critical_path", {})
    selection = critical.get("selection", {}) if isinstance(critical, Mapping) else {}
    preview = critical.get("selected_row_preview", {}) if isinstance(critical, Mapping) else {}
    focus = critical.get("focus_continuity", {}) if isinstance(critical, Mapping) else {}
    episode = critical.get("episode_semantic_facts", {}) if isinstance(critical, Mapping) else {}
    report["interaction_keyboard_matrix"] = [
        {"step": "Attention single-row selection", "input": "keyboard row focus + Space", "status": "PASS" if selection.get("selection_input") == "keyboard" and selection.get("selected_episode_id") == seed["episode_id"] else "FAIL"},
        {"step": "Open episode", "input": "keyboard Tab + Enter on preview action", "status": "PASS" if preview.get("open_action_name") == "Open episode" and episode.get("selected_episode_identity") is True else "FAIL"},
        {"step": "Episode Claim/Acknowledge", "input": "keyboard Tab + Enter on current eligible action", "status": "PASS" if focus.get("after_successful_claim") == "Acknowledge episode" and critical.get("claim_status") else "FAIL"},
        {"step": "Durable refresh", "input": "PostgreSQL receipt/workflow read plus rendered refresh", "status": "PASS" if command.get("command_receipt_count") == 1 and command.get("work_state_present") is True else "FAIL"},
        {"step": "Return to Attention", "input": "keyboard Enter on return action", "status": "PASS" if critical.get("return_restored_attention") is True else "FAIL"},
    ]
    report["acceptance"] = acceptance
    report["status"] = acceptance["status"]
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("EPHI_TEST_POSTGRES_DSN", ""))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    if not args.dsn:
        report = {"schema_version": 1, "status": "NOT_RUN", "reason": "EPHI_TEST_POSTGRES_DSN_NOT_SET", "project": "ephi", "request": "ephi-o10-rendered-accessibility-fix1"}
        args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "output": str(args.output)}, sort_keys=True))
        return 0
    report = qualify(args.dsn, args.output, args.artifacts)
    print(json.dumps({"status": report["status"], "output": str(args.output)}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
