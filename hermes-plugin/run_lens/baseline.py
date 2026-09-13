"""What a normal run looks like, per group — so "too many" means something.

A group is what should behave alike:

- `job:<id>`          a cron job (a nightly report is not an inbox check)
- `kanban:<profile>`  a profile's kanban workers
- `<source>:<profile>` everything else (telegram:default, cli:builder, …)

Only finished runs from the last 14 days count, and runs that already carry a loop
or cap finding are left out: a 500-call loop must not teach the baseline that
500 is normal. Percentiles are nearest-rank; with fewer than MIN_RUNS the baseline
is "unknown" and detectors fall back to absolute limits.
"""
from __future__ import annotations

import math
import sqlite3
import time

WINDOW_S = 14 * 86400
MIN_RUNS = 5
ANOMALOUS = ("loop.%", "cap.%", "runaway.%")


def group_key(source: str | None, profile: str | None, job_id: str | None) -> str:
    if job_id:
        return f"job:{job_id}"
    if source == "kanban":
        return f"kanban:{profile or 'default'}"
    return f"{source or '?'}:{profile or 'default'}"


RUNS_SQL = """
SELECT r.root_id,
       MIN(r.started_at) AS started_at,
       MAX(COALESCE(r.ended_at, r.last_activity_at, r.started_at)) AS last_at,
       SUM(CASE WHEN r.ended_at IS NULL THEN 1 ELSE 0 END) AS open_parts,
       SUM(r.api_calls) AS calls, SUM(r.tool_calls) AS tool_calls,
       SUM(r.input_tokens) AS input_tokens, SUM(r.output_tokens) AS output_tokens,
       (SELECT source FROM sessions x WHERE x.id = r.root_id) AS source,
       (SELECT profile FROM sessions x WHERE x.id = r.root_id) AS profile,
       MAX(r.job_id) AS job_id, MAX(r.task_id) AS task_id,
       (SELECT title FROM sessions x WHERE x.id = r.root_id) AS title,
       (SELECT model FROM sessions x WHERE x.id = r.root_id) AS model,
       COUNT(*) AS parts
FROM sessions r
WHERE r.root_id IN (SELECT root_id FROM sessions WHERE COALESCE(last_activity_at, ended_at, started_at) >= ?)
GROUP BY r.root_id
"""


def runs(conn: sqlite3.Connection, since: float, root: str | None = None) -> list[dict]:
    out = []
    sql, args = RUNS_SQL, (since,)
    if root:
        sql = RUNS_SQL.replace("WHERE r.root_id IN (SELECT root_id FROM sessions WHERE COALESCE(last_activity_at, ended_at, started_at) >= ?)",
                               "WHERE r.root_id = ?")
        args = (root,)
    for r in conn.execute(sql, args):
        d = dict(r)
        d["group"] = group_key(d["source"], d["profile"], d["job_id"])
        d["wall_s"] = max(0.0, (d["last_at"] or 0) - (d["started_at"] or 0))
        out.append(d)
    return out


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, math.ceil(p * len(sorted_vals)) - 1))
    return float(sorted_vals[k])


def _stats(vals: list[float]) -> dict:
    v = sorted(vals)
    med = _pct(v, 0.5)
    mad = _pct(sorted(abs(x - med) for x in v), 0.5) if v else 0.0
    return {"n": len(v), "p50": med, "p90": _pct(v, 0.9), "p95": _pct(v, 0.95), "p99": _pct(v, 0.99),
            "max": v[-1] if v else 0.0, "mad": mad}


def compute(conn: sqlite3.Connection, now: float | None = None) -> dict[str, dict]:
    now = now or time.time()
    flagged = {r[0] for r in conn.execute(
        "SELECT DISTINCT s.root_id FROM findings f JOIN sessions s ON s.id = f.session_id "
        "WHERE " + " OR ".join("f.kind LIKE ?" for _ in ANOMALOUS), ANOMALOUS)}
    # A run that ended at its cap is not a normal run even before any finding says so —
    # otherwise the first detection pass measures a loop against a baseline it inflated.
    flagged |= {r[0] for r in conn.execute(
        "SELECT DISTINCT s.root_id FROM turns t JOIN sessions s ON s.id = t.session_id "
        "WHERE t.exit_reason LIKE 'max_iterations_reached%' OR t.exit_reason IN ('guardrail_halt','budget_exhausted')")}
    groups: dict[str, dict[str, list[float]]] = {}
    for r in runs(conn, now - WINDOW_S):
        if r["open_parts"] or r["root_id"] in flagged or not r["calls"]:
            continue
        g = groups.setdefault(r["group"], {"calls": [], "tool_calls": [], "input_tokens": [], "wall_s": []})
        for k in g:
            g[k].append(float(r[k] or 0))
    return {name: {k: _stats(_trim(v)) for k, v in m.items()} for name, m in groups.items()}


def _trim(vals: list[float]) -> list[float]:
    """Drop gross outliers (beyond median + 15 × MAD) once there are enough runs to tell.

    Leaves ordinary spread alone — a job whose runs vary between 3 and 40 calls keeps
    all of them — but a single 500-call loop among fifty 6-call runs does not set p99.
    """
    if len(vals) < 8:
        return vals
    v = sorted(vals)
    med = _pct(v, 0.5)
    mad = _pct(sorted(abs(x - med) for x in v), 0.5)
    limit = med + 15 * max(mad, 1.0, 0.25 * med)
    kept = [x for x in v if x <= limit]
    return kept if len(kept) >= 0.8 * len(v) else vals


def known(b: dict | None) -> bool:
    return bool(b) and b["calls"]["n"] >= MIN_RUNS
