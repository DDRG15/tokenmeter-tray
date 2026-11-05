# Synthetic protocol fixtures

`dbus-session.conf` configures the private Linux test bus without desktop
service activation. It is used with the synthetic indicator host in
`tests/linux_indicator.py`; its temporary socket disappears with the bus.

The provider fixtures model response formats for parser tests. They are not
account exports. Identifiers, paths, timings and session metadata are synthetic
placeholders; fixed token counts exercise cache subtraction, deduplication,
reasoning-token handling and streaming behavior.

The Codex fixture deliberately includes a truncated JSON line to exercise
recovery from an incomplete write. SSE comments and trailing markers are also
intentional protocol cases.

## Coverage

| Files | Protocol cases |
|---|---|
| `anthropic_*` | Messages responses, streaming and cache usage |
| `openai_*` | Chat responses and streams with or without usage |
| `google_generate.json` | Gemini response usage |
| `ollama_stream.ndjson` | Local model streaming usage |
| `openrouter_*` | Compatible chat/streaming and Claude cache fields |
| `groq_*`, `deepseek_*` | Provider-specific streaming and cache fields |
| `codex_rollout*.jsonl` | Local session records and billing-mode cases |

When adding a case, include the parser assertion that exercises it. Preserve
deliberately incomplete records and SSE boundaries. Use invented identifiers,
paths and metadata; never copy raw account transcripts or authorization headers.
Run the affected parser/collector tests and then the source suite using the
commands in [CONTRIBUTING.md](../../CONTRIBUTING.md).
