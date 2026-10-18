"""History window: tokens per day over the stored events.

It only draws. What counts as a zero and what as "no record", the scale and
which bar is under the pointer are decided by `tokenmeter/history.py`, which is
tested without a display. Like the panel, it lives entirely on Tk's main thread.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from ..history import RANGES_DAYS, bar_at, bar_layout, compact, daily_series, local_midnight, summarize
from ..pricing import cost_usd, format_usd
from .icon import OK
from .panel import BG, BG_ALT, BORDER, FG, FG_MUTED, _hex

CHART_W = 560
CHART_H = 180
AXIS_H = 18
PAD = 16
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class HistoryWindow:
    def __init__(self, root: tk.Tk, app) -> None:
        self._root = root
        self._app = app
        self._win: tk.Toplevel | None = None
        self._days = tk.IntVar(value=30)
        self._bars = []
        self._hover = tk.StringVar()
        self._summary = tk.StringVar()
        self._note = tk.StringVar()
        self._model_rows: list[tk.Widget] = []

    def show(self) -> None:
        if self._win is None or not self._win.winfo_exists():
            self._build()
        self.refresh()
        self._win.deiconify()
        self._win.lift()
        self._win.focus_force()

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        win = tk.Toplevel(self._root)
        win.title("TokenMeter · History")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.bind("<Escape>", lambda _e: win.withdraw())
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self._win = win

        ui = tkfont.Font(family="Segoe UI", size=9)
        small = tkfont.Font(family="Segoe UI", size=8)
        self._small = small

        outer = tk.Frame(win, bg=BG, padx=PAD, pady=14)
        outer.pack(fill="both", expand=True)

        top = tk.Frame(outer, bg=BG)
        top.pack(fill="x")
        self._title = tk.Label(top, font=ui, bg=BG, fg=FG, anchor="w")
        self._title.pack(side="left")
        for days in reversed(RANGES_DAYS):
            tk.Radiobutton(
                top, text=f"{days} d", value=days, variable=self._days,
                command=self.refresh, indicatoron=False, font=small, bg=BG_ALT,
                fg=FG, selectcolor=BORDER, activebackground=BORDER,
                activeforeground=FG, bd=0, padx=8, pady=2,
            ).pack(side="right", padx=(4, 0))

        self._canvas = tk.Canvas(outer, width=CHART_W, height=CHART_H + AXIS_H, bg=BG,
                                 highlightthickness=0, bd=0)
        self._canvas.pack(pady=(10, 0))
        self._canvas.bind("<Motion>", self._on_motion)
        self._canvas.bind("<Leave>", lambda _e: self._hover.set(""))

        tk.Label(outer, textvariable=self._hover, font=small, bg=BG, fg=FG,
                 anchor="w").pack(fill="x", pady=(4, 0))
        tk.Label(outer, textvariable=self._summary, font=ui, bg=BG, fg=FG, anchor="w",
                 justify="left").pack(fill="x", pady=(8, 0))
        tk.Label(outer, textvariable=self._note, font=small, bg=BG, fg=FG_MUTED, anchor="w",
                 justify="left", wraplength=CHART_W).pack(fill="x", pady=(2, 0))

        tk.Label(outer, text="BY MODEL IN THE RANGE", font=small, bg=BG, fg=FG_MUTED,
                 anchor="w").pack(fill="x", pady=(12, 4))
        self._models = tk.Frame(outer, bg=BG)
        self._models.pack(fill="x")
        self._models.columnconfigure(0, weight=1)

    # -- refresh ----------------------------------------------------------

    def refresh(self) -> None:
        if self._win is None:
            return
        cfg = self._app.config
        days = self._days.get()
        from ..dashboard import history_sources

        self._bars = daily_series(self._app.store, days, cfg.percent_basis,
                                  expected_sources=history_sources(cfg, store=self._app.store))
        base = "input + output + cache" if cfg.percent_basis == "all" else "input + output"
        self._title.configure(text=f"Tokens per day ({base})")
        self._draw()

        s = summarize(self._bars)
        if s.known_days == 0:
            self._summary.set("No stored data in this range.")
        else:
            peak = (f" · peak {self._day_text(s.peak_day)} with {s.peak_tokens:,}"
                    if s.peak_day else "")
            self._summary.set(
                f"{s.total_tokens:,} tokens · {s.total_calls:,} calls · "
                f"{s.active_days} of {s.known_days} days with usage · "
                f"average {s.average_per_active_day:,}/active day{peak}"
            )
        notes = []
        partial_days = sum(bar.coverage == "partial" for bar in self._bars)
        if partial_days:
            notes.append(f"{partial_days} day(s) with partial coverage: some usage may be missing.")
        if s.unknown_days:
            notes.append(
                f"{s.unknown_days} day(s) with no record (in gray): TokenMeter was not "
                f"observing. The database keeps {cfg.retain_days} days."
            )
        if s.incomplete_calls:
            notes.append(f"⚠ {s.incomplete_calls} call(s) without usage: the real totals "
                         "are higher.")
        self._note.set("\n".join(notes))
        self._render_models(local_midnight(self._bars[0].day))

    def _draw(self) -> None:
        c = self._canvas
        c.delete("all")
        rects = bar_layout(self._bars, CHART_W, CHART_H)
        slot = CHART_W / len(self._bars)
        for i, (bar, rect) in enumerate(zip(self._bars, rects)):
            if rect is None:
                x0 = i * slot + 1
                c.create_rectangle(x0, CHART_H - 2, (i + 1) * slot - 1, CHART_H,
                                   fill=BORDER, width=0)
            else:
                c.create_rectangle(*rect, fill=_hex(OK), width=0)
        c.create_line(0, CHART_H, CHART_W, CHART_H, fill=BORDER)

        peak = max((b.tokens for b in self._bars if b.tokens), default=0)
        if peak:
            c.create_text(2, 2, text=compact(peak), anchor="nw", fill=FG_MUTED,
                          font=self._small)
        # Labels: first day, last and one in the middle. More would overlap at 90 days.
        n = len(self._bars)
        for i in sorted({0, n // 2, n - 1}):
            anchor = "nw" if i == 0 else "ne" if i == n - 1 else "n"
            x = 0 if i == 0 else CHART_W if i == n - 1 else (i + 0.5) * slot
            c.create_text(x, CHART_H + 3, text=self._bars[i].day.strftime("%d/%m"),
                          anchor=anchor, fill=FG_MUTED, font=self._small)

    def _on_motion(self, event) -> None:
        i = bar_at(event.x, len(self._bars), CHART_W)
        if i is None:
            self._hover.set("")
            return
        bar = self._bars[i]
        today = " (today, in progress)" if i == len(self._bars) - 1 else ""
        if not bar.known:
            self._hover.set(f"{self._day_text(bar.day)}{today} · no record")
        else:
            self._hover.set(f"{self._day_text(bar.day)}{today} · {bar.tokens:,} tokens · "
                            f"{bar.calls:,} calls")

    def _render_models(self, since_ts: float) -> None:
        for w in self._model_rows:
            w.destroy()
        self._model_rows.clear()
        small = ("Segoe UI", 8)
        rows = self._app.store.totals_by_model(since_ts)
        if not rows:
            w = tk.Label(self._models, text="no activity in the range", font=small, bg=BG,
                         fg=FG_MUTED, anchor="w")
            w.grid(row=0, column=0, sticky="w")
            self._model_rows.append(w)
            return
        for i, row in enumerate(rows[:8]):
            text = f"{row.io_tokens:,} tok · {row.calls:,} calls"
            if self._app.config.show_cost:
                cost = cost_usd(row.model, row.input_tokens, row.output_tokens,
                                row.cache_write_tokens, row.cache_read_tokens,
                                row.cache_write_1h_tokens)
                equivalent = "—" if cost is None else format_usd(cost)
                text += f" · API {equivalent}"
            name = tk.Label(self._models, text=row.model, font=("Segoe UI", 9), bg=BG,
                            fg=FG, anchor="w")
            name.grid(row=i, column=0, sticky="w", pady=1)
            detail = tk.Label(
                self._models,
                text=text,
                font=small, bg=BG, fg=FG_MUTED, anchor="e",
            )
            detail.grid(row=i, column=1, sticky="e", pady=1)
            self._model_rows.extend((name, detail))

    @staticmethod
    def _day_text(day) -> str:
        return f"{DAYS[day.weekday()]} {day.strftime('%d/%m')}"

