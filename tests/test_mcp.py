"""Optional official-SDK tests; no host configuration or background service."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

sdk = pytest.importorskip("mcp", reason="optional MCP tests require requirements-mcp-dev.txt")
from tokenmeter.mcp import build_server
from tests.test_usage_query import usage_sources  # noqa: F401 - shared fixture

ROOT = Path(__file__).resolve().parents[1]


def test_official_client_lists_tools_calls_them_and_reads_ui_resource(usage_sources):
    query, _, _, _ = usage_sources
    async def exercise():
        async with sdk.Client(build_server(query), raise_exceptions=True) as client:
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools} == {
                "get_usage_summary", "get_usage_history", "get_collector_status"}
            assert all(t.annotations.read_only_hint for t in tools)
            result = await client.call_tool("get_usage_summary", {})
            assert not result.is_error
            assert result.structured_content["totals"]["calls"] == 2
            history = await client.call_tool("get_usage_history", {"days": 7})
            assert history.structured_content["bars"][-1]["coverage"] == "partial"
            status = await client.call_tool("get_collector_status", {})
            assert status.structured_content["credentials_read"] is False
            resource = await client.read_resource("tokenmeter://interface")
            assert "LINUX.md" in resource.contents[0].text
            assert "Linux-only" in resource.contents[0].text
    asyncio.run(asyncio.wait_for(exercise(), timeout=15))


@pytest.mark.parametrize("tool,args", [
    ("get_usage_summary", {"hours": 0}),
    ("get_usage_summary", {"hours": True}),
    ("get_usage_summary", {"hours": 2161}),
    ("get_usage_summary", {"source": "../other-source"}),
    ("get_usage_history", {"days": 1}),
    ("get_usage_history", {"basis": "wrong"}),
    ("delete_usage", {}),
])
def test_protocol_rejects_invalid_arguments_and_unknown_tools(usage_sources, tool, args):
    query, _, _, _ = usage_sources
    async def exercise():
        async with sdk.Client(build_server(query), raise_exceptions=True) as client:
            result = await client.call_tool(tool, args)
            assert result.is_error
    asyncio.run(asyncio.wait_for(exercise(), timeout=15))


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_real_stdio_session_exits_when_client_disconnects(usage_sources, tmp_path, monkeypatch, mode):
    query, _, _, _ = usage_sources
    import anyio
    original_open = anyio.open_process
    spawned = []
    async def capture(*args, **kwargs):
        process = await original_open(*args, **kwargs)
        spawned.append(process)
        return process
    monkeypatch.setattr(anyio, "open_process", capture)
    async def exercise():
        parameters = sdk.StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "tokenmeter.mcp", "--claude-root", str(query.claude_root),
                  "--codex-root", str(query.codex_root)],
            cwd=str(ROOT),
            env={"PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp_path),
                 "TOKENMETER_HOME": str(tmp_path / "must-not-exist")})
        with (tmp_path / "server.log").open("w") as errlog:
            async with sdk.Client(sdk.stdio_client(parameters, errlog=errlog), mode=mode) as client:
                assert client.server_info.name == "TokenMeter Linux"
                assert len((await client.list_tools()).tools) == 3
                result = await client.call_tool("get_usage_summary", {"source": "codex"})
                assert not result.is_error
                assert result.structured_content["totals"]["all_tokens"] == 1100
                assert "PRIVATE_" not in json.dumps(result.structured_content)
    asyncio.run(asyncio.wait_for(exercise(), timeout=20))
    if sys.platform != "win32":
        assert spawned
    assert all(p.returncode == 0 for p in spawned)
    assert not (tmp_path / "must-not-exist").exists()


def test_mcp_dependency_versions_match_the_optional_manifests():
    from importlib.metadata import version
    for name in ("requirements-mcp.txt", "requirements-mcp-dev.txt"):
        for raw in (ROOT / name).read_text().splitlines():
            if raw and not raw.startswith(("#", "-r")):
                package, pinned = raw.split("==")
                assert version(package) == pinned


def test_stdio_eof_exits_without_creating_data_or_emitting_protocol_noise(tmp_path):
    import subprocess
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(tmp_path),
               TOKENMETER_HOME=str(tmp_path / "no-data"))
    result = subprocess.run(
        [sys.executable, "-B", "-m", "tokenmeter.mcp", "--claude-root", str(tmp_path / "claude"),
         "--codex-root", str(tmp_path / "codex")], cwd=ROOT, env=env,
        input="", capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_other_platforms_are_rejected_before_scanning_or_building_server(tmp_path, monkeypatch, capsys, platform):
    from types import SimpleNamespace
    from tokenmeter import mcp as module

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path / "must-not-exist"))

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported platforms must fail before querying usage")

    monkeypatch.setattr(module, "UsageQuery", forbidden)
    with pytest.raises(SystemExit) as exc:
        module.main(["--no-claude", "--no-codex"])
    assert exc.value.code == 2
    assert "Linux only" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
