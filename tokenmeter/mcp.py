"""Read-only Linux MCP server for local usage, started on demand over stdio."""

import argparse
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from . import __version__
from .collectors.codex import default_codex_root
from .usage_query import UsageQuery


def build_server(query: UsageQuery):
    # Optional dependency: desktop users and --help do not need the MCP SDK.
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations
    from pydantic import Field

    server = MCPServer(
        "TokenMeter Linux", version=__version__, log_level="WARNING",
        instructions="Read-only observed local usage for Codex and Claude Code. "
                     "Missing history is unknown, not zero. API equivalent is not billed spend. "
                     "No GUI is launched and no credentials are read.")
    readonly = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=readonly, structured_output=True)
    def get_usage_summary(
        hours: Annotated[int, Field(ge=1, le=2160, strict=True)] = 5,
        provider: Literal["", "anthropic", "openai"] = "",
        source: Literal["", "transcript", "codex"] = "",
    ) -> dict[str, Any]:
        """Observed tokens, cache, models and API-equivalent estimates over recent hours."""
        return query.summary(hours, provider, source)

    @server.tool(annotations=readonly, structured_output=True)
    def get_usage_history(
        days: Literal[7, 30, 90] = 7,
        basis: Literal["io", "all"] = "io",
        provider: Literal["", "anthropic", "openai"] = "",
        source: Literal["", "transcript", "codex"] = "",
    ) -> dict[str, Any]:
        """Daily local usage; null days mean unknown coverage rather than zero usage."""
        return query.history(days, basis, provider, source)

    @server.tool(annotations=readonly, structured_output=True)
    def get_collector_status() -> dict[str, Any]:
        """Check local source availability and read errors without exposing source paths."""
        return query.status()

    @server.resource("tokenmeter://interface")
    def interface_info() -> str:
        """Where to find the desktop UI; reading this does not launch a window."""
        return ("TokenMeter Linux has a native GTK interface with history and settings; "
                "see LINUX.md. Start the desktop separately with python -m tokenmeter. "
                "This Linux-only MCP server exposes data over stdio and does not start GTK.")

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument("--codex-root", type=Path, default=default_codex_root())
    parser.add_argument("--no-claude", action="store_true")
    parser.add_argument("--no-codex", action="store_true")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("The MCP server in this branch is supported on Linux only")
    query = UsageQuery(None if args.no_claude else args.claude_root.expanduser().resolve(),
                       None if args.no_codex else args.codex_root.expanduser().resolve())
    try:
        server = build_server(query)
    except ImportError:
        parser.exit(2, "MCP dependencies missing: install requirements-mcp.txt in your environment.\n")
    try:
        server.run(transport="stdio")
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
