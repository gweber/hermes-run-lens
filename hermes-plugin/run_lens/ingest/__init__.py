"""Ingesters: pull history out of the places Hermes and LiteLLM already write to.

Order matters: jobs first (the log parser maps job names to ids), sessions before
calls (turn and call rows attach to them), calls before LiteLLM (spend-log rows are
matched onto calls), kanban last (links runs to sessions).
"""
from __future__ import annotations

import time

from .. import settings
from ..store import Tx, connect
from . import agentlog, cron, kanban, litellm, statedb

STEPS = (("cron", cron.ingest), ("statedb", statedb.ingest), ("log", agentlog.ingest),
         ("litellm", litellm.ingest), ("kanban", kanban.ingest))


def run(conn=None, full: bool = False, only: tuple[str, ...] = ()) -> dict:
    conn = conn or connect()
    report = {}
    for name, fn in STEPS:
        if only and name not in only:
            continue
        t = time.time()
        try:
            report[name] = fn(conn, full=full)
        except Exception as exc:  # one broken source must not stop the others
            report[name] = {"error": f"{type(exc).__name__}: {exc}"}
        report[name]["seconds"] = round(time.time() - t, 2)
    if not only:
        try:
            report["retention"] = prune(conn)
        except Exception as exc:
            report["retention"] = {"error": str(exc)}
    if not only or "litellm" in only:
        try:
            report["litellm"]["reconciled"] = litellm.reconcile(conn)
        except Exception as exc:
            report.setdefault("litellm", {})["reconcile_error"] = str(exc)
    return report


def prune(conn) -> dict:
    """Drop per-call detail older than `retention_days`; sessions and findings stay."""
    days = float(settings.get("retention_days") or 0)
    if days <= 0:
        return {"skipped": "retention_days is 0"}
    cutoff = time.time() - days * 86400
    out = {}
    with Tx(conn):
        for table, col in (("calls", "ended_at"), ("tools", "ended_at"), ("events", "at"), ("ext_calls", "ended_at"),
                           ("turns", "ended_at")):
            out[table] = conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)).rowcount
    return out
