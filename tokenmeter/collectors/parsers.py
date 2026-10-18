"""Extraction of `usage` per provider, from full JSON and from streams.

Deliberate separation:

* `UsageSink` handles the **transport** — JSON body, SSE (`data: {...}`) or
  NDJSON — and splits it into individual JSON objects.
* The `extract_*` functions handle each provider's **shape** on an already
  parsed object.

That way a new provider with an OpenAI shape is supported by adding a row to
the map, without touching stream handling.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace

from ..models import UsageEvent

# Paths that produce tokens. Used so a call to /v1/models or to a health
# endpoint is not flagged as "usage lost".
COMPLETION_PATH_HINTS = (
    "/messages",
    "/chat/completions",
    "/completions",
    "/responses",
    "generatecontent",
    "/api/generate",
    "/api/chat",
)


def looks_like_completion(path: str) -> bool:
    lowered = path.lower()
    if lowered.endswith("/count_tokens"):
        return False
    return any(hint in lowered for hint in COMPLETION_PATH_HINTS)


@dataclass
class PartialUsage:
    """Tolerant accumulator: each chunk of the stream contributes what it knows."""

    model: str | None = None
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_1h_tokens: int | None = None

    @property
    def has_tokens(self) -> bool:
        return self.input_tokens is not None or self.output_tokens is not None

    def merge(self, other: "PartialUsage") -> None:
        # Identity and input counters: the first one to arrive wins
        # (Anthropic's `message_start` carries them complete and they do not change).
        for name in ("model", "response_id", "input_tokens", "cache_write_tokens",
                     "cache_read_tokens", "cache_write_1h_tokens"):
            if getattr(self, name) is None and getattr(other, name) is not None:
                setattr(self, name, getattr(other, name))
        # Output tokens are cumulative: the last value seen wins.
        if other.output_tokens is not None:
            self.output_tokens = other.output_tokens


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# -- extractors per provider shape -----------------------------------


def extract_anthropic(obj: dict) -> PartialUsage:
    """Messages API. Covers a full response, `message_start` and `message_delta`."""
    out = PartialUsage()
    message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    if isinstance(message, dict):
        out.model = message.get("model") or None
        out.response_id = message.get("id") or None
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
    if isinstance(usage, dict):
        out.input_tokens = _int(usage.get("input_tokens"))
        out.output_tokens = _int(usage.get("output_tokens"))
        out.cache_write_tokens = _int(usage.get("cache_creation_input_tokens"))
        out.cache_read_tokens = _int(usage.get("cache_read_input_tokens"))
        # Breakdown by TTL: 1 h cache costs 2x the input, 5 min cache 1.25x.
        creation = usage.get("cache_creation")
        if isinstance(creation, dict):
            out.cache_write_1h_tokens = _int(creation.get("ephemeral_1h_input_tokens"))
    return out


def extract_openai(obj: dict) -> PartialUsage:
    """OpenAI shape: shared by OpenRouter, Groq, and Gemini in compat mode."""
    if isinstance(obj.get("response"), dict):
        obj = obj["response"]
    out = PartialUsage(
        model=obj.get("model") or None,
        response_id=obj.get("id") or None,
    )
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        # Groq streaming: the last chunk's usage is in `x_groq.usage`.
        x_groq = obj.get("x_groq")
        usage = x_groq.get("usage") if isinstance(x_groq, dict) else None
    if isinstance(usage, dict):
        # `/v1/responses` uses input_tokens/output_tokens; the rest prompt/completion.
        out.input_tokens = _int(usage.get("prompt_tokens"))
        if out.input_tokens is None:
            out.input_tokens = _int(usage.get("input_tokens"))
        out.output_tokens = _int(usage.get("completion_tokens"))
        if out.output_tokens is None:
            out.output_tokens = _int(usage.get("output_tokens"))

        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
        cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else None
        if not cached:
            # DeepSeek: prompt_tokens = hit + miss. Its response repeats the hit in
            # `cached_tokens`, but that cannot be relied on: if it is missing, the
            # hit arrives only in `prompt_cache_hit_tokens`.
            cached = _int(usage.get("prompt_cache_hit_tokens"))
        if cached:
            out.cache_read_tokens = cached
            # OpenAI counts the cached ones INSIDE prompt_tokens; here they are
            # separated so they are not counted twice in the total.
            if out.input_tokens is not None:
                out.input_tokens = max(0, out.input_tokens - cached)

        # OpenRouter with explicit cache (Claude): the write is also inside
        # prompt_tokens according to its docs (194 prompt, 100 written, total
        # 196). Inferred, not measured: if the write does not fit in what is left
        # of the input, the assumption is false for that response and nothing
        # is touched.
        written = _int(details.get("cache_write_tokens")) if isinstance(details, dict) else None
        if written and out.input_tokens is not None and written <= out.input_tokens:
            out.cache_write_tokens = written
            out.input_tokens -= written
    return out


def extract_google(obj: dict) -> PartialUsage:
    """Native Gemini API (`usageMetadata`)."""
    out = PartialUsage(model=obj.get("modelVersion") or obj.get("model") or None,
                       response_id=obj.get("responseId") or None)
    meta = obj.get("usageMetadata")
    if isinstance(meta, dict):
        cached = _int(meta.get("cachedContentTokenCount")) or 0
        prompt = _int(meta.get("promptTokenCount"))
        out.input_tokens = max(0, prompt - cached) if prompt is not None else None
        out.cache_read_tokens = cached or None
        thoughts = _int(meta.get("thoughtsTokenCount")) or 0
        candidates = _int(meta.get("candidatesTokenCount"))
        if candidates is not None:
            out.output_tokens = candidates + thoughts
    return out


def extract_ollama(obj: dict) -> PartialUsage:
    return PartialUsage(
        model=obj.get("model") or None,
        input_tokens=_int(obj.get("prompt_eval_count")),
        output_tokens=_int(obj.get("eval_count")),
    )


EXTRACTORS = {
    "anthropic": extract_anthropic,
    "openai": extract_openai,
    "openrouter": extract_openai,
    "groq": extract_openai,
    "google": extract_google,
    "ollama": extract_ollama,
}


def detect_extractor(obj: dict):
    """Infers the shape from the object's keys.

    Needed because the provider name is chosen by the user in `config.json`: an
    upstream called `my-claude` is not in the map and without this it would fall
    back to the OpenAI format, returning plausible but wrong numbers —it would
    lose the cache tokens without saying anything.
    """
    if isinstance(obj.get("response"), dict):
        return extract_openai
    if isinstance(obj.get("usageMetadata"), dict):
        return extract_google
    if "prompt_eval_count" in obj or "eval_count" in obj:
        return extract_ollama

    usage = obj.get("usage")
    if not isinstance(usage, dict):
        message = obj.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        x_groq = obj.get("x_groq")  # Groq streaming: usage inside x_groq
        usage = x_groq.get("usage") if isinstance(x_groq, dict) else None
    if isinstance(usage, dict):
        # Keys exclusive to each format, in order of specificity.
        if "cache_creation_input_tokens" in usage or "cache_read_input_tokens" in usage:
            return extract_anthropic
        if ("prompt_tokens" in usage or "completion_tokens" in usage
                or "prompt_tokens_details" in usage or "input_tokens_details" in usage):
            return extract_openai
        if "input_tokens" in usage or "output_tokens" in usage:
            return extract_anthropic

    if isinstance(obj.get("type"), str) and obj["type"].startswith("message"):
        return extract_anthropic
    return None

# Which provider the event is recorded under (several upstreams share a shape).
PROVIDER_LABEL = {
    "openrouter": "openrouter",
    "groq": "groq",
}


class UsageSink:
    """Receives a copy of the response and produces a `UsageEvent` at the end.

    It never raises toward the proxy: if the body is not what was expected,
    `finish()` returns `None` or an event marked as incomplete, but the user's
    request was already forwarded intact.
    """

    MAX_BUFFER = 4 * 1024 * 1024  # non-stream bodies larger than this are discarded

    def __init__(
        self,
        provider: str,
        *,
        path: str = "",
        content_type: str = "",
        request_id: str | None = None,
        fallback_model: str | None = None,
    ) -> None:
        self.provider = provider
        self.path = path
        self.request_id = request_id
        self.fallback_model = fallback_model
        self._extract = EXTRACTORS.get(provider)
        self._acc = PartialUsage()
        self._saw_json = False

        ctype = (content_type or "").lower()
        self._streaming = "event-stream" in ctype or "ndjson" in ctype or "stream+json" in ctype
        self._line_buffer = b""
        self._body_buffer = bytearray()
        self._overflowed = False
        self._terminal_incomplete = False

    # -- input -----------------------------------------------------------

    def feed(self, chunk: bytes) -> None:
        try:
            if self._streaming:
                self._feed_lines(chunk)
            else:
                if len(self._body_buffer) + len(chunk) > self.MAX_BUFFER:
                    self._overflowed = True
                    return
                self._body_buffer.extend(chunk)
        except Exception:
            # A broken parser cannot affect the passthrough.
            self._overflowed = True

    def _feed_lines(self, chunk: bytes) -> None:
        self._line_buffer += chunk
        if len(self._line_buffer) > self.MAX_BUFFER and b"\n" not in self._line_buffer:
            self._line_buffer = b""
            self._overflowed = True
            return
        while b"\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split(b"\n", 1)
            self._consume_line(line)

    def _consume_line(self, raw: bytes) -> None:
        line = raw.strip()
        if not line:
            return
        if line.startswith(b"event:") or line.startswith(b":"):
            return  # SSE event name or heartbeat
        if line.startswith(b"data:"):
            line = line[5:].strip()
        if not line or line == b"[DONE]":
            return
        self._consume_json(line)

    def _consume_json(self, raw: bytes) -> None:
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    self._merge(item)
            return
        if isinstance(obj, dict):
            self._merge(obj)

    def _merge(self, obj: dict) -> None:
        self._saw_json = True
        if obj.get("type") in {"response.failed", "response.incomplete"}:
            self._terminal_incomplete = True
        partial = self._extract(obj) if self._extract else None
        # If the configured extractor gets nothing, retry with the one the
        # object's own shape suggests. It covers providers with a custom name
        # and gateways that respond in a format different from what their name
        # suggests.
        if partial is None or not partial.has_tokens:
            detected = detect_extractor(obj)
            if detected is not None and detected is not self._extract:
                fallback = detected(obj)
                if fallback.has_tokens:
                    partial = fallback
        if partial is not None:
            self._acc.merge(partial)

    # -- output ------------------------------------------------------------

    def finish(self, ts: float | None = None) -> UsageEvent | None:
        if not self._streaming and not self._overflowed and self._body_buffer:
            self._consume_json(bytes(self._body_buffer))
        elif self._streaming and self._line_buffer.strip():
            self._consume_line(self._line_buffer)

        if not self._saw_json and not looks_like_completion(self.path):
            return None  # it was not a generation call

        acc = self._acc
        model = acc.model or self.fallback_model or "unknown"
        complete = acc.has_tokens and not self._overflowed and not self._terminal_incomplete

        if not complete and not looks_like_completion(self.path):
            return None

        return UsageEvent(
            ts=time.time() if ts is None else ts,
            provider=PROVIDER_LABEL.get(self.provider, self.provider),
            model=model,
            input_tokens=acc.input_tokens or 0,
            output_tokens=acc.output_tokens or 0,
            cache_write_tokens=acc.cache_write_tokens or 0,
            cache_read_tokens=acc.cache_read_tokens or 0,
            cache_write_1h_tokens=acc.cache_write_1h_tokens or 0,
            source="proxy",
            dedupe_key=self._dedupe_key(acc),
            usage_complete=complete,
            aliases=tuple(k for k in (
                f"req:{self.request_id}" if self.request_id else None,
                f"msg:{acc.response_id}" if acc.response_id else None,
            ) if k),
        )

    def _dedupe_key(self, acc: PartialUsage) -> str | None:
        # The provider's `request-id` header is the only key that also appears
        # in Claude Code transcripts, so it takes precedence over the body id:
        # it is what allows matching proxy and transcripts without doubling.
        if self.request_id:
            return f"req:{self.request_id}"
        if acc.response_id:
            return f"msg:{acc.response_id}"
        return None


def sink_for_response(
    provider: str,
    path: str,
    response_headers: dict,
    request_body: bytes | None = None,
) -> UsageSink:
    """Builds the sink from the upstream's real response."""
    lowered = {k.lower(): v for k, v in response_headers.items()}
    return UsageSink(
        provider,
        path=path,
        content_type=lowered.get("content-type", ""),
        request_id=lowered.get("request-id") or lowered.get("x-request-id"),
        fallback_model=model_from_request(request_body),
    )


def model_from_request(body: bytes | None) -> str | None:
    """Last resort for the model name when the response does not carry it."""
    if not body:
        return None
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(obj, dict):
        model = obj.get("model")
        return model if isinstance(model, str) else None
    return None


def with_source(event: UsageEvent, source: str) -> UsageEvent:
    return replace(event, source=source)
