"""Per-day history over the events the database already stores.

Everything here is pure or nearly so: the Tk window (`ui/history.py`) only draws
what these functions decide, so the logic can be tested without a display.

The decision that matters is what to draw for a day with no events:

* **0** if some collector was observing that day (`coverage` table): it was
  there and there were no calls;
* **no record** (`None`) if none was observing: the app was closed, the day was
  pruned or it predates the installation. Drawing a zero there would claim the
  model was not used, and that is not known.

Databases without recorded coverage (from before the `coverage` table existed)
use the older rule: 0 from the first stored event onward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import time

from .store import Store

RANGES_DAYS = (7, 30, 90)


@dataclass(frozen=True)
class DayBar:
    day: date
    tokens: int | None  # None = no record, not zero
    calls: int = 0
    incomplete_calls: int = 0
    coverage: str = "unknown"
    observed_seconds: float = 0.0

    @property
    def known(self) -> bool:
        return self.tokens is not None


@dataclass(frozen=True)
class HistorySummary:
    total_tokens: int
    total_calls: int
    active_days: int
    known_days: int
    unknown_days: int
    peak_day: date | None
    peak_tokens: int
    incomplete_calls: int

    @property
    def average_per_active_day(self) -> int:
        return self.total_tokens // self.active_days if self.active_days else 0


def local_midnight(day: date) -> float:
    return datetime(day.year, day.month, day.day).timestamp()


def daily_series(
    store: Store, days: int, percent_basis: str = "io", now: float | None = None,
    provider: str = "", source: str = "", expected_sources: tuple[str, ...] = ()
) -> list[DayBar]:
    """The last `days` calendar days, today included, oldest to newest."""
    now = time.time() if now is None else now
    today = date.fromtimestamp(now)
    start = today - timedelta(days=days - 1)
    per_day = store.daily_totals(local_midnight(start), provider, source)

    first_ts = store.first_event_ts_after(0)
    first_day = date.fromtimestamp(first_ts) if first_ts is not None else None

    # With recorded coverage, a day without events is 0 only if some collector
    # was observing it. Without it —databases from before coverage existed— the
    # older rule is kept: 0 from the first event onward.
    details = store.coverage_details(local_midnight(start), now)

    def coverage(day):
        a, b = local_midnight(day), min(now, local_midnight(day + timedelta(days=1)))
        if b <= a:
            return "unknown", 0.0
        required = (source,) if source else expected_sources
        by_source = {}
        for item in details:
            left, right = max(a, item["start"]), min(b, item["end"])
            if right > left:
                # Coverage from the first scan (`*_archive`) belongs to the same
                # collector: it comes from files that are still on disk.
                name = item["source"].removesuffix("_archive")
                by_source.setdefault(name, []).append((left, right))
        def duration(intervals):
            total, end = 0.0, a
            for left, right in sorted(intervals):
                total += max(0.0, right - max(end, left))
                end = max(end, right)
            return total
        observed = duration([i for items in by_source.values() for i in items])
        # A different collector does not prove the absence of usage in the others.
        complete = bool(required) and all(duration(by_source.get(s, [])) >= b-a-1 for s in required)
        return ("complete" if complete else "partial" if observed else "unknown"), observed

    bars: list[DayBar] = []
    for i in range(days):
        day = start + timedelta(days=i)
        totals = per_day.get(day.isoformat())
        status, observed = coverage(day)
        if totals is None:
            bars.append(DayBar(day, 0 if status == "complete" else None,
                               coverage=status, observed_seconds=observed))
        else:
            bars.append(DayBar(day, totals.basis(percent_basis), totals.calls,
                               totals.incomplete_calls, status if status != "unknown" else "partial", observed))
    return bars


def summarize(bars: list[DayBar]) -> HistorySummary:
    known = [b for b in bars if b.known]
    active = [b for b in known if b.tokens]
    peak = max(active, key=lambda b: b.tokens, default=None)
    return HistorySummary(
        total_tokens=sum(b.tokens for b in known),
        total_calls=sum(b.calls for b in known),
        active_days=len(active),
        known_days=len(known),
        unknown_days=len(bars) - len(known),
        peak_day=peak.day if peak else None,
        peak_tokens=peak.tokens if peak else 0,
        incomplete_calls=sum(b.incomplete_calls for b in known),
    )


def bar_layout(
    bars: list[DayBar], width: int, height: int, gap: int = 2
) -> list[tuple[float, float, float, float] | None]:
    """Rectangles `(x0, y0, x1, y1)` of each bar; `None` for "no record".

    The scale is the maximum of the range. A day with tokens never ends up at
    0 px: with a peak of millions, a 2,000-token day would round to nothing and
    look like a day without usage.
    """
    if not bars:
        return []
    slot = width / len(bars)
    peak = max((b.tokens for b in bars if b.tokens), default=0)
    out: list[tuple[float, float, float, float] | None] = []
    for i, bar in enumerate(bars):
        if not bar.known:
            out.append(None)
            continue
        x0 = i * slot + gap / 2
        x1 = max(x0 + 1, (i + 1) * slot - gap / 2)
        h = 0.0 if not bar.tokens or not peak else max(1.0, height * bar.tokens / peak)
        out.append((x0, height - h, x1, float(height)))
    return out


def bar_at(x: float, count: int, width: int) -> int | None:
    """Index of the bar under the pointer, or None outside the chart."""
    if count <= 0 or width <= 0 or x < 0 or x >= width:
        return None
    return min(count - 1, int(x * count / width))


def compact(n: int) -> str:
    """350000 → "350k", 1362129 → "1.4M". For axes, not for the detail.

    Same numeric format as the panel (`{:,}`), so two conventions are not mixed
    in the same interface.
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n // 1000}k"
    return f"{n:,}"
