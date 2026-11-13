"""Personal budgets: a limit the user sets on their own observed usage.

A budget answers one question: of the calls this app saw in a period, how much
of my limit is used. It is not the provider's quota (that is the session and
the week, calibrated against claude.ai) and it is not an invoice.

Three rules shape everything here:

* **Add calls up, never balances.** The amount is the sum of the recorded
  calls. An account balance or a provider's percentage never feeds it.
* **Periods are half-open, [start, end).** A call stamped exactly at midnight
  belongs to the new day only.
* **A total that may be missing something says so.** A missing price, a cut
  stream or pruned history turn the amount into a minimum. A minimum can prove
  a threshold was crossed; it can never prove there is room left.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import BILLING_LOCAL
from .pricing import PICO_PER_USD, cost_pico, format_pico, is_lower_bound, usd_to_pico

log = logging.getLogger(__name__)

METRICS = ("tokens", "input", "output", "money")
TOKEN_BASES = ("io", "all")
SCOPE_KINDS = ("all", "provider", "model")
PERIODS = ("day", "week", "month")
DEFAULT_THRESHOLDS = (60, 85, 100)
MAX_NAME_LENGTH = 60

# Why an amount is only a minimum. Each one is reported with what it affected,
# so the UI can say "3 calls without a price" instead of a bare "(partial)".
CAUSE_UNPRICED = "unpriced"                    # a model with no verified price
CAUSE_TIERED_PRICE = "tiered_price"            # price known only for the base tier
CAUSE_MISSING_USAGE = "missing_usage"          # a call whose usage never arrived
CAUSE_HISTORY_TRUNCATED = "history_truncated"  # the period starts before the kept history
CAUSE_TIMEZONE_CHANGED = "timezone_changed"    # the periods were cut in another zone

STATE_BELOW = "below"
STATE_WARNING = "warning"
STATE_DANGER = "danger"
STATE_EXCEEDED = "exceeded"


def system_timezone() -> str:
    """Name of the zone the process cuts days in, as precisely as it can be known.

    Used only as a label and to notice a change: the periods themselves are
    always cut with the process's local time, the same clock SQLite's
    `localtime` and the rest of the app use.
    """
    env = os.environ.get("TZ", "").strip().lstrip(":")
    if env:
        return env
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return time.tzname[0] or "local"


@dataclass(frozen=True)
class Budget:
    """One validated definition. Build it with `Budget.create` or `Budget.from_row`."""

    id: str
    name: str
    metric: str
    period: str
    limit: int  # tokens, or picodollars when `metric` is "money"
    token_basis: str = "all"
    scope_kind: str = "all"
    scope_provider: str = ""
    scope_model: str = ""
    thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS
    timezone: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("a budget needs an id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("a budget needs a name")
        if len(self.name) > MAX_NAME_LENGTH:
            raise ValueError(f"the name is longer than {MAX_NAME_LENGTH} characters")
        if self.metric not in METRICS:
            raise ValueError(f"unknown metric: {self.metric!r}")
        if self.token_basis not in TOKEN_BASES:
            raise ValueError(f"unknown token basis: {self.token_basis!r}")
        if self.period not in PERIODS:
            raise ValueError(f"unknown period: {self.period!r}")
        # bool is an int in Python: True would pass as a limit of 1.
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit <= 0:
            raise ValueError("the limit must be a whole number above zero")
        if self.scope_kind not in SCOPE_KINDS:
            raise ValueError(f"unknown scope: {self.scope_kind!r}")
        needs_provider = self.scope_kind in ("provider", "model")
        needs_model = self.scope_kind == "model"
        if bool(self.scope_provider) != needs_provider or bool(self.scope_model) != needs_model:
            raise ValueError(f"scope {self.scope_kind!r} has missing or extra fields")
        marks = self.thresholds
        if (not isinstance(marks, tuple)
                or any(isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= 100
                       for t in marks)
                or list(marks) != sorted(set(marks))):
            raise ValueError("thresholds must be unique whole percentages from 1 to 100, in order")

    @classmethod
    def create(cls, **fields) -> "Budget":
        """A new budget with a fresh id, stamped with the current system zone."""
        fields.setdefault("id", uuid.uuid4().hex[:12])
        fields.setdefault("timezone", system_timezone())
        return cls(**fields)

    @classmethod
    def from_row(cls, row: dict) -> "Budget":
        """Rebuilds a stored definition. Raises ValueError if the row is not valid."""
        try:
            marks = json.loads(row["thresholds"])
            if not isinstance(marks, list):
                raise ValueError("thresholds are not a list")
            return cls(
                id=row["id"], name=row["name"], enabled=bool(row["enabled"]),
                metric=row["metric"], token_basis=row["token_basis"],
                scope_kind=row["scope_kind"], scope_provider=row["scope_provider"],
                scope_model=row["scope_model"], period=row["period"],
                timezone=row["timezone"], limit=row["limit_value"],
                thresholds=tuple(marks),
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"unreadable budget row: {exc}") from None

    def to_row(self) -> dict:
        return {
            "id": self.id, "name": self.name, "enabled": int(self.enabled),
            "metric": self.metric, "token_basis": self.token_basis,
            "scope_kind": self.scope_kind, "scope_provider": self.scope_provider,
            "scope_model": self.scope_model, "period": self.period,
            "timezone": self.timezone, "limit_value": self.limit,
            "thresholds": json.dumps(list(self.thresholds)),
        }

    @property
    def is_money(self) -> bool:
        return self.metric == "money"

    def unit_label(self) -> str:
        if self.metric == "money":
            return "API equivalent"
        if self.metric == "tokens":
            return "tokens (input + output + cache)" if self.token_basis == "all" \
                else "tokens (input + output)"
        return "input tokens (without cache)" if self.metric == "input" else "output tokens"

    def scope_label(self) -> str:
        if self.scope_kind == "provider":
            return self.scope_provider
        if self.scope_kind == "model":
            return f"{self.scope_provider} · {self.scope_model}"
        return "all usage"

    def format_amount(self, amount: int, lower_bound: bool = False) -> str:
        if self.is_money:
            return format_pico(amount, lower_bound)
        return ("≥ " if lower_bound else "") + f"{amount:,}"


def load_budgets(store) -> tuple[list[Budget], int]:
    """(valid budgets, number of stored rows that could not be read).

    A row that fails validation is skipped, not repaired: guessing what the
    user meant would enforce a limit they never set.
    """
    budgets: list[Budget] = []
    invalid = 0
    for row in store.budget_rows():
        try:
            budgets.append(Budget.from_row(row))
        except ValueError:
            invalid += 1
            log.warning("skipping an invalid stored budget")
    return budgets, invalid


def save_budget(store, budget: Budget) -> None:
    store.save_budget_row(budget.to_row())


# -- the editor's form --------------------------------------------------------

FORM_FIELDS = ("name", "metric", "token_basis", "period", "scope_kind", "scope_provider",
               "scope_model", "limit", "thresholds")
_TOKEN_SUFFIXES = {"k": 1_000, "m": 1_000_000}


def parse_token_limit(text: str) -> int:
    """A token limit as typed: "500000", "500,000", "500k", "2.5M".

    Raises ValueError unless it is a whole number of tokens above zero.
    """
    cleaned = str(text).strip().lower().replace(",", "").replace("_", "").replace(" ", "")
    factor = 1
    if cleaned[-1:] in _TOKEN_SUFFIXES:
        factor, cleaned = _TOKEN_SUFFIXES[cleaned[-1]], cleaned[:-1]
    try:
        amount = Decimal(cleaned) * factor
    except InvalidOperation:
        raise ValueError(f"not a number of tokens: {text!r}") from None
    if not amount.is_finite() or amount != amount.to_integral_value() or not 0 < amount <= 10 ** 15:
        raise ValueError(f"not a number of tokens: {text!r}")
    return int(amount)


def parse_thresholds(text: str) -> tuple[int, ...]:
    """Percentages as typed: "60, 85, 100". Empty means no notices."""
    parts = [p for p in str(text).replace("%", " ").replace(",", " ").split() if p]
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        raise ValueError("thresholds must be whole percentages, for example: 60, 85, 100") from None


def budget_from_form(form: dict, existing: Budget | None = None) -> Budget:
    """Builds a budget from the editor's text fields; raises ValueError with a
    message for the user if any field does not make sense.

    Editing keeps the id, the creation zone and whether it is enabled, so the
    notices already given for the current period stay given.
    """
    metric = str(form.get("metric", "")).strip()
    limit_text = str(form.get("limit", ""))
    limit = usd_to_pico(limit_text) if metric == "money" else parse_token_limit(limit_text)
    scope_kind = str(form.get("scope_kind", "all")).strip()
    fields = {
        "name": str(form.get("name", "")).strip(),
        "metric": metric,
        "token_basis": str(form.get("token_basis", "all")).strip() or "all",
        "period": str(form.get("period", "")).strip(),
        "scope_kind": scope_kind,
        # Fields the chosen scope does not use are dropped, not kept hidden.
        "scope_provider": str(form.get("scope_provider", "")).strip()
        if scope_kind in ("provider", "model") else "",
        "scope_model": str(form.get("scope_model", "")).strip() if scope_kind == "model" else "",
        "limit": limit,
        "thresholds": parse_thresholds(form.get("thresholds", "")),
    }
    if existing is not None:
        return Budget(id=existing.id, timezone=existing.timezone, enabled=existing.enabled, **fields)
    return Budget.create(**fields)


def form_from_budget(budget: Budget | None = None) -> dict:
    """The editor's fields for an existing budget, or the defaults for a new one."""
    if budget is None:
        return {"name": "", "metric": "money", "token_basis": "all", "period": "month",
                "scope_kind": "all", "scope_provider": "", "scope_model": "", "limit": "",
                "thresholds": ", ".join(str(t) for t in DEFAULT_THRESHOLDS)}
    limit = (str(Decimal(budget.limit) / PICO_PER_USD) if budget.is_money else str(budget.limit))
    if budget.is_money and "." in limit:
        limit = limit.rstrip("0").rstrip(".")
    return {"name": budget.name, "metric": budget.metric, "token_basis": budget.token_basis,
            "period": budget.period, "scope_kind": budget.scope_kind,
            "scope_provider": budget.scope_provider, "scope_model": budget.scope_model,
            "limit": limit, "thresholds": ", ".join(str(t) for t in budget.thresholds)}


def set_enabled(budget: Budget, enabled: bool) -> Budget:
    return replace(budget, enabled=enabled)


# -- periods ------------------------------------------------------------------


def period_bounds(period: str, now: float) -> tuple[float, float]:
    """[start, end) of the calendar period that contains `now`, in local time.

    Both ends are resolved from their own local date instead of adding a fixed
    number of seconds: a day with a clock change lasts 23 or 25 hours.
    """
    local = datetime.fromtimestamp(now)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        start, end = day, day + timedelta(days=1)
    elif period == "week":
        start = day - timedelta(days=day.weekday())  # Monday
        end = start + timedelta(days=7)
    elif period == "month":
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    else:
        raise ValueError(f"unknown period: {period!r}")
    return start.timestamp(), end.timestamp()


# -- evaluation ---------------------------------------------------------------


@dataclass(frozen=True)
class BudgetStatus:
    """What a budget's current period looks like at one instant.

    `known` is what the recorded calls add up to. It is the exact amount only
    when `causes` is empty; otherwise it is a minimum.
    """

    budget: Budget
    as_of: float
    period_start: float
    period_end: float
    known: int
    calls: int
    causes: dict[str, int] = field(default_factory=dict)

    @property
    def uncertain(self) -> bool:
        return bool(self.causes)

    @property
    def percent_min(self) -> float:
        return 100.0 * self.known / self.budget.limit

    @property
    def percent_exact(self) -> float | None:
        return None if self.uncertain else self.percent_min

    @property
    def remaining_max(self) -> int:
        """At most this much is left. Only with no causes is it what is left."""
        return max(0, self.budget.limit - self.known)

    def reached(self, threshold: int) -> bool:
        """True when the known amount proves `threshold` percent was reached.

        Whole numbers on both sides: `percent_min >= threshold` would compare
        floats and could miss a threshold reached exactly.
        """
        return 100 * self.known >= threshold * self.budget.limit

    @property
    def crossed(self) -> tuple[int, ...]:
        return tuple(t for t in self.budget.thresholds if self.reached(t))

    @property
    def state(self) -> str:
        """Severity proven by the known amount.

        100% is always "exceeded", also when the user removed that notice. Below
        it, the first threshold under 100 is the warning and the last one the
        danger mark; with a single one there is only a warning.
        """
        if self.reached(100):
            return STATE_EXCEEDED
        marks = [t for t in self.budget.thresholds if t < 100]
        if len(marks) >= 2 and self.reached(marks[-1]):
            return STATE_DANGER
        if marks and self.reached(marks[0]):
            return STATE_WARNING
        return STATE_BELOW

    def summary(self) -> str:
        """One line for a list: `≥ $1.20 of $5.00 · at least 24%`."""
        b = self.budget
        amount = f"{b.format_amount(self.known, self.uncertain)} of {b.format_amount(b.limit)}"
        if self.uncertain:
            return f"{amount} · at least {self.percent_min:.0f}%"
        return f"{amount} · {self.percent_min:.0f}%"

    def cause_lines(self) -> list[str]:
        """The causes in words, in a stable order."""
        texts = {
            CAUSE_UNPRICED: "{n} call(s) of a model without a verified price are not in the amount",
            CAUSE_TIERED_PRICE: "{n} call(s) are priced at the base tier, which is a minimum",
            CAUSE_MISSING_USAGE: "{n} call(s) were cut before their usage arrived",
            CAUSE_HISTORY_TRUNCATED: "the period starts before the history that is kept",
            CAUSE_TIMEZONE_CHANGED: "the system time zone changed since this budget was created",
        }
        return [texts[c].format(n=self.causes[c]) for c in texts if c in self.causes]

    def as_dict(self) -> dict:
        """Plain data for a UI or a tool. Amounts are strings: a month of
        picodollars does not fit the integers JavaScript can hold exactly."""
        b = self.budget
        return {
            "id": b.id, "name": b.name, "enabled": b.enabled,
            "metric": b.metric, "unit": b.unit_label(), "scope": b.scope_label(),
            "period": b.period, "timezone": b.timezone,
            "period_start": self.period_start, "period_end": self.period_end,
            "as_of": self.as_of,
            "limit": str(b.limit), "limit_text": b.format_amount(b.limit),
            "known": str(self.known),
            "known_text": b.format_amount(self.known, self.uncertain),
            "calls": self.calls,
            "percent_min": self.percent_min, "percent_exact": self.percent_exact,
            "remaining_max": str(self.remaining_max),
            "remaining_text": b.format_amount(self.remaining_max),
            "state": self.state, "uncertain": self.uncertain,
            "thresholds": list(b.thresholds), "crossed": list(self.crossed),
            "causes": dict(self.causes), "cause_lines": self.cause_lines(),
            "summary": self.summary(),
        }


def evaluate(store, budget: Budget, now: float | None = None) -> BudgetStatus:
    """Adds up the budget's current period from the recorded calls."""
    now = time.time() if now is None else now
    start, end = period_bounds(budget.period, now)
    rows = store.model_days_between(
        start, end, now, provider=budget.scope_provider, model=budget.scope_model)

    known = calls = 0
    causes: dict[str, int] = {}

    def note(cause: str, count: int) -> None:
        if count > 0:
            causes[cause] = causes.get(cause, 0) + count

    for row in rows:
        calls += row.calls
        note(CAUSE_MISSING_USAGE, row.incomplete_calls)
        if budget.metric == "tokens":
            known += row.basis(budget.token_basis)
        elif budget.metric == "input":
            known += row.input_tokens
        elif budget.metric == "output":
            known += row.output_tokens
        elif row.billing != BILLING_LOCAL:
            # A local model costs exactly zero and is never looked up: one that
            # shares a name with a paid model must not inherit its price.
            cost = cost_pico(row.model, row.input_tokens, row.output_tokens,
                             row.cache_write_tokens, row.cache_read_tokens,
                             row.cache_write_1h_tokens, on_date=row.day)
            if cost is None:
                note(CAUSE_UNPRICED, row.calls)
            else:
                known += cost
                if is_lower_bound(row.model):
                    note(CAUSE_TIERED_PRICE, row.calls)

    if start < store.history_start(now):
        causes[CAUSE_HISTORY_TRUNCATED] = 1
    if budget.timezone and budget.timezone != system_timezone():
        causes[CAUSE_TIMEZONE_CHANGED] = 1
    return BudgetStatus(budget=budget, as_of=now, period_start=start, period_end=end,
                        known=known, calls=calls, causes=causes)


# -- notifications ------------------------------------------------------------


@dataclass(frozen=True)
class BudgetAlert:
    budget_id: str
    threshold: int
    title: str
    message: str


def due_alert(store, status: BudgetStatus) -> BudgetAlert | None:
    """The notification this evaluation owes, if any.

    Every crossed threshold is reserved, and only the highest new one is
    returned: coming back to three crossed thresholds must not show three
    balloons. A reservation is never released, so dropping below a threshold
    and crossing it again, editing the limit or restarting the app do not
    repeat a notice within the same period.
    """
    budget = status.budget
    if not budget.enabled or not status.crossed:
        return None
    new = store.reserve_budget_thresholds(budget.id, status.period_start, status.crossed)
    if not new:
        return None
    threshold = new[-1]
    at_least = "at least " if status.uncertain else ""
    used = budget.format_amount(status.known, status.uncertain)
    limit = budget.format_amount(budget.limit)
    if threshold >= 100:
        title = f"TokenMeter · {budget.name}: limit reached"
        message = f"{used} of {limit} ({budget.unit_label()}, this {budget.period})."
    else:
        title = f"TokenMeter · {budget.name}: {at_least}{threshold}%"
        message = f"{used} of {limit} ({budget.unit_label()}, this {budget.period})."
    if status.uncertain:
        message += "\nThe amount is a minimum: " + status.cause_lines()[0] + "."
    return BudgetAlert(budget.id, threshold, title, message)


def evaluate_all(store, now: float | None = None) -> tuple[list[BudgetStatus], int]:
    """(status of every stored budget, number of unreadable definitions)."""
    now = time.time() if now is None else now
    budgets, invalid = load_budgets(store)
    return [evaluate(store, b, now) for b in budgets], invalid

