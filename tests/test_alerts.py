"""Threshold alerts."""

from tokenmeter.alerts import AlertTracker
from tokenmeter.session import Snapshot
from tokenmeter.store import Store, Totals


def snap(percent, *, start_ts=1000.0, has_activity=True, used=None, budget=100_000):
    used = int(budget * percent / 100) if used is None else used
    return Snapshot(
        now_ts=start_ts + 60,
        start_ts=start_ts,
        end_ts=start_ts + 18000,
        remaining_seconds=17940.0,
        totals=Totals(input_tokens=used, calls=1),
        used_tokens=used,
        budget_tokens=budget,
        percent=percent,
        percent_basis="io",
        has_activity=has_activity,
    )


def tracker(tmp_path):
    return AlertTracker(Store(tmp_path / "a.db"))


def test_no_alert_below_the_first_threshold(tmp_path):
    assert tracker(tmp_path).due(snap(59.9)) == []


def test_crossing_a_threshold_fires_once(tmp_path):
    t = tracker(tmp_path)
    alerts = t.due(snap(60.1))
    assert [a.threshold for a in alerts] == [60]
    assert "60%" in alerts[0].title

    # The UI loop evaluates every second: it must not repeat.
    assert t.due(snap(61)) == []
    assert t.due(snap(70)) == []


def test_each_threshold_fires_on_its_own(tmp_path):
    t = tracker(tmp_path)
    assert [a.threshold for a in t.due(snap(62))] == [60]
    assert [a.threshold for a in t.due(snap(86))] == [85]
    assert [a.threshold for a in t.due(snap(101))] == [100]
    assert t.due(snap(120)) == []


def test_several_thresholds_at_once_only_warn_about_the_highest(tmp_path):
    """When opening the app with 95% already spent, three balloons would be noise."""
    alerts = tracker(tmp_path).due(snap(95))
    assert [a.threshold for a in alerts] == [85]


def test_a_new_session_starts_warning_again(tmp_path):
    t = tracker(tmp_path)
    assert t.due(snap(90, start_ts=1000.0))
    assert t.due(snap(90, start_ts=1000.0)) == []

    # New window: the memory of the previous one does not apply.
    assert [a.threshold for a in t.due(snap(90, start_ts=99_000.0))] == [85]


def test_manual_reset_clears_the_memory(tmp_path):
    t = tracker(tmp_path)
    assert t.due(snap(70))
    t.reset()
    assert [a.threshold for a in t.due(snap(70))] == [60]


def test_no_alerts_without_an_active_session(tmp_path):
    assert tracker(tmp_path).due(snap(90, has_activity=False)) == []


def test_memory_survives_a_restart(tmp_path):
    store = Store(tmp_path / "a.db")
    assert AlertTracker(store).due(snap(70))
    # Another process, same database: it must not warn again.
    assert AlertTracker(store).due(snap(70)) == []


def test_corrupt_memory_does_not_crash(tmp_path):
    store = Store(tmp_path / "a.db")
    store.set_state("alerts_fired", "i am not json")
    assert [a.threshold for a in AlertTracker(store).due(snap(70))] == [60]


def test_the_hundred_percent_message_says_when_it_resets(tmp_path):
    alert = tracker(tmp_path).due(snap(100))[0]
    assert "exhausted" in alert.title
    assert "resets at" in alert.message


def test_intermediate_messages_say_how_much_is_left(tmp_path):
    alert = tracker(tmp_path).due(snap(60, budget=100_000))[0]
    assert "40,000" in alert.message
