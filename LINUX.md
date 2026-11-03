# TokenMeter on Linux Mint Cinnamon

TokenMeter runs as a native GTK 3 application with an Ayatana tray indicator.
It never uses WebView or starts a browser. Closing its window exits the app and
collectors. No background service is installed. The validated desktop target is
Linux Mint 22.3 Cinnamon/X11 with Python 3.12; other desktops and Wayland are not
claimed as tested.

## Prerequisites

Distribution Python 3.12 with venv, PyGObject, GTK 3 and the Ayatana AppIndicator 3
typelib must already be available. On Mint/Ubuntu these are provided by
python3-venv, python3-gi, gir1.2-gtk-3.0 and gir1.2-ayatanaappindicator3-0.1.
The installer checks them and never installs system packages automatically.

## User installation

The Linux work lives on the `linux/native-gtk` branch. Clone it explicitly:

```bash
git clone --branch linux/native-gtk --single-branch https://github.com/DDRG15/tokenmeter-tray.git
cd tokenmeter-tray
/usr/bin/python3 scripts/install_linux.py
```

From an already-extracted Linux distribution or checkout, run the installer
with distribution Python from that directory. The installer only reads the
reviewed runtime file list; unrelated local files are excluded.

This creates the application/venv under `~/.local/lib/tokenmeter` and a launcher
in the user applications directory. It does not start the application. Start
TokenMeter explicitly from the Cinnamon applications menu. Autostart is off
unless explicitly selected with `--autostart`; `--no-autostart` removes a previous
owned autostart entry. There is no systemd unit or daemon.

Close TokenMeter before updating. An update from a newer source checkout uses
the same command, retaining one
previous program installation for rollback. Edited/unknown launcher files and
modified prior backups are not silently removed. A failed dependency install
restores the previous application and launchers. For an offline installation,
use `--wheelhouse PATH` containing the pinned Linux runtime wheels.

To uninstall (usage and configuration remain):

```bash
/usr/bin/python3 scripts/install_linux.py --uninstall
```

The installer only removes its own unchanged files. If program/launcher files
were edited or extra files added, it refuses deletion so that they can be
reviewed first. Custom `--prefix`, `--desktop-dir` and `--autostart-dir` support
staged installation tests; no sudo is needed.

## Data locations

| Data | Default location |
|---|---|
| Configuration | `$XDG_CONFIG_HOME/tokenmeter/config.json` or `~/.config/tokenmeter/config.json` |
| SQLite, locks, backups | `$XDG_DATA_HOME/tokenmeter` or `~/.local/share/tokenmeter` |
| Rotating logs | `$XDG_STATE_HOME/tokenmeter` or `~/.local/state/tokenmeter` |

Relative XDG values are ignored. `TOKENMETER_HOME` intentionally groups all files
in one explicit folder for tests/portable operation. Backups include both the
SQLite data and configuration even when their directories differ. Restore
validates first, retains a previous backup, closes collectors and exits afterward.

## Interface

Overview shows Claude session/weekly usage, provider/model tokens, API-equivalent
estimates and Codex's recorded limits. Percentages keep their own plan scope.
History provides filters, a chart and 7/30/90-day table. Unknown days are marked
as unknown, not zero. CSV/JSON exports use the selected filters and protect app
files. Settings provides manual budget, calibration from claude.ai, weekly reset,
cache basis, alerts and session reset without deleting history. Notifications
use an existing desktop notification service and are recalled on exit.

No browser scraping, credentials or proxy endpoints are configured by installation.
Normal collection reads local Codex/Claude Code sources; the proxy is configurable
in config.json. The optional read-only MCP server uses its own environment
and is started by an MCP client over stdio; it does not open GTK. See [MCP.md](MCP.md).

For source development, isolated native tests and cleanup see
[LINUX_DEVELOPMENT.md](LINUX_DEVELOPMENT.md). Build a reproducible source archive with
`python3 scripts/build_linux_archive.py --output PATH.tar.gz`; it excludes test code/data,
logs, sessions, databases, internal notes and virtual environments.
