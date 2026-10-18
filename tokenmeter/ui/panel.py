"""Popup panel with the session breakdown.

It lives entirely on Tk's main thread. No pystray callback can touch these
widgets directly: the app routes them through a queue drained by `after()`.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # `ctypes.wintypes.RECT` does not exist without this explicit import
import tkinter as tk
from datetime import datetime
from tkinter import font as tkfont

from ..dashboard import build_state
from .icon import DANGER, OK, WARN

BG = "#1f2023"
BG_ALT = "#26282c"
BORDER = "#3a3d42"
FG = "#f2f3f5"
FG_MUTED = "#9aa0a6"
ACCENT_IDLE = "#6b7075"

WIDTH = 340
MARGIN = 12
PADX = 16
CONTENT_WIDTH = WIDTH - 2 * PADX


def bar_width(canvas) -> int:
    """Usable width of a bar, even before Tk has laid it out.

    `winfo_width()` returns **1** on an unmapped widget, not 0, so a fallback
    `or WIDTH` never triggers: the bar was drawn half a pixel wide on the first
    refresh, exactly the one the user sees when opening the panel.
    """
    width = canvas.winfo_width()
    return width if width > 1 else CONTENT_WIDTH


def _hex(rgba: tuple[int, int, int, int]) -> str:
    return "#%02x%02x%02x" % rgba[:3]


def work_area() -> tuple[int, int, int, int]:
    """Desktop work area, without the taskbar.

    `winfo_screenheight` includes the taskbar, so anchoring the panel with that
    number puts it underneath it.
    """
    try:
        rect = ctypes.wintypes.RECT()
        # SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return (0, 0, 0, 0)


class Panel:
    def __init__(self, root: tk.Tk, app) -> None:
        self._root = root
        self._app = app
        self._win: tk.Toplevel | None = None
        self._after_id: str | None = None
        self._visible = False
        self._placed_height = 0
        self._vars: dict[str, tk.StringVar] = {}
        self._model_rows: list[tuple[tk.Label, ...]] = []

    # -- lifecycle -----------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._visible

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    def show(self) -> None:
        if self._win is None:
            self._build()
        # Fill in BEFORE measuring: the per-model rows and the warning are added
        # in `refresh()`, so computing the geometry first leaves the footer cut off.
        self.refresh()
        self._place()
        self._win.deiconify()
        self._win.lift()
        self._win.attributes("-topmost", True)
        self._win.focus_force()
        self._visible = True
        self._tick()

    def hide(self) -> None:
        self._visible = False
        if self._after_id is not None:
            self._root.after_cancel(self._after_id)
            self._after_id = None
        if self._win is not None:
            self._win.withdraw()

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        win = tk.Toplevel(self._root)
        win.withdraw()
        win.overrideredirect(True)
        win.configure(bg=BORDER)
        win.bind("<Escape>", lambda _e: self.hide())
        win.bind("<FocusOut>", lambda _e: self.hide())
        self._win = win

        outer = tk.Frame(win, bg=BG, padx=PADX, pady=14)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        ui = tkfont.Font(family="Segoe UI", size=9)
        ui_bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        big = tkfont.Font(family="Segoe UI", size=26, weight="bold")
        mono = tkfont.Font(family="Consolas", size=20, weight="bold")
        small = tkfont.Font(family="Segoe UI", size=8)

        def var(key: str, value: str = "") -> tk.StringVar:
            self._vars[key] = tk.StringVar(value=value)
            return self._vars[key]

        # --- Header: session percentage ---
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        tk.Label(head, textvariable=var("percent", "0%"), font=big, bg=BG, fg=FG).pack(side="left")
        sub = tk.Frame(head, bg=BG)
        sub.pack(side="left", padx=(10, 0), pady=(8, 0))
        tk.Label(sub, text="of the session", font=small, bg=BG, fg=FG_MUTED, anchor="w").pack(fill="x")
        tk.Label(sub, textvariable=var("budget"), font=small, bg=BG, fg=FG_MUTED, anchor="w").pack(fill="x")
        self._budget_origin = tk.Label(
            outer, textvariable=var("budget_origin"), font=small, bg=BG, fg=FG_MUTED,
            anchor="w", wraplength=WIDTH - 40, justify="left",
        )
        self._budget_origin.pack(fill="x", pady=(6, 0))

        # --- Progress bar (Canvas: ttk does not allow coloring by state) ---
        self._bar = tk.Canvas(outer, height=8, bg=BG_ALT, highlightthickness=0, bd=0)
        self._bar.pack(fill="x", pady=(10, 2))
        self._bar_fill = self._bar.create_rectangle(0, 0, 0, 8, fill=_hex(OK), width=0)

        # --- 5 h timer ---
        timer = tk.Frame(outer, bg=BG_ALT, padx=12, pady=10)
        timer.pack(fill="x", pady=(12, 0))
        tk.Label(timer, textvariable=var("window_label", "SESSION RESETS"),
                 font=small, bg=BG_ALT, fg=FG_MUTED, anchor="w").pack(fill="x")
        tk.Label(timer, textvariable=var("countdown", "--:--:--"),
                 font=mono, bg=BG_ALT, fg=FG, anchor="w").pack(fill="x")
        tk.Label(timer, textvariable=var("reset_at"),
                 font=small, bg=BG_ALT, fg=FG_MUTED, anchor="w").pack(fill="x")

        # --- Weekly window: the second limit claude.ai shows ---
        self._weekly_box = tk.Frame(outer, bg=BG)
        self._weekly_box.pack(fill="x", pady=(14, 0))
        header = tk.Frame(self._weekly_box, bg=BG)
        header.pack(fill="x")
        tk.Label(header, text="WEEK", font=small, bg=BG, fg=FG_MUTED,
                 anchor="w").pack(side="left")
        tk.Label(header, textvariable=var("weekly_percent", "—"), font=ui_bold, bg=BG,
                 fg=FG, anchor="e").pack(side="right")

        self._weekly_bar = tk.Canvas(self._weekly_box, height=6, bg=BG_ALT,
                                     highlightthickness=0, bd=0)
        self._weekly_bar.pack(fill="x", pady=(4, 2))
        self._weekly_fill = self._weekly_bar.create_rectangle(0, 0, 0, 6, fill=_hex(OK),
                                                              width=0)
        tk.Label(self._weekly_box, textvariable=var("weekly_detail"), font=small, bg=BG,
                 fg=FG_MUTED, anchor="w").pack(fill="x")
        self._weekly_origin = tk.Label(self._weekly_box, textvariable=var("weekly_origin"),
                                       font=small, bg=BG, fg=FG_MUTED, anchor="w",
                                       wraplength=WIDTH - 40, justify="left")
        self._weekly_origin.pack(fill="x")

        # --- Token breakdown ---
        # The reference is kept because the weekly box is re-anchored here when it
        # is hidden and comes back: a bare `pack()` would send it to the end of
        # the panel.
        self._tokens_header = tk.Label(outer, text="SESSION TOKENS", font=small,
                                       bg=BG, fg=FG_MUTED, anchor="w")
        self._tokens_header.pack(fill="x", pady=(14, 4))
        grid = tk.Frame(outer, bg=BG)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        for row, (key, label, bold) in enumerate(
            [
                ("input", "Input", True),
                ("output", "Output", True),
                ("io", "Input + Output", True),
                ("cache_write", "Cache write", False),
                ("cache_read", "Cache read", False),
            ]
        ):
            fg = FG if bold else FG_MUTED
            tk.Label(grid, text=label, font=ui_bold if bold else ui, bg=BG, fg=fg,
                     anchor="w").grid(row=row, column=0, sticky="w", pady=1)
            tk.Label(grid, textvariable=var(key, "0"), font=ui_bold if bold else ui,
                     bg=BG, fg=fg, anchor="e").grid(row=row, column=1, sticky="e", pady=1)

        tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", pady=(8, 0))
        foot = tk.Frame(outer, bg=BG)
        foot.pack(fill="x", pady=(6, 0))
        foot.columnconfigure(1, weight=1)
        tk.Label(foot, text="Calls", font=ui, bg=BG, fg=FG_MUTED,
                 anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(foot, textvariable=var("calls", "0"), font=ui, bg=BG, fg=FG_MUTED,
                 anchor="e").grid(row=0, column=1, sticky="e")
        # A bare "Estimated cost" suggests it is what is being spent. With a
        # subscription it is not: it is public API rates applied to the same
        # work. The label says so to avoid misleading.
        self._cost_box = tk.Frame(outer, bg=BG)
        self._cost_box.pack(fill="x")
        self._cost_box.columnconfigure(1, weight=1)
        tk.Label(self._cost_box, text="API equivalent", font=ui, bg=BG, fg=FG_MUTED,
                 anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(self._cost_box, textvariable=var("cost", "—"), font=ui, bg=BG, fg=FG_MUTED,
                 anchor="e").grid(row=0, column=1, sticky="e")
        tk.Label(self._cost_box, textvariable=var("cost_note"), font=small, bg=BG, fg=FG_MUTED,
                 anchor="w", wraplength=WIDTH - 40, justify="left").grid(row=1, column=0, columnspan=2, sticky="ew")

        # --- By model ---
        self._models_header = tk.Label(outer, text="BY MODEL", font=small, bg=BG,
                                       fg=FG_MUTED, anchor="w")
        self._models_header.pack(fill="x", pady=(14, 4))
        self._models = tk.Frame(outer, bg=BG)
        self._models.pack(fill="x")
        self._models.columnconfigure(0, weight=1)

        # --- Warnings and collector status ---
        self._warning = tk.Label(outer, textvariable=var("warning"), font=small, bg=BG,
                                 fg=_hex(WARN), anchor="w", wraplength=WIDTH - 40, justify="left")
        tk.Label(outer, textvariable=var("sources"), font=small, bg=BG, fg=FG_MUTED,
                 anchor="w").pack(fill="x", pady=(12, 0))

    def _place(self) -> None:
        assert self._win is not None
        self._win.update_idletasks()
        height = self._win.winfo_reqheight()
        self._placed_height = height
        left, top, right, bottom = work_area()
        if right and bottom:
            x, y = right - WIDTH - MARGIN, bottom - height - MARGIN
        else:  # no Windows API: the taskbar is estimated
            x = self._win.winfo_screenwidth() - WIDTH - MARGIN
            y = self._win.winfo_screenheight() - height - 48
        self._win.geometry(f"{WIDTH}x{height}+{max(0, x)}+{max(0, y)}")

    # -- refresh ----------------------------------------------------------

    def _tick(self) -> None:
        if not self._visible or self._win is None:
            return
        try:
            self.refresh()
        finally:
            self._after_id = self._root.after(1000, self._tick)

    def refresh(self) -> None:
        if self._win is None:
            return
        snap = self._app.snapshot()
        cfg = self._app.config
        state = build_state(self._app)
        v = self._vars

        v["percent"].set(f"{snap.percent:.0f}%" if snap.has_activity else "—")
        v["budget"].set(f"{snap.used_tokens:,} / {snap.budget_tokens:,} tokens")
        # Where the denominator comes from: a calibrated ceiling and an invented
        # one do not deserve the same trust, and the panel must not hide the
        # difference.
        origin, warn = self._app.calibration_health()
        v["budget_origin"].set(origin)
        self._budget_origin.configure(fg=_hex(WARN) if warn else FG_MUTED)

        color = _hex(
            DANGER if snap.percent >= cfg.danger_percent
            else WARN if snap.percent >= cfg.warn_percent
            else OK
        ) if snap.has_activity else ACCENT_IDLE
        width = bar_width(self._bar)
        fraction = max(0.0, min(1.0, snap.percent / 100.0)) if snap.has_activity else 0.0
        self._bar.coords(self._bar_fill, 0, 0, width * fraction, 8)
        self._bar.itemconfigure(self._bar_fill, fill=color)

        if snap.has_activity:
            v["window_label"].set(f"SESSION RESETS  ·  {cfg.session_window_hours:g} h window")
            v["countdown"].set(snap.countdown_text())
            v["reset_at"].set(f"at {snap.reset_at_text()}")
        else:
            v["window_label"].set("NO ACTIVE SESSION")
            v["countdown"].set("--:--:--")
            v["reset_at"].set(f"the {cfg.session_window_hours:g} h window starts with the next call")

        self._refresh_weekly(cfg)

        totals = state["tokens"]
        self._tokens_header.configure(text=state["general_window"].upper())
        for name in ("input", "output", "io", "cache_write", "cache_read", "calls"):
            v[name].set(f"{totals[name]:,}")
        self._render_models(state["models"])

        if cfg.show_cost:
            self._cost_box.pack(fill="x", before=self._models_header)
            v["cost"].set(state["cost"]["text"] if totals["calls"] else "—")
            note = "what this work would cost at API rates; a subscription does not pay this" if totals["calls"] else ""
            if state["cost"]["partial"]:
                note += " · some model has no price"
            v["cost_note"].set(note)
        else:
            self._cost_box.pack_forget()
            v["cost"].set("")
            v["cost_note"].set("")

        if state["warning"]:
            v["warning"].set("⚠ " + state["warning"])
            self._warning.pack(fill="x", pady=(10, 0))
        else:
            v["warning"].set("")
            self._warning.pack_forget()

        v["sources"].set(state["sources"])

        # The content changes height on the fly (a warning appears, the number of
        # models changes). Without re-measuring, those changes fall outside the
        # window and are never seen.
        if self._visible:
            self._win.update_idletasks()
            if self._win.winfo_reqheight() != self._placed_height:
                self._place()

    def _refresh_weekly(self, cfg) -> None:
        if not cfg.weekly_enabled:
            self._weekly_box.pack_forget()
            return
        self._weekly_box.pack(fill="x", pady=(14, 0), before=self._tokens_header)

        v = self._vars
        snap = self._app.weekly_snapshot()
        origin, warn = self._app.weekly_origin()

        v["weekly_origin"].set(origin)
        self._weekly_origin.configure(fg=_hex(WARN) if warn else FG_MUTED)

        if snap.configured:
            v["weekly_percent"].set(f"{snap.percent:.0f}%")
            v["weekly_detail"].set(
                f"{snap.used_tokens:,} / {snap.budget_tokens:,} · resets in "
                f"{snap.remaining_text()} ({snap.reset_at_text()})"
            )
            fraction = max(0.0, min(1.0, snap.percent / 100.0))
            color = _hex(
                DANGER if snap.percent >= cfg.danger_percent
                else WARN if snap.percent >= cfg.warn_percent
                else OK
            )
        else:
            v["weekly_percent"].set("—")
            v["weekly_detail"].set(f"{snap.used_tokens:,} tokens in the last 7 days")
            fraction, color = 0.0, ACCENT_IDLE

        width = bar_width(self._weekly_bar)
        self._weekly_bar.coords(self._weekly_fill, 0, 0, width * fraction, 6)
        self._weekly_bar.itemconfigure(self._weekly_fill, fill=color)

    def _render_models(self, rows) -> None:
        for widgets in self._model_rows:
            for w in widgets:
                w.destroy()
        self._model_rows.clear()

        ui = ("Segoe UI", 9)
        small = ("Segoe UI", 8)
        if not rows:
            label = tk.Label(self._models, text="no activity in this session", font=small,
                             bg=BG, fg=FG_MUTED, anchor="w")
            label.grid(row=0, column=0, columnspan=2, sticky="w")
            self._model_rows.append((label,))
            return

        for i, row in enumerate(rows[:6]):
            name = tk.Label(self._models, text=row["model"], font=ui, bg=BG, fg=FG, anchor="w")
            name.grid(row=i, column=0, sticky="w", pady=1)
            detail = tk.Label(
                self._models,
                text=f"{row['io']:,} tok · {row['calls']} calls",
                font=small, bg=BG, fg=FG_MUTED, anchor="e",
            )
            detail.grid(row=i, column=1, sticky="e", pady=1)
            self._model_rows.append((name, detail))
