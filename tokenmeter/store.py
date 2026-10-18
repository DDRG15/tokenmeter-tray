"""Persistence: SQLite with idempotent insertion.

Dedupe is not an extra. With the proxy and the transcript reader active at the
same time, every Claude Code call is observed twice; without
`UNIQUE(dedupe_key)` the counter doubles and the meter lies.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, asdict
from pathlib import Path

from .models import UsageEvent

# What counts against the Claude quota. The session, the week and the alerts are
# calibrated against claude.ai: they can only add up Anthropic calls that are not
# paid with an API key. Codex, Groq or an Anthropic key consume other quotas (or
# none) and show up in the breakdown, not in the percentage.
SCOPE_SQL = {
    "claude": "provider = 'anthropic' AND billing NOT IN ('api', 'local')",
    "all": "1 = 1",
}


def scope_sql(scope: str | None) -> str:
    return SCOPE_SQL.get(scope or "all", SCOPE_SQL["all"])


# Two observations separated by less than this are considered continuous. It
# covers the 2 s transcript polling with margin for the machine pausing.
COVERAGE_GAP = 300.0
STATE_COVERAGE_FLOOR = "coverage_floor_ts"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    provider        TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    source          TEXT    NOT NULL,
    dedupe_key      TEXT    NOT NULL UNIQUE,
    usage_complete  INTEGER NOT NULL DEFAULT 1,
    billing         TEXT    NOT NULL DEFAULT 'unknown',
    plan            TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
CREATE TABLE IF NOT EXISTS event_aliases (
    provider TEXT NOT NULL,
    alias TEXT NOT NULL,
    event_id INTEGER NOT NULL REFERENCES usage_events(id) ON DELETE CASCADE,
    PRIMARY KEY(provider, alias)
);
-- Identity is looked up by alias without the provider: without this index the
-- (provider, alias) primary key is useless and every insert scans the table.
CREATE INDEX IF NOT EXISTS idx_alias ON event_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_alias_event ON event_aliases(event_id);

CREATE TABLE IF NOT EXISTS file_offsets (
    path    TEXT PRIMARY KEY,
    size    INTEGER NOT NULL,
    offset  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Calibration history. All of them are kept instead of overwritten: with two or
-- more points from the same session the real quota can be separated from the
-- tokens the meter does not see, and the previous calibration's error measured.
CREATE TABLE IF NOT EXISTS calibrations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,
    observed_percent  REAL    NOT NULL,
    counted_tokens    INTEGER NOT NULL,
    basis             TEXT    NOT NULL,
    budget_tokens     INTEGER NOT NULL,
    session_start_ts  REAL    NOT NULL,
    predicted_percent REAL
);
CREATE INDEX IF NOT EXISTS idx_calib_session ON calibrations(session_start_ts);

-- When some collector was observing. Without this, a day with no events cannot
-- tell "it was not used" from "the app was closed".
CREATE TABLE IF NOT EXISTS coverage (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    start REAL NOT NULL,
    end   REAL NOT NULL
);
"""


@dataclass(frozen=True)
class Totals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_1h_tokens: int = 0
    calls: int = 0
    incomplete_calls: int = 0

    @property
    def io_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def all_tokens(self) -> int:
        return self.io_tokens + self.cache_write_tokens + self.cache_read_tokens

    def basis(self, percent_basis: str) -> int:
        return self.all_tokens if percent_basis == "all" else self.io_tokens


@dataclass(frozen=True)
class ModelTotals(Totals):
    model: str = ""
    provider: str = ""
    # The same model can be used with a subscription and with an API key: they
    # are different rows, because only one of them is money spent.
    billing: str = "unknown"
    plan: str = ""


class Store:
    """Concurrent access from several collector threads."""

    def __init__(self, db_path: Path, retain_days: int | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._floor_cache: tuple[float] | None = None
        self._retain_days = retain_days
        self.path = Path(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Adds new columns to databases created by earlier versions.

        `CREATE TABLE IF NOT EXISTS` does not touch an existing table, so
        without this an older database would lack the new columns and every
        query would fail at startup.
        """
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(usage_events)")}
        for column, ddl in (
            ("cache_write_1h_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("billing", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("plan", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in existing:
                self._conn.execute(f"ALTER TABLE usage_events ADD COLUMN {column} {ddl}")
        cal_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(calibrations)")}
        if "scope" not in cal_cols:
            self._conn.execute("ALTER TABLE calibrations ADD COLUMN scope TEXT NOT NULL DEFAULT 'claude'")
        coverage_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(coverage)")}
        if "source" not in coverage_cols:
            self._conn.execute("ALTER TABLE coverage ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy'")
        # Old identities are registered without reinterpreting their counters.
        self._conn.execute("""INSERT OR IGNORE INTO event_aliases(provider, alias, event_id)
                              SELECT provider, dedupe_key, id FROM usage_events""")
        self._conn.execute("""INSERT OR IGNORE INTO event_aliases(provider, alias, event_id)
                              SELECT provider, 'msg:' || substr(dedupe_key, 7), id
                              FROM usage_events WHERE dedupe_key LIKE 'codex:%'""")

    def close(self) -> None:
        """Idempotent: closing twice cannot be an error."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    @property
    def closed(self) -> bool:
        return self._closed

    # -- writes ---------------------------------------------------------

    def _before_floor(self, ts: float) -> bool:
        """History starts at the floor: nothing earlier enters the database.

        After `--start-fresh`, a new collector (Codex arrived after the wipe) or
        a file that had not been read would bring back days the user decided to
        discard. The floor is cached: it is queried on every insert.
        """
        if self._floor_cache is None:
            self._floor_cache = (self.coverage_floor() or 0.0,)
        cutoff = max(self._floor_cache[0], time.time() - self._retain_days * 86400
                     if self._retain_days is not None else 0.0)
        return ts < cutoff

    def add_event(self, event: UsageEvent) -> bool:
        """True only for new calls; a better observation updates the existing one."""
        return bool(self.add_events([event]))

    def add_events(self, events: "Iterable[UsageEvent]") -> int:
        """Inserts a batch in a single transaction. Returns the new ones.

        The initial transcript backfill brings in thousands of events; one
        `commit()` per event turns a scan of seconds into one of minutes.
        """
        count = 0
        columns = ("ts", "provider", "model", "input_tokens", "output_tokens",
                   "cache_write_tokens", "cache_read_tokens", "cache_write_1h_tokens",
                   "source", "dedupe_key", "usage_complete", "billing", "plan")
        token_cols = ("input_tokens", "output_tokens", "cache_write_tokens",
                      "cache_read_tokens", "cache_write_1h_tokens")
        def quality(row):
            tokens = tuple(row[c] for c in token_cols)
            return (bool(row["usage_complete"]), sum(tokens[:4]), tokens)
        def billing_quality(row):
            # The proxy sees the call's credential: it is the most direct source.
            # But `local` is inferred from the *absence* of a credential and
            # cannot override a known type from another source (a Claude Code
            # call used to end up as "local, no cost" and outside Claude's %).
            if row["billing"] == "local":
                return (True, 0)
            return (row["billing"] != "unknown",
                    {"proxy": 3, "codex": 2, "transcript": 1}.get(row["source"], 0))
        # Claude Code repeats the same `usage` on several lines per response.
        # An observation identical to another one in the same batch adds nothing:
        # it is discarded before touching the database.
        seen: set[tuple] = set()
        with self._lock, self._conn:
            for event in events:
                if self._before_floor(event.ts):
                    continue
                event = event.with_fallback_key()
                data = asdict(event)
                fingerprint = tuple((k, v) for k, v in data.items() if k != "aliases")
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                aliases = set(event.aliases) | {event.dedupe_key}
                if event.dedupe_key.startswith("codex:"):
                    aliases.add("msg:" + event.dedupe_key[6:])
                placeholders = ",".join("?" for _ in aliases)
                # The identity of a response (request-id, response_id) is global:
                # the name the user gives the upstream in config.json does not
                # change it. Looking it up per provider counted the same call
                # twice when seen by the transcript (`anthropic`) and by a proxy
                # named `my-claude`.
                rows = self._conn.execute(
                    f"""SELECT DISTINCT e.* FROM usage_events e JOIN event_aliases a ON e.id=a.event_id
                        WHERE a.alias IN ({placeholders}) ORDER BY e.id""",
                    tuple(sorted(aliases))).fetchall()
                if not rows:
                    # Avoid collisions of equal IDs coming from different providers.
                    if self._conn.execute("SELECT 1 FROM usage_events WHERE dedupe_key=?",
                                          (data["dedupe_key"],)).fetchone():
                        data["dedupe_key"] = event.provider + ":" + data["dedupe_key"]
                    cur = self._conn.execute(
                        f"INSERT INTO usage_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        tuple(data[c] for c in columns))
                    event_id = cur.lastrowid
                    count += 1
                else:
                    event_id = rows[0]["id"]
                    observations = [dict(r) for r in rows] + [data]
                    best = max(observations, key=quality)
                    bill = max(observations, key=billing_quality)
                    merged = dict(rows[0])
                    merged.update({c: best[c] for c in token_cols})
                    merged["usage_complete"] = best["usage_complete"]
                    merged["ts"] = min(r["ts"] for r in observations)
                    # "desconocido" is what older versions stored for an unknown model.
                    merged["model"] = next((r["model"] for r in [best, *observations]
                                            if r["model"] not in ("", "desconocido", "unknown")), best["model"])
                    merged["billing"], merged["plan"] = bill["billing"], bill["plan"]
                    if not merged["plan"]:
                        merged["plan"] = next((r["plan"] for r in observations
                                               if r["billing"] == merged["billing"] and r["plan"]), "")
                    updates = [c for c in columns if c not in ("dedupe_key", "source", "provider")]
                    # Only write if the merge changes something: that is the usual
                    # case on a rescan and does not deserve an UPDATE.
                    if len(rows) > 1 or any(merged[c] != rows[0][c] for c in updates):
                        self._conn.execute(
                            f"UPDATE usage_events SET {','.join(c+'=?' for c in updates)} WHERE id=?",
                            (*[merged[c] for c in updates], event_id))
                    for duplicate in rows[1:]:
                        self._conn.execute("UPDATE event_aliases SET event_id=? WHERE event_id=?",
                                           (event_id, duplicate["id"]))
                        self._conn.execute("DELETE FROM usage_events WHERE id=?", (duplicate["id"],))
                self._conn.executemany("INSERT OR IGNORE INTO event_aliases(provider,alias,event_id) VALUES (?,?,?)",
                                       [(event.provider, alias, event_id) for alias in sorted(aliases)])
        return count

    def reset_transcript_data(self) -> int:
        """Deletes the transcript events and their offsets so they are reread.

        Needed when what is extracted from each line changes: the rows already
        stored lack the new fields and no reread would touch them, because the
        offset says that file was already processed. Proxy events are left
        alone: those cannot be reconstructed.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM usage_events WHERE source = 'transcript'")
            self._conn.execute("DELETE FROM file_offsets")
            self._conn.commit()
            return cur.rowcount

    def prune(self, retain_days: int) -> int:
        self._retain_days = retain_days
        cutoff = time.time() - retain_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM usage_events WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM coverage WHERE end < ?", (cutoff,))
            self._conn.execute("UPDATE coverage SET start=? WHERE start < ?", (cutoff, cutoff))
            self._conn.execute("DELETE FROM calibrations WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # -- reads -----------------------------------------------------------

    _AGG_COLS = """
            COALESCE(SUM(input_tokens), 0)       AS input_tokens,
            COALESCE(SUM(output_tokens), 0)      AS output_tokens,
            COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
            COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens,
            COALESCE(SUM(cache_write_1h_tokens), 0) AS cache_write_1h_tokens,
            COUNT(*)                             AS calls,
            COALESCE(SUM(1 - usage_complete), 0) AS incomplete_calls"""

    def totals_since(self, since_ts: float, scope: str | None = None) -> Totals:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._AGG_COLS} FROM usage_events "
                f"WHERE ts >= ? AND {scope_sql(scope)}", (since_ts,)
            ).fetchone()
        return Totals(**{k: int(row[k]) for k in row.keys()})

    def totals_by_model(self, since_ts: float, provider: str = "", source: str = "") -> list[ModelTotals]:
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT model, provider, billing, plan, {self._AGG_COLS}
                    FROM usage_events WHERE ts >= ? AND (?='' OR provider=?) AND (?='' OR source=?)
                    GROUP BY model, provider, billing, plan
                    ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC""",
                (since_ts, provider, provider, source, source),
            ).fetchall()
        return [
            ModelTotals(
                model=r["model"],
                provider=r["provider"],
                billing=r["billing"],
                plan=r["plan"],
                input_tokens=int(r["input_tokens"]),
                output_tokens=int(r["output_tokens"]),
                cache_write_tokens=int(r["cache_write_tokens"]),
                cache_read_tokens=int(r["cache_read_tokens"]),
                cache_write_1h_tokens=int(r["cache_write_1h_tokens"]),
                calls=int(r["calls"]),
                incomplete_calls=int(r["incomplete_calls"]),
            )
            for r in rows
        ]

    def daily_totals(self, since_ts: float, provider: str = "", source: str = "") -> dict[str, Totals]:
        """Aggregates per LOCAL day (`YYYY-MM-DD`) since `since_ts`.

        Grouping is done in SQLite with `localtime`, which uses the same system
        time zone as `date.fromtimestamp` in Python: both sides split the day at
        the same place. A day without events does not appear; filling it in, and
        deciding whether it is a zero or "no record", is `history.py`'s job.
        """
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT date(ts, 'unixepoch', 'localtime') AS day, {self._AGG_COLS}
                    FROM usage_events WHERE ts >= ? AND (?='' OR provider=?) AND (?='' OR source=?)
                    GROUP BY day ORDER BY day""",
                (since_ts, provider, provider, source, source),
            ).fetchall()
        return {
            r["day"]: Totals(**{k: int(r[k]) for k in r.keys() if k != "day"})
            for r in rows
        }

    def calls_by_source(self, since_ts: float) -> dict[str, int]:
        """Calls in the window grouped by collector.

        The panel footer needs this and not the process's insert counter: when
        starting mid-session almost everything is already in the database, and
        showing the new inserts implies that nothing was synced.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) AS n FROM usage_events WHERE ts >= ? GROUP BY source",
                (since_ts,),
            ).fetchall()
        return {r["source"]: int(r["n"]) for r in rows}

    def event_timestamps_since(self, since_ts: float, scope: str | None = None) -> list[float]:
        """Only the ts column, in ascending order. Used by the window calculation."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT ts FROM usage_events WHERE ts >= ? AND {scope_sql(scope)} "
                "ORDER BY ts ASC", (since_ts,)
            ).fetchall()
        return [float(r["ts"]) for r in rows]

    def first_event_ts_after(self, since_ts: float) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(ts) AS t FROM usage_events WHERE ts >= ?", (since_ts,)
            ).fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    def last_event_ts(self, source: str | None = None) -> float | None:
        with self._lock:
            if source is None:
                row = self._conn.execute("SELECT MAX(ts) AS t FROM usage_events").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT MAX(ts) AS t FROM usage_events WHERE source = ?", (source,)
                ).fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    # -- tailer offsets ------------------------------------------------

    def get_offset(self, path: str) -> tuple[int, int]:
        """Returns the known (size, offset) of the file, or (0, 0)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT size, offset FROM file_offsets WHERE path = ?", (path,)
            ).fetchone()
        return (int(row["size"]), int(row["offset"])) if row else (0, 0)

    def set_offset(self, path: str, size: int, offset: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO file_offsets(path, size, offset) VALUES (?,?,?)
                   ON CONFLICT(path) DO UPDATE SET size=excluded.size, offset=excluded.offset""",
                (path, size, offset),
            )
            self._conn.commit()

    # -- calibrations -----------------------------------------------------

    def add_calibration(
        self,
        *,
        ts: float,
        observed_percent: float,
        counted_tokens: int,
        basis: str,
        budget_tokens: int,
        session_start_ts: float,
        predicted_percent: float | None,
        scope: str = "claude",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO calibrations
                   (ts, observed_percent, counted_tokens, basis, budget_tokens,
                    session_start_ts, predicted_percent, scope)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (ts, observed_percent, counted_tokens, basis, budget_tokens,
                 session_start_ts, predicted_percent, scope),
            )
            self._conn.commit()

    def calibrations(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM calibrations ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()

    def calibrations_for_session(self, session_start_ts: float, basis: str | None = None,
                                 scope: str | None = None) -> list[sqlite3.Row]:
        """Points from a single window, in order. The multi-point fit requires them.

        Mixing sessions would falsify the intercept: the tokens the meter does
        not see are different in each session.
        """
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM calibrations WHERE session_start_ts = ? "
                "AND (? IS NULL OR basis=?) AND (? IS NULL OR scope=?) ORDER BY ts ASC",
                (session_start_ts, basis, basis, scope, scope),
            ).fetchall()

    # -- coverage ---------------------------------------------------------

    def mark_covered(self, start: float, end: float, gap: float = COVERAGE_GAP,
                     source: str = "legacy") -> None:
        """Records that observation was happening between `start` and `end`, merging neighbors.

        It never goes below the floor (`coverage_floor`): after a full wipe, the
        history starts there even if the transcripts on disk are older.
        """
        floor = self.coverage_floor()
        if floor is not None:
            start = max(start, floor)
        if end <= start:
            return
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, start, end FROM coverage WHERE start <= ? AND end >= ? AND source=?",
                (end + gap, start - gap, source),
            ).fetchall()
            if rows:
                start = min(start, *(r["start"] for r in rows))
                end = max(end, *(r["end"] for r in rows))
                self._conn.executemany("DELETE FROM coverage WHERE id = ?",
                                       [(r["id"],) for r in rows])
            self._conn.execute("INSERT INTO coverage(start, end, source) VALUES (?, ?, ?)", (start, end, source))
            self._conn.commit()

    def coverage_between(self, start: float, end: float) -> list[tuple[float, float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT start, end FROM coverage WHERE end > ? AND start < ? ORDER BY start",
                (start, end),
            ).fetchall()
        return [(r["start"], r["end"]) for r in rows]

    def has_coverage(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM coverage LIMIT 1").fetchone() is not None

    def coverage_details(self, start: float, end: float) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT start,end,source FROM coverage WHERE end>? AND start<? ORDER BY start",
                (start, end))]

    def filter_options(self) -> dict:
        with self._lock:
            return {key: [r[0] for r in self._conn.execute(
                f"SELECT DISTINCT {key} FROM usage_events ORDER BY {key}")]
                    for key in ("provider", "source")}

    def events_between(self, since: float, until: float, provider: str = "", source: str = "") -> list[dict]:
        """Normalized data only; there are no bodies or credentials to export."""
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT ts,provider,model,input_tokens,output_tokens,cache_write_tokens,"
                "cache_read_tokens,cache_write_1h_tokens,source,usage_complete,billing,plan "
                "FROM usage_events WHERE ts>=? AND ts<=? AND (?='' OR provider=?) "
                "AND (?='' OR source=?) ORDER BY ts,id", (since, until, provider, provider, source, source))]

    def backup_to(self, path: Path) -> None:
        """Consistent SQLite snapshot, even with WAL or active collectors."""
        from contextlib import closing

        # `closing`: sqlite3's `with` does not close the connection and on
        # Windows the destination file stayed locked.
        with self._lock, closing(sqlite3.connect(str(path))) as target:
            self._conn.backup(target)

    def coverage_floor(self) -> float | None:
        raw = self.get_state(STATE_COVERAGE_FLOOR)
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    def set_coverage_floor(self, ts: float) -> None:
        self.set_state(STATE_COVERAGE_FLOOR, repr(ts))
        self._floor_cache = (ts,)

    def retag_billing(self, source: str, billing: str, plan: str) -> int:
        """Tags a collector's `unknown` events. For databases from before billing was tracked."""
        with self._lock:
            n = self._conn.execute(
                "UPDATE usage_events SET billing = ?, plan = ? "
                "WHERE source = ? AND billing = 'unknown'",
                (billing, plan, source),
            ).rowcount
            self._conn.commit()
        return n

    # -- state ------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO state(key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )
            self._conn.commit()
