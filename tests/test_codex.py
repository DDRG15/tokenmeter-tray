import json
import os
import shutil
import time
from pathlib import Path

import pytest

from tokenmeter.collectors.codex import (
    CodexCollector,
    default_codex_root,
    event_from_record,
    load_rate_limits,
    read_auth_mode,
)
from tokenmeter.collectors.transcripts import parse_timestamp
from tokenmeter.models import BILLING_API, BILLING_SUBSCRIPTION, BILLING_UNKNOWN
from tokenmeter.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
TS = "2025-01-01T03:00:00.000Z"


# -- line builders -------------------------------------------------------------


def context(turn="turn-X", model="gpt-6.1-sol", ts=TS):
    return json.dumps(
        {"timestamp": ts, "type": "turn_context", "payload": {"turn_id": turn, "model": model}}
    )


def record(resp="resp_1", turn="turn-X", inp=100, cached=0, cw=0, out=10, reasoning=0,
           ts=TS, with_total=True):
    usage = {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cw,
        "output_tokens": out,
        "reasoning_output_tokens": reasoning,
        "total_tokens": inp + out,
    }
    payload = {"turn_id": turn, "usage": usage}
    if resp is not None:
        payload["response_id"] = resp
    if with_total:
        # Cumulative: if the collector summed them, the numbers would not add up.
        payload["turn_token_usage"] = {k: v * 7 for k, v in usage.items()}
        payload["thread_token_usage"] = {k: v * 9 for k, v in usage.items()}
    return json.dumps({"timestamp": ts, "type": "token_usage_record", "payload": payload})


def token_count(plan="plus", used=1.0, ts=TS, credits=False):
    rate = {
        "limit_id": "codex",
        "primary": {"used_percent": used, "window_minutes": 300, "resets_at": 1735714800},
        "secondary": {"used_percent": used / 2, "window_minutes": 10080, "resets_at": 1736298000},
        "credits": {"has_credits": credits, "unlimited": False, "balance": "0"},
        "plan_type": plan,
    }
    return json.dumps(
        {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": rate}}
    )


def write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append(path, text):
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def rollout_dir(root, sub="sessions"):
    return root / sub / "2025" / "01" / "01"


def write_auth(root, mode, extra=None):
    root.mkdir(parents=True, exist_ok=True)
    data = {"auth_mode": mode, "OPENAI_API_KEY": "sk-FAKE-for-tests-only"}
    data.update(extra or {})
    (root / "auth.json").write_text(json.dumps(data), encoding="utf-8")


def rows(store):
    """Stored events, by dedupe_key."""
    cur = store._conn.execute("SELECT * FROM usage_events ORDER BY ts, id")
    return {r["dedupe_key"]: r for r in cur.fetchall()}


@pytest.fixture
def env(tmp_path):
    root = tmp_path / ".codex"
    store = Store(tmp_path / "u.db")
    return root, store


def install_fixture(root, name="codex_rollout.jsonl", sub="sessions", dest=None):
    target = rollout_dir(root, sub) / (dest or f"rollout-{name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / name, target)
    return target


# -- numbers of each event --------------------------------------------------------


def test_event_numbers_subtract_cached_and_do_not_add_reasoning(env):
    root, store = env
    install_fixture(root)
    assert CodexCollector(store, root).scan_once() == 3

    ev = rows(store)
    r1 = ev["codex:resp_synthetic_0001"]
    assert (r1["input_tokens"], r1["cache_read_tokens"], r1["output_tokens"]) == (4000, 8000, 200)
    assert r1["cache_write_tokens"] == 0

    r2 = ev["codex:resp_synthetic_0002"]  # 400 of reasoning go INSIDE the 500
    assert (r2["input_tokens"], r2["cache_read_tokens"], r2["output_tokens"]) == (2000, 0, 500)

    r3 = ev["codex:resp_synthetic_0003"]
    assert (r3["input_tokens"], r3["cache_read_tokens"], r3["output_tokens"]) == (2000, 3000, 200)

    for r in ev.values():
        assert r["provider"] == "openai"
        assert r["source"] == "codex"
        assert r["usage_complete"] == 1


def test_the_total_matches_total_tokens_and_ignores_the_cumulative_ones(env):
    """total_tokens == input + output; turn/thread_token_usage are cumulative."""
    root, store = env
    install_fixture(root)
    CodexCollector(store, root).scan_once()

    totals = store.totals_since(0)
    assert totals.calls == 3
    assert totals.all_tokens == 12200 + 2500 + 5200
    assert (totals.input_tokens, totals.cache_read_tokens, totals.output_tokens) == (8000, 11000, 900)


def test_the_timestamp_is_interpreted_as_utc(env):
    root, store = env
    install_fixture(root)
    CodexCollector(store, root).scan_once()
    ev = rows(store)["codex:resp_synthetic_0001"]
    assert ev["ts"] == parse_timestamp("2025-01-01T01:00:38.872Z")


def test_the_model_comes_from_each_turns_turn_context(env):
    root, store = env
    install_fixture(root)
    CodexCollector(store, root).scan_once()
    ev = rows(store)
    assert ev["codex:resp_synthetic_0001"]["model"] == "gpt-6.1-sol"
    assert ev["codex:resp_synthetic_0002"]["model"] == "gpt-6.1-sol"
    assert ev["codex:resp_synthetic_0003"]["model"] == "codex-auto-review"


def test_a_turn_without_turn_context_stays_unknown(env):
    root, store = env
    write(rollout_dir(root) / "rollout-a.jsonl", [record("resp_a", turn="turn-orphan")])
    CodexCollector(store, root).scan_once()
    assert rows(store)["codex:resp_a"]["model"] == "unknown"


def test_the_broken_line_and_foreign_types_are_ignored(env):
    root, store = env
    install_fixture(root)
    collector = CodexCollector(store, root)
    assert collector.scan_once() == 3  # the fixture has a JSON line cut off midway
    assert collector.stats.last_error == ""
    assert collector.stats.files_seen == 1


def test_a_record_without_response_id_is_not_counted(env):
    """Without an id there is no possible dedupe; better not to count it than to count it twice."""
    root, store = env
    write(rollout_dir(root) / "rollout-a.jsonl", [context(), record(resp=None)])
    assert CodexCollector(store, root).scan_once() == 0


# -- cache_write and safeguards ---------------------------------------------------


def _event(**kw):
    obj = json.loads(record(**kw))
    return event_from_record(obj, "gpt-6.1-sol", BILLING_UNKNOWN, "")


def test_the_cache_write_is_separated_if_it_fits():
    ev = _event(inp=1000, cached=200, cw=100, out=50)
    assert (ev.input_tokens, ev.cache_read_tokens, ev.cache_write_tokens) == (700, 200, 100)
    assert ev.all_tokens == 1000 + 50


def test_a_cache_write_that_does_not_fit_is_not_subtracted():
    """If it does not fit after subtracting the cached tokens, the assumption is false."""
    ev = _event(inp=1000, cached=200, cw=900, out=50)
    assert (ev.input_tokens, ev.cache_read_tokens, ev.cache_write_tokens) == (800, 200, 0)


def test_cached_tokens_larger_than_the_input_do_not_leave_negative_input():
    ev = _event(inp=100, cached=500, cw=0, out=5)
    assert ev.input_tokens == 0
    assert ev.cache_read_tokens == 500


# -- dedupe -------------------------------------------------------------------------


def test_rereading_the_whole_file_does_not_duplicate(env):
    root, store = env
    path = install_fixture(root)
    collector = CodexCollector(store, root)
    assert collector.scan_once() == 3

    store.set_offset(str(path), 0, 0)  # force a reread from scratch
    assert collector.scan_once() == 0
    assert collector.stats.events_duplicate == 3
    assert store.totals_since(0).calls == 3


def test_the_same_rollout_in_sessions_and_archived_sessions_counts_once(env):
    root, store = env
    install_fixture(root, sub="sessions")
    install_fixture(root, sub="archived_sessions")
    collector = CodexCollector(store, root)
    assert collector.scan_once() == 3
    assert collector.stats.files_seen == 2
    assert store.totals_since(0).calls == 3


def test_reads_both_folders(env):
    root, store = env
    write(rollout_dir(root, "sessions") / "rollout-a.jsonl", [context(), record("resp_s")])
    write(rollout_dir(root, "archived_sessions") / "rollout-b.jsonl", [context(), record("resp_a")])
    assert CodexCollector(store, root).scan_once() == 2


# -- incremental reading ------------------------------------------------------------------


def test_only_reads_what_is_new_including_the_half_written_line(env):
    root, store = env
    f = rollout_dir(root) / "rollout-a.jsonl"
    write(f, [context("t1", "m-one"), record("resp_1", turn="t1")])

    collector = CodexCollector(store, root)
    assert collector.scan_once() == 1
    lines_before = collector.stats.lines_read
    assert collector.scan_once() == 0, "with no changes it must not reread"
    assert collector.stats.lines_read == lines_before

    fresh = record("resp_2", turn="t1", out=77)
    append(f, record("resp_3", turn="t1") + "\n" + fresh[:40])  # the last one, without \n
    assert collector.scan_once() == 1, "the half-written line is not consumed"
    assert collector.stats.lines_read == lines_before + 1

    append(f, fresh[40:] + "\n")
    assert collector.scan_once() == 1
    assert store.totals_since(0).calls == 3
    assert rows(store)["codex:resp_2"]["output_tokens"] == 77
    assert rows(store)["codex:resp_2"]["model"] == "m-one"


def test_a_file_that_shrinks_is_reread_from_scratch(env):
    root, store = env
    f = rollout_dir(root) / "rollout-a.jsonl"
    write(f, [context("t1"), record("resp_1", turn="t1"), record("resp_2", turn="t1")])
    collector = CodexCollector(store, root)
    collector.scan_once()

    write(f, [context("t9", "m-new"), record("resp_9", turn="t9")])
    assert collector.scan_once() == 1
    assert rows(store)["codex:resp_9"]["model"] == "m-new"


def test_a_new_collector_rebuilds_the_model_map_with_an_offset_above_zero(env):
    """The turn_id → model map lives in memory; it is lost when the app restarts."""
    root, store = env
    f = rollout_dir(root) / "rollout-a.jsonl"
    write(f, [context("turn-A", "gpt-6.1-sol"), context("turn-B", "codex-auto-review"),
              record("resp_1", turn="turn-A"), token_count("plus")])
    assert CodexCollector(store, root).scan_once() == 1

    append(f, record("resp_2", turn="turn-B") + "\n" + record("resp_3", turn="turn-A") + "\n")
    fresh = CodexCollector(store, root)  # new process: no map in memory
    assert fresh.scan_once() == 2

    ev = rows(store)
    assert ev["codex:resp_2"]["model"] == "codex-auto-review"
    assert ev["codex:resp_3"]["model"] == "gpt-6.1-sol"
    # The plan is rebuilt too; the reread emits no events and duplicates none.
    assert ev["codex:resp_2"]["billing"] == BILLING_SUBSCRIPTION
    assert ev["codex:resp_2"]["plan"] == "plus"
    assert fresh.stats.events_duplicate == 0
    assert store.totals_since(0).calls == 3


# -- billing and plan -----------------------------------------------------------------------


def test_the_files_plan_type_gives_subscription_and_plan(env):
    root, store = env
    install_fixture(root)
    CodexCollector(store, root).scan_once()
    for r in rows(store).values():
        assert r["billing"] == BILLING_SUBSCRIPTION
        assert r["plan"] == "plus"


def test_plan_type_wins_even_if_it_arrives_after_the_record_in_the_same_batch(env):
    root, store = env
    write(rollout_dir(root) / "rollout-a.jsonl", [context(), record("resp_1"), token_count("pro")])
    CodexCollector(store, root).scan_once()
    r = rows(store)["codex:resp_1"]
    assert (r["billing"], r["plan"]) == (BILLING_SUBSCRIPTION, "pro")


def test_plan_type_wins_over_auth_json_apikey(env):
    root, store = env
    write_auth(root, "apikey")
    write(rollout_dir(root) / "rollout-a.jsonl", [context(), record("resp_1"), token_count("plus")])
    CodexCollector(store, root).scan_once()
    assert rows(store)["codex:resp_1"]["billing"] == BILLING_SUBSCRIPTION


def test_auth_json_chatgpt_is_subscription_without_a_plan(env):
    root, store = env
    write_auth(root, "chatgpt")
    install_fixture(root, "codex_rollout_apikey.jsonl")  # without rate_limits
    assert CodexCollector(store, root).scan_once() == 2
    for r in rows(store).values():
        assert (r["billing"], r["plan"]) == (BILLING_SUBSCRIPTION, "")


def test_auth_json_apikey_is_api(env):
    root, store = env
    write_auth(root, "apikey")
    install_fixture(root, "codex_rollout_apikey.jsonl")
    CodexCollector(store, root).scan_once()
    for r in rows(store).values():
        assert (r["billing"], r["plan"]) == (BILLING_API, "")


def test_without_auth_json_or_plan_it_is_unknown(env):
    root, store = env
    install_fixture(root, "codex_rollout_apikey.jsonl")
    CodexCollector(store, root).scan_once()
    for r in rows(store).values():
        assert r["billing"] == BILLING_UNKNOWN


def test_broken_auth_json_or_an_odd_mode_is_unknown(env):
    root, store = env
    root.mkdir(parents=True)
    (root / "auth.json").write_text("{this is not json", encoding="utf-8")
    assert read_auth_mode(root) == ""
    write_auth(root, "something-new")
    install_fixture(root, "codex_rollout_apikey.jsonl")
    CodexCollector(store, root).scan_once()
    assert {r["billing"] for r in rows(store).values()} == {BILLING_UNKNOWN}


def test_the_auth_json_key_does_not_end_up_anywhere(env):
    root, store = env
    write_auth(root, "apikey", {"tokens": {"access_token": "tok-FAKE"}})
    install_fixture(root)
    CodexCollector(store, root).scan_once()
    dump = json.dumps(
        [dict(r) for r in store._conn.execute("SELECT * FROM state").fetchall()]
        + [dict(r) for r in rows(store).values()],
        default=str,
    )
    assert "sk-FAKE" not in dump
    assert "tok-FAKE" not in dump


# -- real limits ------------------------------------------------------------------------------


def test_stores_the_most_recent_rate_limits(env):
    root, store = env
    install_fixture(root)
    CodexCollector(store, root).scan_once()

    limits = load_rate_limits(store)
    assert limits["captured_ts"] == parse_timestamp("2025-01-01T01:01:10.000Z")
    assert limits["plan_type"] == "plus"
    assert limits["primary"] == {"used_percent": 10.0, "window_minutes": 300, "resets_at": 1735714800}
    assert limits["secondary"] == {"used_percent": 2.0, "window_minutes": 10080, "resets_at": 1736298000}
    assert limits["credits"] == {"has_credits": False}


def test_an_older_rate_limits_does_not_overwrite_the_newer_one(env):
    root, store = env
    write(rollout_dir(root) / "rollout-b.jsonl",
          [token_count("plus", used=40.0, ts="2025-01-01T05:00:00.000Z")])
    collector = CodexCollector(store, root)
    collector.scan_once()

    # An archived file read later carries older data.
    write(rollout_dir(root, "archived_sessions") / "rollout-c.jsonl",
          [token_count("plus", used=99.0, ts="2025-01-01T01:00:00.000Z")])
    collector.scan_once()
    assert load_rate_limits(store)["primary"]["used_percent"] == 40.0

    write(rollout_dir(root) / "rollout-d.jsonl",
          [token_count("pro", used=55.0, ts="2025-01-01T06:00:00.000Z", credits=True)])
    collector.scan_once()
    limits = load_rate_limits(store)
    assert limits["primary"]["used_percent"] == 55.0
    assert limits["plan_type"] == "pro"
    assert limits["credits"] == {"has_credits": True}


def test_load_rate_limits_with_no_data_or_garbage_returns_none(env):
    _root, store = env
    assert load_rate_limits(store) is None
    store.set_state("codex_rate_limits", "{not json")
    assert load_rate_limits(store) is None
    store.set_state("codex_rate_limits", json.dumps({"plan_type": "plus"}))
    assert load_rate_limits(store) is None


# -- root, lifecycle and coverage --------------------------------------------------------------


def test_missing_folder_returns_zero_without_error(tmp_path):
    store = Store(tmp_path / "u.db")
    collector = CodexCollector(store, tmp_path / "does-not-exist")
    assert collector.scan_once() == 0
    assert not store.has_coverage(), "without Codex installed there is nothing to cover"


def test_folder_without_sessions_returns_zero(env):
    root, store = env
    root.mkdir()
    assert CodexCollector(store, root).scan_once() == 0


def test_coverage_starts_at_the_oldest_file_and_reaches_now(env):
    root, store = env
    f = install_fixture(root)
    old = time.time() - 5 * 86400
    os.utime(f, (old, old))

    before = time.time()
    CodexCollector(store, root).scan_once()
    (start, end), = store.coverage_between(0, time.time() + 10)
    assert start == pytest.approx(old, abs=1)
    assert end >= before


def test_default_codex_root_honors_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "other"))
    assert default_codex_root() == tmp_path / "other"
    monkeypatch.delenv("CODEX_HOME")
    assert default_codex_root() == Path.home() / ".codex"


def test_the_collector_name_is_codex(env):
    root, store = env
    assert CodexCollector(store, root).name == "codex"


def test_a_bucket_without_windows_does_not_overwrite_the_good_limits(tmp_path):
    """Codex also sends a `premium` bucket without windows, sometimes as the last one."""
    import json as _json

    root = tmp_path / ".codex"
    folder = root / "sessions" / "2024" / "12" / "31"
    folder.mkdir(parents=True)
    good = {"timestamp": "2025-01-01T03:44:34.485Z", "type": "event_msg", "payload": {
        "type": "token_count", "rate_limits": {
            "limit_id": "codex", "plan_type": "plus",
            "primary": {"used_percent": 21.0, "window_minutes": 300, "resets_at": 1735714800},
            "secondary": {"used_percent": 7.0, "window_minutes": 10080, "resets_at": 1736298000}}}}
    premium = {"timestamp": "2025-01-01T03:44:35.785Z", "type": "event_msg", "payload": {
        "type": "token_count", "rate_limits": {
            "limit_id": "premium", "plan_type": "plus", "primary": None, "secondary": None}}}
    (folder / "rollout-x.jsonl").write_text(
        _json.dumps(good) + "\n" + _json.dumps(premium) + "\n", encoding="utf-8")
    store = Store(tmp_path / "u.db")
    CodexCollector(store, root).scan_once()
    rl = load_rate_limits(store)
    assert rl["primary"]["used_percent"] == 21.0
    assert rl["secondary"]["used_percent"] == 7.0


def test_a_stored_limit_without_windows_is_replaced_even_if_it_is_newer(tmp_path):
    """Databases that stored the `premium` bucket in an earlier version recover on their own."""
    import json as _json

    store = Store(tmp_path / "u.db")
    store.set_state("codex_rate_limits", _json.dumps({
        "captured_ts": 9_999_999_999.0, "plan_type": "plus",
        "primary": None, "secondary": None, "credits": {"has_credits": False}}))
    root = tmp_path / ".codex"
    folder = root / "sessions" / "2024" / "12" / "31"
    folder.mkdir(parents=True)
    good = {"timestamp": "2025-01-01T03:44:34.485Z", "type": "event_msg", "payload": {
        "type": "token_count", "rate_limits": {
            "limit_id": "codex", "plan_type": "plus",
            "primary": {"used_percent": 21.0, "window_minutes": 300, "resets_at": 1735714800},
            "secondary": None}}}
    (folder / "rollout-y.jsonl").write_text(_json.dumps(good) + "\n", encoding="utf-8")
    CodexCollector(store, root).scan_once()
    assert load_rate_limits(store)["primary"]["used_percent"] == 21.0
