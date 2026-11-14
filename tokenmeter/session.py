"""Rolling session window.

The session anchor is derived from the data, not from the wall clock. If the app
was closed while Claude Code kept working, the transcript reader inserts events
with timestamps in the past; anchoring at "now" would leave them outside the
window and the meter would start at zero, lying about what was already consumed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from . import official_limits
from .config import Config
from .store import ModelTotals, Store, Totals

STATE_MANUAL_RESET = "manual_reset_ts"
# Session start taken from the web. Authoritative while it is in effect.
STATE_WEB_START = "session_web_start_ts"

# Where the start of the current session comes from, best first.
SOURCE_CLAUDE_CODE = "claude_code"  # reset time reported by Claude Code itself
SOURCE_CLAUDE_AI = "claude_ai"      # reset time the user copied from claude.ai
SOURCE_ESTIMATED = "estimated"      # deduced from the first local call
SOURCE_NONE = "none"                # no session in progress


@dataclass(frozen=True)
class Snapshot:
    now_ts: float
    start_ts: float
    end_ts: float
    remaining_seconds: float
    totals: Totals
    used_tokens: int
    budget_tokens: int
    percent: float
    percent_basis: str
    has_activity: bool
    estimated_unseen_tokens: int = 0

    @property
    def over_budget(self) -> bool:
        return self.used_tokens > self.budget_tokens

    def remaining_text(self) -> str:
        """Compact form for the tooltip: `2h 19m`."""
        secs = max(0, int(self.remaining_seconds))
        hours, rem = divmod(secs, 3600)
        minutes = rem // 60
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m"
        return f"{secs}s"

    def countdown_text(self) -> str:
        """Per-second countdown for the panel: `04:59:12`."""
        secs = max(0, int(self.remaining_seconds))
        hours, rem = divmod(secs, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def reset_at(self) -> datetime:
        """Local instant at which the window closes."""
        return datetime.fromtimestamp(self.end_ts)

    def reset_at_text(self) -> str:
        """Reset time on the local clock, with the day if it falls on another date."""
        when = self.reset_at()
        today = datetime.fromtimestamp(self.now_ts).date()
        delta_days = (when.date() - today).days
        stamp = when.strftime("%H:%M:%S")
        if delta_days == 0:
            return stamp
        if delta_days == 1:
            return f"{stamp} (tomorrow)"
        return when.strftime("%d/%m %H:%M:%S")


@dataclass(frozen=True)
class SessionRecord:
    """One past or current session: a window and the calls that fell in it."""

    start_ts: float
    end_ts: float
    totals: Totals
    used_tokens: int
    source: str   # SOURCE_CLAUDE_CODE, SOURCE_CLAUDE_AI or SOURCE_ESTIMATED
    active: bool  # still open at the instant the history was built


class SessionTracker:
    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config

    # -- anchor -------------------------------------------------------------

    def manual_reset_ts(self) -> float:
        raw = self._store.get_state(STATE_MANUAL_RESET)
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0

    def web_start_ts(self) -> float | None:
        raw = self._store.get_state(STATE_WEB_START)
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    def known_start(self) -> tuple[float | None, str]:
        """(start, source) of the latest session whose reset the account reported.

        Two things report it: Claude Code, when the limit rejects a call, and
        the user, by copying the reset from claude.ai. Both describe real
        windows, so the later one is the more recent.
        """
        candidates = []
        web = self.web_start_ts()
        if web is not None:
            candidates.append((web, SOURCE_CLAUDE_AI))
        notice = official_limits.latest(self._store, official_limits.FIVE_HOUR)
        if notice is not None:
            candidates.append((notice.resets_at - self._config.window_seconds, SOURCE_CLAUDE_CODE))
        return max(candidates) if candidates else (None, SOURCE_NONE)

    def anchor_source(self, now: float | None = None) -> str:
        """How much the start of the current session can be trusted."""
        now = time.time() if now is None else now
        start, has_activity = self.anchor(now)
        if not has_activity:
            return SOURCE_NONE
        known, source = self.known_start()
        return source if known == start else SOURCE_ESTIMATED

    def set_reset_from_web(self, reset_ts: float) -> float:
        """Anchors the session to the reset that claude.ai shows. Returns the start.

        The web sees usage from the browser, the phone and other machines; the
        local events do not. Its reset time is the only source that knows the
        real start.
        """
        start = reset_ts - self._config.window_seconds
        self._store.set_state(STATE_WEB_START, repr(start))
        return start

    def reset_now(self) -> None:
        """Cuts the current session. History is kept; only the window moves."""
        self._store.set_state(STATE_MANUAL_RESET, repr(time.time()))

    def anchor(self, now: float | None = None) -> tuple[float, bool]:
        """Returns (session_start, had_activity).

        Walks the events in order and opens a new window at every gap longer
        than the configured window. It is independent of arrival order, so a
        backfill of old transcripts converges to the same result.
        """
        now = time.time() if now is None else now
        window = self._config.window_seconds
        manual = self.manual_reset_ts()

        web, _source = self.known_start()
        if web is not None and manual <= web <= now < web + window:
            return web, True
        # A moving cutoff changes the partition of a continuous sequence.
        # Rebuild from a stable boundary, including the expired web anchor.
        web_rules = web is not None and manual <= web <= now
        floor = web if web_rules else manual
        stamps = self._store.event_timestamps_since(floor, self._config.percent_scope)
        stamps = [ts for ts in stamps if ts <= now]

        # An expired web anchor is still a session that existed: the calls made
        # inside it belong to it, and the next session starts with the first
        # call after it ended, as on claude.ai.
        start: float | None = web if web_rules else None
        for ts in stamps:
            if start is None or ts >= start + window:
                start = ts

        if start is None:
            # No activity: the session starts when the first call arrives.
            return (max(manual, now), False)
        if now >= start + window:
            # The last session expired and there has been nothing since.
            return (now, False)
        return (start, True)

    # -- history ------------------------------------------------------------

    def history(self, now: float | None = None, limit: int | None = None) -> list[SessionRecord]:
        """Every session the stored calls belong to, oldest first.

        A session reset does not lose anything: each call belongs to exactly one
        session, the one whose window contains it. The walk is the same one
        `anchor` does, kept whole instead of reduced to its last window, so the
        last record is the current session whenever one is open.

        Two instants cut the timeline, and a session never runs across one:
        a manual reset (the next session starts with the next call) and the
        latest window the account reported (a session starts exactly there).
        Older windows are estimated from the first call after the previous one
        ended; they were not reported by anyone and say so.
        """
        now = time.time() if now is None else now
        window = self._config.window_seconds
        manual = self.manual_reset_ts()
        known, known_source = self.known_start()
        if known is not None and not manual <= known <= now:
            known = None  # same condition `anchor` applies

        stamps = [ts for ts in self._store.event_timestamps_since(0.0, self._config.percent_scope)
                  if ts <= now]
        cuts = sorted({c for c in (manual, known) if c is not None and c > 0})
        windows: list[tuple[float, float, str]] = []  # (start, end, source)
        start: float | None = None
        source = SOURCE_ESTIMATED
        pending = list(cuts)

        def close(limit_ts: float) -> None:
            if start is not None:
                windows.append((start, min(start + window, limit_ts), source))

        for ts in [*stamps, None]:
            # Apply every cut that lies at or before this call (or all the
            # remaining ones once the calls are exhausted).
            while pending and (ts is None or pending[0] <= ts):
                cut = pending.pop(0)
                close(cut)
                if cut == known:
                    start, source = cut, known_source
                else:
                    start = None
            if ts is None:
                break
            if start is None or ts >= start + window:
                close(float("inf"))
                start, source = ts, SOURCE_ESTIMATED
        close(float("inf"))

        if limit is not None:
            windows = windows[-limit:] if limit > 0 else []
        basis = self._config.percent_basis
        records = []
        for begin, end, origin in windows:
            totals = self._store.totals_between(begin, end, self._config.percent_scope, now)
            records.append(SessionRecord(
                start_ts=begin, end_ts=end, totals=totals, used_tokens=totals.basis(basis),
                source=origin, active=begin <= now < end))
        return records

    # -- read for the UI ------------------------------------------------

    def snapshot(self, now: float | None = None) -> Snapshot:
        now = time.time() if now is None else now
        start, has_activity = self.anchor(now)
        window = self._config.window_seconds
        end = start + window

        totals = (self._store.totals_since(start, self._config.percent_scope)
                  if has_activity else Totals())
        used = totals.basis(self._config.percent_basis)
        cfg = self._config
        unseen = (cfg.calibrated_unseen_tokens if has_activity and cfg.is_calibrated
                  and cfg.calibrated_session_start == start
                  and cfg.calibrated_scope == cfg.percent_scope else 0)
        used += unseen
        budget = max(1, self._config.session_budget_tokens)

        return Snapshot(
            now_ts=now,
            start_ts=start,
            end_ts=end,
            remaining_seconds=max(0.0, end - now),
            totals=totals,
            used_tokens=used,
            budget_tokens=self._config.session_budget_tokens,
            percent=100.0 * used / budget,
            percent_basis=self._config.percent_basis,
            has_activity=has_activity,
            estimated_unseen_tokens=unseen,
        )

    def by_model(self, now: float | None = None) -> list[ModelTotals]:
        return self._store.totals_by_model(self.general_start(now))

    def calls_by_source(self, now: float | None = None) -> dict[str, int]:
        return self._store.calls_by_source(self.general_start(now))

    def general_start(self, now: float | None = None) -> float:
        """Breakdown across all providers: the last N observed hours."""
        now = time.time() if now is None else now
        return max(self.manual_reset_ts(), now - self._config.window_seconds)

    def seconds_since_last_call(self, now: float | None = None) -> float | None:
        """Age of the most recent recorded call, in seconds."""
        now = time.time() if now is None else now
        last = self._store.last_event_ts()
        return None if last is None else max(0.0, now - last)
