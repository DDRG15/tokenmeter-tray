"""Weekly window, the second one claude.ai shows.

It is modeled differently from the session window **on purpose**. The 5 h one
is rolling: it starts with the first call and its anchor is derived from the
data. The weekly one is not: the web says "Resets in 13 hr 35 min", which
describes a cycle with a fixed reset moment that repeats every 7 days,
regardless of when it is used.

Forcing both into one abstraction would give a wrong anchor for one of them.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .store import Store, Totals

STATE_WEEKLY_RESET = "weekly_reset_ts"
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class WeeklySnapshot:
    now_ts: float
    start_ts: float
    end_ts: float
    remaining_seconds: float
    totals: Totals
    used_tokens: int
    budget_tokens: int
    percent: float
    configured: bool

    def remaining_text(self) -> str:
        secs = max(0, int(self.remaining_seconds))
        days, rest = divmod(secs, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def countdown_text(self) -> str:
        """`2d 04:13:25` — at weekly scale the seconds matter little, but the
        format stays consistent with the session one."""
        secs = max(0, int(self.remaining_seconds))
        days, rest = divmod(secs, 86400)
        hours, rest = divmod(rest, 3600)
        minutes, seconds = divmod(rest, 60)
        clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{days}d {clock}" if days else clock

    def reset_at_text(self) -> str:
        # `%a` follows the system locale, so the weekday name is fixed here
        # instead of depending on the machine's language.
        end = datetime.fromtimestamp(self.end_ts)
        return f"{DAYS[end.weekday()]} {end.strftime('%d/%m %H:%M')}"


class WeeklyTracker:
    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config

    @property
    def window_seconds(self) -> float:
        return self._config.weekly_window_days * 86400.0

    # -- reset moment --------------------------------------------------

    def raw_reset_ts(self) -> float | None:
        raw = self._store.get_state(STATE_WEEKLY_RESET)
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    def set_reset_in(self, seconds_from_now: float, now: float | None = None) -> float:
        """Sets the cycle from the "Resets in …" the web shows."""
        now = time.time() if now is None else now
        return self.set_reset_at(now + max(0.0, seconds_from_now))

    def set_reset_at(self, reset_ts: float) -> float:
        """Sets the cycle from the exact instant ("Resets Thursday 3:00 AM")."""
        self._store.set_state(STATE_WEEKLY_RESET, repr(reset_ts))
        return reset_ts

    def next_reset(self, now: float | None = None) -> float | None:
        """Current reset, advanced in whole-week multiples if it already passed.

        It is computed, not iterated: if the app was closed for two months, a
        loop would give the same result but take time proportional to the
        downtime.
        """
        now = time.time() if now is None else now
        reset = self.raw_reset_ts()
        if reset is None:
            return None
        if now < reset:
            return reset
        periods = math.floor((now - reset) / self.window_seconds) + 1
        return reset + periods * self.window_seconds

    # -- read for the UI ------------------------------------------------

    def snapshot(self, now: float | None = None) -> WeeklySnapshot:
        now = time.time() if now is None else now
        reset = self.next_reset(now)
        budget = self._config.weekly_budget_tokens

        if reset is None:
            # Not configured: the window is shown but without a countdown or a %.
            start = now - self.window_seconds
            totals = self._store.totals_since(start, self._config.percent_scope)
            used = totals.basis(self._config.percent_basis)
            return WeeklySnapshot(
                now_ts=now, start_ts=start, end_ts=now,
                remaining_seconds=0.0, totals=totals, used_tokens=used,
                budget_tokens=budget, percent=0.0, configured=False,
            )

        start = reset - self.window_seconds
        totals = self._store.totals_since(start, self._config.percent_scope)
        used = totals.basis(self._config.percent_basis)
        return WeeklySnapshot(
            now_ts=now,
            start_ts=start,
            end_ts=reset,
            remaining_seconds=max(0.0, reset - now),
            totals=totals,
            used_tokens=used,
            budget_tokens=budget,
            percent=100.0 * used / max(1, budget),
            configured=True,
        )
