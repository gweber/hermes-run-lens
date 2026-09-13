"""Export runs as OpenTelemetry spans following the GenAI semantic conventions.

One trace per run: an `invoke_agent` span for the run, a `chat <model>` span per LLM
call, an `execute_tool <name>` span per tool call — attribute names from the OTel
GenAI conventions (gen_ai.operation.name, gen_ai.request.model, gen_ai.usage.*,
gen_ai.tool.*), so Langfuse, Phoenix, Jaeger or any OTLP collector can take the file
or the POST as is. Previews stay out of spans; ids and numbers only.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from . import baseline as bl
from . import paths


def _id(text: str, n: int) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def _ns(ts) -> str:
    return str(int(float(ts or 0) * 1e9))


def _attrs(d: dict) -> list:
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": k, "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            out.append({"key": k, "value": {"doubleValue": v}})
        elif isinstance(v, (list, tuple)):
            out.append({"key": k, "value": {"arrayValue": {"values": [{"stringValue": str(x)} for x in v]}}})
        else:
            out.append({"key": k, "value": {"stringValue": str(v)}})
    return out


def otlp(conn: sqlite3.Connection, since: float, max_spans: int = 200_000) -> dict:
    spans = []
    jobs = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM jobs")}
    for run in bl.runs(conn, since):
        root = run["root_id"]
        trace = _id(root, 32)
        rspan = _id(root, 16)
        name = jobs.get(run["job_id"]) if run["job_id"] else (run["title"] or run["source"])
        spans.append({
            "traceId": trace, "spanId": rspan, "name": f"invoke_agent {name}", "kind": 1,
            "startTimeUnixNano": _ns(run["started_at"]), "endTimeUnixNano": _ns(run["last_at"]),
            "attributes": _attrs({"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": run["profile"],
                                  "gen_ai.conversation.id": root, "gen_ai.request.model": run["model"],
                                  "gen_ai.usage.input_tokens": int(run["input_tokens"] or 0),
                                  "gen_ai.usage.output_tokens": int(run["output_tokens"] or 0),
                                  "hermes.source": run["source"], "hermes.job.id": run["job_id"],
                                  "hermes.job.name": jobs.get(run["job_id"]), "hermes.kanban.task": run["task_id"],
                                  "hermes.api_calls": int(run["calls"] or 0),
                                  "hermes.tool_calls": int(run["tool_calls"] or 0)}),
            "status": {"code": 0},
        })
        ids = [r[0] for r in conn.execute("SELECT id FROM sessions WHERE root_id=?", (root,))]
        ph = ",".join("?" for _ in ids)
        for c in conn.execute(f"SELECT * FROM calls WHERE session_id IN ({ph})", ids):
            err = c["status"] == "error"
            spans.append({
                "traceId": trace, "spanId": _id(f"call:{c['id']}", 16), "parentSpanId": rspan,
                "name": f"chat {c['model']}", "kind": 3,
                "startTimeUnixNano": _ns(c["started_at"] or c["ended_at"]), "endTimeUnixNano": _ns(c["ended_at"]),
                "attributes": _attrs({"gen_ai.operation.name": "chat", "gen_ai.request.model": c["model"],
                                      "gen_ai.response.model": c["served_model"], "gen_ai.provider.name": c["provider"],
                                      "gen_ai.response.id": c["litellm_id"],
                                      "gen_ai.usage.input_tokens": c["input_tokens"],
                                      "gen_ai.usage.output_tokens": c["output_tokens"],
                                      "gen_ai.response.finish_reasons": [c["finish_reason"]] if c["finish_reason"] else None,
                                      "gen_ai.server.time_to_first_token": c["ttft_s"],
                                      "error.type": c["error_type"] if err else None,
                                      "hermes.session.id": c["session_id"], "hermes.call.seq": c["seq"]}),
                "status": {"code": 2, "message": c["error_message"] or ""} if err else {"code": 0},
            })
        for t in conn.execute(f"SELECT * FROM tools WHERE session_id IN ({ph})", ids):
            err = t["status"] in ("error", "blocked")
            spans.append({
                "traceId": trace, "spanId": _id(f"tool:{t['tool_call_id']}", 16), "parentSpanId": rspan,
                "name": f"execute_tool {t['name']}", "kind": 1,
                "startTimeUnixNano": _ns(t["started_at"] or t["ended_at"]), "endTimeUnixNano": _ns(t["ended_at"] or t["started_at"]),
                "attributes": _attrs({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": t["name"],
                                      "gen_ai.tool.call.id": t["tool_call_id"], "error.type": t["error_type"] if err else None,
                                      "hermes.tool.fingerprint": t["fingerprint"], "hermes.session.id": t["session_id"]}),
                "status": {"code": 2} if err else {"code": 0},
            })
        if len(spans) >= max_spans:
            break
    return {"resourceSpans": [{
        "resource": {"attributes": _attrs({"service.name": "hermes-agent", "telemetry.sdk.name": "run-lens"})},
        "scopeSpans": [{"scope": {"name": "run-lens", "version": "0.2.0"}, "spans": spans}],
    }]}


def jsonl(conn: sqlite3.Connection, since: float) -> str:
    lines = []
    for run in bl.runs(conn, since):
        lines.append(json.dumps({"type": "run", **run}, default=str))
    for table, col in (("calls", "ended_at"), ("tools", "ended_at"), ("turns", "ended_at"), ("findings", "last_seen")):
        for r in conn.execute(f"SELECT * FROM {table} WHERE {col} >= ?", (since,)):
            lines.append(json.dumps({"type": table[:-1], **dict(r)}, default=str))
    return "\n".join(lines) + "\n"


def _monitoring_otlp() -> dict:
    try:
        import yaml  # type: ignore

        cfg = yaml.safe_load((paths.root_home() / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return (((cfg.get("monitoring") or {}).get("export") or {}).get("otlp")) or {}


def monitoring_endpoint() -> str:
    """The OTLP collector Hermes's own monitoring exports to, if one is configured."""
    return str(_monitoring_otlp().get("endpoint") or "")


def monitoring_headers() -> dict:
    """Headers from monitoring.export.otlp.headers_env — names of env vars, never values in config."""
    import os

    out = {}
    for header, env in (_monitoring_otlp().get("headers_env") or {}).items():
        if os.environ.get(str(env)):
            out[str(header)] = os.environ[str(env)]
    return out
