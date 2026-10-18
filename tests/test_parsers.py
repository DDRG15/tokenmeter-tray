from pathlib import Path

import pytest

from tokenmeter.collectors.parsers import UsageSink, looks_like_completion

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run_sink(sink: UsageSink, payload: bytes, chunk_size: int = 64):
    """Feeds the sink in chunks, as the proxy would with a real stream."""
    for i in range(0, len(payload), chunk_size):
        sink.feed(payload[i : i + chunk_size])
    return sink.finish(ts=1000.0)


# -- Anthropic -------------------------------------------------------------


def test_anthropic_json_response():
    sink = UsageSink(
        "anthropic",
        path="/v1/messages",
        content_type="application/json",
        request_id="req_011XYZ",
    )
    ev = run_sink(sink, read("anthropic_messages.json"))

    assert ev.provider == "anthropic"
    assert ev.model == "claude-opus-5"
    assert ev.input_tokens == 1234
    assert ev.output_tokens == 87
    assert ev.cache_write_tokens == 512
    assert ev.cache_read_tokens == 20480
    assert ev.usage_complete is True
    assert ev.dedupe_key == "req:req_011XYZ"


def test_anthropic_sse_takes_final_cumulative_output():
    sink = UsageSink(
        "anthropic",
        path="/v1/messages",
        content_type="text/event-stream",
        request_id="req_stream",
    )
    ev = run_sink(sink, read("anthropic_stream.sse"))

    assert ev.input_tokens == 1234, "input comes from message_start"
    assert ev.output_tokens == 87, "message_delta wins, not the initial 2"
    assert ev.cache_write_tokens == 512
    assert ev.cache_read_tokens == 20480
    assert ev.usage_complete is True


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 100_000])
def test_sse_parsing_is_independent_of_chunk_boundaries(chunk_size):
    """The proxy chunks wherever the network does; the result cannot depend on that."""
    sink = UsageSink("anthropic", path="/v1/messages", content_type="text/event-stream")
    ev = run_sink(sink, read("anthropic_stream.sse"), chunk_size=chunk_size)
    assert (ev.input_tokens, ev.output_tokens) == (1234, 87)


def test_anthropic_prefers_request_id_over_body_id():
    """It is the only key that also exists in Claude Code transcripts."""
    sink = UsageSink("anthropic", path="/v1/messages", content_type="application/json",
                     request_id="req_from_header")
    ev = run_sink(sink, read("anthropic_messages.json"))
    assert ev.dedupe_key == "req:req_from_header"

    sink = UsageSink("anthropic", path="/v1/messages", content_type="application/json")
    ev = run_sink(sink, read("anthropic_messages.json"))
    assert ev.dedupe_key == "msg:msg_01ABCdefGHIjklMNOpqrs"


# -- OpenAI and compatibles --------------------------------------------------


def test_openai_json_separates_cached_tokens_from_prompt():
    sink = UsageSink("openai", path="/v1/chat/completions", content_type="application/json")
    ev = run_sink(sink, read("openai_chat.json"))

    assert ev.model == "gpt-4o-mini"
    # OpenAI puts the cached tokens inside prompt_tokens; separating them avoids
    # counting them twice when the % uses the "all" basis.
    assert ev.input_tokens == 220
    assert ev.cache_read_tokens == 1280
    assert ev.input_tokens + ev.cache_read_tokens == 1500
    assert ev.output_tokens == 42


def test_openai_sse_with_include_usage():
    sink = UsageSink("openai", path="/v1/chat/completions", content_type="text/event-stream")
    ev = run_sink(sink, read("openai_stream_with_usage.sse"))

    assert ev.input_tokens == 220
    assert ev.output_tokens == 42
    assert ev.usage_complete is True


def test_openai_sse_without_usage_is_flagged_not_silently_zero():
    """Without `stream_options.include_usage` there are no numbers. It must be noticed."""
    sink = UsageSink("openai", path="/v1/chat/completions", content_type="text/event-stream")
    ev = run_sink(sink, read("openai_stream_no_usage.sse"))

    assert ev is not None
    assert ev.usage_complete is False
    assert ev.model == "gpt-4o-mini"
    assert ev.io_tokens == 0


def test_openrouter_uses_openai_shape_but_keeps_its_label():
    sink = UsageSink("openrouter", path="/v1/chat/completions", content_type="application/json")
    ev = run_sink(sink, read("openai_chat.json"))
    assert ev.provider == "openrouter"
    assert ev.output_tokens == 42


# -- Google ----------------------------------------------------------------


def test_google_counts_thinking_tokens_as_output():
    sink = UsageSink("google", path="/v1beta/models/gemini-2.5-pro:generateContent",
                     content_type="application/json")
    ev = run_sink(sink, read("google_generate.json"))

    assert ev.model == "gemini-2.5-pro"
    assert ev.input_tokens == 500, "promptTokenCount minus the cached tokens"
    assert ev.cache_read_tokens == 400
    assert ev.output_tokens == 150, "candidates + thoughts"


# -- Ollama ----------------------------------------------------------------


def test_ollama_ndjson_stream():
    sink = UsageSink("ollama", path="/api/chat", content_type="application/x-ndjson")
    ev = run_sink(sink, read("ollama_stream.ndjson"))

    assert ev.model == "llama3.2"
    assert ev.input_tokens == 26
    assert ev.output_tokens == 298


# -- Edge cases -----------------------------------------------------------


def test_non_completion_endpoint_produces_no_event():
    sink = UsageSink("anthropic", path="/v1/models", content_type="application/json")
    assert run_sink(sink, b'{"data": [{"id": "claude-opus-5"}]}') is None


def test_count_tokens_endpoint_is_not_billed_usage():
    sink = UsageSink("anthropic", path="/v1/messages/count_tokens",
                     content_type="application/json")
    assert looks_like_completion("/v1/messages/count_tokens") is False
    assert run_sink(sink, b'{"input_tokens": 42}') is None


def test_malformed_body_never_raises():
    sink = UsageSink("anthropic", path="/v1/messages", content_type="application/json")
    ev = run_sink(sink, b"<html>502 Bad Gateway</html>")
    assert ev is not None and ev.usage_complete is False


def test_error_response_from_provider_is_flagged_incomplete():
    sink = UsageSink("anthropic", path="/v1/messages", content_type="application/json")
    ev = run_sink(sink, b'{"type":"error","error":{"type":"overloaded_error"}}')
    assert ev.usage_complete is False
    assert ev.io_tokens == 0


def test_model_falls_back_to_request_body():
    sink = UsageSink("openai", path="/v1/chat/completions",
                     content_type="text/event-stream", fallback_model="gpt-5")
    ev = run_sink(sink, b"data: {}\n\ndata: [DONE]\n\n")
    assert ev.model == "gpt-5"
