"""Reset time copied from claude.ai → absolute instant.

TokenMeter infers the session start from the events it sees. Usage it does not
see —claude.ai in the browser, the phone, another machine— starts the session
earlier: on one occasion the web opened the session at 19:40 and TokenMeter
only at 21:30, with the first Claude Code call. The only reliable source for the
anchor is the web itself, so the user copies what it says and it is parsed here.

Accepted formats, as the web shows them or typed by hand (Spanish weekday names
and durations are accepted too, since users paste from claude.ai in either
language):

* time with an optional day: ``Fri 12:40 AM``, ``Thursday 3:00 AM``, ``jue 03:00``,
  ``00:40``, ``9 PM``;
* countdown: ``in 4 hr 25 min``, ``2d 4h``, ``3 h 10 m``;
* a bare number: hours remaining (compatible with the previous dialog).

The result has to fall in the future and within the requested scope (5 h for
the session, 7 days for the week). Anything outside is rejected: a badly copied
time must not move the anchor somewhere absurd.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta


class AnchorError(ValueError):
    pass


_WEEKDAYS = {
    0: ("mon", "monday", "lun", "lunes"),
    1: ("tue", "tues", "tuesday", "mar", "martes"),
    2: ("wed", "wednesday", "mié", "mie", "miércoles", "miercoles"),
    3: ("thu", "thur", "thurs", "thursday", "jue", "jueves"),
    4: ("fri", "friday", "vie", "viernes"),
    5: ("sat", "saturday", "sáb", "sab", "sábado", "sabado"),
    6: ("sun", "sunday", "dom", "domingo"),
}
WEEKDAY_BY_NAME = {name: day for day, names in _WEEKDAYS.items() for name in names}

_DURATION = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(d|days?|días?|dias?|h|hrs?|hours?|horas?|m|mins?|minutes?|minutos?)\b"
)
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(a\.?\s?m\.?|p\.?\s?m\.?)?$")


def _clean(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"^(resets?|reset|se resetea|reinicia)\s+", "", t)
    t = re.sub(r"^(in|en|dentro de)\s+", "", t)
    return t.strip(" .,")


def _duration_seconds(text: str) -> float | None:
    matches = list(_DURATION.finditer(text))
    if not matches:
        return None
    # The whole text has to be a duration; if anything is left over, it is not.
    if _DURATION.sub("", text).strip(" ,y&and") != "":
        return None
    total = 0.0
    for m in matches:
        value = float(m.group(1).replace(",", "."))
        unit = m.group(2)
        if unit.startswith("d"):
            total += value * 86400
        elif unit.startswith("h"):
            total += value * 3600
        else:
            total += value * 60
    return total


def _clock(text: str) -> tuple[int, int]:
    m = _CLOCK.match(text.strip())
    if not m:
        raise AnchorError(f"cannot understand the time '{text}'")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").replace(".", "").replace(" ", "")
    if suffix:
        if not 1 <= hour <= 12:
            raise AnchorError(f"'{text}': with AM/PM the hour goes from 1 to 12")
        hour = hour % 12 + (12 if suffix == "pm" else 0)
    if hour > 23 or minute > 59:
        raise AnchorError(f"'{text}' is not a valid time")
    return hour, minute


def parse_reset(text: str, now: float, max_ahead_seconds: float) -> float:
    """Instant (epoch) of the next reset described by `text`."""
    t = _clean(text or "")
    if not t:
        raise AnchorError("nothing was entered")

    if re.fullmatch(r"\d+(?:[.,]\d+)?", t):
        reset = now + float(t.replace(",", ".")) * 3600
    elif (secs := _duration_seconds(t)) is not None:
        reset = now + secs
    else:
        weekday = None
        first, _, rest = t.partition(" ")
        if first in WEEKDAY_BY_NAME:
            weekday, t = WEEKDAY_BY_NAME[first], rest.strip()
        hour, minute = _clock(t)
        base = datetime.fromtimestamp(now)
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if weekday is not None:
            candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
            if candidate.timestamp() <= now:
                candidate += timedelta(days=7)
        elif candidate.timestamp() <= now:
            candidate += timedelta(days=1)
        reset = candidate.timestamp()

    if reset <= now:
        raise AnchorError("that reset has already passed")
    # One minute of margin: the web rounds and the user takes time to type.
    if reset - now > max_ahead_seconds + 60:
        hours = max_ahead_seconds / 3600
        scope = f"{hours:g} h" if hours < 48 else f"{hours / 24:g} days"
        raise AnchorError(f"that reset is more than {scope} away: check what you copied")
    return reset
