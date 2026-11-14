"""A call is billed by the login in force when it was made, not when it was read."""
from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from tests.test_transcripts import assistant_line, write
from tokenmeter.billing import (
    AUTH_SEEN_EVERY_S, AUTH_TIMELINE_MAX, STATE_AUTH_TIMELINE, UNKNOWN, AuthTimeline,
)
from tokenmeter.collectors.transcripts import TranscriptCollector
from tokenmeter.store import Store

PRO = ("subscription", "pro")
API = ("api", "")
T0 = 1_700_000_000.0
HOUR = 3600.0


class Login:
    """The login on disk; tests change it like the user would."""

    def __init__(self, value=PRO):
        self.value = value

    def get(self):
        return self.value


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "auth.db")
    yield s
    s.close()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# -- the timeline -------------------------------------------------------------


def test_a_call_gets_the_login_in_force_at_its_own_time(store):
    login = Login(PRO)
    timeline = AuthTimeline(store, login)
    timeline.observe(T0)
    timeline.observe(T0 + 2 * HOUR)        # still Pro two hours later
    login.value = API
    timeline.observe(T0 + 2 * HOUR + 40)   # the switch is seen 40 s after the last Pro sighting
    assert timeline.at(T0 + HOUR) == PRO
    assert timeline.at(T0 + 2 * HOUR) == PRO
    assert timeline.at(T0 + 3 * HOUR) == API


def test_a_call_between_two_logins_is_unknown_not_guessed(store):
    """Nobody was looking between the last sighting of one login and the first
    of the next (the app was closed), so either could have paid."""
    login = Login(PRO)
    timeline = AuthTimeline(store, login)
    timeline.observe(T0)
    login.value = API
    timeline.observe(T0 + 10 * HOUR)       # reopened ten hours later, already on a key
    assert timeline.at(T0) == PRO
    assert timeline.at(T0 + 5 * HOUR) == UNKNOWN
    assert timeline.at(T0 + 10 * HOUR) == API


def test_before_the_first_sighting_the_first_login_stands_in(store):
    """A backfill of older transcripts: the earliest login known is the best
    available, and it is no longer the one on disk today."""
    login = Login(PRO)
    timeline = AuthTimeline(store, login)
    timeline.observe(T0)
    login.value = API
    timeline.observe(T0 + HOUR)
    assert timeline.at(T0 - 30 * 86400) == PRO


def test_after_the_last_sighting_the_latest_login_stands_in(store):
    timeline = AuthTimeline(store, Login(API))
    timeline.observe(T0)
    assert timeline.at(T0 + 5) == API


def test_switching_back_keeps_each_span(store):
    login = Login(PRO)
    timeline = AuthTimeline(store, login)
    for step, value in enumerate([PRO, API, API, PRO]):
        login.value = value
        timeline.observe(T0 + step * HOUR)
    assert [timeline.at(T0 + h * HOUR) for h in (0, 1, 2, 3)] == [PRO, API, API, PRO]
    assert timeline.at(T0 + 0.5 * HOUR) == UNKNOWN and timeline.at(T0 + 1.5 * HOUR) == API


def test_an_undetectable_login_is_a_span_of_its_own(store):
    login = Login(PRO)
    timeline = AuthTimeline(store, login)
    timeline.observe(T0)
    login.value = UNKNOWN                   # a subscription and a key at once
    timeline.observe(T0 + 60)
    timeline.observe(T0 + HOUR)
    assert timeline.at(T0 + 30 * 60) == UNKNOWN


def test_nothing_observed_means_unknown(store):
    assert AuthTimeline(store, Login()).at(T0) == UNKNOWN


def test_the_same_login_is_restamped_rarely_and_a_change_at_once(store):
    """The reader scans every two seconds; the database is written only when
    there is something new to say."""
    login = Login(PRO)
    timeline = AuthTimeline(store, login)
    timeline.observe(T0)
    before = store.get_state(STATE_AUTH_TIMELINE)
    timeline.observe(T0 + AUTH_SEEN_EVERY_S - 1)
    assert store.get_state(STATE_AUTH_TIMELINE) == before
    timeline.observe(T0 + AUTH_SEEN_EVERY_S)
    assert json.loads(store.get_state(STATE_AUTH_TIMELINE)) == [[T0, T0 + AUTH_SEEN_EVERY_S, "subscription", "pro"]]
    login.value = API
    timeline.observe(T0 + AUTH_SEEN_EVERY_S + 1)
    assert len(json.loads(store.get_state(STATE_AUTH_TIMELINE))) == 2


def test_the_history_of_changes_is_bounded(store):
    login = Login(PRO)
    timeline = AuthTimeline(store, login)
    for step in range(AUTH_TIMELINE_MAX + 25):
        login.value = PRO if step % 2 == 0 else API
        timeline.observe(T0 + step * HOUR)
    entries = json.loads(store.get_state(STATE_AUTH_TIMELINE))
    assert len(entries) == AUTH_TIMELINE_MAX
    # The oldest entry kept stands for everything before it.
    assert timeline.at(T0) == (entries[0][2], entries[0][3])


@pytest.mark.parametrize("raw", ["", "{", "{}", "[1, 2]", '[[1, 2, "subscription"]]',
                                 '[[2, 1, "subscription", "pro"]]', '[[1, 2, "free", ""]]',
                                 '[[true, 2, "api", ""]]', '[["a", 2, "api", ""]]'])
def test_a_damaged_timeline_is_ignored_and_rebuilt(store, raw):
    store.set_state(STATE_AUTH_TIMELINE, raw)
    timeline = AuthTimeline(store, Login(API))
    assert timeline.at(T0) == UNKNOWN
    assert timeline.observe(T0) == API and timeline.at(T0) == API


def test_the_timeline_survives_a_restart(tmp_path):
    login = Login(PRO)
    first = Store(tmp_path / "r.db")
    AuthTimeline(first, login).observe(T0)
    first.close()
    login.value = API
    second = Store(tmp_path / "r.db")
    try:
        timeline = AuthTimeline(second, login)
        timeline.observe(T0 + HOUR)
        assert timeline.at(T0) == PRO and timeline.at(T0 + HOUR) == API
    finally:
        second.close()


# -- through the transcript reader -------------------------------------------


def billing_of(store):
    return {r["dedupe_key"]: (r["billing"], r["plan"]) for r in store._conn.execute(  # noqa: SLF001
        "SELECT dedupe_key, billing, plan FROM usage_events")}


def test_a_call_made_on_a_plan_and_read_after_switching_to_a_key_stays_on_the_plan(
        store, tmp_path, monkeypatch):
    """The case that was wrong: the line is read late, with another login on disk."""
    import tokenmeter.billing as billing_module
    clock = {"now": T0}
    monkeypatch.setattr(billing_module.time, "time", lambda: clock["now"])
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True)
    login = Login(PRO)
    collector = TranscriptCollector(store, root, billing=login)

    # The app sees the plan while a first call is made.
    write(root / "p" / "a.jsonl", [assistant_line("req_seen", ts=iso(T0 - 60))])
    collector.scan_once()

    # An hour passes with the app open and nothing new to read.
    clock["now"] = T0 + HOUR
    collector.scan_once()

    # Later the user switches to an API key. A transcript with a call from the
    # plan's time (another project, synced late) is only read now.
    clock["now"] = T0 + 2 * HOUR
    login.value = API
    write(root / "p" / "b.jsonl", [assistant_line("req_late_read", ts=iso(T0 + 10)),
                                   assistant_line("req_on_key", ts=iso(T0 + 2 * HOUR - 5))])
    collector.scan_once()

    assert billing_of(store) == {
        "req:req_seen": PRO,
        "req:req_late_read": PRO,            # made under the plan: not a paid API call
        "req:req_on_key": ("unknown", ""),   # between the two sightings: either could have paid
    }
    clock["now"] = T0 + 2 * HOUR + 120
    write(root / "p" / "c.jsonl", [assistant_line("req_now", ts=iso(T0 + 2 * HOUR + 100))])
    collector.scan_once()
    assert billing_of(store)["req:req_now"] == API


def test_a_single_login_tags_the_whole_backfill_as_before(store, tmp_path):
    """Nothing changes for someone who never switched."""
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True)
    write(root / "p" / "a.jsonl", [assistant_line("req_old", ts="2026-01-05T10:00:00.000Z"),
                                   assistant_line("req_new", ts="2026-08-12T15:33:17.376Z")])
    TranscriptCollector(store, root, billing=Login(PRO)).scan_once()
    assert set(billing_of(store).values()) == {PRO}


def test_a_manual_override_still_fills_what_the_timeline_cannot_tell(store, tmp_path):
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True)
    write(root / "p" / "a.jsonl", [assistant_line("req_x")])
    TranscriptCollector(store, root, billing=Login(UNKNOWN), overrides={"anthropic": "api"}).scan_once()
    assert billing_of(store) == {"req:req_x": API}
