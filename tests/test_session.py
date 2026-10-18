from tokenmeter.config import Config
from tokenmeter.models import UsageEvent
from tokenmeter.session import SessionTracker
from tokenmeter.store import Store

HOUR = 3600.0
T0 = 1_700_000_000.0


def tracker(tmp_path, **cfg_kw):
    store = Store(tmp_path / "s.db")
    cfg = Config(session_window_hours=5.0, session_budget_tokens=1000, **cfg_kw)
    return store, SessionTracker(store, cfg)


def add(store, ts, key, inp=100, out=50, cw=0, cr=0):
    store.add_event(
        UsageEvent(
            ts=ts,
            provider="anthropic",
            model="claude-opus-5",
            input_tokens=inp,
            output_tokens=out,
            cache_write_tokens=cw,
            cache_read_tokens=cr,
            source="transcript",
            dedupe_key=key,
        )
    )


def test_session_anchors_at_first_event(tmp_path):
    store, tr = tracker(tmp_path)
    add(store, T0, "a")
    add(store, T0 + HOUR, "b")

    snap = tr.snapshot(now=T0 + 2 * HOUR)
    assert snap.start_ts == T0
    assert snap.has_activity
    assert snap.totals.calls == 2
    assert snap.remaining_seconds == 3 * HOUR
    assert snap.remaining_text() == "3h 0m"


def test_event_past_the_window_opens_a_new_session_anchored_at_that_event(tmp_path):
    store, tr = tracker(tmp_path)
    add(store, T0, "old")
    rollover_ts = T0 + 5 * HOUR + 1
    add(store, rollover_ts, "new")

    snap = tr.snapshot(now=rollover_ts + 60)
    assert snap.start_ts == rollover_ts
    assert snap.totals.calls == 1, "the event from the previous session must not be counted"


def test_backfilled_events_out_of_order_converge_to_same_anchor(tmp_path):
    """The tailer may insert files in any order after a startup."""
    store, tr = tracker(tmp_path)
    rollover_ts = T0 + 6 * HOUR
    add(store, rollover_ts, "new")  # arrives first
    add(store, T0, "old")  # later backfill, older

    snap = tr.snapshot(now=rollover_ts + 60)
    assert snap.start_ts == rollover_ts
    assert snap.totals.calls == 1


def test_expired_session_with_no_new_activity_reports_zero(tmp_path):
    store, tr = tracker(tmp_path)
    add(store, T0, "a")

    snap = tr.snapshot(now=T0 + 6 * HOUR)
    assert snap.has_activity is False
    assert snap.used_tokens == 0
    assert snap.percent == 0.0


def test_manual_reset_moves_window_and_keeps_history(tmp_path):
    store, tr = tracker(tmp_path)
    add(store, T0, "a")
    assert tr.snapshot(now=T0 + 60).totals.calls == 1

    store.set_state("manual_reset_ts", repr(T0 + 120))
    snap = tr.snapshot(now=T0 + 180)
    assert snap.used_tokens == 0
    assert store.totals_since(0).calls == 1, "the history is still in the database"

    add(store, T0 + 200, "b")
    assert tr.snapshot(now=T0 + 240).totals.calls == 1


def test_percent_basis_io_excludes_cache(tmp_path):
    store, tr = tracker(tmp_path, percent_basis="io")
    add(store, T0, "a", inp=100, out=50, cw=200, cr=5000)

    snap = tr.snapshot(now=T0 + 60)
    assert snap.used_tokens == 150
    assert snap.percent == 15.0


def test_percent_basis_all_includes_cache(tmp_path):
    store, tr = tracker(tmp_path, percent_basis="all")
    add(store, T0, "a", inp=100, out=50, cw=200, cr=5000)

    snap = tr.snapshot(now=T0 + 60)
    assert snap.used_tokens == 5350
    assert snap.over_budget is True


def test_countdown_and_reset_clock_time(tmp_path):
    from datetime import datetime

    store, tr = tracker(tmp_path)
    add(store, T0, "a")

    snap = tr.snapshot(now=T0 + HOUR + 48)  # 3h 59m 12s remaining
    assert snap.countdown_text() == "03:59:12"
    assert snap.end_ts == T0 + 5 * HOUR
    # The time shown is the user's local clock time, not UTC.
    assert snap.reset_at() == datetime.fromtimestamp(T0 + 5 * HOUR)
    assert snap.reset_at_text().startswith(
        datetime.fromtimestamp(T0 + 5 * HOUR).strftime("%H:%M:%S")
    )


def test_countdown_clamps_at_zero(tmp_path):
    store, tr = tracker(tmp_path)
    add(store, T0, "a")
    snap = tr.snapshot(now=T0 + 9 * HOUR)
    assert snap.remaining_seconds >= 0
    assert snap.countdown_text() == "05:00:00", "expired session: the new window is whole"


def test_by_model_empty_when_session_expired(tmp_path):
    store, tr = tracker(tmp_path)
    add(store, T0, "a")
    assert tr.by_model(now=T0 + 6 * HOUR) == []
    assert len(tr.by_model(now=T0 + 60)) == 1
