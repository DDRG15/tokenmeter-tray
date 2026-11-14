"""Read-only view of the desktop app's personal budgets, for the MCP server.

The usage tools rebuild usage from files into memory, so they cannot see calls
that only the proxy recorded, nor the desktop app's retention and floor. A
budget has to be the same number the window shows, so this reads the desktop
app's own database instead. It never writes to it: no schema changes, no
reserved notices, no configuration saved.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from . import budgets
from .store import Store

# A budget's name is typed by the user, but the MCP caller is a language model:
# free text must not reach it as instructions or as an unbounded payload.
_NAME = re.compile(r"[A-Za-z0-9 ._:/@+()%$#&'-]{1,60}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9._:/@+<>\[\]-]{1,64}")
UNRECOGNIZED = "unrecognized"
DEFAULT_RETAIN_DAYS = 90
NOTE = ("Personal limits on usage the desktop app recorded, read from its database. "
        "They are not the provider's quota and the API equivalent is not an invoice. "
        "An amount with causes is a minimum: it can show a limit was passed, never that "
        "there is room left. The figures are as fresh as the desktop app's last recorded call.")


def _retain_days(config_path: Path | None) -> int:
    """`retain_days` from config.json, read as plain JSON. `Config.load()` is not
    used here because it repairs and rewrites the file."""
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))["retain_days"]
    except (AttributeError, OSError, ValueError, KeyError, TypeError):
        return DEFAULT_RETAIN_DAYS
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_RETAIN_DAYS
    return value


def _scope_text(budget: budgets.Budget) -> str:
    parts = [p if _IDENTIFIER.fullmatch(p) else UNRECOGNIZED
             for p in (budget.scope_provider, budget.scope_model) if p]
    return " · ".join(parts) if parts else "all usage"


def _public(status: budgets.BudgetStatus) -> dict:
    data = status.as_dict()
    budget = status.budget
    data["name"] = budget.name if _NAME.fullmatch(budget.name) else UNRECOGNIZED
    data["scope"] = _scope_text(budget)
    data["scope_kind"] = budget.scope_kind
    # Zone names have spaces on Windows ("Coordinated Universal Time"), so they
    # are checked like a name, not like an identifier. Empty means "not stored".
    data["timezone"] = (budget.timezone if not budget.timezone or _NAME.fullmatch(budget.timezone)
                        else UNRECOGNIZED)
    # The summary repeats nothing free-form: amounts and percentages only.
    return data


class BudgetQuery:
    """Answers `get_budget_status` from the desktop database, opened read-only."""

    def __init__(self, database: Path, config_path: Path | None = None) -> None:
        self._database = Path(database)
        self._config_path = config_path

    def _unavailable(self, reason: str, now: float) -> dict:
        # Never a zero: "could not read" and "nothing spent" are different facts.
        return {"available": False, "reason": reason, "data_source": "desktop_store",
                "as_of": now, "budgets": [], "invalid_definitions": 0, "note": NOTE}

    def status(self, budget_id: str = "", now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if not self._database.is_file():
            return self._unavailable("no_desktop_database", now)
        try:
            store = Store.open_read_only(self._database, _retain_days(self._config_path))
        except (sqlite3.Error, OSError):
            return self._unavailable("database_not_readable", now)
        try:
            try:
                statuses, invalid = budgets.evaluate_all(store, now)
                last_call = store.last_event_ts()
            except sqlite3.OperationalError as exc:
                # A database from a version without budgets has no such table.
                missing = "no such table" in str(exc) or "no such column" in str(exc)
                return self._unavailable(
                    "desktop_database_too_old" if missing else "database_not_readable", now)
            except sqlite3.Error:
                return self._unavailable("database_not_readable", now)
        finally:
            store.close()
        if budget_id:
            statuses = [s for s in statuses if s.budget.id == budget_id]
        return {
            "available": True, "reason": "", "data_source": "desktop_store", "as_of": now,
            "last_recorded_call_ts": last_call,
            "seconds_since_last_recorded_call": None if last_call is None else max(0.0, now - last_call),
            "budgets": [_public(s) for s in statuses],
            "invalid_definitions": invalid,
            "note": NOTE,
        }
