"""Synthetic OpenRouter, Groq and DeepSeek responses against shape-based detection.

All three speak "OpenAI" but each adds its own traits: OpenRouter injects
`provider`, `cost` and SSE comments; Groq measures timings and, when streaming,
delivers the `usage` inside `x_groq`; DeepSeek splits the cache into
`prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`. If any of them changes
shape, `detect_extractor` has to keep choosing correctly or the counter stays at
zero without saying anything.

Project semantics: `prompt_tokens` includes the cached tokens, so the event's
`input_tokens` is `prompt_tokens - cached` and `cache_read_tokens` are the
cached ones.
"""

import json
from pathlib import Path

import pytest

from tokenmeter.collectors.parsers import (
    UsageSink,
    detect_extractor,
    extract_openai,
)

FIXTURES = Path(__file__).parent / "fixtures"
PATH = "/v1/chat/completions"


def read_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def objects(name: str) -> list[dict]:
    """JSON objects of a fixture, whether a full body or an SSE stream.

    Ignores comments (`:`), `event:` and `[DONE]`, just like the sink.
    """
    raw = read_fixture(name).decode("utf-8")
    if name.endswith(".json"):
        return [json.loads(raw)]
    out = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                out.append(json.loads(payload))
    return out


def run(sink: UsageSink, payload: bytes, chunk: int = 64):
    for i in range(0, len(payload), chunk):
        sink.feed(payload[i : i + chunk])
    return sink.finish(ts=1000.0)


def content_type_for(name: str) -> str:
    return "text/event-stream" if name.endswith(".sse") else "application/json"


# (fixture, model, input, output, cache_read)
# input = prompt_tokens - cached; output = completion_tokens.
CASES = [
    pytest.param("openrouter_chat.json", "deepseek/deepseek-r1", 220, 342, 1280,
                 id="openrouter-json"),
    pytest.param("openrouter_stream.sse", "deepseek/deepseek-r1", 220, 342, 1280,
                 id="openrouter-sse"),
    pytest.param("groq_chat.json", "llama-3.3-70b-versatile", 18, 556, 0,
                 id="groq-json"),
    pytest.param("groq_stream.sse", "llama-3.3-70b-versatile", 46, 112, 0,
                 id="groq-sse-x_groq-usage"),
    pytest.param("deepseek_chat.json", "deepseek-reasoner", 220, 342, 1280,
                 id="deepseek-json-with-details"),
    pytest.param("deepseek_chat_hit_miss_only.json", "deepseek-chat", 220, 42, 1280,
                 id="deepseek-json-hit-miss-only"),
    pytest.param("deepseek_stream.sse", "deepseek-chat", 220, 42, 1280,
                 id="deepseek-sse"),
]


# -- detect_extractor ------------------------------------------------------


@pytest.mark.parametrize("fixture", [c.values[0] for c in CASES])
def test_detect_extractor_picks_the_openai_shape_with_usage(fixture):
    """The object carrying the `usage` has to resolve to `extract_openai`."""
    with_usage = [o for o in objects(fixture) if extract_openai(o).has_tokens]
    assert with_usage, "no object in the fixture contributes tokens"
    for obj in with_usage:
        assert detect_extractor(obj) is extract_openai


@pytest.mark.parametrize("fixture", [c.values[0] for c in CASES])
def test_detect_extractor_ignores_chunks_without_usage(fixture):
    """Content chunks must not trigger any extractor.

    If they did, an intermediate chunk without numbers would overwrite the result.
    """
    without_usage = [o for o in objects(fixture) if not extract_openai(o).has_tokens]
    for obj in without_usage:
        assert detect_extractor(obj) is None


# -- Extracted numbers, with a mapped provider or an invented name ---------


@pytest.mark.parametrize("fixture,model,input_tokens,output_tokens,cache", CASES)
@pytest.mark.parametrize("provider", ["openrouter", "groq", "deepseek", "my-gateway"])
def test_numbers_do_not_depend_on_the_provider_name(
    fixture, model, input_tokens, output_tokens, cache, provider
):
    """The user picks the name in config.json; the shape decides."""
    sink = UsageSink(provider, path=PATH, content_type=content_type_for(fixture))
    ev = run(sink, read_fixture(fixture))

    assert ev is not None
    assert ev.usage_complete is True
    assert ev.model == model
    assert ev.input_tokens == input_tokens
    assert ev.output_tokens == output_tokens
    assert ev.cache_read_tokens == cache
    assert ev.cache_write_tokens == 0


@pytest.mark.parametrize("fixture,model,input_tokens,output_tokens,cache", CASES)
@pytest.mark.parametrize("chunk", [1, 7, 100_000])
def test_result_does_not_depend_on_where_the_network_cuts(
    fixture, model, input_tokens, output_tokens, cache, chunk
):
    sink = UsageSink("my-gateway", path=PATH, content_type=content_type_for(fixture))
    ev = run(sink, read_fixture(fixture), chunk=chunk)
    assert (ev.input_tokens, ev.output_tokens, ev.cache_read_tokens) == (
        input_tokens, output_tokens, cache,
    )


@pytest.mark.parametrize("fixture,model,input_tokens,output_tokens,cache", CASES)
def test_cached_tokens_are_not_counted_twice(fixture, model, input_tokens, output_tokens, cache):
    """input + cache_read rebuilds prompt_tokens; it never exceeds it."""
    prompt_tokens = next(
        o["usage"]["prompt_tokens"] if "usage" in o and o["usage"]
        else o["x_groq"]["usage"]["prompt_tokens"]
        for o in reversed(objects(fixture))
        if o.get("usage") or (o.get("x_groq") or {}).get("usage")
    )
    sink = UsageSink("my-gateway", path=PATH, content_type=content_type_for(fixture))
    ev = run(sink, read_fixture(fixture))
    assert ev.input_tokens + ev.cache_read_tokens == prompt_tokens


# -- Provider label --------------------------------------------------------


def test_openrouter_and_groq_keep_their_label():
    for provider, fixture in (("openrouter", "openrouter_chat.json"),
                              ("groq", "groq_chat.json")):
        sink = UsageSink(provider, path=PATH, content_type="application/json")
        assert run(sink, read_fixture(fixture)).provider == provider


# -- OpenRouter ------------------------------------------------------------


def test_openrouter_sse_comments_generate_no_events_or_noise():
    """`: OPENROUTER PROCESSING` arrives between chunks; it is neither JSON nor usage."""
    raw = read_fixture("openrouter_stream.sse")
    assert raw.count(b": OPENROUTER PROCESSING") == 3

    sink = UsageSink("openrouter", path=PATH, content_type="text/event-stream")
    ev = run(sink, raw, chunk=5)
    assert ev.usage_complete is True
    assert ev.dedupe_key == "msg:gen-synthetic-openrouter-stream"


def test_openrouter_stream_with_only_comments_is_marked_incomplete():
    """If the stream dies before the usage chunk, it is not a silent zero."""
    cut = read_fixture("openrouter_stream.sse").split(b"data: {\"id\":\"gen-synthetic-openrouter-stream\"")[0]
    sink = UsageSink("openrouter", path=PATH, content_type="text/event-stream",
                     fallback_model="deepseek/deepseek-r1")
    ev = run(sink, cut)
    assert ev is not None
    assert ev.usage_complete is False
    assert ev.io_tokens == 0


def test_openrouter_reasoning_tokens_are_already_inside_completion_tokens():
    """completion_tokens (342) already includes reasoning (300): they are not added again."""
    obj = objects("openrouter_chat.json")[0]
    assert obj["usage"]["completion_tokens_details"]["reasoning_tokens"] == 300
    assert extract_openai(obj).output_tokens == 342


# -- Groq ------------------------------------------------------------------


def test_groq_json_ignores_timing_fields():
    """queue_time/prompt_time/... are floats of seconds: they are not tokens."""
    partial = extract_openai(objects("groq_chat.json")[0])
    assert (partial.input_tokens, partial.output_tokens) == (18, 556)
    assert partial.cache_read_tokens is None


def test_groq_stream_reads_usage_from_x_groq():
    """When streaming, Groq does not put `usage` at the top: it goes in the last chunk's `x_groq.usage`.

    Before the fix `extract_openai` only looked at `obj["usage"]` and the event
    came out with 0 tokens and `usage_complete=False`: a Groq stream counted as zero.
    """
    last = [o for o in objects("groq_stream.sse") if "usage" in o.get("x_groq", {})][-1]
    assert "usage" not in last, "the fixture must reproduce the real shape"

    partial = extract_openai(last)
    assert (partial.input_tokens, partial.output_tokens) == (46, 112)
    assert detect_extractor(last) is extract_openai

    sink = UsageSink("groq", path=PATH, content_type="text/event-stream")
    ev = run(sink, read_fixture("groq_stream.sse"))
    assert ev.usage_complete is True
    assert (ev.input_tokens, ev.output_tokens) == (46, 112)


def test_groq_x_groq_without_usage_contributes_no_tokens():
    """The first chunk's `x_groq` only carries the request id."""
    first = objects("groq_stream.sse")[0]
    assert first["x_groq"] == {"id": "req_synthetic_groq_0001"}
    assert extract_openai(first).has_tokens is False
    assert detect_extractor(first) is None


def test_top_level_usage_wins_over_x_groq():
    """If Groq changes and sends `usage` at the top as well as `x_groq.usage`, it is not duplicated."""
    obj = {
        "id": "chatcmpl-x",
        "model": "llama-3.3-70b-versatile",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "x_groq": {"id": "req_1", "usage": {"prompt_tokens": 99, "completion_tokens": 99}},
    }
    partial = extract_openai(obj)
    assert (partial.input_tokens, partial.output_tokens) == (10, 5)


# -- DeepSeek --------------------------------------------------------------


def test_deepseek_separates_cache_hit_from_miss():
    """prompt_tokens (1500) = hit (1280) + miss (220): input must be the miss."""
    obj = objects("deepseek_chat_hit_miss_only.json")[0]
    usage = obj["usage"]
    assert usage["prompt_tokens"] == (
        usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
    )

    partial = extract_openai(obj)
    assert partial.input_tokens == 220
    assert partial.cache_read_tokens == 1280


def test_deepseek_with_details_and_hit_does_not_subtract_cached_twice():
    """The real response carries `cached_tokens` AND `prompt_cache_hit_tokens`, equal."""
    partial = extract_openai(objects("deepseek_chat.json")[0])
    assert partial.input_tokens == 220, "subtract 1280 once, not twice (it would be 0)"
    assert partial.cache_read_tokens == 1280


def test_deepseek_without_cache_does_not_invent_cache_read():
    obj = {
        "id": "ds-0", "model": "deepseek-chat",
        "usage": {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26,
                  "prompt_tokens_details": {"cached_tokens": 0},
                  "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 17},
    }
    partial = extract_openai(obj)
    assert partial.input_tokens == 17
    assert partial.cache_read_tokens is None


def test_deepseek_hit_larger_than_prompt_does_not_produce_negative_input():
    obj = {"usage": {"prompt_tokens": 100, "completion_tokens": 1,
                     "prompt_cache_hit_tokens": 400}}
    assert extract_openai(obj).input_tokens == 0


def test_deepseek_reasoner_reasoning_is_inside_completion_tokens():
    partial = extract_openai(objects("deepseek_chat.json")[0])
    assert partial.output_tokens == 342


# -- Integrity contract ----------------------------------------------------


@pytest.mark.parametrize("provider", ["openrouter", "groq", "deepseek"])
def test_empty_body_or_null_usage_is_marked_incomplete(provider):
    for body in (b"{}", b'{"id":"x","model":"m","usage":null}',
                 b'{"id":"x","model":"m","usage":{}}'):
        sink = UsageSink(provider, path=PATH, content_type="application/json")
        ev = run(sink, body)
        assert ev is not None
        assert ev.usage_complete is False
        assert ev.io_tokens == 0


# -- Cache writes via OpenRouter ---------------------------------------------
#
# Fixture copied from the OpenRouter docs example (usage accounting):
# 194 prompt + 2 completion = 196 total, with 100 written. If the write were
# separate, the total would be 296. It is not a recording of our own.


@pytest.mark.parametrize("provider", ["openrouter", "my-gateway"])
def test_openrouter_cache_write_is_separated_from_the_input(provider):
    sink = UsageSink(provider, path=PATH, content_type="application/json")
    ev = run(sink, read_fixture("openrouter_claude_cache_write.json"))
    assert ev.model == "anthropic/claude-haiku-4.5"
    assert ev.input_tokens == 94
    assert ev.cache_write_tokens == 100
    assert ev.cache_read_tokens == 0
    assert ev.output_tokens == 2
    # Extended invariant: nothing is counted twice or lost.
    assert ev.input_tokens + ev.cache_write_tokens + ev.cache_read_tokens == 194


def test_openrouter_read_and_write_in_the_same_response():
    obj = {"usage": {"prompt_tokens": 1000, "completion_tokens": 5,
                     "prompt_tokens_details": {"cached_tokens": 600,
                                               "cache_write_tokens": 300}}}
    partial = extract_openai(obj)
    assert (partial.input_tokens, partial.cache_read_tokens,
            partial.cache_write_tokens) == (100, 600, 300)


def test_a_write_that_does_not_fit_in_the_prompt_is_not_subtracted():
    """If OpenRouter counted it separately, subtracting it would leave a false input: it is left alone."""
    obj = {"usage": {"prompt_tokens": 50, "completion_tokens": 5,
                     "prompt_tokens_details": {"cached_tokens": 0,
                                               "cache_write_tokens": 400}}}
    partial = extract_openai(obj)
    assert partial.input_tokens == 50
    assert partial.cache_write_tokens is None


def test_zero_or_absent_write_changes_nothing():
    partial = extract_openai(objects("openrouter_chat.json")[0])
    assert partial.input_tokens == 220
    assert partial.cache_write_tokens is None
