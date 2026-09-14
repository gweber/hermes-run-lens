"""Findings: things in the recorded runs a person should know about.

Each detector reads the store and upserts findings keyed by a fingerprint, so
re-running detection updates a finding instead of repeating it. Severity:

    info      worth a look in the dashboard, never notified
    warn      notified once
    high      notified once, shown first
    critical  a run is burning the machine right now

Thresholds are relative to the run's own group baseline (see baseline.py) where one
exists, and absolute otherwise. Every finding carries the numbers it was judged on
(`evidence`) and, where there is one, the change that would have prevented it
(`suggestion`).

The incident these were written against: one cron run repeated a refused write 500
times (cap hit, identical 117-token replies), another re-ran one failing Python
snippet ~450 times; together 890 of the day's 1,142 calls on a shared model.
Every detector below fires on at least one of those two runs.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter

from . import baseline as bl
from .store import Tx

SEVERITY_ORDER = {"info": 0, "warn": 1, "high": 2, "critical": 3}

ABS_CALLS = 100
ABS_TOKENS = 5_000_000
PROMPT_BIG = 120_000
IDENTICAL_STREAK = 10
EXACT_FAILURES = 5
ACTIVE_STALE_S = 600
AUX_WINDOW_S = 86400
AUX_THRESHOLD = 3
AUX_THRESHOLD_BY_TASK = {"paid_lane": 1}   # real money: one occurrence is enough
AUX_SAMPLES = 3


def upsert_finding(conn: sqlite3.Connection, *, kind: str, severity: str, fingerprint: str, title: str,
                   detail: str = "", evidence: dict | None = None, suggestion: str | None = None,
                   session_id: str | None = None, job_id: str | None = None, model: str | None = None,
                   at: float | None = None, origin: str = "detect") -> None:
    at = at or time.time()
    row = conn.execute("SELECT id, severity, state FROM findings WHERE fingerprint=?", (fingerprint,)).fetchone()
    ev = json.dumps(evidence or {}, default=str)
    if row is None:
        conn.execute(
            "INSERT INTO findings(fingerprint, kind, severity, session_id, job_id, model, title, detail, evidence, "
            "suggestion, first_seen, last_seen, count, state, origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,'open',?)",
            (fingerprint, kind, severity, session_id, job_id, model, title, detail, ev, suggestion, at, at, origin))
        return
    escalated = SEVERITY_ORDER.get(severity, 0) > SEVERITY_ORDER.get(row["severity"], 0)
    conn.execute(
        "UPDATE findings SET severity=CASE WHEN ? THEN ? ELSE severity END, title=?, detail=?, evidence=?, "
        "suggestion=COALESCE(?, suggestion), count=count + CASE WHEN ? > last_seen + 1 THEN 1 ELSE 0 END, "
        "last_seen=MAX(last_seen, ?), "
        "notified_at=CASE WHEN ? THEN NULL ELSE notified_at END, "
        "state=CASE WHEN state='resolved' THEN 'open' ELSE state END WHERE id=?",
        (1 if escalated else 0, severity, title, detail, ev, suggestion, at, at, 1 if escalated else 0, row["id"]))


def _fmt_n(n: float) -> str:
    n = float(n or 0)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}k"
    return f"{n:.0f}"


def _label(run: dict, jobs: dict) -> str:
    if run.get("job_id"):
        return jobs.get(run["job_id"], run["job_id"])
    if run.get("task_id"):
        return f"kanban {run['task_id']}"
    return f"{run['source']}:{run['profile']}" + (f" “{run['title'][:40]}”" if run.get("title") else "")


def run_all(conn: sqlite3.Connection, since: float | None = None, now: float | None = None) -> dict:
    now = now or time.time()
    since = since or now - 86400
    jobs = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM jobs")}
    base = bl.compute(conn, now)
    counts: Counter = Counter()
    runs = bl.runs(conn, since)
    with Tx(conn):
        for run in runs:
            members = [r[0] for r in conn.execute("SELECT id FROM sessions WHERE root_id=?", (run["root_id"],))]
            ctx = _RunCtx(conn, run, members, base.get(run["group"]), jobs, now)
            for det in (_cap_hit, _exact_failures, _repeated_shape, _identical_output, _runaway_calls,
                        _runaway_tokens, _active_too_long, _prompt_bloat, _compression_churn, _api_errors):
                try:
                    counts[det.__name__] += det(ctx) or 0
                except Exception as exc:  # a detector bug must not hide the others
                    counts[f"{det.__name__}_error"] += 1
                    counts[f"err:{type(exc).__name__}:{exc}"[:120]] += 1
        for det in (_served_fallback, _cron_overlap, _settle_waste, _model_hog, _kanban_failures, _aux_failures):
            try:
                counts[det.__name__] += det(conn, since, now, jobs) or 0
            except Exception as exc:
                counts[f"{det.__name__}_error"] += 1
                counts[f"err:{type(exc).__name__}:{exc}"[:120]] += 1
    return {"runs": len(runs), **counts}


class _RunCtx:
    def __init__(self, conn, run, members, base, jobs, now):
        self.conn, self.run, self.members, self.base, self.jobs, self.now = conn, run, members, base, jobs, now
        self.label = _label(run, jobs)
        self.ph = ",".join("?" for _ in members)

    def q(self, sql, extra=()):
        return self.conn.execute(sql.replace("{M}", self.ph), (*self.members, *extra)).fetchall()

    def finding(self, kind, severity, title, **kw):
        upsert_finding(self.conn, kind=kind, severity=severity, fingerprint=f"{kind}:{self.run['root_id']}",
                       title=title, session_id=self.run["root_id"], job_id=self.run.get("job_id"),
                       model=self.run.get("model"), at=self.run.get("last_at"), **kw)
        return 1


def _cap_hit(c: _RunCtx) -> int:
    rows = c.q("SELECT exit_reason, api_calls, max_iterations, ended_at FROM turns WHERE session_id IN ({M}) "
               "AND (exit_reason LIKE 'max_iterations_reached%' OR exit_reason IN "
               "('guardrail_halt','budget_exhausted'))")
    if not rows:
        return 0
    r = rows[-1]
    halted = r["exit_reason"] == "guardrail_halt"
    _merge_live_cap_findings(c)
    return c.finding(
        "cap.guardrail_halt" if halted else "cap.max_iterations", "warn" if halted else "high",
        f"{c.label}: {'stopped by the loop guardrail' if halted else 'ran into its call cap'} "
        f"({r['api_calls']}/{r['max_iterations']} calls)" if not halted else f"{c.label}: stopped by the loop guardrail",
        detail="A run that ends at its cap did not finish its task; everything up to the cap was spent anyway.",
        evidence={"exit_reason": r["exit_reason"], "api_calls": r["api_calls"], "max": r["max_iterations"],
                  "turns_at_cap": len(rows)},
        suggestion=None if halted else _cap_suggestion(c))


def _merge_live_cap_findings(c: _RunCtx) -> None:
    """The recorder flags a cap hit live, keyed on the session it saw. After a compression that is a
    child session, so one capped run was reported twice (t_fd4b3b09, 2026-09-14): once per child from
    the hook, once per root from here. The root finding speaks for the run; the children's copies are
    closed and marked notified before the watch reports anything."""
    children = [m for m in c.members if m != c.run["root_id"]]
    if not children:
        return
    ph = ",".join("?" for _ in children)
    c.conn.execute(
        f"UPDATE findings SET state='resolved', notified_at=COALESCE(notified_at, ?) WHERE origin='hook' "
        f"AND kind IN ('cap.max_iterations','cap.guardrail_halt') AND session_id IN ({ph})",
        (c.now, *children))


def _cap_suggestion(c: _RunCtx) -> str | None:
    if bl.known(c.base):
        p99 = c.base["calls"]["p99"]
        return (f"Normal runs of this group need ≤{p99:.0f} calls (p99 of {c.base['calls']['n']}); a cap near "
                f"{max(8, int(p99 * 1.5 + 0.999))} would have ended this run hundreds of calls earlier.")
    return None


def _exact_failures(c: _RunCtx) -> int:
    rows = c.q("SELECT name, fingerprint, COUNT(*) n, MIN(started_at) first, MAX(ended_at) last, "
               "MAX(result_preview) sample, MAX(args_preview) args FROM tools WHERE session_id IN ({M}) "
               "AND status='error' GROUP BY fingerprint HAVING n >= ? ORDER BY n DESC LIMIT 1", (EXACT_FAILURES,))
    if not rows:
        return 0
    r = rows[0]
    sev = "critical" if r["n"] >= 100 else "high" if r["n"] >= 20 else "warn"
    return c.finding(
        "loop.exact_failure", sev, f"{c.label}: the same {r['name']} call failed {r['n']}×",
        detail="Identical arguments, identical failure — the model is retrying instead of changing course.",
        evidence={"tool": r["name"], "count": r["n"], "args": r["args"], "result": r["sample"],
                  "minutes": round(((r["last"] or 0) - (r["first"] or 0)) / 60, 1)},
        suggestion="tool_loop_guardrails.hard_stop_enabled: true in this profile's config.yaml stops this at the "
                   "5th identical failure.")


def _repeated_shape(c: _RunCtx) -> int:
    total = c.q("SELECT COUNT(*) n FROM tools WHERE session_id IN ({M})")[0]["n"]
    if total < 12:
        return 0
    rows = c.q("SELECT name, shape, COUNT(*) n, SUM(status='error') errs, MAX(args_preview) args FROM tools "
               "WHERE session_id IN ({M}) GROUP BY shape ORDER BY n DESC LIMIT 1")
    r = rows[0]
    if r["n"] < 12 or r["n"] < 0.5 * total:
        return 0
    sev = "high" if r["n"] >= 40 else "warn"
    return c.finding(
        "loop.repeated_call", sev, f"{c.label}: {r['n']} of {total} tool calls are the same {r['name']} call",
        detail="Same tool, arguments differing only in numbers or long text — a loop that varies its wording.",
        evidence={"tool": r["name"], "count": r["n"], "total": total, "errors": r["errs"], "args": r["args"]})


def _identical_output(c: _RunCtx) -> int:
    rows = c.q("SELECT output_tokens FROM calls WHERE session_id IN ({M}) ORDER BY ended_at")
    best = cur = 0
    prev = None
    val = None
    for r in rows:
        o = r["output_tokens"]
        if o and o == prev:
            cur += 1
        else:
            cur = 1
        prev = o
        if cur > best:
            best, val = cur, o
    if best < IDENTICAL_STREAK:
        return 0
    sev = "critical" if best >= 100 else "high"
    return c.finding(
        "loop.identical_output", sev, f"{c.label}: {best} calls in a row answered with exactly {val} tokens",
        detail="The model is producing the same reply over and over; each call still re-reads the whole context.",
        evidence={"streak": best, "output_tokens": val, "calls": len(rows)})


def _runaway_calls(c: _RunCtx) -> int:
    calls = c.run["calls"] or 0
    if bl.known(c.base):
        b = c.base["calls"]
        limit = max(3 * b["p95"], b["p99"] + 10, 25)
        if calls <= limit:
            return 0
        sev = "critical" if calls >= 10 * max(b["p95"], 5) else "high"
        return c.finding(
            "runaway.calls", sev, f"{c.label}: {calls} calls — normal is ≤{b['p95']:.0f} (p95)",
            detail=f"{calls / max(b['p50'], 1):.0f}× the median run of this group.",
            evidence={"calls": calls, "p50": b["p50"], "p95": b["p95"], "p99": b["p99"], "n": b["n"],
                      "tokens": c.run["input_tokens"]},
            suggestion=_cap_suggestion(c))
    if calls >= ABS_CALLS:
        return c.finding("runaway.calls", "high", f"{c.label}: {calls} calls in one run",
                         detail="No baseline for this group yet; flagged on the absolute limit.",
                         evidence={"calls": calls, "tokens": c.run["input_tokens"]})
    return 0


def _runaway_tokens(c: _RunCtx) -> int:
    tok = c.run["input_tokens"] or 0
    if bl.known(c.base):
        b = c.base["input_tokens"]
        if tok <= max(5 * b["p95"], 2_000_000):
            return 0
        return c.finding(
            "runaway.tokens", "high", f"{c.label}: {_fmt_n(tok)} prompt tokens — normal is ≤{_fmt_n(b['p95'])}",
            evidence={"input_tokens": tok, "p50": b["p50"], "p95": b["p95"], "n": b["n"]})
    if tok >= ABS_TOKENS:
        return c.finding("runaway.tokens", "high", f"{c.label}: {_fmt_n(tok)} prompt tokens in one run",
                         evidence={"input_tokens": tok})
    return 0


def _active_too_long(c: _RunCtx) -> int:
    r = c.run
    if not r["open_parts"] or (c.now - (r["last_at"] or 0)) > ACTIVE_STALE_S:
        return 0
    if r["source"] not in ("cron", "kanban"):
        return 0  # a chat can legitimately stay open for hours
    limit = 1800.0
    if bl.known(c.base):
        limit = max(limit, 3 * c.base["wall_s"]["p95"])
    if r["wall_s"] <= limit:
        return 0
    return c.finding(
        "runaway.active", "critical", f"{c.label}: still running after {r['wall_s'] / 60:.0f} min",
        detail="The run is live now. `hermes lens stop <id>` interrupts it without restarting the gateway.",
        evidence={"minutes": round(r["wall_s"] / 60, 1), "limit_minutes": round(limit / 60, 1),
                  "calls": r["calls"], "session": r["root_id"]})


def _prompt_bloat(c: _RunCtx) -> int:
    rows = c.q("SELECT input_tokens FROM calls WHERE session_id IN ({M}) AND input_tokens > 0 ORDER BY ended_at")
    if len(rows) < 3:
        return 0
    first, peak = rows[0]["input_tokens"], max(r["input_tokens"] for r in rows)
    if peak < PROMPT_BIG:
        return 0
    return c.finding(
        "bloat.prompt", "warn", f"{c.label}: prompt grew to {_fmt_n(peak)} tokens (started at {_fmt_n(first)})",
        detail="Every call re-sends the whole context; a long run pays for its own history again on each step.",
        evidence={"first": first, "peak": peak, "calls": len(rows)})


def _compression_churn(c: _RunCtx) -> int:
    n = c.q("SELECT COUNT(*) n FROM events WHERE kind='compression.start' AND session_id IN ({M})")[0]["n"]
    if n < 3:
        return 0
    return c.finding("compression.churn", "warn", f"{c.label}: context compressed {n}× in one run",
                     detail="Repeated compression usually means a loop is filling the context faster than "
                            "the task needs it.", evidence={"compressions": n})


def _api_errors(c: _RunCtx) -> int:
    rows = c.q("SELECT detail FROM events WHERE kind='api.error' AND session_id IN ({M})")
    rows += c.q("SELECT error_type AS detail FROM calls WHERE status='error' AND session_id IN ({M})")
    if len(rows) < 5:
        return 0
    kinds = Counter()
    for r in rows:
        try:
            kinds[json.loads(r["detail"]).get("error_type", "?")] += 1
        except Exception:
            kinds[str(r["detail"])] += 1
    return c.finding("api.errors", "warn", f"{c.label}: {len(rows)} failed API calls",
                     evidence={"errors": len(rows), "types": dict(kinds.most_common(5))})


# ── Detectors across runs ─────────────────────────────────────────────


def _served_fallback(conn, since, now, jobs) -> int:
    """A model group answered by a deployment that is not its usual one (silent fallback)."""
    n = 0
    usual = {r["model"]: r["served_model"] for r in conn.execute(
        "SELECT model, served_model, COUNT(*) k FROM calls WHERE served_model IS NOT NULL AND ended_at >= ? "
        "GROUP BY model, served_model ORDER BY k ASC", (now - 7 * 86400,))}
    for r in conn.execute(
            "SELECT c.model, c.served_model, COUNT(*) k, MIN(c.ended_at) first, MAX(c.ended_at) last, "
            "COUNT(DISTINCT c.session_id) sessions FROM calls c WHERE c.served_model IS NOT NULL AND c.ended_at >= ? "
            "GROUP BY c.model, c.served_model", (since,)):
        main = usual.get(r["model"])
        if not main or r["served_model"] == main:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(r["first"]))
        upsert_finding(conn, kind="model.fallback", severity="high",
                       fingerprint=f"model.fallback:{r['model']}:{r['served_model']}:{day}",
                       title=f"{r['model']} was answered by {r['served_model']} {r['k']}× (usually {main})",
                       detail="LiteLLM fell back to another deployment; the caller asked for one model and got another.",
                       evidence={"calls": r["k"], "sessions": r["sessions"], "usual": main,
                                 "first": r["first"], "last": r["last"]},
                       model=r["model"], at=r["last"])
        n += 1
    return n


def _cron_overlap(conn, since, now, jobs) -> int:
    n = 0
    for r in conn.execute("SELECT job_id, COUNT(*) k, MIN(at) first, MAX(at) last FROM events "
                          "WHERE kind='cron.overlap' AND at >= ? AND job_id IS NOT NULL GROUP BY job_id HAVING k >= 3",
                          (since,)):
        name = jobs.get(r["job_id"], r["job_id"])
        day = time.strftime("%Y-%m-%d", time.gmtime(r["first"]))
        upsert_finding(conn, kind="cron.overlap", severity="warn", fingerprint=f"cron.overlap:{r['job_id']}:{day}",
                       title=f"{name}: {r['k']} fires skipped because the previous run was still going",
                       detail="A run that outlives its own schedule is usually stuck — this is how a loop looks from "
                              "the scheduler.",
                       evidence={"skipped": r["k"], "first": r["first"], "last": r["last"]},
                       job_id=r["job_id"], at=r["last"])
        n += 1
    return n


def _settle_waste(conn, since, now, jobs) -> int:
    """Cron runs that only said 'nothing to do' but read a big prompt to say it."""
    n = 0
    rows = conn.execute(
        "SELECT s.job_id, COUNT(*) runs, SUM(s.input_tokens) tok FROM sessions s WHERE s.source='cron' "
        "AND s.job_id IS NOT NULL AND s.started_at >= ? AND s.api_calls = 1 AND s.tool_calls = 0 "
        "GROUP BY s.job_id HAVING tok >= 1000000", (since,)).fetchall()
    for r in rows:
        name = jobs.get(r["job_id"], r["job_id"])
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        upsert_finding(conn, kind="waste.settle", severity="info", fingerprint=f"waste.settle:{r['job_id']}:{day}",
                       title=f"{name}: {r['runs']} one-call runs that did nothing used {_fmt_n(r['tok'])} prompt tokens",
                       detail="Runs with one API call and no tool call are almost always 'nothing to do'. A monitor "
                              "that stays silent in that state saves the whole prompt.",
                       evidence={"runs": r["runs"], "input_tokens": r["tok"]}, job_id=r["job_id"])
        n += 1
    return n


def _model_hog(conn, since, now, jobs) -> int:
    n = 0
    rows = conn.execute(
        "SELECT CAST(c.ended_at/3600 AS INT) hr, c.model, s.root_id, COUNT(*) k, "
        "(SELECT COUNT(*) FROM calls c2 WHERE c2.model=c.model AND CAST(c2.ended_at/3600 AS INT)=CAST(c.ended_at/3600 AS INT)) + "
        "(SELECT COUNT(*) FROM ext_calls e WHERE e.model=c.model AND CAST(e.ended_at/3600 AS INT)=CAST(c.ended_at/3600 AS INT)) total "
        "FROM calls c JOIN sessions s ON s.id=c.session_id WHERE c.ended_at >= ? "
        "GROUP BY hr, c.model, s.root_id HAVING k >= 60", (since,)).fetchall()
    for r in rows:
        if r["k"] < 0.5 * r["total"]:
            continue
        run = conn.execute("SELECT job_id, source, profile, title FROM sessions WHERE id=?", (r["root_id"],)).fetchone()
        label = jobs.get(run["job_id"]) if run and run["job_id"] else (r["root_id"])
        upsert_finding(conn, kind="model.hog", severity="warn", fingerprint=f"model.hog:{r['model']}:{r['root_id']}",
                       title=f"{label}: {r['k']} of {r['total']} {r['model']} calls in one hour",
                       detail="One run took most of this model's capacity; everything else on it queued behind.",
                       evidence={"hour_utc": time.strftime("%Y-%m-%d %H:00", time.gmtime(r["hr"] * 3600)),
                                 "calls": r["k"], "model_total": r["total"]},
                       session_id=r["root_id"], model=r["model"], at=r["hr"] * 3600 + 3599)
        n += 1
    return n


def _kanban_failures(conn, since, now, jobs) -> int:
    n = 0
    for r in conn.execute("SELECT * FROM kanban_runs WHERE COALESCE(ended_at, started_at) >= ? AND outcome IN "
                          "('crashed','timed_out','gave_up','protocol_violation','spawn_failed')", (since,)):
        upsert_finding(conn, kind="kanban.failed", severity="warn",
                       fingerprint=f"kanban.failed:{r['board']}:{r['run_id']}",
                       title=f"kanban {r['task_id']} ({r['profile']}): run {r['outcome']}",
                       detail=(r["title"] or "")[:200], evidence={"board": r["board"], "error": r["error"]},
                       session_id=r["session_id"], at=r["ended_at"] or r["started_at"])
        n += 1
    return n


def aux_fingerprint(profile: str, task: str, sig: str) -> str:
    # No date: one finding per problem, however many days it keeps failing.
    return f"aux.failed:{profile}:{task}:{sig}"


def _aux_failures(conn, since, now, jobs) -> int:
    """Auxiliary tasks that keep failing: the same (profile, task, error signature) ≥3× in 24 h.

    Fixed window, independent of `since`: the threshold is "per day", and a watch tick
    with a 3 h window must still see the day. A resolved finding stays resolved until
    the problem shows up again after it was last seen; then it reopens as a new episode
    (first_seen moves, notified_at clears) so it is reported once more.
    """
    groups: dict[tuple, dict] = {}
    for r in conn.execute("SELECT at, session_id, detail FROM events WHERE kind='aux.failed' AND at >= ? AND at <= ? "
                          "ORDER BY at", (now - AUX_WINDOW_S, now)):
        try:
            d = json.loads(r["detail"] or "{}")
        except Exception:
            continue
        key = (d.get("profile") or "?", d.get("task") or "?", d.get("sig") or "?")
        g = groups.setdefault(key, {"ats": [], "lines": [], "files": set(), "sessions": [], "loggers": set()})
        g["ats"].append(r["at"])
        if d.get("line"):
            g["lines"].append(d["line"])
        if d.get("file"):
            g["files"].add(d["file"])
        if d.get("logger"):
            g["loggers"].add(d["logger"])
        if r["session_id"] and r["session_id"] not in g["sessions"]:
            g["sessions"].append(r["session_id"])
    n = 0
    for (profile, task, sig), g in groups.items():
        threshold = AUX_THRESHOLD_BY_TASK.get(task, AUX_THRESHOLD)
        count = len(g["ats"])
        if count < threshold:
            continue
        fp = aux_fingerprint(profile, task, sig)
        row = conn.execute("SELECT id, state, last_seen FROM findings WHERE fingerprint=?", (fp,)).fetchone()
        first, last = g["ats"][0], g["ats"][-1]
        recurred = False
        if row is not None and row["state"] == "resolved":
            if last <= (row["last_seen"] or 0) + 1:
                continue  # resolved, and nothing new since
            recurred = True
            first = min(a for a in g["ats"] if a > (row["last_seen"] or 0) + 1)
        what = "PAID OpenRouter lane engaged" if task == "paid_lane" else f"{task} failed"
        upsert_finding(
            conn, kind="aux.failed", severity="high" if task == "paid_lane" else "warn", fingerprint=fp,
            title=f"{profile}: {what} {count}× in 24 h — {sig[:90]}",
            detail="An auxiliary task failed in the background; the user's turn still answered, so nothing else "
                   "shows it.",
            evidence={"profile": profile, "task": task, "signature": sig, "count_24h": count, "threshold": threshold,
                      "first": first, "last": last, "samples": g["lines"][-AUX_SAMPLES:],
                      "files": sorted(g["files"]), "loggers": sorted(g["loggers"]), "sessions": g["sessions"][-5:]},
            at=last)
        if row is None:
            conn.execute("UPDATE findings SET first_seen=? WHERE fingerprint=?", (first, fp))
        elif recurred:
            conn.execute("UPDATE findings SET first_seen=?, notified_at=NULL WHERE fingerprint=?", (first, fp))
        n += 1
    return n
