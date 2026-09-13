"""Kanban task runs, linked to the worker session that did the work.

Boards live under the root home only: `kanban.db` (default board) and
`kanban/boards/<slug>/kanban.db`. A worker is `hermes -p <assignee> chat -q "work
kanban task <id>"`; its session is found, in order of reliability, by
`task_runs.metadata.worker_session_id` (stamped on complete/review only), or by a
session titled with the task id that started inside the run's window.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import paths
from ..store import Tx, upsert


def _boards() -> list[tuple[str, Path]]:
    root = paths.root_home()
    out = []
    if (root / "kanban.db").exists():
        out.append(("default", root / "kanban.db"))
    bdir = root / "kanban" / "boards"
    if bdir.is_dir():
        for b in sorted(bdir.iterdir()):
            if (b / "kanban.db").exists():
                out.append((b.name, b / "kanban.db"))
    return out


def ingest(conn: sqlite3.Connection, full: bool = False) -> dict:
    stats = {"runs": 0, "linked": 0}
    since = 0 if full else (conn.execute("SELECT MAX(started_at) FROM kanban_runs").fetchone()[0] or 0) - 3 * 86400
    for board, path in _boards():
        if path.stat().st_size == 0:
            continue
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
            src.row_factory = sqlite3.Row
            rows = src.execute(
                "SELECT r.id, r.task_id, r.profile, r.status, r.outcome, r.started_at, r.ended_at, r.metadata, "
                "r.error, t.title FROM task_runs r LEFT JOIN tasks t ON t.id = r.task_id "
                "WHERE COALESCE(r.ended_at, r.started_at, 0) >= ? OR r.ended_at IS NULL", (since,)).fetchall()
        except sqlite3.Error:
            continue
        finally:
            try:
                src.close()
            except Exception:
                pass
        with Tx(conn):
            for r in rows:
                sid = None
                try:
                    sid = (json.loads(r["metadata"] or "{}") or {}).get("worker_session_id")
                except Exception:
                    pass
                if not sid and r["started_at"]:
                    hit = conn.execute(
                        "SELECT id FROM sessions WHERE task_id=? AND started_at BETWEEN ? AND ? "
                        "AND (parent_id IS NULL OR root_id = id) ORDER BY started_at LIMIT 1",
                        (r["task_id"], r["started_at"] - 60, (r["ended_at"] or 4e9) + 60)).fetchone()
                    sid = hit["id"] if hit else None
                upsert(conn, "kanban_runs", {
                    "board": board, "run_id": r["id"], "task_id": r["task_id"], "title": r["title"],
                    "profile": r["profile"], "status": r["status"], "outcome": r["outcome"],
                    "started_at": r["started_at"], "ended_at": r["ended_at"], "session_id": sid,
                    "error": (r["error"] or None) and str(r["error"])[:500],
                }, ("board", "run_id"))
                stats["runs"] += 1
                if sid:
                    stats["linked"] += 1
                    conn.execute("UPDATE sessions SET task_id=COALESCE(task_id, ?), board=COALESCE(board, ?) "
                                 "WHERE root_id=(SELECT root_id FROM sessions WHERE id=?)",
                                 (r["task_id"], board, sid))
    return stats
