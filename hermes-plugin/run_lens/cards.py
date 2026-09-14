"""Kanban cards for findings that someone should fix rather than read.

A failing auxiliary task (see detect._aux_failures) is not an alert: it is work. The
watch job hands each new one to a profile as a card on a board — `hermes kanban create`
with an idempotency key derived from the finding, so a second tick, or a retry after
a failed create, returns the open card instead of a duplicate.

Settings: `aux_notify` (off | print | kanban), `aux_card_board`, `aux_card_assignee`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys

from . import paths

# Where each task's failure is logged from, relative to the Hermes checkout.
CODE_HINTS = {
    "background_review": ["agent/background_review.py"],
    "title_generation": ["agent/title_generator.py", "agent/auxiliary_client.py"],
    "compression": ["agent/context_compressor.py", "agent/conversation_loop.py", "agent/turn_overflow.py",
                    "agent/auxiliary_client.py"],
    "paid_lane": ["agent/auxiliary_client.py"],
}
DEFAULT_HINT = ["agent/auxiliary_client.py"]
LINE_CHARS = 240


def _clock(ts) -> str:
    return dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"


def idempotency_key(finding: dict) -> str:
    """Stable per finding and episode: a reopened finding (new first_seen) gets a new card."""
    raw = f"{finding['fingerprint']}|{int(float(finding.get('first_seen') or 0))}"
    return "run-lens-" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def card_for(finding: dict) -> dict:
    ev = finding.get("evidence") or {}
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = {}
    profile, task, sig = ev.get("profile", "?"), ev.get("task", "?"), ev.get("signature", "?")
    checkout = paths.root_home() / "hermes-agent"
    what = ("the auxiliary client fell back to a PAID OpenRouter model" if task == "paid_lane"
            else f"auxiliary task `{task}` fails")
    title = (f"PAID auxiliary lane engaged in profile {profile}: {sig}" if task == "paid_lane"
             else f"Auxiliary {task} failing in profile {profile}: {sig}")[:150]
    lines = [
        f"run-lens found a background failure nobody sees: {what} (profile `{profile}`).",
        "",
        f"- Error signature: {sig}",
        f"- Count: {ev.get('count_24h', '?')} in the last 24 h (threshold {ev.get('threshold', '?')})",
        f"- First seen: {_clock(finding.get('first_seen'))} · last seen: {_clock(ev.get('last') or finding.get('last_seen'))}",
        f"- Profile: {profile}",
    ]
    if ev.get("sessions"):
        lines.append("- Sessions: " + ", ".join(ev["sessions"]) + "  (`hermes lens run <id>` shows one)")
    lines += ["", "Sample log lines:"]
    for s in (ev.get("samples") or [])[-3:]:
        s = s if len(s) <= LINE_CHARS else s[:LINE_CHARS - 1] + "…"
        lines.append(f"    {s}")
    lines += ["", "Log files:"]
    lines += [f"    {f}" for f in (ev.get("files") or [])] or ["    (unknown)"]
    lines += ["", "Where the code lives:"]
    lines += [f"    {checkout / h}" for h in CODE_HINTS.get(task, DEFAULT_HINT)]
    if ev.get("loggers"):
        lines.append("    logger: " + ", ".join(ev["loggers"]))
    lines += [
        "",
        "Done when: the cause is fixed (or the behaviour is deliberately configured) and no new matching lines "
        "appear. Then `hermes lens resolve " + str(finding.get("id", "<id>")) + "`; if it fails again later, "
        "run-lens opens a new card.",
        "",
        f"run-lens finding #{finding.get('id', '?')} · fingerprint {finding['fingerprint']}",
    ]
    return {"title": title, "body": "\n".join(lines), "key": idempotency_key(finding)}


def hermes_cmd() -> list[str]:
    hermes = shutil.which("hermes")
    return [hermes] if hermes else [sys.executable, "-m", "hermes_cli.main"]


def create_argv(card: dict, board: str, assignee: str) -> list[str]:
    return hermes_cmd() + ["kanban", "--board", board, "create", card["title"], "--assignee", assignee,
                           "--body", card["body"], "--idempotency-key", card["key"], "--created-by", "run-lens",
                           "--json"]


def create(card: dict, board: str, assignee: str) -> tuple[bool, str]:
    """(ok, task id or error)."""
    try:
        out = subprocess.run(create_argv(card, board, assignee), capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    text = out.stdout or ""
    if out.returncode != 0:
        return False, ((out.stderr or text).strip().splitlines() or [f"exit {out.returncode}"])[-1]
    start = text.find("{")  # a command helper may print a line before the JSON
    if start >= 0:
        try:
            task, _ = json.JSONDecoder().raw_decode(text[start:])
            if task.get("id"):
                return True, str(task["id"])
        except Exception:
            pass
    m = re.search(r"Created (\S+)", text)
    if m:
        return True, m.group(1)
    return False, "no task id in output: " + (text.strip()[-200:] or (out.stderr or "").strip()[-200:])
