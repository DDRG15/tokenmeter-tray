"""The Claude percentage only counts what the Claude plan consumes.

Real incident (16/10, 00:41): when the Codex collector arrived, its 1.6 M tokens
went into the Claude session. The ring showed 355.7%, the "budget exhausted"
alert fired and the session was anchored at 20:00, the time of the first Codex
call. None of the three was true.
"""

from __future__ import annotations

from datetime import datetime

from tokenmeter.config import Config
from tokenmeter.models import UsageEvent
from tokenmeter.session import SessionTracker
from tokenmeter.store import Store
from tokenmeter.weekly import WeeklyTracker

NOW = datetime(2026, 10, 16, 0, 41).timestamp()


def ev(store, hour, minute, io, key, *, provider="anthropic", source="transcript",
       billing="subscription", day=15):
    ts = datetime(2026, 10, day, hour, minute).timestamp()
    store.add_event(UsageEvent(ts=ts, provider=provider, model="m", input_tokens=io,
                               output_tokens=0, source=source, dedupe_key=key,
                               billing=billing))


def scenario(tmp_path, scope="claude"):
    store = Store(tmp_path / "u.db")
    cfg = Config(session_budget_tokens=500_000, percent_scope=scope)
    ev(store, 20, 0, 1_600_000, "codex:1", provider="openai", source="codex")
    ev(store, 21, 30, 166_833, "req:claude")
    ev(store, 22, 0, 5_000, "req:groq", provider="groq", source="proxy", billing="api")
    ev(store, 22, 5, 7_000, "req:key", source="proxy", billing="api")  # Anthropic with an API key
    return store, cfg


def test_the_claude_percentage_does_not_add_codex_or_apis(tmp_path):
    store, cfg = scenario(tmp_path)
    snap = SessionTracker(store, cfg).snapshot(NOW)
    assert snap.used_tokens == 166_833
    assert round(snap.percent, 1) == 33.4
    assert datetime.fromtimestamp(snap.start_ts) == datetime(2026, 10, 15, 21, 30)


def test_the_per_model_breakdown_still_shows_everything(tmp_path):
    """Inside the Claude window, the breakdown shows everything used, not only Claude."""
    store, cfg = scenario(tmp_path)
    ev(store, 23, 0, 900, "codex:2", provider="openai", source="codex")
    models = SessionTracker(store, cfg).by_model(NOW)
    assert {m.provider for m in models} == {"anthropic", "openai", "groq"}


def test_scope_all_restores_the_previous_behavior(tmp_path):
    store, cfg = scenario(tmp_path, scope="all")
    snap = SessionTracker(store, cfg).snapshot(NOW)
    assert snap.used_tokens == 1_600_000 + 166_833 + 5_000 + 7_000


def test_the_week_also_counts_only_claude(tmp_path):
    store, cfg = scenario(tmp_path)
    week = WeeklyTracker(store, cfg)
    week.set_reset_at(datetime(2026, 10, 22, 3, 0).timestamp())
    assert week.snapshot(NOW).used_tokens == 166_833


def test_anthropic_events_without_a_billing_type_still_count(tmp_path):
    """Databases from before billing was tracked: Anthropic's `unknown` counts, as before."""
    store = Store(tmp_path / "u.db")
    cfg = Config()
    ev(store, 23, 0, 1_000, "req:old", billing="unknown")
    assert SessionTracker(store, cfg).snapshot(NOW).used_tokens == 1_000
