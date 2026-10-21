# Contributing to TokenMeter

## Development setup

Use Windows 10/11, Python 3.12, and Microsoft Edge/WebView2. Linux and macOS are
planned platforms; the native UI and installer currently require Windows.
Start in the project root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pip check
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
$env:PYTHONUTF8 = "1"
python -m pytest tests -q -p no:cacheprovider
```

Both requirements files pin direct and transitive dependencies. Runtime packages
belong in `requirements.txt`; test/build tools belong in `requirements-dev.txt`,
which includes the runtime file. Validate dependency changes in a fresh virtual
environment. Do not remove pins to resolve an installation failure.

## Code layout

| Location | Responsibility |
|---|---|
| `tokenmeter/__main__.py`, `app.py` | CLI, application lifecycle and Tk action queue |
| `tokenmeter/collectors/` | Proxy, transcript and Codex collection and parsing |
| `tokenmeter/models.py`, `store.py` | Usage records, persistence and deduplication |
| `session.py`, `weekly.py`, `calibration.py` | Session windows, weekly cycles and calibration |
| `billing.py`, `pricing.py` | Billing classification and dated API-equivalent estimates |
| `dashboard.py`, `history.py`, `exports.py` | Shared display data, history and exports |
| `tokenmeter/ui/` | Tray, Tk views, local dashboard server and WebView2 child |
| `backups.py`, `instance.py`, `files.py` | Validated backups, instance locking and file handling |
| `tests/`, `tests/fixtures/` | Automated checks and synthetic provider examples |
| `tokenmeter.spec`, `install.ps1` | Executable packaging, installation and rollback |

Python module names without a directory prefix are inside tokenmeter/.
The dashboard window runs in a separate process so WebView2 and Tk each own
their required UI thread. Keep UI actions queued onto Tk's thread and avoid
synchronous window operations across threads.

## Tests and native diagnostics

Run focused tests while editing, then the source suite before publishing code
changes. `tests/test_install_instructions.py` checks the README/dependency/CI
installation contract. Tests use temporary data and local fake upstreams;
provider credentials are not needed. Keep protocol fixtures synthetic; see
[the fixture guide](tests/fixtures/README.md).

The source suite skips the packaged checks until `TOKENMETER_TEST_EXE` is set.
Build and run those checks explicitly:

```powershell
python -m PyInstaller tokenmeter.spec --noconfirm
$env:TOKENMETER_TEST_EXE = (Resolve-Path 'dist\TokenMeter.exe').Path
python -m pytest 'tests/test_native_window.py::test_native_webview_renders_real_page_and_accepts_clicks[exe]' tests/test_exe_proxy.py -q -p no:cacheprovider
```

Native tests open real Windows dialogs. The file-dialog checks wait for the
file-name control and its initial value, support hidden extensions, deliver
character notifications to the test-owned control, read the result back and
click its confirmation button. They verify output files and success messages.
Global keyboard/mouse automation is not used.

The test helper selects an isolated WebView2 profile and a free loopback port,
then sets `TOKENMETER_TEST_CDP_PORT` on its child. The application validates that
port and passes it to pywebview's `REMOTE_DEBUGGING_PORT` setting. Diagnostics
are disabled unless explicitly requested; do not enable them for daily use.

For failures, inspect `app.log` and `native-child.log` in the failing pytest
temporary directory. Distinguish application startup, CDP startup and a missing
dialog/control before changing code. Native helpers enforce a 120-second
process guard; pytest enforces a 300-second per-test limit. Python-level output
capture in `pytest.ini` preserves native descriptors required by Windows Tcl.
Do not replace it with file-descriptor capture when diagnosing dialog failures.

## CI and publication

[The workflow](.github/workflows/tests.yml) runs the same pinned requirements
on `windows-latest` with Python 3.12. Its checkout, Python setup and artifact
upload actions are all v6 and declare Node.js 24. Their official manifests are
linked in [the README](README.md#continuous-integration). The action runtime
does not change the app's Python version or require a local Node.js install.

Before pushing, review `git diff --check`, `git diff --stat` and the staged
diff. Include the tests and documentation needed for the change. Publish only
project files: exclude local configuration, credentials, real transcripts,
usage databases, personal exports, backups and internal notes. A source commit
does not update an already-installed executable; build and install separately
when an application update is intended.

After pushing, inspect the workflow for that exact commit. A complete result
includes source tests, the build, packaged tests and artifact upload. Report
failures, skips and annotations accurately. A green job can still carry a
deprecation warning. Updating the workflow affects future runs; historical
annotations remain visible on their original run.

Download the **TokenMeter** artifact from a successful run to test the packaged
app on another Windows machine. Artifacts require GitHub sign-in and expire;
they are not a permanent release distribution. Installation and rollback are
documented in [the README](README.md#updating-and-rolling-back).
