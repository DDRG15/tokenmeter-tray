"""Cross-component contracts for the existing session and weekly budgets."""

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tokenmeter.anchors import parse_reset
from tokenmeter.config import Config
from tokenmeter.models import UsageEvent
from tokenmeter.operations import calibrate_session, calibrate_week
from tokenmeter.runtime import Runtime

HOUR = 3600.0
DAY = 86400.0
NOW = 1_700_000_000.0


@dataclass
class Clock:
    now: float = NOW


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """Use the real runtime/store/config, without starting any collectors."""
    clock = Clock()
    monkeypatch.setattr("time.time", lambda: clock.now)
    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path))
    runtimes = []

    def open_runtime(**settings):
        if settings or not Config.path().exists():
            values = dict(
                session_budget_tokens=1000,
                weekly_budget_tokens=1000,
                proxy_enabled=False,
                transcripts_enabled=False,
                codex_enabled=False,
                transcripts_root=str(tmp_path / "unused-claude"),
                codex_root=str(tmp_path / "unused-codex"),
            )
            values.update(settings)
            Config(**values).save()
        runtime = Runtime(Config.load())
        runtimes.append(runtime)
        return runtime

    yield SimpleNamespace(clock=clock, open=open_runtime, home=tmp_path)
    for runtime in reversed(runtimes):
        runtime.shutdown()


def call(runtime, ts, key, *, inp=100, out=50, **fields):
    values = dict(
        ts=ts,
        provider="anthropic",
        model="synthetic-model",
        input_tokens=inp,
        output_tokens=out,
        billing="subscription",
        source="transcript",
        dedupe_key=key,
    )
    values.update(fields)
    assert runtime.store.add_event(UsageEvent(**values))


def persisted_state(runtime):
    with closing(sqlite3.connect(runtime.store.path)) as connection:
        return dict(connection.execute("SELECT key, value FROM state"))


def saved_config():
    return json.loads(Config.path().read_text(encoding="utf-8"))


def test_idle_snapshots_do_not_persist_an_inferred_session_anchor(legacy):
    runtime = legacy.open()
    config_before = Config.path().read_bytes()

    first = runtime.snapshot()
    legacy.clock.now += HOUR
    later = runtime.snapshot()

    assert (first.start_ts, first.end_ts) == (NOW, NOW + 5 * HOUR)
    assert (later.start_ts, later.end_ts) == (NOW + HOUR, NOW + 6 * HOUR)
    assert not first.has_activity and not later.has_activity
    assert first.used_tokens == later.used_tokens == 0
    assert persisted_state(runtime) == {}
    assert Config.path().read_bytes() == config_before

    call(runtime, legacy.clock.now - 60, "first-local-call")
    runtime.shutdown()
    reopened = legacy.open()
    assert reopened.snapshot().start_ts == NOW + HOUR - 60
    assert reopened.snapshot().used_tokens == 150
    assert persisted_state(reopened) == {}


def test_continuous_calls_open_a_new_window_at_the_exact_five_hour_boundary(legacy):
    runtime = legacy.open()
    legacy.clock.now = NOW + 5 * HOUR
    call(runtime, NOW, "start")
    call(runtime, NOW + 4 * HOUR, "still-first")
    call(runtime, NOW + 5 * HOUR, "exact-boundary", inp=200, out=0)

    snapshot = runtime.snapshot()
    # A window lasts 5 h from its first call, as on claude.ai; an idle gap does not end it.
    assert snapshot.start_ts == NOW + 5 * HOUR
    assert snapshot.end_ts == NOW + 10 * HOUR
    assert snapshot.remaining_seconds == 5 * HOUR
    assert snapshot.used_tokens == 200
    assert snapshot.totals.calls == 1
    assert runtime.store.totals_since(0).calls == 3


@pytest.mark.parametrize(
    "late_offset, expected_start, expected_used",
    [
        pytest.param(-HOUR, NOW - HOUR, 240, id="earlier-inside-window"),
        pytest.param(HOUR, NOW, 240, id="later-inside-window"),
        pytest.param(-6 * HOUR, NOW, 150, id="previous-window"),
    ],
)
def test_late_calls_rebuild_the_anchor_and_survive_reopening(
    legacy, late_offset, expected_start, expected_used
):
    legacy.clock.now = NOW + 2 * HOUR
    runtime = legacy.open()
    call(runtime, NOW, "arrived-first")
    assert runtime.snapshot().start_ts == NOW

    call(runtime, NOW + late_offset, "arrived-late", inp=90, out=0)
    snapshot = runtime.snapshot()
    assert snapshot.start_ts == expected_start
    assert snapshot.used_tokens == expected_used
    assert snapshot.has_activity

    runtime.shutdown()
    reopened = legacy.open()
    assert reopened.snapshot() == snapshot
    assert reopened.store.totals_since(0).calls == 2


def test_a_late_call_can_move_the_last_window_into_the_past(legacy):
    legacy.clock.now = NOW + 2 * HOUR
    runtime = legacy.open()
    call(runtime, NOW, "initial")
    assert runtime.snapshot().has_activity

    call(runtime, NOW - 4 * HOUR, "late-earlier-start")
    snapshot = runtime.snapshot()
    # The late call is real: the session began 4 h earlier and has already ended,
    # so nothing is active now. The history keeps both calls.
    assert not snapshot.has_activity
    assert snapshot.used_tokens == 0
    assert snapshot.start_ts == legacy.clock.now
    assert runtime.store.totals_since(0).calls == 2


def test_manual_reset_persists_the_floor_without_erasing_history_or_other_state(legacy):
    runtime = legacy.open()
    call(runtime, NOW - 60, "before-reset", inp=700, out=0)
    reset = parse_reset("Resets in 3 hr", NOW, runtime.config.window_seconds)
    web_start = runtime.session.set_reset_from_web(reset)
    weekly_reset = runtime.weekly.set_reset_in(DAY)
    assert [alert.threshold for alert in runtime.alerts.due(runtime.snapshot())] == [60]
    alerts_before = runtime.store.get_state("alerts_fired")

    runtime.session.reset_now()
    assert persisted_state(runtime) == {
        "manual_reset_ts": repr(NOW),
        "session_web_start_ts": repr(web_start),
        "weekly_reset_ts": repr(weekly_reset),
        # When the user set it: a reset reported later by Claude Code replaces it.
        "weekly_reset_set_at": repr(NOW),
        "alerts_fired": alerts_before,
    }
    assert not runtime.snapshot().has_activity
    assert runtime.weekly_snapshot().used_tokens == 700
    assert runtime.store.totals_since(0).calls == 1

    # reset_now does not touch the alert memory: both shells call alerts.reset()
    # themselves, and a new session start clears it anyway.
    runtime.shutdown()
    reopened = legacy.open()
    assert not reopened.snapshot().has_activity
    legacy.clock.now += 60
    call(reopened, legacy.clock.now, "after-reset")
    assert reopened.snapshot().start_ts == NOW + 60
    assert reopened.snapshot().used_tokens == 150
    assert reopened.session.web_start_ts() == web_start
    assert reopened.store.get_state("alerts_fired") == alerts_before


def test_reset_text_and_single_point_calibration_persist_in_different_places(legacy):
    runtime = legacy.open()
    call(runtime, NOW - 60, "counted")
    reset = parse_reset("Resets in 3 hr", NOW, runtime.config.window_seconds)
    start = runtime.session.set_reset_from_web(reset)
    calibrate_session(runtime, 25)

    assert persisted_state(runtime) == {"session_web_start_ts": repr(NOW - 2 * HOUR)}
    config = saved_config()
    expected = {
        "session_budget_tokens": 600,
        "percent_basis": "io",
        "percent_scope": "claude",
        "calibrated_at": NOW,
        "calibrated_from_percent": 25,
        "calibrated_tokens": 150,
        "calibrated_session_start": start,
        "calibrated_unseen_tokens": 0,
        "calibrated_scope": "claude",
    }
    assert {key: config[key] for key in expected} == expected
    assert not {"manual_reset_ts", "session_web_start_ts", "weekly_reset_ts", "alerts_fired"} & config.keys()
    row = runtime.store.calibrations()[0]
    assert (row["ts"], row["session_start_ts"], row["scope"], row["basis"]) == (
        NOW, start, "claude", "io"
    )
    assert (row["counted_tokens"], row["budget_tokens"], row["predicted_percent"]) == (
        150, 600, None
    )

    runtime.shutdown()
    snapshot = legacy.open().snapshot()
    assert (snapshot.start_ts, snapshot.end_ts, snapshot.percent) == (start, reset, 25)


def test_literal_resets_at_prefix_is_accepted(legacy):
    # claude.ai writes "Resets at <time>": the connector word is stripped like "in".
    reset = parse_reset("Resets at 15:00", legacy.clock.now, 24 * HOUR)
    assert reset > legacy.clock.now


@pytest.mark.parametrize(
    "next_call, expected_active, expected_start",
    [
        pytest.param(None, False, None, id="no-call-after-the-web-window"),
        pytest.param(-HOUR, False, None, id="late-call-inside-the-expired-window"),
        pytest.param(10 * 60, True, 10 * 60, id="first-call-after-the-web-window"),
    ],
)
def test_an_expired_web_window_keeps_its_calls_and_the_next_session_starts_after_it(
    legacy, next_call, expected_active, expected_start
):
    """claude.ai said the session runs [start, reset). Once it ends, its calls stay
    in it: the next session starts with the first call after the reset, so the
    meter does not keep showing the old session's usage."""
    runtime = legacy.open()
    call(runtime, NOW - 60, "inside-web-window")
    reset = parse_reset("Resets in 3 hr", NOW, runtime.config.window_seconds)
    start = runtime.session.set_reset_from_web(reset)
    assert runtime.snapshot().start_ts == start

    legacy.clock.now = reset + 30 * 60
    if next_call is not None:
        call(runtime, reset + next_call, "after-or-late", inp=40, out=0)
    snapshot = runtime.snapshot()
    assert snapshot.has_activity is expected_active
    if expected_active:
        assert snapshot.start_ts == reset + expected_start
        assert snapshot.used_tokens == 40
    else:
        assert snapshot.start_ts == legacy.clock.now
        assert snapshot.used_tokens == 0
    assert runtime.store.get_state("session_web_start_ts") == repr(start)


def test_multipoint_calibration_reopens_with_the_fit_not_the_last_row_budget(legacy):
    runtime = legacy.open()
    call(runtime, NOW - 60, "first-point", inp=50_000, out=0)
    calibrate_session(runtime, 20)

    legacy.clock.now += 60
    call(runtime, legacy.clock.now, "second-point", inp=50_000, out=0)
    calibrate_session(runtime, 30)
    config = saved_config()
    assert config["session_budget_tokens"] == 500_000
    assert config["calibrated_tokens"] == 100_000
    assert config["calibrated_unseen_tokens"] == 50_000
    assert config["calibrated_at"] == NOW + 60

    rows = list(reversed(runtime.store.calibrations()))
    # Each calibration row records its own single-point budget; Config holds the fit.
    assert [(row["counted_tokens"], row["budget_tokens"]) for row in rows] == [
        (50_000, 250_000), (100_000, 333_333)
    ]
    assert rows[0]["predicted_percent"] is None
    assert rows[1]["predicted_percent"] == 40

    runtime.shutdown()
    reopened = legacy.open()
    snapshot = reopened.snapshot()
    assert snapshot.totals.io_tokens == 100_000
    assert snapshot.used_tokens == 150_000
    assert snapshot.estimated_unseen_tokens == 50_000
    assert snapshot.percent == 30
    assert reopened.weekly_snapshot().used_tokens == 100_000


@pytest.mark.parametrize("mismatch", ["scope", "anchor", "new-session"])
def test_fitted_unseen_tokens_require_the_same_session_and_scope(legacy, mismatch):
    runtime = legacy.open()
    call(runtime, NOW - 60, "first-point", inp=50_000, out=0)
    calibrate_session(runtime, 20)
    legacy.clock.now += 60
    call(runtime, legacy.clock.now, "second-point", inp=50_000, out=0)
    calibrate_session(runtime, 30)
    assert runtime.snapshot().estimated_unseen_tokens == 50_000

    if mismatch == "scope":
        runtime.config.percent_scope = "all"
        runtime.config.save()
    elif mismatch == "anchor":
        runtime.session.set_reset_from_web(legacy.clock.now + HOUR)
    else:
        legacy.clock.now += 6 * HOUR
        call(runtime, legacy.clock.now, "new-session", inp=100, out=0)

    assert runtime.snapshot().has_activity
    assert runtime.snapshot().estimated_unseen_tokens == 0
    assert saved_config()["calibrated_unseen_tokens"] == 50_000


@pytest.mark.parametrize(
    "scope, basis, expected_start, expected_used, expected_calls",
    [
        ("claude", "io", NOW - 600, 33, 2),
        ("claude", "all", NOW - 600, 48, 2),
        ("all", "io", NOW - 7200, 231, 6),
        ("all", "all", NOW - 7200, 336, 6),
    ],
)
def test_basis_and_scope_apply_to_both_trackers_after_reopening(
    legacy, scope, basis, expected_start, expected_used, expected_calls
):
    runtime = legacy.open(percent_scope=scope, percent_basis=basis)
    samples = [
        ("anthropic", "subscription", -600, 1),
        ("anthropic", "unknown", -500, 2),
        ("anthropic", "api", -400, 3),
        ("anthropic", "local", -300, 4),
        ("openai", "unknown", -7200, 5),
        ("ollama", "local", -200, 6),
    ]
    for provider, billing, offset, factor in samples:
        call(runtime, NOW + offset, str(factor), provider=provider, billing=billing,
             inp=10 * factor, out=factor, cache_write_tokens=2 * factor,
             cache_read_tokens=3 * factor, cache_write_1h_tokens=factor)
    runtime.weekly.set_reset_in(DAY)
    runtime.shutdown()
    reopened = legacy.open()

    session = reopened.snapshot()
    week = reopened.weekly_snapshot()
    assert session.start_ts == expected_start
    assert session.used_tokens == week.used_tokens == expected_used
    assert session.totals.calls == week.totals.calls == expected_calls
    assert session.percent == pytest.approx(expected_used / 10)
    assert week.percent == pytest.approx(expected_used / 10)
    assert saved_config()["percent_scope"] == scope
    assert saved_config()["percent_basis"] == basis
    assert set(persisted_state(reopened)) == {"weekly_reset_ts", "weekly_reset_set_at"}


def test_unconfigured_week_moves_its_lookback_without_persisting_an_anchor(legacy):
    runtime = legacy.open()
    call(runtime, NOW - 7 * DAY - 1, "outside", inp=200, out=0)
    call(runtime, NOW - 7 * DAY, "exact-start", inp=100, out=0)
    call(runtime, NOW - HOUR, "recent")
    first = runtime.weekly_snapshot()

    assert not first.configured
    assert first.start_ts == NOW - 7 * DAY
    assert first.end_ts == NOW
    assert first.used_tokens == 250
    assert first.percent == 0
    legacy.clock.now += 1
    later = runtime.weekly_snapshot()
    assert later.start_ts == NOW - 7 * DAY + 1
    assert later.used_tokens == 150
    assert not later.configured
    assert persisted_state(runtime) == {}


@pytest.mark.parametrize("days", [7.0, 10.5])
def test_configured_week_rolls_on_its_exact_reset_and_keeps_the_original_anchor(legacy, days):
    runtime = legacy.open(weekly_window_days=days)
    reset = runtime.weekly.set_reset_in(DAY)
    legacy.clock.now = reset
    call(runtime, reset - 1, "previous-cycle", inp=100, out=0)
    call(runtime, reset, "new-cycle", inp=200, out=0)

    snapshot = runtime.weekly_snapshot()
    assert snapshot.configured
    assert snapshot.start_ts == reset
    assert snapshot.end_ts == reset + days * DAY
    assert snapshot.used_tokens == 200

    legacy.clock.now = reset + 9 * days * DAY + 123
    runtime.shutdown()
    reopened = legacy.open()
    later = reopened.weekly_snapshot()
    assert later.start_ts == reset + 9 * days * DAY
    assert later.end_ts == reset + 10 * days * DAY
    assert later.used_tokens == 0
    # The reset and the moment it was set persist as entered; rolling the cycle
    # forward never rewrites them.
    assert persisted_state(reopened) == {"weekly_reset_ts": repr(reset),
                                         "weekly_reset_set_at": repr(NOW)}
    assert saved_config()["weekly_window_days"] == days


@pytest.mark.parametrize("anchored", [False, True])
def test_weekly_calibration_persists_only_weekly_config_and_not_session_history(legacy, anchored):
    runtime = legacy.open(percent_basis="all", session_budget_tokens=1234)
    call(runtime, NOW - 60, "cache-heavy", inp=100, out=50,
         cache_write_tokens=200, cache_read_tokens=5000, cache_write_1h_tokens=100)
    if anchored:
        runtime.weekly.set_reset_in(DAY)
    config_before = saved_config()
    calibrate_week(runtime, 25)
    expected = {
        "weekly_budget_tokens": 21_400,
        "weekly_calibrated_at": NOW,
        "weekly_calibrated_from_percent": 25,
    }
    config_after = saved_config()
    assert {key: config_after[key] for key in expected} == expected
    assert {key: value for key, value in config_after.items() if key not in expected} == {
        key: value for key, value in config_before.items() if key not in expected
    }
    assert runtime.store.calibrations() == []

    runtime.shutdown()
    reopened = legacy.open()
    assert reopened.config.weekly_is_calibrated
    snapshot = reopened.weekly_snapshot()
    assert snapshot.used_tokens == 5350
    assert snapshot.configured is anchored
    # The percentage needs a weekly reset; both shells set it before calibrating.
    assert snapshot.percent == (25 if anchored else 0)


@pytest.mark.parametrize("tracker", ["session", "week"])
@pytest.mark.parametrize("future_seconds, expected_used", [(30, 300), (61, 100)])
def test_future_clock_tolerance_also_affects_legacy_budget_totals(
    legacy, tracker, future_seconds, expected_used
):
    runtime = legacy.open()
    runtime.weekly.set_reset_in(DAY)
    call(runtime, NOW - 60, "past", inp=100, out=0)
    call(runtime, NOW + future_seconds, "future", inp=200, out=0)
    snapshot = runtime.snapshot() if tracker == "session" else runtime.weekly_snapshot()
    # Totals accept records up to 60 s ahead of the clock (store.CLOCK_TOLERANCE_S), on purpose.
    assert snapshot.now_ts == NOW
    assert snapshot.used_tokens == expected_used


_REOPEN_IN_CHILD = """
import json
import sys
from unittest.mock import patch
from tokenmeter.config import Config
from tokenmeter.runtime import Runtime

with patch("time.time", return_value=float(sys.argv[1])):
    runtime = Runtime(Config.load())
    try:
        snapshot = runtime.snapshot()
        result = {
            "start": snapshot.start_ts,
            "used": snapshot.used_tokens,
            "budget": snapshot.budget_tokens,
            "percent": snapshot.percent,
            "alerts": [alert.threshold for alert in runtime.alerts.due(snapshot)],
            "memory": json.loads(runtime.store.get_state("alerts_fired")),
            "collectors": [runtime.proxy, runtime.transcripts, runtime.codex],
        }
        print(json.dumps(result))
    finally:
        runtime.shutdown()
"""


def test_alert_jump_and_state_survive_a_real_process_restart_until_the_next_session(legacy):
    runtime = legacy.open()
    call(runtime, NOW - 60, "above-all-thresholds", inp=1100, out=0)
    reset = parse_reset("Resets in 3 hr", NOW, runtime.config.window_seconds)
    start = runtime.session.set_reset_from_web(reset)
    assert [alert.threshold for alert in runtime.alerts.due(runtime.snapshot())] == [100]
    expected_memory = {"session_start_ts": start, "thresholds": [60, 85, 100]}
    assert json.loads(runtime.store.get_state("alerts_fired")) == expected_memory
    runtime.shutdown()

    result = subprocess.run(
        [sys.executable, "-B", "-c", _REOPEN_IN_CHILD, str(legacy.clock.now)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "start": start,
        "used": 1100,
        "budget": 1000,
        "percent": 110,
        "alerts": [],
        "memory": expected_memory,
        "collectors": [None, None, None],
    }

    legacy.clock.now = NOW + 6 * HOUR
    reopened = legacy.open()
    call(reopened, legacy.clock.now, "next-session", inp=1100, out=0)
    assert [alert.threshold for alert in reopened.alerts.due(reopened.snapshot())] == [100]
    assert json.loads(reopened.store.get_state("alerts_fired")) == {
        "session_start_ts": legacy.clock.now,
        "thresholds": [60, 85, 100],
    }
    assert reopened.alerts.due(reopened.snapshot()) == []
