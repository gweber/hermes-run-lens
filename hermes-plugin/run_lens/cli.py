"""`hermes lens …` — the command line face of run-lens."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import cards, control, detect, export, fmt, ingest, paths, query, settings
from .store import Tx, connect


def setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-ingest", action="store_true", help="show what is stored, skip the quick refresh")
    # The same two flags after the subcommand (`hermes lens runs --json`); SUPPRESS keeps a
    # flag given before the subcommand from being reset by the subparser's default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--no-ingest", action="store_true", default=argparse.SUPPRESS)
    _sub = parser.add_subparsers(dest="lens_cmd")

    class _Sub:
        def add_parser(self, name, **kw):
            return _sub.add_parser(name, parents=[common], **kw)

    sub = _Sub()

    p = sub.add_parser("overview", help="the last day at a glance (default)")
    p.add_argument("--since", default="24h")

    p = sub.add_parser("runs", help="runs (a session and its compression children), newest first")
    p.add_argument("--since", default="24h")
    p.add_argument("--source", help="cron, telegram, cli, tui, kanban, api_server, …")
    p.add_argument("--profile")
    p.add_argument("--job", help="job name or id")
    p.add_argument("--active", action="store_true", help="only runs that are live now")
    p.add_argument("--sort", choices=("started", "calls", "tokens", "wall"), default="started")
    p.add_argument("--limit", type=int, default=40)

    p = sub.add_parser("run", help="one run, call by call")
    p.add_argument("ref", help="session id, id prefix, or job name (its latest run)")
    p.add_argument("--calls", type=int, default=30, help="how many calls to list (first and last half)")

    p = sub.add_parser("jobs", help="cron jobs and other groups: normal run size, spend, suggested caps")
    p.add_argument("--since", default="7d")

    p = sub.add_parser("models", help="per model: who calls it, what answers, how fast")
    p.add_argument("--since", default="24h")

    p = sub.add_parser("findings", help="what needs a look")
    p.add_argument("--all", action="store_true", help="include acked and resolved")
    p.add_argument("--severity", choices=("info", "warn", "high", "critical"), default="warn")
    p.add_argument("--limit", type=int, default=40)

    for name, help_ in (("ack", "mark findings as seen"), ("resolve", "mark findings as resolved")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("ids", nargs="+", type=int)

    p = sub.add_parser("stop", help="interrupt a live run without restarting the gateway")
    p.add_argument("ref")
    p.add_argument("--reason", default="stopped from the command line")

    p = sub.add_parser("ingest", help="pull history from state.db, agent.log, LiteLLM, cron, kanban")
    p.add_argument("--full", action="store_true", help="re-read everything, not just what is new")
    p.add_argument("--only", nargs="*", choices=[s for s, _ in ingest.STEPS])

    p = sub.add_parser("detect", help="run the detectors")
    p.add_argument("--since", default="24h")

    p = sub.add_parser("watch", help="ingest + detect, print only new findings (for a no_agent cron job)")
    p.add_argument("--since", default=None, help="default: settings.watch_window (3h)")
    p.add_argument("--severity", choices=("warn", "high", "critical"), default=None,
                   help="default: settings.watch_severity (high)")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be delivered and which kanban cards would be created; create nothing, "
                        "mark nothing as notified")

    p = sub.add_parser("setup", help="create or update the watch cron job (no LLM; silent unless something is wrong)")
    p.add_argument("--deliver", default=None, help="delivery target, e.g. telegram:<chat_id> (default: settings.watch_deliver)")
    p.add_argument("--schedule", default=None, help="default: settings.watch_schedule (*/5 * * * *)")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("demo", help="write a synthetic store to try the CLI and dashboard without real data")
    p.add_argument("--db", required=True, help="path of the demo database to create")

    sub.add_parser("doctor", help="is capture live in every profile, and what does the store hold")

    p = sub.add_parser("export", help="export runs as OpenTelemetry GenAI spans (OTLP/JSON) or JSONL")
    p.add_argument("--since", default="24h")
    p.add_argument("--format", choices=("otlp", "jsonl"), default="otlp")
    p.add_argument("--out", default="-")
    p.add_argument("--endpoint", help="POST OTLP/JSON to this collector (…/v1/traces is appended to a bare URL)")
    p.add_argument("--to-monitoring", action="store_true",
                   help="POST to Hermes's own collector: monitoring.export.otlp.endpoint and headers_env")


def handle(args: argparse.Namespace) -> int:
    cmd = getattr(args, "lens_cmd", None) or "overview"
    conn = connect()
    if cmd in ("overview", "runs", "run", "jobs", "models", "findings") and not args.no_ingest:
        _quick_refresh(conn)
    fn = globals()["cmd_" + cmd]
    return fn(conn, args) or 0


def _quick_refresh(conn) -> None:
    try:
        ingest.run(conn, only=("cron", "statedb", "log"))
        detect.run_all(conn, since=time.time() - 3 * 3600)
    except Exception as exc:
        print(fmt.c(f"(refresh failed: {exc})", "2"), file=sys.stderr)


def _out(args, data) -> bool:
    if getattr(args, "json", False):
        json.dump(data, sys.stdout, indent=1, default=str)
        sys.stdout.write("\n")
        return True
    return False


def _sev(s):
    return fmt.c(s or "", fmt.SEV_COLOR.get(s or ""))


def cmd_overview(conn, args):
    since = fmt.parse_since(getattr(args, "since", None), 86400)
    o = query.overview(conn, since)
    if _out(args, o):
        return
    hours = (o["now"] - since) / 3600
    print(fmt.c(f"run-lens · last {fmt.dur(o['now'] - since)}", "1"))
    ext = f"  + {fmt.n(o['external_calls'])} calls by other clients" if o["external_calls"] else ""
    print(f"  {fmt.n(o['runs'])} runs · {fmt.n(o['calls'])} LLM calls · {fmt.n(o['input_tokens'])} prompt / "
          f"{fmt.n(o['output_tokens'])} completion tokens{ext}")
    if o["top3_token_share"]:
        print(f"  the 3 heaviest runs used {o['top3_token_share'] * 100:.0f}% of all prompt tokens")
    sev = o["open_findings"]
    if sev:
        print("  open findings: " + " · ".join(f"{_sev(k)} {sev[k]}" for k in ("critical", "high", "warn", "info")
                                              if sev.get(k)))
    tl = query.timeline(conn, since, 3600 if hours <= 48 else 86400)
    if tl["series"]:
        print()
        print(fmt.c("  calls per " + ("hour" if tl["bucket_s"] == 3600 else "day"), "2"))
        for src, s in sorted(tl["series"].items(), key=lambda kv: -sum(kv[1]["calls"])):
            print(f"  {src:>12} {fmt.spark(s['calls'])} {fmt.n(sum(s['calls']))}")
    if o["running"]:
        print()
        print(fmt.c("running now", "1;32"))
        print(_runs_table(o["running"]))
    print()
    print(fmt.c("heaviest runs", "1"))
    print(_runs_table(o["top_runs"]))
    rows = [[str(k), fmt.n(v["runs"]), fmt.n(v["calls"]), fmt.n(v["input_tokens"])]
            for k, v in sorted(o["by_source"].items(), key=lambda kv: -kv[1]["input_tokens"])]
    print()
    print(fmt.table(rows, ["source", "runs", "calls", "prompt tok"], "lrrr"))
    f = query.findings(conn, min_severity="high", limit=8)
    if f:
        print()
        print(fmt.c("findings", "1"))
        print(_findings_table(f))
    print()
    print(fmt.c(f"store {o['db']} ({fmt.n(o['db_bytes'])}B) · last live hook row "
                f"{fmt.ago(o['last_hook']) if o['last_hook'] else 'never — is the plugin enabled?'}", "2"))


def _runs_table(rs):
    rows = []
    for r in rs:
        status = fmt.c(r.get("status", ""), fmt.STATUS_COLOR.get(r.get("status", "")))
        worst = _sev(r.get("worst_finding")) if r.get("worst_finding") else ""
        rows.append([fmt.clock(r["started_at"]), status, r.get("label") or r["root_id"], r["profile"] or "",
                     r["source"] or "", fmt.n(r["calls"]), fmt.n(r["tool_calls"]), fmt.n(r["input_tokens"]),
                     fmt.dur(r["wall_s"]), worst, r["root_id"]])
    return fmt.table(rows, ["started", "status", "run", "profile", "source", "calls", "tools", "prompt tok",
                            "wall", "worst", "id"], "lllllrrrrll")


def cmd_runs(conn, args):
    since = fmt.parse_since(args.since, 86400)
    rs = query.runs(conn, since, source=args.source, profile=args.profile, job=args.job, active=args.active,
                    sort=args.sort, limit=args.limit)
    if _out(args, rs):
        return
    if not rs:
        print("no runs in that window")
        return
    print(_runs_table(rs))


def cmd_run(conn, args):
    root = query.resolve_run(conn, args.ref)
    if not root:
        print(f"no run matches {args.ref!r}")
        return 1
    d = query.run_detail(conn, root)
    if _out(args, d):
        return
    r = d["run"]
    print(fmt.c(f"{r.get('label')}", "1") + f"  ({root})")
    print(f"  {r.get('profile')} · {r.get('source')} · {r.get('model')} · started {fmt.clock(r.get('started_at'))} · "
          f"wall {fmt.dur(r.get('wall_s'))} · {len(d['sessions'])} session(s)")
    print(f"  {fmt.n(r.get('calls'))} calls · {fmt.n(r.get('tool_calls'))} tool calls · "
          f"{fmt.n(r.get('input_tokens'))} prompt / {fmt.n(r.get('output_tokens'))} completion tokens")
    b = d["baseline"]
    if b and b["calls"]["n"] >= 5:
        print(fmt.c(f"  normal for {r['group']}: calls p50 {fmt.n(b['calls']['p50'])} · p95 {fmt.n(b['calls']['p95'])}"
                    f" · p99 {fmt.n(b['calls']['p99'])}  (n={b['calls']['n']})", "2"))
    ends = [t for t in d["turns"] if t.get("exit_reason")]
    if ends:
        print(f"  ended: {ends[-1]['exit_reason']}" + (f" · session end: {d['sessions'][-1].get('end_reason')}"
                                                      if d['sessions'][-1].get('end_reason') else ""))
    if d["findings"]:
        print()
        print(_findings_table(d["findings"]))
    calls = d["calls"]
    if calls:
        print()
        tin = [c_["input_tokens"] or 0 for c_ in calls]
        print(fmt.c("  prompt tokens per call  ", "2") + fmt.spark(_squeeze(tin, 80)) +
              f"  {fmt.n(tin[0])} → {fmt.n(max(tin))}")
        lat = [c_["latency_s"] or 0 for c_ in calls]
        print(fmt.c("  latency per call        ", "2") + fmt.spark(_squeeze(lat, 80)) +
              f"  p50 {fmt.dur(sorted(lat)[len(lat) // 2])} · max {fmt.dur(max(lat))}")
        k = max(2, args.calls)
        show = calls if len(calls) <= k else calls[: k // 2] + [None] + calls[-k // 2:]
        rows = []
        for c_ in show:
            if c_ is None:
                rows.append(["…", f"({len(calls) - k} more)", "", "", "", "", "", "", ""])
                continue
            served = c_["served_model"] or ""
            rows.append([fmt.clock(c_["ended_at"]), str(c_["seq"] or ""), c_["model"] or "", served,
                         fmt.n(c_["input_tokens"]), fmt.n(c_["output_tokens"]), fmt.dur(c_["latency_s"]),
                         fmt.dur(c_["ttft_s"]) if c_["ttft_s"] else "",
                         fmt.c(c_["status"] or "", "31" if c_["status"] == "error" else None)])
        print(fmt.table(rows, ["at", "#", "model", "served by", "in", "out", "latency", "ttft", "status"],
                        "lrllrrrrl"))
    if d["repeats"] and d["repeats"][0]["count"] > 1:
        print()
        print(fmt.c("most repeated tool calls", "1"))
        rows = [[fmt.n(x["count"]), fmt.c(fmt.n(x["errors"]), "31" if x["errors"] else None), x["name"] or "",
                 x["args"] or ""] for x in d["repeats"][:6]]
        print(fmt.table(rows, ["times", "failed", "tool", "arguments"], "rrll"))
    errs = [t for t in d["tools"] if t["status"] == "error"]
    if errs:
        print()
        print(fmt.c(f"last failed tool results ({len(errs)} failures)", "1"))
        for t in errs[-3:]:
            print(f"  {fmt.clock(t['ended_at'])} {t['name']}: {t['result_preview']}")


def _squeeze(vals, width):
    if len(vals) <= width:
        return vals
    step = len(vals) / width
    return [max(vals[int(i * step): max(int(i * step) + 1, int((i + 1) * step))]) for i in range(width)]


def cmd_jobs(conn, args):
    since = fmt.parse_since(args.since, 7 * 86400)
    gs = query.groups(conn, since)
    if _out(args, gs):
        return
    rows = []
    for g in gs:
        b = g["baseline"]
        known = b and b["calls"]["n"] >= 5
        cap = g["max_turns"]
        sug = g["suggested_cap"]
        cap_txt = str(cap) if cap else "500*" if g["source"] == "cron" else "–"
        warn = sug and (cap or 500) > 4 * sug
        rows.append([
            g["name"], g["profile"] or "", g["source"] or "", fmt.n(g["runs"]),
            fmt.n(b["calls"]["p50"]) if known else "–", fmt.n(b["calls"]["p99"]) if known else "–",
            fmt.c(fmt.n(g["max_calls"]), "31" if known and g["max_calls"] > 3 * b["calls"]["p99"] + 10 else None),
            fmt.n(g["tokens_per_day"]), fmt.c(cap_txt, "33" if warn else None), str(sug or "–"),
            "on" if g["hard_stop"] else fmt.c("off", "33"),
            _sev("high") + f" {g['high_findings']}" if g["high_findings"] else ""])
    print(fmt.table(rows, ["group", "profile", "source", "runs", "calls p50", "p99", "max", "prompt tok/day",
                           "cap", "suggest", "hardstop", "findings"], "lllrrrrrrrll"))
    print(fmt.c("cap = agent.max_turns of the profile (500* = cron default when unset) · suggest = 1.5 × p99 of "
                "normal runs; a cap far above it only decides how long a loop may burn", "2"))


def cmd_models(conn, args):
    since = fmt.parse_since(args.since, 86400)
    ms = query.models(conn, since)
    if _out(args, ms):
        return
    for m in ms:
        served = ", ".join(f"{s['model']} {fmt.n(s['calls'])}" for s in m["served"][:3]) or "–"
        print(fmt.c(m["model"], "1") + f"  {fmt.n(m['calls'])} calls · {fmt.n(m['input_tokens'])} prompt tok · "
              f"latency p50 {fmt.dur(m['latency_p50'])} p95 {fmt.dur(m['latency_p95'])} · "
              f"ttft p50 {fmt.dur(m['ttft_p50'])}" + (fmt.c(f" · {m['errors']} errors", "31") if m["errors"] else ""))
        print(fmt.c(f"  served by: {served}", "2"))
        rows = [[x["caller"], fmt.n(x["calls"]), f"{x['calls'] / m['calls'] * 100:.0f}%", fmt.n(x["input_tokens"])]
                for x in m["by_caller"][:8]]
        print(fmt.table([["  " + r[0]] + r[1:] for r in rows], ["  caller", "calls", "share", "prompt tok"], "lrrr"))
        print()


def _findings_table(fs):
    rows = []
    for f in fs:
        rows.append([str(f["id"]), _sev(f["severity"]), fmt.clock(f["last_seen"]), f["title"],
                     f["state"] if f["state"] != "open" else ""])
    return fmt.table(rows, ["id", "severity", "seen", "finding", "state"], "rllll")


def cmd_findings(conn, args):
    fs = query.findings(conn, state=None if args.all else "open", min_severity=args.severity, limit=args.limit)
    if _out(args, fs):
        return
    if not fs:
        print("nothing open")
        return
    print(_findings_table(fs))
    top = fs[0]
    if top.get("suggestion"):
        print(fmt.c(f"\n#{top['id']}: {top['suggestion']}", "2"))


def _set_state(conn, ids, state):
    with Tx(conn):
        for i in ids:
            conn.execute("UPDATE findings SET state=? WHERE id=?", (state, i))
    print(f"{len(ids)} finding(s) → {state}")


def cmd_ack(conn, args):
    _set_state(conn, args.ids, "acked")


def cmd_resolve(conn, args):
    _set_state(conn, args.ids, "resolved")


def cmd_stop(conn, args):
    root = query.resolve_run(conn, args.ref)
    if not root:
        print(f"no run matches {args.ref!r}")
        return 1
    ids = [r[0] for r in conn.execute("SELECT id FROM sessions WHERE root_id=?", (root,))]
    with Tx(conn):
        control.request_stop(conn, ids, by="cli", reason=args.reason)
    print(f"stop requested for {root} ({len(ids)} session id(s)). The owning process applies it within "
          f"{control.POLL_S:.0f}s of its next LLM or tool call; `hermes lens run {root}` shows when it ended.")


def cmd_ingest(conn, args):
    rep = ingest.run(conn, full=args.full, only=tuple(args.only or ()))
    if _out(args, rep):
        return
    for k, v in rep.items():
        print(f"{k:>8}: " + ", ".join(f"{kk}={vv}" for kk, vv in v.items()))


def cmd_detect(conn, args):
    rep = detect.run_all(conn, since=fmt.parse_since(args.since, 86400))
    if _out(args, rep):
        return
    print(", ".join(f"{k}={v}" for k, v in rep.items() if v))


def cmd_watch(conn, args):
    """Silent unless there is something new at or above --severity. Built for a no_agent cron job."""
    since = fmt.parse_since(args.since or settings.get("watch_window"), 3 * 3600)
    args.severity = args.severity or settings.get("watch_severity") or "high"
    ingest.run(conn)
    detect.run_all(conn, since=since)
    # Only what was seen inside the window: a manual `detect --since 14d` must not turn
    # two weeks of history into a burst of notifications on the next tick.
    fs = [f for f in query.findings(conn, state="open", min_severity=args.severity, since=since, limit=50)
          if not f.get("notified_at") and f["kind"] != "aux.failed"]
    fs += _watch_aux(conn, args)
    if not fs:
        return 0
    if args.dry_run:
        print(f"(dry run) would deliver {len(fs)} finding(s):")
    if _out(args, fs):
        pass
    else:
        lines = [f"run-lens: {len(fs)} new finding(s)"]
        for f in fs[:10]:
            lines.append(f"• [{f['severity']}] {f['title']}")
            if f.get("suggestion"):
                lines.append(f"  → {f['suggestion']}")
            if f.get("session_id"):
                lines.append(f"  hermes lens run {f['session_id']}")
        if len(fs) > 10:
            lines.append(f"… and {len(fs) - 10} more: hermes lens findings")
        print("\n".join(lines))
    if args.dry_run:
        return 0
    with Tx(conn):
        now = time.time()
        for f in fs:
            conn.execute("UPDATE findings SET notified_at=? WHERE id=?", (now, f["id"]))
    return 0


def _watch_aux(conn, args) -> list:
    """New aux.failed findings: a kanban card each (aux_notify: kanban), or delivered like the rest (print).

    Returns the findings still to be printed. A card that was created marks its finding
    notified here, so the tick's output stays empty; a card that could not be created
    falls back to the printed notification. `off` leaves them unnotified, so switching
    to kanban later still turns the last day's findings into cards.
    """
    mode = str(settings.get("aux_notify") or "off")
    new = [f for f in query.findings(conn, state="open", min_severity="info", since=time.time() - detect.AUX_WINDOW_S,
                                     limit=200) if f["kind"] == "aux.failed" and not f.get("notified_at")]
    if not new:
        return []
    board, assignee = settings.get("aux_card_board") or "spark", settings.get("aux_card_assignee") or "ops"
    if args.dry_run:
        print(f"(dry run) aux_notify={mode}; {len(new)} new aux.failed finding(s) → cards on board {board!r} "
              f"for {assignee!r} when aux_notify is kanban:")
        for f in new:
            card = cards.card_for(f)
            ev = f["evidence"] if isinstance(f["evidence"], dict) else {}
            print(f"• {ev.get('profile')} / {ev.get('task')} / {ev.get('signature')} — {ev.get('count_24h')}× in 24 h"
                  f"\n  card: {card['title']}\n  key:  {card['key']}")
        print()
        return []
    if mode == "print":
        return new
    if mode != "kanban":
        return []
    leftover = []
    for f in new:
        ok, res = cards.create(cards.card_for(f), board, assignee)
        if not ok:
            f["suggestion"] = f"kanban card could not be created ({res[:160]})"
            leftover.append(f)
            continue
        with Tx(conn):
            conn.execute("UPDATE findings SET notified_at=?, suggestion=? WHERE id=?",
                         (time.time(), f"kanban card {res} on board {board} for {assignee}", f["id"]))
    return leftover


def cmd_doctor(conn, args):
    import sqlite3 as _sq
    rows = []
    enabled = {}
    try:
        import yaml  # type: ignore
        for profile, home in paths.homes():
            cfg = yaml.safe_load((home / "config.yaml").read_text()) or {}
            en = ((cfg.get("plugins") or {}).get("enabled")) or []
            visible = (home / "plugins" / "run-lens").exists()
            enabled[profile] = ("run-lens" in en, visible)
    except Exception as exc:
        print(f"(could not read profile configs: {exc})")
    last = {r["profile"]: r["t"] for r in conn.execute(
        "SELECT profile, MAX(ended_at) t FROM calls WHERE origin='hook' GROUP BY profile")}
    for profile, (en, vis) in enabled.items():
        rows.append([profile, fmt.c("yes", "32") if en else fmt.c("no", "33"),
                     "yes" if vis else fmt.c("no", "33"), fmt.ago(last.get(profile))])
    if _out(args, {"profiles": enabled, "last_hook": last}):
        return
    print(fmt.table(rows, ["profile", "enabled", "plugin visible", "last live call"], "llll"))
    print()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("sessions", "calls", "tools", "turns", "events", "ext_calls", "findings", "stops")}
    print("store: " + " · ".join(f"{k} {fmt.n(v)}" for k, v in counts.items()))
    by_origin = conn.execute("SELECT origin, COUNT(*) n FROM calls GROUP BY origin").fetchall()
    print("calls by origin: " + " · ".join(f"{r['origin']} {fmt.n(r['n'])}" for r in by_origin))
    from .ingest import litellm as _ll
    print(f"litellm: proxies {', '.join(_ll.proxies(refresh=True)) or 'none found'} · spend logs via "
          f"{_ll.source()} · request tagging {settings.get('tag_litellm')}")
    print(f"breaker: {settings.get('breaker')} · retention {settings.get('retention_days')} days")
    print(fmt.c(f"db: {paths.db_path()}", "2"))


def cmd_export(conn, args):
    since = fmt.parse_since(args.since, 86400)
    if args.format == "jsonl":
        data = export.jsonl(conn, since)
    else:
        data = export.otlp(conn, since)
    if args.to_monitoring:
        args.endpoint = export.monitoring_endpoint()
        if not args.endpoint:
            print("monitoring.export.otlp.endpoint is not set in config.yaml")
            return 1
    if args.endpoint:
        import urllib.request
        body = json.dumps(data).encode()
        url = args.endpoint if args.endpoint.rstrip("/").endswith("/v1/traces") else args.endpoint.rstrip("/") + "/v1/traces"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              **export.monitoring_headers()})
        with urllib.request.urlopen(req, timeout=60) as r:
            print(f"POST {url}: {r.status}")
        return
    text = data if isinstance(data, str) else json.dumps(data)
    if args.out == "-":
        sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out}")


WATCH_SCRIPT = """#!/usr/bin/env python3
# Written by `hermes lens setup` (run-lens). Runs `hermes lens watch` for the no_agent
# cron job; its stdout is what gets delivered, and empty stdout means nothing is sent.
import shutil, subprocess, sys

hermes = shutil.which("hermes")
cmd = [hermes] if hermes else [sys.executable, "-m", "hermes_cli.main"]
out = subprocess.run(cmd + ["lens", "watch"], capture_output=True, text=True, timeout=240)
lines = [l for l in out.stdout.splitlines() if not l.startswith("  Command helper")]
sys.stdout.write("\\n".join(lines).strip() + ("\\n" if lines else ""))
if out.returncode != 0 and not lines:
    print("run-lens watch failed: " + (out.stderr.strip().splitlines() or ["no output"])[-1])
sys.exit(0)
"""


def cmd_setup(conn, args):
    """Create or update the watch job through Hermes's own cron store."""
    deliver = args.deliver or settings.get("watch_deliver") or "local"
    schedule = args.schedule or settings.get("watch_schedule") or "*/5 * * * *"
    home = paths.root_home()
    script = home / "scripts" / "run_lens_watch.py"
    print(f"script   {script}")
    print(f"job      run-lens-watch · {schedule} · no_agent · deliver {deliver}")
    if args.dry_run:
        return
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(WATCH_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    try:
        from cron import jobs as cron_jobs  # type: ignore
        from tools.cronjob_tools import cronjob  # type: ignore  (the same entry point `hermes cron` uses)
    except Exception as exc:
        print(f"Hermes cron is not importable ({exc}); create the job with:\n"
              f"  hermes cron create '{schedule}' --name run-lens-watch --no-agent --script run_lens_watch.py "
              f"--deliver {deliver}")
        return 1
    existing = [j for j in cron_jobs.load_jobs() if j.get("name") == "run-lens-watch"]
    if existing:
        res = json.loads(cronjob(action="update", job_id=existing[0]["id"], schedule=schedule, deliver=deliver,
                                 script="run_lens_watch.py", no_agent=True))
    else:
        res = json.loads(cronjob(action="create", schedule=schedule, name="run-lens-watch", deliver=deliver,
                                 script="run_lens_watch.py", no_agent=True))
    if not res.get("success"):
        print(f"cron: {res.get('error')}")
        return 1
    print(f"{'updated' if existing else 'created'}  {(res.get('job') or {}).get('job_id') or (existing[0]['id'] if existing else '')}")
    # Mark what is already known as notified, so the first tick does not replay history.
    with Tx(conn):
        conn.execute("UPDATE findings SET notified_at=? WHERE notified_at IS NULL", (time.time(),))


def cmd_demo(conn, args):
    from . import demo
    n = demo.build(args.db)
    print(f"wrote {args.db}: {n} synthetic runs\n"
          f"  RUN_LENS_DB={args.db} hermes lens --no-ingest\n"
          f"  RUN_LENS_DB={args.db} hermes dashboard --port 9129   # a second dashboard on the demo store")
