"""Native dialog fixture driven through test-owned GTK controls only."""
from __future__ import annotations

from collections import deque
import csv
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

from tokenmeter.backups import create_backup, inspect_backup
from tokenmeter.config import Config, data_dir
from tokenmeter.instance import InstanceLock
from tokenmeter.models import UsageEvent
from tokenmeter.runtime import Runtime
from tokenmeter.store import Store
from tokenmeter.ui.linux import LinuxShell
from tests.linux_indicator import IndicatorHost


def make_runtime(home):
    config = Config(proxy_enabled=False, transcripts_enabled=False, codex_enabled=False,
                    transcripts_root=str(home / "synthetic-claude"),
                    codex_root=str(home / "synthetic-codex"))
    config.save()
    runtime = Runtime(config)
    for provider, source, tokens in (("anthropic", "transcript", 120), ("openai", "codex", 70)):
        runtime.store.add_event(UsageEvent(
            ts=time.time()-5, provider=provider, model="synthetic-"+provider,
            input_tokens=tokens-20, output_tokens=20, source=source,
            billing="subscription", dedupe_key="req:gtk-files-"+provider))
    now = time.time()
    runtime.store.set_state("codex_rate_limits", json.dumps({
        "captured_ts": now, "plan_type": "synthetic", "primary": {
            "used_percent": 25, "window_minutes": 300, "resets_at": now+3600},
        "secondary": {"used_percent": 5, "window_minutes": 10080,
                      "resets_at": now+86400}}))
    return runtime


def main():
    output, scenario = Path(sys.argv[1]), sys.argv[2]
    home = data_dir()
    result = {}
    errors = []
    with InstanceLock(home):
        runtime = make_runtime(home)
        class Notifier:
            calls = []
            closed = False
            def notify(self, title, message):
                self.calls.append((title, message))
                return True
            def close(self):
                self.closed = True
        notifier = Notifier()
        indicator_host = IndicatorHost()
        shell = LinuxShell(runtime, notifier=notifier)
        selected_json = home / "filtered.json"
        selected_csv = home / "filtered.csv"
        backup = home / "snapshot.zip"
        responses = deque()
        dialog_titles = []

        def ready(ui):
            Gtk = ui.Gtk
            ui.source_filter.set_active_id("codex")
            ui.days_filter.set_active_id("7")
            ui.tabs.set_current_page(1)
            result["filtered_calls"] = ui.history_data["summary"]["calls"]
            result["filtered_tokens"] = ui.history_data["summary"]["total"]
            result["rows"] = len(ui.history_store)
            result["chart_ready"] = len(ui.chart_bars) == 7 and ui.chart_bars[-1].get_fraction() == 1
            result["unknown_calls_hidden"] = all(row[2] == "—" for row in ui.history_store if row[1] == "No record")
            result["codex_limits_visible"] = ("Codex synthetic" in ui.limits.get_text()
                and "session: 25%" in ui.limits.get_text() and "week: 5%" in ui.limits.get_text())
            ui.cost_toggle.set_active(False)
            result["cost_hidden"] = all("API equivalent" not in widget.get_text()
                for widget in (ui.summary, ui.models, ui.history_summary))
            ui.cost_toggle.set_active(True)
            assert result["filtered_calls"] == 1 and result["filtered_tokens"] == 70
            if scenario == "settings":
                responses.extend([
                    ("Session budget", "2000", Gtk.ResponseType.OK),
                    ("Session reset", "", Gtk.ResponseType.OK),
                    ("Observed session percentage", "20", Gtk.ResponseType.OK),
                    ("Calibrated", None, Gtk.ResponseType.OK),
                    ("Weekly reset", "in 2 days", Gtk.ResponseType.OK),
                    ("Observed weekly percentage", "50", Gtk.ResponseType.OK),
                    ("Weekly calibrated", None, Gtk.ResponseType.OK)])
            elif scenario in {"cancel-settings", "invalid-settings"}:
                responses.append(("Session budget", "not-a-number", Gtk.ResponseType.CANCEL if scenario == "cancel-settings" else Gtk.ResponseType.OK))
                if scenario == "invalid-settings":
                    responses.append(("Operation failed", None, Gtk.ResponseType.OK))
            elif scenario == "roundtrip":
                responses.extend([
                    ("Export usage", selected_json, Gtk.ResponseType.ACCEPT),
                    ("Export", None, Gtk.ResponseType.OK),
                    ("Export usage", selected_csv, Gtk.ResponseType.ACCEPT),
                    ("Export", None, Gtk.ResponseType.OK),
                    ("Save backup", backup, Gtk.ResponseType.ACCEPT),
                    ("Backup", None, Gtk.ResponseType.OK),
                    ("Select backup", backup, Gtk.ResponseType.ACCEPT),
                    ("Restore backup", None, Gtk.ResponseType.YES),
                    ("Restore", None, Gtk.ResponseType.OK)])
            elif scenario in {"restore-failure", "cancel-restore"}:
                create_backup(runtime.store, runtime.config, backup)
                if scenario == "restore-failure":
                    from tokenmeter import backups
                    def fail_restore(*args):
                        raise OSError("synthetic restore failure after shutdown")
                    backups.restore_backup = fail_restore
                responses.extend([("Select backup", backup, Gtk.ResponseType.ACCEPT),
                                  ("Restore backup", None, Gtk.ResponseType.YES if scenario == "restore-failure" else Gtk.ResponseType.NO)])
                if scenario == "restore-failure":
                    responses.append(("Restore failed", None, Gtk.ResponseType.OK))
            elif scenario == "invalid-backup":
                backup.write_bytes(b"invalid synthetic archive")
                responses.extend([("Select backup", backup, Gtk.ResponseType.ACCEPT),
                                  ("Operation failed", None, Gtk.ResponseType.OK)])
            elif scenario == "protected-export":
                responses.extend([("Export usage", home / "quit.request", Gtk.ResponseType.ACCEPT),
                                  ("Operation failed", None, Gtk.ResponseType.OK)])
            elif scenario == "dialog-sigterm":
                responses.append(("Export usage", None, "sigterm"))
            else:
                responses.append(("Export usage", None, Gtk.ResponseType.CANCEL))
            last_sent = None
            prepared = None
            prepared_at = 0.0
            sent_at = 0.0
            mapped_dialog = None
            mapped_at = 0.0
            def respond():
                nonlocal last_sent, prepared, prepared_at, sent_at, mapped_dialog, mapped_at
                if ui._closed:
                    return False
                dialog = ui._active_dialog
                if last_sent is not None:
                    if dialog is last_sent:
                        # GTK can defer accepting Save until its folder model
                        # settles. Retry only this same still-open chooser.
                        if isinstance(dialog, Gtk.FileChooserDialog) and time.monotonic()-sent_at > 0.5:
                            last_sent = None
                        else:
                            return True
                    else:
                        responses.popleft()
                        prepared = None
                    last_sent = None
                if dialog is None:
                    return True
                # A response before the chooser is mapped can race GTK's
                # initial folder/selection and X11 window configuration.
                if not dialog.get_mapped():
                    return True
                if mapped_dialog is not dialog:
                    mapped_dialog, mapped_at = dialog, time.monotonic()
                    return True
                if time.monotonic()-mapped_at < 0.2:
                    return True
                try:
                    assert responses, "Unexpected native dialog"
                    title, target, response = responses[0]
                    assert dialog.get_title() == title, (dialog.get_title(), title)
                    if isinstance(dialog, Gtk.FileChooserDialog) and target is not None:
                        if prepared is not dialog or (dialog.get_filename() != str(target)
                                                     and time.monotonic()-prepared_at > 0.5):
                            if dialog.get_action() == Gtk.FileChooserAction.SAVE:
                                dialog.set_current_folder(str(target.parent))
                                dialog.set_current_name(target.name)
                            else:
                                dialog.set_filename(str(target))
                            prepared, prepared_at = dialog, time.monotonic()
                            return True
                        if time.monotonic()-prepared_at < 0.25 or dialog.get_filename() != str(target):
                            return True
                    if isinstance(target, str) and getattr(ui, "_prompt_entry", None) is not None:
                        ui._prompt_entry.set_text(target)
                    if response != "sigterm":
                        button = dialog.get_widget_for_response(response)
                        assert button is not None
                        if not button.get_sensitive():
                            return True
                    last_sent, sent_at = dialog, time.monotonic()
                    dialog_titles.append(title)
                    if response == "sigterm":
                        os.kill(os.getpid(), signal.SIGTERM)
                    else:
                        button = dialog.get_widget_for_response(response)
                        assert button is not None
                        button.clicked()
                except BaseException as exc:
                    import traceback
                    errors.append(traceback.format_exc())
                    ui.exit_code = 1
                    ui.close()
                    return False
                return True
            ui.GLib.timeout_add(60, respond)

            def drive():
                try:
                    if scenario == "settings":
                        ui.tabs.set_current_page(2)
                        ui.budget_button.clicked()
                        assert runtime.config.session_budget_tokens == 2000
                        ui.calibrate_button.clicked()
                        assert runtime.config.session_budget_tokens == 600 and runtime.config.is_calibrated
                        ui.weekly_button.clicked()
                        assert runtime.config.weekly_budget_tokens == 240 and runtime.config.weekly_is_calibrated
                        ui.basis_toggle.set_active(True)
                        assert runtime.config.percent_basis == "all" and not runtime.config.is_calibrated
                        ui.basis_toggle.set_active(False)
                        runtime.config.session_budget_tokens = 100
                        runtime.alerts.reset()
                        ui._tick()
                        ui._tick()
                        assert len(notifier.calls) == 1, ("notification count", len(notifier.calls), runtime.snapshot().percent, runtime.config.alerts_enabled)
                        ui.alerts_toggle.set_active(False)
                        runtime.alerts.reset()
                        ui._tick()
                        assert len(notifier.calls) == 1, ("notification count", len(notifier.calls), runtime.snapshot().percent, runtime.config.alerts_enabled)
                        ui.reset_button.clicked()
                        assert not runtime.snapshot().has_activity
                        assert runtime.store.totals_since(0).calls == 2
                        result["settings_verified"] = True
                        ui.quit_button.clicked()
                    elif scenario in {"cancel-settings", "invalid-settings"}:
                        before = (home / "config.json").read_bytes()
                        ui.budget_button.clicked()
                        assert (home / "config.json").read_bytes() == before
                        result["settings_verified"] = True
                        ui.quit_button.clicked()
                    elif scenario == "roundtrip":
                        ui.export_json_button.clicked()
                        exported = json.loads(selected_json.read_text())
                        assert exported["filters"] == {"provider": "", "source": "codex"}
                        assert len(exported["events"]) == 1
                        ui.export_csv_button.clicked()
                        with selected_csv.open(encoding="utf-8-sig", newline="") as fh:
                            csv_rows = list(csv.DictReader(fh))
                        assert len(csv_rows) == 1 and csv_rows[0]["source"] == "codex"
                        ui.backup_button.clicked()
                        assert inspect_backup(backup)["calls"] == 2
                        runtime.store.add_event(UsageEvent(
                            ts=time.time(), provider="openai", model="synthetic-extra",
                            input_tokens=10, output_tokens=1, source="codex",
                            dedupe_key="req:gtk-extra"))
                        assert runtime.store.totals_since(0).calls == 3
                        ui.restore_button.clicked()
                    elif scenario in {"restore-failure", "cancel-restore"}:
                        ui.restore_button.clicked()
                        if scenario == "cancel-restore":
                            assert not runtime.store.closed
                            assert runtime.store.totals_since(0).calls == 2
                            ui.quit_button.clicked()
                    elif scenario == "invalid-backup":
                        ui.restore_button.clicked()
                        assert runtime.store.totals_since(0).calls == 2
                        ui.quit_button.clicked()
                    else:
                        ui.export_json_button.clicked()
                        assert not selected_json.exists()
                        assert not (home / "quit.request").exists()
                        if not ui._closed:
                            assert runtime.store.totals_since(0).calls == 2
                            ui.quit_button.clicked()
                except BaseException as exc:
                    import traceback
                    errors.append(traceback.format_exc())
                    ui.exit_code = 1
                    ui.close()
                return False
            # Let the main window map before opening its first modal dialog.
            ui.GLib.timeout_add(300, drive)
            def guard():
                if not ui._closed:
                    errors.append("Native file operation exceeded 10 seconds: " + repr((ui._active_dialog.get_title() if ui._active_dialog else None,
                        ui._active_dialog.get_filename() if isinstance(ui._active_dialog, Gtk.FileChooserDialog) else None)))
                    ui.exit_code = 1
                    ui.close()
                return False
            ui.GLib.timeout_add(10000, guard)
        try:
            code = shell.run(ready)
        finally:
            runtime.shutdown()
        assert code == (1 if scenario == "restore-failure" else 0) and not errors, errors
        assert runtime.store.closed
        if indicator_host.registration is not None:
            assert indicator_host.items, "Native indicator did not register on the test bus"
        if scenario in {"roundtrip", "restore-failure", "cancel-restore"}:
            restored = Store(home / "usage.db")
            try:
                result["restored_calls"] = restored.totals_since(0).calls
                result["integrity"] = restored._conn.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                restored.close()
            if scenario == "roundtrip":
                previous = list(home.glob("backup-before-restore-*.zip"))
                assert len(previous) == 1
                result["previous_calls"] = inspect_backup(previous[0])["calls"]
        result.update(closed=runtime.store.closed, errors=errors, shell_exit_code=code,
                      notifier_closed=notifier.closed,
                      dialog_titles=dialog_titles,
                      workers=[t.name for t in threading.enumerate()
                               if t.name in {"proxy", "transcripts", "codex"}],
                      webview_loaded=any(k.startswith("webview") for k in sys.modules))
    output.write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
