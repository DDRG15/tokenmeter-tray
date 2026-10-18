"""Local server for the web window.

It serves `web/index.html` and three JSON routes. It runs on its own thread
with its own asyncio loop, like the proxy, and never touches Tk: the page's
actions are enqueued with `app.enqueue` and run by the main thread.

Security, because any web page open in the browser can request
`http://127.0.0.1:<port>`:

* It only listens on 127.0.0.1, on a port chosen by the system.
* Every `/api` route requires the `X-TM-Token` header, a secret known only to
  the page served by this process. Another web page cannot read it and, without
  it, cannot trigger actions.
* The `Host` header has to be the server's own: it cuts off DNS rebinding,
  where an external domain starts resolving to 127.0.0.1.
* Actions come from a closed list (`ALLOWED_ACTIONS`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from pathlib import Path

from aiohttp import web

from ..dashboard import ALLOWED_ACTIONS, build_history, build_state

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
TOKEN_PLACEHOLDER = "__TM_TOKEN__"
TOKEN_HEADER = "X-TM-Token"
NO_STORE = {"Cache-Control": "no-store"}


class DashboardServer:
    def __init__(self, app, host: str = "127.0.0.1", port: int = 0) -> None:
        self._app = app
        self.host = host
        self.port = port
        self.token = secrets.token_hex(18)
        self.error = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()

    # -- lifecycle -----------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def listening(self) -> bool:
        return self._ready.is_set() and not self.error

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="dashboard", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        self._ready.wait(timeout)
        return self.listening

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:  # a failure here leaves the app with the Tk panel
            self.error = str(exc) or type(exc).__name__
            self._ready.set()
            log.exception("the window server ended with an error")
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        app = web.Application(middlewares=[self._guard])
        app.router.add_get("/", self._index)
        # The browser requests it on its own; without a route, every load left a 404 in the console.
        app.router.add_get("/favicon.ico", self._favicon)
        app.router.add_get("/api/state", self._state)
        app.router.add_get("/api/history", self._history)
        app.router.add_get("/api/export", self._export)
        app.router.add_post("/api/action", self._action)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        try:
            await site.start()
        except OSError as exc:
            self.error = str(exc)
            self._ready.set()
            log.error("could not open the window server: %s", exc)
            await runner.cleanup()
            return

        # With port=0 the system picks a free one; it is read from the real socket.
        sockets = site._server.sockets if site._server else []  # noqa: SLF001
        if sockets:
            self.port = sockets[0].getsockname()[1]
        self._ready.set()
        log.info("web window at %s", self.url)
        while not self._stop.is_set():
            await asyncio.sleep(0.25)
        await runner.cleanup()

    # -- guards -----------------------------------------------------------

    def _allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if request.host not in self._allowed_hosts():
            log.warning("403 %s %s: host %r", request.method, request.path, request.host)
            return web.Response(status=403, text="host not allowed")
        if request.path.startswith("/api/"):
            if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), self.token):
                log.warning("403 %s %s: token missing or different", request.method, request.path)
                return web.json_response({"ok": False, "error": "token"}, status=403)
        log.debug("%s %s received", request.method, request.path)
        return await handler(request)

    # -- routes -------------------------------------------------------------

    async def _favicon(self, _request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def _index(self, _request: web.Request) -> web.Response:
        try:
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            return web.Response(status=500, text="tokenmeter/ui/web/index.html is missing")
        return web.Response(text=html.replace(TOKEN_PLACEHOLDER, self.token),
                            content_type="text/html", charset="utf-8", headers=NO_STORE)

    async def _state(self, request: web.Request) -> web.Response:
        return self._json(build_state(self._app, provider=request.query.get("provider", ""),
                                      source=request.query.get("source", "")))

    async def _history(self, request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", "30"))
        except ValueError:
            days = 30
        return self._json(build_history(self._app, days, provider=request.query.get("provider", ""),
                                        source=request.query.get("source", "")))

    async def _export(self, request: web.Request) -> web.Response:
        from ..exports import export_usage
        try:
            body, content_type, filename = export_usage(
                self._app, int(request.query.get("days", "30")),
                provider=request.query.get("provider", ""), source=request.query.get("source", ""),
                format=request.query.get("format", "json"))
        except ValueError:
            return web.json_response({"error": "invalid format or period"}, status=400)
        return web.Response(body=body, content_type=content_type,
                            headers={**NO_STORE, "Content-Disposition": f'attachment; filename="{filename}"'})

    async def _action(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "json"}, status=400)
        action = body.get("action") if isinstance(body, dict) else None
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
            return web.json_response({"ok": False, "error": "unknown action"}, status=400)
        if action in {"export_csv", "export_json"}:
            from ..history import RANGES_DAYS
            days = body.get("days", 30)
            provider, source = body.get("provider", ""), body.get("source", "")
            if (type(days) is not int or days not in RANGES_DAYS or
                    not all(isinstance(x, str) and len(x) < 200 for x in (provider, source))):
                return web.json_response({"error": "invalid filters"}, status=400)
            self._app.enqueue({"action": action, "days": days, "provider": provider, "source": source})
        else:
            self._app.enqueue(action)
        return web.json_response({"ok": True}, headers=NO_STORE)

    @staticmethod
    def _json(payload: dict) -> web.Response:
        return web.json_response(payload, headers=NO_STORE,
                                 dumps=lambda o: json.dumps(o, ensure_ascii=False))
