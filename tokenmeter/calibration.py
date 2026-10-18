"""Derive the session budget from an observed percentage.

The real quota of a subscription plan is not published in any API, but it is
observable: the claude.ai usage page shows the percentage spent of the current
session. With that number and the tokens TokenMeter has already counted in the
same window, the denominator falls out of a rule of three.

    budget = counted_tokens / (observed_percent / 100)

Real example measured during development: claude.ai showed 24% with 83,854
input+output tokens counted in the window, which implies a quota of about
349,000 tokens — not the 500,000 it shipped with by default.

Limitations that have to be said out loud, because they affect how far to
trust it:

1. **It is a single-point regression.** Proportionality is assumed. If
   Anthropic's real quota weighs cache tokens differently, or is not linear,
   the estimate will drift as it moves away from the measured point.
2. **TokenMeter does not see the browser.** If part of that session's spend
   came from claude.ai in the browser, those tokens are in the observed
   percentage but not in the local counter, and the budget will come out lower
   than the real one.
3. **Calibrating early in the session amplifies the error.** With 3% observed,
   one point of rounding moves the result by a third.
"""

from __future__ import annotations

from dataclasses import dataclass

# Below this percentage the web's rounding dominates the calculation.
MIN_RELIABLE_PERCENT = 10.0
# Safety margin: the web rounds to an integer, so the real value is within
# ±0.5 points. It is used to report the range, not to alter the result.
WEB_ROUNDING = 0.5


class CalibrationError(ValueError):
    """The observed data does not allow deriving a useful budget."""


@dataclass(frozen=True)
class Calibration:
    observed_percent: float
    counted_tokens: int
    basis: str
    budget_tokens: int
    budget_low: int
    budget_high: int

    @property
    def reliable(self) -> bool:
        return self.observed_percent >= MIN_RELIABLE_PERCENT

    @property
    def uncertainty_percent(self) -> float:
        """Width of the range as a percentage of the estimated budget."""
        if self.budget_tokens <= 0:
            return 0.0
        return 100.0 * (self.budget_high - self.budget_low) / self.budget_tokens

    def summary(self) -> str:
        span = f"{self.budget_low:,} – {self.budget_high:,}"
        text = (
            f"{self.counted_tokens:,} counted tokens are "
            f"{self.observed_percent:g}% → budget {self.budget_tokens:,} "
            f"(range {span})"
        )
        if not self.reliable:
            text += (
                f"\n\nWarning: with only {self.observed_percent:g}% consumed, the "
                "web's rounding weighs heavily. Calibrate again once you pass "
                f"{MIN_RELIABLE_PERCENT:g}% to refine it."
            )
        return text


@dataclass(frozen=True)
class Drift:
    """How far off the current budget was at the last observation."""

    predicted_percent: float
    observed_percent: float

    @property
    def points(self) -> float:
        """Error in percentage points. Positive = the meter was falling short."""
        return self.observed_percent - self.predicted_percent

    @property
    def relative(self) -> float:
        """Error as a fraction of what was observed."""
        if self.observed_percent <= 0:
            return 0.0
        return abs(self.points) / self.observed_percent

    @property
    def significant(self) -> bool:
        # Half a point is the web's rounding; below that there is no signal.
        return abs(self.points) > 1.0 and self.relative > 0.10

    def summary(self) -> str:
        direction = "higher" if self.points > 0 else "lower"
        return (
            f"The previous budget predicted {self.predicted_percent:.1f}% and the web "
            f"showed {self.observed_percent:g}%: {abs(self.points):.1f} points "
            f"{direction} ({self.relative * 100:.0f}% error)."
        )


@dataclass(frozen=True)
class SessionFit:
    """Fit of `percent = a·tokens + b` over points from a single session.

    The slope gives the real quota; the intercept estimates the **tokens the
    meter does not see** — mostly spend done in the browser. It is the only way
    to separate the two without access to the provider's accounting.
    """

    points: int
    budget_tokens: int
    unseen_tokens: int
    max_residual_points: float
    # Unclipped intercept, in percentage points. Negative means the model does
    # not fit: see `misaligned`.
    intercept_points: float

    @property
    def exact(self) -> bool:
        """With two points the fit passes through both: there is no residual to measure."""
        return self.points <= 2

    @property
    def misaligned(self) -> bool:
        """The line crosses the axis below zero: the model does not explain the data.

        It would imply the meter counted tokens the web does not attribute to
        this session — usually because the two 5 h windows do not start at the
        same time, or because the quota is not proportional to the tokens we see.

        Clipping the negative to zero and presenting the quota as good would be
        exactly the kind of believable, wrong number this tool avoids.
        """
        return self.intercept_points < -1.0

    def summary(self) -> str:
        text = (
            f"Fit over {self.points} observations from this session: "
            f"limit {self.budget_tokens:,} tokens"
        )
        if self.unseen_tokens > 0:
            text += (
                f", plus {self.unseen_tokens:,} tokens the web counts and "
                "TokenMeter does not see (browser or another tool)"
            )
        if not self.exact:
            text += f". Maximum residual: {self.max_residual_points:.1f} points"
        text += "."

        if self.misaligned:
            text += (
                f"\n\nWarning: the fit crosses the axis at {self.intercept_points:.1f} "
                "points, that is, below zero. That means TokenMeter counted "
                "tokens the web does not attribute to this session: most likely "
                "its 5 h window and yours do not start at the same time. Trust "
                "the last standalone calibration more than this limit."
            )
        return text


def predict_percent(counted_tokens: int, budget_tokens: int) -> float:
    if budget_tokens <= 0:
        return 0.0
    return 100.0 * counted_tokens / budget_tokens


def measure_drift(
    observed_percent: float, counted_tokens: int, budget_tokens: int
) -> Drift | None:
    """Compares what the current budget predicted with what the web shows."""
    if budget_tokens <= 0 or counted_tokens <= 0:
        return None
    return Drift(
        predicted_percent=predict_percent(counted_tokens, budget_tokens),
        observed_percent=observed_percent,
    )


def fit_session(observations: list[tuple[int, float]]) -> SessionFit | None:
    """Least squares over (counted_tokens, observed_percent).

    Returns None if there are fewer than two distinct points or if the slope
    comes out zero or negative — that would mean the percentage drops as more is
    spent, and fitting on that would give a meaningless quota.
    """
    points = sorted({(int(n), float(p)) for n, p in observations if n > 0 and p > 0})
    if len(points) < 2:
        return None
    if len({n for n, _ in points}) < 2:
        return None  # all with the same tokens: there is no line to fit

    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)

    denominator = n * sxx - sx * sx
    if denominator == 0:
        return None
    slope = (n * sxy - sx * sy) / denominator
    if slope <= 0:
        return None
    intercept = (sy - slope * sx) / n

    budget = round(100.0 / slope)
    # intercept = 100·unseen/quota  →  unseen = intercept/slope
    unseen = max(0, round(intercept / slope))
    residual = max(abs(y - (slope * x + intercept)) for x, y in points)

    return SessionFit(
        points=n,
        budget_tokens=int(budget),
        unseen_tokens=int(unseen),
        max_residual_points=residual,
        intercept_points=intercept,
    )


def calibrate(observed_percent: float, counted_tokens: int, basis: str = "io") -> Calibration:
    """Turns a percentage seen on claude.ai into a token budget."""
    if not 0 < observed_percent <= 100:
        raise CalibrationError("The percentage must be between 0 and 100.")
    if counted_tokens <= 0:
        raise CalibrationError(
            "TokenMeter has not counted any tokens in this session yet. "
            "Use it for a while and calibrate again."
        )

    budget = round(counted_tokens / (observed_percent / 100.0))

    # The web shows integers: 24% is really [23.5%, 24.5%). A higher
    # percentage implies a lower quota, hence the crossed limits.
    high_pct = min(100.0, observed_percent + WEB_ROUNDING)
    low_pct = max(0.01, observed_percent - WEB_ROUNDING)
    budget_low = round(counted_tokens / (high_pct / 100.0))
    budget_high = round(counted_tokens / (low_pct / 100.0))

    return Calibration(
        observed_percent=observed_percent,
        counted_tokens=counted_tokens,
        basis=basis,
        budget_tokens=int(budget),
        budget_low=int(budget_low),
        budget_high=int(budget_high),
    )
