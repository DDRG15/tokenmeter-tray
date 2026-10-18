"""Notifications when usage crosses thresholds.

The rule that drives the design: **one notification per threshold and per
session**. The UI loop evaluates this every second; without memory of what was
already notified, crossing 60% would fire one notification per second for the
rest of the window.

The memory is tied to the session start, so when the window rolls over it
clears itself with no need to touch it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

STATE_KEY = "alerts_fired"


@dataclass(frozen=True)
class Alert:
    threshold: int
    title: str
    message: str


def _message(threshold: int, snapshot) -> Alert:
    remaining = max(0, snapshot.budget_tokens - snapshot.used_tokens)
    if threshold >= 100:
        return Alert(
            threshold,
            "TokenMeter · budget exhausted",
            f"You have passed 100% of the session ({snapshot.used_tokens:,} of "
            f"{snapshot.budget_tokens:,} tokens).\n"
            f"The window resets at {snapshot.reset_at_text()}.",
        )
    return Alert(
        threshold,
        f"TokenMeter · {threshold}% of the session",
        f"{snapshot.used_tokens:,} of {snapshot.budget_tokens:,} tokens. "
        f"{remaining:,} left.\n"
        f"The window resets in {snapshot.remaining_text()}.",
    )


class AlertTracker:
    """Decides which notifications are due, and remembers those already sent across restarts."""

    def __init__(self, store, thresholds: tuple[int, ...] = (60, 85, 100)) -> None:
        self._store = store
        self.thresholds = tuple(sorted(thresholds))

    # -- memory -----------------------------------------------------------

    def _fired(self) -> tuple[float, set[int]]:
        raw = self._store.get_state(STATE_KEY)
        if not raw:
            return (0.0, set())
        try:
            data = json.loads(raw)
            return (float(data["session_start_ts"]), {int(t) for t in data["thresholds"]})
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return (0.0, set())

    def _remember(self, session_start_ts: float, fired: set[int]) -> None:
        self._store.set_state(
            STATE_KEY,
            json.dumps({"session_start_ts": session_start_ts,
                        "thresholds": sorted(fired)}),
        )

    def reset(self) -> None:
        """Forgets what was notified. Called when the session is reset by hand."""
        self._store.set_state(STATE_KEY, "")

    # -- decision ----------------------------------------------------------

    def due(self, snapshot) -> list[Alert]:
        """Newly crossed thresholds. Returns [] on the vast majority of ticks."""
        if not snapshot.has_activity:
            return []

        previous_ts, fired = self._fired()
        # Different session: the memory of the previous one does not apply.
        if previous_ts != snapshot.start_ts:
            fired = set()

        crossed = {t for t in self.thresholds if snapshot.percent >= t}
        new = sorted(crossed - fired)
        if not new:
            if previous_ts != snapshot.start_ts:
                self._remember(snapshot.start_ts, fired)
            return []

        self._remember(snapshot.start_ts, fired | crossed)

        # If the app was closed and several thresholds were already crossed by
        # the time it came back, only the highest one is notified: three
        # balloons in a row are noise.
        return [_message(new[-1], snapshot)]
