"""Fingerprints, previews and failure detection for tool calls.

Two fingerprints per call, because loops come in two shapes:

- **exact** — name + arguments byte for byte. A looping cron run that re-ran the
  identical Python snippet ~400 times is caught by exact matching.
- **shape** — name + arguments with digits and whitespace normalised and quoted
  strings cut to their first 48 characters. A loop that retries with a new timestamp or a slightly
  reworded message each time is invisible to the exact print and obvious here.

Previews are short and redacted with Hermes's own `redact_sensitive_text` (forced),
falling back to a local pattern set when run outside the Hermes venv. The store
never holds a full tool result or prompt.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

PREVIEW_CHARS = 240

# (pattern, replacement) — used only when agent.redact is not importable.
_FALLBACK_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{12,}"), "sk-***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1***"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd)[\"']?\s*[=:]\s*[\"']?)[^\s,'\"]{6,}"), r"\1***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "***jwt***"),
    (re.compile(r"(?i)(postgres(?:ql)?://[^:\s]+:)[^@\s]+@"), r"\1***@"),
]


def redact(text: str) -> str:
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text  # type: ignore

        return redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:
        out = text
        for pat, repl in _FALLBACK_SECRET_PATTERNS:
            out = pat.sub(repl, out)
        return out


def preview(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    if limit is None:
        try:
            from . import settings

            limit = int(settings.get("preview_chars") or PREVIEW_CHARS)
        except Exception:
            limit = PREVIEW_CHARS
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            value = str(value)
    flat = " ".join(value.split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + " …"
    return redact(flat)


def _canonical_args(args: Any) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return args
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return str(args)


_DIGITS = re.compile(r"\d+")
_LONG_STR = re.compile(r'"((?:[^"\\]|\\.){48})(?:[^"\\]|\\.)+"')
_WS = re.compile(r"\s+")


def fingerprints(name: str, args: Any) -> tuple[str, str]:
    """(exact, shape) — 16-hex digests."""
    canon = _canonical_args(args)
    exact = hashlib.sha1(f"{name}\x00{canon}".encode("utf-8", "replace")).hexdigest()[:16]
    # Long strings keep their first 48 characters: "python3 tools/queue.py claim --id …"
    # stays distinct from "git status …", while a changing tail no longer hides a loop.
    shaped = _WS.sub(" ", _DIGITS.sub("#", _LONG_STR.sub(lambda m: '"' + m.group(1) + '…"', canon)))
    shape = hashlib.sha1(f"{name}\x00{shaped}".encode("utf-8", "replace")).hexdigest()[:16]
    return exact, shape


def tool_failed(name: str, result: Any) -> tuple[bool, str]:
    """(failed, short reason) for a tool result as the model saw it.

    Hermes appends its loop-guardrail note after the result (`{...json...}\n\n[Tool
    loop warning: …]`) in what it stores, which makes the JSON unparseable — the
    stock detector then calls a 400-times-failing command "ok". The note is split
    off first, and a failure-type note counts as a failure on its own.
    """
    if result is None:
        return False, ""
    if isinstance(result, str) and "[Tool loop" in result:
        marks = guardrail_marks(result)
        result = result.split("\n\n[Tool loop", 1)[0].split("[Tool loop", 1)[0].rstrip()
        if any("failure" in code for _lvl, code, _n in marks):
            failed, why = tool_failed(name, result)
            return True, why or next(code for _lvl, code, _n in marks if "failure" in code)
    try:
        from agent.display import _detect_tool_failure  # type: ignore

        failed, suffix = _detect_tool_failure(name, result if isinstance(result, str) else json.dumps(result, default=str))
        return bool(failed), suffix.strip().strip("[]")
    except Exception:
        pass
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict):
        if name == "terminal" and data.get("exit_code") not in (None, 0):
            return True, f"exit {data.get('exit_code')}"
        if data.get("error") or data.get("success") is False:
            return True, str(data.get("error") or "failed")[:120]
    if text.startswith("Error"):
        return True, text[:120]
    return False, ""


GUARDRAIL_RE = re.compile(r"\[Tool loop (warning|hard stop): (\w+); count=(\d+)")


def guardrail_marks(text: str) -> list[tuple[str, str, int]]:
    """Guardrail suffixes Hermes appends to a tool result: (level, code, count)."""
    if not text or "[Tool loop" not in text:
        return []
    return [(m.group(1), m.group(2), int(m.group(3))) for m in GUARDRAIL_RE.finditer(text)]


def result_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return str(result)
