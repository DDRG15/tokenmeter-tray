"""Weekly window."""

from tokenmeter.config import Config
from tokenmeter.models import UsageEvent
from tokenmeter.store import Store
from tokenmeter.weekly import WeeklyTracker

DAY = 86400.0
WEEK = 7 * DAY
T0 = 1_700_000_000.0


def tracker(tmp_path, **cfg):
    store = Store(tmp_path / "w.db")
    settings = {"weekly_budget_tokens": 1_000_000, "percent_basis": "io", **cfg}
    return store, WeeklyTracker(store, Config(**settings))


def add(store, ts, key, inp=1000, out=500):
    store.add_event(UsageEvent(ts=ts, provider="anthropic", model="claude-opus-5",
                               input_tokens=inp, output_tokens=out,
                               source="transcript", dedupe_key=key))


def test_without_configuring_there_is_no_countdown(tmp_path):
    store, tr = tracker(tmp_path)
    snap = tr.snapshot(now=T0)
    assert snap.configured is False
    assert snap.percent == 0.0
    assert snap.remaining_seconds == 0.0


def test_setting_the_reset_from_what_the_web_says(tmp_path):
    """claude.ai says "Resets in 13 hr 35 min"."""
    store, tr = tracker(tmp_path)
    tr.set_reset_in(13 * 3600 + 35 * 60, now=T0)

    snap = tr.snapshot(now=T0)
    assert snap.configured is True
    assert snap.remaining_seconds == 13 * 3600 + 35 * 60
    assert snap.remaining_text() == "13h 35m"
    assert snap.end_ts == T0 + 13 * 3600 + 35 * 60
    assert snap.start_ts == snap.end_ts - WEEK


def test_the_window_covers_exactly_seven_days_back(tmp_path):
    store, tr = tracker(tmp_path)
    reset = tr.set_reset_in(DAY, now=T0)

    add(store, reset - WEEK + 1, "just_inside")
    add(store, reset - WEEK - 1, "just_outside")
    add(store, T0, "now")

    snap = tr.snapshot(now=T0)
    assert snap.totals.calls == 2
    assert snap.used_tokens == 3000


def test_the_cycle_rolls_forward_on_its_own(tmp_path):
    store, tr = tracker(tmp_path)
    reset = tr.set_reset_in(DAY, now=T0)

    # Right after the reset, the new window starts at the previous reset.
    snap = tr.snapshot(now=reset + 60)
    assert snap.end_ts == reset + WEEK
    assert snap.start_ts == reset


def test_rolling_forward_after_a_long_shutdown_is_computed_not_iterated(tmp_path):
    """Two months switched off: the result cannot depend on the time closed."""
    store, tr = tracker(tmp_path)
    reset = tr.set_reset_in(DAY, now=T0)

    later = reset + 9 * WEEK + 3 * DAY
    snap = tr.snapshot(now=later)
    assert snap.end_ts == reset + 10 * WEEK
    assert snap.start_ts == snap.end_ts - WEEK
    assert snap.start_ts <= later < snap.end_ts


def test_the_stored_reset_is_not_rewritten_by_reading(tmp_path):
    """`next_reset` computes; only `set_reset_in` writes."""
    store, tr = tracker(tmp_path)
    reset = tr.set_reset_in(DAY, now=T0)
    tr.snapshot(now=reset + 5 * WEEK)
    assert tr.raw_reset_ts() == reset


def test_percentage_uses_the_weekly_budget(tmp_path):
    store, tr = tracker(tmp_path)
    tr.set_reset_in(DAY, now=T0)
    for i in range(100):
        add(store, T0 - i * 60, f"k{i}")  # 100 × 1500 = 150,000 tokens

    snap = tr.snapshot(now=T0)
    assert snap.used_tokens == 150_000
    assert snap.percent == 15.0


def test_countdown_shows_days_when_there_are_days(tmp_path):
    store, tr = tracker(tmp_path)
    tr.set_reset_in(2 * DAY + 4 * 3600 + 13 * 60 + 25, now=T0)

    snap = tr.snapshot(now=T0)
    assert snap.countdown_text() == "2d 04:13:25"
    assert snap.remaining_text() == "2d 4h"


def test_countdown_drops_the_day_when_under_24h(tmp_path):
    store, tr = tracker(tmp_path)
    tr.set_reset_in(4 * 3600 + 13 * 60 + 25, now=T0)
    assert tr.snapshot(now=T0).countdown_text() == "04:13:25"


def test_a_corrupt_stored_reset_is_treated_as_unconfigured(tmp_path):
    store, tr = tracker(tmp_path)
    store.set_state("weekly_reset_ts", "i am not a number")
    assert tr.raw_reset_ts() is None
    assert tr.snapshot(now=T0).configured is False


def test_weekly_shares_the_percent_basis_with_the_session(tmp_path):
    store, tr = tracker(tmp_path, percent_basis="all")
    tr.set_reset_in(DAY, now=T0)
    store.add_event(UsageEvent(ts=T0, provider="anthropic", model="claude-opus-5",
                               input_tokens=1000, output_tokens=500,
                               cache_read_tokens=98_500,
                               source="transcript", dedupe_key="k"))
    assert tr.snapshot(now=T0).used_tokens == 100_000
