"""Native GTK fixture for the Budgets and Sessions tabs. Synthetic data only."""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import sys
import time
import traceback

from tokenmeter import budgets
from tokenmeter.config import data_dir
from tokenmeter.instance import InstanceLock
from tests.linux_files_app import make_runtime
from tests.linux_indicator import IndicatorHost
from tokenmeter.ui.linux import LinuxShell


def main():
    output = Path(sys.argv[1])
    home = data_dir()
    result, errors = {}, []
    with InstanceLock(home):
        runtime = make_runtime(home)  # 120 anthropic tokens and 70 openai tokens, just now

        class Notifier:
            calls = []
            def notify(self, title, message):
                self.calls.append((title, message))
                return True
            def close(self):
                pass
        notifier = Notifier()
        indicator_host = IndicatorHost()
        shell = LinuxShell(runtime, notifier=notifier)
        # (dialog title, fields to type or None, response name)
        answers = deque()
        seen = []

        def ready(ui):
            Gtk = ui.Gtk
            mapped = {"dialog": None, "at": 0.0, "sent": None}

            def respond():
                if ui._closed:
                    return False
                dialog = ui._active_dialog
                if dialog is None or dialog is mapped["sent"] or not dialog.get_mapped():
                    return True
                if mapped["dialog"] is not dialog:
                    mapped["dialog"], mapped["at"] = dialog, time.monotonic()
                    return True
                if time.monotonic() - mapped["at"] < 0.2:
                    return True
                try:
                    assert answers, "Unexpected dialog: " + str(dialog.get_title())
                    title, fields, response = answers.popleft()
                    assert dialog.get_title() == title, (dialog.get_title(), title)
                    if fields is not None:
                        for field, value in fields.items():
                            control = ui._budget_controls[field]
                            if field in ("metric", "token_basis", "period", "scope_kind"):
                                control.set_active_id(value)
                            else:
                                control.set_text(value)
                    seen.append(title)
                    mapped["sent"] = dialog
                    dialog.get_widget_for_response(getattr(Gtk.ResponseType, response)).clicked()
                except BaseException:
                    errors.append(traceback.format_exc())
                    ui.exit_code = 1
                    ui.close()
                    return False
                return True
            ui.GLib.timeout_add(60, respond)

            def rows():
                return [tuple(row) for row in ui.budget_store]

            def drive():
                try:
                    ui.tabs.set_current_page(2)
                    result["empty_note"] = ui.budget_note.get_text()
                    result["defaults"] = None

                    # Add: a daily token budget already past its limit.
                    answers.append(("New budget", {
                        "name": "Daily tokens", "metric": "tokens", "token_basis": "io",
                        "period": "day", "scope_kind": "all", "limit": "100",
                        "thresholds": "50, 100"}, "OK"))
                    ui.budget_add_button.clicked()
                    result["after_add"] = rows()

                    # A bad limit is refused with a message and nothing is stored.
                    answers.append(("New budget", {"name": "Broken", "limit": "lots"}, "OK"))
                    answers.append(("Operation failed", None, "OK"))
                    ui.budget_add_button.clicked()
                    result["after_invalid"] = len(rows())

                    # Cancel stores nothing either.
                    answers.append(("New budget", {"name": "Cancelled", "limit": "5"}, "CANCEL"))
                    ui.budget_add_button.clicked()
                    result["after_cancel"] = len(rows())

                    # Editing needs a selection.
                    answers.append(("Operation failed", None, "OK"))
                    ui.budget_edit_button.clicked()

                    # Edit: same budget (same id), now one provider and a higher limit.
                    ui.budget_tree.get_selection().select_path(Gtk.TreePath.new_first())
                    before_id = rows()[0][0]
                    answers.append(("Edit budget", {
                        "scope_kind": "provider", "scope_provider": "openai", "limit": "1k"}, "OK"))
                    ui.budget_edit_button.clicked()
                    result["after_edit"] = rows()
                    result["id_kept"] = rows()[0][0] == before_id
                    result["selection_kept"] = ui._selected_budget_id() == before_id

                    # The tick announces what the first budget owed, once.
                    ui.runtime._budgets_checked_at = 0.0
                    ui._tick()
                    ui.runtime._budgets_checked_at = 0.0
                    ui._tick()
                    result["notices"] = [title for title, _ in notifier.calls]

                    # Notices off, and on again.
                    ui.budget_toggle_button.clicked()
                    result["after_toggle"] = rows()[0][1]
                    ui.budget_toggle_button.clicked()
                    result["after_toggle_back"] = rows()[0][1]

                    # The sessions tab lists the synthetic session.
                    ui.tabs.set_current_page(3)
                    result["sessions"] = [tuple(row) for row in ui.session_store]

                    # Delete asks first; "No" keeps it, "Yes" removes it.
                    ui.tabs.set_current_page(2)
                    answers.append(("Delete budget", None, "NO"))
                    ui.budget_delete_button.clicked()
                    result["after_delete_no"] = len(rows())
                    answers.append(("Delete budget", None, "YES"))
                    ui.budget_delete_button.clicked()
                    result["after_delete_yes"] = len(rows())
                    result["stored_after_delete"] = len(budgets.load_budgets(runtime.store)[0])
                    result["calls_kept"] = runtime.store.totals_since(0).calls
                    ui.quit_button.clicked()
                except BaseException:
                    errors.append(traceback.format_exc())
                    ui.exit_code = 1
                    ui.close()
                return False
            ui.GLib.timeout_add(300, drive)

            def guard():
                if not ui._closed:
                    errors.append("Budget scenario exceeded 15 seconds; dialog: " + repr(
                        ui._active_dialog.get_title() if ui._active_dialog else None))
                    ui.exit_code = 1
                    ui.close()
                return False
            ui.GLib.timeout_add(15000, guard)

        try:
            code = shell.run(ready)
        finally:
            runtime.shutdown()
        result.update({"code": code, "errors": errors, "dialogs": seen,
                       "unanswered": len(answers), "closed": runtime.store.closed})
        if indicator_host.registration is not None:
            assert indicator_host.items, "Native indicator did not register on the test bus"
    output.write_text(json.dumps(result))
    assert code == 0 and not errors, errors


if __name__ == "__main__":
    main()
