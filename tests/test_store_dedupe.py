from tokenmeter.models import UsageEvent
from tokenmeter.store import Store


def make_event(**kw) -> UsageEvent:
    base = dict(
        ts=1000.0,
        provider="anthropic",
        model="claude-opus-5",
        input_tokens=100,
        output_tokens=50,
        cache_write_tokens=10,
        cache_read_tokens=2000,
        source="proxy",
        dedupe_key="req_abc123",
    )
    base.update(kw)
    return UsageEvent(**base)


def test_same_request_seen_by_both_collectors_counts_once(tmp_path):
    store = Store(tmp_path / "t.db")

    assert store.add_event(make_event(source="proxy")) is True
    # The transcript reader sees the same call with the same requestId.
    assert store.add_event(make_event(source="transcript")) is False

    totals = store.totals_since(0)
    assert totals.calls == 1
    assert totals.input_tokens == 100
    assert totals.output_tokens == 50
    assert totals.io_tokens == 150
    assert totals.all_tokens == 2160


def test_distinct_requests_accumulate(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_event(make_event(dedupe_key="req_1"))
    store.add_event(make_event(dedupe_key="req_2"))

    totals = store.totals_since(0)
    assert totals.calls == 2
    assert totals.io_tokens == 300


def test_missing_dedupe_key_gets_synthetic_and_marks_incomplete(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_event(make_event(dedupe_key=None))
    store.add_event(make_event(dedupe_key=None))  # identical retry

    totals = store.totals_since(0)
    assert totals.calls == 1
    assert totals.incomplete_calls == 1


def test_totals_by_model_splits_and_sorts(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_event(make_event(dedupe_key="a", model="claude-opus-5", output_tokens=50))
    store.add_event(make_event(dedupe_key="b", model="gpt-5", provider="openai", output_tokens=5000))

    rows = store.totals_by_model(0)
    assert [r.model for r in rows] == ["gpt-5", "claude-opus-5"]
    assert rows[0].provider == "openai"


def test_opening_a_database_from_an_older_version_migrates_it(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` does not add columns to a table that already exists."""
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE usage_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts REAL NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
               input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
               cache_write_tokens INTEGER NOT NULL DEFAULT 0,
               cache_read_tokens INTEGER NOT NULL DEFAULT 0,
               source TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
               usage_complete INTEGER NOT NULL DEFAULT 1);
           INSERT INTO usage_events
             (ts, provider, model, input_tokens, output_tokens, cache_write_tokens,
              cache_read_tokens, source, dedupe_key, usage_complete)
             VALUES (100.0, 'anthropic', 'claude-opus-5', 10, 20, 30, 40, 'proxy', 'k1', 1);"""
    )
    con.commit()
    con.close()

    store = Store(db)  # it must not blow up, and it must keep what was there
    totals = store.totals_since(0)
    assert totals.calls == 1
    assert totals.input_tokens == 10
    assert totals.cache_write_1h_tokens == 0, "old rows do not know the TTL"

    store.add_event(make_event(dedupe_key="k2", cache_write_1h_tokens=500))
    assert store.totals_since(0).cache_write_1h_tokens == 500


def test_closing_twice_is_not_an_error(tmp_path):
    """The menu and the `finally` of the Tk loop can both request the close."""
    store = Store(tmp_path / "t.db")
    store.close()
    store.close()
    assert store.closed is True


def test_totals_window_excludes_older_events(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_event(make_event(dedupe_key="old", ts=500.0))
    store.add_event(make_event(dedupe_key="new", ts=1500.0))

    assert store.totals_since(1000.0).calls == 1
