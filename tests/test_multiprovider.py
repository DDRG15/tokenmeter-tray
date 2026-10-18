"""Several providers against the same proxy and the same store.

Demonstrates the README's claim: adding a provider whose API returns `usage` is
one line in `upstreams`, not new code.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tokenmeter.collectors.proxy import ProxyCollector
from tokenmeter.store import Store
from tests.test_proxy import free_port

RESPONSES = {
    # Gemini, native API: promptTokenCount includes the cached tokens.
    "/v1beta/models/gemini-2.5-pro:generateContent": {
        "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
        "usageMetadata": {
            "promptTokenCount": 900,
            "cachedContentTokenCount": 400,
            "candidatesTokenCount": 120,
            "thoughtsTokenCount": 30,
        },
        "modelVersion": "gemini-2.5-pro",
    },
    # OpenAI: prompt_tokens includes the cached tokens.
    "/v1/chat/completions": {
        "id": "chatcmpl-x",
        "model": "gpt-5",
        "usage": {
            "prompt_tokens": 1500,
            "completion_tokens": 42,
            "prompt_tokens_details": {"cached_tokens": 1280},
        },
    },
    # Ollama, local: counts under other names.
    "/api/chat": {
        "model": "llama3.2",
        "done": True,
        "prompt_eval_count": 26,
        "eval_count": 298,
    },
    # DeepSeek and company: OpenAI shape under a name that is not in the map.
    "/deepseek/v1/chat/completions": {
        "id": "ds-1",
        "model": "deepseek-chat",
        "usage": {"prompt_tokens": 700, "completion_tokens": 90},
    },
}


class Multi(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = RESPONSES.get(self.path)
        if body is None:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("request-id", f"req_{abs(hash(self.path)) % 10**8}")
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def env(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), Multi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    base = f"http://{host}:{port}"

    store = Store(tmp_path / "multi.db")
    proxy = ProxyCollector(
        store, "127.0.0.1", free_port(),
        {
            "google": base,
            "openai": base,
            "ollama": base,
            # Name invented by the user, without an extractor of its own.
            "deepseek": base,
        },
    )
    proxy.start()
    assert proxy.wait_ready(10.0), proxy.bind_error
    yield proxy, store
    proxy.stop()
    server.shutdown()
    server.server_close()


def call_path(proxy, path):
    con = http.client.HTTPConnection(proxy.host, proxy.port, timeout=15)
    con.request("POST", path, body=b"{}", headers={"Content-Type": "application/json"})
    resp = con.getresponse()
    resp.read()
    con.close()
    return resp.status


def test_four_providers_in_one_counter(env):
    proxy, store = env

    assert call_path(proxy, "/google/v1beta/models/gemini-2.5-pro:generateContent") == 200
    assert call_path(proxy, "/openai/v1/chat/completions") == 200
    assert call_path(proxy, "/ollama/api/chat") == 200
    assert call_path(proxy, "/deepseek/deepseek/v1/chat/completions") == 200

    by_model = {r.model: r for r in store.totals_by_model(0)}
    assert set(by_model) == {"gemini-2.5-pro", "gpt-5", "llama3.2", "deepseek-chat"}

    # Gemini: the cached tokens come out of promptTokenCount; thinking goes to output.
    gemini = by_model["gemini-2.5-pro"]
    assert (gemini.input_tokens, gemini.cache_read_tokens) == (500, 400)
    assert gemini.output_tokens == 150

    # OpenAI: likewise, the cached tokens are separated from prompt_tokens.
    openai = by_model["gpt-5"]
    assert (openai.input_tokens, openai.cache_read_tokens) == (220, 1280)
    assert openai.output_tokens == 42

    # Ollama: other field names, same result.
    ollama = by_model["llama3.2"]
    assert (ollama.input_tokens, ollama.output_tokens) == (26, 298)

    # DeepSeek: no extractor of its own, resolved by shape detection.
    deepseek = by_model["deepseek-chat"]
    assert (deepseek.input_tokens, deepseek.output_tokens) == (700, 90)

    # And everything adds up in a single session counter.
    total = store.totals_since(0)
    assert total.calls == 4
    assert total.io_tokens == 500 + 150 + 220 + 42 + 26 + 298 + 700 + 90


def test_a_new_provider_needs_no_code(env):
    """`deepseek` does not appear in EXTRACTORS: it was only added to `upstreams`."""
    from tokenmeter.collectors.parsers import EXTRACTORS

    assert "deepseek" not in EXTRACTORS
    assert call_path(env[0], "/deepseek/deepseek/v1/chat/completions") == 200
    assert env[1].totals_since(0).output_tokens == 90
