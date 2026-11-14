"""The budget editor's form, the rows the window shows, and the native tabs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from tests.test_dashboard import app_without_tk
from tokenmeter import budgets, dashboard
from tokenmeter.budgets import (
    Budget, budget_from_form, form_from_budget, parse_thresholds, parse_token_limit, save_budget,
)
from tokenmeter.models import UsageEvent
from tokenmeter.pricing import usd_to_pico
from tokenmeter.ui.linux import budget_row, compact_lines, session_row

ROOT = Path(__file__).resolve().parents[1]
FORM = {"name": "Monthly cap", "metric": "money", "token_basis": "all", "period": "month",
        "scope_kind": "all", "scope_provider": "", "scope_model": "", "limit": "25",
        "thresholds": "60, 85, 100"}


# -- the form -----------------------------------------------------------------


@pytest.mark.parametrize("text, tokens", [
    ("500000", 500_000), ("500,000", 500_000), ("500_000", 500_000), ("500k", 500_000),
    ("2M", 2_000_000), ("2.5m", 2_500_000), (" 1 000 ", 1000), ("0.5k", 500),
])
def test_a_token_limit_is_read_as_typed(text, tokens):
    assert parse_token_limit(text) == tokens


@pytest.mark.parametrize("text", ["", "k", "lots", "0", "-5", "1.5", "0.0001k", "1e30", "nan", "inf",
                                  "5 dollars", "$5"])
def test_a_token_limit_that_is_not_a_whole_positive_number_is_refused(text):
    """Half a token, or a typo read as a huge number, would be a limit nobody set."""
    with pytest.raises(ValueError):
        parse_token_limit(text)


@pytest.mark.parametrize("text, marks", [
    ("60, 85, 100", (60, 85, 100)), ("60 85 100", (60, 85, 100)), ("50%", (50,)),
    ("", ()), ("  ", ()),
])
def test_thresholds_are_read_as_typed(text, marks):
    assert parse_thresholds(text) == marks


def test_thresholds_that_are_not_whole_percentages_are_refused():
    with pytest.raises(ValueError, match="whole percentages"):
        parse_thresholds("60, half")
    with pytest.raises(ValueError):
        budget_from_form({**FORM, "thresholds": "85, 60"})   # not in order
    with pytest.raises(ValueError):
        budget_from_form({**FORM, "thresholds": "0"})


def test_the_form_builds_the_budget_the_user_described():
    budget = budget_from_form(FORM)
    assert (budget.name, budget.metric, budget.period, budget.limit, budget.thresholds) == (
        "Monthly cap", "money", "month", usd_to_pico("25"), (60, 85, 100))
    assert budget.enabled and budget.timezone == budgets.system_timezone()


def test_the_limit_is_money_or_tokens_according_to_the_measure():
    """"25" is 25 dollars for the API equivalent and 25 tokens otherwise; "2M"
    is tokens and never two million dollars."""
    assert budget_from_form({**FORM, "metric": "tokens", "limit": "2M"}).limit == 2_000_000
    assert budget_from_form({**FORM, "metric": "money", "limit": "$0.50"}).limit == usd_to_pico("0.5")
    with pytest.raises(ValueError):
        budget_from_form({**FORM, "metric": "money", "limit": "2M"})
    with pytest.raises(ValueError):
        budget_from_form({**FORM, "metric": "tokens", "limit": "$5"})


@pytest.mark.parametrize("change", [
    {"name": ""}, {"metric": ""}, {"period": ""}, {"limit": ""}, {"limit": "0"},
    {"scope_kind": "provider"},                              # provider not given
    {"scope_kind": "model", "scope_provider": "anthropic"},  # model not given
])
def test_an_incomplete_form_is_refused_instead_of_guessed(change):
    with pytest.raises(ValueError):
        budget_from_form({**FORM, **change})


def test_fields_the_scope_does_not_use_are_dropped():
    """The dialog keeps what was typed in a field after the scope changes. It
    must not survive as a hidden filter."""
    budget = budget_from_form({**FORM, "scope_kind": "provider", "scope_provider": " openrouter ",
                               "scope_model": "left over"})
    assert (budget.scope_provider, budget.scope_model) == ("openrouter", "")
    everything = budget_from_form({**FORM, "scope_provider": "left over", "scope_model": "x"})
    assert (everything.scope_kind, everything.scope_provider, everything.scope_model) == ("all", "", "")


def test_editing_keeps_the_identity_so_given_notices_stay_given():
    original = Budget(**{**budget_from_form(FORM).__dict__, "enabled": False, "timezone": "Asia/Tokyo"})
    edited = budget_from_form({**FORM, "name": "Renamed", "limit": "40"}, original)
    assert (edited.id, edited.timezone, edited.enabled) == (original.id, "Asia/Tokyo", False)
    assert (edited.name, edited.limit) == ("Renamed", usd_to_pico("40"))


@pytest.mark.parametrize("form", [
    FORM,
    {**FORM, "limit": "0.5"}, {**FORM, "limit": "1234.56"},
    {**FORM, "metric": "tokens", "token_basis": "io", "limit": "500000", "thresholds": ""},
    {**FORM, "metric": "output", "period": "day", "scope_kind": "model",
     "scope_provider": "anthropic", "scope_model": "claude-opus-5", "limit": "42"},
])
def test_opening_the_editor_and_applying_without_changes_changes_nothing(form):
    budget = budget_from_form(form)
    assert budget_from_form(form_from_budget(budget), budget) == budget


def test_a_new_budget_starts_from_sensible_defaults():
    form = form_from_budget()
    assert set(form) == set(budgets.FORM_FIELDS)
    assert (form["metric"], form["period"], form["scope_kind"], form["thresholds"]) == (
        "money", "month", "all", "60, 85, 100")
    with pytest.raises(ValueError):
        budget_from_form(form)  # no name and no limit yet: nothing to save


# -- the rows -----------------------------------------------------------------


def state_of(app, now, **form):
    save_budget(app.store, budget_from_form({**FORM, **form}))
    return dashboard.build_budgets(app, now)


def test_a_budget_row_shows_usage_measure_and_state(tmp_path):
    app = app_without_tk(tmp_path)
    now = time.time()
    app.store.add_event(UsageEvent(ts=now - 60, provider="anthropic", model="claude-opus-5",
                                   input_tokens=1_000_000, output_tokens=0, dedupe_key="a"))
    data = state_of(app, now, limit="6")
    assert data["invalid"] == 0
    (row,) = [budget_row(b) for b in data["budgets"]]
    assert row[1:] == ("Monthly cap", "▲ $5.00 of $6.00 · 83%", "API equivalent · all usage · this month")


def test_a_minimum_shows_its_reason_in_the_row(tmp_path):
    """"≥" alone does not say what is missing."""
    app = app_without_tk(tmp_path)
    now = time.time()
    for key, model in (("a", "claude-opus-5"), ("b", "model-without-price")):
        app.store.add_event(UsageEvent(ts=now - 60, provider="anthropic", model=model,
                                       input_tokens=1_000_000, output_tokens=0, dedupe_key=key))
    (data,) = state_of(app, now, period="day", limit="4")["budgets"]
    _id, name, usage, detail = budget_row(data)
    assert usage == "■ ≥ $5.00 of $4.00 · at least 125%"
    assert detail == ("API equivalent · all usage · this day\n"
                      "Minimum: 1 call of a model without a verified price is not in the amount")


def test_a_budget_with_notices_off_says_so(tmp_path):
    app = app_without_tk(tmp_path)
    budget = budgets.set_enabled(budget_from_form(FORM), False)
    save_budget(app.store, budget)
    (data,) = dashboard.build_budgets(app, time.time())["budgets"]
    assert budget_row(data)[1] == "Monthly cap (notices off)"
    assert budget_row(data)[2] == "$0.00 of $25.00 · 0%"


def test_unreadable_stored_budgets_are_counted_for_the_window(tmp_path):
    app = app_without_tk(tmp_path)
    app.store.save_budget_row({**budget_from_form(FORM).to_row(), "limit_value": -1})
    assert dashboard.build_budgets(app, time.time()) == {"budgets": [], "invalid": 1}


def test_a_session_row_marks_the_current_one_and_cut_calls():
    base = {"start_text": "Thu 12/11 19:50", "end_text": "00:50", "used": 12_345, "calls": 7,
            "incomplete_calls": 0, "percent": 12.3, "source_text": "from Claude Code"}
    assert session_row({**base, "active": True}) == (
        "Thu 12/11 19:50 – 00:50 · now", "12,345", "7", "12%", "from Claude Code")
    assert session_row({**base, "active": False, "incomplete_calls": 2})[:3] == (
        "Thu 12/11 19:50 – 00:50", "12,345", "7 (2 cut)")


# -- the native window --------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux" or not os.environ.get("DISPLAY"),
                    reason="requires an explicit Linux graphical test session")
def test_the_native_tabs_add_edit_silence_and_delete_a_budget(tmp_path):
    for folder in ("tmp", "cache", "config", "share", "state", "data"):
        (tmp_path / folder).mkdir()
    env = dict(os.environ, TOKENMETER_HOME=str(tmp_path / "data"),
               TMPDIR=str(tmp_path / "tmp"), XDG_CACHE_HOME=str(tmp_path / "cache"),
               XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_DATA_HOME=str(tmp_path / "share"),
               XDG_STATE_HOME=str(tmp_path / "state"), PYTHONDONTWRITEBYTECODE="1",
               GSETTINGS_BACKEND="memory", GIO_USE_VFS="local", NO_AT_BRIDGE="1")
    output = tmp_path / "result.json"
    with (tmp_path / "child.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.linux_budgets_app", str(output)],
            cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            code = process.wait(timeout=40)
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
    assert code == 0, (tmp_path / "child.log").read_text()
    r = json.loads(output.read_text())
    assert r["errors"] == [] and r["unanswered"] == 0 and r["closed"]
    assert "No budgets yet" in r["empty_note"]

    # Collapsed first; "Details" expands without closing anything, and the
    # choice is remembered for the next start.
    assert r["collapsed_at_start"] and r["button_collapsed"] == "Details"
    assert r["expanded"] and r["button_expanded"] == "Hide details" and r["still_running"]
    assert r["collapsed_height"] < r["expanded_height"]
    assert r["remembered"] == "1"
    lines = r["compact"].splitlines()
    assert lines[0].startswith("Claude session: ") and "resets at" in lines[0]
    assert lines[-1].startswith("Data quality: ")

    # Added: 190 tokens seen against a limit of 100 → exceeded, shown with its mark.
    ((_, name, usage, detail),) = r["after_add"]
    assert (name, usage) == ("Daily tokens", "■ 190 of 100 · 190%")
    assert detail == "tokens (input + output) · all usage · this day"
    # A refused or cancelled dialog stores nothing.
    assert (r["after_invalid"], r["after_cancel"]) == (1, 1)

    # Edited in place: only openai's 70 tokens against 1,000.
    ((_, name, usage, detail),) = r["after_edit"]
    assert (name, usage) == ("Daily tokens", "70 of 1,000 · 7%")
    assert detail == "tokens (input + output) · openai · this day"
    assert r["id_kept"] and r["selection_kept"]

    # Before the edit the budget was past 100%; the tick after the edit owes
    # nothing new, and a second tick repeats nothing.
    assert r["notices"] == []
    assert r["after_toggle"] == "Daily tokens (notices off)"
    assert r["after_toggle_back"] == "Daily tokens"

    ((when, tokens, calls, _percent, source),) = r["sessions"]
    assert when.endswith("· now") and (tokens, calls, source) == ("120", "1", "estimated")

    assert [row[1] for row in r["calls"]] == ["anthropic · synthetic-anthropic",
                                              "openai · synthetic-openai"] or \
        [row[1] for row in r["calls"]] == ["openai · synthetic-openai",
                                           "anthropic · synthetic-anthropic"]
    assert all(row[2] == "Subscription" and row[6] == "—" for row in r["calls"]), "no price: a dash"
    assert r["calls_note"] == "All 2 recorded calls are listed." and r["more_enabled"] is False
    assert r["calls_openai"] == ["openai · synthetic-openai"]
    assert r["calls_cost_hidden"] == [""]

    assert (r["after_delete_no"], r["after_delete_yes"], r["stored_after_delete"]) == (1, 0, 0)
    assert r["calls_kept"] == 2, "deleting a budget never touches the usage history"
    assert r["dialogs"] == ["New budget", "New budget", "Operation failed", "New budget",
                            "Operation failed", "Edit budget", "Delete budget", "Delete budget"]


def test_reasons_are_written_for_one_call_and_for_several(tmp_path):
    app = app_without_tk(tmp_path)
    now = time.time()
    for key in "abc":
        app.store.add_event(UsageEvent(ts=now - 60, provider="anthropic", model="claude-haiku-5-5",
                                       input_tokens=10, output_tokens=0, dedupe_key=key,
                                       usage_complete=key != "c"))
    (data,) = state_of(app, now, period="day", limit="4")["budgets"]
    assert data["cause_lines"] == [
        "3 calls are priced at the base tier, which is a minimum",
        "1 call was cut before its usage arrived",
    ]
    assert budget_row(data)[3].count("\nMinimum: ") == 2


# -- the collapsed window -----------------------------------------------------


def compact_state(app, monkeypatch=None):
    now = time.time()
    return dashboard.build_state(app, now), dashboard.build_month(app, now)


def test_the_collapsed_window_shows_session_week_month_and_quality(tmp_path):
    app = app_without_tk(tmp_path)
    app.store.add_event(UsageEvent(ts=time.time() - 60, provider="anthropic", model="claude-opus-5",
                                   input_tokens=50_000, output_tokens=0, dedupe_key="a",
                                   billing="subscription"))
    # Two days and a half hour: the text is "2d 0h" whether the clock moved a
    # millisecond or a few seconds since the reset was set.
    app.weekly.set_reset_in(2 * 86400 + 1800)
    lines = compact_lines(*compact_state(app))
    assert lines[0].startswith("Claude session: 50% · 04:5") and lines[0].endswith("· estimated")
    assert lines[1].startswith("Claude week: ") and "resets in 2d 0h" in lines[1]
    assert lines[2].endswith(": 50,000 tokens in 1 calls · $0.25 API equivalent")
    assert lines[3] == "Data quality: the session reset is estimated"
    assert len(lines) == 4


def test_the_quality_line_is_always_there_and_names_every_gap(tmp_path):
    """An empty line would read as "all fine" both when it is and when nobody
    checked, so it says so in words either way."""
    app = app_without_tk(tmp_path)
    assert compact_lines(*compact_state(app))[-1] == (
        "Data quality: nothing missing in what was recorded")
    now = time.time()
    for key, model, complete in (("a", "claude-haiku-5-5", True), ("b", "claude-opus-5", False)):
        app.store.add_event(UsageEvent(ts=now - 60, provider="anthropic", model=model,
                                       input_tokens=10, output_tokens=0, dedupe_key=key,
                                       usage_complete=complete))
    quality = compact_lines(*compact_state(app))[-1]
    assert "priced at their base tier, so the amount is a minimum" in quality
    assert "1 call was cut before the usage arrived" in quality
    assert "the session reset is estimated" in quality


def test_a_model_without_a_price_is_named_in_the_quality_line(tmp_path):
    app = app_without_tk(tmp_path)
    app.store.add_event(UsageEvent(ts=time.time() - 60, provider="anthropic",
                                   model="model-without-price", input_tokens=10, output_tokens=0,
                                   dedupe_key="a"))
    lines = compact_lines(*compact_state(app))
    # No priced call at all: a dash, never "$0.00", and the reason below.
    assert lines[2].endswith("· — API equivalent")
    assert "some models have no verified price" in lines[-1]


def test_hiding_costs_hides_the_month_amount_and_its_price_notes(tmp_path):
    app = app_without_tk(tmp_path)
    app.config.show_cost = False
    app.store.add_event(UsageEvent(ts=time.time() - 60, provider="anthropic",
                                   model="model-without-price", input_tokens=10, output_tokens=0,
                                   dedupe_key="a"))
    lines = compact_lines(*compact_state(app))
    assert "API equivalent" not in "\n".join(lines) and "price" not in lines[-1]
    assert lines[2].endswith(": 10 tokens in 1 calls")


def test_without_a_session_or_a_week_it_says_so(tmp_path):
    app = app_without_tk(tmp_path)
    lines = compact_lines(*compact_state(app))
    assert lines[0].startswith("Claude session: none in progress")
    assert lines[1] == "Claude week: reset time not known yet"
    app.config.weekly_enabled = False
    assert not any(line.startswith("Claude week") for line in compact_lines(*compact_state(app)))


def test_the_month_is_the_calendar_month_and_reports_lost_history(tmp_path):
    from tokenmeter.store import Store
    app = app_without_tk(tmp_path)
    app.store.close()
    app.store = Store(tmp_path / "short.db", retain_days=0)
    app.session._store = app.weekly._store = app.store  # noqa: SLF001
    now = time.time()
    month = dashboard.build_month(app, now)
    assert month["truncated"] is True and month["calls"] == 0
    assert "the month starts before the history that is kept" in compact_lines(
        dashboard.build_state(app, now), month)[-1]


def test_the_month_name_does_not_follow_the_system_language(tmp_path, monkeypatch):
    """The window is in English; a Spanish system must not turn one word of it
    into "noviembre"."""
    import locale
    app = app_without_tk(tmp_path)
    for name in ("es_PE.UTF-8", "es_ES.UTF-8", "de_DE.UTF-8"):
        try:
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    try:
        label = dashboard.build_month(app, 1_794_500_000.0)["label"]  # 12 November 2026
    finally:
        locale.setlocale(locale.LC_TIME, "C")
    assert label == "November 2026"
