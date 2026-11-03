# Linux development and verification

The original prototype is now the native Linux shell. User installation, XDG
paths and available features are documented in [LINUX.md](LINUX.md). The tested
desktop is Mint 22.3 Cinnamon/X11 with Python 3.12; Wayland/macOS are unverified.
Windows retains its existing UI and release pipeline.

## Architecture

`RuntimeState` supplies the same observers to Windows App and Linux Runtime.
`Runtime` owns Linux collectors, action queue and SQLite; it closes workers
before the database and cleans partial starts. `LinuxShell` owns a single
GTK/GLib loop with direct Ayatana indicator bindings. All widget/dialog work
runs on its main thread. Closing the window, Quit, SIGTERM or a shutdown request
ends the process and collectors. Temporary tray images are removed on exit.
There is no Linux WebView, HTTP dashboard child, browser or system service.

The Linux UI includes filters, history/table/chart, CSV/JSON export, backup and
restore, manual budget, session/weekly calibration, cache basis, cost visibility,
alerts and session reset. It uses shared parsing, history, calibration and
protection logic. Windows lifecycle is still owned by App rather than Runtime;
Windows release checks remain necessary after changing shared code.

Desktop notifications use only an already-running notification service with
D-Bus NO_AUTO_START and recall their own IDs on exit. Tests inject a notifier
instead of showing notifications on the real desktop.

## Isolated tests

Disposable environments, wheels, caches, profiles and data belong under a
unique task folder in `~/sandbox/`. Keep durable code/tests in the repository.
Use distribution Python and preinstalled GTK 3, PyGObject and Ayatana typelib.
The helper exposes only the system GI binding in a matching-version closed
venv; it does not inherit unrelated global packages or install system software.

From the source checkout, choose a fresh task directory:

```bash
mkdir -p "$HOME/sandbox"
TASK_DIR=$(mktemp -d "$HOME/sandbox/tokenmeter-linux.XXXXXX")
mkdir -p "$TASK_DIR/tmp"
/usr/bin/python3 -m venv "$TASK_DIR/venv"
/usr/bin/python3 scripts/expose_system_gi.py --venv "$TASK_DIR/venv"
TMPDIR="$TASK_DIR/tmp" "$TASK_DIR/venv/bin/python" -m pip install --cache-dir "$TASK_DIR/cache" -r requirements-linux-dev.txt
"$TASK_DIR/venv/bin/python" -m pip download --cache-dir "$TASK_DIR/cache" -d "$TASK_DIR/wheels" -r requirements-linux.txt
TMPDIR="$TASK_DIR/tmp" PLAYWRIGHT_BROWSERS_PATH="$TASK_DIR/browsers" \
  PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000 \
  "$TASK_DIR/venv/bin/python" -m playwright install chromium
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  TMPDIR="$TASK_DIR/tmp" TOKENMETER_HOME="$TASK_DIR/default-data" \
  PLAYWRIGHT_BROWSERS_PATH="$TASK_DIR/browsers" \
  TOKENMETER_WHEELHOUSE="$TASK_DIR/wheels" GSETTINGS_BACKEND=memory \
  GIO_USE_VFS=local NO_AT_BRIDGE=1 \
  dbus-run-session --config-file tests/fixtures/dbus-session.conf -- \
  "$TASK_DIR/venv/bin/python" -m pytest tests -q -p no:cacheprovider \
  --basetemp="$TASK_DIR/pytest-run"
```

Create `~/sandbox` first if absent. A real Linux DISPLAY is required for native
controls; those cases skip without one. Browser tests use Playwright's pinned
Chromium and verify the dashboard plus cross-origin proxy protection. To use an
already-installed Chrome instead, omit the browser download and set
`TOKENMETER_TEST_BROWSER_CHANNEL=chrome` for pytest. A browser launch error fails
the tests rather than skipping them. CI installs Chromium and its system
dependencies on its disposable runner. Local native GTK prerequisites remain
unchanged; Playwright does not affect the desktop runtime requirements.

Windows UI/Tk/WebView2 and packaged exe cases do not validate the Linux GTK app;
they remain outside this platform's scope. The optional MCP server is tested
in a separate environment with its own pinned dependencies; see [MCP.md](MCP.md).

The private test bus disables desktop service activation. Native fixtures
provide a synthetic StatusNotifier host, exercising indicator registration
without falling back to the real X11 panel. Dialog automation waits for mapping
and retries asynchronous file selection before accepting a chooser.

Tests use synthetic sources and explicit collector paths, never personal DBs
or credentials. TOKENMETER_HOME alone does not isolate default source roots or
authentication: use the supplied fixtures, or disable every collector when
manually trying an empty UI. The fixture helpers terminate only their own
process groups and verify store/worker closure and free proxy ports.

Native tests exercise filters, unknown coverage, charts, settings/calibration,
alerts, real file dialogs, exports/backup roundtrip, protected destinations,
cancellation, invalid input, restore failures, SIGTERM and orderly shutdown.
Installer tests need TOKENMETER_WHEELHOUSE for offline dependencies and
`desktop-file-validate` (desktop-file-utils). They stage program/launchers inside
the test directory, verify update rollback, preserve user data on uninstall and
refuse removal of unique files. Distribution tests compare deterministic archives.

The Linux CI job uses private D-Bus/Xvfb, preserves failure diagnostics and its
source archive, then removes its sandbox. It supplements Cinnamon validation.
After local tests, verify child exit/port release, summarize useful results and
remove only that task's environment/cache/data. Preserve any uncertain unique
work for review; never clean other sessions or desktop processes.
