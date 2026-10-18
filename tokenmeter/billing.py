"""How each call is paid for: subscription, API key, local, or unknown.

The "API equivalent" of a call made under a subscription is not money spent;
that of a call made with an API key is. Mixing them presents money that was
never spent as if it had been. Each collector decides from what it can see:

* **Proxy**: the credential of the request itself. Only its *shape* is looked
  at and then discarded; it is never stored or logged.
* **Claude Code transcripts**: the lines do not say how the call was paid, so
  Claude Code's authentication state on disk is used. That is the state *now*:
  a backfill of old transcripts inherits the current mode (a known limitation).
* **Codex**: its collector resolves it from each session's `plan_type`.

When it cannot be inferred, `unknown`. The manual per-provider setting
(`billing_overrides` in the configuration) only fills those gaps: a certain
detection is not overridden by a preference.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .models import (
    BILLING_API,
    BILLING_LOCAL,
    BILLING_SUBSCRIPTION,
    BILLING_UNKNOWN,
    BILLINGS,
)

Billing = tuple[str, str]  # (type, plan)
UNKNOWN: Billing = (BILLING_UNKNOWN, "")

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def classify_credential(value: str) -> str:
    """Billing type according to the shape of a credential (without the "Bearer")."""
    v = value.strip()
    if not v:
        return BILLING_UNKNOWN
    if v.startswith("sk-ant-oat"):
        return BILLING_SUBSCRIPTION  # Claude Code OAuth token with Pro/Max
    if v.startswith("eyJ"):
        # JWT: the ChatGPT login used by Codex. API keys are not JWTs.
        return BILLING_SUBSCRIPTION
    return BILLING_API


def classify_request(headers: dict, upstream_url: str, query: dict | None = None) -> Billing:
    """Classifies a proxy request. `headers` and `query` do not leave this function.

    The credential takes precedence over the destination: a gateway on localhost
    (LiteLLM, for example) forwards to a paid API with a key, and marking it
    "local, no cost" would be a believable and false number. Only a request
    **without** a credential to a local upstream counts as an own model (Ollama).
    """
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    for name in ("x-api-key", "x-goog-api-key", "api-key"):
        if lowered.get(name):
            return (classify_credential(lowered[name]), "")
    auth = lowered.get("authorization", "")
    if auth.lower().startswith("bearer ") and auth[7:].strip():
        return (classify_credential(auth[7:]), "")
    if query and query.get("key"):  # Gemini accepts the key in the URL
        return (BILLING_API, "")
    if (urlsplit(upstream_url).hostname or "") in LOCAL_HOSTS:
        return (BILLING_LOCAL, "")
    return UNKNOWN


def claude_code_billing(home: Path | None = None) -> Billing:
    """How Claude Code is authenticated on this machine.

    * `~/.claude/.credentials.json` with `claudeAiOauth.subscriptionType` →
      subscription, with that plan;
    * `primaryApiKey` in `~/.claude.json` or `apiKeyHelper` in
      `~/.claude/settings.json` → API key;
    * both at once → it is unknown which one each call uses: `unknown`.
    """
    home = Path(home or os.path.expanduser("~"))
    oauth_plan = None
    try:
        cred = json.loads((home / ".claude" / ".credentials.json").read_text(encoding="utf-8"))
        oauth = cred.get("claudeAiOauth") if isinstance(cred, dict) else None
        if isinstance(oauth, dict) and oauth.get("accessToken"):
            oauth_plan = str(oauth.get("subscriptionType") or "")
    except (OSError, ValueError):
        pass

    has_key = False
    try:
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        has_key = isinstance(data, dict) and bool(data.get("primaryApiKey"))
    except (OSError, ValueError):
        pass
    try:
        settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        has_key = has_key or (isinstance(settings, dict) and bool(settings.get("apiKeyHelper")))
    except (OSError, ValueError):
        pass

    if oauth_plan is not None and has_key:
        return UNKNOWN
    if oauth_plan is not None:
        return (BILLING_SUBSCRIPTION, oauth_plan)
    if has_key:
        return (BILLING_API, "")
    return UNKNOWN


class CachedBilling:
    """Rereads the authentication at most every `ttl` seconds.

    The transcripts collector scans every 2 s; reading three JSON files on every
    scan is unnecessary, but a login change must be noticed soon.
    """

    def __init__(self, fn=claude_code_billing, ttl: float = 60.0) -> None:
        self._fn = fn
        self._ttl = ttl
        self._value: Billing | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> Billing:
        with self._lock:
            now = time.monotonic()
            if self._value is None or now - self._at >= self._ttl:
                try:
                    self._value = self._fn()
                except Exception:
                    self._value = UNKNOWN
                self._at = now
            return self._value


def apply_override(provider: str, billing: Billing, overrides: dict | None) -> Billing:
    """The manual per-provider setting only fills what could not be detected."""
    if billing[0] != BILLING_UNKNOWN or not overrides:
        return billing
    forced = overrides.get(provider)
    if forced in BILLINGS and forced != BILLING_UNKNOWN:
        return (forced, "")
    return billing


LABELS = {
    BILLING_SUBSCRIPTION: "Subscription",
    BILLING_API: "Paid API",
    BILLING_LOCAL: "Local",
    BILLING_UNKNOWN: "Unknown billing",
}


def label(billing: str, plan: str = "") -> str:
    base = LABELS.get(billing, LABELS[BILLING_UNKNOWN])
    return f"{base} · {plan.capitalize()}" if billing == BILLING_SUBSCRIPTION and plan else base
