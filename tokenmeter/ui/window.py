"""Frameless window for the web interface.

pywebview needs its process's main thread and in TokenMeter that thread belongs
to Tk. That is why the window lives in a **child process**: the same executable
with `--window URL`. The parent only launches it, brings it to the front and
closes it on exit.

If pywebview does not start (missing package or WebView2 runtime), Edge is
opened in app mode, and if that does not exist either, the default browser. What
never happens is being left without an interface.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

log = logging.getLogger(__name__)

TITLE = "TokenMeter — dashboard"
WIDTH, HEIGHT = 440, 780
MIN_SIZE = (380, 560)
MARGIN = 12
BACKGROUND = "#0b0d12"

# Exit code of the child when it cannot open pywebview: the parent uses it to
# switch to the fallback without waiting for the user to notice nothing showed up.
EXIT_NO_WEBVIEW = 3
NATIVE_READY_TIMEOUT_S = 20.0

CREATE_NO_WINDOW = 0x08000000
EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def child_command(url: str, parent_pid: int) -> list[str]:
    """The child's command line, both in the `.exe` and in development."""
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        base = [sys.executable, "-m", "tokenmeter"]
    return [*base, "--window", url, "--parent-pid", str(parent_pid)]


def find_edge() -> str | None:
    for path in EDGE_PATHS:
        if Path(path).is_file():
            return path
    return None


def anchored_geometry(area: tuple[int, int, int, int] | None = None,
                      width: int = WIDTH, height: int = HEIGHT) -> tuple[int, int, int, int] | None:
    """`(x, y, width, height)` at the bottom-right corner, next to the tray.

    The height is clipped to the work area: on a 768 px screen the 780 px design
    height does not fit and the header, which is the only thing that lets you
    drag and close a frameless window, would end up off screen.
    """
    if area is None:
        try:
            from .panel import work_area

            area = work_area()
        except Exception:
            return None
    left, top, right, bottom = area
    if not (right and bottom):
        return None
    height = max(MIN_SIZE[1], min(height, bottom - top - 2 * MARGIN))
    return (max(left, right - width - MARGIN), max(top, bottom - height - MARGIN), width, height)


def bring_to_front(title: str = TITLE) -> bool:
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


class WindowLauncher:
    """Parent side: a single window process at a time."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._proc: subprocess.Popen | None = None
        self._edge: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._closing = False

    @property
    def is_open(self) -> bool:
        return any(p is not None and p.poll() is None for p in (self._proc, self._edge))

    def open(self, url: str) -> None:
        with self._lock:
            self._closing = False
            if self.is_open:
                bring_to_front()
                return
            try:
                self._proc = subprocess.Popen(
                    child_command(url, os.getpid()),
                    creationflags=CREATE_NO_WINDOW,
                    close_fds=True,
                )
            except OSError:
                log.exception("could not launch the window")
                self._fallback(url)
                return
        threading.Thread(target=self._watch_start, args=(self._proc, url),
                         name="window", daemon=True).start()

    def _watch_start(self, proc: subprocess.Popen, url: str) -> None:
        # Staying alive for four seconds does not prove WebView2 has loaded. The
        # child exits with EXIT_NO_WEBVIEW if it does not get the page's
        # confirmation.
        code = proc.wait()
        with self._lock:
            if self._closing or self._proc is not proc:
                return  # deliberate close or a later window
            if code != 0:
                log.warning("the window exited with code %s; using the fallback", code)
                self._fallback(url)

    def _fallback(self, url: str) -> None:
        edge = find_edge()
        if edge:
            # Own profile: that way the process does not merge with the user's
            # Edge and can be closed on exit without touching their tabs.
            profile = self._data_dir / "edge-app"
            try:
                self._edge = subprocess.Popen(
                    [edge, f"--app={url}", f"--window-size={WIDTH},{HEIGHT}",
                     f"--user-data-dir={profile}", "--no-first-run"],
                    creationflags=CREATE_NO_WINDOW,
                )
                return
            except OSError:
                log.exception("could not open Edge in app mode")
        webbrowser.open(url)

    def close(self) -> None:
        with self._lock:
            self._closing = True
            processes = self._proc, self._edge
            self._proc = self._edge = None
        for proc in processes:
            if proc is not None and proc.poll() is None:
                kill_tree(proc)


def kill_tree(proc: subprocess.Popen) -> None:
    """Closes the process and its children.

    A one-file PyInstaller `.exe` is **two** processes: the launcher and the
    real Python. `terminate()` only kills the launcher and the window stayed
    orphaned, showing "No connection", until Windows declared it hung.
    `taskkill /T` walks down the tree.
    """
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       creationflags=CREATE_NO_WINDOW, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        log.debug("taskkill failed; trying terminate()", exc_info=True)
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            log.debug("could not close the window", exc_info=True)


# -- child side ---------------------------------------------------------------


class _Api:
    """What the page can ask of the window (`window.pywebview.api`).

    pywebview exposes **all** public attributes of this object to JS and walks
    them. With the window in `self.window` it descended through WinForms native
    objects (`native.AccessibilityObject.Bounds.Empty.Empty…`) until the stack
    ran out. The underscore keeps it out.
    """

    def __init__(self, parent_pid: int | None = None) -> None:
        self._window = None
        self._ready = threading.Event()
        self._parent_pid = parent_pid
        self._ops = _Win32Window(os.getpid())

    def report_ready(self) -> None:
        """Only a page with rendered state can show the window.

        It does not touch the pywebview window: its properties are applied with
        a synchronous call into the UI thread that holds the GIL. If that thread
        was waiting for the GIL, the window froze forever. Operations go through
        asynchronous Win32 instead.
        """
        if self._ready.is_set():
            return
        if not self._ops.show_not_topmost():
            # No window yet: the page retries on the next render.
            raise RuntimeError("native window not available yet")
        self._ready.set()

    def _wait_until_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def minimize(self) -> None:
        self._ops.minimize()

    def close(self) -> None:
        self._ops.close()

    def allow_foreground(self) -> bool:
        """Lets TokenMeter bring its dialogs to the front.

        The dialogs (calibrate, backup, export) are opened by the main process,
        but the click happens in this window, which is another process. Windows
        only lets the process that has the foreground, or one it authorizes,
        take it: without this, the dialog was born behind the window. It is
        called right at the click, while this window has the focus.
        """
        try:
            return bool(self._parent_pid and ctypes.windll.user32.AllowSetForegroundWindow(self._parent_pid))
        except Exception:
            return False


class _Win32Window:
    """Operations on the native window without waiting for the UI thread.

    ctypes releases the GIL during the call, and these functions only enqueue:
    `ShowWindowAsync`, `SetWindowPos` with `SWP_ASYNCWINDOWPOS` and
    `PostMessage`. None of them can end up waiting on a thread that is waiting
    for the GIL.
    """

    SW_SHOW, SW_MINIMIZE = 5, 6
    HWND_NOTOPMOST = -2
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x1, 0x2, 0x10, 0x4000
    WM_CLOSE = 0x0010

    def __init__(self, pid: int) -> None:
        self._pid = pid

    def hwnd(self) -> int | None:
        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            found: list[int] = []
            proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def collect(handle, _):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                if pid.value == self._pid:
                    buffer = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(handle, buffer, 256)
                    if buffer.value == TITLE:
                        found.append(handle)
                        return False
                return True

            user32.EnumWindows(proto(collect), 0)
            return found[0] if found else None
        except Exception:
            return None

    def show_not_topmost(self) -> bool:
        handle = self.hwnd()
        if not handle:
            return False
        user32 = ctypes.windll.user32
        user32.ShowWindowAsync(handle, self.SW_SHOW)
        user32.SetWindowPos(handle, self.HWND_NOTOPMOST, 0, 0, 0, 0,
                            self.SWP_NOSIZE | self.SWP_NOMOVE | self.SWP_NOACTIVATE
                            | self.SWP_ASYNCWINDOWPOS)
        return True

    def minimize(self) -> None:
        handle = self.hwnd()
        if handle:
            ctypes.windll.user32.ShowWindowAsync(handle, self.SW_MINIMIZE)

    def close(self) -> None:
        handle = self.hwnd()
        if handle:
            ctypes.windll.user32.PostMessageW(handle, self.WM_CLOSE, 0, 0)


def install_request_hook_removal() -> None:
    """Removes pywebview's network hook as soon as WebView2 starts.

    pywebview 6.2.1 registers `AddWebResourceRequestedFilter('*')`: every page
    request (including the one-second polling) stops on the UI thread until a
    Python handler runs, which needs the GIL. Together with any synchronous call
    from another thread that holds the GIL, that froze the window. TokenMeter
    does not use the `request_sent`/`response_received` events that hook feeds.

    It is removed on the same UI thread, right after pywebview sets it and
    before the first request. Tied to the version pinned in requirements.txt: a
    test checks it.
    """
    from webview.platforms import edgechromium

    original = edgechromium.EdgeChrome.on_webview_ready
    if getattr(original, "_tokenmeter_patched", False):
        return

    def on_webview_ready(self, sender, args):
        original(self, sender, args)
        try:
            sender.CoreWebView2.RemoveWebResourceRequestedFilter(
                "*", edgechromium.CoreWebView2WebResourceContext.All)
        except Exception:
            log.warning("could not remove pywebview's network hook", exc_info=True)

    on_webview_ready._tokenmeter_patched = True
    edgechromium.EdgeChrome.on_webview_ready = on_webview_ready


def wait_for_process(pid: int) -> bool:
    """Blocks until `pid` ends. False if it could not be watched."""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def _exit_with_parent(parent_pid: int) -> None:
    """Exits if the TokenMeter process dies without warning.

    `window.destroy()` from this thread left the WinForms loop waiting and
    Windows showed "TokenMeter.exe is not responding". Without the parent there
    is no server and no data: nothing is left to close carefully.
    """
    if wait_for_process(parent_pid):
        os._exit(0)


def _watch_native_ready(api: _Api, closed: threading.Event) -> None:
    if not api._wait_until_ready(NATIVE_READY_TIMEOUT_S) and not closed.is_set():
        log.error("WebView2 did not confirm the dashboard within %.0f s; opening the fallback", NATIVE_READY_TIMEOUT_S)
        # A blocked backend can ignore destroy(): exiting lets the parent open
        # Edge and also removes PyInstaller's bootstrap process.
        os._exit(EXIT_NO_WEBVIEW)


def _test_cdp_port() -> int | None:
    """Opt-in diagnostics for native tests; never enabled by default."""
    raw = os.environ.get("TOKENMETER_TEST_CDP_PORT")
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdecimal() or not 1 <= len(raw) <= 5:
        raise ValueError("TOKENMETER_TEST_CDP_PORT must be a port from 1 to 65535")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError("TOKENMETER_TEST_CDP_PORT must be a port from 1 to 65535")
    return port


def run_window(url: str, parent_pid: int | None = None) -> int:
    try:
        import webview
    except ImportError:
        return EXIT_NO_WEBVIEW

    # Elevated WebView2 hosts ignore WEBVIEW2_* environment overrides. Pass the
    # test-only diagnostics port through pywebview's creation-properties API.
    port = _test_cdp_port()
    if port is not None:
        webview.settings["REMOTE_DEBUGGING_PORT"] = port
    install_request_hook_removal()
    api = _Api(parent_pid)
    geo = anchored_geometry()
    x, y, width, height = geo if geo else (None, None, WIDTH, HEIGHT)
    window = webview.create_window(
        TITLE, url, js_api=api,
        width=width, height=height, x=x, y=y,
        min_size=MIN_SIZE, resizable=True,
        frameless=True, easy_drag=False,
        background_color=BACKGROUND, text_select=False,
        # Windows does not let a freshly launched process steal the focus: the
        # window would appear behind whatever the user has open. It opens on
        # top and is released as soon as it loads, so it does not stay pinned.
        on_top=True, hidden=True,
    )
    api._window = window  # noqa: SLF001
    closed = threading.Event()
    window.events.closed += closed.set
    if parent_pid:
        threading.Thread(target=_exit_with_parent, args=(parent_pid,),
                         name="parent", daemon=True).start()
    try:
        threading.Thread(target=_watch_native_ready, args=(api, closed),
                         name="window-ready", daemon=True).start()
        webview.start(gui="edgechromium", private_mode=True)
    except Exception:
        logging.getLogger(__name__).exception("pywebview could not start")
        return EXIT_NO_WEBVIEW
    return 0
