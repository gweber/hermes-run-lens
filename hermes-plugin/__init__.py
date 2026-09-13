"""run-lens — see every Hermes run: each LLM call, each tool call, what it cost, and
when it is going wrong. Stop one run without restarting the gateway.

Registered here:
  hooks       pre/post_api_request, api_request_error, pre/post_tool_call,
              on_session_start/end/finalize, subagent_stop, kanban lifecycle
  middleware  llm_request — tags LiteLLM requests with the Hermes session and call id
  CLI         hermes lens …
  slash       /lens — the last hour, in the chat

The engine lives in the `run_lens` package next to this file. It is imported by its
absolute name on purpose: Hermes imports this plugin once per profile under a
per-home module name, and every profile in a process must share one recorder.
"""
from __future__ import annotations

import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

logger = logging.getLogger("run_lens")


def _settings(ctx) -> dict:
    from run_lens import settings

    return settings.from_ctx(ctx)


def _slash(raw_args: str = "") -> str:
    import time

    from run_lens import fmt, query
    from run_lens.store import connect

    conn = connect()
    since = time.time() - 3600
    o = query.overview(conn, since)
    lines = [f"run-lens · last hour: {o['runs']} runs, {o['calls']} LLM calls, "
             f"{fmt.n(o['input_tokens'])} prompt tokens"]
    for r in o["running"][:5]:
        lines.append(f"running: {r['label']} ({r['profile']}) — {r['calls']} calls, {fmt.dur(r['wall_s'])}")
    for f in query.findings(conn, min_severity="high", since=time.time() - 6 * 3600, limit=5):
        lines.append(f"[{f['severity']}] {f['title']}")
    lines.append("details: hermes lens · hermes lens run <id>")
    return "\n".join(lines)


def register(ctx) -> None:
    from run_lens import cli
    from run_lens.recorder import shared

    rec = shared(_settings(ctx))

    for name in ("pre_api_request", "post_api_request", "api_request_error", "pre_tool_call", "post_tool_call",
                 "on_session_start", "on_session_end", "on_session_finalize", "subagent_stop"):
        ctx.register_hook(name, getattr(rec, name))
    for hook in ("kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked",
                 "on_kanban_worker_spawned", "on_kanban_worker_exited", "on_kanban_worker_stale_claim"):
        ctx.register_hook(hook, rec.kanban_event(hook))
    try:
        ctx.register_middleware("llm_request", rec.llm_request)
    except Exception as exc:  # older Hermes without middleware: capture still works
        logger.info("run-lens: no llm_request middleware (%s); LiteLLM rows will be matched statistically", exc)
    try:
        ctx.register_cli_command("lens", help="run-lens: runs, calls, loops, stops",
                                 setup_fn=cli.setup, handler_fn=cli.handle,
                                 description="See every Hermes run call by call, find loops and runaways, stop a run.")
    except Exception as exc:
        logger.debug("run-lens: CLI command not registered: %s", exc)
    try:
        ctx.register_command("lens", _slash, description="run-lens: the last hour of runs and findings")
    except Exception as exc:
        logger.debug("run-lens: slash command not registered: %s", exc)
