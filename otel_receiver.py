"""
Minimal OTLP/HTTP receiver -- accepts both JSON and protobuf bodies.

Goose's OTel exporter defaults to OTLP/HTTP **protobuf** (the OTel spec
default when OTEL_EXPORTER_OTLP_PROTOCOL isn't set, and confirmed against
a real Goose build that ignored the JSON override). This server auto-detects
the wire format from the Content-Type header, so it works either way:

    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    export OTEL_TRACES_EXPORTER=otlp
    # OTEL_EXPORTER_OTLP_PROTOCOL is optional -- protobuf works without it

Listens on :4318, accepts POST /v1/traces (and no-ops /v1/metrics and
/v1/logs so Goose doesn't error if those are also enabled), flattens spans
into simple dicts, and stores them in SQLite via storage.py.

Protobuf decoding needs one extra package (JSON decoding needs nothing):
    pip install opentelemetry-proto protobuf
If that package isn't installed, protobuf bodies are skipped with a
warning rather than crashing the receiver.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import storage

PORT = 4318

try:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    HAVE_PROTOBUF = True
except ImportError:
    ExportTraceServiceRequest = None
    HAVE_PROTOBUF = False


def _attr_value(v: dict):
    """OTLP attribute values are tagged unions, e.g. {"stringValue": "x"}."""
    if v is None:
        return None
    for key in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if key in v:
            val = v[key]
            # intValue often comes back as a string in JSON encoding
            if key == "intValue":
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return val
            return val
    if "arrayValue" in v:
        return [_attr_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return _attrs_list_to_dict(v["kvlistValue"].get("values", []))
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def _attrs_list_to_dict(attrs: list) -> dict:
    out = {}
    for a in attrs or []:
        out[a.get("key")] = _attr_value(a.get("value"))
    return out


def parse_export_trace_request(payload: dict) -> list[dict]:
    """Flatten an OTLP ExportTraceServiceRequest (JSON) into span dicts."""
    spans = []
    for rs in payload.get("resourceSpans", []):
        resource_attrs = _attrs_list_to_dict(
            rs.get("resource", {}).get("attributes", [])
        )
        for ss in rs.get("scopeSpans", []):
            scope_name = ss.get("scope", {}).get("name")
            for sp in ss.get("spans", []):
                status = sp.get("status", {})
                events = []
                for ev in sp.get("events", []):
                    events.append({
                        "name": ev.get("name"),
                        "time_ns": int(ev.get("timeUnixNano", 0)),
                        "attributes": _attrs_list_to_dict(ev.get("attributes", [])),
                    })
                spans.append({
                    "trace_id": sp.get("traceId"),
                    "span_id": sp.get("spanId"),
                    "parent_span_id": sp.get("parentSpanId") or None,
                    "name": sp.get("name"),
                    "kind": sp.get("kind"),
                    "start_ns": int(sp.get("startTimeUnixNano", 0)),
                    "end_ns": int(sp.get("endTimeUnixNano", 0)),
                    "status_code": status.get("code", 0),
                    "status_message": status.get("message"),
                    "attributes": _attrs_list_to_dict(sp.get("attributes", [])),
                    "events": events,
                    "resource": resource_attrs,
                    "scope_name": scope_name,
                })
    return spans


def _pb_attr_value(v):
    which = v.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [_pb_attr_value(x) for x in v.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _pb_attr_value(kv.value) for kv in v.kvlist_value.values}
    return getattr(v, which)


def _pb_attrs_to_dict(attrs) -> dict:
    return {a.key: _pb_attr_value(a.value) for a in attrs}


def parse_export_trace_request_pb(req) -> list[dict]:
    """Flatten a protobuf ExportTraceServiceRequest into span dicts."""
    spans = []
    for rs in req.resource_spans:
        resource_attrs = _pb_attrs_to_dict(rs.resource.attributes)
        for ss in rs.scope_spans:
            scope_name = ss.scope.name
            for sp in ss.spans:
                events = []
                for ev in sp.events:
                    events.append({
                        "name": ev.name,
                        "time_ns": ev.time_unix_nano,
                        "attributes": _pb_attrs_to_dict(ev.attributes),
                    })
                spans.append({
                    "trace_id": sp.trace_id.hex(),
                    "span_id": sp.span_id.hex(),
                    "parent_span_id": sp.parent_span_id.hex() or None,
                    "name": sp.name,
                    "kind": sp.kind,
                    "start_ns": sp.start_time_unix_nano,
                    "end_ns": sp.end_time_unix_nano,
                    "status_code": sp.status.code,
                    "status_message": sp.status.message,
                    "attributes": _pb_attrs_to_dict(sp.attributes),
                    "events": events,
                    "resource": resource_attrs,
                    "scope_name": scope_name,
                })
    return spans


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quieter default logging; we print our own summary lines instead.
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _respond_ok(self):
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self._read_body()
        if self.path.startswith("/v1/traces"):
            content_type = self.headers.get("Content-Type", "")
            spans = []
            try:
                if "json" in content_type:
                    payload = json.loads(raw.decode("utf-8"))
                    spans = parse_export_trace_request(payload)
                else:
                    if not HAVE_PROTOBUF:
                        print(
                            "[receiver] got a non-JSON /v1/traces POST "
                            f"(Content-Type: {content_type!r}) but "
                            "opentelemetry-proto isn't installed -- run: "
                            "pip install opentelemetry-proto protobuf",
                            file=sys.stderr,
                        )
                        self._respond_ok()
                        return
                    req = ExportTraceServiceRequest()
                    req.ParseFromString(raw)
                    spans = parse_export_trace_request_pb(req)
            except Exception as e:  # noqa: BLE001 - best effort ingestion
                print(f"[receiver] failed to parse traces payload "
                      f"(Content-Type: {content_type!r}): {e}", file=sys.stderr)
                self._respond_ok()
                return
            if spans:
                conn = storage.get_conn()
                storage.insert_spans(conn, spans)
                conn.close()
                trace_ids = sorted({s["trace_id"] for s in spans})
                names = ", ".join(sorted({s["name"] for s in spans}))[:120]
                print(f"[receiver] +{len(spans)} span(s) "
                      f"across {len(trace_ids)} trace(s) -> {names}")
            self._respond_ok()
        elif self.path.startswith("/v1/metrics") or self.path.startswith("/v1/logs"):
            # Accepted but ignored -- this tool only renders trace reports.
            self._respond_ok()
        else:
            self.send_response(404)
            self.end_headers()


def main():
    print(f"[receiver] listening on http://localhost:{PORT}")
    print("[receiver] point Goose at it with:")
    print(f"    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:{PORT}")
    print("    export OTEL_EXPORTER_OTLP_PROTOCOL=http/json")
    print("    export OTEL_TRACES_EXPORTER=otlp")
    print(f"[receiver] writing to {storage.DB_PATH}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()