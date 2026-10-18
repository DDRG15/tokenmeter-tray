"""The most likely design failure: both collectors seeing the same call.

Comparing keys in isolation is not enough. Here the proxy is run for real
against a fake upstream and, on the same store, the transcript Claude Code would
have written for that same call is passed through.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tokenmeter.collectors.proxy import ProxyCollector
from tokenmeter.collectors.transcripts import TranscriptCollector
from tokenmeter.config import Config
from tokenmeter.session import SessionTracker
from tokenmeter.store import Store
from tests.test_proxy import free_port

REQUEST_ID = "req_011SyntheticShared"
MESSAGE_ID = "msg_011SyntheticShared"

RESPONSE = {
    "id": MESSAGE_ID,
    "model": "claude-opus-5",
    "usage": {
        "input_tokens": 2,
        "output_tokens": 654,
        "cache_creation_input_tokens": 15505,
        "cache_read_input_tokens": 19070,
    },
}


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps(RESPONSE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("request-id", REQUEST_ID)
        self.end_headers()
        self.wfile.write(body)


def transcript_lines(path):
    """The 3 lines Claude Code writes for a single response.

    The timestamp is generated **relative to now**. With a fixed date, the test
    that looks at the session window would pass or fail depending on the clock of
    whoever runs it: as soon as the mark is more than 5 h old, the session
    expires and does not count.
    """
    a_minute_ago = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")

    lines = []
    for block in ("thinking", "tool_use", "tool_use"):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "requestId": REQUEST_ID,
                    "timestamp": a_minute_ago,
                    "message": {
                        "id": MESSAGE_ID,
                        "model": "claude-opus-5",
                        "content": [{"type": block}],
                        "usage": RESPONSE["usage"],
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_same_call_through_proxy_and_transcript_counts_once(tmp_path, upstream):
    store = Store(tmp_path / "shared.db")
    host, port = upstream.server_address

    # 1) The call goes through the proxy.
    proxy = ProxyCollector(store, "127.0.0.1", free_port(),
                           {"anthropic": f"http://{host}:{port}"})
    proxy.start()
    assert proxy.wait_ready(10.0), proxy.bind_error
    try:
        conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=15)
        conn.request("POST", "/anthropic/v1/messages", body=b'{"model":"claude-opus-5"}',
                     headers={"Content-Type": "application/json"})
        assert conn.getresponse().status == 200
        conn.close()
        # The proxy records the event AFTER delivering the response (the client
        # first). Stopping without waiting is a race that, under load, loses the
        # event and fails the test without the product failing.
        deadline = time.time() + 15  # with the suite under load, 5 s was not enough
        while store.totals_since(0).calls == 0 and time.time() < deadline:
            time.sleep(0.02)
    finally:
        proxy.stop()

    assert store.totals_since(0).calls == 1

    # 2) Claude Code writes its transcript of that SAME call, in 3 lines.
    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    transcript_lines(projects / "session.jsonl")
    TranscriptCollector(store, tmp_path / "projects").scan_once()

    totals = store.totals_since(0)
    assert totals.calls == 1, "the proxy and the transcript are the same call"
    assert totals.input_tokens == 2
    assert totals.output_tokens == 654, "neither 1962 (3 lines) nor 1308 (2 collectors)"
    assert totals.cache_write_tokens == 15505
    assert totals.cache_read_tokens == 19070


def test_the_same_call_in_reverse_order_also_counts_once(tmp_path, upstream):
    """The transcript can arrive before the proxy has closed the body."""
    store = Store(tmp_path / "shared.db")
    host, port = upstream.server_address

    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    transcript_lines(projects / "session.jsonl")
    TranscriptCollector(store, tmp_path / "projects").scan_once()
    assert store.totals_since(0).calls == 1

    proxy = ProxyCollector(store, "127.0.0.1", free_port(),
                           {"anthropic": f"http://{host}:{port}"})
    proxy.start()
    assert proxy.wait_ready(10.0), proxy.bind_error
    try:
        conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=15)
        conn.request("POST", "/anthropic/v1/messages", body=b"{}",
                     headers={"Content-Type": "application/json"})
        conn.getresponse().read()
        conn.close()
    finally:
        proxy.stop()

    assert store.totals_since(0).calls == 1
    assert proxy.stats.duplicates == 1, "the proxy must recognize it was already counted"


def test_percentage_is_not_inflated_by_the_duplicate_paths(tmp_path, upstream):
    """What the user sees: the icon's % with both sources active."""
    store = Store(tmp_path / "shared.db")
    host, port = upstream.server_address
    config = Config(session_budget_tokens=1000, percent_basis="io")

    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    transcript_lines(projects / "session.jsonl")
    TranscriptCollector(store, tmp_path / "projects").scan_once()

    proxy = ProxyCollector(store, "127.0.0.1", free_port(),
                           {"anthropic": f"http://{host}:{port}"})
    proxy.start()
    assert proxy.wait_ready(10.0), proxy.bind_error
    try:
        conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=15)
        conn.request("POST", "/anthropic/v1/messages", body=b"{}",
                     headers={"Content-Type": "application/json"})
        conn.getresponse().read()
        conn.close()
    finally:
        proxy.stop()

    snap = SessionTracker(store, config).snapshot()
    assert snap.used_tokens == 656  # 2 + 654, only once
    assert snap.percent == pytest.approx(65.6)
