"""Common collector contract: a daemon thread that pushes to the store."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

# SQLite stores 64-bit integers and `SUM()` raises "integer overflow" past
# 2**63 - 1, which would abort every aggregate query over the table. The cap is
# per count, so it also has to leave room for many rows: at 2**40 (about 1.1e12,
# far above any real call) it takes 2**63 / 2**40 = 2**23 (about 8.4 M) rows at
# the cap before a column sum can overflow.
MAX_COUNT = 2**40


def bounded_count(value: object) -> int:
    """A token count from a record: a non-negative int, 0 when absent or malformed."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(0, value), MAX_COUNT)


class Collector:
    name = "collector"
    # Desktop collectors watch everything; an on-demand reader may set a cutoff
    # and skip files whose modification time is older.
    modified_since: float | None = None

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._guarded_run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        if self.paused and hasattr(self, "_covered_until"):
            self._covered_until = None
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    # -- implementation ----------------------------------------------------

    def _guarded_run(self) -> None:
        try:
            self.run()
        except Exception:
            # A dead collector must not take the whole app down: the icon has
            # to keep showing what was already counted.
            log.exception("collector %s ended with an error", self.name)

    def run(self) -> None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError
