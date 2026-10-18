"""Application wiring and thread discipline.

Layout:

* main thread      → Tk (hidden). The only one allowed to touch widgets.
* `tray` thread    → pystray. Its callbacks only enqueue.
* `proxy` thread   → aiohttp server with its own asyncio loop.
* `transcripts` thread → .jsonl tailer.

Calling a widget from the pystray thread hangs the app intermittently and is
very hard to diagnose. That is why the queue exists.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import time
import tkinter as tk
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from tkinter import messagebox, simpledialog, filedialog

from .alerts import AlertTracker
from .anchors import AnchorError, parse_reset
from .calibration import CalibrationError, calibrate, fit_session, measure_drift
from .collectors.codex import CodexCollector
from .collectors.proxy import ProxyCollector, last_request_ts, proxy_footer
from .collectors.transcripts import TranscriptCollector
from .config import Config, data_dir
from .session import SessionTracker
from .store import Store
from .ui.history import HistoryWindow
from .ui.panel import Panel
from .ui.tray import Tray
from .ui.webserver import DashboardServer
from .ui.window import WindowLauncher
from .weekly import WeeklyTracker

log = logging.getLogger(__name__)

TICK_MS = 1000

# An old calibration is not an error, but the plan's quota may have changed and
# nothing would say so. From here on the panel suggests recalibrating.
STALE_CALIBRATION_DAYS = 14


@contextmanager
def dialog_parent(root):
    """Dialogs of the hidden Tk must stay above the web panel.

    The other process's foreground permission is not enough if Windows keeps
    the focus on another application. Windows owned by a topmost owner inherit
    that position; it is limited to the modal action and restored on exit.
    """
    previous = root.attributes("-topmost")
    root.attributes("-topmost", True)
    root.update_idletasks()
    try:
        yield
    finally:
        root.attributes("-topmost", previous)


class App:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.load()
        self.store = Store(data_dir() / "usage.db", retain_days=self.config.retain_days)
        self.session = SessionTracker(self.store, self.config)
        self.alerts = AlertTracker(self.store, tuple(self.config.alert_thresholds))
        self.weekly = WeeklyTracker(self.store, self.config)

        self._actions: queue.Queue[str | dict] = queue.Queue()
        self._running = True
        self._tick_id: str | None = None
        self._last_prune = 0.0

        self.transcripts: TranscriptCollector | None = None
        self.proxy: ProxyCollector | None = None
        self.codex: CodexCollector | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("TokenMeter")
        self.panel = Panel(self.root, self)
        self.history = HistoryWindow(self.root, self)
        self.tray = Tray(self)
        self.dashboard: DashboardServer | None = None
        self.window = WindowLauncher(data_dir())

    # -- API consumed by the UI --------------------------------------------

    def snapshot(self):
        return self.session.snapshot()

    def by_model(self):
        return self.session.by_model()

    def weekly_snapshot(self):
        return self.weekly.snapshot()

    def weekly_origin(self) -> tuple[str, bool]:
        """Provenance of the weekly limit for the panel: (text, is_warning)."""
        cfg = self.config
        snap = self.weekly.snapshot()
        if not snap.configured:
            return ("not configured · menu → Weekly window", True)
        if not cfg.weekly_is_calibrated:
            return ("cycle set, limit not calibrated", True)
        if snap.percent > 100:
            return ("above 100%: the weekly limit falls short", True)
        when = datetime.fromtimestamp(cfg.weekly_calibrated_at).strftime("%d/%m %H:%M")
        return (
            f"calibrated with {cfg.weekly_calibrated_from_percent:g}% of "
            f"\"All models\" · {when}",
            False,
        )

    def enqueue(self, action: str | dict) -> None:
        """The only bridge allowed between the pystray thread and the Tk one."""
        self._actions.put(action)

    @property
    def collectors_paused(self) -> bool:
        return any(c.paused for c in self._collectors())

    def sources_text(self) -> str:
        """Collector status, counted over the SESSION.

        It used to show the events inserted since the process started. When the
        app was opened mid-session almost everything was already in the
        database, so it said "5 calls" next to a panel showing 93: it looked
        like nothing had synced when it actually had.
        """
        per_source = self.session.calls_by_source()
        parts = []

        if self.transcripts is not None:
            if self.transcripts.paused:
                parts.append("transcripts paused")
            else:
                parts.append(f"transcripts {per_source.get('transcript', 0)}")

        if self.codex is not None and self.codex.stats.files_seen:
            # Only if there are Codex sessions: without Codex installed it adds nothing.
            if self.codex.paused:
                parts.append("codex paused")
            else:
                parts.append(f"codex {per_source.get('codex', 0)}")

        if self.proxy is not None:
            if self.proxy.bind_error:
                parts.append(f"proxy port {self.proxy.port} BUSY")
            elif self.proxy.paused:
                parts.append("proxy paused")
            else:
                parts.append(proxy_footer(
                    self.proxy.port,
                    per_source.get("proxy", 0),
                    last_request_ts(self.store),
                    time.time(),
                ))

        if not parts:
            return "no active collectors"
        return f"{'  ·  '.join(parts)}  ·  {self.sync_text()}"

    def sync_text(self) -> str:
        """Visible proof that the counter is alive and up to date."""
        for collector in self._collectors():
            if collector.stats.last_error:
                return f"{collector.name}: error; check the diagnostics"
            if not collector.running and not collector.paused:
                return f"{collector.name}: stopped"

        age = self.session.seconds_since_last_call()
        if age is None:
            return "no data yet"
        if age < 90:
            return f"last call {int(age)}s ago"
        if age < 5400:
            return f"last call {int(age // 60)} min ago"
        return f"last call {age / 3600:.1f} h ago"

    # -- startup ----------------------------------------------------------

    def _collectors(self):
        return [c for c in (self.transcripts, self.proxy, self.codex) if c is not None]

    def start(self) -> None:
        self.store.prune(self.config.retain_days)
        self._last_prune = time.time()
        if self.config.transcripts_enabled:
            self.transcripts = TranscriptCollector(
                self.store,
                self.config.transcripts_path(),
                self.config.transcripts_poll_seconds,
                overrides=self.config.billing_overrides,
            )
            self.transcripts.start()

        if self.config.codex_enabled:
            self.codex = CodexCollector(
                self.store, self.config.codex_path(), self.config.codex_poll_seconds)
            self.codex.start()

        if self.config.proxy_enabled:
            self.proxy = ProxyCollector(
                self.store,
                self.config.proxy_host,
                self.config.proxy_port,
                self.config.upstreams,
                inject_stream_usage=self.config.inject_stream_usage,
                billing_overrides=self.config.billing_overrides,
            )
            self.proxy.start()

        try:
            self.store.prune(self.config.retain_days)
        except Exception:
            log.exception("failed to prune the history")

        # The web window is the main interface; if its server does not start,
        # the Tk panel is still there and the menu falls back to it.
        self.dashboard = DashboardServer(self)
        self.dashboard.start()

        self.tray.start()
        self._tick_id = self.root.after(TICK_MS, self._tick)

    def run(self) -> None:
        self.start()
        try:
            self.root.mainloop()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if not self._running:
            return
        self._running = False

        # Cancel the pending tick BEFORE closing the database. Otherwise the
        # `after` already queued runs anyway, queries a closed SQLite and leaves
        # a traceback in the log on every clean shutdown.
        if self._tick_id is not None:
            try:
                self.root.after_cancel(self._tick_id)
            except Exception:
                log.debug("could not cancel the tick", exc_info=True)
            self._tick_id = None
        self.panel.hide()
        self.window.close()
        if self.dashboard is not None:
            self.dashboard.stop()

        for collector in self._collectors():
            collector.stop()
        self.tray.stop()
        try:
            self.store.close()
        except Exception:
            log.debug("failed to close the store", exc_info=True)

    # -- main thread loop -----------------------------------------

    def _tick(self) -> None:
        if not self._running:
            return
        try:
            request = data_dir() / "quit.request"
            if request.exists():
                request.unlink(missing_ok=True)
                self._quit()
                return
            self._drain_actions()
            if time.time() - self._last_prune >= 3600:
                self.store.prune(self.config.retain_days)
                self._last_prune = time.time()
            snapshot = self.snapshot()
            self.tray.update(snapshot)
            if self.config.alerts_enabled:
                for alert in self.alerts.due(snapshot):
                    self.tray.notify(alert.title, alert.message)
        except Exception:
            log.exception("failure in the main loop")
        finally:
            self._tick_id = self.root.after(TICK_MS, self._tick) if self._running else None

    def _drain_actions(self) -> None:
        while True:
            try:
                action = self._actions.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle(action)
            except Exception:
                log.exception("failed to run action %s", action)

    def _handle(self, action: str | dict) -> None:
        name = action.get("action") if isinstance(action, dict) else action
        if name in {"calibrate", "weekly", "edit_budget", "backup", "restore", "export_csv", "export_json"}:
            with dialog_parent(self.root):
                self._dispatch(action)
        else:
            self._dispatch(action)

    def _dispatch(self, action: str | dict) -> None:
        if isinstance(action, dict):
            if action.get("action") in {"export_csv", "export_json"}:
                self._export(action)
            return
        if action == "open_dashboard":
            self._open_dashboard()
        elif action == "open_history":
            self._open_dashboard(tab="history")
        elif action == "toggle_panel":
            self.panel.toggle()
        elif action == "reset_session":
            self.session.reset_now()
            # New window, new alerts: otherwise, after resetting with 90% spent,
            # it would not notify again for the whole next session.
            self.alerts.reset()
            self.tray.update(self.snapshot())
            if self.panel.visible:
                self.panel.refresh()
        elif action == "edit_budget":
            self._ask_budget()
        elif action == "calibrate":
            self._ask_calibrate()
        elif action == "weekly":
            self._ask_weekly()
        elif action == "history":
            self.history.show()
        elif action == "toggle_basis":
            self.config.set_basis("all" if self.config.percent_basis == "io" else "io")
            self.config.save()
            self.tray.update(self.snapshot())
            if self.panel.visible:
                self.panel.refresh()
        elif action == "toggle_alerts":
            self.config.alerts_enabled = not self.config.alerts_enabled
            self.config.save()
        elif action == "toggle_pause":
            paused = self.collectors_paused
            for collector in self._collectors():
                collector.resume() if paused else collector.pause()
        elif action == "open_data_dir":
            self._open_data_dir()
        elif action == "backup":
            self._backup()
        elif action == "restore":
            self._restore()
        elif action == "quit":
            self._quit()

    def _open_dashboard(self, tab: str = "") -> None:
        server = self.dashboard
        if server is None or not server.wait_ready(timeout=2.0):
            # Without a server there is no web window: the Tk interface is used.
            self.history.show() if tab == "history" else self.panel.show()
            return
        self.panel.hide()
        self.window.open(server.url + (f"?tab={tab}" if tab else ""))

    def _ask_session_reset(self) -> bool | None:
        """Anchors the session to the reset time shown on claude.ai.

        Returns True if it was anchored, False if the user left it empty and None
        if they cancelled or typed something that could not be used.
        """
        text = simpledialog.askstring(
            "Calibrate with claude.ai",
            "Open claude.ai → Settings → Usage. Under \"Current session\" it says\n"
            "\"Resets …\". Copy it as is (e.g. \"Fri 12:40 AM\" or \"in 3 hr 10 min\").\n\n"
            "That way the session starts when the web started it, also counting\n"
            "what you spent in the browser or on another machine.\n\n"
            "Leave it empty to keep the current time.",
            parent=self.root,
        )
        if text is None:
            return None
        if not text.strip():
            return False
        try:
            reset = parse_reset(text, time.time(), self.config.window_seconds)
        except AnchorError as exc:
            messagebox.showwarning("Calibrate", str(exc), parent=self.root)
            return None
        self.session.set_reset_from_web(reset)
        self.alerts.reset()
        return True

    def _ask_calibrate(self) -> None:
        """Anchors the session to the web and derives the budget from its percentage."""
        anchored = self._ask_session_reset()
        if anchored is None:
            return
        self._refresh_ui()

        snap = self.snapshot()
        if not snap.has_activity or snap.used_tokens <= 0:
            messagebox.showinfo(
                "Calibrate",
                ("Session time set from the web.\n\n" if anchored else "")
                + "There are no tokens counted in this session yet.\n\n"
                "Use Claude for a while and calibrate again: the further along "
                "the session is, the more precise the limit comes out.",
                parent=self.root,
            )
            return

        observed = simpledialog.askfloat(
            "Calibrate with claude.ai",
            "Open claude.ai → Settings → Usage and look at \"Current session\".\n"
            f"TokenMeter has counted {snap.used_tokens:,} tokens in this window.\n\n"
            "What percentage does the web show?",
            parent=self.root,
            minvalue=0.1,
            maxvalue=100.0,
        )
        if observed is None:
            return

        try:
            cal = calibrate(observed, snap.totals.basis(self.config.percent_basis), self.config.percent_basis)
        except CalibrationError as exc:
            messagebox.showwarning("Calibrate", str(exc), parent=self.root)
            return

        # How far off the current budget was. It is measured BEFORE applying the
        # new one, which is the only moment with both a prediction and an
        # observation.
        drift = (
            measure_drift(observed, snap.used_tokens, self.config.session_budget_tokens)
            if self.config.is_calibrated
            else None
        )

        self.store.add_calibration(
            ts=snap.now_ts,
            observed_percent=observed,
            counted_tokens=snap.totals.basis(self.config.percent_basis),
            basis=self.config.percent_basis,
            budget_tokens=cal.budget_tokens,
            session_start_ts=snap.start_ts,
            predicted_percent=drift.predicted_percent if drift else None,
            scope=self.config.percent_scope,
        )

        # With two or more points from this same session, the fit separates the
        # real quota from the tokens the web counts and the meter does not see.
        fit = self._fit_current_session(snap.start_ts)
        if fit is not None and not fit.misaligned:
            cal = replace(cal, budget_tokens=fit.budget_tokens)

        self.config.apply_calibration(cal)
        self.config.calibrated_session_start = snap.start_ts
        self.config.calibrated_scope = self.config.percent_scope
        self.config.calibrated_unseen_tokens = (fit.unseen_tokens if fit is not None and not fit.misaligned else 0)
        self.config.save()

        parts = [cal.summary()]
        if drift is not None:
            parts.append(drift.summary())
        if fit is not None:
            parts.append(fit.summary())
        messagebox.showinfo("Calibrated", "\n\n".join(parts), parent=self.root)

        self.tray.update(self.snapshot())
        if self.panel.visible:
            self.panel.refresh()

    def _fit_current_session(self, session_start_ts: float):
        rows = self.store.calibrations_for_session(session_start_ts, self.config.percent_basis,
                                                   self.config.percent_scope)
        return fit_session([(r["counted_tokens"], r["observed_percent"]) for r in rows])

    def calibration_health(self) -> tuple[str, bool]:
        """Calibration status for the panel: (text, is_warning)."""
        cfg = self.config
        if not cfg.is_calibrated:
            return ("budget not calibrated · menu → Calibrate", True)

        snap = self.snapshot()
        if snap.percent > 100:
            return ("above 100%: the estimated limit falls short, recalibrate", True)

        days = (time.time() - cfg.calibrated_at) / 86400
        when = datetime.fromtimestamp(cfg.calibrated_at).strftime("%d/%m %H:%M")
        base = f"calibrated with {cfg.calibrated_from_percent:g}% from claude.ai · {when}"
        if days >= STALE_CALIBRATION_DAYS:
            return (f"{base} · {int(days)} days ago, consider recalibrating", True)
        return (base, False)

    def _ask_weekly(self) -> None:
        """Configures the weekly window with the two numbers the web gives."""
        text = simpledialog.askstring(
            "Weekly window",
            "On claude.ai → Settings → Usage, under \"This week\", it says \"Resets …\".\n"
            "Copy it as is (e.g. \"Thursday 3:00 AM\"), or type the hours\n"
            "remaining.",
            parent=self.root,
        )
        if text is None or not text.strip():
            return
        try:
            reset = parse_reset(text, time.time(), self.weekly.window_seconds)
        except AnchorError as exc:
            messagebox.showwarning("Weekly window", str(exc), parent=self.root)
            return
        self.weekly.set_reset_at(reset)

        snap = self.weekly.snapshot()
        if snap.used_tokens <= 0:
            messagebox.showinfo(
                "Weekly window",
                "Cycle configured. There are no tokens counted this week yet, "
                "so the percentage is left pending calibration.",
                parent=self.root,
            )
            self._refresh_ui()
            return

        observed = simpledialog.askfloat(
            "Weekly window",
            f"TokenMeter has counted {snap.used_tokens:,} tokens this week.\n\n"
            "What percentage does \"This week\" show?",
            parent=self.root,
            minvalue=0.1,
            maxvalue=100.0,
        )
        if observed is None:
            self._refresh_ui()
            return

        try:
            cal = calibrate(observed, snap.used_tokens, self.config.percent_basis)
        except CalibrationError as exc:
            messagebox.showwarning("Weekly window", str(exc), parent=self.root)
            return

        self.config.apply_weekly_calibration(cal)
        self.config.save()
        messagebox.showinfo("Weekly window", cal.summary(), parent=self.root)
        self._refresh_ui()

    def _refresh_ui(self) -> None:
        self.tray.update(self.snapshot())
        if self.panel.visible:
            self.panel.refresh()

    def _ask_budget(self) -> None:
        self.root.deiconify()
        self.root.withdraw()  # ensures the dialog has a valid parent
        value = simpledialog.askinteger(
            "Session budget",
            "Tokens (input + output) that define 100%:",
            parent=self.root,
            initialvalue=self.config.session_budget_tokens,
            minvalue=1_000,
            maxvalue=1_000_000_000,
        )
        if value:
            self.config.invalidate_calibration(weekly=False)
            self.config.session_budget_tokens = value
            self.config.save()
            self.tray.update(self.snapshot())
            if self.panel.visible:
                self.panel.refresh()

    def _open_data_dir(self) -> None:
        path = str(data_dir())
        try:
            os.startfile(path)  # noqa: S606 - standard Windows API
        except Exception:
            try:
                subprocess.Popen(["explorer", path])
            except Exception:
                messagebox.showinfo("TokenMeter", f"Data folder:\n{path}", parent=self.root)

    def _backup(self) -> None:
        from .backups import create_backup

        path = filedialog.asksaveasfilename(parent=self.root, title="Save backup",
                                            defaultextension=".zip", initialfile="TokenMeter-backup.zip",
                                            filetypes=[("TokenMeter backup", "*.zip")])
        if not path:
            return
        try:
            create_backup(self.store, self.config, path)
            messagebox.showinfo("Backup", "Backup created and verified.", parent=self.root)
        except Exception as exc:
            messagebox.showerror("Backup", str(exc), parent=self.root)

    def _export(self, options: dict) -> None:
        from pathlib import Path
        import tempfile
        from .exports import export_usage
        from .files import output_destination

        format = "csv" if options["action"] == "export_csv" else "json"
        try:
            body, _, filename = export_usage(self, options.get("days", 30),
                provider=options.get("provider", ""), source=options.get("source", ""), format=format)
            path = filedialog.asksaveasfilename(parent=self.root, title="Export usage",
                initialfile=filename, defaultextension="."+format,
                filetypes=[(format.upper(), "*."+format)])
            if not path:
                return
            target = output_destination(path, self.store.path)
            fd, name = tempfile.mkstemp(prefix="export-", suffix=".tmp", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(body)
                os.replace(name, target)
            finally:
                Path(name).unlink(missing_ok=True)
            messagebox.showinfo("Export", "File saved with the selected filters.", parent=self.root)
        except Exception as exc:
            messagebox.showerror("Export", str(exc), parent=self.root)

    def _restore(self) -> None:
        from .backups import inspect_backup, restore_backup

        path = filedialog.askopenfilename(parent=self.root, title="Select backup",
                                          filetypes=[("TokenMeter backup", "*.zip")])
        if not path:
            return
        try:
            info = inspect_backup(path)
            if not messagebox.askyesno("Restore backup",
                    f"{info['calls']:,} calls · {info['first_day']} to {info['last_day']}\n\n"
                    "The current data and settings will be replaced. A backup of them will be created first.\n"
                    "TokenMeter will close when done; reopen it. Continue?", parent=self.root):
                return
            # Close every SQLite user before swapping the files.
            self.shutdown()
            restore_backup(path, data_dir())
            messagebox.showinfo("Restore", "Backup restored. Reopen TokenMeter.", parent=self.root)
            self.root.quit()
        except Exception as exc:
            messagebox.showerror("Restore", f"{exc}\nIf the application closed, reopen it.", parent=self.root)
            if not self._running:
                self.root.quit()

    def _quit(self) -> None:
        self.shutdown()
        try:
            self.root.quit()
        except Exception:
            log.debug("failed to stop the Tk loop", exc_info=True)
