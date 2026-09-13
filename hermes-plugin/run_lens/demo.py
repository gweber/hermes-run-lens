"""A synthetic store: try `hermes lens` and the Runs tab without real data.

Invented jobs, profiles and calls over the last three days — normal runs with their
natural spread, and the failures run-lens exists for: a cron run retrying one
failing command until its cap, a kanban worker far past its normal size, a model
group silently answered by its fallback, and other clients sharing the GPU.
Nothing in it comes from a real Hermes install.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from . import detect
from .store import Tx, connect, upsert

JOBS = [  # id, name, profile, schedule, model, (calls lo, hi), tokens per call
    ("a1b2c3d4e5f6", "daily-digest", "default", "0 7 * * *", "big", (6, 14), 18_000),
    ("b2c3d4e5f6a1", "inbox-triage", "default", "every 30m", "small", (2, 5), 9_000),
    ("c3d4e5f6a1b2", "site-monitor", "ops", "every 15m", "small", (1, 3), 6_000),
    ("d4e5f6a1b2c3", "weekly-report", "research", "0 9 * * 1", "big", (20, 40), 40_000),
    ("e5f6a1b2c3d4", "backup-check", "ops", "0 3 * * *", "small", (3, 6), 5_000),
]
TOOLS = [
    ("terminal", {"command": "git -C ~/projects/site status --short"}),
    ("read_file", {"path": "~/notes/inbox.md"}),
    ("web_search", {"query": "release notes"}),
    ("terminal", {"command": "curl -s -o /dev/null -w '%{http_code}' https://example.org"}),
    ("write_file", {"path": "~/notes/digest.md"}),
]


def build(path: str, seed: int = 7) -> int:
    rnd = random.Random(seed)
    p = Path(path)
    for f in (p, Path(f"{p}-wal"), Path(f"{p}-shm")):
        if f.exists():
            f.unlink()
    conn = connect(p)
    now = time.time()
    runs = 0
    with Tx(conn):
        for jid, name, profile, sched, model, _r, _t in JOBS:
            upsert(conn, "jobs", {"id": jid, "profile": profile, "name": name, "schedule": sched, "model": model,
                                  "enabled": 1, "state": "scheduled", "no_agent": 0, "updated_at": now}, ("id",))
        # normal cron runs
        for jid, name, profile, _s, model, (lo, hi), tok in JOBS:
            every = {"inbox-triage": 1800, "site-monitor": 900}.get(name, 6 * 3600)
            t = now - 3 * 86400
            while t < now - 600:
                t += every * rnd.uniform(0.9, 1.1)
                _run(conn, rnd, f"cron_{jid}_{time.strftime('%Y%m%d_%H%M%S', time.gmtime(t))}", "cron", profile,
                     model, jid, t, rnd.randint(lo, hi), tok)
                runs += 1
        # chats and kanban workers
        for i in range(40):
            t = now - rnd.uniform(0, 3 * 86400)
            src, prof = rnd.choice([("telegram", "default"), ("cli", "default"), ("kanban", "writer"),
                                    ("kanban", "research"), ("tui", "default")])
            _run(conn, rnd, f"demo_{i:03d}", src, prof, rnd.choice(["big", "big-thinking"]), None, t,
                 rnd.randint(4, 30), rnd.randint(12_000, 45_000))
            runs += 1
        # the loop: a cron run retrying one refused command until the cap
        t = now - 5 * 3600
        _run(conn, rnd, f"cron_{JOBS[1][0]}_loop", "cron", "default", "small", JOBS[1][0], t, 150, 30_000,
             loop=True, cap=150)
        # the runaway: a kanban worker at 8x its normal size, still going
        _run(conn, rnd, "demo_runaway", "kanban", "research", "big", None, now - 2.5 * 3600, 160, 60_000,
             active=True, task="t_1a2b3c4d")
        # silent fallback: one digest run answered by the small deployment
        _run(conn, rnd, f"cron_{JOBS[0][0]}_fallback", "cron", "default", "big", JOBS[0][0], now - 20 * 3600, 9, 18_000)
        conn.execute("UPDATE calls SET served_model='openai/small-8b' WHERE session_id=?", (f"cron_{JOBS[0][0]}_fallback",))
        # other clients on the same proxy
        for i in range(900):
            t = now - rnd.uniform(0, 3 * 86400)
            upsert(conn, "ext_calls", {"litellm_id": f"ext-{i}", "model": rnd.choice(["big", "small", "embedding"]),
                                       "served_model": "openai/big-30b", "client": rnd.choice(["opencode/1.2", "curl/8.5", "python-requests/2.32"]),
                                       "started_at": t - 2, "ended_at": t, "latency_s": rnd.uniform(0.2, 9),
                                       "ttft_s": rnd.uniform(0.1, 3), "input_tokens": rnd.randint(200, 9000),
                                       "output_tokens": rnd.randint(10, 600), "status": "success"}, ("litellm_id",))
    detect.run_all(conn, since=now - 3 * 86400)
    return runs


def _run(conn, rnd, sid, source, profile, model, job, t0, calls, tok, loop=False, cap=None, active=False, task=None):
    # Spread the run over the time it has had, so no call ends in the future.
    budget = max(60.0, time.time() - 60 - t0)
    pace = min(1.0, budget / max(1.0, calls * 17.0))
    t = t0
    served = {"big": "openai/big-30b", "big-thinking": "openai/big-30b", "small": "openai/small-8b"}[model]
    tools = 0
    for i in range(calls):
        tin = int(tok * (1 + i * (0.05 if loop else 0.08)) * rnd.uniform(0.95, 1.05))
        tout = 117 if loop and i > 3 else rnd.randint(40, 700)
        lat = rnd.uniform(1.5, 25) * (1.8 if model == "big-thinking" else 1) * pace
        conn.execute(
            "INSERT INTO calls(session_id, seq, profile, platform, model, served_model, started_at, ended_at, latency_s, "
            "ttft_s, input_tokens, output_tokens, finish_reason, status, origin, api_request_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, i + 1, profile, source, model, served, t, t + lat, lat, lat * rnd.uniform(0.1, 0.6), tin, tout,
             "tool_calls" if i < calls - 1 else "stop", "ok", "hook", f"{sid}:api:{i + 1}"))
        t += lat
        if i < calls - 1:
            if loop and i > 3:
                name, args = "terminal", {"command": "python3 tools/queue.py claim --id 42"}
                failed, result = True, {"output": "refused: item 42 is already claimed", "exit_code": 1}
            else:
                name, args = rnd.choice(TOOLS)
                failed = rnd.random() < 0.04
                result = {"output": "error: not found" if failed else "ok", "exit_code": 1 if failed else 0}
            from .textutil import fingerprints
            fp, shape = fingerprints(name, args)
            dur = rnd.uniform(0.05, 4) * pace
            conn.execute(
                "INSERT INTO tools(tool_call_id, session_id, name, fingerprint, shape, args_preview, status, result_preview, "
                "result_chars, duration_ms, started_at, ended_at, origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{sid}:tool:{i}", sid, name, fp, shape, json.dumps(args), "error" if failed else "ok",
                 json.dumps(result), 60, dur * 1000, t, t + dur, "hook"))
            t += dur
            tools += 1
    shift = max(0.0, t - (time.time() - 30))  # a run that would end in the future starts earlier instead
    if shift:
        conn.execute("UPDATE calls SET started_at=started_at-?, ended_at=ended_at-? WHERE session_id=?", (shift, shift, sid))
        conn.execute("UPDATE tools SET started_at=started_at-?, ended_at=ended_at-? WHERE session_id=?", (shift, shift, sid))
        t0, t = t0 - shift, t - shift
    ended = None if active else t
    conn.execute(
        "INSERT INTO sessions(id, root_id, profile, source, job_id, task_id, title, model, started_at, ended_at, "
        "last_activity_at, end_reason, api_calls, tool_calls, input_tokens, output_tokens, origin, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, sid, profile, source, job, task, f"Work kanban task {task}" if task else None, model, t0, ended,
         time.time() - 30 if active else t, None if active else ("cron_complete" if source == "cron" else "cli_close"),
         calls, tools, sum(r[0] for r in conn.execute("SELECT input_tokens FROM calls WHERE session_id=?", (sid,))),
         sum(r[0] for r in conn.execute("SELECT output_tokens FROM calls WHERE session_id=?", (sid,))), "hook", t))
    reason = f"max_iterations_reached({cap}/{cap})" if cap else "text_response(finish_reason=stop)"
    if not active:
        conn.execute("INSERT INTO turns(id, session_id, ended_at, exit_reason, api_calls, max_iterations, origin) "
                     "VALUES(?,?,?,?,?,?,?)", (f"{sid}:turn", sid, t, reason, calls, cap or 90, "hook"))
