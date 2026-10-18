"""Estimated cost per model.

Informational only: the percentage is driven by the token budget, not by this.

Deliberate rule: a model that is not in the table returns `None` and the UI
shows `—`. It is never estimated with another model's price — an invented
number is worse than no number.
"""

from __future__ import annotations

from dataclasses import dataclass

# Date of the table. Prices change; if this is very old, check it at
# https://platform.claude.com/docs/en/pricing
VERIFIED_ON = "2026-06-24"

# Cache multipliers relative to the input price.
#
# The TTL matters a lot: writing 1-hour cache costs 60% more than 5-minute
# cache. Read each response's TTL breakdown instead of applying the
# 5-minute multiplier to every cache write.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.00
CACHE_READ_MULTIPLIER = 0.10


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    # Promotional price in effect until the given date (ISO), if any.
    intro_input_per_mtok: float | None = None
    intro_output_per_mtok: float | None = None
    intro_until: str | None = None

    def rates(self, on_date: str) -> tuple[float, float]:
        if self.intro_until and on_date <= self.intro_until:
            return (
                self.intro_input_per_mtok or self.input_per_mtok,
                self.intro_output_per_mtok or self.output_per_mtok,
            )
        return (self.input_per_mtok, self.output_per_mtok)


# Only models with a published and verified price. No guesses.
PRICES: dict[str, Price] = {
    "claude-fable-5": Price(10.00, 50.00),
    "claude-mythos-5": Price(10.00, 50.00),
    "claude-opus-5": Price(5.00, 25.00),
    "claude-opus-4-8": Price(5.00, 25.00),
    "claude-opus-4-7": Price(5.00, 25.00),
    "claude-opus-4-6": Price(5.00, 25.00),
    "claude-opus-4-5": Price(5.00, 25.00),
    "claude-sonnet-5": Price(
        3.00, 15.00,
        intro_input_per_mtok=2.00, intro_output_per_mtok=10.00,
        intro_until="2026-08-31",
    ),
    "claude-sonnet-4-6": Price(3.00, 15.00),
    "claude-sonnet-4-5": Price(3.00, 15.00),
    "claude-haiku-4-5": Price(1.00, 5.00),
}


def normalize(model: str) -> str:
    """Reduces a platform alias to the first-party id."""
    name = (model or "").strip().lower()
    for prefix in ("anthropic.", "us.anthropic.", "eu.anthropic."):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    name = name.split("@", 1)[0]  # Vertex version suffix
    # OpenRouter: `anthropic/claude-haiku-4.5` and the `:thinking`/`:beta`
    # variants, which are priced the same as the base model.
    if name.startswith("anthropic/"):
        name = name[len("anthropic/"):].split(":", 1)[0].replace(".", "-")
    if name in PRICES:
        return name
    # Dated snapshots: `claude-haiku-4-5-20251001` → `claude-haiku-4-5`.
    parts = name.split("-")
    while len(parts) > 2:
        parts.pop()
        candidate = "-".join(parts)
        if candidate in PRICES:
            return candidate
    return name


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
        + cache_read_tokens * per_token_in * CACHE_READ_MULTIPLIER
    )


def format_usd(value: float | None) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "$0.00"  # "nothing paid" does not need four decimals
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"
