"""LiteLLM: which deployment really answered, time to first token, and the traffic
that is not Hermes at all.

Optional. Nothing here runs unless a LiteLLM proxy is found or configured.

**Finding the proxy.** `litellm_url` if set; otherwise every provider `base_url` in
the Hermes configs (model, providers, custom_providers, auxiliary) is probed once
with `GET /health/liveliness` — no key needed — and the ones that answer like
LiteLLM are used. The result is cached for an hour.

**Reading spend logs** (`litellm_spend_source`):

- `api`       `GET /spend/logs/v2` with an admin key from the env var named by
              `litellm_admin_key_env`. LiteLLM restricts this route to the proxy admin;
              an ordinary virtual key gets 401.
- `postgres`  the proxy's database, DSN from the env var named by
              `litellm_postgres_dsn_env` (needs `psql` on PATH).
- `docker`    `docker exec <litellm_docker_container> psql -U litellm -d litellm`, for a
              proxy whose database runs in a container on the same host.
- `auto`      the first of those that is configured; otherwise skipped quietly.

**Matching a spend-log row to a Hermes call.** Exact when the request carried
`metadata.spend_logs_metadata.api_request_id` (the recorder's `llm_request`
middleware adds it). Otherwise by model group + prompt and completion tokens + end
time within a few seconds; an ambiguous match is left unmatched rather than guessed.
Unclaimed rows are kept as other clients' traffic (`ext_calls`).
"""
from __future__ import annotations

import ast
import csv
import io
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .. import paths, settings
from ..store import Tx, get_watermark, set_watermark, upsert

MATCH_WINDOW_S = 4.0
BATCH = 5000
DETECT_TTL_S = 3600

_detected: dict = {"at": 0.0, "urls": []}


# ── finding the proxy ─────────────────────────────────────────────────


def _config_base_urls() -> list[str]:
    urls: set[str] = set()
    try:
        import yaml  # type: ignore
    except Exception:
        return []
    for _profile, home in paths.homes():
        try:
            cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        blocks = [cfg.get("model") or {}]
        blocks += list((cfg.get("providers") or {}).values()) if isinstance(cfg.get("providers"), dict) else []
        blocks += cfg.get("custom_providers") or [] if isinstance(cfg.get("custom_providers"), list) else []
        blocks += list((cfg.get("auxiliary") or {}).values()) if isinstance(cfg.get("auxiliary"), dict) else []
        for b in blocks:
            if isinstance(b, dict) and isinstance(b.get("base_url"), str) and b["base_url"].startswith("http"):
                urls.add(_root(b["base_url"]))
    return sorted(urls)


def _root(url: str) -> str:
    u = url.rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def _is_litellm(root: str) -> bool:
    try:
        with urllib.request.urlopen(root + "/health/liveliness", timeout=2) as r:
            body = r.read(200).decode("utf-8", "replace")
            return r.status == 200 and "alive" in body.lower()
    except Exception:
        return False


def proxies(refresh: bool = False) -> list[str]:
    """LiteLLM proxy roots (no trailing /v1). Cached; safe to call from a writer thread."""
    configured = settings.get("litellm_url")
    if configured:
        return [_root(str(configured))]
    now = time.time()
    if refresh or now - _detected["at"] > DETECT_TTL_S:
        _detected["urls"] = [u for u in _config_base_urls() if _is_litellm(u)]
        _detected["at"] = now
    return list(_detected["urls"])


def is_proxy_url(base_url: str | None) -> bool:
    """Cheap membership test for the recorder's hot path (no network)."""
    if not base_url:
        return False
    root = _root(str(base_url))
    return any(root == p or root.replace("localhost", "127.0.0.1") == p.replace("localhost", "127.0.0.1")
               for p in (_detected["urls"] or ([_root(settings.get("litellm_url"))] if settings.get("litellm_url") else [])))


# ── spend-log sources ─────────────────────────────────────────────────

COLUMNS = ('request_id, model_group, model, "startTime", "endTime", "completionStartTime", prompt_tokens, '
           "completion_tokens, request_duration_ms, status, metadata->'spend_logs_metadata' AS slm, "
           "metadata->>'user_api_key_alias' AS key_alias, request_tags::text AS tags")


def source() -> str:
    mode = str(settings.get("litellm_spend_source") or "auto")
    if mode != "auto":
        return mode
    if os.environ.get(str(settings.get("litellm_admin_key_env") or "")) and proxies():
        return "api"
    if os.environ.get(str(settings.get("litellm_postgres_dsn_env") or "")) and shutil.which("psql"):
        return "postgres"
    if settings.get("litellm_docker_container") and shutil.which("docker"):
        return "docker"
    return "off"


_WM = __import__("re").compile(r"^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(\.\d{1,6})?$")


def _sql_rows(cmd: list[str], since: str) -> list[dict]:
    if not _WM.match(since):  # it is interpolated into SQL below; accept a timestamp and nothing else
        raise ValueError(f"unexpected watermark {since!r}")
    sql = (f'SELECT {COLUMNS} FROM "LiteLLM_SpendLogs" WHERE "startTime" > \'{since}\' '
           f'ORDER BY "startTime" LIMIT {BATCH}')
    out = subprocess.run(cmd + ["-q", "-v", "ON_ERROR_STOP=1", "-c", f"COPY ({sql}) TO STDOUT WITH CSV HEADER"],
                         capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    rows = []
    for r in csv.DictReader(io.StringIO(out.stdout)):
        r["slm"] = _json(r.get("slm"))
        r["tags"] = _json(r.get("tags")) or []
        rows.append(r)
    return rows


def _api_rows(since: str) -> list[dict]:
    """One window of spend logs via the admin API, oldest first."""
    base = proxies()[0]
    key = os.environ.get(str(settings.get("litellm_admin_key_env")), "")
    start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    end = min(datetime.now(timezone.utc) + timedelta(minutes=1), start + timedelta(hours=6))
    rows, page = [], 1
    while True:
        q = urllib.parse.urlencode({"start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                                    "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
                                    "page": page, "page_size": 500})
        req = urllib.request.Request(f"{base}/spend/logs/v2?{q}", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        for d in data.get("data") or []:
            meta = d.get("metadata")
            meta = meta if isinstance(meta, dict) else (_json(meta) or {})
            tags = d.get("request_tags")
            rows.append({
                "request_id": d.get("request_id"), "model_group": d.get("model_group"), "model": d.get("model"),
                "startTime": d.get("startTime"), "endTime": d.get("endTime"),
                "completionStartTime": d.get("completionStartTime"),
                "prompt_tokens": d.get("prompt_tokens"), "completion_tokens": d.get("completion_tokens"),
                "request_duration_ms": d.get("request_duration_ms"), "status": d.get("status"),
                "slm": meta.get("spend_logs_metadata"), "key_alias": meta.get("user_api_key_alias"),
                "tags": tags if isinstance(tags, list) else (_json(tags) or []),
            })
        if page >= int(data.get("total_pages") or 1):
            break
        page += 1
    rows = [r for r in rows if (_epoch(r["startTime"]) or 0) > start.timestamp()]
    rows.sort(key=lambda r: _epoch(r["startTime"]) or 0)
    if not rows and end < datetime.now(timezone.utc):
        # an empty window: move the watermark past it
        rows = [{"_advance_to": end.strftime("%Y-%m-%d %H:%M:%S")}]
    return rows


def _json(v):
    if v is None or v == "" or isinstance(v, (dict, list)):
        return v
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(v)
        except Exception:
            continue
    return None


def _epoch(v) -> float | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except Exception:
        return None


def _fetch(mode: str, since: str) -> list[dict]:
    if mode == "api":
        return _api_rows(since)
    if mode == "postgres":
        dsn = os.environ.get(str(settings.get("litellm_postgres_dsn_env")), "")
        return _sql_rows(["psql", dsn], since)
    if mode == "docker":
        container = str(settings.get("litellm_docker_container"))
        return _sql_rows(["docker", "exec", "-i", container, "psql", "-U", "litellm", "-d", "litellm"], since)
    return []


# ── ingest ────────────────────────────────────────────────────────────


def ingest(conn: sqlite3.Connection, full: bool = False) -> dict:
    mode = source()
    stats = {"source": mode, "rows": 0, "exact": 0, "matched": 0, "external": 0}
    if mode == "off":
        return stats
    wm = None if full else get_watermark(conn, "litellm:starttime")
    if not wm:
        days = float(settings.get("litellm_backfill_days") or 14)
        wm = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(500):  # bounded: at most 500 windows per ingest
        rows = _fetch(mode, wm)
        if not rows:
            break
        with Tx(conn):
            for r in rows:
                if "_advance_to" in r:
                    wm = r["_advance_to"]
                    continue
                _one(conn, r, stats)
                wm = datetime.fromtimestamp(_epoch(r["startTime"]), timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
                stats["rows"] += 1
            set_watermark(conn, "litellm:starttime", wm)
        if mode != "api" and len(rows) < BATCH:
            break
        if mode == "api" and (_epoch(wm.replace(" ", "T")) or 0) >= time.time() - 60:
            break
    return stats


def _client(tags) -> str | None:
    for t in tags or []:
        if isinstance(t, str) and t.startswith("User-Agent: ") and "/" in t:
            return t[len("User-Agent: "):]
    for t in tags or []:
        if isinstance(t, str) and t.startswith("User-Agent: "):
            return t[len("User-Agent: "):]
    return None


def _one(conn, r, stats) -> None:
    lid = r["request_id"]
    start, end = _epoch(r["startTime"]), _epoch(r["endTime"])
    ttft = None
    cst = _epoch(r.get("completionStartTime"))
    if cst and start and cst >= start:
        ttft = cst - start
    latency = (float(r["request_duration_ms"]) / 1000.0) if r.get("request_duration_ms") else \
        ((end - start) if start and end else None)
    tin = int(r["prompt_tokens"] or 0)
    tout = int(r["completion_tokens"] or 0)
    served = r["model"] or None
    status = r.get("status") or None
    if conn.execute("SELECT 1 FROM calls WHERE litellm_id=?", (lid,)).fetchone():
        return
    slm = r.get("slm") if isinstance(r.get("slm"), dict) else {}
    target = None
    if slm.get("api_request_id"):
        hit = conn.execute("SELECT id FROM calls WHERE api_request_id=?", (slm["api_request_id"],)).fetchone()
        if hit:
            target = hit["id"]
            stats["exact"] += 1
    if target is None and end:
        cands = conn.execute(
            "SELECT id FROM calls WHERE litellm_id IS NULL AND model=? AND input_tokens=? AND output_tokens=? "
            "AND ABS(ended_at - ?) < ? LIMIT 2", (r["model_group"], tin, tout, end, MATCH_WINDOW_S)).fetchall()
        if len(cands) == 1:
            target = cands[0]["id"]
            stats["matched"] += 1
    if target is not None:
        conn.execute("UPDATE calls SET litellm_id=?, served_model=COALESCE(served_model, ?), "
                     "ttft_s=COALESCE(ttft_s, ?), latency_s=COALESCE(latency_s, ?) WHERE id=?",
                     (lid, served, ttft, latency, target))
        return
    if slm.get("hermes_session_id"):
        conn.execute(
            "INSERT OR IGNORE INTO calls(api_request_id, session_id, model, served_model, started_at, ended_at, "
            "latency_s, ttft_s, input_tokens, output_tokens, status, litellm_id, origin, profile) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (slm.get("api_request_id"), slm["hermes_session_id"], r["model_group"], served, start, end, latency,
             ttft, tin, tout, "ok" if status in (None, "success") else "error", lid, "litellm", slm.get("profile")))
        stats["exact"] += 1
        return
    upsert(conn, "ext_calls", {
        "litellm_id": lid, "model": r["model_group"], "served_model": served,
        "key_alias": r.get("key_alias") or None, "client": _client(r.get("tags")),
        "started_at": start, "ended_at": end, "latency_s": latency, "ttft_s": ttft,
        "input_tokens": tin, "output_tokens": tout, "status": status,
    }, ("litellm_id",))
    stats["external"] += 1


def reconcile(conn: sqlite3.Connection, horizon_s: float = 6 * 3600) -> int:
    """Move external rows onto Hermes calls that were recorded after LiteLLM's row
    was read (a log flushed late, a hook batch written after the spend log)."""
    moved = 0
    since = time.time() - horizon_s
    rows = conn.execute("SELECT * FROM ext_calls WHERE ended_at >= ?", (since,)).fetchall()
    with Tx(conn):
        for e in rows:
            cands = conn.execute(
                "SELECT id FROM calls WHERE litellm_id IS NULL AND model=? AND input_tokens=? AND output_tokens=? "
                "AND ABS(ended_at - ?) < ? LIMIT 2",
                (e["model"], e["input_tokens"], e["output_tokens"], e["ended_at"], MATCH_WINDOW_S)).fetchall()
            if len(cands) != 1:
                continue
            conn.execute("UPDATE calls SET litellm_id=?, served_model=COALESCE(served_model, ?), "
                         "ttft_s=COALESCE(ttft_s, ?) WHERE id=?",
                         (e["litellm_id"], e["served_model"], e["ttft_s"], cands[0]["id"]))
            conn.execute("DELETE FROM ext_calls WHERE litellm_id=?", (e["litellm_id"],))
            moved += 1
    return moved
