"""Analytics layer for the BD visa dropdown monitor.

Three artefacts, all append-only so a crash mid-cycle loses nothing:

    samples.jsonl    one line per poll          -> raw data
    transitions.jsonl one line per state change -> exact online/offline times
    (rollup)         computed on demand         -> trends, heatmap, uptime

States are deliberately simple:

    ONLINE   Bangladesh present in the target dropdown
    OFFLINE  Bangladesh absent
    UNKNOWN  the check itself failed (site unreachable / WAF)

UNKNOWN never counts as OFFLINE. Treating an outage as "BD closed" would
corrupt every uptime percentage we report, so UNKNOWN intervals are tracked
separately and excluded from availability math.

Timestamps are stored as ISO-8601 with an explicit UTC offset. Timezone math
uses fixed offsets (BST = UTC+6, IST = UTC+5:30) because neither Bangladesh
nor India observes DST -- this avoids the `tzdata` dependency that stock
Windows Python needs for zoneinfo.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BST = timezone(timedelta(hours=6), name="BST")
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
UTC = timezone.utc

ONLINE = "ONLINE"
OFFLINE = "OFFLINE"
UNKNOWN = "UNKNOWN"

SAMPLES_FILE = "samples.jsonl"
TRANSITIONS_FILE = "transitions.jsonl"


def now_iso(tz: timezone = UTC) -> str:
    return datetime.now(tz).isoformat(timespec="seconds")


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp. Falls back to UTC when no offset present."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def humanize(seconds: float) -> str:
    """24120 -> '6h 42m'. Compact, human-first duration formatting."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, _secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


@dataclass
class Sample:
    ts: str
    state: str
    available: bool
    option_count: int | None = None
    latency_ms: int | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, separators=(",", ":"))


@dataclass
class Transition:
    ts: str  # when the change was observed
    from_state: str
    to_state: str
    span_seconds: float  # how long the PREVIOUS state lasted
    span_started: str  # when the previous state began

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, separators=(",", ":"))


@dataclass
class Analytics:
    """Append-only logger + on-demand rollup."""

    base_dir: Path
    max_samples: int = 400_000  # ~1 year at ~15 min cadence
    _last_state: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.samples_path = self.base_dir / SAMPLES_FILE
        self.transitions_path = self.base_dir / TRANSITIONS_FILE

    # ---------------------------------------------------------------- writes
    def _append(self, path: Path, line: str) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def record_sample(self, sample: Sample) -> Transition | None:
        """Append a sample; if the state changed, also append a transition.

        Returns the Transition when one fired, else None.
        """
        previous = self._last_state

        # Resolve when `previous` began BEFORE appending: walking backwards
        # after the append would hit the brand-new record first and collapse
        # the measured span to zero.
        started = self._state_started_at(previous) if previous is not None else None

        self._append(self.samples_path, sample.to_json())

        # First observation just seeds the state machine -- no transition.
        if previous is None:
            self._last_state = sample.state
            return None
        if previous == sample.state:
            return None

        if started is None:
            # No record of `previous` on disk (state was seeded out-of-band);
            # fall back to the oldest sample we have.
            oldest = self._read_samples(limit=1)
            started = parse_ts(oldest[0].ts) if oldest else parse_ts(sample.ts)

        span = (parse_ts(sample.ts) - started).total_seconds()
        transition = Transition(
            ts=sample.ts,
            from_state=previous,
            to_state=sample.state,
            span_seconds=span,
            span_started=started.isoformat(timespec="seconds"),
        )
        self._append(self.transitions_path, transition.to_json())
        self._last_state = sample.state
        return transition

    def _state_started_at(self, state: str) -> datetime | None:
        """When `state` began: timestamp of the oldest record in its run.

        Walks backwards through samples until a differing state is found, so
        the final assignment is the oldest sample of the current run.
        Returns None when no record carries that state.
        """
        started: datetime | None = None
        for record in reversed(self._read_samples()):
            if record.state != state:
                break
            started = parse_ts(record.ts)
        return started

    def seed_state(self) -> str | None:
        """Warm _last_state from disk so a restart does not re-fire a transition."""
        samples = self._read_samples(limit=1)
        if samples:
            self._last_state = samples[0].state
        return self._last_state

    # ----------------------------------------------------------------- reads
    def _read_samples(self, limit: int | None = None) -> list[Sample]:
        if not self.samples_path.exists():
            return []
        out: list[Sample] = []
        with open(self.samples_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                out.append(
                    Sample(
                        ts=raw.get("ts", ""),
                        state=raw.get("state", UNKNOWN),
                        available=bool(raw.get("available", False)),
                        option_count=raw.get("option_count"),
                        latency_ms=raw.get("latency_ms"),
                        error=raw.get("error"),
                    )
                )
        return out[-limit:] if limit else out

    def _read_transitions(self) -> list[Transition]:
        if not self.transitions_path.exists():
            return []
        out: list[Transition] = []
        with open(self.transitions_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                out.append(
                    Transition(
                        ts=raw.get("ts", ""),
                        from_state=raw.get("from_state", UNKNOWN),
                        to_state=raw.get("to_state", UNKNOWN),
                        span_seconds=float(raw.get("span_seconds", 0)),
                        span_started=raw.get("span_started", ""),
                    )
                )
        return out

    # --------------------------------------------------------------- rollups
    def rollup(self, days: int = 30) -> dict[str, Any]:
        """Compute everything the dashboard and `--report` need."""
        samples = list(self._read_samples())
        transitions = self._read_transitions()

        current = samples[-1] if samples else None
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=days)

        window = [
            s
            for s in samples
            if s.state in (ONLINE, OFFLINE, UNKNOWN) and parse_ts(s.ts) >= cutoff
        ]

        # --- per-state totals over the window (UNKNOWN excluded from
        #     availability, reported separately).
        by_state = {ONLINE: 0, OFFLINE: 0, UNKNOWN: 0}
        for s in window:
            by_state[s.state] = by_state.get(s.state, 0) + 1

        counted = by_state[ONLINE] + by_state[OFFLINE]
        uptime_pct = round(100.0 * by_state[ONLINE] / counted, 2) if counted else 0.0

        # --- daily trend (BST calendar days)
        daily: dict[str, dict[str, int]] = {}
        for s in window:
            day = parse_ts(s.ts).astimezone(BST).strftime("%Y-%m-%d")
            bucket = daily.setdefault(day, {ONLINE: 0, OFFLINE: 0, UNKNOWN: 0})
            bucket[s.state] = bucket.get(s.state, 0) + 1
        trend = []
        for day in sorted(daily):
            b = daily[day]
            tot = b[ONLINE] + b[OFFLINE]
            trend.append(
                {
                    "date": day,
                    "uptime_pct": round(100.0 * b[ONLINE] / tot, 2) if tot else 0.0,
                    "online": b[ONLINE],
                    "offline": b[OFFLINE],
                    "unknown": b[UNKNOWN],
                }
            )

        # --- hour-of-day heatmap (BST hours): which hours BD tends to open
        hourly: dict[int, list[int]] = {h: [0, 0] for h in range(24)}
        for s in window:
            if s.state not in (ONLINE, OFFLINE):
                continue
            h = parse_ts(s.ts).astimezone(BST).hour
            hourly[h][0 if s.state == ONLINE else 1] += 1
        heatmap = []
        for h in range(24):
            on, off = hourly[h]
            tot = on + off
            heatmap.append(
                {
                    "hour": h,
                    "online": on,
                    "offline": off,
                    "uptime_pct": round(100.0 * on / tot, 2) if tot else 0.0,
                }
            )

        # --- current streak: how long the current state has held
        current_streak_s = 0.0
        if current:
            started = self._state_started_at(current.state)
            if started is not None:
                current_streak_s = max(0.0, (now - started).total_seconds())
            else:
                # Fall back to the last recorded sample's own timestamp.
                current_streak_s = max(
                    0.0, (now - parse_ts(current.ts)).total_seconds()
                )

        # --- longest ONLINE spell inside the window.
        # Completed spells come from ONLINE->OFFLINE transitions; the currently
        # running spell has no closing transition yet, so it must be added
        # separately or "longest ONLINE" reports 0s while BD is up.
        longest_online = 0.0
        for t in transitions:
            if t.to_state == OFFLINE and t.span_started:
                try:
                    if parse_ts(t.span_started) >= cutoff:
                        longest_online = max(longest_online, t.span_seconds)
                except ValueError:
                    continue
        if current and current.state == ONLINE:
            longest_online = max(longest_online, current_streak_s)

        # --- how many times today (BST) did BD flip availability
        today_bst = now.astimezone(BST).strftime("%Y-%m-%d")
        flips_today = 0
        for t in transitions:
            same_day = parse_ts(t.ts).astimezone(BST).strftime("%Y-%m-%d") == today_bst
            if same_day and {t.from_state, t.to_state} == {ONLINE, OFFLINE}:
                flips_today += 1

        recent = transitions[-15:]
        return {
            "generated_at": now_iso(BST),
            "window_days": days,
            "current": (
                {
                    "state": current.state,
                    "since": current.ts,
                    "since_bst": parse_ts(current.ts)
                    .astimezone(BST)
                    .isoformat(timespec="seconds"),
                    "option_count": current.option_count,
                    "available": current.available,
                    "error": current.error,
                    "streak_seconds": round(current_streak_s),
                    "streak_human": humanize(current_streak_s),
                }
                if current
                else None
            ),
            "totals": {
                "samples": len(window),
                "online": by_state[ONLINE],
                "offline": by_state[OFFLINE],
                "unknown": by_state[UNKNOWN],
                "uptime_pct": uptime_pct,
            },
            "flips_today": flips_today,
            "longest_online_seconds": round(longest_online),
            "longest_online_human": humanize(longest_online),
            "trend": trend,
            "heatmap": heatmap,
            "transitions": [
                {
                    "ts": t.ts,
                    "ts_bst": parse_ts(t.ts)
                    .astimezone(BST)
                    .isoformat(timespec="seconds"),
                    "from": t.from_state,
                    "to": t.to_state,
                    "duration_seconds": round(t.span_seconds),
                    "duration_human": humanize(t.span_seconds),
                    "started": t.span_started,
                }
                for t in reversed(recent)
            ],
            "latest_samples": [
                {
                    "ts": s.ts,
                    "ts_bst": parse_ts(s.ts)
                    .astimezone(BST)
                    .isoformat(timespec="seconds"),
                    "state": s.state,
                    "option_count": s.option_count,
                    "latency_ms": s.latency_ms,
                    "error": s.error,
                }
                for s in samples[-25:]
            ],
        }

    def status_summary(self, days: int = 7) -> str:
        """Plain-English one-pager used by `--report` and webhook summaries."""
        r = self.rollup(days=days)
        cur = r.get("current")
        if not cur:
            return "No data yet."
        t = r["totals"]
        lines = [
            (
                f"BD is currently {cur['state']} "
                f"(has been for {cur['streak_human']}, since {cur['since_bst']})."
            ),
            (
                f"Last {r['window_days']}d: uptime {t['uptime_pct']}% "
                f"({t['online']} online / {t['offline']} offline / {t['unknown']} unknown)."
            ),
            f"Longest continuous ONLINE spell: {r['longest_online_human']}.",
            f"Flips today (BST): {r['flips_today']}.",
        ]
        if r["trend"]:
            last = r["trend"][-1]
            lines.append(
                f"Latest full day ({last['date']}): {last['uptime_pct']}% uptime."
            )
        return "\n".join(lines)


def load_env_default(key: str, default: str) -> str:
    return os.environ.get(key, default) or default
