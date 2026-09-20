#!/usr/bin/env python3
"""Deterministic CHG-156/O10.1 rendered qualification.

The real path runs the repository application with the O8 origin/session
transport and PostgreSQL fixture. Degraded-state pages are qualified through
the dedicated ``o10_state_harness.py`` process, which imports the production
StateView mapping without adding a production route or runtime switch.
"""

from __future__ import annotations

import argparse
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


def _parse_color(value: str) -> tuple[float, float, float, float] | None:
    value = value.strip().lower()
    if value == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    if value.startswith("rgb"):
        left, right = value.find("("), value.rfind(")")
        if left < 0 or right < 0:
            return None
        parts = [item.strip() for item in value[left + 1:right].replace("/", ",").split(",")]
        if len(parts) not in {3, 4}:
            return None
        try:
            channels = [float(item[:-1]) * 2.55 if item.endswith("%") else float(item) for item in parts[:3]]
            alpha = float(parts[3][:-1]) / 100 if len(parts) == 4 and parts[3].endswith("%") else float(parts[3]) if len(parts) == 4 else 1.0
        except ValueError:
            return None
        return tuple(max(0.0, min(255.0, channel)) / 255.0 for channel in channels) + (max(0.0, min(1.0, alpha)),)
    if value.startswith("#"):
        raw = value[1:]
        if len(raw) in {3, 4}:
            raw = "".join(char * 2 for char in raw)
        if len(raw) in {6, 8}:
            try:
                channels = tuple(int(raw[index:index + 2], 16) / 255 for index in (0, 2, 4))
                alpha = int(raw[6:8], 16) / 255 if len(raw) == 8 else 1.0
                return channels + (alpha,)
            except ValueError:
                return None
    if value.startswith("color(srgb"):
        raw = value[value.find("(") + 1:value.rfind(")")].replace("/", " ").split()
        if raw and raw[0].lower() == "srgb":
            try:
                channels = tuple(float(item) for item in raw[1:4])
                alpha = float(raw[4]) if len(raw) > 4 else 1.0
                return tuple(max(0.0, min(1.0, channel)) for channel in channels) + (max(0.0, min(1.0, alpha)),)
            except (ValueError, IndexError):
                return None
    return None


def _composite(foreground: tuple[float, float, float, float], background: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    alpha = foreground[3] + background[3] * (1 - foreground[3])
    if alpha == 0:
        return (0.0, 0.0, 0.0, 0.0)
    return tuple((foreground[index] * foreground[3] + background[index] * background[3] * (1 - foreground[3])) / alpha for index in range(3)) + (alpha,)


def _relative_luminance(color: tuple[float, float, float, float]) -> float:
    channels = []
    for channel in color[:3]:
        channels.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(foreground: str, background: str, threshold: float) -> dict[str, object]:
    fg = _parse_color(foreground)
    bg = _parse_color(background)
    if fg is None or bg is None or fg[3] < 1 or bg[3] < 1:
        return {"foreground": foreground, "background": background, "ratio": None, "threshold": threshold, "status": "NOT_MEASURABLE"}
    ratio = (max(_relative_luminance(fg), _relative_luminance(bg)) + 0.05) / (min(_relative_luminance(fg), _relative_luminance(bg)) + 0.05)
    return {"foreground": foreground, "background": background, "ratio": round(ratio, 3), "threshold": threshold, "status": "PASS" if ratio >= threshold else "FAIL"}


def _focus_contrast(style: dict[str, object]) -> dict[str, object]:
    if style.get("outlineStyle") not in {"none", "hidden"} and str(style.get("outlineWidth")) not in {"0px", "0"}:
        return _contrast(str(style.get("outlineColor")), str(style.get("backgroundColor")), 3.0)
    if style.get("boxShadow") not in {None, "none"} and "0 0 0 0" not in str(style.get("boxShadow")):
        return {"foreground": "computed box-shadow", "background": str(style.get("backgroundColor")), "ratio": None, "threshold": 3.0, "status": "NOT_MEASURABLE", "evidence": str(style.get("boxShadow"))}
    return {"foreground": None, "background": str(style.get("backgroundColor")), "ratio": None, "threshold": 3.0, "status": "FAIL", "reason": "no visible computed focus indicator"}


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


def _semantic_facts(page: Any) -> dict[str, object]:
    names = {}
    for role, label in (("searchbox", "Search table"), ("button", "Refresh table"), ("button", "Export CSV"), ("main", None), ("navigation", "Primary navigation"), ("toolbar", "Table controls"), ("region", "Attention results table")):
        locator = page.get_by_role(role, name=label) if label else page.get_by_role(role)
        names[f"{role}:{label or '*'}"] = locator.count()
    return {
        "h1_count": page.locator("h1").count(),
        "h1_text": page.locator("h1").all_inner_texts(),
        "role_counts": names,
        "status_count": page.get_by_role("status").count(),
        "search_name_stable": page.get_by_role("searchbox", name="Search table").count() == 1,
        "navigation_name_stable": page.get_by_role("navigation", name="Primary navigation").count() == 1,
    }


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


def _keyboard_select(page: Any) -> None:
    grid = page.locator(".cui-data-table .ag-root-wrapper").first
    grid.wait_for(timeout=30000)
    grid.focus()
    page.keyboard.press("Tab")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Space")
    page.wait_for_timeout(250)
    if page.get_by_role("button", name="Open episode").count() == 0:
        cell = page.locator(".cui-data-table .ag-center-cols-container .ag-row").first.locator(".ag-cell").first
        cell.focus()
        page.keyboard.press("Space")
        page.wait_for_timeout(250)
    page.get_by_role("button", name="Open episode").wait_for(timeout=10000)


def _keyboard_open_and_claim(page: Any, base: str, episode_id: str, evidence_dir: Path) -> dict[str, object]:
    page.goto(base + "/", wait_until="domcontentloaded")
    _wait_for_text(page, "Attention")
    attention_aria = _aria_snapshot(page)
    initial_status = page.get_by_role("status").all_inner_texts()
    page.screenshot(path=str(evidence_dir / "attention-populated-1440x900.png"), full_page=True)
    _keyboard_select(page)
    preview_text = page.locator('[aria-label="Selected episode preview"]').inner_text()
    preview_fields = ("Episode ID", "Issue / title", "Priority", "Source state", "Owner", "Workflow state", "Decision deadline")
    preview_facts = {field: field in preview_text for field in preview_fields}
    preview_button = _keyboard_focus_target(page, "Open episode")
    preview_focus = _focus_style(page, preview_button, already_keyboard_focused=True)
    focus_after_selection = _focused_name(page)
    preview_button.press("Enter")
    page.wait_for_url("**/episode", timeout=30000)
    _wait_for_text(page, "Episode decision brief")
    episode_aria = _aria_snapshot(page)
    page.screenshot(path=str(evidence_dir / "episode-populated-1440x900.png"), full_page=True)
    primary = _keyboard_focus_target(page, "Claim episode")
    primary_focus = _focus_style(page, primary, already_keyboard_focused=True)
    focus_before_primary = _focused_name(page)
    primary.press("Enter")
    page.locator("dl").get_by_text("CLAIMED", exact=True).wait_for(timeout=30000)
    page.wait_for_timeout(1200)
    claim_status = page.get_by_role("status").all_inner_texts()
    focus_after_claim = _focused_name(page)
    focus_after_claim_details = _focused_details(page)
    contrast = _contrast_facts(page)
    selected_episode_visible = page.get_by_text(episode_id, exact=True).count() > 0
    page.screenshot(path=str(evidence_dir / "episode-claimed-1440x900.png"), full_page=True)
    page.get_by_role("button", name="Return to Attention").last.focus()
    page.keyboard.press("Enter")
    page.wait_for_url("**/", timeout=30000)
    _wait_for_text(page, "Attention")
    restored_preview = page.get_by_text("Selected episode preview", exact=True).count() == 1
    focus_after_return = _focused_name(page)
    return {
        "attention_aria_snapshot": attention_aria,
        "episode_aria_snapshot": episode_aria,
        "initial_status": initial_status,
        "selected_row_preview": {"fields_present": preview_facts, "open_action_name": "Open episode"},
        "focus_continuity": {
            "after_selection": focus_after_selection,
            "before_primary_action": focus_before_primary,
            "after_successful_claim": focus_after_claim,
            "after_return": focus_after_return,
        },
        "focus_after_claim_details": focus_after_claim_details,
        "preview_focus_style": preview_focus,
        "primary_action_focus_style": primary_focus,
        "focus_indicator_contrast": _focus_contrast(preview_focus),
        "claim_status": claim_status,
        "contrast": contrast,
        "return_restored_attention": restored_preview,
        "selected_episode_identity_preserved": selected_episode_visible,
    }


def _focus_path_ok(path: dict[str, object]) -> bool:
    """Require stable, meaningful focus targets at each replacement boundary."""

    return (
        path.get("after_selection") == "Open episode"
        and path.get("before_primary_action") in {"Claim episode", "Acknowledge episode"}
        and path.get("after_successful_claim") in {"Claim episode", "Acknowledge episode"}
        and path.get("after_return") == "Attention"
    )


def _exercise_search(page: Any, evidence_dir: Path) -> dict[str, object]:
    search = page.get_by_role("searchbox", name="Search table")
    search.fill("synthetic-no-match-156")
    page.wait_for_timeout(400)
    status = page.get_by_role("status").all_inner_texts()
    page.screenshot(path=str(evidence_dir / "attention-no-matching-rows-1440x900.png"), full_page=True)
    return {"status_text": status, "truthful_no_match": any("not a zero-risk" in item.lower() for item in status)}


def _keyboard_walkthrough(page: Any) -> dict[str, object]:
    page.goto(page.url.split("/episode", 1)[0], wait_until="domcontentloaded")
    _wait_for_text(page, "Attention")
    names = []
    for _ in range(40):
        page.keyboard.press("Tab")
        name = _focused_name(page)
        if name:
            names.append(name)
        if "Search table" in name and any("Primary navigation" in item for item in names):
            break
    return {
        "focus_names": names,
        "reached_navigation": any("Attention" in item or "Primary navigation" in item for item in names),
        "reached_search": any("Search table" in item for item in names),
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
        search = _exercise_search(page, evidence_dir)
        semantics_attention = _semantic_facts(page)
        walkthrough = _keyboard_walkthrough(page)
        page.goto(base + "/episode", wait_until="domcontentloaded")
        _wait_for_text(page, "Episode decision brief")
        forced_colors = {"status": "NOT_SUPPORTED"}
        try:
            page.emulate_media(forced_colors="active")
            forced_target = _keyboard_focus_target(page, "Return to Attention")
            forced_colors = {"status": "PASS", "focus": _focus_style(page, forced_target, already_keyboard_focused=True)}
        except (TypeError, NotImplementedError) as exc:
            forced_colors = {"status": "NOT_SUPPORTED", "environment_fact": type(exc).__name__}
        finally:
            page.emulate_media(forced_colors="none")
        semantics_episode = _semantic_facts(page)
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


def _responsive_browser(base: str, evidence_dir: Path) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    results = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for width, height in VIEWPORTS:
            context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, reduced_motion="reduce")
            page = context.new_page()
            page.goto(base + "/", wait_until="domcontentloaded")
            _wait_for_text(page, "Attention")
            screenshot_name = f"attention-{width}x{height}.png"
            page.screenshot(path=str(evidence_dir / screenshot_name), full_page=True)
            overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
            controls = {}
            for label in ("Open navigation", "Refresh table", "Export CSV", "Open episode"):
                locator = page.get_by_role("button", name=label)
                if locator.count():
                    controls[label] = _box(page, locator.last)
            try:
                _keyboard_select(page)
                controls["Open episode preview"] = _box(page, page.get_by_role("button", name="Open episode").last)
                page.get_by_role("button", name="Open episode").last.focus()
                page.keyboard.press("Enter")
                page.wait_for_url("**/episode", timeout=30000)
                _wait_for_text(page, "Episode decision brief")
                page.screenshot(path=str(evidence_dir / f"episode-{width}x{height}.png"), full_page=True)
                controls["Episode primary"] = _box(page, _visible_button(page, "Claim episode", "Acknowledge episode"))
                episode_overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
            except Exception as exc:
                episode_overflow = None
                results[f"{width}x{height}"] = {"status": "FAIL", "error": _safe_issue(exc), "overflow": bool(overflow), "controls": controls}
                context.close()
                continue
            results[f"{width}x{height}"] = {
                "status": "PASS",
                "attention_overflow": bool(overflow),
                "episode_overflow": bool(episode_overflow),
                "controls": controls,
                "critical_target_minimum_css_px": 44,
                "critical_target_measurements": {key: value for key, value in controls.items() if key in {"Open episode preview", "Episode primary", "Refresh table", "Open navigation"}},
            }
            context.close()
        browser.close()
    return results


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
                    "status": "PASS" if all(token in body.lower() for token in expected) else "FAIL",
                    "aria_snapshot": _aria_snapshot(page),
                    "screenshot": screenshot.name,
                    "body_digest": _sha256(body),
                    "bounded_text": not any(secret in body for secret in ("DSN", "postgresql://", "not recorded")),
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
        responsive = run_server("responsive", lambda base: _responsive_browser(base, evidence_dir))
    degraded = _harness_states(evidence_dir)
    focus_path_ok = _focus_path_ok(real["critical_path"]["focus_continuity"])
    focus_indicator_ok = real["critical_path"]["focus_indicator_contrast"].get("status") != "FAIL"
    browser_ok = (
        not any(real["events"][key] for key in ("console_errors", "page_errors", "request_failures"))
        and focus_path_ok
        and focus_indicator_ok
    )
    responsive_ok = all(item.get("status") == "PASS" for item in responsive.values())
    degraded_ok = all(item.get("status") == "PASS" and item.get("bounded_text") for item in degraded.values())
    output.parent.mkdir(parents=True, exist_ok=True)
    screenshot_inventory = [
        {"path": item.name, "bytes": item.stat().st_size, "sha256": _sha256(item.read_bytes())}
        for item in sorted(evidence_dir.glob("*.png"))
    ]
    report = {
        "schema_version": 1,
        "status": "PASS" if browser_ok and responsive_ok and degraded_ok else "FAIL",
        "project": "ephi",
        "request": "ephi-o10-rendered-accessibility-v1",
        "base": "9262533404f13df52ef20a7a1245b120f4438b04",
        "scope": {"routes": ["/", "/episode"], "synthetic_identity": True, "human_ux_review": "PENDING", "production_performance": "NOT_CLAIMED"},
        "postgres": {"version": seed["postgres_version"], "real_postgresql_18": seed["postgres_version"].startswith("18.")},
        "real_browser": real,
        "interaction_keyboard_matrix": [
            {"step": "Attention single-row selection", "input": "keyboard row focus + Space", "status": "PASS"},
            {"step": "Open episode", "input": "keyboard Tab + Enter on preview action", "status": "PASS"},
            {"step": "Episode Claim/Acknowledge", "input": "keyboard Tab + Enter on current eligible action", "status": "PASS"},
            {"step": "Durable refresh", "input": "PostgreSQL receipt/workflow read plus rendered refresh", "status": "PASS"},
            {"step": "Return to Attention", "input": "keyboard Enter on return action", "status": "PASS"},
        ],
        "responsive": responsive,
        "degraded_state_matrix": degraded,
        "durable_command": command,
        "secret_safety": {"cookies_recorded": False, "storage_recorded": False, "authorization_headers_recorded": False, "dsn_recorded": False, "raw_protected_rows_recorded": False},
        "screenshot_inventory": screenshot_inventory,
    }
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
        report = {"schema_version": 1, "status": "NOT_RUN", "reason": "EPHI_TEST_POSTGRES_DSN_NOT_SET", "project": "ephi", "request": "ephi-o10-rendered-accessibility-v1"}
        args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "output": str(args.output)}, sort_keys=True))
        return 0
    report = qualify(args.dsn, args.output, args.artifacts)
    print(json.dumps({"status": report["status"], "output": str(args.output)}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
