"""Native Linux shell. No WebView, browser, Tk or service."""
from __future__ import annotations

import hashlib
import logging
import queue
import signal
import tempfile
from pathlib import Path

from ..billing import label as billing_label
from ..config import data_dir
from ..dashboard import build_state, build_history
from ..runtime import Runtime

log = logging.getLogger(__name__)


def model_line(row: dict, show_cost: bool) -> str:
    """One model of the breakdown. The billing type is part of the line because
    the same model can appear twice, under a subscription and through an API key.
    A local model has no API equivalent to show."""
    line = (f"{row['provider']} · {row['model']} ({billing_label(row['billing'], row['plan'])}): "
            f"{row['io']:,} tokens")
    if not show_cost:
        return line
    if row["billing"] == "local":
        return line + " · local, no cost"
    return line + f" · {row['cost_text']} API equivalent"


class LinuxShell:
    """One GTK main loop; the indicator and widgets belong to its main thread."""
    def __init__(self, runtime, *, notifier=None):
        from .notifications import DesktopNotifier
        self.runtime = runtime
        self.notifier = notifier if notifier is not None else DesktopNotifier()
        self._chart_key = None
        self.chart_bars = []
        self.window = self.indicator = None
        self._sources = []
        self._icons = None
        self._closed = False
        self._icon_key = None
        self._icon_generation = 0
        self.exit_code = 0
        self._action_running = False
        self._updating_filters = False
        self._active_dialog = None
        self._filter_options = None
        self.history_data = None

    def build(self):
        # Imports are deliberately local: even maintenance CLI commands must
        # work without a desktop or any installed GUI libraries.
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import Gtk, GLib, AyatanaAppIndicator3
        self.Gtk, self.GLib, self.AppIndicator = Gtk, GLib, AyatanaAppIndicator3
        if not Gtk.init_check()[0]:
            raise RuntimeError("A graphical Linux session is required")
        self._icons = tempfile.TemporaryDirectory(prefix="tray-", dir=data_dir())
        self.window = Gtk.Window(title="TokenMeter")
        self.window.set_default_size(620, 680)
        self.window.set_border_width(18)
        self.window.connect("delete-event", self._delete)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.body = box
        self.window.add(box)
        filters = Gtk.Box(spacing=8)
        self.provider_filter = Gtk.ComboBoxText()
        self.source_filter = Gtk.ComboBoxText()
        for title, control in (("Provider", self.provider_filter), ("Source", self.source_filter)):
            filters.pack_start(Gtk.Label(label=title), False, False, 0)
            filters.pack_start(control, True, True, 0)
            control.connect("changed", self._filters_changed)
        box.pack_start(filters, False, False, 0)
        self.tabs = Gtk.Notebook()
        box.pack_start(self.tabs, True, True, 0)
        overview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        overview.set_border_width(12)
        self.summary = self._label()
        overview.pack_start(self.summary, False, False, 0)
        self.models = self._label()
        overview.pack_start(self.models, False, False, 0)
        self.limits = self._label()
        overview.pack_start(self.limits, False, False, 0)
        overview_scroll = Gtk.ScrolledWindow()
        overview_scroll.add(overview)
        self.tabs.append_page(overview_scroll, Gtk.Label(label="Overview"))
        history = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        history.set_border_width(12)
        self.days_filter = Gtk.ComboBoxText()
        for days in (7, 30, 90):
            self.days_filter.append(str(days), f"{days} days")
        self.days_filter.set_active_id("30")
        self.days_filter.connect("changed", self._filters_changed)
        history.pack_start(self.days_filter, False, False, 0)
        self.history_summary = self._label()
        history.pack_start(self.history_summary, False, False, 0)
        self.chart = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, homogeneous=True)
        self.chart.set_size_request(-1, 110)
        history.pack_start(self.chart, False, False, 0)
        self.history_store = Gtk.ListStore(str, str, str, str)
        tree = Gtk.TreeView(model=self.history_store)
        for column, title in enumerate(("Day", "Tokens", "Calls", "Coverage")):
            cell = Gtk.CellRendererText()
            tree.append_column(Gtk.TreeViewColumn(title, cell, text=column))
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(160)
        scroll.add(tree)
        history.pack_start(scroll, True, True, 0)
        note = self._label()
        note.set_text("No record means unknown usage, not zero. Partial days have incomplete coverage.")
        history.pack_start(note, False, False, 0)
        exports = Gtk.Box(spacing=8)
        self.export_json_button = self._button("Export JSON", lambda: self._export("json"))
        self.export_csv_button = self._button("Export CSV", lambda: self._export("csv"))
        exports.pack_start(self.export_json_button, True, True, 0)
        exports.pack_start(self.export_csv_button, True, True, 0)
        history.pack_start(exports, False, False, 0)
        self.tabs.append_page(history, Gtk.Label(label="History"))
        settings = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        settings.set_border_width(12)
        for attribute, title, action in (
            ("budget_button", "Manual session budget", self._ask_budget),
            ("calibrate_button", "Calibrate with claude.ai", self._calibrate),
            ("weekly_button", "Configure weekly window", self._weekly),
            ("reset_button", "Reset session now (keep history)", self._reset_session)):
            button = self._button(title, action)
            setattr(self, attribute, button)
            settings.pack_start(button, False, False, 0)
        self.basis_toggle = Gtk.CheckButton(label="Include cache in the Claude percentage")
        self.basis_toggle.set_active(self.runtime.config.percent_basis == "all")
        self.basis_toggle.connect("toggled", lambda *_: self._perform(self._toggle_basis))
        settings.pack_start(self.basis_toggle, False, False, 0)
        self.alerts_toggle = Gtk.CheckButton(label="Notify at " + " / ".join(f"{n:g}" for n in self.runtime.config.alert_thresholds) + "%")
        self.alerts_toggle.set_active(self.runtime.config.alerts_enabled)
        self.alerts_toggle.connect("toggled", lambda *_: self._perform(self._toggle_alerts))
        settings.pack_start(self.alerts_toggle, False, False, 0)
        self.tabs.append_page(settings, Gtk.Label(label="Settings"))
        maintenance = Gtk.Box(spacing=8)
        self.backup_button = self._button("Create backup", self._backup)
        self.restore_button = self._button("Restore backup", self._restore)
        maintenance.pack_start(self.backup_button, True, True, 0)
        maintenance.pack_start(self.restore_button, True, True, 0)
        box.pack_start(maintenance, False, False, 0)
        self.cost_toggle = Gtk.CheckButton(label="Show API equivalent")
        self.cost_toggle.set_active(self.runtime.config.show_cost)
        self.cost_toggle.connect("toggled", lambda *_: self._perform(self._toggle_cost))
        box.pack_start(self.cost_toggle, False, False, 0)
        controls = Gtk.Box(spacing=8)
        self.pause_button = Gtk.Button(label="Pause collectors")
        self.pause_button.connect("clicked", lambda *_: self.runtime.enqueue("pause"))
        controls.pack_start(self.pause_button, True, True, 0)
        self.quit_button = Gtk.Button(label="Quit")
        self.quit_button.connect("clicked", lambda *_: self.close())
        controls.pack_start(self.quit_button, True, True, 0)
        box.pack_end(controls, False, False, 0)
        menu = Gtk.Menu()
        self.open_item = Gtk.MenuItem(label="Open TokenMeter")
        self.open_item.connect("activate", lambda *_: self.show())
        menu.append(self.open_item)
        pause = Gtk.MenuItem(label="Pause / resume collectors")
        pause.connect("activate", lambda *_: self.runtime.enqueue("pause"))
        menu.append(pause)
        self.quit_item = Gtk.MenuItem(label="Quit")
        self.quit_item.connect("activate", lambda *_: self.close())
        menu.append(self.quit_item)
        menu.show_all()
        instance = hashlib.sha256(str(data_dir().resolve()).encode()).hexdigest()[:12]
        self.indicator = self.AppIndicator.Indicator.new(
            "tokenmeter-" + instance, "", self.AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_menu(menu)
        self.indicator.set_title("TokenMeter")
        self.refresh()
        self.indicator.set_status(self.AppIndicator.IndicatorStatus.ACTIVE)
        self.show()

    def show(self):
        self.window.show_all()
        self.window.present()

    def _label(self):
        label = self.Gtk.Label(xalign=0)
        label.set_line_wrap(True)
        label.set_selectable(True)
        return label

    def _button(self, title, action):
        button = self.Gtk.Button(label=title)
        button.connect("clicked", lambda *_: self._perform(action))
        return button

    def _filters_changed(self, *_):
        if self.indicator is not None and not self._updating_filters and not self._action_running and not self._closed:
            self.refresh()

    def _populate_filters(self):
        options = self.runtime.store.filter_options()
        if options == self._filter_options:
            return
        self._updating_filters = True
        try:
            for field, control, title in (("provider", self.provider_filter, "All providers"),
                                           ("source", self.source_filter, "All sources")):
                selected = control.get_active_id() or ""
                control.remove_all()
                control.append("", title)
                for value in options[field]:
                    control.append(value, value)
                control.set_active_id(selected if selected in options[field] else "")
            self._filter_options = options
        finally:
            self._updating_filters = False

    def _selection(self):
        return {"days": int(self.days_filter.get_active_id() or "30"),
                "provider": self.provider_filter.get_active_id() or "",
                "source": self.source_filter.get_active_id() or ""}

    def refresh(self):
        self._populate_filters()
        selection = self._selection()
        state = build_state(self.runtime, provider=selection["provider"], source=selection["source"])
        session, tokens = state["session"], state["tokens"]
        pct = session["percent"]
        percent = "—" if pct is None else f"{pct:.1f}%"
        lines = [f"Claude session: {percent} · {session['countdown']}",
                 session["origin"]["text"], state["general_window"],
                 f"Input {tokens['input']:,} · Output {tokens['output']:,}",
                 f"Cache read {tokens['cache_read']:,} · write {tokens['cache_write']:,}",
                 f"Calls: {tokens['calls']:,}"]
        if state["show_cost"]:
            lines.append("API equivalent: " + state["cost"]["text"])
        lines.extend((state["sources"], state["warning"]))
        self.summary.set_text("\n".join(line for line in lines if line))
        self.models.set_text("\n".join(
            model_line(row, state["show_cost"]) for row in state["models"]))
        limits = []
        week = state["weekly"]
        if week["enabled"]:
            limits.extend(("Claude week: " + week["detail"], week["origin"]["text"]))
        codex = state["codex"]
        if codex:
            limits.append(f"Codex {codex['plan']} · captured {codex['age_text']}" +
                          (" · stale" if codex["stale"] else ""))
            for name in ("session", "week"):
                window = codex[name]
                if window:
                    used = window["used_percent"]
                    percent = "—" if used is None else f"{used:g}%"
                    limits.append(f"{name}: {percent} · {window['remaining_text']}")
        self.limits.set_text("\n".join(limits))
        self.history_data = build_history(self.runtime, **selection)
        history = self.history_data
        summary = history["summary"]
        text = (f"{summary['total']:,} tokens · {summary['calls']:,} calls · "
                f"{summary['partial_days']} partial days · {summary['unknown_days']} days with no record")
        if state["show_cost"]:
            text += "\nAPI equivalent: " + history["cost"]["text"]
        self.history_summary.set_text(text)
        self._update_chart(history["bars"])
        self.history_store.clear()
        for bar in history["bars"]:
            value = "No record" if bar["tokens"] is None else f"{bar['tokens']:,}"
            calls = "—" if bar["tokens"] is None else str(bar["calls"])
            self.history_store.append([bar["day"], value, calls, bar["coverage"]])
        self.pause_button.set_label("Resume collectors" if self.runtime.collectors_paused
                                    else "Pause collectors")
        snap = self.runtime.snapshot()
        key = (int(snap.percent), snap.has_activity, self.runtime.config.warn_percent,
               self.runtime.config.danger_percent)
        if key != self._icon_key:
            from .icon import render_icon
            self._icon_generation += 1
            # Alternate two owned files so the indicator notices the new image.
            path = Path(self._icons.name) / f"status-{self._icon_generation % 2}.png"
            render_icon(snap.percent, has_activity=snap.has_activity,
                        warn=key[2], danger=key[3]).save(path)
            self.indicator.set_icon_full(str(path), "TokenMeter")
            self._icon_key = key
        return state

    def _update_chart(self, bars):
        key = tuple((b["day"], b["tokens"], b["coverage"]) for b in bars)
        if key == self._chart_key:
            return
        self._chart_key = key
        for child in self.chart.get_children():
            self.chart.remove(child)
            child.destroy()
        self.chart_bars = []
        peak = max((b["tokens"] or 0 for b in bars), default=0)
        for bar in bars:
            if bar["tokens"] is None:
                widget = self.Gtk.Label(label="·")
                detail = "No record"
            else:
                widget = self.Gtk.ProgressBar(orientation=self.Gtk.Orientation.VERTICAL, inverted=True)
                widget.set_fraction(max(0.01, bar["tokens"]/peak) if bar["tokens"] and peak else 0)
                detail = f"{bar['tokens']:,} tokens · {bar['calls']} calls"
            widget.set_tooltip_text(f"{bar['day']} · {detail} · {bar['coverage']}")
            self.chart.pack_start(widget, True, True, 0)
            self.chart_bars.append(widget)
        self.chart.show_all()

    def _prompt(self, title, text, initial=""):
        Gtk = self.Gtk
        dialog = Gtk.Dialog(title=title, transient_for=self.window, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Apply", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        label = Gtk.Label(label=text)
        label.set_line_wrap(True)
        label.set_max_width_chars(65)
        box.pack_start(label, False, False, 8)
        entry = Gtk.Entry()
        entry.set_text(initial)
        entry.set_activates_default(True)
        box.pack_start(entry, False, False, 8)
        dialog.set_default_response(Gtk.ResponseType.OK)
        self._prompt_entry = entry
        self._active_dialog = dialog
        try:
            dialog.show_all()
            return entry.get_text().strip() if dialog.run() == Gtk.ResponseType.OK and not self._closed else None
        finally:
            self._active_dialog = None
            self._prompt_entry = None
            dialog.destroy()

    def _ask_budget(self):
        from ..operations import set_budget
        value = self._prompt("Session budget", "Tokens defining 100% of the Claude session:",
                             str(self.runtime.config.session_budget_tokens))
        if value is not None:
            set_budget(self.runtime, int(value))

    def _calibrate(self):
        import time
        from ..anchors import parse_reset
        from ..operations import calibrate_session
        reset = self._prompt("Session reset", "Copy the current session's reset time from claude.ai. "
                             "Leave empty to keep the current session.")
        if reset is None:
            return
        if reset:
            when = parse_reset(reset, time.time(), self.runtime.config.window_seconds)
            self.runtime.session.set_reset_from_web(when)
            self.runtime.alerts.reset()
        value = self._prompt("Observed session percentage", "Current session percentage on claude.ai (0–100):")
        if value is not None:
            self._message("Calibrated", calibrate_session(self.runtime, float(value)))

    def _weekly(self):
        import time
        from ..anchors import parse_reset
        from ..operations import calibrate_week
        reset = self._prompt("Weekly reset", "Copy the weekly reset time from claude.ai (for example, in 2 days).")
        if reset is None or not reset:
            return
        when = parse_reset(reset, time.time(), self.runtime.weekly.window_seconds)
        self.runtime.weekly.set_reset_at(when)
        if self.runtime.weekly.snapshot().used_tokens <= 0:
            self._message("Weekly window", "Cycle configured. Usage is not available yet; calibrate after using Claude.")
            return
        value = self._prompt("Observed weekly percentage", "Weekly percentage on claude.ai (0–100):")
        if value is not None:
            self._message("Weekly calibrated", calibrate_week(self.runtime, float(value)))

    def _reset_session(self):
        self.runtime.session.reset_now()
        self.runtime.alerts.reset()

    def _toggle_basis(self):
        self.runtime.config.set_basis("all" if self.basis_toggle.get_active() else "io")
        self.runtime.config.save()

    def _toggle_alerts(self):
        self.runtime.config.alerts_enabled = self.alerts_toggle.get_active()
        self.runtime.config.save()

    def _perform(self, action):
        if self._closed or self._action_running:
            return
        self._action_running = True
        self.body.set_sensitive(False)
        try:
            action()
        except Exception as exc:
            if not self._closed:
                self._message("Operation failed", str(exc), error=True)
        finally:
            self._action_running = False
            if not self._closed:
                self.body.set_sensitive(True)
                self.refresh()

    def _run_dialog(self, dialog):
        self._active_dialog = dialog
        try:
            dialog.show_all()
            return dialog.run()
        finally:
            self._active_dialog = None
            dialog.destroy()

    def _message(self, title, text, *, error=False, confirm=False):
        Gtk = self.Gtk
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
            message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.QUESTION if confirm else Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.YES_NO if confirm else Gtk.ButtonsType.OK,
            text=title)
        dialog.set_title(title)
        dialog.format_secondary_text(text)
        response = self._run_dialog(dialog)
        return response == Gtk.ResponseType.YES if confirm else response

    def _choose_file(self, title, *, name=None):
        Gtk = self.Gtk
        dialog = Gtk.FileChooserDialog(title=title, transient_for=self.window, modal=True,
            action=Gtk.FileChooserAction.SAVE if name else Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Save" if name else "Open", Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        dialog.set_current_folder(str(data_dir()))
        if name:
            dialog.set_current_name(name)
            dialog.set_do_overwrite_confirmation(True)
        selected = None
        self._active_dialog = dialog
        try:
            dialog.show_all()
            if dialog.run() == Gtk.ResponseType.ACCEPT and not self._closed:
                selected = dialog.get_filename()
        finally:
            self._active_dialog = None
            dialog.destroy()
        return selected

    def _export(self, format):
        from datetime import date
        from ..operations import save_export
        selection = self._selection()
        name = f"tokenmeter-{date.today():%Y-%m-%d}-{selection['days']}d.{format}"
        path = self._choose_file("Export usage", name=name)
        if path is not None:
            save_export(self.runtime, path, format=format, **selection)
            self._message("Export", "File saved with the selected filters.")

    def _backup(self):
        from ..backups import create_backup
        path = self._choose_file("Save backup", name="TokenMeter-backup.zip")
        if path is not None:
            create_backup(self.runtime.store, self.runtime.config, path)
            self._message("Backup", "Backup created and verified.")

    def _restore(self):
        from ..backups import inspect_backup, restore_backup
        path = self._choose_file("Select backup")
        if path is None:
            return
        info = inspect_backup(path)
        if not self._message("Restore backup",
            f"{info['calls']:,} calls · {info['first_day']} to {info['last_day']}\n\n"
            "The current data and settings will be replaced. A previous backup will be saved. "
            "TokenMeter will close afterward. Continue?", confirm=True):
            return
        try:
            self.runtime.shutdown()
            previous = restore_backup(path, data_dir())
            self._message("Restore", f"Backup restored. Reopen TokenMeter.\nPrevious backup: {previous}")
        except Exception as exc:
            if self.runtime.store.closed:
                self.exit_code = 1
            if not self._closed:
                self._message("Restore failed", str(exc) +
                              ("\nReopen TokenMeter after closing this message."
                               if self.runtime.store.closed else ""), error=True)
        finally:
            if self.runtime.store.closed:
                self.close()

    def _toggle_cost(self):
        self.runtime.config.show_cost = self.cost_toggle.get_active()
        self.runtime.config.save()

    def _tick(self):
        if self._closed:
            return False
        try:
            request = data_dir() / "quit.request"
            if request.exists():
                request.unlink()
                self.close()
                return False
            if self._action_running:
                return True
            while True:
                try:
                    action = self.runtime._actions.get_nowait()
                except queue.Empty:
                    break
                if action == "pause":
                    self.runtime.toggle_pause()
            self.refresh()
            if self.runtime.config.alerts_enabled:
                for alert in self.runtime.alerts.due(self.runtime.snapshot()):
                    self.notifier.notify(alert.title, alert.message)
                for alert in self.runtime.due_budget_alerts():
                    self.notifier.notify(alert.title, alert.message)
        except Exception:
            log.exception("Linux shell update failed")
            self.exit_code = 1
            self.close()
            return False
        return True

    def _delete(self, *_):
        self.close()
        return True

    def _signal(self):
        self.close()
        return True

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.notifier.close()
        if self.indicator is not None:
            self.indicator.set_status(self.AppIndicator.IndicatorStatus.PASSIVE)
        if self._active_dialog is not None:
            self._active_dialog.response(self.Gtk.ResponseType.CANCEL)
        if self.window is not None:
            self.window.destroy()
        if hasattr(self, "Gtk") and self.Gtk.main_level():
            self.GLib.idle_add(self._quit_main_loop)

    def _quit_main_loop(self):
        if self.Gtk.main_level():
            self.Gtk.main_quit()
        return False

    def run(self, ready=None):
        try:
            self.build()
            self.runtime.start()
            self.refresh()
            self._sources.append(self.GLib.timeout_add(250, self._tick))
            for sig in (signal.SIGINT, signal.SIGTERM):
                self._sources.append(self.GLib.unix_signal_add(
                    self.GLib.PRIORITY_DEFAULT, sig, self._signal))
            if ready is not None:
                ready(self)
            self.Gtk.main()
            return self.exit_code
        finally:
            self.close()
            for source in self._sources:
                if self.GLib.MainContext.default().find_source_by_id(source):
                    self.GLib.source_remove(source)
            try:
                self.runtime.shutdown()
            finally:
                if self._icons is not None:
                    self._icons.cleanup()


def run(config):
    runtime = Runtime(config)
    try:
        return LinuxShell(runtime).run()
    finally:
        runtime.shutdown()
