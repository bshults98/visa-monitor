"""Webhook payload builders for the BD visa dropdown monitor.

Every payload carries a `summary` field -- a single human-readable sentence
that reads correctly on its own, so webhook.site / Slack / Telegram previews
are useful without parsing anything.

Payload shapes:
    MONITOR_STARTED        fired once when the process comes up
    BANGLADESH_AVAILABLE   the event that matters -- BD is in the dropdown
    BANGLADESH_UNAVAILABLE  BD dropped back out
    MONITOR_BLOCKED        consecutive failures (WAF / structure change)
    MONITOR_HEARTBEAT      periodic liveness signal
    MONITOR_TEST           connectivity smoke test
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from analytics import OFFLINE, ONLINE, humanize

BST = timezone(timedelta(hours=6), name="BST")

APPLY_URL = "https://indianvisaonline.gov.in/visa/Registration"


def _bst_now() -> str:
    return datetime.now(BST).isoformat(timespec="seconds")


def started(
    *,
    country: str,
    field: str,
    armed: bool,
    fast_windows: str,
    baseline_interval: int,
    host: str,
) -> dict[str, Any]:
    return {
        "event": "MONITOR_STARTED",
        "summary": (
            f"👁 Monitor up watching {country} in the '{field}' dropdown "
            f"(fast windows: {fast_windows}, baseline {baseline_interval}s)."
        ),
        "country": country,
        "field": field,
        "state": "STARTING",
        "armed": armed,
        "ts": _bst_now(),
        "timezone": "BST (Asia/Dhaka, UTC+6)",
        "fast_windows": fast_windows,
        "baseline_interval_s": baseline_interval,
        "host": host,
        "url": APPLY_URL,
    }


def available(
    *,
    country: str,
    field: str,
    option_count: int,
    offline_for_s: float | None,
    flips_today: int,
    streak_human: str,
    uptime_7d: float | None,
) -> dict[str, Any]:
    since = _bst_now()
    duration_txt = (
        f"after being offline for {humanize(offline_for_s)}"
        if offline_for_s
        else "just now"
    )
    return {
        "event": "BANGLADESH_AVAILABLE",
        "summary": (
            f"✅ {country} is BACK in the {field} dropdown {duration_txt}. "
            f"Apply now -- option {option_count} on the list, "
            f"{flips_today} flip(s) today, 7d uptime {uptime_7d}%."
        ),
        "country": country,
        "field": field,
        "state": ONLINE,
        "available": True,
        "option_count": option_count,
        "since": since,
        "was_offline_for_s": round(offline_for_s, 1)
        if offline_for_s is not None
        else None,
        "was_offline_for": humanize(offline_for_s)
        if offline_for_s is not None
        else None,
        "flips_today": flips_today,
        "current_streak": streak_human,
        "uptime_7d_pct": uptime_7d,
        "action": "Apply now -- open the Regular/Paper Visa form",
        "url": APPLY_URL,
    }


def unavailable(
    *,
    country: str,
    field: str,
    option_count: int,
    online_for_s: float | None,
    flips_today: int,
) -> dict[str, Any]:
    held = f"after {humanize(online_for_s)}" if online_for_s else "just now"
    return {
        "event": "BANGLADESH_UNAVAILABLE",
        "summary": (
            f"🔴 {country} dropped OUT of the {field} dropdown {held}. "
            f"Quota likely re-filled ({flips_today} flip(s) today)."
        ),
        "country": country,
        "field": field,
        "state": OFFLINE,
        "available": False,
        "option_count": option_count,
        "ts": _bst_now(),
        "was_online_for_s": round(online_for_s, 1)
        if online_for_s is not None
        else None,
        "was_online_for": humanize(online_for_s) if online_for_s is not None else None,
        "flips_today": flips_today,
        "action": "BD unavailable -- monitor will keep watching",
        "url": APPLY_URL,
    }


def blocked(
    *,
    country: str,
    field: str,
    consecutive_errors: int,
    last_error: str,
) -> dict[str, Any]:
    return {
        "event": "MONITOR_BLOCKED",
        "summary": (
            f"⚠️ Monitor can't read the {field} dropdown -- "
            f"{consecutive_errors} consecutive failures. Last error: {last_error}"
        ),
        "country": country,
        "field": field,
        "state": "BLOCKED",
        "ts": _bst_now(),
        "consecutive_errors": consecutive_errors,
        "last_error": last_error,
        "action": "IP likely WAF-blocked or site structure changed -- check manually",
        "url": APPLY_URL,
    }


def heartbeat(
    *,
    country: str,
    field: str,
    state: str,
    option_count: int | None,
    armed: bool,
    uptime_7d: float | None,
    samples_today: int,
) -> dict[str, Any]:
    return {
        "event": "MONITOR_HEARTBEAT",
        "summary": (
            f"💓 {country} monitor alive -- currently {state} "
            f"({samples_today} checks today, 7d uptime {uptime_7d}%)."
        ),
        "country": country,
        "field": field,
        "state": state,
        "ts": _bst_now(),
        "available": state == ONLINE,
        "option_count": option_count,
        "armed": armed,
        "uptime_7d_pct": uptime_7d,
        "samples_today": samples_today,
    }


def test(host: str) -> dict[str, Any]:
    return {
        "event": "MONITOR_TEST",
        "summary": "🧪 Webhook connectivity test from BD visa monitor.",
        "country": "BANGLADESH",
        "ts": _bst_now(),
        "message": "Webhook connectivity test from BD visa monitor",
        "host": host,
    }
