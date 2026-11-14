"""Reset times that Claude Code itself reports for the plan's limits.

When the plan's 5-hour or weekly limit rejects a call, Claude Code writes the
rejection to the transcript with the exact instant the limit resets. That
instant comes from the account, so it also reflects usage this machine never
saw (the browser, the phone, another computer). It is better than deducing the
window from the first local call and needs no copying from claude.ai.

The field is not a documented contract. It is read defensively: anything that
does not look exactly like a reset time is ignored, and the windows then fall
back to what they did before.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
# Longest a reset can lie ahead of the moment it was reported: the window's
# length plus a margin for clock differences.
MAX_AHEAD_S = {FIVE_HOUR: 5 * 3600 + 600, SEVEN_DAY: 7 * 86400 + 600}
STATE_KEYS = {FIVE_HOUR: "claude_limit_five_hour", SEVEN_DAY: "claude_limit_seven_day"}


@dataclass(frozen=True)
class LimitNotice:
    kind: str           # FIVE_HOUR or SEVEN_DAY
    resets_at: float    # unix seconds
    observed_at: float  # when Claude Code reported it


def notice_from_record(obj: object, observed_at: float | None) -> LimitNotice | None:
    """Reads the reset out of a transcript record, or None if there is none to trust."""
    if not isinstance(obj, dict) or observed_at is None:
        return None
    params = obj.get("apiErrorParams")
    info = params.get("rate_limit_info") if isinstance(params, dict) else None
    if not isinstance(info, dict):
        return None
    kind, resets = info.get("rateLimitType"), info.get("resetsAt")
    if kind not in MAX_AHEAD_S:
        return None
    # bool is an int in Python; a reset of `True` is not a timestamp.
    if isinstance(resets, bool) or not isinstance(resets, (int, float)) or not math.isfinite(resets):
        return None
    # A reset is in the future of its own report, and no further than one window.
    if not observed_at < resets <= observed_at + MAX_AHEAD_S[kind]:
        return None
    return LimitNotice(kind, float(resets), float(observed_at))


def latest(store, kind: str) -> LimitNotice | None:
    raw = store.get_state(STATE_KEYS[kind])
    if not raw:
        return None
    try:
        data = json.loads(raw)
        notice = LimitNotice(kind, float(data["resets_at"]), float(data["observed_at"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(notice.resets_at) and math.isfinite(notice.observed_at)):
        return None
    return notice


def record(store, notice: LimitNotice) -> bool:
    """Keeps the notice if it is newer than the stored one. Returns True if kept.

    Transcripts are read in no particular order (a backfill brings old files
    after new ones), so "newer" is decided by when it was reported, never by
    arrival.
    """
    current = latest(store, notice.kind)
    if current is not None and current.observed_at >= notice.observed_at:
        return False
    store.set_state(STATE_KEYS[notice.kind], json.dumps(
        {"resets_at": notice.resets_at, "observed_at": notice.observed_at}))
    return True
