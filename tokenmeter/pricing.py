"""Estimated cost per model.

Informational only: the percentage is driven by the token budget, not by this.

Deliberate rule: a model that is not in the table returns `None` and the UI
shows `—`. It is never estimated with another model's price — an invented
number is worse than no number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Date of the table. Prices change; if this is very old, check it at
# https://platform.claude.com/docs/en/about-claude/pricing
VERIFIED_ON = "2026-11-12"

# Cache multipliers relative to the input price.
#
# The TTL matters a lot: writing 1-hour cache costs 60% more than 5-minute
# cache. Read each response's TTL breakdown instead of applying the
# 5-minute multiplier to every cache write.
#
# Cache reads are 0.1x for most models, but a model can override it with
# `Price.cache_read_multiplier` (Fable 5.1/5.2 and Mythos 5.1/5.2 use 0.025x,
# Opus 5.5 and Sonnet 5.5 use 0.05x).
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.00
CACHE_READ_MULTIPLIER = 0.10

# A model's cache-read multiplier is a per-model fact, so it is named here once
# instead of repeating the literals in the table below.
CACHE_READ_MULTIPLIER_FABLE_5_1_PLUS = 0.025
CACHE_READ_MULTIPLIER_OPUS_SONNET_5_5 = 0.05


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    # Promotional price in effect until the given date (ISO), if any.
    intro_input_per_mtok: float | None = None
    intro_output_per_mtok: float | None = None
    intro_until: str | None = None
    cache_read_multiplier: float = CACHE_READ_MULTIPLIER
    # True when the model is billed in tiers decided per request (a higher rate
    # above a prompt-length threshold). `cost_usd` receives totals aggregated
    # over many requests and cannot tell which ones crossed the threshold, so
    # the table carries the base tier and the figure is a minimum, never exact.
    lower_bound: bool = False

    def rates(self, on_date: str) -> tuple[float, float]:
        if self.intro_until and on_date <= self.intro_until:
            return (
                self.intro_input_per_mtok or self.input_per_mtok,
                self.intro_output_per_mtok or self.output_per_mtok,
            )
        return (self.input_per_mtok, self.output_per_mtok)


# Only models with a published and verified price. No guesses.
#
# Source: https://platform.claude.com/docs/en/about-claude/pricing (model
# pricing table and "Long context pricing"), model ids from
# https://platform.claude.com/docs/en/models/overview.
#
# Haiku 5.5 is priced by prompt length: $0.10 / $0.50 up to 100,000 prompt
# tokens and $0.50 / $2.50 above that, decided per request. `cost_usd` receives
# totals aggregated over many requests and cannot tell which requests crossed
# the threshold, so the table carries the base tier. A request with a prompt
# over 100,000 tokens (input + cache reads + cache writes) costs up to 5x more
# than reported here: the Haiku 5.5 figure is a lower bound, not an exact cost,
# and its `Price` carries `lower_bound=True` so every surface shows it as one.
PRICES: dict[str, Price] = {
    "claude-fable-5-2": Price(
        8.00, 40.00, cache_read_multiplier=CACHE_READ_MULTIPLIER_FABLE_5_1_PLUS),
    "claude-mythos-5-2": Price(
        8.00, 40.00, cache_read_multiplier=CACHE_READ_MULTIPLIER_FABLE_5_1_PLUS),
    "claude-fable-5-1": Price(
        10.00, 50.00, cache_read_multiplier=CACHE_READ_MULTIPLIER_FABLE_5_1_PLUS),
    "claude-mythos-5-1": Price(
        10.00, 50.00, cache_read_multiplier=CACHE_READ_MULTIPLIER_FABLE_5_1_PLUS),
    "claude-fable-5": Price(10.00, 50.00),
    "claude-mythos-5": Price(10.00, 50.00),
    "claude-opus-5-5": Price(
        4.00, 20.00, cache_read_multiplier=CACHE_READ_MULTIPLIER_OPUS_SONNET_5_5),
    "claude-opus-5": Price(5.00, 25.00),
    "claude-opus-4-8": Price(5.00, 25.00),
    "claude-opus-4-7": Price(5.00, 25.00),
    "claude-opus-4-6": Price(5.00, 25.00),
    "claude-opus-4-5": Price(5.00, 25.00),
    "claude-sonnet-5-5": Price(
        2.00, 10.00, cache_read_multiplier=CACHE_READ_MULTIPLIER_OPUS_SONNET_5_5),
    # The $2 / $10 launch price became the standard price; the increase to
    # $3 / $15 scheduled for 2026-09-01 was cancelled.
    "claude-sonnet-5": Price(2.00, 10.00),
    "claude-sonnet-4-6": Price(3.00, 15.00),
    "claude-sonnet-4-5": Price(3.00, 15.00),
    "claude-haiku-5-5": Price(0.10, 0.50, lower_bound=True),
    "claude-haiku-4-5": Price(1.00, 5.00),
}

# Platform prefixes: Bedrock (`anthropic.`, regional and global inference
# profiles).
_PLATFORM_PREFIXES = ("anthropic.", "us.anthropic.", "eu.anthropic.", "global.anthropic.")
# Bedrock version suffix: `claude-haiku-4-5-20251001-v1:0`.
_BEDROCK_VERSION_SUFFIX = re.compile(r"-v\d+:\d+$")
# A dated snapshot is exactly an 8-digit `-YYYYMMDD` segment. A version
# segment such as the `-5` in `claude-opus-5-5` is a different model with its
# own price and must never be stripped.
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalize(model: str) -> str:
    """Reduces a platform alias to the first-party id."""
    name = (model or "").strip().lower()
    for prefix in _PLATFORM_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    name = name.split("@", 1)[0]  # Vertex version suffix
    # OpenRouter: `anthropic/claude-haiku-4.5` and the `:thinking`/`:beta`
    # variants, which are priced the same as the base model.
    if name.startswith("anthropic/"):
        name = name[len("anthropic/"):].split(":", 1)[0].replace(".", "-")
    if name in PRICES:
        return name
    # Dated snapshots: `claude-haiku-4-5-20251001` → `claude-haiku-4-5`.
    base = _DATE_SUFFIX.sub("", _BEDROCK_VERSION_SUFFIX.sub("", name))
    if base in PRICES:
        return base
    return name


def is_lower_bound(model: str) -> bool:
    """True when the model's aggregated cost can only be a minimum.

    Unknown models are not lower bounds: they have no cost at all.
    """
    price = PRICES.get(normalize(model))
    return price is not None and price.lower_bound


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    on_date: str = "",
) -> float | None:
    """Equivalent cost in USD at API rates, or None if the model has no price.

    `cache_write_1h_tokens` is the subset of `cache_write_tokens` written with a
    1-hour TTL; the rest is priced at 5 minutes.

    For a model whose `Price.lower_bound` is set (see `is_lower_bound`) the
    result is a minimum: requests in a higher tier cost more than this.

    This is **not what a subscription user pays**. It is what the same work
    would have cost calling the API at these public rates.
    """
    price = PRICES.get(normalize(model))
    if price is None:
        return None
    if not on_date:
        from datetime import date

        on_date = date.today().isoformat()

    rate_in, rate_out = price.rates(on_date)
    per_token_in = rate_in / 1_000_000

    # Never negative: an inconsistent breakdown must not subtract cost.
    write_1h = max(0, min(cache_write_1h_tokens, cache_write_tokens))
    write_5m = cache_write_tokens - write_1h

    return (
        input_tokens * per_token_in
        + output_tokens * (rate_out / 1_000_000)
        + write_5m * per_token_in * CACHE_WRITE_5M_MULTIPLIER
        + write_1h * per_token_in * CACHE_WRITE_1H_MULTIPLIER
        + cache_read_tokens * per_token_in * price.cache_read_multiplier
    )


def format_usd(value: float | None) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "$0.00"  # "nothing paid" does not need four decimals
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def format_cost(value: float | None, lower_bound: bool = False) -> str:
    """`format_usd`, prefixed with "≥ " when the amount is only a minimum."""
    text = format_usd(value)
    return f"≥ {text}" if lower_bound and value is not None else text
