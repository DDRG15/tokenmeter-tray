"""Session history: every call belongs to exactly one session.

A fixed clock everywhere; nothing sleeps or reads the real time.
"""

from __future__ import annotations

import random

import pytest

from tests.test_dashboard import app_without_tk
from tokenmeter import dashboard
from tokenmeter.config import Config
from tokenmeter.models import UsageEvent
from tokenmeter.official_limits import FIVE_HOUR, LimitNotice, record
from tokenmeter.session import (
    SOURCE_CLAUDE_AI, SOURCE_CLAUDE_CODE, SOURCE_ESTIMATED, STATE_MANUAL_RESET, SessionTracker,
)
from tokenmeter.store import Store

HOUR = 3600.0
T0 = 1_700_000_000.0  # a fixed instant in the past: the store refuses calls from the future


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "h.db")
    yield s
    s.close()


_seq = iter(range(10 ** 9))


def call(store, ts, tokens=100, provider="anthropic", billing="subscription", complete=True):
    store.add_event(UsageEvent(ts=ts, provider=provider, model="claude-opus-5", input_tokens=tokens,
                               output_tokens=0, source="transcript", dedupe_key=f"h:{next(_seq)}",
                               billing=billing, usage_complete=complete))


def spans(records):
    return [(r.start_ts - T0, r.end_ts - T0, r.used_tokens, r.source, r.active) for r in records]


def test_calls_are_split_into_the_sessions_they_fell_in(store):
    """A new session opens with the first call after the previous window ended.
    A call late inside a window belongs to it, however long the pause before."""
    tracker = SessionTracker(store, Config())
    for offset, tokens in [(0, 100), (1 * HOUR, 20), (4.9 * HOUR, 3),        # session 1
                           (5 * HOUR, 1000), (6 * HOUR, 500),               # session 2 starts at +5h sharp
                           (30 * HOUR, 7)]:                                 # session 3, a day later
        call(store, T0 + offset, tokens)
    now = T0 + 31 * HOUR
    assert spans(tracker.history(now)) == [
        (0, 5 * HOUR, 123, SOURCE_ESTIMATED, False),
        (5 * HOUR, 10 * HOUR, 1500, SOURCE_ESTIMATED, False),
        (30 * HOUR, 35 * HOUR, 7, SOURCE_ESTIMATED, True),
    ]


def test_a_call_exactly_at_the_end_of_a_window_opens_the_next_one(store):
    """[start, end): it must not be counted in both sessions."""
    tracker = SessionTracker(store, Config())
    call(store, T0, 1)
    call(store, T0 + 5 * HOUR, 10)
    first, second = tracker.history(T0 + 6 * HOUR)
    assert (first.used_tokens, second.used_tokens) == (1, 10)
    assert first.end_ts == second.start_ts == T0 + 5 * HOUR


def test_every_call_is_in_exactly_one_session(store):
    rng = random.Random(7)
    ts, total = T0, 0
    for _ in range(300):
        ts += rng.choice([30, 600, 2 * HOUR, 5 * HOUR, 9 * HOUR])
        tokens = rng.randint(1, 500)
        total += tokens
        call(store, ts, tokens)
    records = SessionTracker(store, Config()).history(ts + 60)
    assert sum(r.used_tokens for r in records) == total
    assert sum(r.totals.calls for r in records) == 300
    for earlier, later in zip(records, records[1:]):
        assert earlier.end_ts <= later.start_ts, "sessions never overlap"


def test_a_manual_reset_cuts_the_session_without_losing_its_calls(store):
    """What the user asked for: a reset must not matter to the history."""
    tracker = SessionTracker(store, Config())
    call(store, T0, 100)
    call(store, T0 + HOUR, 50)
    store.set_state(STATE_MANUAL_RESET, repr(T0 + 2 * HOUR))
    call(store, T0 + 3 * HOUR, 9)
    assert spans(tracker.history(T0 + 4 * HOUR)) == [
        (0, 2 * HOUR, 150, SOURCE_ESTIMATED, False),          # closed by the reset, calls kept
        (3 * HOUR, 8 * HOUR, 9, SOURCE_ESTIMATED, True),      # starts with the next call
    ]


def test_a_reported_window_starts_exactly_where_the_account_says(store):
    """The local estimate (first call at +1h) ends where the reported window
    begins, and the reported one carries its source."""
    tracker = SessionTracker(store, Config())
    call(store, T0 - 3 * HOUR, 40)                    # an earlier, estimated session
    call(store, T0 + 1 * HOUR, 100)
    record(store, LimitNotice(FIVE_HOUR, T0 + 5 * HOUR, T0 + 2 * HOUR))  # window T0 .. T0+5h
    assert spans(tracker.history(T0 + 2 * HOUR)) == [
        (-3 * HOUR, 0, 40, SOURCE_ESTIMATED, False),  # cut at the reported start, not at +2h
        (0, 5 * HOUR, 100, SOURCE_CLAUDE_CODE, True),
    ]


def test_a_window_copied_from_claude_ai_is_labelled_as_such(store):
    tracker = SessionTracker(store, Config())
    call(store, T0 + HOUR, 5)
    tracker.set_reset_from_web(T0 + 5 * HOUR)
    (only,) = tracker.history(T0 + 2 * HOUR)
    assert (only.start_ts, only.source) == (T0, SOURCE_CLAUDE_AI)


def test_a_reported_window_exists_even_before_its_first_local_call(store):
    """The account opened it (usage elsewhere); it is the current session."""
    tracker = SessionTracker(store, Config())
    record(store, LimitNotice(FIVE_HOUR, T0 + 5 * HOUR, T0 + 60))
    assert spans(tracker.history(T0 + 120)) == [(0, 5 * HOUR, 0, SOURCE_CLAUDE_CODE, True)]


def test_only_the_calls_that_count_toward_the_plan_make_sessions(store):
    """Same scope as the meter: an API-key call or another provider does not
    open a Claude session."""
    tracker = SessionTracker(store, Config())
    call(store, T0, 100, billing="api")
    call(store, T0 + 60, 100, provider="openai")
    assert tracker.history(T0 + 120) == []
    call(store, T0 + 90, 5)
    (only,) = tracker.history(T0 + 120)
    assert (only.start_ts, only.used_tokens) == (T0 + 90, 5)


def test_a_call_from_the_future_is_not_in_the_history_yet(store):
    tracker = SessionTracker(store, Config())
    call(store, T0, 1)
    call(store, T0 + 2 * HOUR, 10)
    assert spans(tracker.history(T0 + HOUR)) == [(0, 5 * HOUR, 1, SOURCE_ESTIMATED, True)]


def test_the_limit_keeps_the_newest_sessions(store):
    tracker = SessionTracker(store, Config())
    for i in range(6):
        call(store, T0 + i * 6 * HOUR, i + 1)
    now = T0 + 40 * HOUR
    assert [r.used_tokens for r in tracker.history(now, limit=2)] == [5, 6]
    assert tracker.history(now, limit=0) == []
    assert len(tracker.history(now)) == 6


def test_no_calls_no_sessions(store):
    assert SessionTracker(store, Config()).history(T0) == []


@pytest.mark.parametrize("seed", range(25))
def test_the_last_session_of_the_history_is_the_one_the_meter_shows(store, seed):
    """The meter and the history are the same walk. If they could disagree, the
    window on screen and the table under it would show different sessions."""
    rng = random.Random(seed)
    tracker = SessionTracker(store, Config())
    ts = T0
    for _ in range(rng.randint(0, 40)):
        ts += rng.choice([20, 900, 3 * HOUR, 5 * HOUR, 5 * HOUR + 1, 12 * HOUR])
        call(store, ts, rng.randint(1, 50))
    end = ts + rng.choice([1, HOUR, 4.99 * HOUR, 5 * HOUR, 20 * HOUR])
    if rng.random() < 0.5:
        store.set_state(STATE_MANUAL_RESET, repr(T0 + rng.uniform(0, end - T0)))
    if rng.random() < 0.5:
        tracker.set_reset_from_web(T0 + rng.uniform(0, end - T0 + 6 * HOUR))
    if rng.random() < 0.5:
        reported = T0 + rng.uniform(0, end - T0)
        record(store, LimitNotice(FIVE_HOUR, reported + rng.uniform(1, 5 * HOUR), reported))

    start, has_activity = tracker.anchor(end)
    history = tracker.history(end)
    current = [r for r in history if r.active]
    if has_activity:
        assert [r.start_ts for r in current] == [start]
        assert history[-1].active
        assert history[-1].used_tokens == tracker.snapshot(end).used_tokens
        assert history[-1].source == tracker.anchor_source(end)
    else:
        assert current == []


def test_the_table_data_is_newest_first_and_says_where_each_window_came_from(tmp_path):
    app = app_without_tk(tmp_path)
    call(app.store, T0, 30_000)
    call(app.store, T0 + 8 * HOUR, 60_000, complete=False)
    record(app.store, LimitNotice(FIVE_HOUR, T0 + 12 * HOUR, T0 + 8 * HOUR + 60))  # window +7h..+12h
    rows = dashboard.build_sessions(app, T0 + 9 * HOUR)
    assert [(r["used"], r["calls"], r["active"], r["source"], r["source_text"]) for r in rows] == [
        (60_000, 1, True, "claude_code", "from Claude Code"),
        (30_000, 1, False, "estimated", "estimated"),
    ]
    assert rows[0]["start_ts"] == T0 + 7 * HOUR and rows[0]["incomplete_calls"] == 1
    assert rows[0]["percent"] == pytest.approx(60.0) and rows[1]["percent"] == pytest.approx(30.0)
    assert dashboard.build_sessions(app, T0 + 9 * HOUR, limit=1) == rows[:1]


def test_a_reported_window_that_has_not_started_yet_is_not_history(store):
    """A reset copied with a start later than now describes the future."""
    tracker = SessionTracker(store, Config())
    call(store, T0, 5)
    tracker.set_reset_from_web(T0 + 8 * HOUR)  # window T0+3h .. T0+8h
    assert spans(tracker.history(T0 + HOUR)) == [(0, 5 * HOUR, 5, SOURCE_ESTIMATED, True)]


def test_a_manual_reset_after_a_reported_window_overrides_it(store):
    """The user cut the session after the account reported it: the reported
    start no longer opens a session, exactly as the meter treats it."""
    tracker = SessionTracker(store, Config())
    record(store, LimitNotice(FIVE_HOUR, T0 + 5 * HOUR, T0 + 60))  # window T0 .. T0+5h
    call(store, T0 + HOUR, 5)
    store.set_state(STATE_MANUAL_RESET, repr(T0 + 2 * HOUR))
    call(store, T0 + 3 * HOUR, 7)
    assert spans(tracker.history(T0 + 4 * HOUR)) == [
        (HOUR, 2 * HOUR, 5, SOURCE_ESTIMATED, False),
        (3 * HOUR, 8 * HOUR, 7, SOURCE_ESTIMATED, True),
    ]
