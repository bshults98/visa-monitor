#!/usr/bin/env python3
"""
BD Visa Country-Dropdown Monitor
================================

Watches the "Country/Region you are applying visa from" dropdown on the
Indian visa registration form and fires a webhook the instant Bangladesh
reappears in it (i.e. when the BD quota opens again).

    GET  /visa/Registration                      -> session cookies
    POST /visa/Registration  service_requested=1 -> full form HTML (~230 KB)

IMPORTANT - false-positive trap
-------------------------------
Only the <select name="appl.countryname"> block is inspected. Two OTHER
selects on the very same page contain the literal string "BANGLADESH":

    appl.purpose      -> "...MISCELLANEOUS VISA (X3)... (FOR BANGLADESHI NATIONALS ONLY)"
    gate_nationality  -> "<option value='BANGLADESH'>BANGLADESH</option>"

A naive  "if 'BANGLADESH' in html"  would therefore report AVAILABLE on
every single poll. The matcher below is deliberately scoped to the one
dropdown that matters.

Read-only: nothing is ever submitted, no passport/PII required, no captcha.
Portable: pure `requests` + stdlib -> runs unmodified on Windows and
Termux/Android.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("[!] Missing dependency: requests\n    Install it:  pip install requests")

try:
    import notifier
    from analytics import OFFLINE, ONLINE, UNKNOWN, Analytics, Sample, humanize
except ImportError as exc:  # pragma: no cover - missing sibling module
    sys.exit(
        f"[!] Missing project module ({exc}). Keep analytics.py/notifier.py next to monitor.py."
    )


# ---------------------------------------------------------------------------
# Timezones
# ---------------------------------------------------------------------------
# Fixed UTC offsets are used instead of zoneinfo.ZoneInfo so that no `tzdata`
# package is required (stock Windows Python cannot resolve 'Asia/Dhaka'
# without it). Neither Bangladesh nor India observes DST, so the offsets are
# permanent and always correct.
# ---------------------------------------------------------------------------
BST = timezone(timedelta(hours=6), name="BST")  # Asia/Dhaka
IST = timezone(
    timedelta(hours=5, minutes=30), name="IST"
)  # Asia/Kolkata (reference only)
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# ---------------------------------------------------------------------------
BASE = Path(os.environ.get("MONITOR_HOME") or Path(__file__).resolve().parent)

URL = "https://indianvisaonline.gov.in/visa/Registration"

WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://webhook.site/b4709261-68c9-4931-91ac-80e3afdccf0f",
).strip()
TARGET_COUNTRY = os.environ.get("TARGET_COUNTRY", "BANGLADESH").strip().upper()
TARGET_CODE = os.environ.get("TARGET_CODE", "BGD").strip().upper()
SELECT_NAME = os.environ.get("SELECT_NAME", "appl.countryname")

# Fast-polling windows, interpreted in BST (Asia/Dhaka). Reset times are
# community folklore (unverified), so these are configured rather than assumed.
FAST_WINDOWS = os.environ.get("FAST_WINDOWS", "00:00-00:30,08:00-09:30")
FAST_MIN = int(os.environ.get("FAST_MIN", "60"))  # seconds
FAST_MAX = int(os.environ.get("FAST_MAX", "90"))  # seconds
BASE_INTERVAL = int(os.environ.get("BASE_INTERVAL", "900"))  # 15 min
JITTER = float(os.environ.get("JITTER", "0.15"))  # +/- 15 %
MAX_BACKOFF = int(os.environ.get("MAX_BACKOFF", "1800"))
ERROR_THRESHOLD = int(os.environ.get("ERROR_THRESHOLD", "4"))
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "6"))
TIMEOUT = float(os.environ.get("TIMEOUT", "30"))

STATE_FILE = BASE / "state.json"
LOG_FILE = BASE / "monitor.log"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Scoped to the ONE dropdown that matters. See module docstring.
SELECT_RE = re.compile(
    r"<select[^>]*\bname\s*=\s*['\"]"
    + re.escape(SELECT_NAME)
    + r"['\"][^>]*>(.*?)</select>",
    re.IGNORECASE | re.DOTALL,
)
OPTION_RE = re.compile(r"<option([^>]*)>(.*?)</option>", re.IGNORECASE | re.DOTALL)
VALUE_RE = re.compile(r"\bvalue\s*=\s*['\"]([^'\"]*)['\"]", re.IGNORECASE)
WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _force_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 so emoji in summaries cannot crash the process.

    Windows terminals default to cp1252, which raises UnicodeEncodeError the
    moment a glyph like U+2705 is printed. Webhook summaries contain emoji, so
    any code path that prints one must not be able to take the monitor down.
    """
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # closed/redirected stream - not worth failing over


def now_iso(tz: timezone = UTC) -> str:
    # NB: `timezone.utc` is an *instance*, not a callable -> no parentheses.
    return datetime.now(tz).isoformat(timespec="seconds")


def log_setup(verbose: bool = False) -> logging.Logger:
    _force_utf8_stdio()
    log = logging.getLogger("visa")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass  # unwritable dir is not fatal
    return log


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _hm(text: str) -> int:
    hh, mm = text.strip().split(":")
    return int(hh) * 60 + int(mm)


def parse_windows(spec: str) -> list[tuple[int, int]]:
    """'00:00-00:30,08:00-09:30' -> [(0,30), (480,570)] (minutes since midnight)."""
    out: list[tuple[int, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        try:
            start, end = (_hm(p) for p in part.split("-", 1))
        except ValueError:
            continue
        if end <= start:  # window crosses midnight
            out.append((start, 1440))
            out.append((0, end))
        else:
            out.append((start, end))
    return out


WINDOWS = parse_windows(FAST_WINDOWS)


def in_fast_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(BST)
    mins = now.hour * 60 + now.minute
    return any(s <= mins < e for s, e in WINDOWS)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def check_once() -> dict:
    """One read-only poll of the registration form."""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    # 1) Landing page -> establishes JSESSIONID / IVFRT_Cookie (required;
    #    posting without it gets bounced to index.html).
    r = s.get(URL, timeout=TIMEOUT)
    if r.status_code == 403:
        raise RuntimeError("HTTP 403 on GET - IP blocked by WAF")
    if r.status_code == 429:
        raise RuntimeError("HTTP 429 on GET - rate limited")
    r.raise_for_status()

    # 2) Render the form. Redirects are NOT followed: a 302 to index.html
    #    means the WAF rejected the POST, which we want to surface.
    r = s.post(
        URL,
        data={"service_requested": "1", "requestedPage": ""},
        headers={
            "Referer": URL,
            "Origin": "https://indianvisaonline.gov.in",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        allow_redirects=False,
        timeout=TIMEOUT,
    )
    if r.status_code == 302:
        raise RuntimeError("POST redirected to index - session rejected (WAF)")
    if r.status_code == 403:
        raise RuntimeError("HTTP 403 on POST - IP blocked by WAF")
    if r.status_code == 429:
        raise RuntimeError("HTTP 429 on POST - rate limited")
    if r.status_code != 200:
        raise RuntimeError(f"POST returned HTTP {r.status_code}")

    m = SELECT_RE.search(r.text)
    if not m:
        raise RuntimeError(
            f"select '{SELECT_NAME}' not found - page structure changed or block page returned"
        )

    matched = []
    count = 0
    for attrs, text in OPTION_RE.findall(m.group(1)):
        vm = VALUE_RE.search(attrs)
        value = vm.group(1).strip() if vm else ""
        norm = WS_RE.sub(" ", text).strip()
        count += 1
        # Accept either the visible label or the ISO-3166 alpha-3 code
        # (appl.countryname uses codes such as DZA / NPL / PAK).
        if norm.upper() == TARGET_COUNTRY or value.upper() == TARGET_CODE:
            matched.append({"value": value, "text": norm})

    return {
        "available": bool(matched),
        "option_count": count,
        "matched": matched,
        "ts": now_iso(),
    }


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
def send_webhook(log: logging.Logger, payload: dict, retries: int = 3) -> bool:
    if not WEBHOOK_URL:
        log.warning("WEBHOOK_URL not set - skipping notification")
        return False
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
            if r.ok:
                log.info(
                    "webhook OK [%s] -> HTTP %d",
                    payload.get("event", "?"),
                    r.status_code,
                )
                return True
            log.warning(
                "webhook HTTP %d for [%s] (attempt %d/%d)",
                r.status_code,
                payload.get("event", "?"),
                attempt,
                retries,
            )
        except Exception as exc:  # noqa: BLE001 - any transport error is retryable
            log.warning(
                "webhook error %s for [%s] (attempt %d/%d)",
                exc,
                payload.get("event", "?"),
                attempt,
                retries,
            )
        time.sleep(2 * attempt)
    log.error(
        "webhook FAILED for [%s] after %d attempts", payload.get("event"), retries
    )
    return False


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
def next_sleep(errors: int, log: logging.Logger) -> float:
    now = datetime.now(BST)

    if errors:
        # Exponential backoff while the site is unhappy.
        delay = min(BASE_INTERVAL * (2**errors), MAX_BACKOFF)
        delay *= random.uniform(0.9, 1.15)
        delay = max(20.0, delay)
        log.info(
            "backoff: next check in %.0fs (%d consecutive error(s))", delay, errors
        )
        return delay

    if in_fast_window(now):
        delay = float(random.randint(FAST_MIN, FAST_MAX))
        log.info(
            "FAST WINDOW [BST %s]: next check in %.0fs",
            now.strftime("%H:%M"),
            delay,
        )
        return delay

    delay = BASE_INTERVAL * random.uniform(1 - JITTER, 1 + JITTER)
    log.info("baseline: next check in %.0fs", delay)
    return delay


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
_STOP = False


def _handle_stop(signum, frame):
    global _STOP
    _STOP = True


for _sig in ("SIGINT", "SIGTERM"):
    if hasattr(signal, _sig):
        try:
            signal.signal(getattr(signal, _sig), _handle_stop)
        except (ValueError, OSError):
            pass


def _interruptible_sleep(seconds: float) -> None:
    end = time.time() + seconds
    while not _STOP and time.time() < end:
        time.sleep(min(1.0, end - time.time()))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    # _STOP is module-level (set by signal handlers); it is also assigned in
    # the KeyboardInterrupt branch below, so it MUST be declared global here
    # or Python would treat it as a local for the whole function and raise
    # UnboundLocalError on `while not _STOP`.
    global _STOP
    log = log_setup(args.verbose)
    state = load_state()

    armed = state.get("armed", True)  # True = we still owe a notification
    errors = 0
    blocked_sent = bool(state.get("blocked_sent", False))
    last_heartbeat = state.get("last_heartbeat")

    # Analytics: append-only sample + transition logs for trend analysis.
    analytics = Analytics(BASE / "data")
    seeded_state = analytics.seed_state()
    log.info(
        "analytics  : %s (seeded state=%s)", analytics.base_dir, seeded_state or "none"
    )

    log.info("=" * 62)
    log.info("BD visa dropdown monitor starting")
    log.info(
        "target    : %s (code %s) in <select name='%s'>",
        TARGET_COUNTRY,
        TARGET_CODE,
        SELECT_NAME,
    )
    log.info("webhook   : %s", WEBHOOK_URL or "(disabled)")
    log.info(
        "schedule  : fast=%s (%d-%ds) | baseline=%ds +/- %d%%",
        FAST_WINDOWS,
        FAST_MIN,
        FAST_MAX,
        BASE_INTERVAL,
        int(JITTER * 100),
    )
    log.info("state     : armed=%s last_heartbeat=%s", armed, last_heartbeat or "none")
    log.info("scripts   : %s", BASE)
    log.info("=" * 62)

    send_webhook(
        log,
        {
            "event": "MONITOR_STARTED",
            "country": TARGET_COUNTRY,
            "field": SELECT_NAME,
            "ts": now_iso(),
            "timezone": "BST (Asia/Dhaka, UTC+6)",
            "fast_windows": FAST_WINDOWS,
            "baseline_interval_s": BASE_INTERVAL,
            "host": sys.platform,
            "armed": armed,
        },
    )

    while not _STOP:
        try:
            t0 = time.monotonic()
            res = check_once()
            latency_ms = int((time.monotonic() - t0) * 1000)
            errors = 0
            if blocked_sent:
                blocked_sent = False

            avail = res["available"]
            new_state = ONLINE if avail else OFFLINE

            # Always record the sample -- this is the raw trend data.
            transition = analytics.record_sample(
                Sample(
                    ts=now_iso(UTC),
                    state=new_state,
                    available=avail,
                    option_count=res.get("option_count"),
                    latency_ms=latency_ms,
                )
            )
            roll = analytics.rollup(days=7)

            log.info(
                "check: available=%-5s options=%-4d latency=%dms%s",
                str(avail),
                res["option_count"],
                latency_ms,
                f"  TRANSITION {transition.from_state}->{transition.to_state}"
                if transition
                else "",
            )

            # --- transition-driven notification (exact online/offline events)
            if transition:
                flips = roll.get("flips_today", 0)
                if transition.to_state == ONLINE:
                    send_webhook(
                        log,
                        notifier.available(
                            country=TARGET_COUNTRY,
                            field=SELECT_NAME,
                            option_count=res["option_count"],
                            offline_for_s=transition.span_seconds,
                            flips_today=flips,
                            streak_human=roll["current"]["streak_human"]
                            if roll.get("current")
                            else humanize(0),
                            uptime_7d=roll["totals"]["uptime_pct"],
                        ),
                    )
                    armed = False
                    log.warning(
                        "*** %s APPEARED in dropdown after %s - notified ***",
                        TARGET_COUNTRY,
                        humanize(transition.span_seconds),
                    )
                elif transition.to_state == OFFLINE:
                    send_webhook(
                        log,
                        notifier.unavailable(
                            country=TARGET_COUNTRY,
                            field=SELECT_NAME,
                            option_count=res["option_count"],
                            online_for_s=transition.span_seconds,
                            flips_today=flips,
                        ),
                    )
                    armed = True
                    log.info(
                        "%s left dropdown after %s -> re-armed",
                        TARGET_COUNTRY,
                        humanize(transition.span_seconds),
                    )

                # Re-render the public dashboard whenever the state changes.
                if not args.no_render:
                    _render_dashboard(analytics, log)
            elif avail and armed:
                # No transition recorded (e.g. fresh install with BD already up)
                # but we still owe the user a first notification.
                send_webhook(
                    log,
                    notifier.available(
                        country=TARGET_COUNTRY,
                        field=SELECT_NAME,
                        option_count=res["option_count"],
                        offline_for_s=None,
                        flips_today=roll.get("flips_today", 0),
                        streak_human=roll["current"]["streak_human"]
                        if roll.get("current")
                        else humanize(0),
                        uptime_7d=roll["totals"]["uptime_pct"],
                    ),
                )
                armed = False
                log.warning("*** %s present in dropdown - notified ***", TARGET_COUNTRY)
            elif not avail and not armed:
                armed = True
                log.info("%s not in dropdown -> re-armed", TARGET_COUNTRY)

            # Periodic liveness signal so a dead process is noticeable.
            if HEARTBEAT_HOURS > 0:
                due = True
                if last_heartbeat:
                    try:
                        elapsed = (
                            datetime.now(UTC) - datetime.fromisoformat(last_heartbeat)
                        ).total_seconds()
                        due = elapsed >= HEARTBEAT_HOURS * 3600
                    except ValueError:
                        due = True
                if due:
                    send_webhook(
                        log,
                        notifier.heartbeat(
                            country=TARGET_COUNTRY,
                            field=SELECT_NAME,
                            state=new_state,
                            option_count=res["option_count"],
                            armed=armed,
                            uptime_7d=roll["totals"]["uptime_pct"],
                            samples_today=roll["totals"]["samples"],
                        ),
                    )
                    last_heartbeat = now_iso()

        except KeyboardInterrupt:
            _STOP = True
        except Exception as exc:  # noqa: BLE001 - one bad poll must not kill the loop
            errors += 1
            log.error("check failed (%d/%d): %s", errors, ERROR_THRESHOLD, exc)
            # Record UNKNOWN so site outages are visible in the trends but do
            # NOT get counted as "BD offline" in availability maths.
            analytics.record_sample(
                Sample(
                    ts=now_iso(UTC),
                    state=UNKNOWN,
                    available=False,
                    error=str(exc)[:300],
                )
            )
            if errors >= ERROR_THRESHOLD and not blocked_sent:
                blocked_sent = True
                send_webhook(
                    log,
                    notifier.blocked(
                        country=TARGET_COUNTRY,
                        field=SELECT_NAME,
                        consecutive_errors=errors,
                        last_error=str(exc),
                    ),
                )
        finally:
            state.update(
                {
                    "armed": armed,
                    "blocked_sent": blocked_sent,
                    "last_heartbeat": last_heartbeat,
                    "last_run": now_iso(BST),
                    "last_errors": errors,
                }
            )
            save_state(state)

        if _STOP:
            break

        _interruptible_sleep(next_sleep(errors, log))

    log.info("stopped cleanly")
    return 0


# ---------------------------------------------------------------------------
# One-shot modes
# ---------------------------------------------------------------------------
def _render_dashboard(
    analytics: Analytics,
    log: logging.Logger,
    out_dir: Path | None = None,
    days: int = 30,
) -> dict:
    """Best-effort dashboard render: a failure here must never kill the loop."""
    try:
        import dashboard as _dashboard

        return _dashboard.render(analytics, out_dir or (BASE / "site"), days=days)
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard render failed: %s", exc)
        return {}


def do_once(args: argparse.Namespace) -> int:
    log = log_setup(args.verbose)
    try:
        res = check_once()
    except Exception as exc:  # noqa: BLE001
        log.error("check failed: %s", exc)
        print(json.dumps({"available": False, "error": str(exc)}, indent=2))
        return 1

    log.info(
        "check: available=%-5s options=%d",
        str(res["available"]),
        res["option_count"],
    )
    print(json.dumps(res, indent=2))

    # Record the sample so even one-shot runs contribute to the trends.
    if not args.no_record:
        analytics = Analytics(BASE / "data")
        analytics.seed_state()
        transition = analytics.record_sample(
            Sample(
                ts=now_iso(UTC),
                state=ONLINE if res["available"] else OFFLINE,
                available=res["available"],
                option_count=res.get("option_count"),
            )
        )
        if transition:
            print(
                f"transition: {transition.from_state} -> {transition.to_state} "
                f"(previous held {humanize(transition.span_seconds)})"
            )
        if not args.no_render:
            _render_dashboard(analytics, log)

    if res["available"] and not args.no_notify:
        roll = Analytics(BASE / "data").rollup(days=7)
        send_webhook(
            log,
            notifier.available(
                country=TARGET_COUNTRY,
                field=SELECT_NAME,
                option_count=res["option_count"],
                offline_for_s=None,
                flips_today=roll.get("flips_today", 0),
                streak_human=roll["current"]["streak_human"]
                if roll.get("current")
                else humanize(0),
                uptime_7d=roll["totals"]["uptime_pct"],
            ),
        )
    # Exit 0 whenever the check itself succeeded; availability is reported in
    # the JSON above. A failed check already returned 1 earlier.
    return 0


def do_test_webhook(args: argparse.Namespace) -> int:
    log = log_setup(args.verbose)
    ok = send_webhook(log, notifier.test(host=sys.platform))
    print("webhook test:", "OK" if ok else "FAILED")
    return 0 if ok else 1


def do_rearm(_args: argparse.Namespace) -> int:
    state = load_state()
    state["armed"] = True
    state["blocked_sent"] = False
    save_state(state)
    print("re-armed -> next availability event will notify")
    return 0


def do_render(args: argparse.Namespace) -> int:
    """Render the dashboard from existing logs without polling the site."""
    log = log_setup(args.verbose)
    analytics = Analytics(BASE / "data")
    analytics.seed_state()
    if not analytics.samples_path.exists():
        log.warning("no samples yet at %s - nothing to render", analytics.samples_path)
    out = Path(args.out) if args.out else (BASE / "site")
    data = _render_dashboard(analytics, log, out_dir=out, days=args.days)
    t = data.get("totals", {})
    print(f"rendered -> {out / 'index.html'}")
    print(f"  window : last {data.get('window_days')} days")
    print(f"  uptime : {t.get('uptime_pct')}% ({t.get('samples')} checks)")
    print(f"  changes: {len(data.get('transitions', []))}")
    return 0


def do_report(args: argparse.Namespace) -> int:
    """Plain-English trend summary on stdout (no network needed)."""
    log_setup(args.verbose)
    analytics = Analytics(BASE / "data")
    analytics.seed_state()
    if args.json:
        print(json.dumps(analytics.rollup(days=args.days), indent=2, ensure_ascii=True))
    else:
        print(analytics.status_summary(days=args.days))
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="Monitor the Indian visa form for Bangladesh availability."
    )
    p.add_argument("--once", action="store_true", help="run a single check and exit")
    p.add_argument(
        "--test-webhook", action="store_true", help="send a test webhook and exit"
    )
    p.add_argument(
        "--rearm", action="store_true", help="re-arm the one-shot notification"
    )
    p.add_argument(
        "--no-notify", action="store_true", help="with --once, do not send a webhook"
    )
    p.add_argument(
        "--no-record",
        action="store_true",
        help="with --once, skip writing to the analytics log",
    )
    p.add_argument(
        "--no-render",
        action="store_true",
        help="do not regenerate the dashboard (main loop / --once)",
    )
    p.add_argument(
        "--render",
        action="store_true",
        help="render the dashboard from existing logs and exit (no network)",
    )
    p.add_argument(
        "--report",
        action="store_true",
        help="print a plain-English trend summary and exit (no network)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="window for --report / --render (default: 30)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="with --report, emit machine-readable JSON",
    )
    p.add_argument(
        "--out",
        default=None,
        help="output directory for --render (default: ./site)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = p.parse_args()

    if args.test_webhook:
        return do_test_webhook(args)
    if args.rearm:
        return do_rearm(args)
    if args.render:
        return do_render(args)
    if args.report:
        return do_report(args)
    if args.once:
        return do_once(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
