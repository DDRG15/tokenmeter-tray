"""The reset time from claude.ai anchors the session and the week.

The real case that motivated it, on 15/10 at 21:41: the web said "Current session
· Resets Fri 12:40 AM" and "This week · Resets Thursday 3:00 AM"; TokenMeter
opened the session at 21:30 with Claude Code's first call and anchored the week
on a Thursday at 22:11 inherited from August.

The parser also accepts Spanish weekday names and durations ("jue", "en 3
horas"), because users paste from claude.ai in either language.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tokenmeter.anchors import AnchorError, parse_reset
from tokenmeter.config import Config
from tokenmeter.models import UsageEvent
from tokenmeter.session import SessionTracker
from tokenmeter.store import Store
from tokenmeter.weekly import WeeklyTracker

NOW = datetime(2026, 10, 15, 21, 41).timestamp()  # Thursday
FIVE_H = 5 * 3600
SEVEN_D = 7 * 86400


def at(text: str, scope: float = FIVE_H) -> datetime:
    return datetime.fromtimestamp(parse_reset(text, NOW, scope))


@pytest.mark.parametrize("text", ["Resets Fri 12:40 AM", "Fri 12:40 AM", "fri 12:40am",
                                  "00:40", "0:40", "vie 00:40", "12:40 a.m."])
def test_session_time_copied_from_the_web(text):
    assert at(text) == datetime(2026, 10, 16, 0, 40)


@pytest.mark.parametrize("text", ["Resets Thursday 3:00 AM", "Thursday 3:00 AM",
                                  "jueves 03:00", "thu 3 am"])
def test_weekly_time_on_the_same_weekday_falls_next_week(text):
    """Today is Thursday at 21:41: "Thursday 3:00" is the one seven days from now."""
    assert at(text, SEVEN_D) == datetime(2026, 10, 22, 3, 0)


@pytest.mark.parametrize("text,minutes", [("in 4 hr 25 min", 265), ("4h 25m", 265),
                                          ("en 3 horas", 180), ("2.5", 150)])
def test_countdowns_and_bare_hours(text, minutes):
    assert parse_reset(text, NOW, FIVE_H) == pytest.approx(NOW + minutes * 60)


def test_days_in_the_weekly_countdown():
    assert parse_reset("2d 4h", NOW, SEVEN_D) == pytest.approx(NOW + 52 * 3600)


@pytest.mark.parametrize("text", ["", "tomorrow", "25:00", "13:00 PM", "Thursday 3:00 AM"])
def test_what_does_not_fit_is_rejected(text):
    """"Thursday 3:00 AM" as a SESSION reset is 6 days away: copied from the wrong row."""
    with pytest.raises(AnchorError):
        parse_reset(text, NOW, FIVE_H)


# -- the anchored session --------------------------------------------------------


def tracker(tmp_path):
    store = Store(tmp_path / "usage.db")
    return store, SessionTracker(store, Config(session_budget_tokens=100_000))


def call(store, ts, key):
    store.add_event(UsageEvent(ts=ts, provider="anthropic", model="claude-opus-5",
                               input_tokens=1_000, output_tokens=0, source="transcript",
                               dedupe_key=key))


def test_without_an_anchor_the_session_starts_with_the_first_local_call(tmp_path):
    store, s = tracker(tmp_path)
    call(store, datetime(2026, 10, 15, 21, 30).timestamp(), "a")
    start, active = s.anchor(NOW)
    assert active and datetime.fromtimestamp(start) == datetime(2026, 10, 15, 21, 30)


def test_with_an_anchor_the_session_starts_when_the_web_started_it(tmp_path):
    store, s = tracker(tmp_path)
    call(store, datetime(2026, 10, 15, 21, 30).timestamp(), "a")
    s.set_reset_from_web(parse_reset("Fri 12:40 AM", NOW, FIVE_H))

    snap = s.snapshot(NOW)
    assert datetime.fromtimestamp(snap.start_ts) == datetime(2026, 10, 15, 19, 40)
    assert snap.reset_at() == datetime(2026, 10, 16, 0, 40)
    assert snap.used_tokens == 1_000  # the local call falls inside the web window


def test_the_anchor_counts_as_an_active_session_even_without_local_calls(tmp_path):
    """The web says the session is running: TokenMeter cannot claim otherwise."""
    _, s = tracker(tmp_path)
    s.set_reset_from_web(parse_reset("Fri 12:40 AM", NOW, FIVE_H))
    start, active = s.anchor(NOW)
    assert active and datetime.fromtimestamp(start) == datetime(2026, 10, 15, 19, 40)


def test_when_the_web_window_expires_local_data_rules_again(tmp_path):
    store, s = tracker(tmp_path)
    s.set_reset_from_web(parse_reset("Fri 12:40 AM", NOW, FIVE_H))
    later = datetime(2026, 10, 16, 2, 0).timestamp()
    call(store, later, "b")
    start, active = s.anchor(later + 60)
    assert active and start == later


def test_a_later_manual_reset_beats_the_anchor(tmp_path, monkeypatch):
    store, s = tracker(tmp_path)
    s.set_reset_from_web(parse_reset("Fri 12:40 AM", NOW, FIVE_H))
    monkeypatch.setattr("tokenmeter.session.time.time", lambda: NOW)
    s.reset_now()
    start, active = s.anchor(NOW + 60)
    assert not active


def test_week_anchored_to_the_exact_reset(tmp_path):
    store = Store(tmp_path / "usage.db")
    week = WeeklyTracker(store, Config())
    week.set_reset_at(parse_reset("Thursday 3:00 AM", NOW, SEVEN_D))
    snap = week.snapshot(NOW)
    assert snap.reset_at_text() == "Thu 22/10 03:00"
    # The week started this morning: anything before 03:00 does not count.
    assert datetime.fromtimestamp(snap.start_ts) == datetime(2026, 10, 15, 3, 0)
