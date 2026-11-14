"""Personal budgets: definitions, calendar periods, exact sums, honesty and notices.

Every test uses a fixed clock and a fixed time zone. Nothing here sleeps or
reads the real time of day.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import pytest

from tokenmeter import budgets
from tokenmeter.budgets import Budget, due_alert, evaluate, load_budgets, period_bounds, save_budget
from tokenmeter.models import UsageEvent
from tokenmeter.pricing import (
    PICO_PER_USD, PRICES, _pico_per_token, cost_pico, cost_usd, format_pico, usd_to_pico,
)
from tokenmeter.state import BUDGET_CHECK_SECONDS, RuntimeState
from tokenmeter.store import Store

needs_tzset = pytest.mark.skipif(not hasattr(time, "tzset"), reason="the zone cannot be switched here")


@pytest.fixture
def zone(monkeypatch):
    """Switches the process time zone for one test and restores it afterwards.

    Where the zone cannot be switched (Windows has no `tzset`), a test that
    asks for this fixture is skipped instead of failing on the call.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("the zone cannot be switched here")
    def switch(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()
    yield switch
    monkeypatch.undo()
    if hasattr(time, "tzset"):
        time.tzset()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "b.db")
    yield s
    s.close()


def at(*parts: int) -> float:
    """Local wall-clock time in the zone currently set."""
    return datetime(*parts).timestamp()


_seq = iter(range(10 ** 9))


def call(store, ts, *, model="claude-opus-5", provider="anthropic", inp=0, out=0, cw=0, cr=0,
         cw1h=0, billing="subscription", complete=True):
    store.add_event(UsageEvent(
        ts=ts, provider=provider, model=model, input_tokens=inp, output_tokens=out,
        cache_write_tokens=cw, cache_read_tokens=cr, cache_write_1h_tokens=cw1h,
        source="proxy", dedupe_key=f"t:{next(_seq)}", billing=billing, usage_complete=complete))


def budget(**fields) -> Budget:
    base = {"name": "test", "metric": "tokens", "period": "day", "limit": 1000}
    return Budget.create(**{**base, **fields})


# -- definitions --------------------------------------------------------------


@pytest.mark.parametrize("fields", [
    {"limit": 0}, {"limit": -5}, {"limit": True}, {"limit": 1.5}, {"limit": float("nan")},
    {"limit": float("inf")}, {"limit": "100"},
    {"metric": "dollars"}, {"period": "session_5h"}, {"token_basis": "cache"},
    {"name": ""}, {"name": "   "}, {"name": "x" * 61},
    {"scope_kind": "provider"},                                   # provider missing
    {"scope_kind": "model", "scope_provider": "anthropic"},       # model missing
    {"scope_kind": "all", "scope_provider": "anthropic"},         # extra field
    {"scope_kind": "provider", "scope_provider": "a", "scope_model": "m"},  # extra field
    {"thresholds": (60, 60)}, {"thresholds": (85, 60)}, {"thresholds": (0,)},
    {"thresholds": (101,)}, {"thresholds": (True,)}, {"thresholds": [60, 85]},
])
def test_an_invalid_definition_is_rejected(fields):
    """A limit that is not a positive whole number, or a scope that does not say
    what it selects, would be enforced as something the user never set."""
    with pytest.raises(ValueError):
        budget(**fields)


def test_no_thresholds_is_a_valid_budget_that_never_notifies(store, zone):
    zone("UTC")
    quiet = budget(thresholds=())
    call(store, at(2026, 3, 10, 12), inp=5000)
    status = evaluate(store, quiet, at(2026, 3, 10, 13))
    assert status.state == budgets.STATE_EXCEEDED, "the state does not depend on the notices"
    assert due_alert(store, status) is None


def test_a_definition_survives_the_database(store):
    original = budget(name="OpenRouter month", metric="money", period="month",
                      limit=usd_to_pico("25"), scope_kind="model", scope_provider="openrouter",
                      scope_model="anthropic/claude-opus-5", thresholds=(50, 90))
    save_budget(store, original)
    assert load_budgets(store) == ([original], 0)


def test_an_unreadable_stored_row_is_skipped_and_counted(store):
    """A damaged row is not repaired by guessing: the others still load."""
    good = budget(name="good")
    save_budget(store, good)
    for i, broken in enumerate([{"limit_value": -1}, {"metric": "nope"}, {"thresholds": "{"},
                                {"thresholds": '"60"'}]):
        store.save_budget_row({**budget(name=f"bad{i}").to_row(), **broken})
    assert load_budgets(store) == ([good], 4)


def test_saving_again_replaces_the_definition_and_keeps_its_place(store, monkeypatch):
    """Also when both were created in the same instant, as happens with a
    coarse clock: the list keeps the order they were added in."""
    monkeypatch.setattr("tokenmeter.store.time.time", lambda: 1_700_000_000.0)
    first, second = budget(name="first"), budget(name="second")
    save_budget(store, first)
    save_budget(store, second)
    save_budget(store, Budget(**{**first.__dict__, "limit": 5}))
    loaded, _ = load_budgets(store)
    assert [(b.name, b.limit) for b in loaded] == [("first", 5), ("second", 1000)]


# -- periods ------------------------------------------------------------------


@needs_tzset
@pytest.mark.parametrize("tz", ["America/Lima", "UTC", "Asia/Tokyo"])
def test_a_day_runs_from_local_midnight_to_the_next(zone, tz):
    zone(tz)
    assert period_bounds("day", at(2026, 11, 12, 15, 30)) == (at(2026, 11, 12), at(2026, 11, 13))
    assert period_bounds("day", at(2026, 12, 31, 23, 59, 59)) == (at(2026, 12, 31), at(2027, 1, 1))


@needs_tzset
@pytest.mark.parametrize("tz", ["America/Lima", "UTC", "Asia/Tokyo"])
def test_a_week_runs_from_monday_to_monday(zone, tz):
    """A calendar week, not the last seven days. 2026-11-12 is a Thursday."""
    zone(tz)
    assert period_bounds("week", at(2026, 11, 12, 15)) == (at(2026, 11, 9), at(2026, 11, 16))
    assert period_bounds("week", at(2026, 11, 9)) == (at(2026, 11, 9), at(2026, 11, 16))
    assert period_bounds("week", at(2026, 11, 15, 23, 59)) == (at(2026, 11, 9), at(2026, 11, 16))
    # A week that crosses the year.
    assert period_bounds("week", at(2027, 1, 1, 8)) == (at(2026, 12, 28), at(2027, 1, 4))


@needs_tzset
@pytest.mark.parametrize("tz", ["America/Lima", "UTC", "Asia/Tokyo"])
def test_a_month_is_the_calendar_month_not_thirty_days(zone, tz):
    zone(tz)
    assert period_bounds("month", at(2026, 11, 12)) == (at(2026, 11, 1), at(2026, 12, 1))
    assert period_bounds("month", at(2026, 12, 31, 23)) == (at(2026, 12, 1), at(2027, 1, 1))
    assert period_bounds("month", at(2028, 2, 29, 12)) == (at(2028, 2, 1), at(2028, 3, 1)), "leap year"
    assert period_bounds("month", at(2027, 2, 28, 12)) == (at(2027, 2, 1), at(2027, 3, 1))


@needs_tzset
def test_a_day_with_a_clock_change_is_not_assumed_to_last_24_hours(zone):
    """New York, 2026-03-08: clocks jump from 02:00 to 03:00, the day has 23 hours.
    On 2026-11-01 they fall back and the day has 25."""
    zone("America/New_York")
    start, end = period_bounds("day", at(2026, 3, 8, 12))
    assert (start, end) == (at(2026, 3, 8), at(2026, 3, 9)) and end - start == 23 * 3600
    start, end = period_bounds("day", at(2026, 11, 1, 12))
    assert (start, end) == (at(2026, 11, 1), at(2026, 11, 2)) and end - start == 25 * 3600


def test_an_unknown_period_is_an_error_not_a_default():
    with pytest.raises(ValueError):
        period_bounds("fortnight", 0.0)


# -- what is counted ----------------------------------------------------------


@needs_tzset
def test_a_call_exactly_at_the_boundary_belongs_to_the_new_period_only(store, zone):
    """[start, end): midnight itself is the first instant of the new day."""
    zone("America/Lima")
    call(store, at(2026, 11, 12, 23, 59, 59), inp=1)
    call(store, at(2026, 11, 13), inp=10)
    daily = budget()
    assert evaluate(store, daily, at(2026, 11, 12, 23, 59, 59)).known == 1
    assert evaluate(store, daily, at(2026, 11, 13, 0, 0, 1)).known == 10


@needs_tzset
def test_a_call_stamped_after_the_evaluation_has_not_happened_yet(store, zone):
    zone("UTC")
    call(store, at(2026, 11, 12, 10), inp=1)
    call(store, at(2026, 11, 12, 18), inp=100)
    assert evaluate(store, budget(), at(2026, 11, 12, 12)).known == 1
    assert evaluate(store, budget(), at(2026, 11, 12, 18)).known == 101


@needs_tzset
def test_each_metric_adds_up_its_own_tokens(store, zone):
    """Cache is separate from input, and the 1-hour write is part of the cache
    write, not one more term."""
    zone("UTC")
    call(store, at(2026, 11, 12, 9), inp=100, out=20, cw=1000, cr=5000, cw1h=600)
    now = at(2026, 11, 12, 10)
    assert evaluate(store, budget(metric="tokens", token_basis="io"), now).known == 120
    assert evaluate(store, budget(metric="tokens", token_basis="all"), now).known == 6120
    assert evaluate(store, budget(metric="input"), now).known == 100
    assert evaluate(store, budget(metric="output"), now).known == 20


@needs_tzset
def test_the_scope_selects_by_exact_provider_and_model(store, zone):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, provider="anthropic", model="claude-opus-5", inp=1)
    call(store, now - 60, provider="anthropic", model="claude-opus-5-5", inp=10)
    call(store, now - 60, provider="openrouter", model="claude-opus-5", inp=100)
    assert evaluate(store, budget(), now).known == 111
    by_provider = budget(scope_kind="provider", scope_provider="anthropic")
    assert evaluate(store, by_provider, now).known == 11
    by_model = budget(scope_kind="model", scope_provider="anthropic", scope_model="claude-opus-5")
    assert evaluate(store, by_model, now).known == 1, "a longer model name is another model"


@needs_tzset
def test_a_budget_counts_every_model_not_only_the_ones_a_list_would_show(store, zone):
    """The dashboard lists eight models; a budget must not add up that list."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    for i in range(12):
        call(store, now - 60, model=f"model-{i}", inp=10)
    status = evaluate(store, budget(), now)
    assert (status.known, status.calls) == (120, 12)


# -- money --------------------------------------------------------------------


def test_every_verified_rate_is_a_whole_number_of_picodollars():
    """This is what makes the sums exact. A new price that breaks it must be
    noticed here, not as a rounding drift in someone's month."""
    for model, price in PRICES.items():
        rates = {price.input_per_mtok, price.output_per_mtok}
        rates |= {r for r in (price.intro_input_per_mtok, price.intro_output_per_mtok) if r}
        for rate in rates:
            for multiplier in (1, 1.25, 2.0, price.cache_read_multiplier):
                assert _pico_per_token(rate, multiplier).denominator == 1, (model, rate, multiplier)


@pytest.mark.parametrize("model", sorted(PRICES))
def test_the_exact_cost_is_the_same_amount_as_the_float_cost(model):
    args = (model, 123_457, 7_919, 40_001, 900_007, 15_013)
    assert cost_pico(*args, on_date="2026-11-12") / PICO_PER_USD == pytest.approx(
        cost_usd(*args, on_date="2026-11-12"), rel=1e-12)


def test_a_model_without_a_price_has_no_cost_in_either_form():
    assert cost_pico("model-without-price", 10, 10) is None


@needs_tzset
def test_many_small_calls_add_up_exactly(store, zone):
    """One input token of Haiku 5.5 is a tenth of a micro-dollar. Three thousand
    of them are exactly $0.0003; floats would not land on it."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    for i in range(3000):
        call(store, now - 60 - i, model="claude-opus-5", inp=1)
    status = evaluate(store, budget(metric="money", limit=usd_to_pico("1")), now)
    assert status.known == 3000 * 5_000_000 == usd_to_pico("0.015")
    assert status.causes == {}


@needs_tzset
def test_a_local_model_costs_exactly_zero_and_never_borrows_a_price(store, zone):
    """A local model named like a paid one must not inherit its price, and a
    local model without a price does not make the amount a minimum."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, provider="ollama", model="claude-opus-5", inp=1_000_000, billing="local")
    call(store, now - 60, provider="ollama", model="llama3.2", inp=1_000_000, billing="local")
    status = evaluate(store, budget(metric="money", limit=usd_to_pico("1")), now)
    assert (status.known, status.causes, status.calls) == (0, {}, 2)


@needs_tzset
def test_a_missing_price_is_left_out_and_reported_never_counted_as_free(store, zone):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, model="claude-opus-5", inp=1_000_000)
    call(store, now - 50, model="model-without-price", inp=1_000_000)
    call(store, now - 40, model="model-without-price", inp=1)
    status = evaluate(store, budget(metric="money", limit=usd_to_pico("10")), now)
    assert status.known == usd_to_pico("5")
    assert status.causes == {budgets.CAUSE_UNPRICED: 2}
    assert status.percent_exact is None and status.percent_min == 50.0
    assert status.as_dict()["known_text"] == "≥ $5.00"


@needs_tzset
def test_a_tiered_price_makes_the_amount_a_minimum(store, zone):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, model="claude-haiku-5-5", inp=1_000_000)
    status = evaluate(store, budget(metric="money", limit=usd_to_pico("1")), now)
    assert status.known == usd_to_pico("0.10")
    assert status.causes == {budgets.CAUSE_TIERED_PRICE: 1}


@needs_tzset
def test_unknown_billing_does_not_make_the_api_equivalent_uncertain(store, zone):
    """The API equivalent is what the work costs at published rates, whoever paid."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, model="claude-opus-5", inp=1_000_000, billing="unknown")
    status = evaluate(store, budget(metric="money", limit=usd_to_pico("10")), now)
    assert (status.known, status.causes) == (usd_to_pico("5"), {})


@pytest.mark.parametrize("text, pico", [
    ("25", 25 * PICO_PER_USD), ("0.50", PICO_PER_USD // 2), ("$1,200.75", 1_200_750_000_000_000),
    (" 3 ", 3 * PICO_PER_USD), ("0", 0),
])
def test_a_typed_dollar_amount_is_read_exactly(text, pico):
    assert usd_to_pico(text) == pico


@pytest.mark.parametrize("text", ["", "abc", "-1", "nan", "inf", "1e400", "1000000001",
                                  "0.0000000000001", "1.2.3"])
def test_a_typed_amount_that_is_not_money_is_rejected(text):
    with pytest.raises(ValueError):
        usd_to_pico(text)


def test_money_is_shown_like_every_other_cost():
    assert format_pico(usd_to_pico("1234.5")) == "$1,234.50"
    assert format_pico(usd_to_pico("5"), lower_bound=True) == "≥ $5.00"
    assert format_pico(None) == "—"


# -- honesty ------------------------------------------------------------------


@needs_tzset
def test_a_cut_call_makes_the_tokens_a_minimum(store, zone):
    """A stream cut before its usage arrived recorded fewer tokens than were
    used. The total cannot claim to be complete."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, inp=400)
    call(store, now - 50, inp=0, complete=False)
    status = evaluate(store, budget(), now)
    assert status.causes == {budgets.CAUSE_MISSING_USAGE: 1}
    assert status.percent_exact is None
    assert status.summary() == "≥ 400 of 1,000 · at least 40%"
    assert status.state == budgets.STATE_BELOW, "40% is proven; room left is not"


@needs_tzset
def test_a_minimum_can_prove_the_limit_was_passed(store, zone):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, inp=1100)
    call(store, now - 50, inp=0, complete=False)
    status = evaluate(store, budget(), now)
    assert status.uncertain and status.state == budgets.STATE_EXCEEDED
    assert status.remaining_max == 0


@needs_tzset
def test_a_period_older_than_the_kept_history_is_reported(tmp_path, zone):
    """With three days of retention, a month has already lost calls."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    short = Store(tmp_path / "short.db", retain_days=3)
    try:
        short.add_event(UsageEvent(ts=now - 60, provider="anthropic", model="m", input_tokens=5,
                                   output_tokens=0, dedupe_key="k"))
        monthly = evaluate(short, budget(period="month"), now)
        assert monthly.causes == {budgets.CAUSE_HISTORY_TRUNCATED: 1}
        assert evaluate(short, budget(period="day"), now).causes == {}
    finally:
        short.close()


@needs_tzset
def test_history_discarded_by_a_fresh_start_is_reported_too(store, zone):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    store.set_coverage_floor(at(2026, 11, 12, 6))
    assert evaluate(store, budget(period="day"), now).causes == {budgets.CAUSE_HISTORY_TRUNCATED: 1}


@needs_tzset
def test_a_changed_system_zone_is_visible_instead_of_silent(store, zone):
    """Periods are cut with the system clock. If the zone is not the one the
    budget was created in, its days are not the days the user set it for."""
    zone("America/Lima")
    made_in_lima = budget()
    assert made_in_lima.timezone == "America/Lima"
    assert evaluate(store, made_in_lima, at(2026, 11, 12, 10)).causes == {}
    zone("Asia/Tokyo")
    assert evaluate(store, made_in_lima, at(2026, 11, 12, 10)).causes == {
        budgets.CAUSE_TIMEZONE_CHANGED: 1}


@needs_tzset
@pytest.mark.parametrize("used, state", [
    (0, "below"), (599, "below"), (600, "warning"), (849, "warning"),
    (850, "danger"), (999, "danger"), (1000, "exceeded"), (5000, "exceeded"),
])
def test_the_state_follows_the_thresholds(store, zone, used, state):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    if used:
        call(store, now - 60, inp=used)
    assert evaluate(store, budget(), now).state == state


@needs_tzset
@pytest.mark.parametrize("thresholds, used, state", [
    ((50,), 500, "warning"), ((50,), 990, "warning"),            # one mark: only a warning
    ((40, 70, 90), 700, "warning"), ((40, 70, 90), 900, "danger"),  # the last mark is danger
    ((), 999, "below"), ((), 1000, "exceeded"),                  # 100% exceeds without a notice
    ((100,), 999, "below"),
])
def test_custom_thresholds_decide_warning_and_danger(store, zone, thresholds, used, state):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, inp=used)
    assert evaluate(store, budget(thresholds=thresholds), now).state == state


@needs_tzset
def test_a_threshold_reached_exactly_is_reached(store, zone):
    """Whole-number comparison: 1/3 of a limit of 3 is exactly 33.33…%, and a
    33% mark is reached, while 34% is not."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, inp=1)
    status = evaluate(store, budget(limit=3, thresholds=(33, 34)), now)
    assert status.crossed == (33,)
    call(store, now - 50, inp=2)
    assert evaluate(store, budget(limit=3, thresholds=(33, 34, 100)), now).crossed == (33, 34, 100)


@needs_tzset
def test_the_plain_data_carries_amounts_as_text(store, zone):
    """A month of picodollars is larger than the integers JavaScript holds exactly."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, model="claude-opus-5", inp=1_000_000)
    data = evaluate(store, budget(name="m", metric="money", limit=usd_to_pico("9000")), now).as_dict()
    assert data["limit"] == "9000000000000000" and data["known"] == "5000000000000"
    assert (data["limit_text"], data["known_text"]) == ("$9,000.00", "$5.00")
    assert data["unit"] == "API equivalent" and data["scope"] == "all usage"
    assert data["period_start"] == at(2026, 11, 12) and data["period_end"] == at(2026, 11, 13)


# -- notifications ------------------------------------------------------------


@needs_tzset
def test_several_thresholds_crossed_at_once_give_one_notice_for_the_highest(store, zone):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    daily = budget(name="Daily cap")
    call(store, now - 60, inp=1100)
    alert = due_alert(store, evaluate(store, daily, now))
    assert alert.threshold == 100 and "Daily cap" in alert.title and "limit reached" in alert.title
    assert "1,100 of 1,000" in alert.message
    assert due_alert(store, evaluate(store, daily, now + 1)) is None


@needs_tzset
def test_each_threshold_is_announced_once_as_usage_grows(store, zone):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    daily = budget()
    seen = []
    for step, amount in enumerate([500, 100, 100, 200, 200]):  # 500, 600, 700, 900, 1100
        call(store, now + step, inp=amount)
        alert = due_alert(store, evaluate(store, daily, now + step))
        seen.append(alert.threshold if alert else None)
    assert seen == [None, 60, None, 85, 100]


@needs_tzset
def test_a_restart_does_not_repeat_a_notice(tmp_path, zone):
    """The memory is in the database, not in the process."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    daily = budget()
    first = Store(tmp_path / "r.db")
    save_budget(first, daily)
    first.add_event(UsageEvent(ts=now - 60, provider="anthropic", model="m", input_tokens=700,
                               output_tokens=0, dedupe_key="k"))
    assert due_alert(first, evaluate(first, daily, now)).threshold == 60
    first.close()
    second = Store(tmp_path / "r.db")
    try:
        (reloaded,), _ = load_budgets(second)
        assert due_alert(second, evaluate(second, reloaded, now + 5)) is None
    finally:
        second.close()


@needs_tzset
def test_raising_the_limit_and_crossing_again_does_not_repeat_the_notice(store, zone):
    """Editing the limit must not produce a burst: 60% was already announced
    for this period, whatever the limit is now."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    daily = budget()
    call(store, now - 60, inp=700)
    assert due_alert(store, evaluate(store, daily, now)).threshold == 60
    raised = Budget(**{**daily.__dict__, "limit": 2000})
    assert due_alert(store, evaluate(store, raised, now + 1)) is None       # 35%
    call(store, now + 2, inp=600)                                          # 65% of 2000
    assert due_alert(store, evaluate(store, raised, now + 3)) is None
    call(store, now + 4, inp=500)                                          # 90%
    assert due_alert(store, evaluate(store, raised, now + 5)).threshold == 85


@needs_tzset
def test_a_new_period_starts_with_clean_notices(store, zone):
    zone("UTC")
    daily = budget()
    call(store, at(2026, 11, 12, 9), inp=700)
    assert due_alert(store, evaluate(store, daily, at(2026, 11, 12, 10))).threshold == 60
    call(store, at(2026, 11, 13, 9), inp=700)
    assert due_alert(store, evaluate(store, daily, at(2026, 11, 13, 10))).threshold == 60


@needs_tzset
def test_a_disabled_budget_measures_but_does_not_notify_or_reserve(store, zone):
    """Switching it back on later must still be able to announce the threshold."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    off = budget(enabled=False)
    call(store, now - 60, inp=700)
    status = evaluate(store, off, now)
    assert status.known == 700 and due_alert(store, status) is None
    on = Budget(**{**off.__dict__, "enabled": True})
    assert due_alert(store, evaluate(store, on, now + 1)).threshold == 60


@needs_tzset
def test_a_notice_about_a_minimum_says_it_is_a_minimum(store, zone):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    call(store, now - 60, inp=700)
    call(store, now - 50, inp=0, complete=False)
    alert = due_alert(store, evaluate(store, budget(), now))
    assert "at least 60%" in alert.title
    assert "≥ 700 of 1,000" in alert.message and "minimum" in alert.message


@needs_tzset
def test_two_budgets_keep_separate_notices(store, zone):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    one, two = budget(name="one"), budget(name="two")
    call(store, now - 60, inp=700)
    assert due_alert(store, evaluate(store, one, now)).budget_id == one.id
    assert due_alert(store, evaluate(store, two, now)).budget_id == two.id


def test_two_callers_crossing_the_same_threshold_get_it_once_between_them(store):
    first = store.reserve_budget_thresholds("b", 100.0, [60, 85])
    second = store.reserve_budget_thresholds("b", 100.0, [60, 85, 100])
    assert (first, second) == ([60, 85], [100])


@needs_tzset
def test_deleting_a_budget_forgets_its_notices(store, zone):
    zone("UTC")
    now = at(2026, 11, 12, 10)
    daily = budget()
    save_budget(store, daily)
    call(store, now - 60, inp=700)
    assert due_alert(store, evaluate(store, daily, now)) is not None
    assert store.delete_budget(daily.id) is True
    assert load_budgets(store) == ([], 0)
    assert store.reserve_budget_thresholds(daily.id, evaluate(store, daily, now).period_start,
                                           [60]) == [60]
    assert store.delete_budget("no-such-budget") is False


# -- the shells' tick ---------------------------------------------------------


class _Shell(RuntimeState):
    def __init__(self, store):
        self.store = store


@needs_tzset
def test_the_tick_checks_budgets_at_its_own_pace_not_every_second(store, zone):
    """The shells tick every second; adding a month up that often is waste. A
    threshold crossed between checks is announced at the next one."""
    zone("UTC")
    now = at(2026, 11, 12, 10)
    save_budget(store, budget(name="Daily cap"))
    shell = _Shell(store)
    assert shell.due_budget_alerts(now) == []
    call(store, now, inp=700)
    assert shell.due_budget_alerts(now + 1) == [], "inside the pace: not evaluated"
    (alert,) = shell.due_budget_alerts(now + BUDGET_CHECK_SECONDS)
    assert alert.threshold == 60
    assert shell.due_budget_alerts(now + 2 * BUDGET_CHECK_SECONDS) == []


def test_the_system_zone_prefers_the_explicit_setting(monkeypatch):
    monkeypatch.setenv("TZ", ":Europe/Madrid")
    assert budgets.system_timezone() == "Europe/Madrid"
    assert os.environ["TZ"] == ":Europe/Madrid"
