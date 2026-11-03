"""Resources and state for a desktop shell without importing a GUI toolkit."""
from __future__ import annotations

import queue

from .alerts import AlertTracker
from .collectors.codex import CodexCollector
from .collectors.proxy import ProxyCollector
from .collectors.transcripts import TranscriptCollector
from .config import Config, data_dir
from .session import SessionTracker
from .state import RuntimeState
from .store import Store
from .weekly import WeeklyTracker


class Runtime(RuntimeState):
    def __init__(self, config: Config, *, transcript_billing=None):
        self.config = config
        self._actions = queue.Queue()
        self.transcripts = self.codex = self.proxy = None
        self._started = False
        self._closed = False
        self._transcript_billing = transcript_billing
        self.store = Store(data_dir() / "usage.db", retain_days=config.retain_days)
        try:
            self.session = SessionTracker(self.store, config)
            self.weekly = WeeklyTracker(self.store, config)
            self.alerts = AlertTracker(self.store, tuple(config.alert_thresholds))
        except BaseException:
            self.store.close()
            raise

    def enqueue(self, action):
        if not self._closed:
            self._actions.put(action)

    def start(self):
        if self._closed:
            raise RuntimeError("Cannot restart a closed runtime")
        if self._started:
            return
        self._started = True
        try:
            self.store.prune(self.config.retain_days)
            if self.config.transcripts_enabled:
                self.transcripts = TranscriptCollector(
                    self.store, self.config.transcripts_path(),
                    self.config.transcripts_poll_seconds,
                    billing=self._transcript_billing,
                    overrides=self.config.billing_overrides)
                self.transcripts.start()
            if self.config.codex_enabled:
                self.codex = CodexCollector(self.store, self.config.codex_path(),
                                            self.config.codex_poll_seconds)
                self.codex.start()
            if self.config.proxy_enabled:
                self.proxy = ProxyCollector(
                    self.store, self.config.proxy_host, self.config.proxy_port,
                    self.config.upstreams,
                    inject_stream_usage=self.config.inject_stream_usage,
                    billing_overrides=self.config.billing_overrides)
                self.proxy.start()
                if not self.proxy.wait_ready(5):
                    raise RuntimeError(self.proxy.bind_error or "Proxy did not start")
        except BaseException:
            self.shutdown()
            raise

    def toggle_pause(self):
        paused = self.collectors_paused
        for collector in self._collectors():
            collector.resume() if paused else collector.pause()

    def shutdown(self):
        if self._closed:
            return
        failures = []
        for collector in reversed(self._collectors()):
            try:
                collector.stop()
            except Exception as exc:
                failures.append(f"{collector.name}: {exc}")
            if collector.running:
                failures.append(f"{collector.name}: worker is still running")
        if failures:
            # The owner must report failure and stop its process. Never close
            # SQLite underneath a collector that can still be writing to it.
            raise RuntimeError("Shutdown incomplete: " + "; ".join(failures))
        self.store.close()
        self._closed = True
