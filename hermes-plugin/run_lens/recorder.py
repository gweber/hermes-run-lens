"""Live capture: every LLM call, tool call, turn and session, as it happens.

The hooks run on the agent's own thread, inline, in every process that loaded the
plugin — the gateway (all multiplexed profiles, cron included), CLI and TUI chats,
kanban workers. So a hook here does almost nothing: it copies a handful of scalar
fields into an in-memory queue and returns. One daemon thread per process drains the
queue into SQLite about once a second in a single short transaction, and flushes at
exit. If the database is locked or broken, rows are dropped with a warning; an agent
is never slowed or failed by its observer (fail-open).

Alongside, a small per-session state machine watches the stream for the two loop
signatures that burned 2026-09-12 — the same failing tool call again and again, and
the same completion size again and again — and for a run far past its group's normal
call count. It records a finding the moment a threshold is crossed, and, if the
breaker is armed for that kind of session, stops the run (see control.py).
"""
from __future__ import annotations

import atexit
import collections
import json
import logging
import re
import threading
import time
from typing import Any

from . import baseline as bl
from . import control, paths, textutil
from .detect import upsert_finding
from .store import Tx, connect, upsert

logger = logging.getLogger("run_lens")

CRON_ID = re.compile(r"^cron_([0-9a-f]{12})_\d{8}_\d{6}$")
FLUSH_S = 1.0
MAX_QUEUE = 50_000
BASELINE_TTL_S = 600

from .settings import DEFAULTS  # noqa: E402  (declared in plugin.yaml config_schema)


class _Live:
    __slots__ = ("calls", "first", "last_out", "streak", "fail_counts", "flagged", "job", "profile", "source")

    def __init__(self):
        self.calls = 0
        self.first = time.time()
        self.last_out = None
        self.streak = 0
        self.fail_counts: collections.Counter = collections.Counter()
        self.flagged: set[str] = set()
        self.job = None
        self.profile = None
        self.source = None


class Recorder:
    def __init__(self, settings: dict | None = None):
        self.settings = {**DEFAULTS, **(settings or {})}
        self._q: collections.deque = collections.deque(maxlen=MAX_QUEUE)
        self._cv = threading.Condition()
        self._pending_calls: dict[str, dict] = {}
        self._tool_starts: dict[str, float] = {}
        self._live: dict[str, _Live] = {}
        self._live_lock = threading.Lock()
        self._baselines: dict = {}
        self._baselines_at = 0.0
        self._thread: threading.Thread | None = None
        self._stopping = False
        self.dropped = 0
        self.written = 0

    # ── plumbing ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="run-lens-writer", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def close(self) -> None:
        self._stopping = True
        with self._cv:
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            self._flush()
        except Exception:
            pass

    def _put(self, op: str, payload: Any) -> None:
        if self._thread is None:
            self.start()  # lazily: a `hermes lens` CLI process never records, so never starts a writer
        if len(self._q) >= MAX_QUEUE:
            self.dropped += 1
        self._q.append((op, payload))

    def _run(self) -> None:
        while not self._stopping:
            with self._cv:
                self._cv.wait(timeout=FLUSH_S)
            try:
                self._flush()
            except Exception as exc:
                logger.warning("run-lens: write failed, %d rows dropped: %s", len(self._q), exc)
                self._q.clear()
                self._conn = None

    _conn = None
    _proxies_checked = False

    def _db(self):
        if self._conn is None:
            self._conn = connect()
        return self._conn

    def _flush(self) -> None:
        conn = self._db()
        if not self._proxies_checked and str(self.settings.get("tag_litellm")) == "auto":
            # Network probe for LiteLLM proxies — here on the writer thread, never on an
            # agent thread; until it has run, requests simply go out untagged.
            self._proxies_checked = True
            try:
                from .ingest import litellm as _ll
                _ll.proxies()
            except Exception as exc:
                logger.debug("run-lens: LiteLLM detection failed: %s", exc)
        if time.time() - self._baselines_at > BASELINE_TTL_S:
            self._baselines_at = time.time()
            try:
                self._baselines = bl.compute(conn)
            except Exception as exc:
                logger.debug("run-lens: baseline refresh failed: %s", exc)
        if self._q:
            items = []
            while self._q and len(items) < 5000:
                items.append(self._q.popleft())
            with Tx(conn):
                for op, p in items:
                    getattr(self, "_w_" + op)(conn, p)
            self.written += len(items)
        # After the writes: a breaker stop queued above must be in the table before the
        # in-memory stop list is reloaded from it.
        control.refresh(conn)

    # ── writers (writer thread only) ──────────────────────────────────

    def _w_session(self, conn, p):
        conn.execute(
            "INSERT INTO sessions(id, root_id, profile, source, job_id, model, started_at, last_activity_at, origin, "
            "updated_at) VALUES(:id, :id, :profile, :source, :job_id, :model, :at, :at, 'hook', :at) "
            "ON CONFLICT(id) DO UPDATE SET last_activity_at=MAX(COALESCE(sessions.last_activity_at, 0), :at), "
            "profile=COALESCE(sessions.profile, :profile), source=COALESCE(sessions.source, :source), "
            "model=COALESCE(sessions.model, :model), job_id=COALESCE(sessions.job_id, :job_id)", p)

    def _w_session_end(self, conn, p):
        conn.execute("UPDATE sessions SET ended_at=COALESCE(ended_at, :at), end_reason=COALESCE(end_reason, :reason) "
                     "WHERE id=:id", p)

    def _w_call(self, conn, p):
        upsert(conn, "calls", p, ("api_request_id",))

    def _w_tool(self, conn, p):
        upsert(conn, "tools", p, ("tool_call_id",))

    def _w_turn(self, conn, p):
        upsert(conn, "turns", p, ("id",))
        if p.get("exit_reason"):
            conn.execute("UPDATE sessions SET last_exit_reason=? WHERE id=?", (p["exit_reason"], p["session_id"]))

    def _w_event(self, conn, p):
        conn.execute("INSERT OR IGNORE INTO events(at, kind, session_id, job_id, detail, origin, dedupe) "
                     "VALUES(:at, :kind, :session_id, :job_id, :detail, 'hook', :dedupe)", p)

    def _w_finding(self, conn, p):
        upsert_finding(conn, **p)

    def _w_stop(self, conn, p):
        control.request_stop(conn, [p["session_id"]], p["by"], p["reason"])
        conn.execute("UPDATE stops SET applied_at=?, applied_by=? WHERE session_id=?",
                     (time.time(), p["by"], p["session_id"]))

    def _w_stop_applied(self, conn, p):
        conn.execute("UPDATE stops SET applied_at=COALESCE(applied_at, ?), applied_by=COALESCE(applied_by, ?) "
                     "WHERE session_id=?", (p["at"], p["by"], p["session_id"]))

    # ── helpers ───────────────────────────────────────────────────────

    def _profile(self) -> str:
        return paths.profile_name()

    def _touch(self, sid: str, platform: str | None, model: str | None, at: float) -> _Live:
        with self._live_lock:
            st = self._live.get(sid)
            if st is None:
                st = self._live[sid] = _Live()
                m = CRON_ID.match(sid or "")
                st.job = m.group(1) if m else None
                st.profile = self._profile()
                st.source = platform
                if len(self._live) > 2000:  # forget the oldest sessions
                    for k in list(self._live)[:500]:
                        self._live.pop(k, None)
        self._put("session", {"id": sid, "profile": st.profile, "source": platform, "job_id": st.job,
                              "model": model, "at": at})
        return st

    def _baseline(self, st: _Live):
        # Read-only on the agent thread; the writer thread refreshes it (see _flush).
        return self._baselines.get(bl.group_key(st.source, st.profile, st.job))

    def _armed(self, st: _Live) -> bool:
        mode = str(self.settings.get("breaker") or "off")
        if mode == "off":
            return False
        if mode == "all":
            return True
        kinds = set(mode.split("+"))
        return (st.source or "") in kinds

    def _live_finding(self, sid, st: _Live, kind, severity, title, evidence, trip: bool):
        key = f"{kind}:{sid}"
        if key in st.flagged:
            return
        st.flagged.add(key)
        self._put("finding", {"kind": kind, "severity": severity, "fingerprint": key, "title": title,
                              "detail": "Seen live by the run-lens recorder.", "evidence": evidence,
                              "session_id": sid, "job_id": st.job, "origin": "hook"})
        logger.warning("run-lens: %s — %s", kind, title)
        if trip and self._armed(st):
            reason = f"breaker: {kind}"
            control.local_stop(sid, reason)
            self._put("stop", {"session_id": sid, "by": "breaker", "reason": reason})
            control.apply(sid)

    # ── hooks ─────────────────────────────────────────────────────────

    def pre_api_request(self, **kw):
        sid = kw.get("session_id") or ""
        rid = kw.get("api_request_id")
        now = time.time()
        if sid:
            self._touch(sid, kw.get("platform"), kw.get("model"), now)
            if control.is_stopped(sid) and control.apply(sid):
                self._put("stop_applied", {"session_id": sid, "at": now, "by": "hook:pre_api_request"})
        if rid:
            self._pending_calls[rid] = {
                "started_at": kw.get("started_at") or now, "message_count": kw.get("message_count"),
                "tool_count": kw.get("tool_count"), "approx_in": kw.get("approx_input_tokens"),
            }
            if len(self._pending_calls) > 5000:
                for k in list(self._pending_calls)[:1000]:
                    self._pending_calls.pop(k, None)

    def post_api_request(self, **kw):
        sid = kw.get("session_id") or ""
        rid = kw.get("api_request_id")
        pend = self._pending_calls.pop(rid, {}) if rid else {}
        usage = kw.get("usage") or {}
        ended = kw.get("ended_at") or time.time()
        tin = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        tout = usage.get("output_tokens") or 0
        tool_calls = kw.get("assistant_tool_call_count")
        row = {
            "api_request_id": rid, "session_id": sid, "turn_id": kw.get("turn_id"), "seq": kw.get("api_call_count"),
            "retry": 0, "profile": self._profile(), "platform": kw.get("platform"), "model": kw.get("model"),
            "provider": kw.get("provider"), "base_url": kw.get("base_url"),
            "started_at": kw.get("started_at") or pend.get("started_at"), "ended_at": ended,
            "latency_s": kw.get("api_duration"), "input_tokens": tin, "output_tokens": tout,
            "cache_read_tokens": usage.get("cache_read_tokens"), "reasoning_tokens": usage.get("reasoning_tokens"),
            "message_count": kw.get("message_count") or pend.get("message_count"), "tool_count": pend.get("tool_count"),
            "tool_call_count": tool_calls, "content_chars": kw.get("assistant_content_chars"),
            "finish_reason": kw.get("finish_reason"), "status": "ok", "origin": "hook",
        }
        if not rid:
            return
        self._put("call", row)
        if not sid:
            return
        st = self._touch(sid, kw.get("platform"), kw.get("model"), ended)
        st.calls += 1
        if tout and tout == st.last_out:
            st.streak += 1
        else:
            st.streak = 1
        st.last_out = tout
        lim = int(self.settings["breaker_identical_output"])
        if st.streak >= IDENTICAL_WARN:
            self._live_finding(sid, st, "loop.identical_output", "high",
                               f"{sid}: {st.streak} calls in a row answered with exactly {tout} tokens",
                               {"streak": st.streak, "output_tokens": tout}, trip=False)
        if st.streak >= lim:
            self._live_finding(sid, st, "breaker.identical_output", "critical",
                               f"{sid}: {st.streak} identical completions — breaker",
                               {"streak": st.streak, "output_tokens": tout}, trip=True)
        b = self._baseline(st)
        floor = int(self.settings["breaker_calls_floor"])
        limit = max(floor, float(self.settings["breaker_calls_factor"]) * b["calls"]["p99"]) if bl.known(b) else None
        if limit and st.calls >= limit:
            self._live_finding(sid, st, "breaker.calls", "critical",
                               f"{sid}: {st.calls} calls — {st.calls / max(b['calls']['p50'], 1):.0f}× its normal run",
                               {"calls": st.calls, "p50": b["calls"]["p50"], "p99": b["calls"]["p99"]}, trip=True)

    def api_request_error(self, **kw):
        rid = kw.get("api_request_id")
        if not rid:
            return
        pend = self._pending_calls.pop(rid, {})
        err = kw.get("error") or {}
        retry = kw.get("retry_count") or 0
        self._put("call", {
            "api_request_id": f"{rid}:err{retry}", "session_id": kw.get("session_id"),
            "turn_id": kw.get("turn_id"), "seq": kw.get("api_call_count"), "retry": retry,
            "profile": self._profile(), "platform": kw.get("platform"), "model": kw.get("model"),
            "provider": kw.get("provider"), "base_url": kw.get("base_url"),
            "started_at": kw.get("started_at") or pend.get("started_at"), "ended_at": kw.get("ended_at") or time.time(),
            "latency_s": kw.get("api_duration"), "status": "error", "error_type": err.get("type"),
            "status_code": kw.get("status_code"), "error_message": textutil.preview(err.get("message"), 300),
            "origin": "hook",
        })

    def pre_tool_call(self, **kw):
        cid = kw.get("tool_call_id")
        if cid:
            self._tool_starts[cid] = time.time()
            if len(self._tool_starts) > 5000:
                for k in list(self._tool_starts)[:1000]:
                    self._tool_starts.pop(k, None)
        msg = control.block_message(kw.get("session_id"))
        if msg:
            control.apply(kw.get("session_id"))
            self._put("stop_applied", {"session_id": kw.get("session_id"), "at": time.time(),
                                       "by": "hook:pre_tool_call"})
            return {"action": "block", "message": msg}
        return None

    def post_tool_call(self, **kw):
        cid = kw.get("tool_call_id")
        if not cid:
            return
        sid = kw.get("session_id") or ""
        name = kw.get("tool_name") or "?"
        args = kw.get("args")
        now = time.time()
        started = self._tool_starts.pop(cid, None)
        status = kw.get("status") or "ok"
        text = textutil.result_text(kw.get("result"))
        if status == "ok":
            failed, why = textutil.tool_failed(name, text)
            if failed:
                status = "error"
                kw.setdefault("error_message", why)
        exact, shape = textutil.fingerprints(name, args)
        dur = kw.get("duration_ms")
        self._put("tool", {
            "tool_call_id": cid, "session_id": sid, "turn_id": kw.get("turn_id"),
            "api_request_id": kw.get("api_request_id"), "name": name, "fingerprint": exact, "shape": shape,
            "args_preview": textutil.preview(args), "status": status,
            "error_type": kw.get("error_type") or (("tool_error:" + (kw.get("error_message") or "")[:80])
                                                  if status == "error" else None),
            "result_preview": textutil.preview(text if status != "ok" else text[:200], 240 if status != "ok" else 80),
            "result_chars": len(text), "duration_ms": dur if dur is not None else
            ((now - started) * 1000.0 if started else None),
            "started_at": started or (now - (dur or 0) / 1000.0), "ended_at": now, "origin": "hook",
        })
        if not sid or status != "error":
            return
        st = self._touch(sid, None, None, now)
        st.fail_counts[exact] += 1
        n = st.fail_counts[exact]
        if n >= EXACT_WARN:
            self._live_finding(sid, st, "loop.exact_failure", "high",
                               f"{sid}: the same {name} call failed {n}×",
                               {"tool": name, "count": n, "args": textutil.preview(args)}, trip=False)
        if n >= int(self.settings["breaker_exact_failures"]):
            self._live_finding(sid, st, "breaker.exact_failure", "critical",
                               f"{sid}: the same {name} call failed {n}× — breaker",
                               {"tool": name, "count": n, "args": textutil.preview(args)}, trip=True)

    def on_session_start(self, **kw):
        sid = kw.get("session_id")
        if sid:
            self._touch(sid, kw.get("platform"), kw.get("model"), time.time())

    def on_session_end(self, **kw):
        """Despite the name, Hermes fires this at the end of every turn."""
        sid = kw.get("session_id")
        if not sid:
            return
        now = time.time()
        tid = kw.get("turn_id") or f"{sid}:hook:{now:.3f}"
        reason = kw.get("turn_exit_reason")
        self._put("turn", {"id": tid, "session_id": sid, "ended_at": now, "exit_reason": reason,
                           "completed": 1 if kw.get("completed") else 0,
                           "interrupted": 1 if kw.get("interrupted") else 0,
                           "model": kw.get("model"), "platform": kw.get("platform"), "origin": "hook"})
        if reason and (reason.startswith("max_iterations_reached") or reason == "guardrail_halt"):
            m = re.search(r"\((\d+)/(\d+)\)", reason)
            st = self._touch(sid, kw.get("platform"), kw.get("model"), now)
            self._live_finding(sid, st, "cap.guardrail_halt" if reason == "guardrail_halt" else "cap.max_iterations",
                               "warn" if reason == "guardrail_halt" else "high",
                               f"{sid}: turn ended — {reason}",
                               {"exit_reason": reason, "api_calls": int(m.group(1)) if m else None}, trip=False)

    def on_session_finalize(self, **kw):
        sid = kw.get("session_id")
        if sid:
            self._put("session_end", {"id": sid, "at": time.time(), "reason": kw.get("reason")})
            with self._live_lock:
                self._live.pop(sid, None)

    def subagent_stop(self, **kw):
        self._put("event", {"at": time.time(), "kind": "subagent.stop", "session_id": kw.get("parent_session_id"),
                            "job_id": None, "dedupe": f"sub:{kw.get('child_session_id')}",
                            "detail": json.dumps({"child": kw.get("child_session_id"), "role": kw.get("child_role"),
                                                  "status": kw.get("child_status"),
                                                  "duration_ms": kw.get("duration_ms")})})

    def kanban_event(self, hook: str):
        def cb(**kw):
            self._put("event", {"at": time.time(), "kind": "kanban." + hook, "session_id": None, "job_id": None,
                                "dedupe": f"kb:{hook}:{kw.get('task_id')}:{kw.get('run_id')}:{time.time():.0f}",
                                "detail": json.dumps({k: kw.get(k) for k in ("task_id", "profile_name", "board",
                                                                             "assignee", "run_id")}, default=str)})
        cb.__name__ = f"run_lens_{hook}"
        return cb

    # ── middleware: tag LiteLLM requests with the Hermes ids ──────────

    def llm_request(self, request=None, **kw):
        mode = str(self.settings.get("tag_litellm") or "auto").lower()
        if mode in ("off", "false", "0") or not isinstance(request, dict):
            return None
        if mode == "auto":
            from .ingest import litellm as _ll
            if not _ll.is_proxy_url(kw.get("base_url")):
                return None
        if kw.get("api_mode") not in (None, "chat_completions"):
            return None
        req = dict(request)
        extra = dict(req.get("extra_body") or {})
        meta = dict(extra.get("metadata") or {})
        slm = dict(meta.get("spend_logs_metadata") or {})
        slm.update({"hermes_session_id": kw.get("session_id"), "api_request_id": kw.get("api_request_id"),
                    "profile": self._profile(), "platform": kw.get("platform")})
        meta["spend_logs_metadata"] = slm
        extra["metadata"] = meta
        req["extra_body"] = extra
        return {"request": req, "source": "run-lens", "reason": "tag LiteLLM spend log"}


IDENTICAL_WARN = 10
EXACT_WARN = 5


_shared: Recorder | None = None
_shared_lock = threading.Lock()


def shared(settings: dict | None = None) -> Recorder:
    """One recorder per process. A multiplexed gateway calls the plugin's register()
    once per profile; all of them must feed the same queue and writer thread."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Recorder(settings)
        elif settings:
            _shared.settings.update({k: v for k, v in settings.items() if v is not None})
        return _shared
