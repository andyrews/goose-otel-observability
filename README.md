# Goose E2E Run Reports

Renders Goose's built-in OpenTelemetry traces as a human-readable
"E2E AGENT RUN" report, per task/turn — model requests, tool calls with
duration, tool results, and a pass/fail summary.

No third-party dependencies. Python 3.10+ stdlib only.

```
goose-observability/
├── otel_receiver.py   # local OTLP/HTTP(JSON) endpoint Goose exports traces to
├── storage.py          # SQLite storage for captured spans
├── report.py           # renders a trace as the ASCII report
└── goose_traces.db     # created automatically on first run
```

## 0. Install one extra package (for protobuf decoding)

Goose's OTLP exporter sends **protobuf**, not JSON, by default — there's
no documented env var to force JSON, so the receiver decodes protobuf
directly. This needs one extra package (still no external services):

```bash
pip install opentelemetry-proto protobuf
```

Best to put this on a venv. If you skip this, the receiver still runs and accepts connections, but
logs a warning and drops any protobuf-encoded traces it receives instead
of crashing.

## 1. Start the receiver

```bash
python3 otel_receiver.py
```

This listens on `http://localhost:4318` and writes every span it receives
into `goose_traces.db`. Leave it running in a terminal (or a tmux pane)
alongside Goose.

## 2. Point Goose at it — as real environment variables, not config.yaml

**Important:** put these in your actual shell environment, not in
`config.yaml`. The official Goose telemetry docs only ever show these as
`export ...` shell commands — never as config-file keys — and in testing,
OTEL_* keys placed in `config.yaml` were silently ignored while the same
names set as real env vars worked immediately.

PowerShell:
```powershell
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318"
$env:OTEL_TRACES_EXPORTER = "otlp"
$env:OTEL_METRICS_EXPORTER = "none"
$env:OTEL_LOGS_EXPORTER = "none"

# Capture actual prompt/tool content in the traces (off by default)
$env:OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT = "true"

# Reduce batching delay so a report is available soon after each turn,
# instead of only after the session exits
$env:OTEL_BSP_SCHEDULE_DELAY = "500"

goose session start
```

bash/zsh: same lines with `export VAR=value` instead of `$env:VAR = "value"`.

Ask a question in Goose as normal. You should see the receiver print a
line like:

```
[receiver] +5 span(s) across 1 trace(s) -> dispatch_tool_call, reply, reply_stream, stream_response_from_provider
```

**If nothing shows up right away:** OTLP export is batched, so spans may
not be sent until the batch interval fires or the session exits — this is
different from the `console` exporter (below), which writes immediately.
`OTEL_BSP_SCHEDULE_DELAY=500` above shortens that wait to ~0.5s; if you
still see nothing, try exiting the Goose session (or wait a few seconds)
before checking the receiver log.

## Debugging telemetry itself (bypass the receiver)

If you're not sure whether Goose is emitting telemetry at all, skip the
receiver and dump straight to your terminal:

```powershell
Remove-Item Env:OTEL_EXPORTER_OTLP_ENDPOINT -ErrorAction SilentlyContinue
$env:OTEL_TRACES_EXPORTER = "console"
$env:OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT = "true"
goose session start
```

This prints full span text per event as it happens — useful for
confirming Goose's telemetry is alive, and for checking real attribute
names if a future Goose version changes them (see the note at the top of
`report.py`).

## 3. Render the report

```bash
# Most recently captured trace
python3 report.py

# A specific trace
python3 report.py --trace <trace_id>

# Continuously print a report each time a trace goes quiet for 3s
# (handy for "ask a question, see the report pop out" workflow)
python3 report.py --watch
```

## Schema this was tuned against

Confirmed against a real Goose **1.49.0** console-exporter dump. Per turn,
the span hierarchy is:

```
reply                                  (root; no parent — user_message,
  └─ reply_stream                       trace_output, session.id live here)
       ├─ stream_response_from_provider (model call — gen_ai.request.model,
       │                                  gen_ai.usage.*_tokens, etc.)
       ├─ dispatch_tool_call            (tool call — gen_ai.tool.name,
       │                                  gen_ai.tool.call.arguments/.result)
       └─ stream_response_from_provider (final model call)
```

`report.py` renders `stream_response_from_provider` as MODEL REQUEST/RESPONSE
steps and `dispatch_tool_call` as TOOL EXECUTION steps; `reply`/`reply_stream`
are treated as containers, not steps (their content feeds the report header).

This can still drift between versions/builds, so the mapping stays
adjustable rather than hardcoded:

- Spans are classified MODEL / TOOL / structural / other by **keyword
  matching** on the span name (`KEYWORDS` / `STRUCTURAL_NAMES` at the top
  of `report.py`), not by requiring an exact name.
- Field values (task text, tool args/results, etc.) are looked up from a
  **list of candidate attribute keys** (`ATTR_CANDIDATES`), tried in order.
- Anything unrecognized still shows up as a generic `EVENT: <name>` block
  with all captured attributes dumped, so nothing is silently lost.

If a future Goose version changes things and the report looks off, run:

```bash
python3 report.py --raw --trace <trace_id>
```

to see the actual captured span names/attributes, and adjust `KEYWORDS` /
`STRUCTURAL_NAMES` / `ATTR_CANDIDATES` accordingly.

**Content capture:** prompt/tool content only appears if
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` is set (it's off
by default). Without it, `Task`/`Output` will show as "not captured".

## Notes

- `PASS`/`FAIL` per step is derived from the OTel span status code
  (`ERROR` → FAIL); there's no way for this tool to know your *expected*
  answer, so unlike the original mockup there's no "Expected/Actual/task
  success" line — you'd need to add that yourself if you want automated
  grading against known answers.
- The receiver also accepts (and ignores) `/v1/metrics` and `/v1/logs` so
  Goose won't error if you leave those exporters on.