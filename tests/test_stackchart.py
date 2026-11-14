"""The stacked daily chart: its data and its geometry, without a display."""
from __future__ import annotations

from datetime import date, timedelta
import time

import pytest

from tests.test_dashboard import app_without_tk
from tokenmeter import dashboard
from tokenmeter.models import UsageEvent
from tokenmeter.stackchart import (
    OTHERS, PALETTE, Rect, build_series, build_stack, hit, layout, legend_markup,
)


def bars(*tokens, coverage="full"):
    start = date(2026, 11, 1)
    return [{"day": (start + timedelta(days=i)).isoformat(), "label": f"day {i}", "tokens": t,
             "coverage": coverage} for i, t in enumerate(tokens)]


def day(i):
    return (date(2026, 11, 1) + timedelta(days=i)).isoformat()


# -- data ---------------------------------------------------------------------


def test_the_five_largest_models_get_a_colour_and_the_rest_are_others():
    models = {day(0): {f"m{i}": 10 * (i + 1) for i in range(8)}}   # m7 is the largest
    assert build_series(models) == ["m7", "m6", "m5", "m4", "m3", OTHERS]
    stack = build_stack(bars(360), models)
    assert stack["days"][0]["segments"] == [80, 70, 60, 50, 40, 30 + 20 + 10]
    assert stack["days"][0]["total"] == 360 == stack["peak"]
    assert [s["color"] for s in stack["series"]] == list(PALETTE)


def test_five_models_or_fewer_have_no_others_group():
    stack = build_stack(bars(30), {day(0): {"a": 10, "b": 20}})
    assert [s["name"] for s in stack["series"]] == ["b", "a"]


def test_a_model_keeps_its_place_and_colour_on_every_day():
    """Ranked over the whole range, not per day: otherwise the same colour
    would mean a different model from one bar to the next."""
    models = {day(0): {"a": 100, "b": 1}, day(1): {"a": 1, "b": 50}, day(2): {"b": 30}}
    stack = build_stack(bars(101, 51, 30), models)
    assert [s["name"] for s in stack["series"]] == ["a", "b"]      # a: 101, b: 81
    assert [d["segments"] for d in stack["days"]] == [[100, 1], [1, 50], [0, 30]]


def test_equal_totals_are_ordered_by_name_not_by_chance():
    assert build_series({day(0): {"zeta": 5, "alpha": 5, "mid": 5}}) == ["alpha", "mid", "zeta"]


def test_a_day_without_a_record_has_no_segments_and_is_not_a_zero():
    stack = build_stack(bars(10, None, 0), {day(0): {"a": 10}})
    assert stack["days"][1]["segments"] is None and stack["days"][1]["total"] is None
    assert stack["days"][2]["segments"] == [0] and stack["days"][2]["total"] == 0


def test_an_empty_range_has_no_series():
    stack = build_stack(bars(None, None), {})
    assert stack["series"] == [] and stack["peak"] == 0 and legend_markup(stack) == ""


# -- geometry -----------------------------------------------------------------


def test_segments_fill_the_bar_from_the_top_down_without_gaps_or_overlap():
    stack = build_stack(bars(100, 50), {day(0): {"a": 60, "b": 40}, day(1): {"a": 50}})
    rects = layout(stack, width=200, height=100, gap=0)
    first = [r for r in rects if r.day == 0]
    assert [(r.series, r.y, r.height) for r in first] == [(0, 0.0, 60.0), (1, 60.0, 40.0)]
    (second,) = [r for r in rects if r.day == 1]
    assert (second.x, second.width, second.y, second.height) == (100.0, 100.0, 50.0, 50.0)
    assert all(r.y + r.height <= 100.0 + 1e-9 for r in rects)


def test_every_day_keeps_its_slot_so_a_missing_day_leaves_a_hole():
    stack = build_stack(bars(10, None, 10), {day(0): {"a": 10}, day(2): {"a": 10}})
    rects = layout(stack, width=300, height=50, gap=0)
    assert [(r.day, r.x, r.series) for r in rects] == [(0, 0.0, 0), (1, 100.0, -1), (2, 200.0, 0)]
    missing = rects[1]
    assert (missing.height, missing.y) == (2.0, 48.0), "a thin baseline mark, not a bar"


def test_a_tiny_day_is_still_visible_and_a_zero_day_draws_nothing():
    stack = build_stack(bars(1_000_000, 1, 0), {day(0): {"a": 1_000_000}, day(1): {"a": 1}})
    rects = layout(stack, width=300, height=100, gap=0)
    small = [r for r in rects if r.day == 1]
    assert len(small) == 1 and small[0].height == pytest.approx(1.0)
    assert [r for r in rects if r.day == 2] == []


@pytest.mark.parametrize("width, height", [(0, 100), (100, 0), (-5, 10)])
def test_no_room_no_rectangles(width, height):
    stack = build_stack(bars(10), {day(0): {"a": 10}})
    assert layout(stack, width, height) == []
    assert layout(build_stack([], {}), 100, 100) == []


def test_a_bar_is_never_thinner_than_a_pixel_however_many_days():
    stack = build_stack(bars(*([5] * 90)), {day(i): {"a": 5} for i in range(90)})
    assert all(r.width >= 1.0 for r in layout(stack, width=60, height=40))


# -- what the pointer reads ---------------------------------------------------


def test_the_tooltip_names_the_day_its_total_and_every_model():
    stack = build_stack(bars(100, None), {day(0): {"a": 60, "b": 40}})
    rects = layout(stack, 200, 100, gap=0)
    # Over the lower segment: that model first, the other after it.
    assert hit(stack, rects, 50, 80, 200) == "day 0 · 100 tokens\nb: 40\na: 60"
    assert hit(stack, rects, 50, 10, 200) == "day 0 · 100 tokens\na: 60\nb: 40"
    assert hit(stack, rects, 150, 50, 200) == "day 1 · no record (unknown usage, not zero)"
    assert hit(stack, rects, -1, 50, 200) is None and hit(stack, rects, 200, 50, 200) is None


def test_the_whole_column_answers_not_only_the_painted_part():
    """A short bar would otherwise be a target a few pixels tall."""
    stack = build_stack(bars(1000, 10), {day(0): {"a": 1000}, day(1): {"a": 10}})
    rects = layout(stack, 200, 100, gap=0)
    assert hit(stack, rects, 150, 5, 200) == "day 1 · 10 tokens\na: 10"


def test_partial_coverage_is_said_in_the_tooltip():
    stack = build_stack(bars(10, coverage="partial"), {day(0): {"a": 10}})
    assert hit(stack, layout(stack, 100, 50), 10, 40, 100).startswith("day 0 · 10 tokens · partial coverage")


def test_the_legend_names_every_series_and_escapes_model_names():
    stack = build_stack(bars(30), {day(0): {"x · <synthetic>": 10, "a & b": 20}})
    markup = legend_markup(stack)
    assert markup == ('<span foreground="#0072B2">■</span> a &amp; b   '
                      '<span foreground="#E69F00">■</span> x · &lt;synthetic&gt;')


# -- from the database --------------------------------------------------------


def test_the_segments_of_a_day_add_up_to_its_bar(tmp_path):
    """Same basis and same filters as the history bars, so the chart and the
    table under it never disagree."""
    app = app_without_tk(tmp_path)
    now = time.time()
    for n, (provider, model, tokens) in enumerate([
            ("anthropic", "claude-opus-5", 500), ("anthropic", "claude-opus-5", 250),
            ("openai", "gpt-6.1-sol", 100), ("anthropic", "claude-haiku-5-5", 7)]):
        app.store.add_event(UsageEvent(ts=now - 60 - n, provider=provider, model=model,
                                       input_tokens=tokens, output_tokens=0, cache_read_tokens=9000,
                                       source="proxy", dedupe_key=f"s{n}"))
    history = dashboard.build_history(app, 7, now)
    stack, today = history["stack"], history["stack"]["days"][-1]
    assert [s["name"] for s in stack["series"]] == [
        "anthropic · claude-opus-5", "openai · gpt-6.1-sol", "anthropic · claude-haiku-5-5"]
    assert today["segments"] == [750, 100, 7]
    assert today["total"] == history["bars"][-1]["tokens"] == 857, "cache is not in the io basis"
    assert len(stack["days"]) == 7 and stack["peak"] == 857

    filtered = dashboard.build_history(app, 7, now, provider="openai")["stack"]
    assert [s["name"] for s in filtered["series"]] == ["openai · gpt-6.1-sol"]
    assert filtered["days"][-1]["total"] == 100

    app.config.percent_basis = "all"
    with_cache = dashboard.build_history(app, 7, now)
    assert with_cache["stack"]["days"][-1]["total"] == with_cache["bars"][-1]["tokens"] == 857 + 4 * 9000


def test_rect_is_plain_data():
    assert Rect(1, 2, 3, 4, 0, 0) == Rect(1.0, 2.0, 3.0, 4.0, 0, 0)
