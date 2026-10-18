"""The native window must render, confirm data and close without reopening.

CDP is enabled only in the test process. To test the exe as well:
TOKENMETER_TEST_EXE=dist/TokenMeter.exe python -m pytest tests/test_native_window.py
"""

import json
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from tokenmeter.ui import window, webserver
from tests.test_dashboard import app_without_tk, call


def test_cdp_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TOKENMETER_TEST_CDP_PORT", raising=False)
    assert window._test_cdp_port() is None


@pytest.mark.parametrize("port", ["1", "9222", "65535"])
def test_cdp_port_accepts_valid_ports(monkeypatch, port):
    monkeypatch.setenv("TOKENMETER_TEST_CDP_PORT", port)
    assert window._test_cdp_port() == int(port)


@pytest.mark.parametrize("port", ["", "0", "65536", "-1", "9222 --extra-flag", "９２２２"])
def test_cdp_port_rejects_invalid_values(monkeypatch, port):
    monkeypatch.setenv("TOKENMETER_TEST_CDP_PORT", port)
    with pytest.raises(ValueError, match="must be a port"):
        window._test_cdp_port()


def test_ready_shows_window_once_after_first_render():
    class Ops:
        calls = 0
        def show_not_topmost(self):
            self.calls += 1
            return True
    api = window._Api()
    api._ops = Ops()
    assert not api._wait_until_ready(0)
    api.report_ready()
    api.report_ready()
    assert api._ops.calls == 1
    assert api._wait_until_ready(0)
    assert not {k for k in vars(api) if not k.startswith("_")}


def test_ready_before_the_native_window_exists_is_retried_not_lost():
    class Ops:
        def show_not_topmost(self):
            return False
    api = window._Api()
    api._ops = Ops()
    with pytest.raises(RuntimeError):
        api.report_ready()  # the page catches the rejection and retries
    assert not api._wait_until_ready(0)


def test_stalled_native_start_requests_fallback_but_closed_window_does_not(monkeypatch):
    api = window._Api()
    exited = []
    monkeypatch.setattr(window, "NATIVE_READY_TIMEOUT_S", 0)
    monkeypatch.setattr(window.os, "_exit", exited.append)
    closed = threading.Event()
    window._watch_native_ready(api, closed)
    assert exited == [window.EXIT_NO_WEBVIEW]
    closed.set()
    window._watch_native_ready(api, closed)
    assert exited == [window.EXIT_NO_WEBVIEW]
    closed.clear()
    api._ready.set()
    window._watch_native_ready(api, closed)
    assert exited == [window.EXIT_NO_WEBVIEW]


def test_watcher_handles_late_failure_and_ignores_shutdown_or_stale_child(tmp_path, monkeypatch):
    launcher = window.WindowLauncher(tmp_path)
    used = []
    monkeypatch.setattr(launcher, "_fallback", used.append)
    class Child:
        def wait(self):
            return window.EXIT_NO_WEBVIEW
    child = Child()
    launcher._proc = child
    launcher._watch_start(child, "http://local/one")
    assert used == ["http://local/one"]
    launcher._closing = True
    launcher._watch_start(child, "http://local/two")
    launcher._closing = False
    launcher._proc = Child()
    launcher._watch_start(child, "http://local/three")
    assert used == ["http://local/one"]


# A native test that outlives this limit has its processes killed: the test
# then fails with a clear error instead of hanging the whole suite (a GIL
# deadlock once froze the window forever; pytest.ini adds a global limit on top of this one).
NATIVE_TEST_LIMIT_S = 120


@contextmanager
def kill_after(proc, seconds=NATIVE_TEST_LIMIT_S):
    fired = threading.Event()

    def fire():
        fired.set()
        window.kill_tree(proc)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    try:
        yield fired
    finally:
        timer.cancel()
    assert not fired.is_set(), f"native test exceeded {seconds} s; its processes were killed"


@contextmanager
def native_page(tmp_path, mode, url, parent_pid):
    """Real WebView2 with an isolated profile; closes only the child created here."""
    if sys.platform != "win32":
        pytest.skip("WebView2 is Windows-only")
    playwright = pytest.importorskip("playwright.sync_api")
    pytest.importorskip("webview")
    executable = os.environ.get("TOKENMETER_TEST_EXE")
    if mode == "exe" and not executable:
        pytest.skip("Run after the build with TOKENMETER_TEST_EXE")
    if executable and mode == "exe":
        assert Path(executable).is_file(), "The requested executable does not exist"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, TOKENMETER_HOME=str(tmp_path),
               TOKENMETER_TEST_CDP_PORT=str(port),
               WEBVIEW2_USER_DATA_FOLDER=str(tmp_path / "webview-profile"))
    command = ([str(Path(executable).resolve())] if mode == "exe" else
               [sys.executable, "-m", "tokenmeter"])
    command += ["--window", url, "--parent-pid", str(parent_pid)]
    logfile = (tmp_path / "native-child.log").open("w", encoding="utf-8")
    proc = None
    browser = None
    guard = None
    try:
        proc = subprocess.Popen(command, env=env, stdout=logfile, stderr=logfile,
                                creationflags=window.CREATE_NO_WINDOW)
        guard = kill_after(proc)
        guard.__enter__()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert proc.poll() is None, "The window exited before loading"
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=.3) as response:
                    if json.load(response):
                        break
            except OSError:
                pass
            time.sleep(.1)
        else:
            pytest.fail("WebView2 did not open the diagnostics channel in time")
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
            page = browser.contexts[0].pages[0]
            page.wait_for_function(
                "document.querySelector('#app') && !document.querySelector('#app').classList.contains('loading')"
                " && typeof window.pywebview?.api?.report_ready === 'function'", timeout=20000)
            yield page, proc
            if proc.poll() is None:
                page.evaluate("setTimeout(() => window.pywebview.api.close(), 50); null")
                assert proc.wait(timeout=15) == 0
            browser.close()
            browser = None
    finally:
        if proc is not None and proc.poll() is None:
            window.kill_tree(proc)
            proc.wait(timeout=10)
        logfile.close()
        if guard is not None:
            guard.__exit__(None, None, None)  # fails clearly if the limit fired


@pytest.mark.parametrize("mode", ["source", "exe"])
def test_native_webview_renders_real_page_and_accepts_clicks(tmp_path, mode):
    app = app_without_tk(tmp_path)
    call(app, 12345, "req:native-window")
    server = webserver.DashboardServer(app)
    server.start()
    try:
        assert server.wait_ready(10)
        with native_page(tmp_path, mode, server.url, os.getpid()) as (page, _proc):
            assert page.title() == window.TITLE
            # The number animates from zero: wait for its final value, not an intermediate frame.
            page.wait_for_function("document.querySelector('#used').textContent === '12,345'", timeout=5000)
            page.click("#tabSeg button[data-tab=history]")
            page.wait_for_selector("#panel-history.active")
            page.click("[data-action=export_json]")
            page.wait_for_function("document.querySelector('#toast').textContent.includes('JSON')")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not app.queued:
                page.wait_for_timeout(50)
            assert app.queued[-1]["action"] == "export_json"
    finally:
        server.stop()
        app.store.close()


def test_repeated_open_while_hidden_does_not_spawn_another_window(tmp_path, monkeypatch):
    launcher = window.WindowLauncher(tmp_path)
    class Child:
        def poll(self):
            return None
    launcher._proc = Child()
    monkeypatch.setattr(window, "bring_to_front", lambda: False)
    monkeypatch.setattr(window.subprocess, "Popen", lambda *_a, **_k: pytest.fail("Do not duplicate the child that is loading"))
    launcher.open("http://local/")


def test_window_operations_never_block_on_the_ui_thread():
    """`report_ready` used to set `window.on_top` from a js_api thread. pywebview
    applies it with a synchronous cross-thread call that keeps the GIL while the
    UI thread waits for the GIL to run pywebview's per-request hook: deadlock,
    and the window froze in "No connection" (reproduced with thread dumps).
    Window operations must go through asynchronous Win32 calls instead."""
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"pywebview window touched from a js_api thread: {name}")

        def __setattr__(self, name, value):
            raise AssertionError(f"pywebview window touched from a js_api thread: {name}")

    calls = []

    class Ops:
        def show_not_topmost(self):
            calls.append("show")
            return True

        def minimize(self):
            calls.append("minimize")

        def close(self):
            calls.append("close")

    api = window._Api()
    api._window = Forbidden()
    api._ops = Ops()
    api.report_ready()
    api.report_ready()
    api.minimize()
    api.close()
    assert calls == ["show", "minimize", "close"]
    assert api._wait_until_ready(0)


def test_per_request_hook_is_removed_from_pinned_pywebview():
    """The hook that made every network request need the GIL is dropped right
    after WebView2 initialises. Pinned to the pywebview version it was written for."""
    import importlib.metadata as md

    from webview.platforms import edgechromium

    assert md.version("pywebview") == "6.2.1"
    window.install_request_hook_removal()
    assert getattr(edgechromium.EdgeChrome.on_webview_ready, "_tokenmeter_patched", False)


def test_kill_after_kills_a_hung_process_and_fails_clearly():
    hung = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            creationflags=window.CREATE_NO_WINDOW)
    try:
        with pytest.raises(AssertionError, match="exceeded 1 s"):
            with kill_after(hung, seconds=1):
                hung.wait(timeout=20)
        assert hung.poll() is not None
    finally:
        window.kill_tree(hung)
