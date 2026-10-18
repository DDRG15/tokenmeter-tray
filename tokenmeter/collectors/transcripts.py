"""Reader for Claude Code's JSONL transcripts.

Claude Code writes **one line per content block**, all with the same
`requestId` and the same `usage` object repeated. Measured on a real
transcript: 74 lines for 44 calls, and summing them all inflates output tokens
2.04×. Deduplicating by `requestId` is not a defense against the other
collector: it is what makes this one correct.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ..billing import CachedBilling, apply_override
from ..models import UsageEvent
from ..store import Store
from .base import Collector

log = logging.getLogger(__name__)


@dataclass
class TranscriptStats:
    files_seen: int = 0
    lines_read: int = 0
    events_new: int = 0
    events_duplicate: int = 0
    last_scan_ts: float = 0.0
    last_error: str = ""


def parse_timestamp(raw: object) -> float | None:
    """`2026-08-12T15:33:17.376Z` → unix. Returns None if it cannot be parsed."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def event_from_line(line: str) -> UsageEvent | None:
    """Converts a transcript line into a `UsageEvent`, or None if it does not apply."""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None

    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    def num(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    # `cache_creation` breaks the write down by TTL. The 1-hour one costs 2x the
    # input versus 1.25x for the 5-minute one, so without this breakdown the
    # cache cost comes out systematically low.
    creation = usage.get("cache_creation")
    write_1h = 0
    if isinstance(creation, dict):
        value = creation.get("ephemeral_1h_input_tokens")
        if isinstance(value, int) and not isinstance(value, bool):
            write_1h = value

    ts = parse_timestamp(obj.get("timestamp")) or time.time()

    request_id = obj.get("requestId")
    message_id = message.get("id")
    if isinstance(request_id, str) and request_id:
        # Same format as the `request-id` header the proxy sees: it is what lets
        # both collectors recognize the same call.
        dedupe_key = f"req:{request_id}"
    elif isinstance(message_id, str) and message_id:
        dedupe_key = f"msg:{message_id}"
    else:
        return None  # without an identifier it cannot be deduplicated; better not to count it

    return UsageEvent(
        ts=ts,
        provider="anthropic",
        model=message.get("model") or "unknown",
        input_tokens=num("input_tokens"),
        output_tokens=num("output_tokens"),
        cache_write_tokens=num("cache_creation_input_tokens"),
        cache_read_tokens=num("cache_read_input_tokens"),
        cache_write_1h_tokens=write_1h,
        source="transcript",
        dedupe_key=dedupe_key,
        usage_complete=True,
        aliases=tuple(k for k in (
            f"req:{request_id}" if isinstance(request_id, str) and request_id else None,
            f"msg:{message_id}" if isinstance(message_id, str) and message_id else None,
        ) if k),
    )


class TranscriptCollector(Collector):
    name = "transcripts"

    def __init__(self, store: Store, root: Path, poll_seconds: float = 2.0, *,
                 billing=None, overrides: dict | None = None) -> None:
        super().__init__()
        self._store = store
        self._root = root
        self._poll = max(0.25, poll_seconds)
        self.stats = TranscriptStats()
        # The lines do not say how the call was paid: Claude Code's
        # authentication on disk is used. `billing` is injectable for tests.
        self._billing = billing or CachedBilling()
        self._overrides = overrides
        self._retagged = False
        self._covered_until: float | None = None

    def run(self) -> None:
        while not self._stop.is_set():
            if not self.paused:
                try:
                    self.scan_once()
                except Exception as exc:
                    self.stats.last_error = str(exc)
                    log.exception("failed scanning transcripts")
            self._stop.wait(self._poll)

    # -- scanning -----------------------------------------------------------

    def scan_once(self) -> int:
        """Reads what is new in each transcript. Returns the events actually added."""
        self.stats.last_error = ""
        if not self._root.exists():
            return 0
        if not self._retagged:
            self._retag_unknown()
        added = 0
        files = 0
        oldest = None
        for path in sorted(self._root.glob("**/*.jsonl")):
            files += 1
            if self._covered_until is None:
                try:
                    mtime = path.stat().st_mtime
                    oldest = mtime if oldest is None else min(oldest, mtime)
                except OSError:
                    pass
            try:
                added += self._read_new(path)
            except OSError as exc:
                # File locked or deleted midway: retried on the next cycle.
                self.stats.last_error = f"{path.name}: {exc}"
        self.stats.files_seen = files
        now = time.time()
        self.stats.last_scan_ts = now
        self._mark_coverage(oldest, now)
        return added

    def _retag_unknown(self) -> None:
        """Events stored before billing was tracked were left as `unknown`.

        They are tagged once with Claude Code's current authentication: it is the
        same assumption any transcript backfill makes (the lines do not say how
        the call was paid). If the authentication cannot be inferred, they stay
        as they are.
        """
        self._retagged = True
        kind, plan = apply_override("anthropic", self._billing.get(), self._overrides)
        if kind == "unknown":
            return
        try:
            n = self._store.retag_billing("transcript", kind, plan)
            if n:
                log.info("%s transcript events tagged as %s", n, kind)
        except Exception:
            log.debug("could not retag events", exc_info=True)

    def _mark_coverage(self, oldest: float | None, now: float) -> None:
        """Coverage of what the transcripts on disk guarantee.

        On the first scan, from the oldest file still on disk: Claude Code
        deletes by age, so all usage after that file is in some surviving
        transcript. After that, each scan extends up to now.
        """
        start = self._covered_until if self._covered_until is not None else (oldest or now)
        try:
            self._store.mark_covered(start, now, gap=0,
                                     source="transcript_archive" if self._covered_until is None else "transcript")
        except Exception:
            log.debug("could not record the coverage", exc_info=True)
        self._covered_until = now

    def _read_new(self, path: Path) -> int:
        key = str(path)
        size = path.stat().st_size
        known_size, offset = self._store.get_offset(key)

        if size < known_size or offset > size:
            # The file shrank: rotated or rewritten. It is reread in full.
            offset = 0
        if size == offset:
            return 0

        with path.open("rb") as fh:
            fh.seek(offset)
            blob = fh.read()

        # Only complete lines are consumed: the last one may be half-written
        # while Claude Code is still flushing its buffer.
        last_newline = blob.rfind(b"\n")
        if last_newline == -1:
            self._store.set_offset(key, size, offset)
            return 0
        consumed = blob[: last_newline + 1]
        new_offset = offset + last_newline + 1

        batch = []
        for raw in consumed.split(b"\n"):
            if not raw.strip():
                continue
            self.stats.lines_read += 1
            # Byte pre-filter: most lines are user turns and tool results.
            # Discarding them avoids parsing their JSON.
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            event = event_from_line(raw.decode("utf-8", errors="replace"))
            if event is not None:
                batch.append(event)

        if batch:
            kind, plan = apply_override("anthropic", self._billing.get(), self._overrides)
            batch = [replace(e, billing=kind, plan=plan) for e in batch]

        # A single commit per file, not one per event.
        added = self._store.add_events(batch)
        self.stats.events_new += added
        self.stats.events_duplicate += len(batch) - added

        self._store.set_offset(key, size, new_offset)
        return added
