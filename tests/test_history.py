"""Per-day history: what is a zero and what is "no record".

Events are placed at local noon so that no clock change moves an event to
another day: what is tested is the grouping, not DST.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from tokenmeter.history import (
    DayBar,
    bar_at,
    bar_layout,
    compact,
    daily_series,
    local_midnight,
    summarize,
)
from tokenmeter.models import UsageEvent
from tokenmeter.store import Store

TODAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 18, 0).timestamp()


def noon(day: date) -> float:
    return datetime(day.year, day.month, day.day, 12, 0).timestamp()


def add_event(store: Store, day: date, io: int, key: str, *, cache: int = 0,
              complete: bool = True, hour: int = 12) -> None:
    ts = datetime(day.year, day.month, day.day, hour, 0).timestamp()
    store.add_event(UsageEvent(ts=ts, provider="anthropic", model="claude-opus-5",
                               input_tokens=io, output_tokens=0, cache_read_tokens=cache,
                               source="transcript", dedupe_key=key,
                               usage_complete=complete))


def test_days_are_grouped_by_local_calendar_day(tmp_path):
    store = Store(tmp_path / "h.db")
    yesterday = TODAY - timedelta(days=1)
    add_event(store, yesterday, 100, "a", hour=0)    # first minute of the day
    add_event(store, yesterday, 200, "b", hour=23)   # last hour of the same day
    add_event(store, TODAY, 50, "c")

    bars = daily_series(store, 2, now=NOW)
    assert [(b.day, b.tokens, b.calls) for b in bars] == [(yesterday, 300, 2), (TODAY, 50, 1)]


def test_a_quiet_observed_day_is_zero(tmp_path):
    """A day without calls is 0 when the collector observed all of it."""
    store = Store(tmp_path / "h.db")
    add_event(store, TODAY - timedelta(days=3), 10, "a")
    add_event(store, TODAY, 10, "b")
    store.mark_covered(local_midnight(TODAY - timedelta(days=3)), NOW, source="transcript")
    bars = daily_series(store, 4, now=NOW, expected_sources=("transcript",))
    assert [b.tokens for b in bars] == [10, 0, 0, 10]


def test_days_before_the_first_event_are_unknown_not_zero(tmp_path):
    """Before the first event the database knows nothing: pruned or not yet installed."""
    store = Store(tmp_path / "h.db")
    add_event(store, TODAY - timedelta(days=1), 10, "a")
    store.mark_covered(local_midnight(TODAY - timedelta(days=1)), NOW, source="transcript")
    bars = daily_series(store, 5, now=NOW, expected_sources=("transcript",))
    assert [b.tokens for b in bars] == [None, None, None, 10, 0]
    assert summarize(bars).unknown_days == 3


def test_an_empty_store_is_all_unknown(tmp_path):
    bars = daily_series(Store(tmp_path / "h.db"), 7, now=NOW)
    assert len(bars) == 7 and not any(b.known for b in bars)
    assert bars[-1].day == TODAY


def test_the_basis_follows_the_percentage_setting(tmp_path):
    store = Store(tmp_path / "h.db")
    add_event(store, TODAY, 100, "a", cache=5000)
    assert daily_series(store, 1, "io", now=NOW)[0].tokens == 100
    assert daily_series(store, 1, "all", now=NOW)[0].tokens == 5100


def test_summary_ignores_unknown_days_and_finds_the_peak():
    d = TODAY
    bars = [DayBar(d - timedelta(days=3), None), DayBar(d - timedelta(days=2), 0),
            DayBar(d - timedelta(days=1), 900, 3, 1), DayBar(d, 300, 1)]
    s = summarize(bars)
    assert (s.total_tokens, s.total_calls, s.active_days, s.known_days) == (1200, 4, 2, 3)
    assert (s.peak_day, s.peak_tokens) == (d - timedelta(days=1), 900)
    assert s.average_per_active_day == 600
    assert s.incomplete_calls == 1


def test_summary_of_nothing_does_not_divide_by_zero():
    s = summarize([DayBar(TODAY, None)])
    assert s.average_per_active_day == 0 and s.peak_day is None


def test_layout_scales_to_the_peak_and_keeps_small_days_visible():
    bars = [DayBar(TODAY, None), DayBar(TODAY, 0), DayBar(TODAY, 2_000), DayBar(TODAY, 4_000_000)]
    rects = bar_layout(bars, width=400, height=100)
    assert rects[0] is None                       # no record: not drawn
    assert rects[1][1] == rects[1][3] == 100      # zero: height 0
    assert rects[2][3] - rects[2][1] == 1.0       # small but visible
    assert rects[3][1] == 0.0                     # the peak fills the height
    assert all(r is None or r[0] < r[2] for r in rects)


def test_bar_under_the_pointer():
    assert bar_at(0, 30, 300) == 0
    assert bar_at(299.9, 30, 300) == 29
    assert bar_at(150, 30, 300) == 15
    assert bar_at(-1, 30, 300) is None
    assert bar_at(300, 30, 300) is None
    assert bar_at(10, 0, 300) is None


def test_compact_axis_labels():
    assert compact(950) == "950"
    assert compact(9_999) == "9,999"
    assert compact(350_000) == "350k"
    assert compact(1_362_129) == "1.4M"


def test_local_midnight_is_the_start_of_the_day():
    assert datetime.fromtimestamp(local_midnight(TODAY)) == datetime(2026, 10, 12)
