"""Dependency-light O10 presentation and acceptance contracts.

This module contains only renderer-independent wording, qualification helpers,
WCAG math, and the fail-closed acceptance predicate used by the browser lane.
It deliberately does not import NiceGUI Base or Playwright.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .context import Principal
from .episodes import EpisodeBrief
from .errors import (
    AuthorizationDeniedError,
    CoherentReadConflictError,
    QuerySnapshotExpiredError,
    ScopeDeniedError,
    StorageFailureError,
    VersionConflictError,
)


@dataclass(frozen=True, slots=True)
class O10State:
    """Safe state facts which a renderer can convert to its own state view."""

    kind: str
    title: str
    message: str
    action_label: str | None = None


def display_value(value: object, *, unavailable: str = "Unavailable / not yet qualified") -> str:
    """Format a domain value without implying health from missing data."""

    if value is None or value == "":
        return unavailable
    if isinstance(value, Mapping):
        return ", ".join(f"{key}: {display_value(item)}" for key, item in sorted(value.items()))
    return str(value)


def attention_result_status(*, total_count: int | None, search: str = "", loading: bool = False) -> str:
    """Describe the current authorized Attention result without overclaiming."""

    if loading:
        return "Loading authorized Attention rows; source coverage is not yet known."
    if total_count == 0 and search.strip():
        return "No permitted Attention rows match this search. This is not a zero-risk result."
    if total_count == 0:
        return "No permitted Attention rows are available in this authorized view; unavailable data is not treated as zero risk."
    if total_count is None:
        return "Attention results are available only after the authorized query completes."
    noun = "row" if total_count == 1 else "rows"
    return f"{total_count} permitted Attention {noun}; source coverage is limited to this authorized result."


def episode_action(brief: EpisodeBrief, principal: Principal) -> tuple[str, str] | None:
    """Return the one eligible workflow action from already-authorized facts."""

    work_state = brief.workflow.get("work_state")
    owner = brief.workflow.get("owner")
    if work_state == "OPEN" and "ephi.episode.claim" in principal.capabilities:
        return ("Claim episode", "ClaimEpisode")
    if work_state == "CLAIMED" and owner == principal.subject and "ephi.episode.acknowledge" in principal.capabilities:
        return ("Acknowledge episode", "AcknowledgeEpisode")
    return None


def state_for_error(error: BaseException) -> O10State:
    """Classify typed failures into bounded renderer-independent state facts."""

    if isinstance(error, (AuthorizationDeniedError, ScopeDeniedError)):
        return O10State("permission", "Permission denied", "Current authorization does not permit this data or action.")
    if isinstance(error, QuerySnapshotExpiredError):
        return O10State("error", "Attention snapshot expired", "Refresh Attention to start a new retained query snapshot.", "Refresh")
    if isinstance(error, VersionConflictError):
        return O10State("error", "Version conflict", "The workflow changed in another session. Refresh the Episode before retrying.", "Refresh")
    if isinstance(error, CoherentReadConflictError):
        return O10State("error", "Stale decision read", "The analytical revision and workflow state were not one coherent read.", "Refresh")
    if isinstance(error, StorageFailureError):
        return O10State("offline", "Source unavailable", "PostgreSQL or the required EPHI source is unavailable. No healthy or empty state is inferred.", "Retry")
    return O10State("error", "EPHI request failed", "The operation did not commit. Retry only after reviewing the current state.")


def parse_color(value: str) -> tuple[float, float, float, float] | None:
    """Parse the CSS color forms emitted by Chromium computed styles."""

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


def _relative_luminance(color: tuple[float, float, float, float]) -> float:
    channels = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in color[:3]]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(foreground: str, background: str, threshold: float) -> dict[str, object]:
    """Return a WCAG contrast result with explicit measurement boundaries."""

    fg = parse_color(foreground)
    bg = parse_color(background)
    if fg is None or bg is None or fg[3] < 1 or bg[3] < 1:
        return {"foreground": foreground, "background": background, "ratio": None, "threshold": threshold, "status": "NOT_MEASURABLE"}
    ratio = (max(_relative_luminance(fg), _relative_luminance(bg)) + 0.05) / (min(_relative_luminance(fg), _relative_luminance(bg)) + 0.05)
    return {"foreground": foreground, "background": background, "ratio": round(ratio, 3), "threshold": threshold, "status": "PASS" if ratio >= threshold else "FAIL"}


def focus_contrast(style: Mapping[str, object]) -> dict[str, object]:
    """Require a computed, non-zero focus treatment before measuring it."""

    if style.get("outlineStyle") not in {"none", "hidden"} and str(style.get("outlineWidth")) not in {"0px", "0"}:
        return contrast(str(style.get("outlineColor")), str(style.get("backgroundColor")), 3.0)
    if style.get("boxShadow") not in {None, "none"} and "0 0 0 0" not in str(style.get("boxShadow")):
        return {"foreground": "computed box-shadow", "background": str(style.get("backgroundColor")), "ratio": None, "threshold": 3.0, "status": "NOT_MEASURABLE", "evidence": str(style.get("boxShadow"))}
    return {"foreground": None, "background": str(style.get("backgroundColor")), "ratio": None, "threshold": 3.0, "status": "FAIL", "reason": "no visible computed focus indicator"}


def _truthy(mapping: Mapping[str, Any], key: str) -> bool:
    return mapping.get(key) is True


def evaluate_o10_acceptance(report: Mapping[str, object]) -> dict[str, object]:
    """Evaluate every decisive O10 fact used by the report and CI.

    The returned predicate is intentionally strict.  A missing fact is false,
    so a partial or stale report cannot receive PASS by accident.
    """

    real = report.get("real_browser") if isinstance(report.get("real_browser"), Mapping) else {}
    critical = real.get("critical_path") if isinstance(real.get("critical_path"), Mapping) else {}
    events = real.get("events") if isinstance(real.get("events"), Mapping) else {}
    attention = real.get("semantics_attention") if isinstance(real.get("semantics_attention"), Mapping) else real.get("semantics_attention_after_return") if isinstance(real.get("semantics_attention_after_return"), Mapping) else {}
    episode = real.get("semantics_episode") if isinstance(real.get("semantics_episode"), Mapping) else {}
    search = real.get("search_no_match") if isinstance(real.get("search_no_match"), Mapping) else {}
    walkthrough = real.get("keyboard_walkthrough") if isinstance(real.get("keyboard_walkthrough"), Mapping) else {}
    focus = critical.get("focus_continuity") if isinstance(critical.get("focus_continuity"), Mapping) else {}
    focus_indicator = critical.get("focus_indicator_contrast") if isinstance(critical.get("focus_indicator_contrast"), Mapping) else {}
    forced = real.get("forced_colors") if isinstance(real.get("forced_colors"), Mapping) else {}
    reduced = real.get("reduced_motion") if isinstance(real.get("reduced_motion"), Mapping) else {}
    durable = report.get("durable_command") if isinstance(report.get("durable_command"), Mapping) else {}

    semantic_attention_ok = (
        attention.get("h1_count") == 1
        and attention.get("h1_text") == ["Attention"]
        and attention.get("main_count") == 1
        and attention.get("primary_navigation_count") == 1
        and attention.get("search_table_count") == 1
        and attention.get("table_controls_count") == 1
        and attention.get("attention_results_region_count") == 1
        and attention.get("status_count", 0) >= 1
        and attention.get("stable_names") is True
    )
    semantic_episode_ok = (
        episode.get("h1_count") == 1
        and episode.get("h1_text") == ["Episode decision brief"]
        and episode.get("main_count") == 1
        and episode.get("primary_navigation_count") == 1
        and episode.get("episode_region_count") == 1
        and episode.get("status_count", 0) >= 1
        and episode.get("stable_names") is True
        and episode.get("selected_episode_identity") is True
        and episode.get("eligible_primary_action") is True
        and episode.get("episode_aria_truthful") is True
    )
    responsive = report.get("responsive") if isinstance(report.get("responsive"), Mapping) else {}
    def _responsive_viewport_ok(viewport: object, facts: object) -> bool:
        if not isinstance(facts, Mapping):
            return False
        measurements = facts.get("critical_target_measurements")
        if not isinstance(measurements, Mapping):
            return False
        phone_viewport = str(viewport).split("x", 1)[0] in {"320", "390"}
        applicable = {
            name: box
            for name, box in measurements.items()
            if phone_viewport or name != "Open navigation"
        }
        return (
            facts.get("status") == "PASS"
            and facts.get("document_overflow") is False
            and facts.get("application_horizontal_overflow") is False
            and facts.get("critical_targets_visible") is True
            and all(
                isinstance(box, Mapping)
                and box.get("width") is not None
                and box.get("height") is not None
                and float(box["width"]) >= 44
                and float(box["height"]) >= 44
                for box in applicable.values()
            )
        )

    responsive_ok = bool(responsive) and all(_responsive_viewport_ok(viewport, facts) for viewport, facts in responsive.items())
    contrast_facts = real.get("contrast") if isinstance(real.get("contrast"), Mapping) else {}
    contrast_ok = bool(contrast_facts) and all(isinstance(item, Mapping) and item.get("status") == "PASS" for item in contrast_facts.values())
    degraded = report.get("degraded_state_matrix") if isinstance(report.get("degraded_state_matrix"), Mapping) else {}
    degraded_ok = bool(degraded) and all(isinstance(item, Mapping) and item.get("status") == "PASS" and item.get("bounded_text") is True for item in degraded.values())
    forced_ok = forced.get("status") == "NOT_SUPPORTED" or (
        forced.get("status") == "PASS"
        and isinstance(forced.get("focus_indicator"), Mapping)
        and forced["focus_indicator"].get("status") == "PASS"
        and forced.get("focus_observable") is True
    )
    criteria = {
        "postgresql_18": report.get("postgres", {}).get("real_postgresql_18") is True if isinstance(report.get("postgres"), Mapping) else False,
        "keyboard_critical_path_complete": _truthy(critical, "keyboard_path_complete"),
        "focus_continuity_complete": _truthy(critical, "focus_continuity_complete") and _truthy(focus, "expected_sequence"),
        "browser_events_clean": all(not events.get(key) for key in ("console_errors", "page_errors", "request_failures")),
        "normal_focus_indicator": focus_indicator.get("status") == "PASS",
        "semantic_assertions": semantic_attention_ok and semantic_episode_ok,
        "truthful_populated_attention": _truthy(critical, "populated_attention_truthful"),
        "truthful_search_empty": _truthy(search, "truthful_no_match") and _truthy(search, "unfiltered_authorized_row_existed") and _truthy(search, "neutral_empty_overlay"),
        "responsive_viewports": responsive_ok,
        "contrast": contrast_ok,
        "reduced_motion": _truthy(reduced, "match_media"),
        "forced_colors": forced_ok,
        "degraded_states": degraded_ok,
        "keyboard_walkthrough": _truthy(walkthrough, "reached_search") and _truthy(walkthrough, "reached_table") and _truthy(walkthrough, "no_empty_focus"),
        "durable_command": durable.get("command_receipt_count") == 1 and durable.get("work_state_present") is True and durable.get("aggregate_version") == 1,
        "secret_safety": isinstance(real.get("secret_safety"), Mapping) and all(value is False for value in real["secret_safety"].values()),
    }
    failed = [name for name, passed in criteria.items() if not passed]
    return {"status": "PASS" if not failed else "FAIL", "criteria": criteria, "failed": failed}


__all__ = [
    "O10State",
    "attention_result_status",
    "contrast",
    "display_value",
    "episode_action",
    "evaluate_o10_acceptance",
    "focus_contrast",
    "parse_color",
    "state_for_error",
]
