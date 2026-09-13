"""Cron jobs (jobs.json) and execution attempts (cron/executions.db) for every home.

A job's sessions are found by id (`cron_<job_id>_<YYYYmmdd_HHMMSS>`); executions add
what never becomes a session — fires the monitor suppressed, no_agent scripts,
fires skipped because the previous run was still going.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

from .. import paths
from ..store import Tx, get_watermark, set_watermark, upsert


def _iso(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def ingest(conn: sqlite3.Connection, full: bool = False) -> dict:
    stats = {"jobs": 0, "executions": 0}
    now = dt.datetime.now().timestamp()
    for profile, home in paths.homes():
        jf = home / "cron" / "jobs.json"
        if jf.exists():
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            jobs = data.get("jobs", []) if isinstance(data, dict) else data
            with Tx(conn):
                for j in jobs:
                    if not isinstance(j, dict) or not j.get("id"):
                        continue
                    upsert(conn, "jobs", {
                        "id": j["id"], "profile": profile, "name": j.get("name"),
                        "schedule": j.get("schedule_display") or json.dumps(j.get("schedule")),
                        "model": j.get("model"), "enabled": 1 if j.get("enabled", True) else 0,
                        "state": j.get("state"), "no_agent": 1 if j.get("no_agent") else 0,
                        "monitor": j.get("monitor_script") or j.get("monitor_url"),
                        "script": j.get("script"), "deliver": j.get("deliver"),
                        "last_status": j.get("last_status"), "last_run_at": _iso(j.get("last_run_at")),
                        "updated_at": now,
                    }, ("id",))
                    stats["jobs"] += 1
        ef = home / "cron" / "executions.db"
        if not ef.exists() or ef.stat().st_size == 0:
            continue
        try:
            src = sqlite3.connect(f"file:{ef}?mode=ro", uri=True, timeout=10)
            src.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        wm_key = f"cron:{profile}:executions"
        wm = "" if full else (get_watermark(conn, wm_key, "") or "")
        try:
            rows = src.execute(
                "SELECT id, job_id, source, status, claimed_at, started_at, finished_at, error FROM executions "
                "WHERE COALESCE(finished_at, claimed_at) >= ? OR status IN ('claimed','running') "
                "ORDER BY claimed_at", (wm,)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            src.close()
        newest = wm
        with Tx(conn):
            for r in rows:
                upsert(conn, "executions", {
                    "id": r["id"], "job_id": r["job_id"], "profile": profile, "source": r["source"],
                    "status": r["status"], "claimed_at": _iso(r["claimed_at"]),
                    "started_at": _iso(r["started_at"]), "finished_at": _iso(r["finished_at"]),
                    "error": (r["error"] or None) and str(r["error"])[:500],
                }, ("id",))
                stamp = r["finished_at"] or r["claimed_at"] or ""
                if stamp > newest and r["status"] not in ("claimed", "running"):
                    newest = stamp
                stats["executions"] += 1
            set_watermark(conn, wm_key, newest)
    return stats
