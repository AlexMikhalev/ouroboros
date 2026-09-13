# Recorded provider wire

Real SSE streams and non-stream JSON bodies, recorded from live provider routes on
2026-09-13 with synthetic prompts (an etymology question that must call one `lookup`
tool once or twice). They are the offline evidence behind `tests/test_llm_wire_corpus.py`:
the stream assembler (`ouroboros/llm_stream.py`) must consume every `.sse` here without an
exception and produce the same normalized shape as the `.json` sibling of the same request.

Layout: `<route>/<model>/<case>_stream.sse` and, where the same request was also sent
non-streaming, `<case>_nonstream.json`. `route` is the transport family (`openrouter`,
`openai` Chat Completions, `anthropic` native Messages), never a vendor-specific rule.

How they were recorded: a plain `curl -N`-shaped POST (`urllib.request`, no SDK) of the
Chat Completions / Messages request with `"stream": true` (plus
`"stream_options": {"include_usage": true}` on the OpenAI-compatible routes), the raw
response bytes written to disk unchanged. The sprint's throwaway `capture_wire.py` and
`replay_corpus.py` did exactly that; the production runtime also retains every stream's
raw wire in the private CAS `physical_stream` manifest (`_StreamAssembly.retain`), which is
the source for future exports.

Redaction rule (applied before commit): every string value under the keys `data` and
`signature` (opaque encrypted reasoning / thought signatures) keeps its first 24
characters followed by `…`. Ids (`call_…`, `rs_…`, `gen-…`, `toolu_…`), types, indices,
text, tool arguments and usage are intact. SSE framing is byte-exact (`: OPENROUTER
PROCESSING` comment lines, blank lines, `[DONE]`, `event:` lines, the provider's own JSON
spacing on frames the redaction did not touch); a redacted `data:` line is re-serialized
compactly. Non-stream bodies keep their leading keep-alive whitespace.

Fixture pairs are separate live requests, so parity is structural (message keys, tool
names, argument key sets, `reasoning_details` type sequence, finish reason, usage keys),
never exact text.
