"""The plan-limit reset that Claude Code reports, and how the windows use it.

All transcript lines here are invented. They copy the shape of a rejection
(key names and types), never the content of a real conversation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tests.test_dashboard import app_without_tk
from tests.test_transcripts import assistant_line, write
from tokenmeter import dashboard, official_limits
from tokenmeter.collectors.transcripts import (
    TranscriptCollector, limit_notice_from_line, parse_line,
)
from tokenmeter.config import Config
from tokenmeter.models import UsageEvent
from tokenmeter.official_limits import FIVE_HOUR, SEVEN_DAY, LimitNotice, latest, record
from tokenmeter.session import (
    SOURCE_CLAUDE_AI, SOURCE_CLAUDE_CODE, SOURCE_ESTIMATED, SOURCE_NONE, SessionTracker,
)
from tokenmeter.store import Store
from tokenmeter.weekly import WeeklyTracker

HOUR, DAY = 3600.0, 86400.0
# The rejection is reported at 15:00:50 UTC and says the window resets at 17:50.
REPORTED = datetime(2026, 11, 10, 15, 0, 50, tzinfo=timezone.utc).timestamp()
RESETS = datetime(2026, 11, 10, 17, 50, tzinfo=timezone.utc).timestamp()
START = RESETS - 5 * HOUR  # 12:50


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def rejection_line(kind="five_hour", resets=RESETS, reported=REPORTED, request_id="req_rejected",
                   **overrides) -> str:
    """A limit rejection as Claude Code writes it: an assistant message it made up."""
    info = {"status": "rejected", "resetsAt": int(resets) if isinstance(resets, float) else resets,
            "rateLimitType": kind, "overageStatus": "rejected",
            "overageDisabledReason": "org_level_disabled", "isUsingOverage": False}
    record_ = {
        "type": "assistant", "requestId": request_id,
        "timestamp": iso(reported) if reported is not None else None,
        "message": {"id": f"msg_{request_id}", "model": "<synthetic>", "role": "assistant",
                    "content": [{"type": "text"}],
                    "usage": {"input_tokens": 0, "output_tokens": 0,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
        "apiError": "rate_limit", "apiErrorParams": {"rate_limit_info": info},
        "error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429,
    }
    record_.update(overrides)
    return json.dumps(record_)


class Subscription:
    def get(self):
        return "subscription", "pro"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "limits.db")
    yield s
    s.close()


def scan(store, tmp_path, lines, name="t.jsonl") -> int:
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True, exist_ok=True)
    write(root / "p" / name, lines)
    return TranscriptCollector(store, root, billing=Subscription()).scan_once()


def call(store, ts, key, tokens=100):
    store.add_event(UsageEvent(ts=ts, provider="anthropic", model="claude-opus-5",
                               input_tokens=tokens, output_tokens=0, source="transcript",
                               dedupe_key=key, billing="subscription"))


# -- reading the notice -------------------------------------------------------


@pytest.mark.parametrize("kind, resets", [("five_hour", RESETS), ("seven_day", REPORTED + 3 * DAY)])
def test_a_rejection_carries_the_exact_reset_of_its_limit(kind, resets):
    assert limit_notice_from_line(rejection_line(kind, resets)) == LimitNotice(kind, resets, REPORTED)


@pytest.mark.parametrize("line", [
    rejection_line(kind="monthly"),                         # a limit this app does not model
    rejection_line(resets=True),                            # bool is not a timestamp
    rejection_line(resets="1794333000"),
    rejection_line(resets=None),
    rejection_line(resets=RESETS * 1000),                   # milliseconds, not seconds
    rejection_line(resets=REPORTED - 60),                   # a reset before its own report
    rejection_line(resets=REPORTED),                        # ...or at the same instant
    rejection_line(resets=REPORTED + 5 * HOUR + 3600),      # further than one window ahead
    rejection_line(kind="seven_day", resets=REPORTED + 8 * DAY),
    rejection_line(reported=None),                          # no date: cannot be ordered
    rejection_line(apiErrorParams={}),
    rejection_line(apiErrorParams={"rate_limit_info": "rejected"}),
    rejection_line(apiErrorParams=None),
    assistant_line(),                                       # an ordinary call
    "not json", "[]", "",
])
def test_anything_that_is_not_exactly_a_reset_time_is_ignored(line):
    """The field is not a documented contract, so nothing is guessed from it: a
    wrong reset would move the whole window."""
    assert limit_notice_from_line(line) is None


@pytest.mark.parametrize("resets", [float("nan"), float("inf")])
def test_a_reset_that_is_not_a_finite_number_is_ignored(resets):
    record_ = json.loads(rejection_line())
    record_["apiErrorParams"]["rate_limit_info"]["resetsAt"] = resets
    assert official_limits.notice_from_record(record_, REPORTED) is None


def test_a_rejection_is_not_a_call():
    """Claude Code writes the rejection as an assistant message with zeroed
    usage. No model answered, so it must not count as a call or show up as a
    model named "<synthetic>"."""
    assert parse_line(rejection_line()) == (None, False)
    # Any failed request has the same shape, also without limit details.
    assert parse_line(rejection_line(apiErrorParams=None, apiError="overloaded")) == (None, False)
    only_model = json.loads(rejection_line())
    del only_model["isApiErrorMessage"]
    assert parse_line(json.dumps(only_model)) == (None, False)


def test_a_made_up_message_that_does_carry_tokens_is_still_counted():
    """Dropping the rejection must never drop usage: only a record with no
    tokens at all is discarded."""
    with_tokens = json.loads(rejection_line())
    with_tokens["message"]["usage"]["cache_read_input_tokens"] = 7
    event, undated = parse_line(json.dumps(with_tokens))
    assert (event.cache_read_tokens, undated) == (7, False)


def test_the_collector_stores_the_reset_and_counts_only_the_real_calls(store, tmp_path):
    added = scan(store, tmp_path, [
        assistant_line("req_ok", ts=iso(REPORTED - 600)),
        rejection_line(),
        rejection_line("seven_day", REPORTED + 2 * DAY, request_id="req_week"),
    ])
    assert added == 1
    assert [m.model for m in store.totals_by_model(0)] == ["claude-opus-5"]
    assert latest(store, FIVE_HOUR) == LimitNotice(FIVE_HOUR, RESETS, REPORTED)
    assert latest(store, SEVEN_DAY) == LimitNotice(SEVEN_DAY, REPORTED + 2 * DAY, REPORTED)


def test_the_newest_report_wins_whatever_order_the_files_are_read_in(store, tmp_path):
    """A backfill reads old transcripts after new ones. An old rejection must
    not replace a newer one."""
    newer = rejection_line(resets=RESETS + 5 * HOUR, reported=REPORTED + 5 * HOUR)
    scan(store, tmp_path, [newer], name="a-new.jsonl")
    scan(store, tmp_path, [rejection_line()], name="b-old.jsonl")
    assert latest(store, FIVE_HOUR).resets_at == RESETS + 5 * HOUR
    assert record(store, LimitNotice(FIVE_HOUR, RESETS, REPORTED + 5 * HOUR)) is False, "same age"


@pytest.mark.parametrize("raw", ["", "{", "[]", '{"resets_at": "x", "observed_at": 1}',
                                 '{"resets_at": 1}', '{"resets_at": NaN, "observed_at": 1}'])
def test_a_damaged_stored_notice_is_treated_as_none(store, raw):
    store.set_state(official_limits.STATE_KEYS[FIVE_HOUR], raw)
    assert latest(store, FIVE_HOUR) is None
    tracker = SessionTracker(store, Config())
    assert tracker.known_start() == (None, SOURCE_NONE)


# -- the session --------------------------------------------------------------


def test_the_session_takes_the_reported_window_not_the_first_local_call(store):
    """The first call on this machine was at 14:00, but the account's window
    opened at 12:50 (usage in the browser). Deducing from the local call would
    put the reset at 19:00; Claude Code reported 17:50."""
    tracker = SessionTracker(store, Config())
    call(store, START + 70 * 60, "first-local")          # 14:00
    now = REPORTED + 60
    assert tracker.anchor(now) == (START + 70 * 60, True)
    assert tracker.anchor_source(now) == SOURCE_ESTIMATED

    record(store, LimitNotice(FIVE_HOUR, RESETS, REPORTED))
    snapshot = tracker.snapshot(now)
    assert (snapshot.start_ts, snapshot.end_ts) == (START, RESETS)
    assert tracker.anchor_source(now) == SOURCE_CLAUDE_CODE


def test_after_the_reported_reset_the_next_session_starts_with_the_next_call(store):
    tracker = SessionTracker(store, Config())
    record(store, LimitNotice(FIVE_HOUR, RESETS, REPORTED))
    call(store, RESETS - 600, "inside")
    assert tracker.anchor(RESETS + 60) == (RESETS + 60, False), "expired, nothing since"
    assert tracker.anchor_source(RESETS + 60) == SOURCE_NONE

    call(store, RESETS + 900, "after")
    now = RESETS + 1000
    assert tracker.anchor(now) == (RESETS + 900, True)
    assert tracker.anchor_source(now) == SOURCE_ESTIMATED, "this window was not reported by anyone"
    assert tracker.snapshot(now).used_tokens == 100, "the call inside the old window is not in the new one"


def test_the_later_window_wins_between_claude_code_and_a_copy_from_claude_ai(store):
    tracker = SessionTracker(store, Config())
    record(store, LimitNotice(FIVE_HOUR, RESETS, REPORTED))
    call(store, RESETS + 100, "next-window")
    # The user copies the reset of the following session from claude.ai.
    later_start = tracker.set_reset_from_web(RESETS + 100 + 5 * HOUR)
    now = RESETS + 200
    assert tracker.anchor(now) == (later_start, True)
    assert tracker.anchor_source(now) == SOURCE_CLAUDE_AI

    # A newer rejection from Claude Code then reports an even later window.
    record(store, LimitNotice(FIVE_HOUR, RESETS + 11 * HOUR, RESETS + 9 * HOUR))
    now = RESETS + 9 * HOUR + 60
    assert tracker.anchor(now) == (RESETS + 6 * HOUR, True)
    assert tracker.anchor_source(now) == SOURCE_CLAUDE_CODE


def test_a_manual_session_reset_after_the_report_still_cuts_the_session(store, monkeypatch):
    tracker = SessionTracker(store, Config())
    record(store, LimitNotice(FIVE_HOUR, RESETS, REPORTED))
    call(store, REPORTED - 60, "before")
    monkeypatch.setattr("tokenmeter.session.time.time", lambda: REPORTED + 120)
    tracker.reset_now()
    assert tracker.anchor(REPORTED + 180) == (REPORTED + 180, False)


def test_a_custom_window_length_is_measured_back_from_the_reported_reset(store):
    """Same rule as a reset copied from claude.ai: the configured length."""
    tracker = SessionTracker(store, Config(session_window_hours=4.0))
    record(store, LimitNotice(FIVE_HOUR, RESETS, REPORTED))
    assert tracker.known_start() == (RESETS - 4 * HOUR, SOURCE_CLAUDE_CODE)


# -- the week -----------------------------------------------------------------


def test_the_week_takes_the_reported_reset_without_any_manual_setup(store):
    weekly = WeeklyTracker(store, Config())
    assert weekly.snapshot(REPORTED).configured is False
    week_reset = REPORTED + 2 * DAY
    record(store, LimitNotice(SEVEN_DAY, week_reset, REPORTED))
    snapshot = weekly.snapshot(REPORTED + 60)
    assert snapshot.configured and (snapshot.start_ts, snapshot.end_ts) == (week_reset - 7 * DAY, week_reset)
    assert weekly.reset_and_source() == (week_reset, "claude_code")
    # Past the reset it rolls in whole weeks, like a manual one.
    assert weekly.next_reset(week_reset + 8 * DAY) == week_reset + 14 * DAY


def test_a_reset_set_by_hand_before_this_was_tracked_gives_way_to_claude_code(store):
    weekly = WeeklyTracker(store, Config())
    store.set_state("weekly_reset_ts", repr(REPORTED + DAY))  # as an older version stored it
    assert weekly.reset_and_source() == (REPORTED + DAY, "claude_ai")
    record(store, LimitNotice(SEVEN_DAY, REPORTED + 2 * DAY, REPORTED))
    assert weekly.reset_and_source() == (REPORTED + 2 * DAY, "claude_code")


def test_the_latest_report_wins_between_the_user_and_claude_code(store):
    weekly = WeeklyTracker(store, Config())
    record(store, LimitNotice(SEVEN_DAY, REPORTED + 2 * DAY, REPORTED))
    # The user sets it afterwards: that is the newer information.
    weekly.set_reset_at(REPORTED + 3 * DAY, now=REPORTED + 600)
    assert weekly.reset_and_source() == (REPORTED + 3 * DAY, "claude_ai")
    # An older rejection found later in a backfill does not undo it.
    record(store, LimitNotice(SEVEN_DAY, REPORTED + 2 * DAY, REPORTED - DAY))
    assert weekly.reset_and_source() == (REPORTED + 3 * DAY, "claude_ai")
    # A rejection reported after the user's edit does.
    record(store, LimitNotice(SEVEN_DAY, REPORTED + 9 * DAY + 60, REPORTED + 5 * DAY))
    assert weekly.reset_and_source() == (REPORTED + 9 * DAY + 60, "claude_code")


# -- what the user sees -------------------------------------------------------


def state_at(app, now, monkeypatch):
    for module in ("tokenmeter.session", "tokenmeter.weekly", "tokenmeter.store",
                   "tokenmeter.dashboard", "tokenmeter.state"):
        monkeypatch.setattr(f"{module}.time.time", lambda: now)
    return dashboard.build_state(app, now)


@pytest.mark.parametrize("prepare, kind, short, phrase", [
    (lambda app: None, SOURCE_ESTIMATED, "estimated", "estimated from your first call"),
    (lambda app: record(app.store, LimitNotice(FIVE_HOUR, RESETS, REPORTED)),
     SOURCE_CLAUDE_CODE, "from Claude Code", "reported by Claude Code"),
    (lambda app: app.session.set_reset_from_web(RESETS), SOURCE_CLAUDE_AI, "from claude.ai",
     "copied from claude.ai"),
])
def test_the_window_says_where_its_reset_comes_from(tmp_path, monkeypatch, prepare, kind, short, phrase):
    """An estimated reset can be an hour off; the user has to be able to tell
    it apart from one the account reported."""
    app = app_without_tk(tmp_path)
    call(app.store, REPORTED - 300, "one")
    prepare(app)
    session = state_at(app, REPORTED + 60, monkeypatch)["session"]
    assert session["source"]["kind"] == kind
    assert phrase in session["source"]["text"]
    assert session["source"]["estimated"] is (kind == SOURCE_ESTIMATED)
    assert session["reset_text"].endswith(f" · {short}")


def test_no_session_shows_no_source(tmp_path, monkeypatch):
    app = app_without_tk(tmp_path)
    session = state_at(app, REPORTED, monkeypatch)["session"]
    assert session["source"] == {"kind": SOURCE_NONE, "text": "", "estimated": False}
    assert "starts with the next call" in session["reset_text"]


def test_the_week_says_where_its_reset_comes_from(tmp_path, monkeypatch):
    app = app_without_tk(tmp_path)
    assert state_at(app, REPORTED, monkeypatch)["weekly"]["source"] == {"kind": "none", "text": ""}
    record(app.store, LimitNotice(SEVEN_DAY, REPORTED + 2 * DAY, REPORTED))
    assert state_at(app, REPORTED + 60, monkeypatch)["weekly"]["source"] == {
        "kind": "claude_code", "text": "weekly reset reported by Claude Code"}


def test_the_source_filter_of_the_breakdown_is_not_replaced_by_the_session_source(tmp_path, monkeypatch):
    """The window's source and the breakdown's collector filter are different
    things with the same word: asking for proxy calls must still filter by proxy."""
    app = app_without_tk(tmp_path)
    call(app.store, REPORTED - 300, "from-transcript")
    for module in ("tokenmeter.session", "tokenmeter.weekly", "tokenmeter.store",
                   "tokenmeter.dashboard", "tokenmeter.state"):
        monkeypatch.setattr(f"{module}.time.time", lambda: REPORTED + 60)
    assert [m["model"] for m in dashboard.build_state(app, REPORTED + 60)["models"]] == ["claude-opus-5"]
    assert dashboard.build_state(app, REPORTED + 60, source="transcript")["models"] != []
    assert dashboard.build_state(app, REPORTED + 60, source="proxy")["models"] == []
