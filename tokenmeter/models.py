"""The single event that every collector produces."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

PROVIDERS = ("anthropic", "openai", "google", "ollama", "unknown")
SOURCES = ("proxy", "transcript", "codex")

# How the call is paid for. Each collector decides from what it can see; if it
# cannot tell, it is `unknown`: billing is never assumed (a guessed "free"
# subscription call would hide real API spend).
BILLING_SUBSCRIPTION = "subscription"  # Pro, Max, ChatGPT Plus…: flat fee
BILLING_API = "api"                    # API key: every token is paid
BILLING_LOCAL = "local"                # own model (Ollama): no cost
BILLING_UNKNOWN = "unknown"
BILLINGS = (BILLING_SUBSCRIPTION, BILLING_API, BILLING_LOCAL, BILLING_UNKNOWN)


@dataclass(frozen=True)
class UsageEvent:
    """Token usage of a single model call.

    Every collector normalizes to this shape before touching the store, so
    the rest of the app does not know which provider the data came from.
    """

    ts: float
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    # Subset of `cache_write_tokens` written with a 1-hour TTL, which costs 2x
    # the input price versus 1.25x for the 5-minute TTL. Claude Code uses 1 hour.
    cache_write_1h_tokens: int = 0
    source: str = "proxy"
    dedupe_key: str | None = None
    usage_complete: bool = True
    billing: str = BILLING_UNKNOWN
    # Plan name when it is a subscription ("pro", "max", "plus"). Empty if
    # unknown or if it is API.
    plan: str = ""
    # Alternative identifiers of the same generation, never credentials.
    aliases: tuple[str, ...] = ()

    @property
    def io_tokens(self) -> int:
        """Default percentage basis: what you actually sent and got back."""
        return self.input_tokens + self.output_tokens

    @property
    def all_tokens(self) -> int:
        return self.io_tokens + self.cache_write_tokens + self.cache_read_tokens

    def with_fallback_key(self) -> UsageEvent:
        """Fills in `dedupe_key` when the provider gave no identifier.

        The synthetic key avoids duplicates from exact retries, but it cannot
        match across collectors: two views of the same request with no shared id
        would be counted twice. That is why the event is marked as incomplete.
        """
        if self.dedupe_key:
            return self
        raw = "|".join(
            str(x)
            for x in (
                round(self.ts, 3),
                self.provider,
                self.model,
                self.input_tokens,
                self.output_tokens,
                self.cache_write_tokens,
                self.cache_read_tokens,
            )
        )
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return replace(self, dedupe_key=f"synth:{digest}", usage_complete=False)
