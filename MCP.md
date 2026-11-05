# TokenMeter Linux MCP server

This is a read-only, optional data interface for MCP clients. The client starts
`python -m tokenmeter.mcp` as a subprocess and communicates over stdin/stdout.
There is no HTTP listener, system service, autostart entry, WebView or desktop
window. Disconnecting the client closes the stdio server.

This branch supports the MCP server only on Linux. Startup on another platform
fails before reading usage sources. The server identifies itself as
`TokenMeter Linux`; use `tokenmeter-linux` as its client registration name and
keep its environment separate from other integrations. It uses the standard
MCP protocol and is independent of GTK, Tk and WebView2.

## What it exposes

| Tool | Data | Arguments |
|---|---|---|
| `get_usage_summary` | Normalized tokens/cache, models and API-equivalent estimates | `hours` (1–2160, default 5), optional `provider`, `source` |
| `get_usage_history` | Daily usage with unknown/partial coverage | `days` (7, 30, 90), `basis` (`io` or `all`), optional filters |
| `get_collector_status` | Source availability, record/file counts, undated-record counts and read errors | none |

Provider filters: empty, `anthropic`, `openai`. Source filters: empty,
`transcript` (Claude Code), `codex`. Tools cannot change source paths or settings,
restore files, delete records or launch programs. All three advertise read-only
annotations and return both JSON text and structured data.

Each query scans local usage files into an in-memory SQLite database, performs
aggregation with the existing parsers/deduplication and closes that database.
It does not read or modify an installed TokenMeter database. It does not poll
in the background or include proxy-only calls. It reads usage records, not web
conversations. A gap without usage records is unknown, not measured zero.
Queries run one at a time. A summary or history query skips files not modified
since its window started (with a one-hour margin), so short windows stay fast
on large histories; `get_collector_status` still counts every file.

A summary window is rolling hours ending now (`window.kind` is `rolling`), over
the files currently on disk. It does not apply the desktop app's start-fresh
floor, retention or manual session reset, and cannot see transcripts already
removed from disk, so it can differ from the desktop panel; `window.scope_note`
says so in every response. Records stamped more than a minute in the future are
excluded. History days are local calendar days of the server process;
`utc_offset_seconds` reports that offset.

A usage record whose timestamp is missing or unparseable is never counted: the
server does not guess a date for it. Each source reports how many such records
it skipped in `events_undated` (under `collectors` in every tool response); a
value above zero means the totals leave those calls out.

Credentials are never read. Claude billing stays unknown; Codex can use plan
metadata in its usage records. An API equivalent is an estimate rather than
an invoice, and unknown model prices are reported as partial. There are no
message bodies, request/response IDs, credentials or source paths in tool output.
Model and plan names come from local records, so they are returned only when
they look like identifiers (letters, digits and `._:/@+-<>[]`, up to 64
characters); any other value becomes `unrecognized`. Codex limit fields are
returned only as finite numbers; anything else becomes `null`. A Codex window
whose reset time has passed is marked `expired: true` and its `used_percent` is
`null`, because it no longer describes current usage. When some calls have
unknown billing, both the API spend and the subscription equivalent are partial,
never a certain zero.
Read errors appear as source status, without echoing private exception contents.

## Standalone environment

Use Python 3.12 and a separate environment for this optional server. Its SDK
requires newer transitive dependencies than the desktop lockfiles, so do not
combine the manifests in the same environment. The tested SDK is the official
`mcp==2.3.0`, with runtime and transitive versions in `requirements-mcp.txt`.

For an isolated trial, from the repository root:

```bash
mkdir -p "$HOME/sandbox/tokenmeter-linux-mcp/tmp"
python3 -m venv "$HOME/sandbox/tokenmeter-linux-mcp/venv"
PIP_CACHE_DIR="$HOME/sandbox/tokenmeter-linux-mcp/cache" \
  TMPDIR="$HOME/sandbox/tokenmeter-linux-mcp/tmp" \
  "$HOME/sandbox/tokenmeter-linux-mcp/venv/bin/python" -m pip install -r requirements-mcp-dev.txt
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  TMPDIR="$HOME/sandbox/tokenmeter-linux-mcp/tmp" \
  "$HOME/sandbox/tokenmeter-linux-mcp/venv/bin/python" -m pytest \
  tests/test_usage_query.py tests/test_mcp.py tests/test_codex.py \
  tests/test_transcripts.py tests/test_store_dedupe.py \
  -q -p no:cacheprovider --basetemp="$HOME/sandbox/tokenmeter-linux-mcp/pytest-run"
```

Tests use synthetic local sources. They exercise official SDK clients in memory
and over real stdio, both automatic negotiation and legacy initialize, invalid
arguments, output privacy, EOF and child-process exit. The workflow runs these
checks in a separate Ubuntu job; desktop Windows tests retain their environment.

## Persistent Linux installation

For actual use, keep the environment outside a disposable sandbox. From a
source checkout you intend to retain:

```bash
mkdir -p "$HOME/.local/lib/tokenmeter-linux-mcp"
/usr/bin/python3 -m venv "$HOME/.local/lib/tokenmeter-linux-mcp/venv"
"$HOME/.local/lib/tokenmeter-linux-mcp/venv/bin/python" -m pip install -r requirements-mcp.txt
"$HOME/.local/lib/tokenmeter-linux-mcp/venv/bin/python" -m pip check
```

Point the client's `command` at that environment's Python and `PYTHONPATH` at
the retained checkout, using absolute paths as shown below. After installing
the Linux desktop, its installed `app` directory also contains the MCP modules
and runtime manifest, but the MCP environment remains separate. This does not
register or enable a client integration automatically.

## Launch configuration example

Replace the absolute paths in this generic MCP configuration. It is an example;
this project never writes a client's configuration or activates a server for you.
Use an environment you intend to keep for actual use, not a test venv that will
be removed. `PYTHONPATH` lets the client import this checkout regardless of cwd.
The host must support command/args/env stdio servers.

```json
{
  "mcpServers": {
    "tokenmeter-linux": {
      "command": "/absolute/path/to/tokenmeter-linux-mcp/venv/bin/python",
      "args": ["-B", "-m", "tokenmeter.mcp"],
      "env": {"PYTHONPATH": "/absolute/path/to/tokenmeter-tray"}
    }
  }
}
```

Defaults read `~/.claude/projects` and `CODEX_HOME` (or `~/.codex`). Override
roots at launch with `--claude-root PATH --codex-root PATH`; disable a source
with `--no-claude` or `--no-codex`. Source paths never come from tool arguments.
A source missing on disk is reported without creating it. Launching the command
in a terminal waits for MCP input; it does not open a GUI.

After a trial, verify the test-owned subprocesses exited, preserve useful reports,
and remove only its sandbox venv/cache/temp files. No client registration,
service installation or persistent listener is needed to run the tests.

## Where is the graphical interface?

The original web dashboard remains in `tokenmeter/ui/web/index.html`, served by
`tokenmeter/ui/webserver.py`. The Windows app opens it in its desktop window.
Linux has a native GTK interface with filters, chart/history, exports, backups
and calibration/settings, described in [LINUX.md](LINUX.md).
The MCP resource `tokenmeter://interface` returns this location information.
It does not start a browser or embedded web engine.

Protocol and SDK references:
[official Python SDK](https://py.sdk.modelcontextprotocol.io/),
[stdio lifecycle](https://py.sdk.modelcontextprotocol.io/run/),
[client tests](https://py.sdk.modelcontextprotocol.io/get-started/testing/).
