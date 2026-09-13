"""
Tiny SQLite-backed store for OTLP spans captured from Goose.

Kept dependency-free (stdlib only) so it runs anywhere Python 3 runs.
"""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "goose_traces.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    name TEXT,
    kind TEXT,
    start_ns INTEGER,
    end_ns INTEGER,
    status_code INTEGER,
    status_message TEXT,
    attributes_json TEXT,
    events_json TEXT,
    resource_json TEXT,
    scope_name TEXT,
    received_at REAL,
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_received ON spans(received_at);
"""


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    return conn


def insert_spans(conn: sqlite3.Connection, spans: list[dict]) -> None:
    now = time.time()
    rows = []
    for s in spans:
        rows.append((
            s["trace_id"], s["span_id"], s.get("parent_span_id"),
            s.get("name"), s.get("kind"), s.get("start_ns"), s.get("end_ns"),
            s.get("status_code"), s.get("status_message"),
            json.dumps(s.get("attributes", {})),
            json.dumps(s.get("events", [])),
            json.dumps(s.get("resource", {})),
            s.get("scope_name"),
            now,
        ))
    conn.executemany(
        """INSERT OR REPLACE INTO spans
           (trace_id, span_id, parent_span_id, name, kind, start_ns, end_ns,
            status_code, status_message, attributes_json, events_json,
            resource_json, scope_name, received_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def recent_trace_ids(conn: sqlite3.Connection, limit: int = 20) -> list[str]:
    cur = conn.execute(
        """SELECT trace_id, MAX(received_at) as last_seen
           FROM spans GROUP BY trace_id ORDER BY last_seen DESC LIMIT ?""",
        (limit,),
    )
    return [r[0] for r in cur.fetchall()]


def spans_for_trace(conn: sqlite3.Connection, trace_id: str) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM spans WHERE trace_id = ? ORDER BY start_ns ASC",
        (trace_id,),
    )
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        d["attributes"] = json.loads(d.pop("attributes_json") or "{}")
        d["events"] = json.loads(d.pop("events_json") or "[]")
        d["resource"] = json.loads(d.pop("resource_json") or "{}")
        out.append(d)
    return out


def last_activity(conn: sqlite3.Connection, trace_id: str) -> float:
    cur = conn.execute(
        "SELECT MAX(received_at) FROM spans WHERE trace_id = ?", (trace_id,)
    )
    r = cur.fetchone()
    return r[0] if r and r[0] else 0.0