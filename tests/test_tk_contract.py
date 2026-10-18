"""The real Tk windows must respect the same contract as the dashboard."""

import time
import tkinter as tk

import pytest

from tests.test_dashboard import app_without_tk, call
from tokenmeter.models import UsageEvent
from tokenmeter.ui.history import HistoryWindow
from tokenmeter.ui.panel import Panel


@pytest.fixture
def views(tmp_path):
    root = tk.Tk()
    root.withdraw()
    app = app_without_tk(tmp_path)
    app.config.weekly_enabled = False
    panel = Panel(root, app)
    history = HistoryWindow(root, app)
    try:
        panel._build()
        history._build()
        history._win.withdraw()
        yield root, app, panel, history
    finally:
        root.destroy()
        app.store.close()


def test_tk_panel_keeps_general_tokens_without_a_claude_session(views):
    root, app, panel, _history = views
    app.store.add_event(UsageEvent(ts=time.time() - 30, provider="openai", model="unknown-test",
        input_tokens=1234, output_tokens=56, source="codex", dedupe_key="req:other",
        billing="api", usage_complete=False))
    panel.refresh()
    root.update_idletasks()
    assert panel._vars["percent"].get() == "—"
    assert panel._vars["input"].get() == "1,234"
    assert panel._vars["output"].get() == "56"
    assert panel._vars["io"].get() == "1,290"
    assert panel._vars["calls"].get() == "1"
    assert "OBSERVED USAGE" in panel._tokens_header.cget("text")
    assert "without usage data" in panel._vars["warning"].get()
    assert panel._vars["cost"].get() == "—"
    assert any(w.cget("text") == "unknown-test" for row in panel._model_rows for w in row)
    call(app, 3000, "req:claude")
    panel.refresh()
    assert panel._vars["percent"].get() == "3%"
    assert panel._vars["io"].get() == "4,290"
    assert panel._vars["calls"].get() == "2"


def test_tk_panel_hides_and_restores_all_cost_content(views):
    root, app, panel, _history = views
    call(app, 12345, "req:cost")
    for visible in (False, True, False, True):
        app.config.show_cost = visible
        panel.refresh()
        root.update_idletasks()
        assert panel._vars["io"].get() == "12,345"
        assert panel._cost_box.winfo_manager() == ("pack" if visible else "")
        if visible:
            assert "$" in panel._vars["cost"].get()
            assert panel._vars["cost_note"].get()
            siblings = panel._cost_box.master.pack_slaves()
            assert siblings.index(panel._cost_box) < siblings.index(panel._models_header)
        else:
            assert panel._vars["cost"].get() == panel._vars["cost_note"].get() == ""


def test_tk_history_hides_model_costs_and_explains_partial_coverage(views):
    root, app, _panel, history = views
    call(app, 12345, "req:history")
    for visible in (False, True, False):
        app.config.show_cost = visible
        history.refresh()
        root.update_idletasks()
        assert "partial coverage" in history._note.get()
        assert "12,345 tokens" in history._summary.get()
        details = [w.cget("text") for w in history._model_rows]
        assert any("12,345 tok" in text for text in details)
        assert any(" · API " in text for text in details) == visible
        if not visible:
            assert all("$" not in text for text in details)
