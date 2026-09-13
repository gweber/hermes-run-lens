"""Core tests: fingerprints, parsing, detectors, the live recorder and the stop path.

    ~/.hermes/hermes-agent/venv/bin/python -m pytest -q tests/

Everything runs against a temporary database and a temporary Hermes home; nothing
touches ~/.hermes.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hermes-plugin"))
_HERMES = os.environ.get("HERMES_AGENT_DIR") or str(Path.home() / ".hermes" / "hermes-agent")
if Path(_HERMES).is_dir():  # optional: with Hermes importable, its redactor and failure detector are used
    sys.path.insert(0, _HERMES)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "logs").mkdir(parents=True)
    (home / "config.yaml").write_text("model: {}\n")
    monkeypatch.setenv("RUN_LENS_DB", str(tmp_path / "lens.db"))
    monkeypatch.setenv("RUN_LENS_HERMES_ROOT", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    from run_lens import paths, settings, store
    store._initialized.clear()
    paths._homes_cache["value"] = None
    settings.load(refresh=True)
    conn = store.connect()
    return {"home": home, "conn": conn, "tmp": tmp_path}


# ── textutil ──────────────────────────────────────────────────────────


def test_fingerprints_exact_and_shape():
    from run_lens.textutil import fingerprints
    a = fingerprints("terminal", {"command": "python3 tools/queue.py write --channel operations-inbox \"one\""})
    b = fingerprints("terminal", '{"command": "python3 tools/queue.py write --channel operations-inbox \\"two\\""}')
    c = fingerprints("terminal", {"command": "git status --short && git log --oneline | head -20 and more"})
    assert a[0] != b[0] and a[1] == b[1] and a[1] != c[1]
    # argument order does not matter
    assert fingerprints("x", {"a": 1, "b": 2})[0] == fingerprints("x", '{"b": 2, "a": 1}')[0]


def test_failure_detection_sees_through_guardrail_suffix():
    from run_lens.textutil import tool_failed
    stored = ('{"output": "entry line indices: []\\nIndexError", "exit_code": 1, "error": null}\n\n'
              '[Tool loop warning: repeated_exact_failure_warning; count=34; terminal has failed 34 times]')
    assert tool_failed("terminal", stored)[0] is True
    assert tool_failed("terminal", '{"output": "fine", "exit_code": 0}')[0] is False


def test_redaction():
    from run_lens.textutil import preview
    p = preview("curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123' api_key=supersecret123")
    assert "abcdefghijklmnopqrstuvwxyz0123" not in p and "supersecret123" not in p


# ── store ─────────────────────────────────────────────────────────────


def test_upsert_keep_fills_gaps_only(env):
    from run_lens.store import Tx, upsert
    c = env["conn"]
    with Tx(c):
        upsert(c, "tools", {"tool_call_id": "t1", "name": "terminal", "status": "error", "origin": "hook"}, ("tool_call_id",))
        upsert(c, "tools", {"tool_call_id": "t1", "name": "terminal", "status": "ok", "args_preview": "ls",
                            "origin": "statedb"}, ("tool_call_id",), keep=("name", "status", "args_preview", "origin"))
    r = c.execute("SELECT status, args_preview, origin FROM tools").fetchone()
    assert (r["status"], r["args_preview"], r["origin"]) == ("error", "ls", "hook")


# ── agent.log ─────────────────────────────────────────────────────────

LOG = """2026-09-12 23:30:10,641 INFO [cron_ed190f76386e_20260912_204110] agent.conversation_loop: API call #351: model=big provider=custom in=160679 out=223 total=160902 latency=49.0s
2026-09-12 23:30:10,760 WARNING [cron_ed190f76386e_20260912_204110] agent.tool_executor: Tool terminal returned error (0.10s): {"output": "x
Traceback (most recent call last):
IndexError", "exit_code": 1}
2026-09-12 23:31:43,038 INFO cron.scheduler: Job 'nightly-report' already running — skipping
2026-09-12 23:41:23,778 INFO [cron_ed190f76386e_20260912_204110] agent.conversation_loop: Turn ended: reason=max_iterations_reached(500/500) model=big api_calls=500/500 budget=500/500 tool_turns=339 last_msg_role=assistant response_len=8 session=cron_ed190f76386e_20260912_204110
2026-09-12 23:42:00,000 INFO [x] agent.conversation_compression: context compression started: session=cron_ed190f76386e_20260912_204110 messages=837 tokens=~192,082 model=big focus=None
"""


def test_agentlog_parsing_and_offsets(env):
    from run_lens.ingest import agentlog
    from run_lens.store import Tx
    c = env["conn"]
    with Tx(c):
        c.execute("INSERT INTO jobs(id, name) VALUES('ed190f76386e', 'nightly-report')")
    log = env["home"] / "logs" / "agent.log"
    log.write_text(LOG + "2026-09-12 23:50:00,000 INFO [s] agent.conversation_loop: API call #1: model=big provider=custom in=10 out=5 total=15 latency=1.0s")
    rep = agentlog.ingest(c)
    assert rep["calls"] == 1 and rep["turns"] == 1  # the unterminated last line waits
    call = c.execute("SELECT * FROM calls").fetchone()
    assert (call["seq"], call["input_tokens"], call["output_tokens"], call["latency_s"]) == (351, 160679, 223, 49.0)
    turn = c.execute("SELECT * FROM turns").fetchone()
    assert turn["exit_reason"] == "max_iterations_reached(500/500)" and turn["max_iterations"] == 500
    kinds = {r["kind"]: r["job_id"] for r in c.execute("SELECT kind, job_id FROM events")}
    assert kinds["cron.overlap"] == "ed190f76386e" and "compression.start" in kinds
    with open(log, "a") as f:
        f.write("\n")
    rep2 = agentlog.ingest(c)
    assert rep2["calls"] == 1  # only the now-complete line
    assert c.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 2


# ── detectors ─────────────────────────────────────────────────────────


def _loop_run(c, sid="cron_aaaaaaaaaaaa_20260912_204110", n=60, job="aaaaaaaaaaaa"):
    from run_lens.store import Tx
    t0 = time.time() - 4000
    with Tx(c):
        c.execute("INSERT INTO jobs(id, name) VALUES(?, 'loopy')", (job,))
        c.execute("INSERT INTO sessions(id, root_id, profile, source, job_id, started_at, last_activity_at, ended_at, "
                  "api_calls, tool_calls, input_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, sid, "research", "cron", job, t0, t0 + n * 20, t0 + n * 20, n, n, n * 100_000))
        for i in range(n):
            c.execute("INSERT INTO calls(session_id, seq, model, started_at, ended_at, input_tokens, output_tokens, "
                      "status, origin) VALUES(?,?,?,?,?,?,?,?,?)",
                      (sid, i + 1, "big", t0 + i * 20, t0 + i * 20 + 10, 40_000 + i * 2000, 117, "ok", "log"))
            c.execute("INSERT INTO tools(tool_call_id, session_id, name, fingerprint, shape, status, started_at, ended_at) "
                      "VALUES(?,?,?,?,?,?,?,?)", (f"t{i}", sid, "terminal", "fp", "sh", "error", t0 + i * 20, t0 + i * 20 + 1))
        c.execute("INSERT INTO turns(id, session_id, ended_at, exit_reason, api_calls, max_iterations) VALUES(?,?,?,?,?,?)",
                  (sid + ":t", sid, t0 + n * 20, f"max_iterations_reached({n}/{n})", n, n))
        # a baseline of normal runs for the same job
        for k in range(8):
            nid = f"cron_{job}_2026091{k}_000000"
            c.execute("INSERT INTO sessions(id, root_id, profile, source, job_id, started_at, ended_at, api_calls, "
                      "tool_calls, input_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (nid, nid, "research", "cron", job, t0 - 86400 * (k + 1), t0 - 86400 * (k + 1) + 60, 4 + k % 3, 3,
                       120_000))
    return sid


def test_detectors_fire_on_a_capped_loop(env):
    from run_lens import detect
    c = env["conn"]
    sid = _loop_run(c)
    detect.run_all(c, since=time.time() - 86400)
    kinds = {r["kind"]: r["severity"] for r in c.execute("SELECT kind, severity FROM findings WHERE session_id=?", (sid,))}
    for k in ("cap.max_iterations", "loop.exact_failure", "loop.identical_output", "runaway.calls", "bloat.prompt"):
        assert k in kinds, (k, kinds)
    # re-running updates instead of duplicating
    before = c.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    detect.run_all(c, since=time.time() - 86400)
    assert c.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == before
    sug = c.execute("SELECT suggestion FROM findings WHERE kind='cap.max_iterations'").fetchone()[0]
    assert sug and "cap near" in sug


def test_baseline_excludes_flagged_runs(env):
    from run_lens import baseline, detect
    c = env["conn"]
    _loop_run(c)
    detect.run_all(c, since=time.time() - 86400)
    b = baseline.compute(c)["job:aaaaaaaaaaaa"]
    assert b["calls"]["max"] <= 6  # the 60-call loop is not "normal"


# ── recorder, breaker, stop ───────────────────────────────────────────


class AIAgent:  # the stop path finds agents by class name
    def __init__(self, sid):
        self.session_id = sid
        self.interrupted = None

    def interrupt(self, message=None, *, hard_cancel=False):
        self.interrupted = (message, hard_cancel)


def test_recorder_end_to_end_with_breaker(env):
    from run_lens import control
    from run_lens.recorder import Recorder
    rec = Recorder({"breaker": "cron", "breaker_exact_failures": 6})
    sid = "cron_bbbbbbbbbbbb_20260913_010000"
    agent = AIAgent(sid)
    for i in range(8):
        rid = f"{sid}:task:turn1:api:{i + 1}"
        rec.pre_api_request(session_id=sid, api_request_id=rid, platform="cron", model="big", started_at=time.time())
        rec.post_api_request(session_id=sid, api_request_id=rid, platform="cron", model="big", api_call_count=i + 1,
                             api_duration=1.5, usage={"input_tokens": 1000 + i, "output_tokens": 117},
                             finish_reason="tool_calls", assistant_tool_call_count=1)
        blocked = rec.pre_tool_call(session_id=sid, tool_call_id=f"c{i}", tool_name="terminal", args={"command": "boom"})
        if i >= 6:
            assert blocked and blocked["action"] == "block" and "stopped" in blocked["message"]
        rec.post_tool_call(session_id=sid, tool_call_id=f"c{i}", tool_name="terminal", args={"command": "boom"},
                           result='{"output": "no", "exit_code": 1}', status="ok", duration_ms=12)
    rec.on_session_end(session_id=sid, turn_id="turn1", turn_exit_reason="max_iterations_reached(8/8)", platform="cron")
    rec._flush()
    c = env["conn"]
    assert c.execute("SELECT COUNT(*) FROM calls WHERE session_id=?", (sid,)).fetchone()[0] == 8
    assert c.execute("SELECT COUNT(*) FROM tools WHERE session_id=? AND status='error'", (sid,)).fetchone()[0] == 8
    kinds = {r["kind"] for r in c.execute("SELECT kind FROM findings WHERE session_id=?", (sid,))}
    assert {"loop.exact_failure", "breaker.exact_failure", "cap.max_iterations"} <= kinds
    stop = c.execute("SELECT * FROM stops WHERE session_id=?", (sid,)).fetchone()
    assert stop and stop["requested_by"] == "breaker" and stop["applied_at"]
    assert agent.interrupted and agent.interrupted[1] is True
    s = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert s["job_id"] == "bbbbbbbbbbbb" and s["source"] == "cron"
    control._stops.clear()
    control._applied.clear()


def test_breaker_off_for_chat_sessions(env):
    from run_lens import control
    from run_lens.recorder import Recorder
    rec = Recorder({"breaker": "cron", "breaker_exact_failures": 3})
    sid = "20260913_010000_abcdef"
    rec.on_session_start(session_id=sid, platform="telegram", model="big")
    for i in range(5):
        rec.post_tool_call(session_id=sid, tool_call_id=f"x{i}", tool_name="terminal", args={"command": "boom"},
                           result='{"exit_code": 2}', status="ok")
    assert control.is_stopped(sid) is None
    rec._flush()
    assert env["conn"].execute("SELECT COUNT(*) FROM stops").fetchone()[0] == 0


def test_cli_stop_reaches_the_owning_process(env):
    from run_lens import control
    from run_lens.recorder import Recorder
    from run_lens.store import Tx
    c = env["conn"]
    sid = "cron_cccccccccccc_20260913_020000"
    agent = AIAgent(sid)
    with Tx(c):
        control.request_stop(c, [sid], by="cli", reason="test")
    rec = Recorder()
    control._last_poll = 0
    rec._flush()  # the writer thread's poll
    rec.pre_api_request(session_id=sid, api_request_id="r1", platform="cron")
    assert agent.interrupted is not None
    control._stops.clear()
    control._applied.clear()


def test_llm_request_tags_only_litellm(env, monkeypatch):
    from run_lens.ingest import litellm
    from run_lens.recorder import Recorder
    monkeypatch.setitem(litellm._detected, "urls", ["http://localhost:4000"])
    rec = Recorder()
    req = {"model": "big", "messages": [], "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    out = rec.llm_request(request=req, base_url="http://localhost:4000/v1", session_id="s1", api_request_id="s1:api:1",
                          api_mode="chat_completions", platform="cli")
    body = out["request"]["extra_body"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["metadata"]["spend_logs_metadata"]["api_request_id"] == "s1:api:1"
    assert rec.llm_request(request=req, base_url="https://api.openai.com/v1", session_id="s1") is None
    assert "metadata" not in req["extra_body"]  # the caller's dict is not mutated


def test_query_views_render_on_synthetic_data(env):
    from run_lens import detect, query
    c = env["conn"]
    sid = _loop_run(c)
    detect.run_all(c, since=time.time() - 86400)
    o = query.overview(c, time.time() - 3 * 86400)
    assert o["calls"] == 60 and o["runs"] >= 1
    d = query.run_detail(c, query.resolve_run(c, "loopy"))
    assert d["run"]["label"] == "loopy" and d["repeats"][0]["count"] == 60
    g = {x["group"]: x for x in query.groups(c, time.time() - 30 * 86400)}
    assert g["job:aaaaaaaaaaaa"]["suggested_cap"] is not None
    from run_lens import export
    spans = export.otlp(c, time.time() - 3 * 86400)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert any(s["name"].startswith("chat ") for s in spans) and any(s["name"].startswith("execute_tool") for s in spans)


def test_tagging_off_and_auto_without_proxy(env, monkeypatch):
    from run_lens.ingest import litellm
    from run_lens.recorder import Recorder
    monkeypatch.setitem(litellm._detected, "urls", [])
    req = {"model": "big", "messages": []}
    kw = dict(base_url="http://localhost:4000/v1", session_id="s", api_request_id="s:api:1", api_mode="chat_completions")
    assert Recorder().llm_request(request=req, **kw) is None                      # auto, nothing detected
    assert Recorder({"tag_litellm": "on"}).llm_request(request=req, **kw)          # forced on
    assert Recorder({"tag_litellm": "off"}).llm_request(request=req, **kw) is None


def test_settings_from_config(env):
    from run_lens import settings
    (env["home"] / "config.yaml").write_text(
        "plugins:\n  entries:\n    run-lens:\n      settings:\n        breaker: all\n        retention_days: 7\n")
    s = settings.load(refresh=True)
    assert s["breaker"] == "all" and s["retention_days"] == 7 and s["tag_litellm"] == "auto"
    settings.load(refresh=True)


def test_litellm_source_off_without_configuration(env, monkeypatch):
    from run_lens import settings
    from run_lens.ingest import litellm
    settings.load(refresh=True)
    monkeypatch.setitem(litellm._detected, "urls", [])
    monkeypatch.setitem(litellm._detected, "at", time.time())
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    monkeypatch.delenv("LITELLM_DATABASE_URL", raising=False)
    assert litellm.source() == "off"
    assert litellm.ingest(env["conn"])["source"] == "off"


def test_demo_store_produces_findings(tmp_path):
    from run_lens import demo, query
    from run_lens.store import connect
    db = tmp_path / "demo.db"
    assert demo.build(str(db)) > 50
    c = connect(db)
    kinds = {r["kind"] for r in c.execute("SELECT kind FROM findings")}
    assert {"cap.max_iterations", "loop.exact_failure", "runaway.active", "model.fallback"} <= kinds, kinds
    assert query.overview(c, time.time() - 3 * 86400)["external_calls"] == 900


def test_retention_prunes_detail_only(env):
    from run_lens import ingest, settings
    from run_lens.store import Tx
    c = env["conn"]
    old = time.time() - 200 * 86400
    with Tx(c):
        c.execute("INSERT INTO sessions(id, root_id, started_at) VALUES('s','s',?)", (old,))
        c.execute("INSERT INTO calls(session_id, ended_at) VALUES('s', ?)", (old,))
    settings.load(refresh=True)
    ingest.prune(c)
    assert c.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
