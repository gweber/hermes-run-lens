"""Sessions and tool calls from Hermes's own state.db files.

Which database holds what (checked 2026-09-13):

- `~/.hermes/state.db` — everything run by a process with the root home: gateway
  platforms, CLI/TUI chats, default-profile kanban workers, subagents, AND the cron
  runs of multiplexed secondary profiles, stamped with `profile_name`.
- `~/.hermes/profiles/<p>/state.db` — sessions of processes started with that
  profile's home: `hermes -p <p>` chats and that profile's kanban workers.

Sessions come across whole; state.db keeps their aggregates current per API call.
Tool calls are rebuilt from `messages`: the assistant row carries the call (name,
arguments), the tool row with the same `tool_call_id` carries the result. Neither
side is guaranteed to arrive in the same ingest pass, so each is upserted on its own.
Rows a live hook already wrote are only gap-filled, never overwritten.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .. import paths, textutil
from ..store import Tx, get_watermark, set_watermark, upsert

CRON_ID = re.compile(r"^cron_([0-9a-f]{12})_\d{8}_\d{6}$")
KANBAN_TITLE = re.compile(r"(?i)\bkanban task (t_[0-9a-f]{6,})")
ACTIVE_MARGIN_S = 1800
MSG_BATCH = 5000

SESSION_COLS = ("id", "source", "model", "parent_session_id", "started_at", "ended_at",
                "last_activity_at", "end_reason", "api_call_count", "tool_call_count",
                "input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens",
                "title", "profile_name")


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM sessions LIMIT 1")
        return conn
    except sqlite3.Error:
        return None


def _columns(src: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in src.execute(f"PRAGMA table_info({table})")}


def ingest(conn: sqlite3.Connection, full: bool = False) -> dict:
    stats = {"sessions": 0, "tools": 0, "dbs": 0}
    for profile, home in paths.homes():
        src = _open_ro(home / "state.db")
        if src is None:
            continue
        stats["dbs"] += 1
        try:
            stats["sessions"] += _sessions(conn, src, profile, full)
            stats["tools"] += _tools(conn, src, profile, full)
        finally:
            src.close()
    _link_roots(conn)
    return stats


def _sessions(conn, src, profile, full) -> int:
    have = _columns(src, "sessions")
    cols = [c for c in SESSION_COLS if c in have]
    wm_key = f"statedb:{profile}:sessions"
    wm = 0.0 if full else float(get_watermark(conn, wm_key, 0) or 0)
    act = "COALESCE(last_activity_at, ended_at, started_at)" if "last_activity_at" in have \
        else "COALESCE(ended_at, started_at)"
    rows = src.execute(
        f"SELECT {','.join(cols)}, {act} AS act FROM sessions WHERE {act} >= ? ORDER BY started_at",
        (wm - ACTIVE_MARGIN_S if wm else 0,)).fetchall()
    newest = wm
    with Tx(conn):
        for r in rows:
            d = dict(r)
            sid = d["id"]
            m = CRON_ID.match(sid)
            title = d.get("title")
            task = None
            if title:
                k = KANBAN_TITLE.search(title)
                task = k.group(1) if k else None
            row = {
                "id": sid,
                "parent_id": d.get("parent_session_id"),
                "profile": d.get("profile_name") or profile,
                "source": d.get("source"),
                "job_id": m.group(1) if m else None,
                "task_id": task,
                "title": title,
                "model": d.get("model"),
                "started_at": d.get("started_at"),
                "ended_at": d.get("ended_at"),
                "last_activity_at": d.get("last_activity_at"),
                "end_reason": d.get("end_reason"),
                "api_calls": d.get("api_call_count") or 0,
                "tool_calls": d.get("tool_call_count") or 0,
                "input_tokens": d.get("input_tokens") or 0,
                "output_tokens": d.get("output_tokens") or 0,
                "cache_read_tokens": d.get("cache_read_tokens") or 0,
                "reasoning_tokens": d.get("reasoning_tokens") or 0,
                "origin": "statedb",
                "updated_at": d["act"],
            }
            upsert(conn, "sessions", row, ("id",), keep=("job_id", "task_id"))
            newest = max(newest, d["act"] or 0)
        set_watermark(conn, wm_key, newest)
    return len(rows)


def _tools(conn, src, profile, full) -> int:
    have = _columns(src, "messages")
    wm_key = f"statedb:{profile}:msgid"
    last = 0 if full else int(get_watermark(conn, wm_key, 0) or 0)
    n = 0
    fill_only = ("session_id", "name", "fingerprint", "shape", "args_preview", "status",
                 "error_type", "result_preview", "result_chars", "started_at", "ended_at",
                 "duration_ms", "turn_id", "api_request_id", "origin")
    while True:
        rows = src.execute(
            "SELECT id, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp "
            "FROM messages WHERE id > ? AND (role='tool' OR (role='assistant' AND tool_calls IS NOT NULL)) "
            "ORDER BY id LIMIT ?", (last, MSG_BATCH)).fetchall()
        if not rows:
            break
        with Tx(conn):
            for r in rows:
                last = r["id"]
                if r["role"] == "assistant":
                    try:
                        calls = json.loads(r["tool_calls"] or "[]")
                    except Exception:
                        continue
                    for c in calls if isinstance(calls, list) else []:
                        fn = (c or {}).get("function") or {}
                        cid = c.get("id") or c.get("call_id")
                        if not cid:
                            continue
                        name = fn.get("name") or "?"
                        args = fn.get("arguments")
                        exact, shape = textutil.fingerprints(name, args)
                        upsert(conn, "tools", {
                            "tool_call_id": cid, "session_id": r["session_id"], "name": name,
                            "fingerprint": exact, "shape": shape,
                            "args_preview": textutil.preview(args),
                            "started_at": r["timestamp"], "origin": "statedb",
                        }, ("tool_call_id",), keep=fill_only)
                        n += 1
                else:
                    cid = r["tool_call_id"]
                    if not cid:
                        continue
                    content = r["content"] or ""
                    failed, why = textutil.tool_failed(r["tool_name"] or "", content)
                    upsert(conn, "tools", {
                        "tool_call_id": cid, "session_id": r["session_id"],
                        "name": r["tool_name"],
                        "status": "error" if failed else "ok",
                        "error_type": ("tool_error:" + why[:80]) if failed else None,
                        "result_preview": textutil.preview(content) if failed else textutil.preview(content, 80),
                        "result_chars": len(content), "ended_at": r["timestamp"],
                        "origin": "statedb",
                    }, ("tool_call_id",), keep=fill_only)
                    for level, code, count in textutil.guardrail_marks(content):
                        conn.execute(
                            "INSERT OR IGNORE INTO events(at, kind, session_id, detail, origin, dedupe) "
                            "VALUES(?,?,?,?,?,?)",
                            (r["timestamp"], "guardrail." + ("halt" if level == "hard stop" else "warn"),
                             r["session_id"], json.dumps({"code": code, "count": count, "tool_call_id": cid}),
                             "statedb", f"guard:{cid}:{code}"))
            set_watermark(conn, wm_key, last)
        if len(rows) < MSG_BATCH:
            break
    # Durations where both halves are known and no hook measured one.
    conn.execute("UPDATE tools SET duration_ms = (ended_at - started_at) * 1000.0 "
                 "WHERE duration_ms IS NULL AND ended_at IS NOT NULL AND started_at IS NOT NULL "
                 "AND ended_at >= started_at")
    return n


def _link_roots(conn: sqlite3.Connection) -> None:
    """root_id: follow parents across compression boundaries; inherit job/task."""
    with Tx(conn):
        conn.execute(
            "UPDATE sessions SET root_id = id WHERE root_id IS NULL AND (parent_id IS NULL OR "
            "parent_id NOT IN (SELECT id FROM sessions WHERE end_reason='compression'))")
        for _ in range(50):
            cur = conn.execute(
                "UPDATE sessions SET root_id = (SELECT p.root_id FROM sessions p WHERE p.id = sessions.parent_id), "
                "job_id = COALESCE(sessions.job_id, (SELECT p.job_id FROM sessions p WHERE p.id = sessions.parent_id)), "
                "task_id = COALESCE(sessions.task_id, (SELECT p.task_id FROM sessions p WHERE p.id = sessions.parent_id)) "
                "WHERE (root_id IS NULL OR root_id = id) AND parent_id IS NOT NULL AND EXISTS "
                "(SELECT 1 FROM sessions p WHERE p.id = sessions.parent_id AND p.end_reason='compression' "
                " AND p.root_id IS NOT NULL AND p.root_id != sessions.id)")
            if cur.rowcount == 0:
                break
        conn.execute("UPDATE sessions SET root_id = id WHERE root_id IS NULL")
