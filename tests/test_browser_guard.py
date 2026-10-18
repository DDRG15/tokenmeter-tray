"""A page from another origin cannot use the proxy as an intermediary."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest
from aiohttp import web

from tests.test_proxy_passthrough import Upstream, setup_proxy  # noqa: F401 - shared fixture
from tokenmeter.collectors.proxy import ProxyCollector


def test_browser_cross_origin_posts_never_reach_upstream(request, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    origins = []
    original_guard = ProxyCollector._guard

    @web.middleware
    async def observed_guard(self, request, handler):
        origins.append(request.headers.get("Origin"))
        return await original_guard(self, request, handler)

    monkeypatch.setattr(ProxyCollector, "_guard", observed_guard)
    proxy, store = request.getfixturevalue("setup_proxy")
    forwarded = []
    original = Upstream.do_POST

    def record(handler):
        forwarded.append(handler.path)
        return original(handler)

    monkeypatch.setattr(Upstream, "do_POST", record)

    class Page(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = b"<!doctype html><title>Foreign origin test</title><iframe sandbox='allow-scripts'></iframe>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    site = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = threading.Thread(target=site.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as driver:
            try:
                browser = driver.chromium.launch(channel="msedge", headless=True)
            except Exception as exc:
                pytest.skip(f"Edge is not available: {exc}")
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{site.server_port}/")
                url = f"http://127.0.0.1:{proxy.port}/test/v1/messages"
                # Simple POST: it does not depend on CORS blocking a preflight.
                # Origin: null from a sandboxed iframe is also tested.
                for frame, origin in ((page.main_frame, f"http://127.0.0.1:{site.server_port}"),
                                      (page.frames[1], "null")):
                    with page.expect_response(lambda r: r.url == url, timeout=10000) as received:
                        frame.evaluate("""url => fetch(url, {method: 'POST', mode: 'no-cors',
                            headers: {'Content-Type': 'text/plain'}, body: '{}'})
                            .then(() => null).catch(() => null)""", url)
                    response = received.value
                    assert response.status == 403
                    assert origins and origins[-1] == origin, (origins, response.headers, response.text())
                assert forwarded == []
                assert store.totals_since(0).calls == 0
            finally:
                browser.close()
    finally:
        site.shutdown()
        site.server_close()
        thread.join(timeout=5)
