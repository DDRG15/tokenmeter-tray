import json

from tokenmeter.collectors.transcripts import (
    TranscriptCollector,
    event_from_line,
    parse_timestamp,
)
from tokenmeter.store import Store

TS = "2026-08-12T15:33:17.376Z"


def assistant_line(request_id="req_A", msg_id=None, block="text", out=654, ts=TS):
    # Each synthetic request has its own message.id. The Store joins
    # `req:` and `msg:` aliases so repeated blocks are not counted twice.
    msg_id = msg_id or f"msg_{request_id}"
    return json.dumps(
        {
            "type": "assistant",
            "requestId": request_id,
            "timestamp": ts,
            "message": {
                "id": msg_id,
                "model": "claude-opus-5",
                "role": "assistant",
                "content": [{"type": block}],
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": out,
                    "cache_creation_input_tokens": 15505,
                    "cache_read_input_tokens": 19070,
                },
            },
        }
    )


def write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_timestamp_handles_zulu():
    from datetime import datetime, timezone

    expected = datetime(2026, 8, 12, 15, 33, 17, 376000, tzinfo=timezone.utc).timestamp()
    assert parse_timestamp(TS) == expected
    assert parse_timestamp("garbage") is None
    assert parse_timestamp(None) is None


def test_timestamp_is_interpreted_as_utc_not_local():
    """The transcript marks Z; reading it as local time would shift the window."""
    from datetime import datetime, timezone

    naive_local = datetime(2026, 8, 12, 15, 33, 17, 376000).timestamp()
    as_utc = datetime(2026, 8, 12, 15, 33, 17, 376000, tzinfo=timezone.utc).timestamp()
    parsed = parse_timestamp(TS)
    assert parsed == as_utc
    if naive_local != as_utc:  # only applies outside UTC
        assert parsed != naive_local


def test_event_from_line_maps_all_four_counters():
    ev = event_from_line(assistant_line())
    assert ev.provider == "anthropic"
    assert ev.model == "claude-opus-5"
    assert (ev.input_tokens, ev.output_tokens) == (2, 654)
    assert (ev.cache_write_tokens, ev.cache_read_tokens) == (15505, 19070)
    assert ev.source == "transcript"
    assert ev.dedupe_key == "req:req_A"


def test_reads_the_cache_ttl_breakdown():
    """Without this the cache cost comes out low: 1 h costs 2x, 5 min 1.25x."""
    line = json.dumps(
        {
            "type": "assistant",
            "requestId": "req_ttl",
            "timestamp": TS,
            "message": {
                "id": "msg_ttl",
                "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 10,
                    "cache_creation_input_tokens": 15505,
                    "cache_read_input_tokens": 0,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 15505,
                    },
                },
            },
        }
    )
    ev = event_from_line(line)
    assert ev.cache_write_tokens == 15505
    assert ev.cache_write_1h_tokens == 15505


def test_missing_ttl_breakdown_defaults_to_the_cheaper_rate():
    """Without a breakdown it cannot be claimed to be 1 h; it is priced at 5 min."""
    ev = event_from_line(assistant_line())
    assert ev.cache_write_tokens == 15505
    assert ev.cache_write_1h_tokens == 0


def test_non_assistant_lines_are_ignored():
    assert event_from_line(json.dumps({"type": "user", "message": {}})) is None
    assert event_from_line(json.dumps({"type": "assistant", "message": {}})) is None
    assert event_from_line("not json") is None


def test_repeated_blocks_of_one_response_count_once(tmp_path):
    """Real Claude Code behavior: 1 call, N lines, same usage."""
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    write(
        root / "s.jsonl",
        [
            assistant_line(block="thinking"),
            assistant_line(block="tool_use"),
            assistant_line(block="tool_use"),
        ],
    )

    store = Store(tmp_path / "t.db")
    collector = TranscriptCollector(store, tmp_path / "projects")
    assert collector.scan_once() == 1

    totals = store.totals_since(0)
    assert totals.calls == 1
    assert totals.output_tokens == 654, "not 1962 — summing the 3 lines would triple it"
    assert collector.stats.events_duplicate == 2


def test_distinct_requests_are_all_counted(tmp_path):
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    write(
        root / "s.jsonl",
        [
            assistant_line(request_id="req_1", out=100),
            assistant_line(request_id="req_2", out=200),
        ],
    )
    store = Store(tmp_path / "t.db")
    assert TranscriptCollector(store, tmp_path / "projects").scan_once() == 2
    assert store.totals_since(0).output_tokens == 300


def test_tailer_only_reads_new_lines(tmp_path):
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    f = root / "s.jsonl"
    write(f, [assistant_line(request_id="req_1")])

    store = Store(tmp_path / "t.db")
    collector = TranscriptCollector(store, tmp_path / "projects")
    assert collector.scan_once() == 1
    assert collector.scan_once() == 0, "with no changes it must not reread"

    with f.open("a", encoding="utf-8") as fh:
        fh.write(assistant_line(request_id="req_2") + "\n")
    assert collector.scan_once() == 1
    assert store.totals_since(0).calls == 2


def test_partial_line_is_not_consumed_until_complete(tmp_path):
    """Claude Code may be midway through writing a line when we scan."""
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    f = root / "s.jsonl"
    partial = assistant_line(request_id="req_1")[:40]
    f.write_text(partial, encoding="utf-8")

    store = Store(tmp_path / "t.db")
    collector = TranscriptCollector(store, tmp_path / "projects")
    assert collector.scan_once() == 0

    with f.open("a", encoding="utf-8") as fh:
        fh.write(assistant_line(request_id="req_1")[40:] + "\n")
    assert collector.scan_once() == 1


def test_truncated_file_is_reread_from_scratch(tmp_path):
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    f = root / "s.jsonl"
    write(f, [assistant_line(request_id="req_1"), assistant_line(request_id="req_2")])

    store = Store(tmp_path / "t.db")
    collector = TranscriptCollector(store, tmp_path / "projects")
    collector.scan_once()

    write(f, [assistant_line(request_id="req_3")])
    assert collector.scan_once() == 1
    assert store.totals_since(0).calls == 3


def test_scans_nested_project_folders(tmp_path):
    base = tmp_path / "projects"
    (base / "a").mkdir(parents=True)
    (base / "b").mkdir(parents=True)
    write(base / "a" / "s1.jsonl", [assistant_line(request_id="req_a")])
    write(base / "b" / "s2.jsonl", [assistant_line(request_id="req_b")])

    store = Store(tmp_path / "t.db")
    collector = TranscriptCollector(store, base)
    assert collector.scan_once() == 2
    assert collector.stats.files_seen == 2


def test_missing_root_is_not_an_error(tmp_path):
    store = Store(tmp_path / "t.db")
    assert TranscriptCollector(store, tmp_path / "does-not-exist").scan_once() == 0


def test_proxy_and_transcript_agree_on_the_dedupe_key():
    """The contract that prevents double counting between collectors."""
    from tokenmeter.collectors.parsers import UsageSink

    sink = UsageSink(
        "anthropic",
        path="/v1/messages",
        content_type="application/json",
        request_id="req_011SyntheticRequest0001",
    )
    sink.feed(b'{"id":"msg_x","model":"claude-opus-5","usage":{"input_tokens":2,"output_tokens":654}}')
    from_proxy = sink.finish(ts=1.0)

    from_transcript = event_from_line(
        assistant_line(request_id="req_011SyntheticRequest0001", msg_id="msg_x")
    )
    assert from_proxy.dedupe_key == from_transcript.dedupe_key
