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

# Unrecognized credentials at least this long count as API evidence. Shorter
# values are ambiguous unless they are known placeholders or have a vendor prefix.
MIN_REAL_KEY_LENGTH = 20
# Prefixes issued by the providers in `DEFAULT_UPSTREAMS` and their neighbours.
# A value that starts with one is a key, not a placeholder.
VENDOR_KEY_PREFIXES = ("sk-ant-", "sk-proj-", "sk-svcacct-", "sk-admin-", "sk-or-",
                       "gsk_", "aiza", "xai-")
# Only these well-known dummy values count as placeholders (case-insensitive).
PLACEHOLDER_CREDENTIALS = frozenset({
    "ollama", "lm-studio", "lmstudio", "local", "sk-local", "not-needed",
    "no-key", "no-key-required", "sk-no-key-required", "none", "null", "empty",
    "dummy", "sk-dummy", "api-key", "your-api-key", "your-api-key-here",
    "sk-your-api-key-here", "sk-xxx", "token-abc123",
})


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


def is_placeholder_credential(value: str) -> bool:
    """True only for a known dummy credential, never merely because it is short.

    A local gateway may forward paid requests using a short real secret.
    Unrecognized short values therefore need unknown billing, while known
    placeholders remain useful for clients whose SDK requires a key.
    """
    return value.strip().lower() in PLACEHOLDER_CREDENTIALS


def classify_request(headers: dict, upstream_url: str, query: dict | None = None) -> Billing:
    """Classify credentials without storing them or guessing a gateway is free.

    Local destinations without credentials or with only known placeholders are
    local. A provider prefix or an unrecognized credential of at least 20
    characters is API evidence; subscription credentials keep their own type.
    Other local credentials are ambiguous and stay unknown, so the existing
    per-provider override can explicitly resolve them.

    Credentials against remote destinations retain their existing classification.
    """
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    credentials: list[str] = []  # in order of precedence
    for name in ("x-api-key", "x-goog-api-key", "api-key"):
        if lowered.get(name):
            credentials.append(lowered[name])
    auth = lowered.get("authorization", "")
    if auth.lower().startswith("bearer ") and auth[7:].strip():
        credentials.append(auth[7:])
    # Gemini also accepts the API key in the URL.
    url_key = str(query["key"]) if query and query.get("key") else ""

    is_local = (urlsplit(upstream_url).hostname or "") in LOCAL_HOSTS
    offered = [*credentials, url_key] if url_key else credentials
    if is_local:
        supplied = [value.strip() for value in offered if value.strip()]
        for value in supplied:
            kind = classify_credential(value)
            if kind == BILLING_SUBSCRIPTION:
                return (kind, "")
            if not is_placeholder_credential(value) and (
                    len(value) >= MIN_REAL_KEY_LENGTH or value.lower().startswith(VENDOR_KEY_PREFIXES)):
                return (BILLING_API, "")
        if all(is_placeholder_credential(value) for value in supplied):
            return (BILLING_LOCAL, "")
        return UNKNOWN

    if credentials:
        return (classify_credential(credentials[0]), "")
    if url_key:
        return (BILLING_API, "")
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
