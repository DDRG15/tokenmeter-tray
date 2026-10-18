"""Reader for Codex's JSONL rollouts (`~/.codex`).

Codex writes one rollout per session in `sessions/YYYY/MM/DD/rollout-*.jsonl`
and, when archiving it, moves it to `archived_sessions/`. Each line is
`{"timestamp", "type", "payload"}`. Only four types matter:

- `turn_context`: carries the turn's model. It does NOT carry the usage, so the
  two have to be matched by `turn_id`.
- `token_usage_record`: one line per model response, with a unique
  `response_id`. It is the only counting source.
- `event_msg` / `token_count`: carries the plan's real `rate_limits`. Its
  `info.total_token_usage` is NOT used for counting: it repeats what
  `token_usage_record` already says and adding it would duplicate it.
- `turn_token_usage` and `thread_token_usage` (inside the record) are
  cumulative; summing them would count each response several times.

Token semantics: `total_tokens == input_tokens + output_tokens`.
As in OpenAI, cached tokens go INSIDE `input` and
reasoning INSIDE `output`. That is why the cached tokens are subtracted from the
input here (same as `extract_openai`) and reasoning is not added separately.

`session_meta` carries huge `base_instructions` and user ids, and `auth.json`
carries tokens and keys. Only `auth_mode` is read from the latter.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..models import (
    BILLING_API,
    BILLING_SUBSCRIPTION,
    BILLING_UNKNOWN,
    UsageEvent,
)
from ..store import Store
from .base import Collector
from .transcripts import parse_timestamp

log = logging.getLogger(__name__)

STATE_RATE_LIMITS = "codex_rate_limits"

# Byte pre-filter: almost all lines are messages and tool calls, and parsing
# their JSON is the bulk of a backfill's cost.
_MARKERS = (b'"turn_context"', b'"token_usage_record"', b'"rate_limits"')


def default_codex_root() -> Path:
    """`~/.codex`, or whatever `CODEX_HOME` says (that is how Codex itself resolves it)."""
    home = os.environ.get("CODEX_HOME")
    return Path(home) if home else Path.home() / ".codex"


@dataclass
class CodexStats:
    files_seen: int = 0
    lines_read: int = 0
    events_new: int = 0
    events_duplicate: int = 0
    last_scan_ts: float = 0.0
    last_error: str = ""


@dataclass
class _FileContext:
    """What has to be remembered about a rollout to interpret its new lines.

    Usage and model live on different lines; with incremental reading, the
    `turn_context` may have been read in an earlier scan (or in another process).
    """

    models: dict[str, str] = field(default_factory=dict)  # turn_id → model
    plan: str = ""  # last `plan_type` seen in this file


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def read_auth_mode(root: Path) -> str:
    """`auth_mode` from `auth.json` in lowercase, or "" if it cannot be read.

    It is the only thing taken from that file. The rest (tokens,
    `OPENAI_API_KEY`) is neither kept nor logged. A read failure is swallowed
    without logging: the `json` error message may quote a fragment of the content.
    """
    try:
        data = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    mode = data.get("auth_mode") if isinstance(data, dict) else None
    return mode.strip().lower() if isinstance(mode, str) else ""


def billing_from_auth_mode(mode: str) -> tuple[str, str]:
    """(billing, plan) according to the login mode. Without a clear signal, `unknown`."""
    if mode.startswith("chatgpt"):
        return BILLING_SUBSCRIPTION, ""
    if mode in ("apikey", "api_key"):
        return BILLING_API, ""
    return BILLING_UNKNOWN, ""


def event_from_record(
    obj: dict, model: str, billing: str, plan: str
) -> UsageEvent | None:
    """Converts a `token_usage_record` into a `UsageEvent`, or None if it does not apply."""
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    response_id = payload.get("response_id")
    if not isinstance(usage, dict) or not isinstance(response_id, str) or not response_id:
        return None  # without a response_id it cannot be deduplicated; better not to count it
    if "input_tokens" not in usage and "output_tokens" not in usage:
        return None

    total_in = _int(usage.get("input_tokens"))
    cached = _int(usage.get("cached_input_tokens"))
    written = _int(usage.get("cache_write_input_tokens"))

    # Cached tokens are inside `input_tokens`: they are separated so they are
    # not counted twice in the total.
    net_input = max(0, total_in - cached)
    # The write would also be inside the input (inferred, as with OpenRouter).
    # If it does not fit in what is left after subtracting the cached tokens,
    # the assumption is false for this response and nothing is touched (same
    # safeguard as in the OpenRouter parser).
    cache_write = 0
    if written and written <= net_input:
        cache_write = written
        net_input -= written

    ts = parse_timestamp(obj.get("timestamp")) or time.time()
    return UsageEvent(
        ts=ts,
        provider="openai",
        model=model or "unknown",
        input_tokens=net_input,
        output_tokens=_int(usage.get("output_tokens")),
        cache_write_tokens=cache_write,
        cache_read_tokens=cached,
        source="codex",
        dedupe_key=f"codex:{response_id}",
        aliases=(f"msg:{response_id}",),
        usage_complete=True,
        billing=billing,
        plan=plan,
    )


def _window(raw: object) -> dict | None:
    """Copies only the three fields of a limit window."""
    if not isinstance(raw, dict):
        return None
    return {k: raw.get(k) for k in ("used_percent", "window_minutes", "resets_at")}


def load_rate_limits(store: Store) -> dict | None:
    """Latest stored real Codex limits, or None if there are none or they are broken."""
    raw = store.get_state(STATE_RATE_LIMITS)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("captured_ts"), (int, float)):
        return None
    return data


class CodexCollector(Collector):
    name = "codex"

    def __init__(self, store: Store, root: Path, poll_seconds: float = 5.0) -> None:
        super().__init__()
        self._store = store
        self._root = root
        self._poll = max(0.25, poll_seconds)
        self.stats = CodexStats()
        self._covered_until: float | None = None
        self._contexts: dict[str, _FileContext] = {}
        self._auth_mode = ""

    def run(self) -> None:
        while not self._stop.is_set():
            if not self.paused:
                try:
                    self.scan_once()
                except Exception as exc:
                    self.stats.last_error = str(exc)
                    log.exception("failed scanning Codex rollouts")
            self._stop.wait(self._poll)

    # -- scanning -----------------------------------------------------------

    def _rollouts(self) -> list[Path]:
        # An archived rollout may still be in `sessions/` or be in both; dedupe
        # by response_id avoids double counting.
        found: list[Path] = []
        for sub in ("sessions", "archived_sessions"):
            base = self._root / sub
            if base.is_dir():
                found.extend(base.glob("**/*.jsonl"))
        return sorted(found)

    def scan_once(self) -> int:
        """Reads what is new in each rollout. Returns the events actually added."""
        self.stats.last_error = ""
        if not self._root.exists():
            return 0
        self._auth_mode = read_auth_mode(self._root)
        added = 0
        files = 0
        oldest = None
        for path in self._rollouts():
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

    def _mark_coverage(self, oldest: float | None, now: float) -> None:
        """Coverage of what the rollouts on disk guarantee.

        Same criterion as the transcripts: on the first scan, from the oldest
        file still on disk; after that, up to each scan.
        """
        start = self._covered_until if self._covered_until is not None else (oldest or now)
        try:
            self._store.mark_covered(start, now, gap=0,
                                     source="codex_archive" if self._covered_until is None else "codex")
        except Exception:
            log.debug("could not record the coverage", exc_info=True)
        self._covered_until = now

    # -- incremental reading -------------------------------------------------

    def _read_new(self, path: Path) -> int:
        key = str(path)
        size = path.stat().st_size
        known_size, offset = self._store.get_offset(key)

        if size < known_size or offset > size:
            # The file shrank: rotated or rewritten. It is reread in full and the
            # context, which no longer matches the content, is forgotten.
            offset = 0
            self._contexts.pop(key, None)
        if size == offset:
            return 0

        ctx = self._contexts.get(key)
        with path.open("rb") as fh:
            if offset > 0 and ctx is None:
                # The turn_id → model map lives in memory and is lost between
                # processes. Without it, new responses of an already open turn
                # would come out as "unknown". What was already consumed is
                # reread only to rebuild it, without emitting events.
                ctx = _FileContext()
                self._digest(fh.read(offset), ctx, emit=False)
            elif ctx is None:
                ctx = _FileContext()
            fh.seek(offset)
            blob = fh.read()
        self._contexts[key] = ctx

        # Only complete lines are consumed: the last one may be half-written
        # while Codex is still flushing its buffer.
        last_newline = blob.rfind(b"\n")
        if last_newline == -1:
            self._store.set_offset(key, size, offset)
            return 0
        consumed = blob[: last_newline + 1]
        new_offset = offset + last_newline + 1

        batch, limits = self._digest(consumed, ctx, emit=True)
        if limits:
            self._save_rate_limits(limits)

        # A single commit per file, not one per event.
        added = self._store.add_events(batch)
        self.stats.events_new += added
        self.stats.events_duplicate += len(batch) - added

        self._store.set_offset(key, size, new_offset)
        return added

    def _digest(
        self, data: bytes, ctx: _FileContext, emit: bool
    ) -> tuple[list[UsageEvent], list[tuple[float, dict]]]:
        """Processes complete lines. With `emit=False` it only updates `ctx`.

        Two passes: the `plan_type` may appear (in a `token_count`) after the
        record that has to be tagged in the same batch, and a turn may have been
        opened in the previous batch. First the context is gathered; then the
        events are built.
        """
        records: list[dict] = []
        limits: list[tuple[float, dict]] = []
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            if emit:
                self.stats.lines_read += 1
            if not any(marker in raw for marker in _MARKERS):
                continue
            try:
                obj = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue  # broken line (interrupted dump): simply ignored
            if not isinstance(obj, dict):
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = obj.get("type")
            if kind == "turn_context":
                turn_id, model = payload.get("turn_id"), payload.get("model")
                if isinstance(turn_id, str) and turn_id and isinstance(model, str) and model:
                    ctx.models[turn_id] = model
            elif kind == "token_usage_record":
                if emit:
                    records.append(obj)
            elif kind == "event_msg" and payload.get("type") == "token_count":
                rate = payload.get("rate_limits")
                if not isinstance(rate, dict):
                    continue
                plan = rate.get("plan_type")
                if isinstance(plan, str) and plan.strip():
                    ctx.plan = plan.strip()
                ts = parse_timestamp(obj.get("timestamp"))
                # Codex reports several buckets: `codex` carries the 5 h and weekly
                # windows; `premium` arrives without windows. Without this filter,
                # the last `premium` overwrote the good data with empty windows.
                has_window = isinstance(rate.get("primary"), dict) or isinstance(
                    rate.get("secondary"), dict)
                if emit and ts is not None and has_window:
                    limits.append((ts, rate))

        events: list[UsageEvent] = []
        if records:
            if ctx.plan:
                billing, plan = BILLING_SUBSCRIPTION, ctx.plan
            else:
                billing, plan = billing_from_auth_mode(self._auth_mode)
            for obj in records:
                turn_id = obj["payload"].get("turn_id")
                model = ctx.models.get(turn_id, "") if isinstance(turn_id, str) else ""
                event = event_from_record(obj, model, billing, plan)
                if event is not None:
                    events.append(event)
        return events, limits

    # -- real limits --------------------------------------------------------

    def _save_rate_limits(self, limits: list[tuple[float, dict]]) -> None:
        """Stores the most recent `rate_limits` across all files.

        Rollouts are read in name order, not time order, and archived ones repeat
        old sessions: without comparing by timestamp, an old file would overwrite
        the fresh data with a stale one.
        """
        ts, rate = max(limits, key=lambda item: item[0])
        stored = load_rate_limits(self._store)
        # One stored without windows (the `premium` bucket, from an earlier
        # version) is not data: it is replaced even if it is more recent.
        stored_useful = stored is not None and (stored.get("primary") or stored.get("secondary"))
        if stored_useful and stored["captured_ts"] >= ts:
            return
        credits = rate.get("credits")
        snapshot = {
            "captured_ts": ts,
            "plan_type": rate.get("plan_type") if isinstance(rate.get("plan_type"), str) else "",
            "primary": _window(rate.get("primary")),
            "secondary": _window(rate.get("secondary")),
            "credits": {
                "has_credits": bool(credits.get("has_credits")) if isinstance(credits, dict) else False
            },
        }
        self._store.set_state(STATE_RATE_LIMITS, json.dumps(snapshot))
