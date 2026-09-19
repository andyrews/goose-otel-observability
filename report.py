"""
Render a captured Goose OTel trace as an "E2E AGENT RUN" style report.

Usage:
    python report.py                  # render the most recent trace
    python report.py --trace <id>     # render a specific trace
    python report.py --watch          # print a report each time a trace
                                       # goes quiet (no new spans for 3s)
    python report.py --raw --trace <id>   # dump raw spans (for tuning)

NOTE ON ATTRIBUTE NAMES
------------------------
Confirmed against a real Goose 1.49.0 console-exporter dump (Sept 2026).
Real span hierarchy per turn:

    reply                              (root; no parent)
      |-- reply_stream                 (wrapper; one per agent loop)
           |-- stream_response_from_provider  (one per model call)
           |-- dispatch_tool_call             (one per tool call)
           |-- stream_response_from_provider  (final model call)

`reply` and `reply_stream` are structural containers, not steps we render
directly -- `reply`'s attributes (user_message, trace_output, session.id)
are used for the report header instead. `stream_response_from_provider`
is a MODEL step, `dispatch_tool_call` is a TOOL step.

Since versions/builds can still drift, classification stays keyword-based
(KEYWORDS) rather than hardcoded exact matches, and field lookups try a
list of *candidate* attribute keys (ATTR_CANDIDATES) in priority order.
Anything unrecognized still falls back to a generic "EVENT" block dumping
all attributes, so nothing captured is silently dropped.

Run with --raw against a session to check current key names if the
rendered report ever looks off, and adjust the tables below.
"""
import argparse
import json
import time

import storage

WIDTH = 66

# Exact-name containers we don't render as their own step -- their
# attributes feed the report header / final-answer instead.
STRUCTURAL_NAMES = {"reply", "reply_stream", "session"}

KEYWORDS = {
    "model": ["stream_response_from_provider", "stream_response",
              "provider_chat", "chat", "completion", "llm"],
    "tool": ["dispatch_tool_call", "tool_call", "mcp"],
}

ATTR_CANDIDATES = {
    "task": ["user_message", "trace_input", "input", "task",
             "user.message", "gen_ai.prompt", "message", "prompt"],
    "model_name": ["gen_ai.request.model", "gen_ai.response.model",
                   "model.name", "model", "llm.model_name"],
    "provider": ["gen_ai.provider.name", "provider.name", "gen_ai.system", "provider"],
    "session_id": ["session.id", "session_id"],
    "tool_name": ["gen_ai.tool.name", "tool.name", "name", "function.name"],
    "tool_args": ["gen_ai.tool.call.arguments", "tool.arguments",
                  "gen_ai.tool.arguments", "arguments", "input"],
    "tool_result": ["gen_ai.tool.call.result", "tool.result",
                    "gen_ai.tool.result", "result", "output"],
    "input_tokens": ["gen_ai.usage.input_tokens", "tokens.input",
                      "gen_ai.usage.prompt_tokens"],
    "output_tokens": ["gen_ai.usage.output_tokens", "tokens.output",
                       "gen_ai.usage.completion_tokens"],
    "extension_name": ["extension.name", "mcp.server.name"],
    "final_answer": ["trace_output", "gen_ai.completion", "response",
                      "output", "answer"],
}


def first_attr(attrs: dict, candidates: list[str]):
    for key in candidates:
        if key in attrs and attrs[key] not in (None, ""):
            return attrs[key]
    # case-insensitive exact-match fallback only -- NOT substring matching.
    # (Substring matching previously let a candidate like "output" match
    # inside "gen_ai.usage.output_tokens" and return the wrong value.)
    lower = {k.lower(): v for k, v in attrs.items()}
    for key in candidates:
        v = lower.get(key.lower())
        if v not in (None, ""):
            return v
    return None


def classify(name: str) -> str:
    if name in STRUCTURAL_NAMES:
        return "structural"
    n = (name or "").lower()
    for kind, words in KEYWORDS.items():
        if any(w in n for w in words):
            return kind
    return "other"


def dur_s(span: dict) -> float:
    return max(0.0, (span.get("end_ns", 0) - span.get("start_ns", 0)) / 1e9)


def status_mark(span: dict) -> str:
    return "✗ FAIL" if span.get("status_code") == 2 else "✓ PASS"


def tool_result_failed(result) -> bool:
    """Detect an application-level tool failure that OTel's span status
    won't catch on its own -- Goose successfully *dispatched* the tool
    (span status stays Unset) even when the tool's own result payload
    represents a failure: a non-zero shell exit code, isError:true either
    at the top level or nested under "value" (seen when Goose itself
    rejects/wraps a tool call), etc.
    """
    if result is None:
        return False
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return False  # not JSON -- nothing structured to check
    if not isinstance(result, dict):
        return False
    if result.get("isError") is True:
        return True
    if result.get("status") == "error":
        return True
    value = result.get("value")
    if isinstance(value, dict):
        if value.get("isError") is True:
            return True
        exit_code = value.get("structuredContent", {}).get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return True
    return False


def notable_events(span: dict) -> list[dict]:
    """WARN/ERROR-level span events -- e.g. Rust `tracing::warn!` calls
    bridged into OTel as span events, such as a JSON-RPC "-32600: Tool
    'X' was not advertised for this model turn" rejection. These aren't
    spans of their own, so without this they're captured in storage but
    never shown in the rendered report. DEBUG-level events (config
    loading, etc.) are deliberately excluded to avoid drowning the report.
    """
    out = []
    for e in span.get("events", []):
        level = (e.get("attributes", {}) or {}).get("level")
        if level in ("WARN", "ERROR"):
            out.append(e)
    return out


def divider(char="─"):
    return char * WIDTH


def box(char="═"):
    return char * WIDTH


def kv_block(label: str, value) -> str:
    if isinstance(value, (dict, list)):
        value_str = json.dumps(value, indent=2)
        value_str = "\n".join("  " + line for line in value_str.splitlines())
        return f"{label}:\n{value_str}"
    return f"{label}:\n  {value}"


def render_report(spans: list[dict]) -> str:
    if not spans:
        return "(no spans in this trace)"

    span_ids = {s["span_id"] for s in spans}
    roots = [s for s in spans if not s.get("parent_span_id")
             or s["parent_span_id"] not in span_ids]
    root = roots[0] if roots else spans[0]

    all_attrs = {}
    for s in spans:
        all_attrs.update(s.get("attributes", {}))
        all_attrs.update(s.get("resource", {}))

    trace_id = root["trace_id"]
    session_id = first_attr(all_attrs, ATTR_CANDIDATES["session_id"])
    task = first_attr(all_attrs, ATTR_CANDIDATES["task"]) or \
        "<content not captured -- run with --raw to inspect available attributes>"
    model_name = first_attr(all_attrs, ATTR_CANDIDATES["model_name"]) or "unknown"
    provider = first_attr(all_attrs, ATTR_CANDIDATES["provider"]) or "unknown"
    agent_name = all_attrs.get("service.name", "Goose")

    # trace_output/final answer text lives on the root span, not on the
    # individual model-call span -- attach it to whichever model/tool call
    # actually finishes last (by end time), so it shows on the true final step.
    call_kinds = ("model", "tool")
    call_end_times = [s.get("end_ns", 0) for s in spans if classify(s["name"]) in call_kinds]
    last_call_end_ns = max(call_end_times) if call_end_times else None
    root_final_answer = first_attr(root.get("attributes", {}), ATTR_CANDIDATES["final_answer"])

    # Build a single chronological timeline mixing spans (by start time) and
    # any WARN/ERROR span events (by event time) -- including events on
    # structural spans (reply/reply_stream), since that's where a rejected
    # tool-call warning is most likely to be attached even though the
    # containing span itself isn't rendered as its own step.
    timeline = []
    for s in spans:
        timeline.append((s.get("start_ns", 0), "span", s, None))
        for ev in notable_events(s):
            timeline.append((ev.get("time_ns", s.get("start_ns", 0)), "event", ev, s["name"]))
    timeline.sort(key=lambda item: item[0])

    lines = []
    lines.append(box())
    lines.append(" E2E AGENT RUN")
    lines.append(box())
    lines.append("")
    lines.append(kv_block("Task", task))
    lines.append("")
    lines.append(kv_block("Model", model_name))
    lines.append("")
    lines.append(kv_block("Provider", provider))
    lines.append("")
    lines.append(kv_block("Agent", agent_name))
    lines.append("")
    lines.append(kv_block("Run ID", session_id or trace_id))
    lines.append("")

    step_no = 1
    model_calls = 0
    tool_calls = 0
    failed_tools = 0
    warnings = 0
    any_error = False

    def step_header(title):
        nonlocal step_no
        lines.append(f"[{step_no:02d}] {title}")
        lines.append(divider())
        step_no += 1

    # Step 1: user input, synthesized from the task text found above.
    step_header("USER INPUT")
    lines.append(status_mark(root))
    lines.append("")
    lines.append(kv_block("Input", task))
    lines.append("")

    for _t, item_type, item, span_name in timeline:

        if item_type == "event":
            warnings += 1
            any_error = True
            step_header("⚠ WARNING")
            lines.append("✗ FAIL")
            lines.append("")
            lines.append(kv_block("From span", span_name))
            lines.append("")
            lines.append(kv_block("Message", item.get("name", "")))
            attrs = item.get("attributes", {})
            if attrs:
                lines.append("")
                lines.append(kv_block("Attributes", attrs))
            lines.append("")
            continue

        s = item
        kind = classify(s["name"])
        if kind == "structural":
            continue  # reply / reply_stream / session are containers, not steps
        attrs = s.get("attributes", {})

        if kind == "model":
            model_calls += 1
            step_header("MODEL REQUEST")
            lines.append(status_mark(s))
            lines.append("")
            lines.append(kv_block("Provider", first_attr(attrs, ATTR_CANDIDATES["provider"]) or provider))
            lines.append("")
            lines.append(kv_block("Model", first_attr(attrs, ATTR_CANDIDATES["model_name"]) or model_name))
            ctx = first_attr(attrs, ATTR_CANDIDATES["input_tokens"])
            if ctx is not None:
                lines.append("")
                lines.append(kv_block("Context tokens", ctx))
            lines.append("")

            step_header("MODEL RESPONSE")
            lines.append(status_mark(s))
            lines.append("")
            lines.append(kv_block("Span", s["name"]))
            out_tokens = first_attr(attrs, ATTR_CANDIDATES["output_tokens"])
            if out_tokens is not None:
                lines.append("")
                lines.append(kv_block("Output tokens", out_tokens))
            answer = first_attr(attrs, ATTR_CANDIDATES["final_answer"])
            if answer is None and last_call_end_ns is not None and s.get("end_ns") == last_call_end_ns:
                answer = root_final_answer  # this is the final turn's model call
            if answer is not None:
                lines.append("")
                lines.append(kv_block("Output", answer))
            lines.append("")
            lines.append(kv_block("Duration", f"{dur_s(s):.2f}s"))
            lines.append("")
            if s.get("status_code") == 2:
                any_error = True

        elif kind == "tool":
            tool_calls += 1
            tool_name = first_attr(attrs, ATTR_CANDIDATES["tool_name"]) or s["name"]
            result = first_attr(attrs, ATTR_CANDIDATES["tool_result"])
            # OTel span status alone misses this: Goose successfully
            # dispatches a tool whose *own result* represents a failure
            # (non-zero exit code, isError:true), so span status_code stays
            # Unset/0. Check the actual payload too.
            tool_failed = s.get("status_code") == 2 or tool_result_failed(result)

            step_header("TOOL EXECUTION")
            lines.append("✗ FAIL" if tool_failed else "✓ PASS")
            lines.append("")
            lines.append(kv_block("Tool", tool_name))
            args = first_attr(attrs, ATTR_CANDIDATES["tool_args"])
            if args is not None:
                lines.append("")
                lines.append(kv_block("Arguments", args))
            if result is not None:
                lines.append("")
                lines.append(kv_block("Result", result))
            lines.append("")
            lines.append(kv_block("Duration", f"{dur_s(s):.2f}s"))
            lines.append("")
            if tool_failed:
                failed_tools += 1
                any_error = True

            step_header("TOOL RESULT → MODEL")
            lines.append(status_mark(s))
            lines.append("")
            lines.append("Result successfully added to conversation.")
            lines.append("")

        else:
            step_header(f"EVENT: {s['name']}")
            lines.append(status_mark(s))
            if attrs:
                lines.append("")
                lines.append(kv_block("Attributes", attrs))
            lines.append("")
            lines.append(kv_block("Duration", f"{dur_s(s):.2f}s"))
            lines.append("")
            if s.get("status_code") == 2:
                any_error = True

    total_start = min(s.get("start_ns", 0) for s in spans)
    total_end = max(s.get("end_ns", 0) for s in spans)
    total_dur = max(0.0, (total_end - total_start) / 1e9)

    lines.append(box())
    lines.append(f" E2E RESULT: {'FAIL' if any_error else 'PASS'}")
    lines.append(box())
    lines.append("")
    lines.append(f"Total duration:       {total_dur:.2f}s")
    lines.append(f"Model calls:          {model_calls}")
    lines.append(f"Tool calls:           {tool_calls}")
    lines.append(f"Failed tool calls:    {failed_tools}")
    lines.append(f"Warnings:             {warnings}")

    return "\n".join(lines)


def cmd_raw(trace_id: str | None):
    conn = storage.get_conn()
    if not trace_id:
        ids = storage.recent_trace_ids(conn, limit=1)
        if not ids:
            print("No traces captured yet.")
            return
        trace_id = ids[0]
    spans = storage.spans_for_trace(conn, trace_id)
    print(json.dumps(spans, indent=2))


def cmd_report(trace_id: str | None):
    conn = storage.get_conn()
    if not trace_id:
        ids = storage.recent_trace_ids(conn, limit=1)
        if not ids:
            print("No traces captured yet. Run a Goose task first, "
                  "with the receiver running and OTEL env vars set.")
            return
        trace_id = ids[0]
    spans = storage.spans_for_trace(conn, trace_id)
    print(render_report(spans))


def cmd_watch(quiet_seconds: float = 3.0, poll: float = 1.0):
    conn = storage.get_conn()
    reported = set()
    pending_since = {}
    print("[report] watching for completed traces (Ctrl+C to stop)...")
    while True:
        ids = storage.recent_trace_ids(conn, limit=50)
        now = time.time()
        for tid in ids:
            if tid in reported:
                continue
            last = storage.last_activity(conn, tid)
            if tid not in pending_since:
                pending_since[tid] = last
            if now - last >= quiet_seconds:
                spans = storage.spans_for_trace(conn, tid)
                print()
                print(render_report(spans))
                print()
                reported.add(tid)
        time.sleep(poll)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trace", help="Trace ID to render (default: most recent)")
    ap.add_argument("--watch", action="store_true",
                     help="Continuously print a report per completed trace")
    ap.add_argument("--raw", action="store_true",
                     help="Dump raw captured spans as JSON instead of a report")
    args = ap.parse_args()

    if args.watch:
        cmd_watch()
    elif args.raw:
        cmd_raw(args.trace)
    else:
        cmd_report(args.trace)


if __name__ == "__main__":
    main()