# Synthetic protocol fixtures

These files model provider response formats for parser tests. They are not
account exports. Identifiers, paths, timings and session metadata are synthetic
placeholders; fixed token counts exercise cache subtraction, deduplication,
reasoning-token handling and streaming behavior.

The Codex fixture deliberately includes a truncated JSON line to exercise
recovery from an incomplete write. SSE comments and trailing markers are also
intentional protocol cases.
