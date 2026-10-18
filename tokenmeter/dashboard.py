"""App state as JSON for the web window.

This is the contract between Python and `ui/web/index.html`. Everything that
decides what is shown —color levels, provenance texts, what counts as "no
record"— is decided here, with the same logic as the Tk panel, so the two
interfaces cannot tell different truths. The page only draws.

It is called from the web server thread. It only reads: the `Store` already
serializes access with its own lock, and nothing here writes configuration.
"""

from __future__ import annotations

import time

from datetime import datetime

from . import __version__
from .billing import label as billing_label
from .models import BILLING_API, BILLING_SUBSCRIPTION
from .history import RANGES_DAYS, daily_series, local_midnight, summarize
from .pricing import cost_usd, format_usd, VERIFIED_ON
from .store import Totals

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_MODELS = 8

# Actions the page may request. All of them go through the Tk thread's queue:
# the ones that open dialogs open them there, and the rest touch config on a
# single thread.
ALLOWED_ACTIONS = frozenset({
    "calibrate", "weekly", "edit_budget", "reset_session", "toggle_basis",
    "toggle_alerts", "toggle_pause", "open_data_dir", "toggle_panel",
    "backup", "restore",
    "export_csv", "export_json",
})


def level(percent: float, warn: int, danger: int, active: bool = True) -> str:
    if not active:
        return "idle"
    if percent >= danger:
        return "danger"
    if percent >= warn:
        return "warn"
    return "ok"


def _model_cost(row) -> float | None:
    return cost_usd(row.model, row.input_tokens, row.output_tokens,
                    row.cache_write_tokens, row.cache_read_tokens,
                    row.cache_write_1h_tokens)


class _Sum:
    """Sum of costs that remembers whether a price was missing."""

    def __init__(self) -> None:
        self.value, self.partial, self.any = 0.0, False, False

    def add(self, c: float | None) -> None:
        self.any = True
        if c is None:
            self.partial = True
        else:
            self.value += c

    def as_dict(self) -> dict:
        if self.any and self.partial and self.value == 0:
            return {"value": None, "text": "—", "partial": True}
        text = format_usd(self.value) + (" (partial)" if self.partial else "")
        return {"value": self.value, "text": text, "partial": self.partial}


ZERO = {"value": 0.0, "text": format_usd(0.0), "partial": False}


def _models(rows) -> tuple[list[dict], dict, list[dict]]:
    """Rows per model, cost, and breakdown by billing type.

    The total is still the API equivalent of everything. Below it, what **was**
    paid via API is separated from what was done under a subscription, which is
    included in the plan: only the former is money spent.
    """
    total_io = sum(r.io_tokens for r in rows) or 1
    out, total = [], _Sum()
    api, sub = _Sum(), _Sum()
    unknown = False
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        c = _model_cost(row)
        total.add(c)
        if row.billing == BILLING_API:
            api.add(c)
        elif row.billing == BILLING_SUBSCRIPTION:
            sub.add(c)
        elif row.billing == "unknown":
            unknown = True
        out.append({
            "model": row.model,
            "provider": row.provider,
            "billing": row.billing,
            "plan": row.plan,
            "io": row.io_tokens,
            "calls": row.calls,
            "cost": c,
            "cost_text": format_usd(c),
            "share": row.io_tokens / total_io,
        })
        g = groups.setdefault((row.billing, row.plan), {
            "kind": row.billing, "plan": row.plan,
            "label": billing_label(row.billing, row.plan),
            "calls": 0, "io": 0, "_sum": _Sum(),
        })
        g["calls"] += row.calls
        g["io"] += row.io_tokens
        g["_sum"].add(c)

    billing = []
    for g in sorted(groups.values(), key=lambda g: -g["io"]):
        d = g.pop("_sum").as_dict()
        g["cost"], g["cost_text"] = d["value"], d["text"]
        billing.append(g)

    if not rows:
        none = {"value": None, "text": "—", "partial": False}
        cost = {"value": None, "text": "—", "partial": False, "note": "",
                "api_spend": none, "subscription_equivalent": none}
    else:
        note = "what this work would cost at API rates; a subscription does not pay this"
        if total.partial:
            note += " · some model has no price"
        t = total.as_dict()
        cost = {
            "value": t["value"],
            "text": t["text"],
            "partial": t["partial"],
            "note": note,
            # With no API calls, what was paid is a certain zero, not a "don't know".
            "api_spend": api.as_dict() if api.any else dict(ZERO),
            "subscription_equivalent": sub.as_dict() if sub.any else dict(ZERO),
        }
        if unknown:
            known = cost["api_spend"]
            known["partial"] = True
            if not api.any:
                known["value"], known["text"] = None, "—"
            else:
                known["text"] += " (partial)"
            cost["note"] += " · some calls have unknown billing"
    return out[:MAX_MODELS], cost, billing


CODEX_STALE_S = 6 * 3600


def _remaining(seconds: float) -> str:
    secs = max(0, int(seconds))
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _age(seconds: float) -> str:
    secs = max(0, int(seconds))
    if secs < 90:
        return f"{secs} s ago"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs / 3600:.1f} h ago"
    return f"{secs // 86400} days ago"


def _codex_window(raw, now: float, warn: int, danger: int, weekly: bool) -> dict | None:
    if not isinstance(raw, dict):
        return None
    resets = raw.get("resets_at")
    pct = raw.get("used_percent")
    if not isinstance(resets, (int, float)) or not isinstance(pct, (int, float)):
        return None
    when = datetime.fromtimestamp(resets)
    if weekly:
        resets_text = f"{DAYS[when.weekday()]} {when.strftime('%d/%m %H:%M')}"
    else:
        resets_text = when.strftime("%H:%M")
    if resets <= now:
        # The window already reset and there is no new data: the old percentage
        # describes a window that ended. It is not shown.
        return {"used_percent": None, "window_minutes": raw.get("window_minutes"),
                "resets_at": resets, "resets_text": resets_text,
                "remaining_text": "reset; no new data", "level": "idle"}
    return {
        "used_percent": float(pct),
        "window_minutes": raw.get("window_minutes"),
        "resets_at": resets,
        "resets_text": resets_text,
        "remaining_text": _remaining(resets - now),
        "level": level(float(pct), warn, danger),
    }


def build_codex(store, cfg, now: float) -> dict | None:
    """Real Codex limits: OpenAI reports them in each session, no calibration needed."""
    try:
        from .collectors.codex import load_rate_limits
    except ImportError:
        return None
    try:
        rl = load_rate_limits(store)
    except Exception:
        return None
    if not rl:
        return None
    captured = rl.get("captured_ts") or 0.0
    return {
        "plan": rl.get("plan_type") or "",
        "captured_ts": captured,
        "age_text": _age(now - captured),
        "stale": now - captured > CODEX_STALE_S,
        "session": _codex_window(rl.get("primary"), now, cfg.warn_percent,
                                 cfg.danger_percent, weekly=False),
        "week": _codex_window(rl.get("secondary"), now, cfg.warn_percent,
                              cfg.danger_percent, weekly=True),
    }


def build_state(app, now: float | None = None, provider: str = "", source: str = "") -> dict:
    now = time.time() if now is None else now
    cfg = app.config
    snap = app.snapshot()
    origin, origin_warn = app.calibration_health()

    if snap.has_activity:
        window_label = f"Session resets · {cfg.session_window_hours:g} h window"
        reset_text = f"at {snap.reset_at_text()}"
    else:
        window_label = "No active session"
        reset_text = f"the {cfg.session_window_hours:g} h window starts with the next call"

    session = {
        "has_activity": snap.has_activity,
        "percent": snap.percent if snap.has_activity else None,
        "used": snap.used_tokens,
        "budget": snap.budget_tokens,
        "basis": snap.percent_basis,
        "scope": cfg.percent_scope,
        "window_hours": cfg.session_window_hours,
        "window_seconds": cfg.window_seconds,
        "remaining_seconds": snap.remaining_seconds if snap.has_activity else None,
        "countdown": snap.countdown_text() if snap.has_activity else "--:--:--",
        "window_label": window_label,
        "reset_text": reset_text,
        "origin": {"text": origin, "warn": origin_warn},
        "level": level(snap.percent, cfg.warn_percent, cfg.danger_percent, snap.has_activity),
        "estimated_unseen": snap.estimated_unseen_tokens,
    }

    weekly: dict = {"enabled": cfg.weekly_enabled}
    if cfg.weekly_enabled:
        w = app.weekly_snapshot()
        w_origin, w_warn = app.weekly_origin()
        weekly.update({
            "configured": w.configured,
            "percent": w.percent if w.configured else None,
            "used": w.used_tokens,
            "budget": w.budget_tokens,
            "remaining_text": w.remaining_text() if w.configured else None,
            "reset_at_text": w.reset_at_text() if w.configured else None,
            "detail": (
                f"{w.used_tokens:,} / {w.budget_tokens:,} · resets in "
                f"{w.remaining_text()} ({w.reset_at_text()})"
                if w.configured else f"{w.used_tokens:,} tokens in the last 7 days"
            ),
            "origin": {"text": w_origin, "warn": w_warn},
            "level": level(w.percent, cfg.warn_percent, cfg.danger_percent, w.configured),
        })

    rows = app.store.totals_by_model(app.session.general_start(now), provider, source)
    t = Totals(**{name: sum(getattr(r, name) for r in rows) for name in Totals.__dataclass_fields__})
    models, cost, billing = _models(rows)

    warning = ""
    if t.incomplete_calls:
        warning = (f"{t.incomplete_calls} call(s) without usage data: the real tokens "
                   "are higher than shown.")

    age = app.session.seconds_since_last_call(now)
    return {
        "version": __version__,
        "filters": {"provider": provider, "source": source, "options": app.store.filter_options()},
        "general_window": f"Last {cfg.session_window_hours:g} h · observed usage",
        "pricing_verified_on": VERIFIED_ON,
        "show_cost": cfg.show_cost,
        "collectors": build_health(app, now),
        "now_ts": now,
        "session": session,
        "weekly": weekly,
        "tokens": {
            "input": t.input_tokens,
            "output": t.output_tokens,
            "io": t.io_tokens,
            "cache_write": t.cache_write_tokens,
            "cache_read": t.cache_read_tokens,
            "calls": t.calls,
            "incomplete": t.incomplete_calls,
        },
        "cost": cost,
        "models": models,
        "billing": billing,
        "codex": build_codex(app.store, cfg, now),
        "warning": warning,
        "sources": app.sources_text(),
        # "Live" = there was a call less than 90 s ago: the same cutoff
        # `sync_text` uses to switch from seconds to minutes.
        "live": age is not None and age < 90,
        "flags": {
            "basis_all": cfg.percent_basis == "all",
            "alerts": cfg.alerts_enabled,
            "paused": app.collectors_paused,
        },
        "thresholds": {"warn": cfg.warn_percent, "danger": cfg.danger_percent},
    }


def build_health(app, now: float) -> list[dict]:
    health = []
    for attr, title in (("transcripts", "Claude Code"), ("codex", "Codex"), ("proxy", "Proxy")):
        collector = getattr(app, attr, None)
        if collector is None:
            health.append({"name": title, "state": "disabled", "error": False})
            continue
        stats = collector.stats
        failed = bool(stats.last_error or getattr(collector, "bind_error", ""))
        state = ("error" if failed else "paused" if collector.paused else
                 "active" if collector.running else "stopped")
        seen = getattr(stats, "last_scan_ts", None)
        health.append({"name": title, "state": state, "error": failed,
                       "last_scan_age": max(0, now-seen) if seen else None,
                       "files": getattr(stats, "files_seen", None),
                       "missing_usage": getattr(stats, "missing_usage", 0)})
    return health


def history_sources(cfg, provider: str = "", store=None) -> tuple[str, ...]:
    """Collectors that must have observed a day before it can claim "0 tokens".

    Only a collector that could be seeing usage is required. A proxy that never
    received traffic has nothing behind it, and a Codex without a folder does not
    exist: requiring them would make any zero impossible, even if the app ran the
    whole day.
    """
    proxy_used = False
    if cfg.proxy_enabled and store is not None:
        from .collectors.proxy import last_request_ts

        try:
            proxy_used = last_request_ts(store) is not None
        except Exception:
            proxy_used = True  # if it cannot be known, it is required: better "no record" than a false 0
    try:
        codex_present = cfg.codex_enabled and cfg.codex_path().exists()
    except Exception:
        codex_present = cfg.codex_enabled
    return tuple(name for name, enabled in (
        ("transcript", cfg.transcripts_enabled and provider in ("", "anthropic")),
        ("codex", codex_present and provider in ("", "openai")),
        ("proxy", proxy_used)) if enabled)


def build_history(app, days: int, now: float | None = None, provider: str = "", source: str = "") -> dict:
    """Daily series of the range. `tokens: null` means "no record", not zero."""
    if days not in RANGES_DAYS:
        days = 30
    cfg = app.config
    bars = daily_series(app.store, days, cfg.percent_basis, now=now, provider=provider,
                        source=source, expected_sources=history_sources(cfg, provider, app.store))
    s = summarize(bars)
    last = len(bars) - 1
    rows = app.store.totals_by_model(local_midnight(bars[0].day), provider, source)
    models, cost, billing = _models(rows)
    return {
        "days": days,
        "filters": {"provider": provider, "source": source, "options": app.store.filter_options()},
        "show_cost": cfg.show_cost,
        "ranges": list(RANGES_DAYS),
        "basis": cfg.percent_basis,
        "basis_text": "input + output + cache" if cfg.percent_basis == "all" else "input + output",
        "retain_days": cfg.retain_days,
        "bars": [
            {
                "day": b.day.isoformat(),
                "label": f"{DAYS[b.day.weekday()]} {b.day.strftime('%d/%m')}",
                "short": b.day.strftime("%d/%m"),
                "tokens": b.tokens,
                "calls": b.calls,
                "today": i == last,
                "coverage": b.coverage,
            }
            for i, b in enumerate(bars)
        ],
        "summary": {
            "total": s.total_tokens,
            "calls": s.total_calls,
            "active_days": s.active_days,
            "known_days": s.known_days,
            "unknown_days": s.unknown_days,
            "average": s.average_per_active_day,
            "peak": s.peak_tokens,
            "peak_label": (f"{DAYS[s.peak_day.weekday()]} {s.peak_day.strftime('%d/%m')}"
                           if s.peak_day else None),
            "incomplete": s.incomplete_calls,
            "partial_days": sum(b.coverage == "partial" for b in bars),
        },
        "models": models,
        "billing": billing,
        "cost": cost,
    }
