"""Web click → Tk queue → Windows dialog → file, without moving the mouse.

Every Win32 message is limited to windows of the test process. SendKeys and
global automation of the user's desktop are not used.
"""

import ctypes
from ctypes import wintypes
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
import subprocess
import sys
import time

import pytest

from tokenmeter.backups import inspect_backup
from tokenmeter.ui.window import CREATE_NO_WINDOW, kill_tree
from tests.test_native_window import kill_after, native_page

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows dialogs")


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def process_tree(pid: int) -> set[int]:
    """`pid` plus every descendant.

    In a Windows venv the interpreter is a child of the `Scripts/python.exe`
    launcher, so a window started with `Popen([sys.executable, ...])` belongs
    to a child of the Popen pid, not to the pid itself.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    parents: dict[int, int] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    tree, grew = {pid}, True
    while grew:
        grew = False
        for child, parent in parents.items():
            if parent in tree and child not in tree:
                tree.add(child)
                grew = True
    return tree


class Windows:
    def __init__(self, pid):
        self.pid = pid
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.api.IsWindowVisible.argtypes = [wintypes.HWND]
        self.api.GetDlgCtrlID.argtypes = [wintypes.HWND]
        self.api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.SendMessageW.restype = wintypes.LPARAM
        self.api.EnumWindows.argtypes = [self.callback, wintypes.LPARAM]
        self.api.EnumChildWindows.argtypes = [wintypes.HWND, self.callback, wintypes.LPARAM]

    def text(self, hwnd, classname=False):
        result = ctypes.create_unicode_buffer(512)
        (self.api.GetClassNameW if classname else self.api.GetWindowTextW)(hwnd, result, len(result))
        return result.value

    def control_text(self, hwnd):
        # GetWindowText cannot read another process's Edit control contents.
        result = ctypes.create_unicode_buffer(32768)
        self.api.SendMessageW(hwnd, 0x000D, len(result), ctypes.addressof(result))  # WM_GETTEXT
        return result.value

    def handles(self, parent=None, all_processes=False):
        found = []

        owners = process_tree(self.pid)
        @self.callback
        def collect(hwnd, _):
            pid = wintypes.DWORD()
            self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if (all_processes or pid.value in owners) and self.api.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        if parent is None:
            self.api.EnumWindows(collect, 0)
        else:
            self.api.EnumChildWindows(parent, collect, 0)
        return found

    def wait_dialog(self, title):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for hwnd in self.handles():
                if self.text(hwnd) == title:
                    return hwnd
            self.page.wait_for_timeout(50)
        visible = [(self.text(h), [self.text(c) for c in self.handles(h)]) for h in self.handles()]
        pytest.fail(f"{title} did not appear: {visible}")

    def wait_file_edit(self, dialog, initial_name=None):
        # Explorer can show the dialog shell before its file-name controls.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            controls = self.handles(dialog)
            edits = [h for h in controls if self.text(h, True) == "Edit"
                     and self.api.GetDlgCtrlID(h) in (1001, 1148, 1152)]
            if initial_name is not None:
                # Explorer can hide the selected file type's extension.
                patterns = (initial_name, str(Path(initial_name).with_suffix("")))
                edits = [h for h in edits if any(fnmatchcase(self.control_text(h), pattern)
                                               for pattern in patterns)]
            if edits:
                # File-name IDs in modern and classic Explorer dialogs.
                return edits[-1]
            self.page.wait_for_timeout(50)
        controls = [(self.text(h, True), self.api.GetDlgCtrlID(h), self.control_text(h))
                    for h in self.handles(dialog)]
        pytest.fail(f"File-name control did not appear within 15 s: {controls}")

    def choose_file(self, title, path, initial_name=None):
        dialog = self.wait_dialog(title)
        edit = self.wait_file_edit(dialog, initial_name)
        # CDP does not generate physical input: another program may keep the focus.
        # What matters is that the dialog does not end up behind the web panel.
        order = self.handles(all_processes=True)
        panels = [h for h in Windows(self.native_pid).handles() if "TokenMeter" in self.text(h)]
        assert panels, "The web window must be visible"
        assert all(order.index(dialog) < order.index(h) for h in panels), f"{title} ended up behind the panel"
        value = ctypes.create_unicode_buffer(str(path.resolve()))
        # WM_SETTEXT can change the visible Edit without updating the modern
        # dialog's selected name. Character messages trigger its normal edit
        # notifications, confined to this test-owned control.
        self.api.SendMessageW(edit, 0x00B1, 0, -1)  # EM_SETSEL: select all
        encoded = value.value.encode("utf-16-le")
        for offset in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[offset:offset + 2], "little")
            self.api.SendMessageW(edit, 0x0102, unit, 1)  # WM_CHAR
        assert self.control_text(edit) == value.value, "The dialog did not accept the requested file name"
        buttons = [h for h in self.handles(dialog) if self.api.GetDlgCtrlID(h) == 1
                   and self.text(h, True) == "Button"]
        assert len(buttons) == 1, "The file dialog must have one confirmation button"
        self.api.PostMessageW(buttons[0], 0x00F5, 0, 0)  # BM_CLICK

    def accept(self, title, yes=False, expected_text=None):
        dialog = self.wait_dialog(title)
        if expected_text is not None:
            contents = [self.control_text(h) for h in self.handles(dialog)]
            assert expected_text in contents, f"Unexpected {title} result: {contents}"
        identifier = 6 if yes else 1  # IDYES / IDOK
        buttons = [h for h in self.handles(dialog) if self.api.GetDlgCtrlID(h) == identifier]
        if not buttons:
            # Tk may use a TaskDialog with its own IDs; identify its button
            # without depending on the translation of "OK".
            buttons = [h for h in self.handles(dialog) if "button" in self.text(h, True).lower()]
            if yes and len(buttons) == 2:
                buttons = buttons[:1]
        assert len(buttons) == 1, [(self.text(h), self.text(h, True), self.api.GetDlgCtrlID(h))
                                   for h in self.handles(dialog)]
        self.api.PostMessageW(buttons[0], 0x00F5, 0, 0)  # BM_CLICK
        deadline = time.monotonic() + 5
        while self.api.IsWindowVisible(dialog):
            assert time.monotonic() < deadline, f"{title} did not close"
            self.page.wait_for_timeout(50)


def click_action(page, name):
    """Wait for the real acknowledgement from the queue, not just the CDP click."""
    network = []
    def on_failed(request):
        if request.url.endswith("/api/action"):
            network.append(f"requestfailed: {request.failure}")
    def on_response(response):
        if response.url.endswith("/api/action"):
            network.append(f"response: {response.status}")
    page.on("requestfailed", on_failed)
    page.on("response", on_response)
    try:
        with page.expect_response(lambda r: r.request.method == "POST" and
                                  r.url.endswith("/api/action") and
                                  r.request.post_data_json.get("action") == name,
                                  timeout=15000) as response:
            page.click(f"[data-action={name}]")
    except Exception as exc:
        details = page.evaluate("""({toast:document.querySelector('#toast').textContent,
            bridge:window.__dialogBridge, active:document.activeElement?.outerHTML})""")
        raise AssertionError(f"{name} was not confirmed: {details}; network: {network}") from exc
    finally:
        page.remove_listener("requestfailed", on_failed)
        page.remove_listener("response", on_response)
    assert response.value.status == 200 and response.value.json() == {"ok": True}


def test_native_export_backup_and_restore_roundtrip(tmp_path):
    pytest.importorskip("playwright.sync_api")
    pytest.importorskip("webview")
    home = tmp_path / "isolated-app"
    home.mkdir()
    log = (home / "app.log").open("w", encoding="utf-8")
    env = dict(os.environ, TOKENMETER_HOME=str(home), PYTHONUTF8="1")
    proc = subprocess.Popen([sys.executable, "-m", "tests.native_dialog_app", str(home)],
                            env=env, stdout=log, stderr=log, creationflags=CREATE_NO_WINDOW)
    guard = kill_after(proc)
    guard.__enter__()
    try:
        deadline = time.monotonic() + 20
        while not (home / "ready.json").exists():
            assert proc.poll() is None, (home / "app.log").read_text(encoding="utf-8")
            assert time.monotonic() < deadline, "The isolated app did not start"
            time.sleep(.05)
        info = json.loads((home / "ready.json").read_text(encoding="utf-8"))
        windows = Windows(info["pid"])
        with native_page(tmp_path, "source", info["url"], info["pid"]) as (page, _native):
            windows.native_pid = _native.pid
            windows.page = page
            page.wait_for_function("document.visibilityState === 'visible'")
            page.evaluate("""() => {
                const original = window.pywebview.api.allow_foreground;
                window.pywebview.api.allow_foreground = (...args) => {
                    window.__dialogBridge = 'waiting';
                    return original(...args).then(value => {window.__dialogBridge = value; return value;});
                };
            }""")
            page.click("#tabSeg button[data-tab=history]")
            for format in ("json", "csv"):
                target = home / f"export.{format}"
                click_action(page, f"export_{format}")
                windows.choose_file("Export usage", target, initial_name=f"tokenmeter-*-30d.{format}")
                windows.accept("Export", expected_text="File saved with the selected filters.")
                assert target.is_file(), (home / "app.log").read_text(encoding="utf-8")
                if format == "json":
                    data = json.loads(target.read_text(encoding="utf-8"))
                    assert data["summary"]["calls"] == 1
                    assert data["events"][0]["input_tokens"] == 12345
                else:
                    assert "12345" in target.read_text(encoding="utf-8-sig")
            page.click("#btnMenu")
            click_action(page, "backup")
            backup = home / "backup.zip"
            windows.choose_file("Save backup", backup, initial_name="TokenMeter-backup.zip")
            windows.accept("Backup", expected_text="Backup created and verified.")
            assert inspect_backup(backup)["calls"] == 1
            (home / "add-event").touch()
            deadline = time.monotonic() + 10
            while not (home / "added-event").exists():
                assert time.monotonic() < deadline
                page.wait_for_timeout(50)
            page.click("#btnMenu")
            click_action(page, "restore")
            windows.choose_file("Select backup", backup)
            windows.accept("Restore backup", yes=True)
            windows.accept("Restore", expected_text="Backup restored. Reopen TokenMeter.")
            assert proc.wait(timeout=15) == 0
            assert _native.wait(timeout=15) == 0
            assert len(list(home.glob("backup-before-restore-*.zip"))) == 1
            previous, = home.glob("backup-before-restore-*.zip")
            assert inspect_backup(previous)["calls"] == 2
            with closing(sqlite3.connect((home / "usage.db").as_uri() + "?mode=ro", uri=True)) as db:
                assert db.execute("SELECT COUNT(*), SUM(input_tokens) FROM usage_events").fetchone() == (1, 12345)
                assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        (home / "stop").touch()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait(timeout=10)
        log.close()
        guard.__exit__(None, None, None)  # fails clearly if the limit fired


def test_file_dialog_waits_for_its_controls(monkeypatch):
    from types import SimpleNamespace

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    def wait(milliseconds):
        clock[0] += milliseconds / 1000
    dialog = Windows.__new__(Windows)
    dialog.page = SimpleNamespace(wait_for_timeout=wait)
    dialog.api = SimpleNamespace(GetDlgCtrlID=lambda h: 1001 if h == 11 else 41477)
    dialog.handles = lambda _parent: [10, 11] if clock[0] >= .1 else [10]
    # An address-bar Edit must not be mistaken for the still-loading file name.
    dialog.text = lambda _handle, _classname: "Edit"
    assert dialog.wait_file_edit(1) == 11
    assert clock[0] >= .1


@pytest.mark.parametrize("initial_value", ["tokenmeter-example-30d.json", "tokenmeter-example-30d"])
def test_file_dialog_waits_for_the_initial_name_before_editing(monkeypatch, initial_value):
    from types import SimpleNamespace

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    def wait(milliseconds):
        clock[0] += milliseconds / 1000
    dialog = Windows.__new__(Windows)
    dialog.page = SimpleNamespace(wait_for_timeout=wait)
    dialog.api = SimpleNamespace(GetDlgCtrlID=lambda _h: 1001)
    dialog.handles = lambda _parent: [11]
    dialog.text = lambda _handle, _classname: "Edit"
    dialog.control_text = lambda _h: initial_value if clock[0] >= .1 else ""
    assert dialog.wait_file_edit(1, "tokenmeter-*-30d.json") == 11
    assert clock[0] >= .1


def test_missing_file_dialog_controls_fail_with_a_deadline(monkeypatch):
    from types import SimpleNamespace

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    def wait(milliseconds):
        clock[0] += milliseconds / 1000
    dialog = Windows.__new__(Windows)
    dialog.page = SimpleNamespace(wait_for_timeout=wait)
    dialog.handles = lambda _parent: []
    with pytest.raises(pytest.fail.Exception, match="within 15 s"):
        dialog.wait_file_edit(1)
    assert 15 <= clock[0] < 15.1


def test_process_tree_includes_children_of_a_venv_launcher():
    """In a Windows venv, `.venv/Scripts/python.exe` is a launcher that starts
    the real interpreter as a child. The window belongs to that child, so
    looking windows up by the Popen pid alone found nothing (reproduced on a
    fresh clone on 2026-10-18)."""
    code = ("import subprocess, sys, time;"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
            "print(c.pid, flush=True); time.sleep(30)")
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                              creationflags=CREATE_NO_WINDOW)
    try:
        child = int(parent.stdout.readline())
        tree = process_tree(parent.pid)
        assert parent.pid in tree and child in tree
    finally:
        kill_tree(parent)
