"""The exe honors a configured port and forwards without polluting real data."""

from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from tokenmeter.config import Config
from tokenmeter.ui.window import CREATE_NO_WINDOW, kill_tree


def test_exe_proxy_uses_configured_port_and_deduplicates_without_logging_secrets(tmp_path, monkeypatch):
    if sys.platform != "win32":
        pytest.skip("The distributed executable is for Windows")
    executable = os.environ.get("TOKENMETER_TEST_EXE")
    if not executable:
        pytest.skip("Run with TOKENMETER_TEST_EXE after building")
    executable = str(Path(executable).resolve())
    assert Path(executable).is_file()
    requests = []
    credential = "Bearer sk-synthetic-exe-port-test"
    private_body = "synthetic-body-do-not-persist"

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, self.headers.get("Authorization"), json.loads(payload)))
            body = json.dumps({"id": "msg_exe_port", "model": "synthetic-exe",
                               "usage": {"input_tokens": 10, "output_tokens": 3}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("request-id", "req_exe_port")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "isolated-exe"
    home.mkdir()
    monkeypatch.setenv("TOKENMETER_HOME", str(home))
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    assert port not in (8787, 8788)
    Config(proxy_port=port, transcripts_enabled=False, codex_enabled=False,
           upstreams={"anthropic": f"http://127.0.0.1:{upstream.server_port}"}).save()
    env = dict(os.environ)
    proc = None

    def call(path, *, payload=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            client.request("POST" if payload is not None else "GET", path,
                           body=payload, headers=headers or {})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    try:
        proc = subprocess.Popen([executable], env=env, creationflags=CREATE_NO_WINDOW)
        deadline = time.monotonic() + 30
        while True:
            assert proc.poll() is None, "The exe exited before opening the proxy"
            try:
                status, data = call("/__readiness__")
                assert status == 404 and data["available"] == ["anthropic"]
                break
            except OSError:
                assert time.monotonic() < deadline, "The exe did not open the configured port"
                time.sleep(.05)
        body = json.dumps({"model": "synthetic-exe", "system": private_body}).encode()
        for _ in range(2):
            status, response = call("/anthropic/v1/messages", payload=body,
                                    headers={"Authorization": credential, "Content-Type": "application/json"})
            assert status == 200 and response["usage"] == {"input_tokens": 10, "output_tokens": 3}
        assert len(requests) == 2
        assert all(path == "/v1/messages" and auth == credential and data["system"] == private_body
                   for path, auth, data in requests)
        assert call("/anthropic/v1/messages", payload=body, headers={"Host": "foreign.example"})[0] == 403
        assert call("/anthropic/v1/messages", payload=body, headers={"Origin": "https://foreign.example"})[0] == 403
        assert len(requests) == 2
        deadline = time.monotonic() + 10
        with closing(sqlite3.connect((home / "usage.db").as_uri() + "?mode=ro", uri=True)) as db:
            while True:
                totals = db.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens) FROM usage_events").fetchone()
                if totals == (1, 10, 3):
                    break
                assert time.monotonic() < deadline, totals
                time.sleep(.05)
    finally:
        try:
            if proc is not None and proc.poll() is None:
                try:
                    command = subprocess.run([executable, "--shutdown"], env=env,
                                             creationflags=CREATE_NO_WINDOW, timeout=30)
                    assert command.returncode == 0
                    assert proc.wait(timeout=20) == 0
                finally:
                    if proc.poll() is None:
                        kill_tree(proc)
                        proc.wait(timeout=10)
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)
    assert json.loads((home / "config.json").read_text(encoding="utf-8"))["proxy_port"] == port
    with closing(sqlite3.connect((home / "usage.db").as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens) FROM usage_events").fetchone() == (1, 10, 3)
    for path in (home / "usage.db", home / "tokenmeter.log"):
        assert credential.encode() not in path.read_bytes()
        assert private_body.encode() not in path.read_bytes()
