"""Exercises the production proxy with two clients and a real HTTP upstream."""

import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from tokenmeter.collectors.proxy import ProxyCollector
from tokenmeter.store import Store


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self,*args):
        pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length","0")))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        payload = json.loads(body or b"{}")
        result = json.dumps({"id":"msg_"+self.path, "model":"m", "usage":{"input_tokens":10,"output_tokens":2},
                             "cookie":self.headers.get("Cookie"), "payload":payload,
                             "encoding":self.headers.get("Content-Encoding")}).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(result)))
        if self.path == "/set":
            self.send_header("Set-Cookie","session=user_a; Path=/")
            self.send_header("Set-Cookie","other=two; Path=/")
        self.end_headers()
        self.wfile.write(result)


@pytest.fixture
def setup_proxy(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1",0),Upstream)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    store = Store(tmp_path/"u.db")
    proxy = ProxyCollector(store,"127.0.0.1",0,{"test":f"http://localhost:{upstream.server_port}"})
    proxy.start()
    assert proxy.wait_ready()
    yield proxy,store
    proxy.stop()
    upstream.shutdown()
    upstream.server_close()
    store.close()


def post(proxy,path,body=b"{}",headers=None):
    connection = http.client.HTTPConnection("127.0.0.1",proxy.port,timeout=5)
    try:
        connection.request("POST","/test"+path,body=body,headers=headers or {})
        response = connection.getresponse()
        status, headers, body = response.status,response.getheaders(),response.read()
        return status,headers,body
    finally:
        connection.close()


@pytest.mark.parametrize("inject",[False,True])
def test_gzip_remains_valid_with_and_without_injection(setup_proxy,inject):
    proxy,_ = setup_proxy
    proxy._inject = inject
    body = gzip.compress(json.dumps({"model":"m","stream":True}).encode())
    status,_,received = post(proxy,"/v1/chat/completions",body,{"Content-Encoding":"gzip","Content-Type":"application/json"})
    assert status == 200
    payload = json.loads(received)
    assert payload["encoding"] == "gzip"
    assert payload["payload"].get("stream_options",{}).get("include_usage",False) == inject


def test_cookies_are_not_shared_and_repeated_headers_survive(setup_proxy):
    proxy,_ = setup_proxy
    status,headers,_ = post(proxy,"/set")
    assert status == 200
    assert len([v for k,v in headers if k.lower()=="set-cookie"]) == 2
    assert json.loads(post(proxy,"/other")[2])["cookie"] is None
    assert json.loads(post(proxy,"/other",headers={"Cookie":"client=explicit"})[2])["cookie"] == "client=explicit"


def test_pause_keeps_passthrough_but_does_not_record(setup_proxy):
    proxy,store = setup_proxy
    proxy.pause()
    assert post(proxy,"/v1/messages")[0] == 200
    time.sleep(.05)
    assert store.totals_since(0).calls == 0
    proxy.resume()
    assert post(proxy,"/v1/messages")[0] == 200
    for _ in range(50):
        if store.totals_since(0).calls:
            break
        time.sleep(.02)
    assert store.totals_since(0).calls == 1


@pytest.mark.parametrize("headers",[{"Host":"external.example"},{"Origin":"https://external.example"},{"Origin":"null"}])
def test_foreign_hosts_and_origins_are_rejected(setup_proxy,headers):
    proxy,_ = setup_proxy
    assert post(proxy,"/v1/messages",headers=headers)[0] == 403
