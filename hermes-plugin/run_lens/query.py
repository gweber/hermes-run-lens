"""Read models: what the CLI, the dashboard API and the watchdog show.

Plain dicts and lists, JSON-serialisable, no formatting. One function per view.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import baseline as bl
from . import paths

SEV_RANK = "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'warn' THEN 2 ELSE 3 END"


def _jobs(conn) -> dict[str, dict]:
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM jobs")}


def _label(run: dict, jobs: dict) -> str:
    if run.get("job_id"):
        j = jobs.get(run["job_id"])
        return j["name"] if j else f"job {run['job_id']}"
    if run.get("task_id"):
        return f"kanban {run['task_id']}"
    t = (run.get("title") or "").strip()
    return t[:60] if t else run["root_id"]


def _pct(vals, p):
    return bl._pct(sorted(vals), p) if vals else None


# ── Runs ──────────────────────────────────────────────────────────────


def runs(conn: sqlite3.Connection, since: float, *, source: str | None = None, profile: str | None = None,
         job: str | None = None, active: bool = False, sort: str = "started", limit: int = 50) -> list[dict]:
    jobs = _jobs(conn)
    now = time.time()
    open_findings = {}
    for r in conn.execute(f"SELECT s.root_id, MIN({SEV_RANK}) rank, COUNT(*) n FROM findings f "
                          "JOIN sessions s ON s.id=f.session_id WHERE f.state='open' GROUP BY s.root_id"):
        open_findings[r["root_id"]] = ({0: "critical", 1: "high", 2: "warn", 3: "info"}[r["rank"]], r["n"])
    caps = {r["session_id"] for r in conn.execute(
        "SELECT DISTINCT s.root_id AS session_id FROM turns t JOIN sessions s ON s.id=t.session_id "
        "WHERE t.exit_reason LIKE 'max_iterations_reached%' OR t.exit_reason='guardrail_halt'")}
    out = []
    for r in bl.runs(conn, since):
        if source and r["source"] != source:
            continue
        if profile and r["profile"] != profile:
            continue
        if job:
            jn = jobs.get(r["job_id"] or "", {}).get("name")
            if job not in (r["job_id"], jn):
                continue
        live = bool(r["open_parts"]) and (now - (r["last_at"] or 0)) < 900
        if active and not live:
            continue
        sev = open_findings.get(r["root_id"])
        r.update({
            "label": _label(r, jobs),
            "status": "running" if live else ("capped" if r["root_id"] in caps else
                                              ("open" if r["open_parts"] else "done")),
            "worst_finding": sev[0] if sev else None,
            "findings": sev[1] if sev else 0,
        })
        out.append(r)
    key = {"started": lambda x: -(x["started_at"] or 0), "calls": lambda x: -(x["calls"] or 0),
           "tokens": lambda x: -(x["input_tokens"] or 0), "wall": lambda x: -(x["wall_s"] or 0)}.get(sort)
    out.sort(key=key or (lambda x: -(x["started_at"] or 0)))
    return out[:limit]


def resolve_run(conn: sqlite3.Connection, ref: str) -> str | None:
    """A run id from: a session id (any part of the chain), an id prefix, or a job name (latest run)."""
    r = conn.execute("SELECT root_id FROM sessions WHERE id=?", (ref,)).fetchone()
    if r:
        return r["root_id"]
    r = conn.execute("SELECT root_id FROM sessions WHERE id LIKE ? ORDER BY started_at DESC LIMIT 1",
                     (ref + "%",)).fetchone()
    if r:
        return r["root_id"]
    r = conn.execute("SELECT s.root_id FROM sessions s JOIN jobs j ON j.id=s.job_id WHERE j.name=? "
                     "ORDER BY s.started_at DESC LIMIT 1", (ref,)).fetchone()
    return r["root_id"] if r else None


def run_detail(conn: sqlite3.Connection, root_id: str) -> dict | None:
    members = [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE root_id=? ORDER BY started_at",
                                             (root_id,))]
    if not members:
        return None
    ids = [m["id"] for m in members]
    ph = ",".join("?" for _ in ids)
    head = next(iter(bl.runs(conn, 0, root=root_id)), None) or {}
    jobs = _jobs(conn)
    calls = [dict(r) for r in conn.execute(
        f"SELECT id, api_request_id, session_id, turn_id, seq, retry, model, served_model, started_at, ended_at, "
        f"latency_s, ttft_s, input_tokens, output_tokens, cache_read_tokens, tool_call_count, finish_reason, "
        f"status, error_type, error_message, origin FROM calls WHERE session_id IN ({ph}) ORDER BY ended_at", ids)]
    tools = [dict(r) for r in conn.execute(
        f"SELECT tool_call_id, session_id, name, fingerprint, shape, args_preview, status, error_type, "
        f"result_preview, result_chars, duration_ms, started_at, ended_at, origin FROM tools "
        f"WHERE session_id IN ({ph}) ORDER BY COALESCE(started_at, ended_at)", ids)]
    turns = [dict(r) for r in conn.execute(
        f"SELECT * FROM turns WHERE session_id IN ({ph}) ORDER BY ended_at", ids)]
    events = [dict(r) for r in conn.execute(
        f"SELECT at, kind, session_id, detail FROM events WHERE session_id IN ({ph}) ORDER BY at", ids)]
    findings = [dict(r) for r in conn.execute(
        f"SELECT * FROM findings WHERE session_id IN ({ph}) ORDER BY {SEV_RANK}, last_seen DESC", ids)]
    kanban = [dict(r) for r in conn.execute(f"SELECT * FROM kanban_runs WHERE session_id IN ({ph})", ids)]
    group = bl.group_key(members[0]["source"], members[0]["profile"], head.get("job_id"))
    base = bl.compute(conn).get(group)
    # Tool repetition summary: the top fingerprints by count, with their failure counts.
    reps: dict[str, dict] = {}
    for t in tools:
        k = t["fingerprint"] or t["tool_call_id"]
        d = reps.setdefault(k, {"name": t["name"], "args": t["args_preview"], "count": 0, "errors": 0})
        d["count"] += 1
        d["errors"] += 1 if t["status"] == "error" else 0
    top = sorted(reps.values(), key=lambda d: -d["count"])[:8]
    head = dict(head)
    head["label"] = _label(head, jobs) if head else root_id
    head["group"] = group
    if head.get("job_id") and head["job_id"] in jobs:
        head["job"] = jobs[head["job_id"]]
    return {"run": head, "sessions": members, "calls": calls, "tools": tools, "turns": turns, "events": events,
            "findings": findings, "kanban": kanban, "baseline": base, "repeats": top}


# ── Groups / jobs ─────────────────────────────────────────────────────


def profile_limits() -> dict[str, dict]:
    """agent.max_turns and loop hard stops as each profile's config.yaml sets them."""
    out = {}
    try:
        import yaml  # type: ignore
    except Exception:
        return out
    for profile, home in paths.homes():
        try:
            cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        agent = cfg.get("agent") or {}
        guard = cfg.get("tool_loop_guardrails") or {}
        out[profile] = {"max_turns": agent.get("max_turns"),
                        "hard_stop": bool(guard.get("hard_stop_enabled", False))}
    return out


def groups(conn: sqlite3.Connection, since: float) -> list[dict]:
    jobs = _jobs(conn)
    limits = profile_limits()
    base = bl.compute(conn)
    agg: dict[str, dict] = {}
    for r in bl.runs(conn, since):
        g = agg.setdefault(r["group"], {"group": r["group"], "runs": 0, "calls": 0, "input_tokens": 0,
                                        "output_tokens": 0, "last_at": 0, "profile": r["profile"],
                                        "source": r["source"], "job_id": r["job_id"], "max_calls": 0})
        g["runs"] += 1
        g["calls"] += r["calls"] or 0
        g["input_tokens"] += r["input_tokens"] or 0
        g["output_tokens"] += r["output_tokens"] or 0
        g["max_calls"] = max(g["max_calls"], r["calls"] or 0)
        g["last_at"] = max(g["last_at"], r["last_at"] or 0)
    open_f = {}
    for r in conn.execute("SELECT s.root_id, s.source, s.profile, s.job_id FROM findings f JOIN sessions s "
                          "ON s.id=f.session_id WHERE f.state='open' AND f.severity IN ('high','critical')"):
        k = bl.group_key(r["source"], r["profile"], r["job_id"])
        open_f[k] = open_f.get(k, 0) + 1
    days = max(1.0, (time.time() - since) / 86400)
    out = []
    for k, g in agg.items():
        b = base.get(k)
        prof = g["profile"] or "default"
        lim = limits.get(prof, {})
        g["name"] = jobs[g["job_id"]]["name"] if g["job_id"] in jobs else k
        g["schedule"] = jobs[g["job_id"]]["schedule"] if g["job_id"] in jobs else None
        g["baseline"] = b
        g["tokens_per_day"] = g["input_tokens"] / days
        g["high_findings"] = open_f.get(k, 0)
        g["max_turns"] = lim.get("max_turns")
        g["hard_stop"] = lim.get("hard_stop")
        g["suggested_cap"] = int(b["calls"]["p99"] * 1.5 + 0.999) if bl.known(b) else None
        out.append(g)
    out.sort(key=lambda g: -g["input_tokens"])
    return out


# ── Models ────────────────────────────────────────────────────────────


def models(conn: sqlite3.Connection, since: float) -> list[dict]:
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT c.model, c.served_model, c.latency_s, c.ttft_s, c.input_tokens, c.output_tokens, "
                          "c.status, s.source FROM calls c LEFT JOIN sessions s ON s.id=c.session_id "
                          "WHERE c.ended_at >= ?", (since,)):
        m = out.setdefault(r["model"] or "?", _model_blank(r["model"]))
        _model_add(m, r, "hermes:" + (r["source"] or "?"))
    for r in conn.execute("SELECT model, served_model, latency_s, ttft_s, input_tokens, output_tokens, status, "
                          "COALESCE(key_alias, '') || '|' || COALESCE(client, '') AS who FROM ext_calls "
                          "WHERE ended_at >= ?", (since,)):
        m = out.setdefault(r["model"] or "?", _model_blank(r["model"]))
        who = r["who"]
        label = ("hermes (unattributed)" if who.startswith("hermes-agent|") else
                 "other: " + (who.split("|", 1)[1] or who.split("|", 1)[0] or "unknown"))
        _model_add(m, r, label)
    res = []
    for m in out.values():
        lat, ttft = m.pop("_lat"), m.pop("_ttft")
        m["latency_p50"], m["latency_p95"] = _pct(lat, 0.5), _pct(lat, 0.95)
        m["ttft_p50"], m["ttft_p95"] = _pct(ttft, 0.5), _pct(ttft, 0.95)
        m["by_caller"] = sorted(({"caller": k, **v} for k, v in m["by_caller"].items()), key=lambda d: -d["calls"])
        m["served"] = sorted(({"model": k, "calls": v} for k, v in m["served"].items()), key=lambda d: -d["calls"])
        res.append(m)
    res.sort(key=lambda m: -m["calls"])
    return res


def _model_blank(name):
    return {"model": name or "?", "calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0,
            "by_caller": {}, "served": {}, "_lat": [], "_ttft": []}


def _model_add(m, r, caller):
    m["calls"] += 1
    m["input_tokens"] += r["input_tokens"] or 0
    m["output_tokens"] += r["output_tokens"] or 0
    if r["status"] not in (None, "ok", "success"):
        m["errors"] += 1
    c = m["by_caller"].setdefault(caller, {"calls": 0, "input_tokens": 0})
    c["calls"] += 1
    c["input_tokens"] += r["input_tokens"] or 0
    if r["served_model"]:
        m["served"][r["served_model"]] = m["served"].get(r["served_model"], 0) + 1
    if r["latency_s"]:
        m["_lat"].append(r["latency_s"])
    if r["ttft_s"]:
        m["_ttft"].append(r["ttft_s"])


# ── Findings, timeline, overview ──────────────────────────────────────


def findings(conn: sqlite3.Connection, *, state: str | None = "open", min_severity: str = "info",
             since: float | None = None, limit: int = 100) -> list[dict]:
    rank = {"info": 3, "warn": 2, "high": 1, "critical": 0}[min_severity]
    sql = f"SELECT *, {SEV_RANK} AS rank FROM findings WHERE {SEV_RANK} <= ?"
    args: list = [rank]
    if state:
        sql += " AND state=?"
        args.append(state)
    if since:
        sql += " AND last_seen >= ?"
        args.append(since)
    sql += " ORDER BY rank, last_seen DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        try:
            d["evidence"] = json.loads(d["evidence"] or "{}")
        except Exception:
            pass
        out.append(d)
    return out


def timeline(conn: sqlite3.Connection, since: float, bucket_s: int = 3600) -> dict:
    """Calls and prompt tokens per bucket, split by caller (hermes source or external)."""
    series: dict[str, dict[int, list[int]]] = {}
    for r in conn.execute("SELECT CAST(c.ended_at / ? AS INT) b, COALESCE(s.source, '?') src, COUNT(*) n, "
                          "SUM(c.input_tokens) tok FROM calls c LEFT JOIN sessions s ON s.id=c.session_id "
                          "WHERE c.ended_at >= ? GROUP BY b, src", (bucket_s, since)):
        series.setdefault(r["src"], {})[r["b"]] = [r["n"], r["tok"] or 0]
    for r in conn.execute("SELECT CAST(ended_at / ? AS INT) b, COUNT(*) n, SUM(input_tokens) tok FROM ext_calls "
                          "WHERE ended_at >= ? GROUP BY b", (bucket_s, since)):
        series.setdefault("external", {})[r["b"]] = [r["n"], r["tok"] or 0]
    start = int(since // bucket_s)
    end = int(time.time() // bucket_s)
    buckets = list(range(start, end + 1))
    return {"bucket_s": bucket_s, "buckets": [b * bucket_s for b in buckets],
            "series": {k: {"calls": [v.get(b, [0, 0])[0] for b in buckets],
                           "tokens": [v.get(b, [0, 0])[1] for b in buckets]} for k, v in series.items()}}


def overview(conn: sqlite3.Connection, since: float) -> dict:
    tot = conn.execute("SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) tin, COALESCE(SUM(output_tokens),0) tout "
                       "FROM calls WHERE ended_at >= ?", (since,)).fetchone()
    ext = conn.execute("SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) tin FROM ext_calls WHERE ended_at >= ?",
                       (since,)).fetchone()
    rs = runs(conn, since, limit=100000)
    by_source: dict[str, dict] = {}
    for r in rs:
        d = by_source.setdefault(r["source"] or "?", {"runs": 0, "calls": 0, "input_tokens": 0})
        d["runs"] += 1
        d["calls"] += r["calls"] or 0
        d["input_tokens"] += r["input_tokens"] or 0
    sev = {r["severity"]: r["n"] for r in conn.execute(
        "SELECT severity, COUNT(*) n FROM findings WHERE state='open' GROUP BY severity")}
    top = sorted(rs, key=lambda r: -(r["input_tokens"] or 0))[:10]
    concentration = None
    if tot["tin"]:
        top3 = sum(r["input_tokens"] or 0 for r in top[:3])
        concentration = top3 / max(1, sum(r["input_tokens"] or 0 for r in rs))
    return {
        "since": since, "now": time.time(),
        "calls": tot["calls"], "input_tokens": tot["tin"], "output_tokens": tot["tout"],
        "external_calls": ext["calls"], "external_input_tokens": ext["tin"],
        "runs": len(rs), "running": [r for r in rs if r["status"] == "running"],
        "by_source": by_source, "top_runs": top, "top3_token_share": concentration,
        "open_findings": sev, "db": str(paths.db_path()),
        "db_bytes": Path(paths.db_path()).stat().st_size if Path(paths.db_path()).exists() else 0,
        "last_ingest": {r["source"]: r["updated_at"] for r in conn.execute(
            "SELECT source, MAX(updated_at) updated_at FROM watermarks GROUP BY substr(source, 1, instr(source, ':'))")},
        "last_hook": conn.execute("SELECT MAX(ended_at) FROM calls WHERE origin='hook'").fetchone()[0],
    }
