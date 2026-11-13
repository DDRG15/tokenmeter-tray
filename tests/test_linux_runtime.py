"""Linux lifecycle and native prototype checks with only synthetic data."""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

from tokenmeter.config import Config
from tokenmeter.dashboard import build_state
from tokenmeter.models import UsageEvent
from tokenmeter.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path))
    return Config(proxy_enabled=False, transcripts_enabled=False, codex_enabled=False,
                  transcripts_root=str(tmp_path / "claude"),
                  codex_root=str(tmp_path / "codex"))


def test_cli_import_and_help_need_no_gui_or_user_data(isolated, tmp_path):
    code = ("import sys; import tokenmeter.__main__ as c; "
            "assert not {'tkinter', 'gi', 'webview', 'pystray'} & set(sys.modules); "
            "c.main(['--help'])")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())



def test_linux_embedded_web_window_is_rejected_before_writing_data(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux CLI guard")
    env = dict(os.environ, TOKENMETER_HOME=str(tmp_path / "must-not-exist"))
    result = subprocess.run([sys.executable, "-m", "tokenmeter", "--window", "http://127.0.0.1/"], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "Windows-only" in result.stderr
    assert not (tmp_path / "must-not-exist").exists()

def test_runtime_uses_the_shared_state_and_closes_idempotently(isolated):
    runtime = Runtime(isolated)
    try:
        runtime.store.add_event(UsageEvent(
            ts=time.time(), provider="anthropic", model="synthetic-model",
            input_tokens=1200, output_tokens=300, source="transcript",
            billing="subscription", dedupe_key="req:linux-test"))
        runtime.start()
        state = build_state(runtime)
        assert state["tokens"]["io"] == 1500
        assert state["session"]["used"] == 1500
        assert state["models"][0]["io"] == 1500
    finally:
        runtime.shutdown()
    runtime.shutdown()
    assert runtime.store.closed
    with pytest.raises(RuntimeError, match="closed runtime"):
        runtime.start()


def test_partial_start_failure_closes_acquired_resources(isolated, monkeypatch):
    class BrokenCollector:
        name = "broken"
        running = False
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            self.running = True
            raise ValueError("synthetic startup failure")
        def stop(self):
            self.running = False
    isolated.transcripts_enabled = True
    monkeypatch.setattr("tokenmeter.runtime.TranscriptCollector", BrokenCollector)
    runtime = Runtime(isolated)
    with pytest.raises(ValueError, match="synthetic startup failure"):
        runtime.start()
    assert not runtime.transcripts.running
    assert runtime.store.closed


def test_failed_worker_shutdown_keeps_database_open_for_retry(isolated):
    class Stuck:
        name = "stuck"
        running = True
        def stop(self):
            pass
    runtime = Runtime(isolated)
    runtime.transcripts = Stuck()
    try:
        with pytest.raises(RuntimeError, match="worker is still running"):
            runtime.shutdown()
        assert not runtime.store.closed
    finally:
        runtime.transcripts.running = False
        runtime.shutdown()
    assert runtime.store.closed


def test_real_proxy_releases_its_port_and_worker(isolated):
    isolated.proxy_enabled = True
    isolated.proxy_port = 0
    isolated.upstreams = {}
    runtime = Runtime(isolated)
    try:
        runtime.start()
        port = runtime.proxy.port
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    finally:
        runtime.shutdown()
    assert not runtime.proxy.running
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux dependency metadata contract")
def test_linux_dependency_manifest_matches_the_test_environment():
    for filename in ("requirements-linux.txt", "requirements-linux-dev.txt"):
        for line in (ROOT / filename).read_text().splitlines():
            if line and not line.startswith(("#", "-r")):
                name, version = line.split("==")
                assert importlib.metadata.version(name) == version


@pytest.mark.skipif(sys.platform != "linux" or not os.environ.get("DISPLAY"),
                    reason="requires an explicit Linux graphical test session")
@pytest.mark.parametrize("scenario", ["quit-menu", "close-window", "sigterm", "shutdown-request", "startup-error"])
def test_native_shell_exits_with_all_resources_closed(tmp_path, scenario):
    for folder in ("tmp", "cache", "config", "share", "state", "data"):
        (tmp_path / folder).mkdir()
    env = dict(os.environ, TOKENMETER_HOME=str(tmp_path / "data"),
               TMPDIR=str(tmp_path / "tmp"), XDG_CACHE_HOME=str(tmp_path / "cache"),
               XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_DATA_HOME=str(tmp_path / "share"),
               XDG_STATE_HOME=str(tmp_path / "state"), PYTHONDONTWRITEBYTECODE="1",
               GSETTINGS_BACKEND="memory", GIO_USE_VFS="local", NO_AT_BRIDGE="1")
    output = tmp_path / "result.json"
    # Own the entire test process group; never target another desktop process.
    with (tmp_path / "child.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.linux_smoke_app", str(output), scenario],
            cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            code = process.wait(timeout=20)
        finally:
            # The shell must not spawn children; also remove any unexpectedly
            # remaining member of our own group if a regression does so.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
    assert code == 0, (tmp_path / "child.log").read_text()
    result = json.loads(output.read_text())
    assert result["closed"] and result["workers"] == []
    assert result["webview_loaded"] is False
    assert not list((tmp_path / "data").glob("tray-*"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", result["port"]))
    if scenario != "startup-error":
        assert result["visible"] and result["menu_active"]
        assert result["pause_verified"] and result["reopen_verified"]
        assert "Input 1,200" in result["summary"]


def test_the_same_model_under_two_billing_types_reads_as_two_lines():
    from tokenmeter.ui.linux import model_line

    base = {"provider": "anthropic", "model": "claude-opus-5-5", "io": 1500, "cost_text": "$0.03"}
    lines = [model_line({**base, "billing": "subscription", "plan": "max"}, show_cost=True),
             model_line({**base, "billing": "api", "plan": ""}, show_cost=False)]
    assert lines == [
        "anthropic · claude-opus-5-5 (Subscription · Max): 1,500 tokens · $0.03 API equivalent",
        "anthropic · claude-opus-5-5 (Paid API): 1,500 tokens",
    ]
