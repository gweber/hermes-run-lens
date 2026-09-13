"""run-lens dashboard backend — mounted at /api/plugins/run-lens/ by the Hermes dashboard.

Thin: every route is one call into run_lens.query (the same read models the CLI
prints), plus the three writes a person makes from the page — refresh, acknowledge
or resolve a finding, stop a run.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from run_lens import control, detect, fmt, ingest, query  # noqa: E402
from run_lens.store import Tx, connect  # noqa: E402

try:
    from fastapi import APIRouter, Body, HTTPException
except Exception:  # importable without FastAPI, for tests
    class APIRouter:  # type: ignore
        def get(self, *_a, **_k):
            return lambda fn: fn

        post = get

    def Body(default=None, **_k):  # type: ignore
        return default

    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code: int = 500, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

router = APIRouter()

_refresh_lock = threading.Lock()
_last_refresh = {"at": 0.0, "report": None}
AUTO_REFRESH_S = 60


def _since(text: str | None, default: str) -> float:
    try:
        return fmt.parse_since(text or default, 86400)
    except SystemExit as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _maybe_refresh(force: bool = False) -> dict | None:
    """Pull what is new from the local sources, at most once a minute (LiteLLM on force only)."""
    now = time.time()
    if not force and now - _last_refresh["at"] < AUTO_REFRESH_S:
        return _last_refresh["report"]
    if not _refresh_lock.acquire(blocking=force):
        return _last_refresh["report"]
    try:
        conn = connect()
        only = () if force else ("cron", "statedb", "log")
        rep = ingest.run(conn, only=only)
        rep["detect"] = detect.run_all(conn, since=now - 6 * 3600)
        _last_refresh.update(at=time.time(), report=rep)
        return rep
    finally:
        _refresh_lock.release()


@router.get("/overview")
def overview(since: str = "24h"):
    _maybe_refresh()
    conn = connect()
    s = _since(since, "24h")
    o = query.overview(conn, s)
    hours = (time.time() - s) / 3600
    o["timeline"] = query.timeline(conn, s, 3600 if hours <= 72 else 86400)
    o["findings"] = query.findings(conn, min_severity="warn", limit=12)
    o["last_refresh"] = _last_refresh["at"]
    return o


@router.get("/runs")
def runs(since: str = "24h", source: str = "", profile: str = "", job: str = "", active: bool = False,
         sort: str = "started", limit: int = 200):
    _maybe_refresh()
    conn = connect()
    return {"runs": query.runs(conn, _since(since, "24h"), source=source or None, profile=profile or None,
                               job=job or None, active=active, sort=sort, limit=min(max(limit, 1), 2000))}


@router.get("/run/{ref}")
def run(ref: str):
    conn = connect()
    root = query.resolve_run(conn, ref)
    if not root:
        raise HTTPException(status_code=404, detail=f"no run matches {ref!r}")
    d = query.run_detail(conn, root)
    stop = conn.execute("SELECT * FROM stops WHERE session_id=?", (root,)).fetchone()
    d["stop"] = dict(stop) if stop else None
    return d


@router.get("/jobs")
def jobs(since: str = "7d"):
    _maybe_refresh()
    return {"groups": query.groups(connect(), _since(since, "7d"))}


@router.get("/models")
def models(since: str = "24h"):
    return {"models": query.models(connect(), _since(since, "24h"))}


@router.get("/findings")
def findings(state: str = "open", severity: str = "info", limit: int = 300):
    st = None if state in ("", "all") else state
    return {"findings": query.findings(connect(), state=st, min_severity=severity, limit=min(limit, 2000))}


@router.post("/findings/{fid}/state")
def set_finding_state(fid: int, body: dict = Body(default={})):
    state = (body or {}).get("state")
    if state not in ("open", "acked", "resolved"):
        raise HTTPException(status_code=400, detail="state must be open, acked or resolved")
    conn = connect()
    with Tx(conn):
        cur = conn.execute("UPDATE findings SET state=? WHERE id=?", (state, fid))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="no such finding")
    return {"id": fid, "state": state}


@router.post("/stop/{ref}")
def stop(ref: str, body: dict = Body(default={})):
    conn = connect()
    root = query.resolve_run(conn, ref)
    if not root:
        raise HTTPException(status_code=404, detail=f"no run matches {ref!r}")
    ids = [r[0] for r in conn.execute("SELECT id FROM sessions WHERE root_id=?", (root,))]
    reason = ((body or {}).get("reason") or "stopped from the dashboard")[:200]
    with Tx(conn):
        control.request_stop(conn, ids, by="dashboard", reason=reason)
    return {"root": root, "sessions": len(ids), "poll_seconds": control.POLL_S}


@router.post("/refresh")
def refresh():
    return {"report": _maybe_refresh(force=True), "at": _last_refresh["at"]}


@router.get("/timeline")
def timeline(since: str = "24h", bucket: int = 3600):
    return query.timeline(connect(), _since(since, "24h"), max(300, bucket))
