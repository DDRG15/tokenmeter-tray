"""Stacked daily chart: which model the tokens of each day went to.

Everything here is plain data and arithmetic, so it is tested without a
display. The window only paints the rectangles this module computes.
"""

from __future__ import annotations

from dataclasses import dataclass

TOP_MODELS = 5
OTHERS = "Others"
# Okabe–Ito: told apart with the common forms of colour blindness. The last one
# is the neutral grey of "Others". Colour is never the only cue: the legend and
# the tooltip name every model.
PALETTE = ("#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#8A8A8A")


def build_series(day_models: dict[str, dict[str, int]]) -> list[str]:
    """The models that get a colour of their own, largest first, then Others.

    Chosen over the whole range, so a model keeps its colour and its place in
    the stack on every day instead of changing with each day's ranking.
    """
    totals: dict[str, int] = {}
    for models in day_models.values():
        for name, tokens in models.items():
            totals[name] = totals.get(name, 0) + tokens
    # Ties break by name so the order does not depend on dictionary order.
    ranked = sorted((n for n, t in totals.items() if t > 0), key=lambda n: (-totals[n], n))
    series = ranked[:TOP_MODELS]
    if len(ranked) > TOP_MODELS:
        series.append(OTHERS)
    return series


def build_stack(bars: list[dict], day_models: dict[str, dict[str, int]]) -> dict:
    """Joins the daily series with the per-model split.

    `bars` are the history bars (`day`, `label`, `tokens`, `coverage`). A day
    whose tokens are None has no record: it gets no segments at all, which is
    different from a day with zero usage.
    """
    series = build_series({b["day"]: day_models.get(b["day"], {}) for b in bars})
    named = [s for s in series if s != OTHERS]
    days = []
    for bar in bars:
        if bar["tokens"] is None:
            days.append({"day": bar["day"], "label": bar["label"], "coverage": bar["coverage"],
                         "segments": None, "total": None})
            continue
        models = day_models.get(bar["day"], {})
        segments = [models.get(name, 0) for name in named]
        if OTHERS in series:
            segments.append(sum(t for n, t in models.items() if n not in named))
        days.append({"day": bar["day"], "label": bar["label"], "coverage": bar["coverage"],
                     "segments": segments, "total": sum(segments)})
    peak = max((d["total"] or 0 for d in days), default=0)
    return {"series": [{"name": name, "color": PALETTE[-1] if name == OTHERS else PALETTE[i]}
                       for i, name in enumerate(series)],
            "days": days, "peak": peak}


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float
    day: int       # index into stack["days"]
    series: int    # index into stack["series"]; -1 marks a day with no record


def layout(stack: dict, width: float, height: float, gap: float = 2.0) -> list[Rect]:
    """Rectangles to paint, in a `width` x `height` area with the origin at the top left.

    Every day gets the same slot, so a missing day leaves its place empty
    instead of pulling the others together. A day with usage is at least one
    pixel tall: a small day must not look like no day.
    """
    days = stack["days"]
    if not days or width <= 0 or height <= 0:
        return []
    slot = width / len(days)
    bar = max(1.0, slot - gap)
    peak = stack["peak"]
    rects: list[Rect] = []
    for index, day in enumerate(days):
        x = index * slot + (slot - bar) / 2
        if day["segments"] is None:
            # A thin mark on the baseline: "no record", told apart from zero.
            rects.append(Rect(x, height - 2.0, bar, 2.0, index, -1))
            continue
        if not peak or not day["total"]:
            continue
        full = max(1.0, height * day["total"] / peak)
        top = height - full
        for series, tokens in enumerate(day["segments"]):
            if tokens <= 0:
                continue
            part = full * tokens / day["total"]
            rects.append(Rect(x, top, bar, part, index, series))
            top += part
    return rects


def hit(stack: dict, rects: list[Rect], x: float, y: float, width: float) -> str | None:
    """What the pointer at (x, y) is over, in words; None outside the days.

    The whole column of a day answers, not only the painted part, so a short
    bar is as easy to read as a tall one.
    """
    days = stack["days"]
    if not days or width <= 0 or not 0 <= x < width:
        return None
    day = days[min(len(days) - 1, int(x / (width / len(days))))]
    if day["segments"] is None:
        return f"{day['label']} · no record (unknown usage, not zero)"
    over = next((r for r in rects if r.series >= 0 and stack["days"][r.day] is day
                 and r.y <= y <= r.y + r.height and r.x <= x <= r.x + r.width), None)
    head = f"{day['label']} · {day['total']:,} tokens"
    if day["coverage"] == "partial":
        head += " · partial coverage"
    parts = [f"{stack['series'][i]['name']}: {tokens:,}"
             for i, tokens in enumerate(day["segments"]) if tokens > 0]
    if over is not None:
        focus = f"{stack['series'][over.series]['name']}: {day['segments'][over.series]:,}"
        parts = [focus] + [p for p in parts if p != focus]
    return head + ("\n" + "\n".join(parts) if parts else "")


def legend_markup(stack: dict) -> str:
    """Pango markup: a coloured square and the name of each series."""
    def escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "   ".join(f'<span foreground="{s["color"]}">■</span> {escape(s["name"])}'
                      for s in stack["series"])
