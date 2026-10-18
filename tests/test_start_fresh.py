"""`--start-fresh`: backs up, deletes what was measured and calibrated, and the history starts today."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

from tests.test_transcripts import assistant_line, write
from tokenmeter.__main__ import start_fresh
from tokenmeter.collectors.transcripts import TranscriptCollector
from tokenmeter.config import Config
from tokenmeter.history import daily_series, local_midnight
from tokenmeter.store import Store


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_start_fresh(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TOKENMETER_HOME", str(home))
    root = tmp_path / "projects"
    root.mkdir()

    now = time.time()
    three_days_ago = now - 3 * 86400
    write(root / "s.jsonl", [assistant_line("req_old", "m1", ts=iso(three_days_ago)),
                             assistant_line("req_today", "m2", ts=iso(now - 60))])

    config = Config(transcripts_root=str(root), codex_root=str(tmp_path / "no-codex"),
                    session_budget_tokens=264_320,
                    calibrated_at=123.0, calibrated_from_percent=19.0,
                    weekly_budget_tokens=1_362_129, weekly_calibrated_at=456.0)
    config.save()
    store = Store(home / "usage.db")
    TranscriptCollector(store, root).scan_once()
    store.set_state("weekly_reset_ts", "1786677095.5")
    store.close()

    assert start_fresh(config, now=now) == 0

    backups = list(home.glob("backup-*"))
    assert len(backups) == 1 and (backups[0] / "usage.db").exists()

    fresh = Config.load()
    assert fresh.calibrated_at == 0 and fresh.session_budget_tokens == Config().session_budget_tokens
    assert fresh.weekly_calibrated_at == 0
    assert fresh.transcripts_root == str(root)             # the user's settings are kept

    store = Store(home / "usage.db")
    assert store.get_state("weekly_reset_ts") is None       # weekly cycle deleted
    assert store.totals_since(0).calls == 1                 # only today's call
    # When the app starts, the collector does not load the old data again.
    assert TranscriptCollector(store, root).scan_once() == 0
    bars = daily_series(store, 7, now=now)
    assert bars[-1].tokens and all(b.tokens is None for b in bars[:-1])
    assert store.coverage_floor() == local_midnight(date.fromtimestamp(now))
    store.close()
