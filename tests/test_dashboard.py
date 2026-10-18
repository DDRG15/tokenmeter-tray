"""Web window: the JSON the page draws, the server and the launcher.

The `App` is built without Tk (`App.__new__`) with a real `Store`: that way the
state comes from the same logic the Tk panel uses and not from a double that
could diverge from it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from tokenmeter import dashboard
from tokenmeter.app import App
from tokenmeter.config import Config
from tokenmeter.models import UsageEvent
from tokenmeter.session import SessionTracker
from tokenmeter.store import Store
from tokenmeter.ui import webserver, window
from tokenmeter.weekly import WeeklyTracker


def app_without_tk(tmp_path) -> App:
    app = App.__new__(App)
    # Codex in a folder that does not exist: otherwise the result would depend
    # on whether the machine running the tests has ~/.codex.
    app.config = Config(session_budget_tokens=100_000, warn_percent=60, danger_percent=85,
                        codex_root=str(tmp_path / "no-codex"))
    app.store = Store(tmp_path / "usage.db")
    app.session = SessionTracker(app.store, app.config)
    app.weekly = WeeklyTracker(app.store, app.config)
    app.transcripts = None
    app.proxy = None
    app.codex = None
    app.queued = []
    app.enqueue = app.queued.append
    return app


def call(app: App, io: int, key: str, *, model: str = "claude-opus-5",
         ts: float | None = None, complete: bool = True) -> None:
    app.store.add_event(UsageEvent(
        ts=time.time() - 60 if ts is None else ts, provider="anthropic", model=model,
        input_tokens=io, output_tokens=0, source="transcript", dedupe_key=key,
        usage_complete=complete))


# -- state --------------------------------------------------------------------


def test_no_activity_does_not_invent_numbers(tmp_path):
    state = dashboard.build_state(app_without_tk(tmp_path))
    assert state["session"]["percent"] is None
    assert state["session"]["remaining_seconds"] is None
    assert state["session"]["countdown"] == "--:--:--"
    assert state["session"]["level"] == "idle"
    assert state["cost"]["text"] == "—"
    assert state["models"] == []
    assert state["live"] is False
    json.dumps(state)  # serializable as is


def test_percent_level_and_models(tmp_path):
    app = app_without_tk(tmp_path)
    call(app, 50_000, "a")
    call(app, 20_000, "b", model="model-without-price")
    state = dashboard.build_state(app)

    assert state["session"]["percent"] == pytest.approx(70.0)
    assert state["session"]["level"] == "warn"
    assert state["tokens"]["io"] == 70_000
    assert state["live"] is True
    assert sum(m["share"] for m in state["models"]) == pytest.approx(1.0)
    unpriced = next(m for m in state["models"] if m["model"] == "model-without-price")
    assert unpriced["cost"] is None and unpriced["cost_text"] == "—"
    assert state["cost"]["partial"] is True
    assert state["cost"]["text"].endswith("(partial)")


def test_calls_without_usage_raise_a_warning(tmp_path):
    app = app_without_tk(tmp_path)
    call(app, 1_000, "a", complete=False)
    assert "without usage data" in dashboard.build_state(app)["warning"]


@pytest.mark.parametrize("pct,expected", [(10, "ok"), (60, "warn"), (85, "danger"), (140, "danger")])
def test_level_by_thresholds(pct, expected):
    assert dashboard.level(pct, 60, 85) == expected
    assert dashboard.level(pct, 60, 85, active=False) == "idle"


def test_disabled_week_just_says_so(tmp_path):
    app = app_without_tk(tmp_path)
    app.config.weekly_enabled = False
    assert dashboard.build_state(app)["weekly"] == {"enabled": False}


def test_history_keeps_no_record_as_null(tmp_path):
    app = app_without_tk(tmp_path)
    call(app, 1_000, "a", ts=time.time() - 3 * 86400)
    app.store.mark_covered(time.time() - 3 * 86400, time.time(), source="transcript")
    hist = dashboard.build_history(app, 7)
    tokens = [b["tokens"] for b in hist["bars"]]
    assert len(tokens) == 7
    assert tokens[0] is None            # before the first event: unknown
    assert 0 in tokens                  # afterwards, a day without calls is a real zero
    assert hist["bars"][-1]["today"] is True
    assert '"tokens": null' in json.dumps(hist)


def test_history_unknown_range_falls_back_to_30(tmp_path):
    assert dashboard.build_history(app_without_tk(tmp_path), 999)["days"] == 30


# -- server -------------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text('<meta name="tm-token" content="__TM_TOKEN__">',
                                    encoding="utf-8")
    monkeypatch.setattr(webserver, "WEB_DIR", web)
    app = app_without_tk(tmp_path)
    srv = webserver.DashboardServer(app)
    srv.start()
    assert srv.wait_ready(5)
    yield srv, app
    srv.stop()


def fetch(srv, path, *, token=True, host=None, body=None):
    req = urllib.request.Request(srv.url.rstrip("/") + path, data=body,
                                 method="POST" if body is not None else "GET")
    if token:
        req.add_header(webserver.TOKEN_HEADER, srv.token)
    if host:
        req.add_header("Host", host)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_the_page_carries_the_token_and_not_the_placeholder(server):
    srv, _ = server
    status, html = fetch(srv, "/", token=False)
    assert status == 200
    assert srv.token in html and "__TM_TOKEN__" not in html


def test_api_without_a_token_is_rejected(server):
    srv, _ = server
    assert fetch(srv, "/api/state", token=False)[0] == 403
    status, body = fetch(srv, "/api/state")
    assert status == 200
    assert {"session", "tokens", "models", "flags"} <= json.loads(body).keys()


def test_foreign_host_is_rejected(server):
    """DNS rebinding: an external domain that resolves to 127.0.0.1."""
    srv, _ = server
    assert fetch(srv, "/", token=False, host=f"evil.example:{srv.port}")[0] == 403
    assert fetch(srv, "/api/state", host=f"evil.example:{srv.port}")[0] == 403


def test_actions_only_from_the_list_and_through_the_queue(server):
    srv, app = server
    assert fetch(srv, "/api/action", body=b'{"action": "toggle_basis"}')[0] == 200
    assert fetch(srv, "/api/action", body=b'{"action": "quit"}')[0] == 400
    assert fetch(srv, "/api/action", body=b"not json")[0] == 400
    assert app.queued == ["toggle_basis"]


def test_history_over_http(server):
    srv, _ = server
    status, body = fetch(srv, "/api/history?days=7")
    assert status == 200 and len(json.loads(body)["bars"]) == 7
    assert json.loads(fetch(srv, "/api/history?days=abc")[1])["days"] == 30


# -- window launcher --------------------------------------------------------------


def test_child_command_in_development_and_in_the_exe(monkeypatch):
    monkeypatch.delattr(window.sys, "frozen", raising=False)
    command = window.child_command("http://127.0.0.1:1/", 42)
    assert command[1:] == ["-m", "tokenmeter", "--window", "http://127.0.0.1:1/", "--parent-pid", "42"]
    monkeypatch.setattr(window.sys, "frozen", True, raising=False)
    assert window.child_command("u", 1)[1:] == ["--window", "u", "--parent-pid", "1"]


def test_without_pywebview_the_child_exits_with_its_code(monkeypatch):
    monkeypatch.setitem(window.sys.modules, "webview", None)
    assert window.run_window("http://x/") == window.EXIT_NO_WEBVIEW


def test_if_the_child_fails_the_fallback_is_used(tmp_path, monkeypatch):
    launcher = window.WindowLauncher(tmp_path)
    used = []
    monkeypatch.setattr(launcher, "_fallback", used.append)

    class Child:
        def __init__(self, code):
            self.code = code

        def wait(self):
            return self.code

    launcher._proc = Child(window.EXIT_NO_WEBVIEW)
    launcher._watch_start(launcher._proc, "http://u/")
    launcher._proc = Child(0)
    launcher._watch_start(launcher._proc, "http://v/")  # normal close: nothing to do
    assert used == ["http://u/"]


def test_the_cli_diverts_window_to_the_child(monkeypatch):
    import tokenmeter.__main__ as cli

    received = {}
    monkeypatch.setattr(window, "run_window",
                        lambda url, parent: received.update(url=url, parent=parent) or 0)
    assert cli.main(["--window", "http://127.0.0.1:5/", "--parent-pid", "7"]) == 0
    assert received == {"url": "http://127.0.0.1:5/", "parent": 7}


def test_weekly_reset_shows_a_fixed_english_weekday(tmp_path):
    """`%a` depended on the locale and showed the weekday in the system's language."""
    from datetime import datetime

    app = app_without_tk(tmp_path)
    # Fixed `now`: with the real clock the test expired at 22:11 on that day.
    before = datetime(2026, 10, 15, 20, 0).timestamp()
    app.weekly.set_reset_at(datetime(2026, 10, 15, 22, 11).timestamp())
    assert app.weekly.snapshot(before).reset_at_text() == "Thu 15/10 22:11"


def test_closing_the_window_walks_down_the_process_tree(tmp_path):
    """A one-file .exe is two processes; terminate() left the grandchild alive."""
    import subprocess
    import sys

    grandchild_pid = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time, pathlib;"
        "n = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"pathlib.Path(r'{grandchild_pid}').write_text(str(n.pid));"
        "time.sleep(60)"
    )
    child = subprocess.Popen([sys.executable, "-c", code])
    for _ in range(100):
        if grandchild_pid.exists() and grandchild_pid.read_text():
            break
        time.sleep(0.05)
    pid = int(grandchild_pid.read_text())

    window.kill_tree(child)

    assert child.wait(timeout=5) is not None
    # The grandchild too: if it were still alive, `wait_for_process` would not return.
    watched = []
    t = __import__("threading").Thread(
        target=lambda: watched.append(window.wait_for_process(pid)), daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()


def test_the_window_api_does_not_expose_the_window_to_js():
    """pywebview walks the public attributes: with the window inside it overflowed the stack."""
    api = window._Api()
    public = {n for n in vars(api) if not n.startswith("_")}
    assert public == set()
    assert {"minimize", "close", "allow_foreground"} <= set(dir(api))


def test_only_collectors_that_can_see_usage_are_required(tmp_path):
    """Requiring a proxy without traffic or a Codex without a folder would make any 0 impossible."""
    app = app_without_tk(tmp_path)
    assert dashboard.history_sources(app.config, store=app.store) == ("transcript",)
    (tmp_path / "no-codex").mkdir()
    app.store.set_state("proxy_last_request_ts", repr(time.time()))
    assert dashboard.history_sources(app.config, store=app.store) == (
        "transcript", "codex", "proxy")
    assert dashboard.history_sources(app.config, "openai", app.store) == ("codex", "proxy")
