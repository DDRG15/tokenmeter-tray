"""Codex running against a local model, with its traffic also going through the proxy.

`codex --oss` talks to Ollama. If the user points it at the TokenMeter proxy, the
same call is seen twice: by the proxy (provider `ollama`) and by the rollout
reader (provider `openai`, key `codex:<response_id>`). They are one call only if
both records carry the same response id. These tests run a real proxy against a
fake local server and read a synthetic rollout into the same store.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.test_codex import context, record, rollout_dir, write, write_auth
from tests.test_proxy import free_port
from tokenmeter.collectors.codex import CodexCollector
from tokenmeter.collectors.proxy import ProxyCollector
from tokenmeter.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
MODEL = "gpt-oss:20b"
RESPONSE_ID = "resp_local_0001"

USAGE = {"input_tokens": 26, "output_tokens": 298, "input_tokens_details": {"cached_tokens": 0}}
RESPONSES_JSON = {"id": RESPONSE_ID, "object": "response", "model": MODEL, "usage": USAGE}
CHAT_JSON = {"id": "chatcmpl-local-1", "model": MODEL,
             "usage": {"prompt_tokens": 26, "completion_tokens": 298}}


def responses_sse(response_id: str | None = RESPONSE_ID) -> bytes:
    """The events of an OpenAI Responses stream, as Ollama emits them."""
    def event(kind, **body):
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **body})}\n\n".encode()
    ident = {"id": response_id} if response_id else {}
    return (event("response.created", response={**ident, "model": MODEL})
            + event("response.output_text.delta", delta="Hello")
            + event("response.completed", response={**ident, "model": MODEL, "usage": USAGE}))


# wire -> (path, content type, body, response id). `/api/chat` is Ollama's native
# endpoint and carries no id at all, so the rollout cannot have one either: Codex
# takes the id from the server's response and does not invent it.
WIRES = {
    "responses_json": ("/v1/responses", "application/json", json.dumps(RESPONSES_JSON).encode(), RESPONSE_ID),
    "responses_stream": ("/v1/responses", "text/event-stream", responses_sse(), RESPONSE_ID),
    "chat_completions": ("/v1/chat/completions", "application/json", json.dumps(CHAT_JSON).encode(),
                         "chatcmpl-local-1"),
    "native_chat": ("/api/chat", "application/x-ndjson", (FIXTURES / "ollama_stream.ndjson").read_bytes(), ""),
}


class LocalServer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    wire = "responses_json"

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _path, ctype, body, _rid = WIRES[type(self).wire]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_server():
    handler = type("Handler", (LocalServer,), {})
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, handler
    server.shutdown()
    server.server_close()


def session_meta(provider: str, ts: str) -> str:
    """First line of a rollout. `codex --oss` writes the local provider it used."""
    return json.dumps({"timestamp": ts, "type": "session_meta",
                       "payload": {"id": "session-local", "model_provider": provider}})


def codex_reads_rollout(root: Path, store: Store, response_id: str, auth_mode: str = "",
                        provider: str | None = None) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    if auth_mode:
        write_auth(root, auth_mode)
    lines = [context(model=MODEL, ts=now), record(resp=response_id, inp=26, out=298, ts=now)]
    if provider is not None:
        lines.insert(0, session_meta(provider, now))
    write(rollout_dir(root) / "rollout-local.jsonl", lines)
    CodexCollector(store, root).scan_once()


def proxy_forwards_call(store: Store, server, wire: str) -> None:
    path, _ctype, _body, _rid = WIRES[wire]
    host, port = server.server_address
    proxy = ProxyCollector(store, "127.0.0.1", free_port(), {"ollama": f"http://{host}:{port}"})
    proxy.start()
    assert proxy.wait_ready(10.0), proxy.bind_error
    try:
        conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=15)
        # What Codex sends to a local server: a placeholder key.
        conn.request("POST", f"/ollama{path}", body=b'{"stream": true}',
                     headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"})
        conn.getresponse().read()
        conn.close()
        deadline = time.time() + 15
        while proxy.stats.events + proxy.stats.duplicates == 0 and time.time() < deadline:
            time.sleep(0.02)
    finally:
        proxy.stop()


def stored(store: Store):
    return [dict(r) for r in store._conn.execute(  # noqa: SLF001
        "SELECT provider, model, source, billing, input_tokens, output_tokens, dedupe_key "
        "FROM usage_events ORDER BY id")]


@pytest.mark.parametrize("order", ["proxy_first", "rollout_first"])
@pytest.mark.parametrize("wire", ["responses_json", "responses_stream", "chat_completions"])
def test_one_local_codex_call_is_counted_once(tmp_path, local_server, wire, order):
    server, handler = local_server
    handler.wire = wire
    store = Store(tmp_path / "u.db")
    root = tmp_path / ".codex"
    # The id Codex writes in its rollout is the one the local server returned.
    response_id = WIRES[wire][3]

    if order == "proxy_first":
        proxy_forwards_call(store, server, wire)
        codex_reads_rollout(root, store, response_id)
    else:
        codex_reads_rollout(root, store, response_id)
        proxy_forwards_call(store, server, wire)

    rows = stored(store)
    assert store.totals_since(0).calls == 1, rows
    assert (store.totals_since(0).input_tokens, store.totals_since(0).output_tokens) == (26, 298)


@pytest.mark.parametrize("order", ["proxy_first", "rollout_first"])
def test_a_server_that_returns_no_id_cannot_make_codex_count_the_call_too(tmp_path, local_server, order):
    """No id on the wire, so no id in the rollout: the reader skips the record, the proxy counts it."""
    server, handler = local_server
    handler.wire = "native_chat"
    store = Store(tmp_path / "u.db")
    root = tmp_path / ".codex"
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    write(rollout_dir(root) / "rollout-local.jsonl",
          [context(model="llama3.2", ts=now), record(resp=None, inp=26, out=298, ts=now)])
    if order == "proxy_first":
        proxy_forwards_call(store, server, "native_chat")
        CodexCollector(store, root).scan_once()
    else:
        CodexCollector(store, root).scan_once()
        proxy_forwards_call(store, server, "native_chat")
    assert [(r["source"], r["billing"]) for r in stored(store)] == [("proxy", "local")]


@pytest.mark.parametrize("order", ["proxy_first", "rollout_first"])
@pytest.mark.parametrize("auth_mode", ["chatgpt", "apikey"])
def test_a_local_call_keeps_local_billing_when_the_rollout_has_a_login(tmp_path, local_server, auth_mode,
                                                                      order):
    """`~/.codex/auth.json` still holds the user's login while `--oss` runs.

    The login belongs to the machine, not to the call: the rollout says the call
    went to the local provider, so both collectors agree it is local and the
    merged call stays local whichever one saw it first.
    """
    server, handler = local_server
    handler.wire = "responses_json"
    store = Store(tmp_path / "u.db")
    root = tmp_path / ".codex"
    if order == "proxy_first":
        proxy_forwards_call(store, server, "responses_json")
        codex_reads_rollout(root, store, RESPONSE_ID, auth_mode, provider="ollama")
    else:
        codex_reads_rollout(root, store, RESPONSE_ID, auth_mode, provider="ollama")
        proxy_forwards_call(store, server, "responses_json")
    assert [r["billing"] for r in stored(store)] == ["local"]


@pytest.mark.parametrize("provider", ["ollama", "lmstudio", "ollama-chat", "Ollama"])
@pytest.mark.parametrize("auth_mode", ["chatgpt", "apikey"])
def test_a_rollout_to_a_local_provider_is_local_without_the_proxy(tmp_path, provider, auth_mode):
    """Without the proxy, the rollout alone must not charge a local model to the login."""
    store = Store(tmp_path / "u.db")
    codex_reads_rollout(tmp_path / ".codex", store, RESPONSE_ID, auth_mode, provider=provider)
    (row,) = stored(store)
    assert (row["billing"], row["provider"]) == ("local", provider.lower())


@pytest.mark.parametrize("auth_mode, billing", [("chatgpt", "subscription"), ("apikey", "api")])
@pytest.mark.parametrize("provider", ["openai", "my-gateway", None])
def test_a_rollout_to_any_other_provider_keeps_the_login_billing(tmp_path, auth_mode, billing, provider):
    """OpenAI, an unrecognized provider or no session_meta: the login is the only signal.

    A provider the user names in config.toml may point anywhere, including a paid
    API behind a local gateway, so its name alone cannot make the call free.
    """
    store = Store(tmp_path / "u.db")
    codex_reads_rollout(tmp_path / ".codex", store, RESPONSE_ID, auth_mode, provider=provider)
    (row,) = stored(store)
    assert (row["billing"], row["provider"]) == (billing, "openai")


def test_a_later_scan_remembers_the_local_provider_of_an_open_rollout(tmp_path):
    """A new process reads only the appended lines; the provider is on the first line."""
    store = Store(tmp_path / "u.db")
    root = tmp_path / ".codex"
    write_auth(root, "chatgpt")
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    path = rollout_dir(root) / "rollout-local.jsonl"
    write(path, [session_meta("ollama", now), context(model=MODEL, ts=now),
                 record(resp="resp_first", inp=10, out=1, ts=now)])
    CodexCollector(store, root).scan_once()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record(resp="resp_second", inp=20, out=2, ts=now) + "\n")
    CodexCollector(store, root).scan_once()  # a fresh collector, as after a restart
    assert [(r["billing"], r["provider"]) for r in stored(store)] == [("local", "ollama")] * 2
