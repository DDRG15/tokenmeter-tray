"""The table of calls: paging that survives new calls, and exact per-call cost."""
from __future__ import annotations

import time

import pytest

from tests.test_dashboard import app_without_tk
from tokenmeter import dashboard
from tokenmeter.models import UsageEvent
from tokenmeter.pricing import usd_to_pico
from tokenmeter.ui.linux import call_row, new_calls_text

BASE = time.time() - 3600


def add(app, n, *, ts=None, model="claude-opus-5", provider="anthropic", source="proxy",
        billing="subscription", plan="", inp=1000, out=0, cr=0, cw=0, complete=True):
    app.store.add_event(UsageEvent(
        ts=BASE + n if ts is None else ts, provider=provider, model=model, input_tokens=inp,
        output_tokens=out, cache_read_tokens=cr, cache_write_tokens=cw, source=source,
        dedupe_key=f"c{n}", billing=billing, plan=plan, usage_complete=complete))


@pytest.fixture
def app(tmp_path):
    a = app_without_tk(tmp_path)
    yield a
    a.store.close()


def test_the_latest_calls_come_first_one_page_at_a_time(app):
    for n in range(7):
        add(app, n, inp=n + 1)
    first = dashboard.build_calls(app, limit=3)
    assert [c["input"] for c in first["calls"]] == [7, 6, 5]
    second = dashboard.build_calls(app, tuple(first["next"]), limit=3)
    assert [c["input"] for c in second["calls"]] == [4, 3, 2]
    third = dashboard.build_calls(app, tuple(second["next"]), limit=3)
    assert [c["input"] for c in third["calls"]] == [1] and third["next"] is None


def test_a_page_that_ends_exactly_on_the_oldest_call_says_there_is_no_more(app):
    for n in range(3):
        add(app, n)
    assert dashboard.build_calls(app, limit=3)["next"] is None
    assert dashboard.build_calls(app, limit=2)["next"] is not None


def test_new_calls_while_paging_neither_repeat_nor_skip_a_row(app):
    """Paging by position would shift every row when a call arrives; paging
    after the last row seen does not."""
    for n in range(6):
        add(app, n, inp=n + 1)
    first = dashboard.build_calls(app, limit=3)
    for n in (10, 11):
        add(app, n, inp=100 + n)                      # two calls arrive meanwhile
    second = dashboard.build_calls(app, tuple(first["next"]), limit=3)
    assert [c["input"] for c in first["calls"] + second["calls"]] == [6, 5, 4, 3, 2, 1]
    newest = first["calls"][0]
    assert app.store.events_newer_than(newest["ts"], newest["id"]) == 2


def test_calls_with_the_same_timestamp_are_all_listed_once(app):
    for n in range(5):
        add(app, n, ts=BASE, inp=n + 1)
    seen, before = [], None
    while True:
        page = dashboard.build_calls(app, before, limit=2)
        seen += [c["input"] for c in page["calls"]]
        if page["next"] is None:
            break
        before = tuple(page["next"])
    assert sorted(seen) == [1, 2, 3, 4, 5]


def test_the_filters_apply_to_the_list_and_to_the_new_call_count(app):
    add(app, 1, provider="anthropic", source="transcript")
    add(app, 2, provider="openai", source="codex")
    add(app, 3, provider="openai", source="proxy")
    assert [c["provider"] for c in dashboard.build_calls(app, provider="openai")["calls"]] == ["openai"] * 2
    assert [c["source"] for c in dashboard.build_calls(app, source="codex")["calls"]] == ["codex"]
    oldest = dashboard.build_calls(app)["calls"][-1]
    assert app.store.events_newer_than(oldest["ts"], oldest["id"], provider="openai") == 2
    assert app.store.events_newer_than(oldest["ts"], oldest["id"], source="transcript") == 0


def test_each_call_is_priced_exactly_and_never_guessed(app):
    add(app, 1, inp=1_000_000)                                           # $5.00 exactly
    add(app, 2, model="claude-haiku-5-5", inp=1_000_000)                 # base tier: a minimum
    add(app, 3, model="model-without-price", inp=1_000_000)              # no price: a dash
    add(app, 4, model="claude-opus-5", provider="ollama", billing="local", inp=1_000_000)
    by_n = {c["input"] and c["model"] + "/" + c["provider"]: c for c in dashboard.build_calls(app)["calls"]}
    assert (by_n["claude-opus-5/anthropic"]["cost"], by_n["claude-opus-5/anthropic"]["cost_text"]) == (
        str(usd_to_pico("5")), "$5.00")
    assert by_n["claude-haiku-5-5/anthropic"]["cost_text"] == "≥ $0.10"
    assert (by_n["model-without-price/anthropic"]["cost"],
            by_n["model-without-price/anthropic"]["cost_text"]) == (None, "—")
    # A local model that shares a name with a paid one costs nothing.
    assert (by_n["claude-opus-5/ollama"]["cost"], by_n["claude-opus-5/ollama"]["cost_text"]) == ("0", "local")


def test_a_call_from_the_future_is_not_listed(app):
    add(app, 1)
    add(app, 2, ts=time.time() + 3600)
    assert len(dashboard.build_calls(app)["calls"]) == 1


def test_a_row_shows_what_the_call_used_and_marks_a_cut_one(app):
    add(app, 1, inp=1200, out=300, cr=5000, cw=40, billing="subscription", plan="max", complete=False)
    (call,) = dashboard.build_calls(app)["calls"]
    row = call_row(call, show_cost=True)
    assert row[1:] == ("anthropic · claude-opus-5", "Subscription · Max", "1,200 (cut)", "300 (cut)",
                       "5,000 read · 40 written", row[6])
    assert row[6].startswith("$") and call_row(call, show_cost=False)[6] == ""


def test_the_new_call_notice_counts_in_words():
    assert new_calls_text(0) == "" and new_calls_text(-1) == ""
    assert new_calls_text(1) == "1 new call since this list was loaded"
    assert new_calls_text(1500) == "1,500 new calls since this list was loaded"
