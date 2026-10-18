"""Export of normalized usage with period, filters and coverage."""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime

from .dashboard import build_history
from .history import RANGES_DAYS, local_midnight


def export_usage(app, days: int = 30, *, provider: str = "", source: str = "",
                 format: str = "json", now: float | None = None) -> tuple[bytes, str, str]:
    if format not in {"csv", "json"} or days not in RANGES_DAYS:
        raise ValueError("Invalid export format or period")
    now = time.time() if now is None else now
    # The same snapshot keeps the detail and its summary consistent.
    with app.store._lock:
        history = build_history(app, days, now, provider, source)
        first = datetime.fromisoformat(history["bars"][0]["day"]).date()
        rows = app.store.events_between(local_midnight(first), now, provider, source)
    stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    filename = f"tokenmeter-{stamp}-{days}d.{format}"
    if format == "json":
        payload = {"format": "tokenmeter-export-v1", "exported_at": now,
                   "period": {"since": local_midnight(first), "until": now},
                   "filters": {"provider": provider, "source": source},
                   "basis": app.config.percent_basis,
                   "summary": history["summary"], "days": history["bars"], "events": rows}
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"), "application/json", filename
    output = io.StringIO(newline="")
    columns = ["timestamp", "provider", "model", "source", "billing", "plan",
               "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens",
               "cache_write_1h_tokens", "usage_complete", "coverage"]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    coverage = {b["day"]: b["coverage"] for b in history["bars"]}
    for event in rows:
        when = datetime.fromtimestamp(event["ts"]).astimezone()
        row = {k: event[k] for k in columns if k in event}
        row["timestamp"] = when.isoformat()
        row["coverage"] = coverage.get(when.date().isoformat(), "unknown")
        # Avoid formulas when the CSV is opened in Excel; JSON keeps the original values.
        for key, value in row.items():
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                row[key] = "'" + value
        writer.writerow(row)
    return output.getvalue().encode("utf-8-sig"), "text/csv", filename
