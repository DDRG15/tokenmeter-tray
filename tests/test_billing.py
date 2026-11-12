"""How each call is paid for.

Only what is paid with an API key is money spent; the equivalent of a
subscription is included in the plan. Confusing the two presents money that was
never spent as if it had been, so whatever cannot be inferred stays `unknown`,
never assumed.
"""

from __future__ import annotations

import json
import time

import pytest

from tests.test_dashboard import app_without_tk, call
from tests.test_proxy import ANTHROPIC_JSON, post  # noqa: F401 - fixtures below
from tests.test_proxy import proxy, upstream  # noqa: F401 - pytest fixtures
from tests.test_transcripts import assistant_line, write
from tokenmeter import dashboard
from tokenmeter.billing import (
    UNKNOWN,
    CachedBilling,
    apply_override,
    claude_code_billing,
    classify_credential,
    classify_request,
    label,
)
from tokenmeter.collectors.transcripts import TranscriptCollector
from tokenmeter.models import UsageEvent
from tokenmeter.store import Store


# -- shape of the credential --------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("sk-ant-oat01-xxxx", "subscription"),   # Claude Code OAuth (Pro/Max)
    ("sk-ant-api03-xxxx", "api"),
    ("eyJhbGciOiJSUzI1NiJ9.e30.x", "subscription"),  # JWT: ChatGPT login
    ("sk-proj-xxxx", "api"),
    ("gsk_xxxx", "api"),                      # Groq
    ("", "unknown"),
])
def test_type_from_the_shape_of_the_credential(value, expected):
    assert classify_credential(value) == expected


def test_proxy_requests():
    assert classify_request({"x-api-key": "sk-ant-api03-a"}, "https://api.anthropic.com") == ("api", "")
    assert classify_request({"Authorization": "Bearer sk-ant-oat01-a"},
                            "https://api.anthropic.com") == ("subscription", "")
    assert classify_request({"x-goog-api-key": "AIza"}, "https://generativelanguage.googleapis.com") == ("api", "")
    assert classify_request({}, "https://generativelanguage.googleapis.com", {"key": "AIza"}) == ("api", "")
    assert classify_request({}, "http://127.0.0.1:11434") == ("local", "")
    assert classify_request({}, "https://api.openai.com") == UNKNOWN


def test_a_local_gateway_with_a_key_is_api_not_local():
    """LiteLLM on localhost forwards to a paid API: "local, no cost" would be false."""
    assert classify_request({"Authorization": "Bearer sk-litellm-0123456789abcdefghij"},
                            "http://127.0.0.1:4000") == ("api", "")


DUMMY_KEYS = ["ollama", "sk-local", "lm-studio", "not-needed", "sk-no-key-required", "EMPTY",
              "dummy", "token-abc123"]


@pytest.mark.parametrize("key", DUMMY_KEYS)
@pytest.mark.parametrize("upstream", ["http://127.0.0.1:11434", "http://localhost:1234", "http://[::1]:8080"])
def test_a_dummy_key_to_a_local_upstream_is_local(key, upstream):
    """Clients whose SDK insists on a key send one: it must not turn local usage into paid API."""
    assert classify_request({"Authorization": f"Bearer {key}"}, upstream) == ("local", "")
    assert classify_request({"x-api-key": key}, upstream) == ("local", "")
    assert classify_request({"api-key": key}, upstream) == ("local", "")
    assert classify_request({}, upstream, {"key": key}) == ("local", "")


def test_a_listed_placeholder_is_local_even_when_it_is_long_enough_to_pass_for_a_key():
    value = "sk-your-api-key-here"
    assert len(value) >= 20
    assert classify_request({"Authorization": f"Bearer {value.upper()}"}, "http://127.0.0.1:11434") == ("local", "")


@pytest.mark.parametrize("key", [
    "sk-litellm-0123456789abcdefghij",            # gateway virtual key
    "sk-ant-api03-" + "a" * 90,                   # Anthropic
    "sk-proj-" + "b" * 60,                        # OpenAI
    "gsk_" + "c" * 52,                            # Groq
    "x" * 20,                                     # unknown shape, long enough to be a key
    "sk-ant-api03-secret",                        # short, but carries a vendor prefix
])
def test_a_real_looking_key_to_a_local_gateway_stays_api(key):
    """LiteLLM on localhost forwards to a paid API: a real key keeps counting as money."""
    assert classify_request({"Authorization": f"Bearer {key}"}, "http://127.0.0.1:4000") == ("api", "")
    assert classify_request({"x-api-key": key}, "http://localhost:4000") == ("api", "")


def test_a_real_key_anywhere_in_the_request_wins_over_a_dummy_one():
    headers = {"x-api-key": "ollama", "Authorization": "Bearer sk-ant-api03-" + "a" * 90}
    assert classify_request(headers, "http://127.0.0.1:4000") == ("api", "")
    assert classify_request({"x-api-key": "ollama"}, "http://127.0.0.1:4000",
                            {"key": "AIza" + "d" * 35}) == ("api", "")


def test_a_subscription_credential_is_never_taken_for_a_placeholder():
    assert classify_request({"Authorization": "Bearer sk-ant-oat01-a"},
                            "http://127.0.0.1:4000") == ("subscription", "")
    assert classify_request({"Authorization": "Bearer eyJ.e30.x"},
                            "http://127.0.0.1:4000") == ("subscription", "")


@pytest.mark.parametrize("key", [*DUMMY_KEYS, "x"])
def test_a_dummy_key_to_a_remote_upstream_is_classified_as_before(key):
    """Only a local destination makes a placeholder harmless."""
    assert classify_request({"Authorization": f"Bearer {key}"}, "https://api.openai.com") == ("api", "")
    assert classify_request({"x-api-key": key}, "https://api.anthropic.com") == ("api", "")
    assert classify_request({}, "https://generativelanguage.googleapis.com", {"key": key}) == ("api", "")


def test_a_blank_credential_to_a_remote_upstream_stays_unknown():
    assert classify_request({"x-api-key": "  "}, "https://api.openai.com") == UNKNOWN


def test_the_proxy_records_a_dummy_key_to_a_local_upstream_as_local(proxy):  # noqa: F811
    collector, store = proxy
    post(collector, "/anthropic/v1/messages", headers={"Authorization": "Bearer ollama"})
    deadline = time.time() + 15
    while time.time() < deadline and not stored_billing(store):
        time.sleep(0.05)
    assert [tuple(r) for r in stored_billing(store)] == [("local", "")]


# -- Claude Code authentication on disk --------------------------------------------------


def home_dir(tmp_path, *, oauth_plan=None, api_key=False, helper=False):
    (tmp_path / ".claude").mkdir(exist_ok=True)
    if oauth_plan is not None:
        (tmp_path / ".claude" / ".credentials.json").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "x", "subscriptionType": oauth_plan}}), encoding="utf-8")
    if api_key:
        (tmp_path / ".claude.json").write_text(json.dumps({"primaryApiKey": "x"}), encoding="utf-8")
    if helper:
        (tmp_path / ".claude" / "settings.json").write_text(
            json.dumps({"apiKeyHelper": "x.sh"}), encoding="utf-8")
    return tmp_path


def test_claude_code_with_a_subscription(tmp_path):
    assert claude_code_billing(home_dir(tmp_path, oauth_plan="pro")) == ("subscription", "pro")


@pytest.mark.parametrize("kw", [{"api_key": True}, {"helper": True}])
def test_claude_code_with_a_key(tmp_path, kw):
    assert claude_code_billing(home_dir(tmp_path, **kw)) == ("api", "")


def test_claude_code_with_both_it_is_unknown(tmp_path):
    """With OAuth and a key at once there is no way to know which one each call used."""
    assert claude_code_billing(home_dir(tmp_path, oauth_plan="max", api_key=True)) == UNKNOWN


def test_claude_code_with_nothing_or_broken(tmp_path):
    assert claude_code_billing(home_dir(tmp_path)) == UNKNOWN
    (tmp_path / ".claude" / ".credentials.json").write_text("{broken", encoding="utf-8")
    assert claude_code_billing(tmp_path) == UNKNOWN


def test_the_cache_rereads_after_the_ttl():
    calls = []
    c = CachedBilling(lambda: calls.append(1) or ("api", ""), ttl=0.0)
    c.get(), c.get()
    assert len(calls) == 2
    c = CachedBilling(lambda: calls.append(1) or ("api", ""), ttl=3600)
    c.get(), c.get()
    assert len(calls) == 3


# -- manual override ---------------------------------------------------------------------


def test_the_override_only_fills_what_is_unknown():
    over = {"groq": "api", "anthropic": "subscription", "odd": "free"}
    assert apply_override("groq", UNKNOWN, over) == ("api", "")
    assert apply_override("anthropic", ("api", ""), over) == ("api", "")  # what was detected rules
    assert apply_override("odd", UNKNOWN, over) == UNKNOWN                # invalid value
    assert apply_override("groq", UNKNOWN, None) == UNKNOWN


def test_labels():
    assert label("subscription", "pro") == "Subscription · Pro"
    assert label("subscription") == "Subscription"
    assert label("api") == "Paid API"
    assert label("whatever") == "Unknown billing"


# -- collectors ------------------------------------------------------------------------------


class Fixed:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def stored_billing(store):
    return store._conn.execute("SELECT billing, plan FROM usage_events").fetchall()  # noqa: SLF001


def test_transcripts_are_tagged_with_claude_code_authentication(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    write(root / "s.jsonl", [assistant_line()])
    store = Store(tmp_path / "u.db")
    TranscriptCollector(store, root, billing=Fixed(("subscription", "pro"))).scan_once()
    assert [tuple(r) for r in stored_billing(store)] == [("subscription", "pro")]


def test_transcripts_use_the_override_if_nothing_is_detected(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    write(root / "s.jsonl", [assistant_line()])
    store = Store(tmp_path / "u.db")
    TranscriptCollector(store, root, billing=Fixed(UNKNOWN),
                        overrides={"anthropic": "api"}).scan_once()
    assert [tuple(r) for r in stored_billing(store)] == [("api", "")]


def test_proxy_tags_by_credential_without_storing_it(proxy):  # noqa: F811
    collector, store = proxy
    post(collector, "/anthropic/v1/messages", headers={"x-api-key": "sk-ant-api03-secret"})
    deadline = time.time() + 15  # the proxy records after responding
    while time.time() < deadline and not stored_billing(store):
        time.sleep(0.05)
    assert [tuple(r) for r in stored_billing(store)] == [("api", "")]
    dump = "\n".join(store._conn.iterdump())  # noqa: SLF001
    assert "secret" not in dump


def test_an_old_database_gains_the_columns(tmp_path):
    """A database from before billing was tracked is migrated on open and its rows stay `unknown`."""
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE usage_events (id INTEGER PRIMARY KEY, ts REAL, provider TEXT,
                   model TEXT, input_tokens INTEGER, output_tokens INTEGER,
                   cache_write_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
                   source TEXT, dedupe_key TEXT UNIQUE, usage_complete INTEGER DEFAULT 1)""")
    con.execute("INSERT INTO usage_events (ts, provider, model, input_tokens, output_tokens,"
                " source, dedupe_key) VALUES (1, 'anthropic', 'm', 1, 1, 'proxy', 'k')")
    con.commit()
    con.close()
    store = Store(db)
    (row,) = store.totals_by_model(0)
    assert (row.billing, row.plan) == ("unknown", "")


# -- what the window sees --------------------------------------------------------------------


def with_billing(app, io, key, billing, plan="", model="claude-opus-5"):
    app.store.add_event(UsageEvent(ts=time.time() - 60, provider="anthropic", model=model,
                                   input_tokens=io, output_tokens=0, source="proxy",
                                   dedupe_key=key, billing=billing, plan=plan))


def test_paid_via_api_versus_subscription_equivalent(tmp_path):
    app = app_without_tk(tmp_path)
    with_billing(app, 1_000_000, "a", "subscription", "pro")
    with_billing(app, 200_000, "b", "api")
    state = dashboard.build_state(app)

    # Opus 5 at $5/M of input.
    assert state["cost"]["subscription_equivalent"]["value"] == pytest.approx(5.0)
    assert state["cost"]["api_spend"]["value"] == pytest.approx(1.0)
    assert state["cost"]["value"] == pytest.approx(6.0)
    kinds = {(b["kind"], b["plan"]): b for b in state["billing"]}
    assert kinds[("subscription", "pro")]["label"] == "Subscription · Pro"
    assert kinds[("api", "")]["calls"] == 1
    # The same model with two ways of paying is two rows.
    assert {(m["model"], m["billing"]) for m in state["models"]} == {
        ("claude-opus-5", "subscription"), ("claude-opus-5", "api")}


def test_without_api_what_was_paid_is_a_certain_zero(tmp_path):
    app = app_without_tk(tmp_path)
    with_billing(app, 1_000, "a", "subscription", "pro")
    assert dashboard.build_state(app)["cost"]["api_spend"] == {
        "value": 0.0, "text": "$0.00", "partial": False, "lower_bound": False}


def test_api_without_a_price_does_not_invent_money(tmp_path):
    app = app_without_tk(tmp_path)
    with_billing(app, 1_000, "a", "api", model="model-without-price")
    spend = dashboard.build_state(app)["cost"]["api_spend"]
    assert spend["value"] is None and spend["text"] == "—"


def local_call(app, io, key, model="llama3.2"):
    app.store.add_event(UsageEvent(ts=time.time() - 60, provider="ollama", model=model,
                                   input_tokens=io, output_tokens=0, source="proxy",
                                   dedupe_key=key, billing="local"))


def test_local_usage_is_an_exact_zero_and_never_partial(tmp_path):
    """A local model has no price, but it also costs nothing: the total stays exact."""
    app = app_without_tk(tmp_path)
    with_billing(app, 200_000, "a", "api")
    local_call(app, 5_000, "b")
    state = dashboard.build_state(app)

    assert state["cost"]["partial"] is False
    assert state["cost"]["text"] == "$1.00"  # not "$1.00 (partial)"
    assert state["cost"]["api_spend"]["value"] == pytest.approx(1.0)
    assert state["cost"]["api_spend"]["partial"] is False
    (local,) = [b for b in state["billing"] if b["kind"] == "local"]
    assert local["cost"] == 0.0 and local["cost_text"] == "$0.00"
    (row,) = [m for m in state["models"] if m["billing"] == "local"]
    assert row["cost"] == 0.0 and row["cost_text"] == "local"


def test_only_local_usage_totals_a_certain_zero(tmp_path):
    app = app_without_tk(tmp_path)
    local_call(app, 5_000, "a")
    cost = dashboard.build_state(app)["cost"]
    assert (cost["value"], cost["text"], cost["partial"]) == (0.0, "$0.00", False)
    assert cost["api_spend"]["value"] == 0.0 and cost["api_spend"]["partial"] is False


def test_a_local_model_is_not_priced_like_the_paid_model_it_is_named_after(tmp_path):
    """A local copy served under a paid name has no API equivalent."""
    app = app_without_tk(tmp_path)
    local_call(app, 1_000_000, "a", model="claude-opus-5")
    state = dashboard.build_state(app)
    assert state["cost"]["value"] == 0.0
    assert state["models"][0]["cost"] == 0.0


def test_an_unpriced_paid_model_still_marks_the_total_partial_next_to_local_usage(tmp_path):
    app = app_without_tk(tmp_path)
    with_billing(app, 1_000, "a", "api", model="model-without-price")
    local_call(app, 5_000, "b")
    state = dashboard.build_state(app)
    # The missing price is the paid model's, not the local one's.
    assert state["cost"]["partial"] is True and state["cost"]["value"] is None
    (local,) = [b for b in state["billing"] if b["kind"] == "local"]
    assert local["cost"] == 0.0


def test_history_and_mcp_share_the_same_local_rule(tmp_path):
    """The history tab, the Tk window and the MCP summary price rows with the same functions."""
    from tokenmeter import usage_query
    from tokenmeter.models import BILLING_LOCAL

    assert usage_query._models is dashboard._models  # noqa: SLF001
    app = app_without_tk(tmp_path)
    with_billing(app, 200_000, "a", "api")
    local_call(app, 5_000, "b")
    history = dashboard.build_history(app, 7)
    assert history["cost"]["partial"] is False and history["cost"]["text"] == "$1.00"
    # A local zero is exact: never a missing price and never a minimum.
    (local_row,) = [r for r, c, floor in dashboard.priced_models(app.store.totals_by_model_day(0))
                    if r.billing == BILLING_LOCAL and c == 0.0 and floor is False]
    assert local_row.model == "llama3.2"


def test_the_linux_model_line_says_local_instead_of_an_api_equivalent():
    from tokenmeter.ui.linux import model_line

    local = {"provider": "ollama", "model": "llama3.2", "io": 5_000, "billing": "local", "plan": "",
             "cost_text": "local"}
    paid = {"provider": "groq", "model": "m", "io": 10, "billing": "api", "plan": "", "cost_text": "$1.00"}
    assert model_line(local, True) == "ollama · llama3.2 (Local): 5,000 tokens · local, no cost"
    assert model_line(paid, True) == "groq · m (Paid API): 10 tokens · $1.00 API equivalent"
    assert model_line(local, False) == "ollama · llama3.2 (Local): 5,000 tokens"


# -- Codex limits -------------------------------------------------------------------------------


def limits(app, now, primary_reset, pct=12.0):
    app.store.set_state("codex_rate_limits", json.dumps({
        "captured_ts": now - 180, "plan_type": "plus",
        "primary": {"used_percent": pct, "window_minutes": 300, "resets_at": primary_reset},
        "secondary": {"used_percent": 68.0, "window_minutes": 10080, "resets_at": now + 5 * 86400},
        "credits": {"has_credits": False}}))


def test_codex_shows_its_real_limits(tmp_path):
    app = app_without_tk(tmp_path)
    now = time.time()
    limits(app, now, now + 3 * 3600, pct=70.0)
    codex = dashboard.build_codex(app.store, app.config, now)
    assert codex["plan"] == "plus" and codex["stale"] is False
    assert codex["session"]["used_percent"] == 70.0 and codex["session"]["level"] == "warn"
    assert codex["week"]["used_percent"] == 68.0
    assert codex["age_text"] == "3 min ago"


def test_codex_with_an_already_reset_window_does_not_show_the_old_percentage(tmp_path):
    app = app_without_tk(tmp_path)
    now = time.time()
    limits(app, now, now - 60)
    session = dashboard.build_codex(app.store, app.config, now)["session"]
    assert session["used_percent"] is None and session["level"] == "idle"


def test_codex_stale_data_is_flagged(tmp_path):
    app = app_without_tk(tmp_path)
    now = time.time()
    limits(app, now - 7 * 3600, now + 3600)
    assert dashboard.build_codex(app.store, app.config, now)["stale"] is True


def test_without_codex_there_is_no_card(tmp_path):
    assert dashboard.build_state(app_without_tk(tmp_path))["codex"] is None


# -- history floor and older databases ------------------------------------------------------------


def test_nothing_before_the_floor_enters_the_database(tmp_path):
    """A new collector after --start-fresh must not bring back discarded days."""
    store = Store(tmp_path / "u.db")
    store.set_coverage_floor(1_000.0)
    old = UsageEvent(ts=999.0, provider="openai", model="m", input_tokens=1,
                     output_tokens=1, source="codex", dedupe_key="codex:old")
    new = UsageEvent(ts=1_001.0, provider="openai", model="m", input_tokens=1,
                     output_tokens=1, source="codex", dedupe_key="codex:new")
    assert store.add_event(old) is False
    assert store.add_events([old, new]) == 1
    assert store.totals_since(0).calls == 1


def test_the_floor_survives_reopening_the_database(tmp_path):
    Store(tmp_path / "u.db").set_coverage_floor(1_000.0)
    store = Store(tmp_path / "u.db")
    assert store.add_event(UsageEvent(ts=10.0, provider="x", model="m", input_tokens=1,
                                      output_tokens=1, dedupe_key="k")) is False


def test_old_transcript_events_are_tagged_once(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    store = Store(tmp_path / "u.db")
    store.add_event(UsageEvent(ts=time.time(), provider="anthropic", model="m",
                               input_tokens=1, output_tokens=1, source="transcript",
                               dedupe_key="req:old"))
    store.add_event(UsageEvent(ts=time.time(), provider="openai", model="m",
                               input_tokens=1, output_tokens=1, source="proxy",
                               dedupe_key="req:proxy"))
    TranscriptCollector(store, root, billing=Fixed(("subscription", "pro"))).scan_once()
    rows = dict(store._conn.execute(  # noqa: SLF001
        "SELECT source, billing FROM usage_events").fetchall())
    assert rows == {"transcript": "subscription", "proxy": "unknown"}
