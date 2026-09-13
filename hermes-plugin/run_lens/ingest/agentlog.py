"""Per-call history from logs/agent.log (and its rotations, and every profile's log).

Before the plugin's hooks were live, this is the only record of individual LLM calls:
state.db keeps session totals, LiteLLM keeps calls without knowing whose they are.
The agent log has both halves in one line:

    2026-09-12 23:30:28,699 INFO [cron_ed190f76386e_20260912_204110] agent.conversation_loop:
        API call #478: model=big provider=custom in=129615 out=117 total=129732 latency=35.1s

Files rotate at 5 MB (agent.log -> .1 -> .2 -> .3), so reading position is tracked by
inode, not by name. Records can span lines (tool error previews contain newlines);
a line that does not start with a timestamp continues the previous record.

Log timestamps are local time (Python logging default); they are converted with the
host's timezone.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from .. import paths
from ..store import Tx, get_watermark, set_watermark

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) (\w+)(?: \[([^\]]+)\])? ([\w.]+): (.*)$")
API_CALL = re.compile(
    r"API call #(\d+): model=(\S+) provider=(\S+) in=(\d+) out=(\d+) total=(\d+) latency=([\d.]+)s"
    r"(?: cache=(\d+)/(\d+))?")
API_FAIL = re.compile(r"API call failed \(attempt (\d+)/(\d+)\) error_type=(\w+).*?model=(\S+)(?: summary=(.*))?")
TURN_END = re.compile(
    r"Turn ended: reason=(\S+?)(?:\((.*?)\))? model=(\S+) api_calls=(\d+)/(\d+) budget=(\d+)/(\d+) "
    r"tool_turns=(\d+) last_msg_role=(\w+) response_len=(\d+) session=(\S+)")
COMP_START = re.compile(r"context compression started: session=(\S+) messages=(\d+) tokens=~([\d,]+) model=(\S+)")
COMP_DONE = re.compile(r"context compression done: session=(\S+) messages=(\d+)->(\d+) rough_tokens=~([\d,]+)")
CRON = [
    ("cron.suppressed", re.compile(r"Job '(\w{12})': monitor output unchanged — suppressing agent run")),
    ("cron.overlap", re.compile(r"Job '(.+)' already running — skipping")),
    ("cron.silent", re.compile(r"Job '(\w{12})': agent returned \[SILENT\] — skipping delivery")),
    ("cron.start", re.compile(r"Running job '(.+)' \(ID: (\w{12})\)")),
    ("cron.failed", re.compile(r"Job '(.+)' failed: (.*)")),
    ("cron.completed", re.compile(r"Job '(.+)' completed successfully")),
    ("cron.monitor_failed", re.compile(r"Job '(\w{12})': monitor source failed: (.*)")),
]
MATCH_WINDOW_S = 5.0


def _ts(date: str, ms: str) -> float:
    return dt.datetime.strptime(date, "%Y-%m-%d %H:%M:%S").timestamp() + int(ms) / 1000.0


def log_files() -> list[Path]:
    out = []
    for _profile, home in paths.homes():
        d = home / "logs"
        if not d.is_dir():
            continue
        # Oldest rotation first, so the call sequence reads in order.
        for name in ("agent.log.3", "agent.log.2", "agent.log.1", "agent.log"):
            p = d / name
            if p.exists():
                out.append(p)
    return out


def _records(path: Path, offset: int):
    """Yield (end_offset, ts, level, session, logger, message) for complete records."""
    with open(path, "rb") as f:
        f.seek(offset)
        cur = None
        pos = offset
        for raw in f:
            pos += len(raw)
            if not raw.endswith(b"\n"):
                break  # a record still being written; pick it up next time
            line = raw.decode("utf-8", "replace").rstrip("\n")
            m = LINE.match(line)
            if m:
                if cur is not None:
                    yield cur
                cur = [pos, _ts(m.group(1), m.group(2)), m.group(3), m.group(4), m.group(5), m.group(6)]
            elif cur is not None:
                cur[5] += "\n" + line
                cur[0] = pos
        if cur is not None:
            yield cur


def ingest(conn: sqlite3.Connection, full: bool = False) -> dict:
    stats = {"calls": 0, "turns": 0, "events": 0, "files": 0}
    job_by_name = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM jobs")}
    for path in log_files():
        try:
            st = path.stat()
        except OSError:
            continue
        key = f"log:{st.st_dev}:{st.st_ino}"
        offset = 0 if full else int(get_watermark(conn, key, 0) or 0)
        if offset > st.st_size:
            offset = 0
        if offset == st.st_size:
            continue
        stats["files"] += 1
        batch: list = []
        end = offset
        for rec in _records(path, offset):
            batch.append(rec)
            end = rec[0]
            if len(batch) >= 2000:
                _apply(conn, batch, stats, job_by_name)
                with Tx(conn):
                    set_watermark(conn, key, end)
                batch = []
        if batch:
            _apply(conn, batch, stats, job_by_name)
        with Tx(conn):
            set_watermark(conn, key, end)
    return stats


def _apply(conn, batch, stats, job_by_name) -> None:
    with Tx(conn):
        for _end, ts, level, session, logger, msg in batch:
            if logger == "agent.conversation_loop" and session:
                m = API_CALL.match(msg)
                if m:
                    _call(conn, ts, session, m)
                    stats["calls"] += 1
                    continue
                m = API_FAIL.match(msg)
                if m:
                    _event(conn, ts, "api.error", session, None,
                           {"attempt": int(m.group(1)), "max": int(m.group(2)), "error_type": m.group(3),
                            "model": m.group(4), "summary": (m.group(5) or "")[:300]})
                    stats["events"] += 1
                    continue
                m = TURN_END.match(msg)
                if m:
                    _turn(conn, ts, m)
                    stats["turns"] += 1
                    continue
            if logger == "agent.conversation_compression":
                m = COMP_START.match(msg)
                if m:
                    _event(conn, ts, "compression.start", m.group(1), None,
                           {"messages": int(m.group(2)), "tokens": int(m.group(3).replace(",", "")),
                            "model": m.group(4)})
                    stats["events"] += 1
                    continue
                m = COMP_DONE.match(msg)
                if m:
                    _event(conn, ts, "compression.done", m.group(1), None,
                           {"messages_before": int(m.group(2)), "messages_after": int(m.group(3)),
                            "tokens": int(m.group(4).replace(",", ""))})
                    stats["events"] += 1
                    continue
            if logger == "cron.scheduler":
                for kind, rx in CRON:
                    m = rx.match(msg)
                    if not m:
                        continue
                    first = m.group(1)
                    if kind == "cron.start":
                        job = m.group(2)
                    elif re.fullmatch(r"[0-9a-f]{12}", first):
                        job = first
                    else:
                        job = job_by_name.get(first)
                    detail = {"job": first}
                    if kind in ("cron.failed", "cron.monitor_failed"):
                        detail["error"] = (m.group(2) or "")[:300]
                    _event(conn, ts, kind, None, job, detail)
                    stats["events"] += 1
                    break


def _event(conn, ts, kind, session, job, detail) -> None:
    dedupe = hashlib.sha1(f"{ts:.3f}|{kind}|{session}|{job}|{json.dumps(detail, sort_keys=True)}".encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO events(at, kind, session_id, job_id, detail, origin, dedupe) "
                 "VALUES(?,?,?,?,?,?,?)", (ts, kind, session, job, json.dumps(detail), "log", dedupe))


def _call(conn, ts, session, m) -> None:
    seq, model, provider = int(m.group(1)), m.group(2), m.group(3)
    tin, tout, latency = int(m.group(4)), int(m.group(5)), float(m.group(7))
    cache_read = int(m.group(8)) if m.group(8) else None
    row = conn.execute(
        "SELECT id, origin FROM calls WHERE session_id=? AND input_tokens=? AND output_tokens=? "
        "AND ABS(ended_at - ?) < ? LIMIT 1", (session, tin, tout, ts, MATCH_WINDOW_S)).fetchone()
    if row:
        conn.execute("UPDATE calls SET seq=COALESCE(seq, ?), latency_s=COALESCE(latency_s, ?), "
                     "cache_read_tokens=COALESCE(cache_read_tokens, ?) WHERE id=?",
                     (seq, latency, cache_read, row["id"]))
        return
    conn.execute(
        "INSERT INTO calls(session_id, seq, model, provider, started_at, ended_at, latency_s, input_tokens, "
        "output_tokens, cache_read_tokens, status, origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (session, seq, model, provider, ts - latency, ts, latency, tin, tout, cache_read, "ok", "log"))


def _turn(conn, ts, m) -> None:
    reason = m.group(1) + (f"({m.group(2)})" if m.group(2) else "")
    session = m.group(11)
    row = conn.execute("SELECT id FROM turns WHERE session_id=? AND ABS(ended_at - ?) < ? LIMIT 1",
                       (session, ts, MATCH_WINDOW_S)).fetchone()
    if row:
        conn.execute("UPDATE turns SET exit_reason=COALESCE(exit_reason, ?), api_calls=COALESCE(api_calls, ?), "
                     "max_iterations=COALESCE(max_iterations, ?), tool_turns=COALESCE(tool_turns, ?) WHERE id=?",
                     (reason, int(m.group(4)), int(m.group(5)), int(m.group(8)), row["id"]))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO turns(id, session_id, ended_at, exit_reason, api_calls, max_iterations, "
            "tool_turns, model, origin) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"{session}:log:{ts:.3f}", session, ts, reason, int(m.group(4)), int(m.group(5)),
             int(m.group(8)), m.group(3), "log"))
    conn.execute("UPDATE sessions SET last_exit_reason=? WHERE id=? AND "
                 "(last_activity_at IS NULL OR last_activity_at <= ? + 5)", (reason, session, ts))
