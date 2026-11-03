"""On-demand usage queries never change sources or read credentials."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time

import pytest

from tokenmeter.usage_query import UsageQuery
from tokenmeter.store import Store


@pytest.fixture
def usage_sources(tmp_path):
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    claude.mkdir()
    (codex / "sessions").mkdir(parents=True)
    assistant = {"timestamp": stamp, "type": "assistant", "requestId": "req_synthetic_mcp",
                 "message": {"id": "msg_synthetic_mcp", "model": "synthetic-claude",
                             "content": [{"type": "text", "text": "PRIVATE_CONVERSATION_SENTINEL"}],
                             "usage": {"input_tokens": 100, "output_tokens": 20,
                                       "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40}}}
    transcript = claude / "session.jsonl"
    transcript.write_text((json.dumps(assistant) + "\n") * 2)
    lines = [
        {"type": "session_meta", "payload": {"base_instructions": "PRIVATE_INSTRUCTIONS_SENTINEL"}},
        {"timestamp": stamp, "type": "turn_context", "payload": {"turn_id": "turn1", "model": "synthetic-codex"}},
        {"timestamp": stamp, "type": "token_usage_record", "payload": {
            "turn_id": "turn1", "response_id": "resp_synthetic_mcp",
            "usage": {"input_tokens": 1000, "cached_input_tokens": 600,
                      "output_tokens": 100, "reasoning_output_tokens": 40, "total_tokens": 1100}}},
    ]
    (codex / "sessions" / "session.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\n")
    (codex / "auth.json").write_text('{"auth_mode": "apikey", "OPENAI_API_KEY": "PRIVATE_KEY_SENTINEL"}')
    return UsageQuery(claude, codex), now, assistant, transcript


def test_memory_store_never_creates_a_database_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store.in_memory()
    try:
        assert store._conn.execute("PRAGMA database_list").fetchone()[2] == ""
        assert store._conn.execute("PRAGMA temp_store").fetchone()[0] == 2
    finally:
        store.close()
    assert list(tmp_path.iterdir()) == []


def test_summary_separates_cache_and_deduplicates(usage_sources):
    query, now, _, _ = usage_sources
    summary = query.summary(now=now)
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["input_tokens"] == 500
    assert summary["totals"]["output_tokens"] == 120
    assert summary["totals"]["io_tokens"] == 620
    assert summary["totals"]["all_tokens"] == 1290
    assert query.summary(source="codex", now=now)["totals"]["all_tokens"] == 1100
    assert query.summary(provider="anthropic", now=now)["totals"]["all_tokens"] == 190
    assert query.summary(now=now)["totals"] == summary["totals"]


def test_no_credentials_messages_or_paths_are_exposed(usage_sources, monkeypatch):
    query, now, _, _ = usage_sources
    def forbidden(*args, **kwargs):
        pytest.fail("Attempt to inspect credentials")
    monkeypatch.setattr("tokenmeter.collectors.codex.read_auth_mode", forbidden)
    monkeypatch.setattr("tokenmeter.billing.claude_code_billing", forbidden)
    output = json.dumps(query.summary(now=now))
    assert "PRIVATE_" not in output
    assert str(query.claude_root) not in output
    assert str(query.codex_root) not in output
    assert all(item["billing"] == "unknown" for item in query.summary(now=now)["models"])


def test_sources_remain_unchanged_and_append_is_seen(usage_sources):
    query, now, assistant, path = usage_sources
    before = {p: p.read_bytes() for root in (query.claude_root, query.codex_root)
              for p in root.rglob("*") if p.is_file()}
    query.history(now=now)
    assert all(p.read_bytes() == content for p, content in before.items())
    assistant["requestId"] = "req_synthetic_mcp_2"
    assistant["message"]["id"] = "msg_synthetic_mcp_2"
    with path.open("a") as fh:
        fh.write(json.dumps(assistant) + "\n")
    assert query.summary(now=now)["totals"]["calls"] == 3
    assert not list(query.claude_root.parent.rglob("*.db"))


def test_history_gaps_are_unknown_and_observed_days_are_partial(usage_sources):
    query, now, _, _ = usage_sources
    result = query.history(now=now)
    assert result["bars"][-1]["tokens"] == 620
    assert result["bars"][-1]["coverage"] == "partial"
    assert all(row["tokens"] is None for row in result["bars"][:-1])
    assert query.history(basis="all", now=now)["bars"][-1]["tokens"] == 1290


def test_missing_and_disabled_sources_create_no_directories(tmp_path):
    query = UsageQuery(tmp_path / "missing", None)
    result = query.status()
    assert result["collectors"]["transcript"]["state"] == "missing"
    assert result["collectors"]["codex"]["state"] == "disabled"
    assert not list(tmp_path.iterdir())


def test_scan_failures_are_reported_without_private_exception_text(usage_sources, monkeypatch):
    query, _, _, _ = usage_sources
    def broken(*args):
        raise ValueError("PRIVATE_TRANSCRIPT_FRAGMENT")
    monkeypatch.setattr("tokenmeter.usage_query.CodexCollector.scan_once", broken)
    result = query.status()
    assert result["collectors"]["codex"]["state"] == "error"
    assert "PRIVATE_" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["directory_check", "reader_init"])
def test_inaccessible_sources_report_errors_without_leaking_paths(usage_sources, monkeypatch, failure):
    query, _, _, _ = usage_sources

    def forbidden(*args, **kwargs):
        raise PermissionError("PRIVATE_SOURCE_PATH")

    if failure == "directory_check":
        original = Path.is_dir

        def is_dir(path):
            return forbidden() if path == query.codex_root else original(path)

        monkeypatch.setattr(Path, "is_dir", is_dir)
    else:
        monkeypatch.setattr("tokenmeter.usage_query.CodexCollector", forbidden)
    result = query.status()
    assert result["collectors"]["codex"]["state"] == "error"
    assert result["collectors"]["codex"]["read_error"] is True
    assert result["collectors"]["transcript"]["state"] == "ready"
    assert "PRIVATE_" not in json.dumps(result)


@pytest.mark.parametrize("kwargs", [{"hours": 0}, {"hours": True}, {"hours": 2161},
                                    {"provider": "arbitrary"}, {"source": "../../file"}])
def test_invalid_summary_filters_fail_before_scanning(kwargs):
    with pytest.raises(ValueError):
        UsageQuery(None, None).summary(**kwargs)
