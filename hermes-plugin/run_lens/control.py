"""Stopping one run without restarting the gateway.

On 2026-09-12 a looping cron run could only be ended by restarting the whole gateway
— which would also have cut a live TUI session. Hermes has a clean per-agent stop
(`AIAgent.interrupt(hard_cancel=True)`, what `/stop` uses) but nothing that reaches a
cron run from outside the process. So:

1. `hermes lens stop <run>` (or the breaker) writes a row to `stops`.
2. Every process with the plugin polls that table every few seconds.
3. When it sees one of its own sessions: it finds the AIAgent in memory and calls
   `interrupt(hard_cancel=True)`; and until the run is gone, `pre_tool_call` blocks
   every tool with a message telling the model to end its turn. Either alone ends a
   loop; together they end it within one call.

Why not raise from middleware: an `llm_execution` middleware that raises before
calling `next_call` is logged and skipped — the call goes through anyway.
"""
from __future__ import annotations

import gc
import json
import logging
import sqlite3
import threading
import time

logger = logging.getLogger("run_lens")

STOP_MESSAGE = ("run-lens: this run was stopped by the operator ({reason}). Do not call any more tools. "
                "End your turn now with one short line saying you were stopped.")
POLL_S = 4.0
STOP_TTL_S = 6 * 3600

_lock = threading.Lock()
_stops: dict[str, str] = {}
_applied: set[str] = set()
_last_poll = 0.0


def request_stop(conn: sqlite3.Connection, session_ids: list[str], by: str, reason: str) -> int:
    now = time.time()
    n = 0
    for sid in session_ids:
        conn.execute("INSERT INTO stops(session_id, requested_at, requested_by, reason) VALUES(?,?,?,?) "
                     "ON CONFLICT(session_id) DO UPDATE SET requested_at=excluded.requested_at, "
                     "requested_by=excluded.requested_by, reason=excluded.reason, applied_at=NULL, applied_by=NULL",
                     (sid, now, by, reason))
        n += 1
    conn.execute("INSERT OR IGNORE INTO events(at, kind, session_id, detail, origin, dedupe) VALUES(?,?,?,?,?,?)",
                 (now, "stop.requested", session_ids[0] if session_ids else None,
                  json.dumps({"by": by, "reason": reason, "sessions": session_ids}), "control", f"stop:{now:.3f}"))
    return n


def refresh(conn: sqlite3.Connection) -> None:
    """Called from the recorder's writer thread."""
    global _last_poll
    now = time.time()
    if now - _last_poll < POLL_S:
        return
    _last_poll = now
    rows = conn.execute("SELECT session_id, reason FROM stops WHERE requested_at >= ?",
                        (now - STOP_TTL_S,)).fetchall()
    with _lock:
        _stops.clear()
        _stops.update({r["session_id"]: r["reason"] or "no reason given" for r in rows})


def is_stopped(session_id: str | None) -> str | None:
    if not session_id:
        return None
    with _lock:
        return _stops.get(session_id)


def local_stop(session_id: str, reason: str) -> None:
    """Make a stop effective in this process immediately (the breaker's path)."""
    with _lock:
        _stops[session_id] = reason


def apply(session_id: str) -> bool:
    """Interrupt the in-memory agent that owns this session, once."""
    reason = is_stopped(session_id)
    if not reason:
        return False
    with _lock:
        if session_id in _applied:
            return True
        _applied.add(session_id)
    hit = False
    try:
        for obj in gc.get_objects():
            if type(obj).__name__ != "AIAgent":
                continue
            if getattr(obj, "session_id", None) != session_id:
                continue
            try:
                obj.interrupt(f"run-lens: stopped ({reason})", hard_cancel=True)
            except TypeError:
                obj.interrupt(f"run-lens: stopped ({reason})")
            hit = True
    except Exception as exc:
        logger.warning("run-lens: interrupt of %s failed: %s", session_id, exc)
    logger.warning("run-lens: stop applied to %s (%s) — agent %s", session_id, reason,
                   "interrupted" if hit else "not found in this process; blocking its tools")
    return hit


def block_message(session_id: str | None) -> str | None:
    reason = is_stopped(session_id)
    return STOP_MESSAGE.format(reason=reason) if reason else None
