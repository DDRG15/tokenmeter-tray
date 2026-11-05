"""On-demand, read-only queries of local usage; no credentials, UI or disk DB."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import math
from pathlib import Path
import re
import threading
import time

from .collectors.codex import CodexCollector, load_rate_limits
from .collectors.transcripts import TranscriptCollector
from .dashboard import _models
from .history import daily_series, summarize
from .store import CLOCK_TOLERANCE_S, Store, Totals, merge_model_days


# Model and plan names are written by whatever produced a local record. The MCP
# caller is a language model, so free text from those records must not reach it
# as instructions or as an unbounded payload.
_IDENTIFIER = re.compile(r"[A-Za-z0-9._:/@+<>\[\]-]{1,64}")
UNRECOGNIZED = "unrecognized"
# A file last written before the window started cannot hold an event inside it;
# the margin absorbs clock skew between a record's timestamp and its write.
SCAN_MARGIN_S = 3600
SCOPE_NOTE = ("Rolling hours over usage files currently on disk. It does not apply the "
              "desktop app's start-fresh floor, retention or manual session reset, and "
              "cannot see transcripts already removed from disk.")


def _identifier(value, empty=""):
    if value in (None, ""):
        return empty
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else UNRECOGNIZED


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _limits(raw, now):
    """Rebuilds Codex limits with only identifiers and finite numbers.

    A window whose reset already passed no longer describes current usage, as
    the desktop panel shows; its percentage becomes null and it is marked expired.
    """
    if not raw:
        return None
    windows = {}
    for name in ("primary", "secondary"):
        window = raw.get(name)
        if not isinstance(window, dict):
            windows[name] = None
            continue
        values = {k: _number(window.get(k))
                  for k in ("used_percent", "window_minutes", "resets_at")}
        resets = values["resets_at"]
        values["expired"] = resets is not None and resets <= now
        if values["expired"]:
            values["used_percent"] = None
        windows[name] = values
    credits = raw.get("credits")
    return {"captured_ts": _number(raw.get("captured_ts")),
            "plan_type": _identifier(raw.get("plan_type")),
            **windows,
            "credits": {"has_credits": bool(isinstance(credits, dict)
                                            and credits.get("has_credits") is True)}}


class _UnknownBilling:
    def get(self):
        return "unknown", ""


class UsageQuery:
    def __init__(self, claude_root: Path | None, codex_root: Path | None):
        self.claude_root = claude_root
        self.codex_root = codex_root
        # One scan at a time: concurrent tool calls would multiply memory and I/O.
        self._lock = threading.Lock()

    @contextmanager
    def snapshot(self, modified_since: float | None = None):
        with self._lock, self._snapshot(modified_since) as result:
            yield result

    @contextmanager
    def _snapshot(self, modified_since):
        store = Store.in_memory()
        try:
            readers = {
                "transcript": (self.claude_root, TranscriptCollector),
                "codex": (self.codex_root, CodexCollector),
            }
            status = {}
            for source, (root, factory) in readers.items():
                item = {"enabled": root is not None, "state": "disabled",
                        "files": 0, "records": 0, "events_undated": 0,
                        "read_error": False}
                status[source] = item
                if root is None:
                    continue
                try:
                    if not root.is_dir():
                        item["state"] = "missing"
                        continue
                    reader = (factory(store, root, billing=_UnknownBilling())
                              if source == "transcript" else
                              factory(store, root, read_authentication=False))
                    reader.modified_since = modified_since
                    item["records"] = reader.scan_once()
                    item["files"] = reader.stats.files_seen
                    item["events_undated"] = reader.stats.events_undated
                    item["read_error"] = bool(reader.stats.last_error)
                    item["state"] = "error" if item["read_error"] else "ready"
                except Exception:
                    # Parsing failures must not send a path, message or content
                    # fragment from a transcript to the MCP caller or stderr.
                    item["state"], item["read_error"] = "error", True
            # A one-shot file scan cannot prove continuous observation or zero
            # usage in gaps. Preserve observed days as partial, gaps as unknown.
            with store._lock:
                store._conn.execute("DELETE FROM coverage")
                store._conn.commit()
            yield store, status
        finally:
            store.close()

    @staticmethod
    def _filters(provider, source):
        if provider not in ("", "anthropic", "openai"):
            raise ValueError("provider must be anthropic, openai or empty")
        if source not in ("", "transcript", "codex"):
            raise ValueError("source must be transcript, codex or empty")

    def summary(self, hours=5, provider="", source="", *, now=None):
        if type(hours) is not int or not 1 <= hours <= 2160:
            raise ValueError("hours must be an integer between 1 and 2160")
        self._filters(provider, source)
        now = time.time() if now is None else now
        since = now - hours * 3600
        with self.snapshot(since - SCAN_MARGIN_S) as (store, status):
            day_rows = [replace(row, model=_identifier(row.model, UNRECOGNIZED),
                                plan=_identifier(row.plan))
                        for row in store.totals_by_model_day(since, provider, source,
                                                             until_ts=now + CLOCK_TOLERANCE_S)]
            rows = merge_model_days(day_rows)
            totals = Totals(**{name: sum(getattr(row, name) for row in rows)
                               for name in Totals.__dataclass_fields__})
            models, cost, billing = _models(day_rows)
            return {
                "window": {"hours": hours, "since_ts": since, "until_ts": now,
                           "kind": "rolling", "scope_note": SCOPE_NOTE},
                "filters": {"provider": provider, "source": source},
                "totals": {**asdict(totals), "io_tokens": totals.io_tokens,
                           "all_tokens": totals.all_tokens},
                "models": models, "model_groups": len(rows),
                "models_truncated": len(models) < len(rows),
                "api_equivalent": cost, "billing": billing,
                "codex_limits": _limits(load_rate_limits(store), now) if provider != "anthropic"
                                 and source != "transcript" else None,
                "collectors": status, "observed_only": True,
                "billing_note": "Credentials are never read; billing may be unknown. "
                                "API equivalent is an estimate, not an invoice.",
            }

    def history(self, days=7, basis="io", provider="", source="", *, now=None):
        if type(days) is not int or days not in (7, 30, 90):
            raise ValueError("days must be 7, 30 or 90")
        if basis not in ("io", "all"):
            raise ValueError("basis must be io or all")
        self._filters(provider, source)
        now = time.time() if now is None else now
        cutoff = now - (days + 1) * 86400 - SCAN_MARGIN_S
        with self.snapshot(cutoff) as (store, status):
            bars = daily_series(store, days, basis, now=now, provider=provider, source=source)
            summary = asdict(summarize(bars))
            peak = summary["peak_day"]
            summary["peak_day"] = peak.isoformat() if peak else None
            return {
                "days": days, "basis": basis,
                "filters": {"provider": provider, "source": source},
                "bars": [{"day": b.day.isoformat(), "tokens": b.tokens,
                          "calls": b.calls, "coverage": b.coverage,
                          "incomplete_calls": b.incomplete_calls} for b in bars],
                "summary": summary, "collectors": status, "observed_only": True,
                # Days are local calendar days of the process running this server;
                # the offset is the one in force at the query's `now`, not today's.
                "utc_offset_seconds": time.localtime(now).tm_gmtoff,
                "coverage_note": "A gap is unknown, not zero; this server does not watch continuously.",
            }

    def status(self):
        with self.snapshot() as (_, status):
            return {"collectors": status, "collection_mode": "on_demand",
                    "storage": "memory", "read_only": True,
                    "credentials_read": False, "proxy_included": False}
