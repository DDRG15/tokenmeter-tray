"""Local forwarding proxy that reads the `usage` of responses.

Absolute priority: the user's request. Chunks are forwarded as soon as they
arrive and the counter gets a copy; any parser failure is swallowed without
touching the response. A broken meter is annoying, a broken proxy leaves you
without an LLM.

Credentials: headers are forwarded intact and never persisted. Only `usage`
numbers go to disk.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import threading
import time
import zlib
from urllib.parse import urlsplit
from dataclasses import dataclass, replace

import aiohttp
from aiohttp import web

from ..store import Store
from .base import Collector
from ..billing import apply_override, classify_request
from .parsers import sink_for_response

log = logging.getLogger(__name__)

# Hop-by-hop headers: they must not be forwarded (RFC 9110 §7.6.1).
HOP_BY_HOP = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    }
)

# aiohttp decompresses for us, so the body we forward is no longer compressed
# and its length changes: forwarding these headers would break the client. The
# streaming layer recomputes them.
STRIPPED_RESPONSE_HEADERS = HOP_BY_HOP | {"content-encoding", "content-length"}

# `state` key with the timestamp of the last forwarded request. It is what
# tells "the proxy never received anything" apart from "it was receiving and
# stopped": events only count requests that carry `usage`.
LAST_REQUEST_STATE_KEY = "proxy_last_request_ts"

# How often the proxy records that it is still listening. Smaller than
# COVERAGE_GAP so the stretches merge into one.
PROXY_COVERAGE_EVERY = 60.0


def wants_stream_usage(path: str) -> bool:
    """Is this path a completions endpoint with an OpenAI shape?

    It is decided by the PATH, not by the upstream name: an upstream the user
    calls `deepseek` or `my-gateway` speaks the same protocol as `openai`, and
    with a name map its streams ended up without tokens (the same mistake that
    was fixed in the extractors). `/v1/responses` is left out on purpose: it
    already carries `usage` in `response.completed` and its `stream_options`
    does not accept `include_usage`.
    """
    return path.lower().rstrip("/").endswith("completions")


@dataclass
class ProxyStats:
    requests: int = 0
    events: int = 0
    duplicates: int = 0
    missing_usage: int = 0
    errors: int = 0
    last_error: str = ""


def inject_include_usage(body: bytes, path: str) -> bytes:
    """Asks for `usage` in OpenAI-shaped streaming responses.

    Without this, an OpenAI stream returns no numbers and the call can only be
    recorded as incomplete. It modifies the user's request, so it is a flag and
    is documented in the README.
    """
    if not wants_stream_usage(path) or not body:
        return body
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict) or payload.get("stream") is not True:
        return body

    options = payload.get("stream_options")
    if isinstance(options, dict) and options.get("include_usage") is True:
        return body
    payload["stream_options"] = {**(options if isinstance(options, dict) else {}),
                                 "include_usage": True}
    return json.dumps(payload).encode("utf-8")


def format_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{int(seconds // 86400)} d"


def last_request_ts(store: Store) -> float | None:
    """Last request that went through the proxy, live or from an earlier run.

    The `state` mark also covers requests without `usage` and is written on
    every request, so it is never older than a proxy event. Events are only
    queried if it is missing: someone upgrading from a version that did not
    store it. The footer calls this every second; the events query has no index
    on `source` and should not be repeated without a reason.
    """
    raw = store.get_state(LAST_REQUEST_STATE_KEY)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass  # corrupt value: treated as absent, it does not break the footer
    return store.last_event_ts("proxy")


def proxy_footer(port: int, session_calls: int, last_seen: float | None, now: float) -> str:
    """Proxy text in the panel footer.

    Three states that used to look the same (`proxy :8787 0`):

    * it has never received anything → probably no tool points at it;
    * it has calls in the session → it works;
    * it was receiving and has not in this session → someone changed the
      environment variable.
    """
    if session_calls > 0:
        return f"proxy :{port} {session_calls}"
    if last_seen is None:
        return f"proxy :{port} never received traffic"
    return f"proxy :{port} 0 (last used {format_age(max(0.0, now - last_seen))} ago)"


class ProxyCollector(Collector):
    name = "proxy"

    def __init__(
        self,
        store: Store,
        host: str,
        port: int,
        upstreams: dict[str, str],
        *,
        inject_stream_usage: bool = True,
        billing_overrides: dict | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self.host = host
        self.port = port
        self._upstreams = {k.lower(): v.rstrip("/") for k, v in upstreams.items()}
        self._inject = inject_stream_usage
        self._overrides = billing_overrides
        self.stats = ProxyStats()
        self.bind_error: str = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ready = threading.Event()
        self._measurement_epoch = 0

    def pause(self) -> None:
        self._measurement_epoch += 1
        super().pause()

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        host = request.host.lower()
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        if host not in allowed:
            return web.json_response({"error": "host not allowed"}, status=403)
        origin = request.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.netloc.lower() not in allowed or parsed.path:
                return web.json_response({"error": "origin not allowed"}, status=403)
        return await handler(request)

    @property
    def listening(self) -> bool:
        return self._ready.is_set() and not self.bind_error

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Blocks until the port is open (or fails to open)."""
        self._ready.wait(timeout)
        return self.listening

    def base_url(self, provider: str) -> str:
        return f"http://{self.host}:{self.port}/{provider}"

    # -- thread --------------------------------------------------------------

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)
        super().stop(timeout=timeout)

    async def _serve(self) -> None:
        app = web.Application(client_max_size=64 * 1024 ** 2, middlewares=[self._guard])
        app.router.add_route("*", "/{provider}/{tail:.*}", self._handle)
        app.router.add_route("*", "/{provider}", self._handle)

        # The client session is created BEFORE opening the port: if the site
        # accepted a request before it existed, `_handle` would fail with
        # AttributeError on the first call after startup.
        # `sock_read=None` is mandatory: a long stream cannot time out.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=True,
                                         cookie_jar=aiohttp.DummyCookieJar()) as session:
            self._session = session

            # Keep the client's bytes, including their compression.
            runner = web.AppRunner(app, access_log=None, auto_decompress=False)
            await runner.setup()
            site = web.TCPSite(runner, self.host, self.port)
            try:
                await site.start()
            except OSError as exc:
                # Port busy: it is recorded and the app carries on with the other collectors.
                self.bind_error = str(exc)
                self._ready.set()
                log.error("could not open port %s: %s", self.port, exc)
                await runner.cleanup()
                return

            if site._server and site._server.sockets:
                self.port = site._server.sockets[0].getsockname()[1]
            self._ready.set()
            log.info("proxy listening on http://%s:%s", self.host, self.port)
            # The proxy has no past to recover: it only covers while it is
            # listening and not paused.
            covered_from = time.time()
            last_mark = 0.0
            while not self._stop.is_set():
                await asyncio.sleep(0.25)
                now = time.time()
                if self.paused:
                    covered_from = now
                elif now - last_mark >= PROXY_COVERAGE_EVERY:
                    self._mark_covered(covered_from, now)
                    covered_from, last_mark = now, now
            if not self.paused:
                self._mark_covered(covered_from, time.time())
            await runner.cleanup()

    # -- forwarding -----------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if self._session is None:  # pragma: no cover - impossible window after the reordering
            return web.json_response({"error": "TokenMeter is still starting up"}, status=503)

        provider = request.match_info.get("provider", "").lower()
        upstream = self._upstreams.get(provider)
        if upstream is None:
            return web.json_response(
                {
                    "error": f"provider '{provider}' is not configured in TokenMeter",
                    "available": sorted(self._upstreams),
                },
                status=404,
            )

        tail = request.match_info.get("tail", "")
        target = f"{upstream}/{tail}" if tail else upstream
        body = await request.read()
        parser_body = body
        encoding = request.headers.get("Content-Encoding", "").lower()
        if encoding == "gzip":
            try:
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                decoded = decoder.decompress(body, 4 * 1024 ** 2 + 1)
                parser_body = decoded if decoder.eof and len(decoded) <= 4 * 1024 ** 2 else b""
            except zlib.error:
                parser_body = b""
        elif encoding and encoding != "identity":
            parser_body = b""
        if self._inject and parser_body:
            injected = inject_include_usage(parser_body, f"/{tail}")
            if injected != parser_body:
                body = gzip.compress(injected) if encoding == "gzip" else injected
            parser_body = injected

        connection_headers = {h.strip().lower() for h in request.headers.get("Connection", "").split(",")}
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP | connection_headers
            and k.lower() not in ("host", "content-length")
        }

        # Billing type from the shape of the credential. The credential does not
        # leave this function: only the category travels.
        try:
            billing = apply_override(
                provider, classify_request(headers, upstream, dict(request.rel_url.query)),
                self._overrides)
        except Exception:
            billing = ("unknown", "")

        self.stats.requests += 1
        started = time.time()
        epoch = self._measurement_epoch
        measure = not self.paused
        self._mark_seen(started)

        try:
            async with self._session.request(
                request.method,
                target,
                params=request.rel_url.query,
                headers=headers,
                data=body or None,
                allow_redirects=False,
            ) as upstream_response:
                return await self._pump(request, upstream_response, provider, tail, parser_body,
                                        started, billing, measure=measure, epoch=epoch)
        except asyncio.CancelledError:
            raise
        except aiohttp.ClientError as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)
            return web.json_response(
                {"error": "TokenMeter could not reach the upstream", "detail": str(exc)},
                status=502,
            )

    def _mark_covered(self, start: float, end: float) -> None:
        try:
            self._store.mark_covered(start, end, gap=0, source="proxy")
        except Exception:
            log.debug("could not record the proxy coverage", exc_info=True)

    def _mark_seen(self, ts: float) -> None:
        """Persists the last request seen. A failure here does not touch the request."""
        try:
            self._store.set_state(LAST_REQUEST_STATE_KEY, repr(ts))
        except Exception:
            log.debug("could not store the last-request mark", exc_info=True)

    async def _pump(
        self,
        request: web.Request,
        upstream_response: aiohttp.ClientResponse,
        provider: str,
        tail: str,
        request_body: bytes,
        started: float,
        billing: tuple[str, str] = ("unknown", ""),
        *, measure: bool = True, epoch: int | None = None,
    ) -> web.StreamResponse:
        out = web.StreamResponse(status=upstream_response.status)
        for key, value in upstream_response.headers.items():
            if key.lower() not in STRIPPED_RESPONSE_HEADERS:
                out.headers.add(key, value)
        out.enable_chunked_encoding()
        await out.prepare(request)

        sink = None
        if upstream_response.status < 400:
            try:
                sink = sink_for_response(
                    provider, f"/{tail}", dict(upstream_response.headers), request_body
                )
            except Exception:
                log.debug("could not create the sink", exc_info=True)

        async for chunk in upstream_response.content.iter_any():
            await out.write(chunk)  # the client first, always
            if sink is not None:
                sink.feed(chunk)

        await out.write_eof()

        if sink is not None and measure and epoch == self._measurement_epoch and not self.paused:
            self._record(sink, started, billing)
        return out

    def _record(self, sink, started: float, billing: tuple[str, str] = ("unknown", "")) -> None:
        if self.paused:
            return
        try:
            event = sink.finish(ts=started)
        except Exception:
            log.debug("the sink failed on close", exc_info=True)
            return
        if event is not None:
            event = replace(event, billing=billing[0], plan=billing[1])
        if event is None:
            return
        if not event.usage_complete:
            self.stats.missing_usage += 1
        try:
            added = self._store.add_event(event)
        except Exception:
            self.stats.errors += 1
            self.stats.last_error = "Could not store usage"
            log.warning("Could not store proxy usage", exc_info=False)
            return
        if added:
            self.stats.events += 1
        else:
            # The transcript reader had already seen it: this is exactly what
            # dedupe exists to prevent.
            self.stats.duplicates += 1
