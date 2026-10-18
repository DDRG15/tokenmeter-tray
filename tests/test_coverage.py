"""A day without events is only 0 if some collector was observing it.

Real case: the user's database had no events from 15/08 to 15/10 because the
app was closed, and the history drew 0 on those days.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

from tokenmeter.collectors.transcripts import TranscriptCollector
from tokenmeter.history import daily_series, local_midnight
from tokenmeter.models import UsageEvent
from tokenmeter.store import Store

TODAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 21, 0).timestamp()


def ts(day: date, hour: int = 12) -> float:
    return datetime(day.year, day.month, day.day, hour).timestamp()


def add_event(store, day, key, io=1_000):
    store.add_event(UsageEvent(ts=ts(day), provider="anthropic", model="claude-opus-5",
                               input_tokens=io, output_tokens=0, source="transcript",
                               dedupe_key=key))


def test_days_without_coverage_are_no_record_not_zero(tmp_path):
    store = Store(tmp_path / "u.db")
    ten_ago = TODAY - timedelta(days=10)
    add_event(store, ten_ago, "a")
    store.mark_covered(ts(ten_ago, 8), ts(ten_ago, 20), source="transcript")
    yesterday = TODAY - timedelta(days=1)
    store.mark_covered(local_midnight(yesterday), NOW, source="transcript")  # all of yesterday and today

    bars = {b.day: b.tokens for b in daily_series(store, 14, now=NOW,
                                                  expected_sources=("transcript",))}
    assert bars[ten_ago] == 1_000
    assert bars[yesterday] == 0                              # observed in full and unused
    assert bars[TODAY] == 0                                  # observed since 00:00
    for d in range(2, 10):                                   # closed: unknown
        assert bars[TODAY - timedelta(days=d)] is None


def test_a_half_observed_day_is_not_zero(tmp_path):
    """A stretch of a few hours does not prove there was no usage the rest of the day."""
    store = Store(tmp_path / "u.db")
    yesterday = TODAY - timedelta(days=1)
    store.mark_covered(ts(yesterday, 9), ts(yesterday, 18), source="transcript")
    (bar,) = [b for b in daily_series(store, 3, now=NOW, expected_sources=("transcript",))
              if b.day == yesterday]
    assert bar.tokens is None and bar.coverage == "partial"


def test_first_scan_coverage_counts_for_its_collector(tmp_path):
    """`transcript_archive` comes from files that are still on disk: it covers just the same."""
    store = Store(tmp_path / "u.db")
    yesterday = TODAY - timedelta(days=1)
    store.mark_covered(local_midnight(yesterday), NOW, source="transcript_archive")
    bars = daily_series(store, 2, now=NOW, expected_sources=("transcript",))
    assert [b.tokens for b in bars] == [0, 0]


def test_a_missing_collector_prevents_the_zero(tmp_path):
    """With transcripts and Codex expected, one collector's coverage alone is not enough."""
    store = Store(tmp_path / "u.db")
    yesterday = TODAY - timedelta(days=1)
    store.mark_covered(local_midnight(yesterday), NOW, source="transcript")
    bars = daily_series(store, 2, now=NOW, expected_sources=("transcript", "codex"))
    assert all(b.tokens is None for b in bars)


def test_without_recorded_coverage_no_empty_day_is_zero(tmp_path):
    """The first event does not prove observation continued afterwards.

    An older rule made a day without events after the first one worth 0. The app
    may have been closed; without coverage, those days are "no record".
    """
    store = Store(tmp_path / "u.db")
    add_event(store, TODAY - timedelta(days=3), "a")
    bars = [b.tokens for b in daily_series(store, 7, now=NOW, expected_sources=("transcript",))]
    assert bars[3] == 1_000
    assert [bars[i] for i in (0, 1, 2, 4, 5, 6)] == [None] * 6


def test_contiguous_stretches_are_merged(tmp_path):
    store = Store(tmp_path / "u.db")
    for i in range(10):
        store.mark_covered(NOW + i * 2, NOW + i * 2 + 2)
    assert store.coverage_between(0, NOW * 2) == [(NOW, NOW + 20)]


def test_a_long_gap_is_not_merged(tmp_path):
    store = Store(tmp_path / "u.db")
    store.mark_covered(NOW, NOW + 60)
    store.mark_covered(NOW + 3600, NOW + 3660)
    assert len(store.coverage_between(0, NOW * 2)) == 2


def test_the_floor_cuts_coverage_backwards(tmp_path):
    """After wiping everything to start today, old transcripts do not cover past days."""
    store = Store(tmp_path / "u.db")
    store.set_coverage_floor(local_midnight(TODAY))
    store.mark_covered(ts(TODAY - timedelta(days=20)), NOW, source="transcript")
    bars = daily_series(store, 7, now=NOW, expected_sources=("transcript",))
    assert bars[-1].tokens == 0
    assert all(b.tokens is None for b in bars[:-1])


def test_transcripts_cover_from_the_oldest_file_on_disk(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    old, new = root / "old.jsonl", root / "new.jsonl"
    old.write_text("", encoding="utf-8")
    new.write_text("", encoding="utf-8")
    five_ago = ts(TODAY - timedelta(days=5))
    os.utime(old, (five_ago, five_ago))

    store = Store(tmp_path / "u.db")
    TranscriptCollector(store, root).scan_once()
    (start, _end), = store.coverage_between(0, 2 * NOW)
    assert start == five_ago
