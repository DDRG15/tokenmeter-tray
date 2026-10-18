"""Proxy tests against a real fake upstream, over actual HTTP.

The upstream is implemented with `http.server` on purpose: it is a different
stack from the proxy's (aiohttp), so a shared misunderstanding does not cancel
itself out between the two sides.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tokenmeter.collectors.proxy import (
    LAST_REQUEST_STATE_KEY,
    ProxyCollector,
    inject_include_usage,
    last_request_ts,
    proxy_footer,
)
from tokenmeter.store import Store

ANTHROPIC_JSON = {
    "id": "msg_01Test",
    "model": "claude-opus-5",
    "usage": {
        "input_tokens": 1234,
        "output_tokens": 87,
        "cache_creation_input_tokens": 512,
        "cache_read_input_tokens": 20480,
    },
}

SSE_BODY = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"msg_stream","model":"claude-opus-5",'
    b'"usage":{"input_tokens":1000,"output_tokens":1,"cache_creation_input_tokens":0,'
    b'"cache_read_input_tokens":0}}}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","usage":{"output_tokens":555}}\n\n'
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    last_request: dict = {}

    def log_message(self, *_args):  # silence in the tests
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        FakeUpstream.last_request = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        }

        if self.path.startswith("/v1/models"):
            self._send(200, b'{"data":[{"id":"claude-opus-5","type":"model"}]}',
                       "application/json")
        elif self.path.startswith("/echo"):
            payload = json.dumps({"received": json.loads(body or b"{}")}).encode()
            self._send(200, payload, "application/json")
        elif self.path.startswith("/v1/messages/stream-slow"):
            self._send_chunked_slow()
        elif self.path.startswith("/v1/messages/stream"):
            self._send(200, SSE_BODY, "text/event-stream")
        elif self.path.startswith("/v1/messages/boom"):
            self._send(500, b'{"error":"upstream down"}', "application/json")
        else:
            self._send(200, json.dumps(ANTHROPIC_JSON).encode(), "application/json")

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("request-id", "req_011FakeUpstream")
        self.send_header("x-upstream-secret", "value")
        self.end_headers()
        self.wfile.write(body)

    def _send_chunked_slow(self):
        """Two chunks half a second apart, in real chunked encoding."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("request-id", "req_011Slow")
        self.end_headers()
        first, second = SSE_BODY.split(b"event: message_delta")
        for i, part in enumerate([first, b"event: message_delta" + second]):
            self.wfile.write(b"%X\r\n%s\r\n" % (len(part), part))
            self.wfile.flush()
            if i == 0:
                time.sleep(0.5)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), FakeUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy(tmp_path, upstream):
    store = Store(tmp_path / "p.db")
    host, port = upstream.server_address
    collector = ProxyCollector(
        store,
        "127.0.0.1",
        free_port(),
        # `odd` points to the same place but under a name that is not in the
        # extractor map: it covers upstreams the user names by hand.
        {"anthropic": f"http://{host}:{port}", "odd": f"http://{host}:{port}"},
    )
    collector.start()
    assert collector.wait_ready(10.0), f"the proxy did not start: {collector.bind_error}"
    yield collector, store
    collector.stop()


def post(collector, path, payload=None, headers=None):
    conn = http.client.HTTPConnection(collector.host, collector.port, timeout=15)
    body = json.dumps(payload or {}).encode()
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json", **(headers or {})})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


# -- forwarding ---------------------------------------------------------------


def test_json_response_reaches_client_untouched_and_is_counted(proxy):
    collector, store = proxy
    resp, data = post(collector, "/anthropic/v1/messages", {"model": "claude-opus-5"})

    assert resp.status == 200
    assert json.loads(data) == ANTHROPIC_JSON, "the body must arrive identical"
    assert resp.getheader("x-upstream-secret") == "value", "upstream headers preserved"

    totals = store.totals_since(0)
    assert totals.calls == 1
    assert totals.input_tokens == 1234
    assert totals.output_tokens == 87
    assert totals.cache_write_tokens == 512
    assert totals.cache_read_tokens == 20480


def test_unknown_provider_name_still_parses_the_right_shape(proxy):
    """The upstream name is chosen by the user; the format is told by the response.

    Without shape detection this returned correct input and output but lost the
    cache tokens: a silent and plausible error.
    """
    collector, store = proxy
    post(collector, "/odd/v1/messages", {"model": "claude-opus-5"})

    totals = store.totals_since(0)
    assert totals.input_tokens == 1234
    assert totals.cache_write_tokens == 512
    assert totals.cache_read_tokens == 20480


def test_dedupe_key_comes_from_the_request_id_header(proxy):
    collector, store = proxy
    post(collector, "/anthropic/v1/messages", {"model": "claude-opus-5"})
    # Second call: the fake upstream repeats the same request-id, so the proxy
    # must recognize it as the same one and not double the counter.
    post(collector, "/anthropic/v1/messages", {"model": "claude-opus-5"})

    assert store.totals_since(0).calls == 1
    assert collector.stats.requests == 2
    assert collector.stats.duplicates == 1


def test_credentials_are_forwarded_to_upstream(proxy):
    collector, _ = proxy
    post(collector, "/anthropic/v1/messages", {"model": "x"},
         headers={"x-api-key": "sk-ant-secret", "anthropic-version": "2023-06-01"})

    sent = FakeUpstream.last_request["headers"]
    assert sent.get("x-api-key") == "sk-ant-secret"
    assert sent.get("anthropic-version") == "2023-06-01"


def test_sse_stream_records_cumulative_output(proxy):
    collector, store = proxy
    resp, data = post(collector, "/anthropic/v1/messages/stream", {"stream": True})

    assert resp.status == 200
    assert data == SSE_BODY
    totals = store.totals_since(0)
    assert totals.input_tokens == 1000
    assert totals.output_tokens == 555, "message_delta wins, not the 1 from message_start"


def test_chunks_reach_the_client_before_the_upstream_finishes(proxy):
    """Real passthrough: the proxy cannot buffer the whole response."""
    collector, store = proxy
    conn = http.client.HTTPConnection(collector.host, collector.port, timeout=15)
    conn.request("POST", "/anthropic/v1/messages/stream-slow", body=b"{}",
                 headers={"Content-Type": "application/json"})

    start = time.perf_counter()
    resp = conn.getresponse()
    first = resp.read(20)
    first_at = time.perf_counter() - start
    rest = resp.read()
    total_at = time.perf_counter() - start
    conn.close()

    assert first, "no byte arrived"
    assert total_at >= 0.5, "the upstream takes 0.5s; the total cannot be less"

    # What is measured is the GAP between the first byte and the last, not an
    # absolute deadline. If the proxy buffered, both would arrive together and
    # the gap would be ~0. An absolute deadline would fail on a loaded machine
    # even if streaming works: both marks shift, but their difference does not.
    gap = total_at - first_at
    assert gap > 0.3, (
        f"first byte at {first_at:.2f}s and last at {total_at:.2f}s: only "
        f"{gap:.2f}s apart, the proxy is buffering"
    )
    assert b"message_delta" in first + rest

    time.sleep(0.3)  # the record happens after the body is closed
    assert store.totals_since(0).output_tokens == 555


def test_upstream_error_is_forwarded_and_not_counted(proxy):
    collector, store = proxy
    resp, data = post(collector, "/anthropic/v1/messages/boom", {})

    assert resp.status == 500
    assert json.loads(data) == {"error": "upstream down"}
    assert store.totals_since(0).calls == 0, "a 500 consumes no countable tokens"


def test_unknown_provider_returns_404_with_the_list(proxy):
    collector, store = proxy
    resp, data = post(collector, "/nonexistent/v1/messages", {})

    assert resp.status == 404
    assert "anthropic" in json.loads(data)["available"]
    assert store.totals_since(0).calls == 0


def test_models_endpoint_produces_no_event(proxy):
    collector, store = proxy
    post(collector, "/anthropic/v1/models", {})
    assert store.totals_since(0).calls == 0


# -- include_usage injection -------------------------------------------


def test_inject_adds_include_usage_to_openai_streams():
    body = json.dumps({"model": "gpt-5", "stream": True}).encode()
    out = json.loads(inject_include_usage(body, "/v1/chat/completions"))
    assert out["stream_options"] == {"include_usage": True}


def test_inject_preserves_other_stream_options():
    body = json.dumps({"stream": True, "stream_options": {"other": 1}}).encode()
    out = json.loads(inject_include_usage(body, "/v1/chat/completions"))
    assert out["stream_options"] == {"other": 1, "include_usage": True}


def test_inject_leaves_non_streaming_and_other_providers_alone():
    no_stream = json.dumps({"model": "gpt-5"}).encode()
    assert inject_include_usage(no_stream, "/v1/chat/completions") == no_stream

    anthropic = json.dumps({"model": "claude-opus-5", "stream": True}).encode()
    assert inject_include_usage(anthropic, "/v1/messages") == anthropic


def test_inject_survives_a_non_json_body():
    assert inject_include_usage(b"i am not json", "/v1/chat/completions") == b"i am not json"


def test_injection_actually_reaches_the_upstream(tmp_path, upstream):
    store = Store(tmp_path / "i.db")
    host, port = upstream.server_address
    collector = ProxyCollector(store, "127.0.0.1", free_port(),
                               {"openai": f"http://{host}:{port}"})
    collector.start()
    assert collector.wait_ready(10.0)
    try:
        post(collector, "/openai/echo/v1/chat/completions", {"model": "gpt-5", "stream": True})
        received = json.loads(FakeUpstream.last_request["body"])
        assert received["stream_options"] == {"include_usage": True}
    finally:
        collector.stop()


def test_injection_can_be_disabled(tmp_path, upstream):
    store = Store(tmp_path / "j.db")
    host, port = upstream.server_address
    collector = ProxyCollector(store, "127.0.0.1", free_port(),
                               {"openai": f"http://{host}:{port}"},
                               inject_stream_usage=False)
    collector.start()
    assert collector.wait_ready(10.0)
    try:
        post(collector, "/openai/echo/v1/chat/completions", {"model": "gpt-5", "stream": True})
        received = json.loads(FakeUpstream.last_request["body"])
        assert "stream_options" not in received
    finally:
        collector.stop()


# -- startup --------------------------------------------------------------


def test_busy_port_is_reported_not_crashed(tmp_path):
    port = free_port()
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        collector = ProxyCollector(Store(tmp_path / "b.db"), "127.0.0.1", port, {})
        collector.start()
        collector.wait_ready(10.0)
        assert collector.listening is False
        assert collector.bind_error, "the bind error must be recorded for the UI"
        collector.stop()
    finally:
        blocker.close()


# -- "never received anything" versus "was receiving and stopped" ------------


def test_injection_is_decided_by_path_not_by_upstream_name():
    """An upstream called `deepseek` or `my-gateway` speaks the OpenAI shape just the same."""
    body = json.dumps({"model": "deepseek-chat", "stream": True}).encode()
    out = json.loads(inject_include_usage(body, "/v1/chat/completions"))
    assert out["stream_options"] == {"include_usage": True}
    # The Responses API already carries usage and does not accept `include_usage`: it is left alone.
    assert inject_include_usage(body, "/v1/responses") == body


def test_injection_reaches_an_upstream_with_an_unmapped_name(tmp_path, upstream):
    store = Store(tmp_path / "k.db")
    host, port = upstream.server_address
    collector = ProxyCollector(store, "127.0.0.1", free_port(),
                               {"my-gateway": f"http://{host}:{port}"})
    collector.start()
    assert collector.wait_ready(10.0)
    try:
        post(collector, "/my-gateway/echo/v1/chat/completions",
             {"model": "x", "stream": True})
        received = json.loads(FakeUpstream.last_request["body"])
        assert received["stream_options"] == {"include_usage": True}
    finally:
        collector.stop()


def test_footer_distinguishes_never_used_from_stopped():
    now = 1_000_000.0
    assert proxy_footer(8787, 0, None, now) == "proxy :8787 never received traffic"
    assert proxy_footer(8787, 4, now - 10, now) == "proxy :8787 4"
    assert proxy_footer(8787, 0, now - 3 * 86400, now) == "proxy :8787 0 (last used 3 d ago)"
    assert proxy_footer(8787, 0, now - 7200, now) == "proxy :8787 0 (last used 2.0 h ago)"


def test_every_forwarded_request_is_remembered_even_without_usage(proxy):
    """`/v1/models` produces no event, but proves someone points at the proxy."""
    collector, store = proxy
    assert last_request_ts(store) is None
    before = time.time()
    post(collector, "/anthropic/v1/models", {})
    seen = last_request_ts(store)
    assert seen is not None and seen >= before - 1
    assert store.last_event_ts("proxy") is None


def test_last_request_survives_a_restart(tmp_path):
    db = tmp_path / "r.db"
    store = Store(db)
    store.set_state(LAST_REQUEST_STATE_KEY, repr(123.5))
    store.close()
    assert last_request_ts(Store(db)) == 123.5


def test_last_request_falls_back_to_proxy_events_for_upgraded_installs(tmp_path):
    from tokenmeter.models import UsageEvent

    store = Store(tmp_path / "u.db")
    store.add_event(UsageEvent(ts=500.0, provider="anthropic", model="m", source="proxy",
                               dedupe_key="req_a", input_tokens=1, output_tokens=1))
    store.add_event(UsageEvent(ts=900.0, provider="anthropic", model="m", source="transcript",
                               dedupe_key="req_b", input_tokens=1, output_tokens=1))
    assert last_request_ts(store) == 500.0


def test_corrupt_last_request_mark_is_treated_as_absent(tmp_path):
    store = Store(tmp_path / "c.db")
    store.set_state(LAST_REQUEST_STATE_KEY, "not-a-number")
    assert last_request_ts(store) is None
