"""Terminal formatting: numbers, durations, times, tables, sparklines, colour."""
from __future__ import annotations

import os
import re
import shutil
import sys
import time

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
SEV_COLOR = {"critical": "1;31", "high": "31", "warn": "33", "info": "36"}
STATUS_COLOR = {"running": "1;32", "capped": "31", "open": "33", "done": "2"}


def c(text: str, code: str | None) -> str:
    if not _COLOR or not code:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def n(v) -> str:
    if v is None:
        return "–"
    v = float(v)
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M"
    if abs(v) >= 1e4:
        return f"{v / 1e3:.0f}k"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{v:.0f}" if v == int(v) else f"{v:.1f}"


def dur(s) -> str:
    if s is None:
        return "–"
    s = float(s)
    if s < 1:
        return f"{s * 1000:.0f}ms"
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    if s < 86400:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def ago(ts) -> str:
    if not ts:
        return "–"
    s = time.time() - float(ts)
    return "just now" if s < 1 else dur(s) + " ago"


def clock(ts) -> str:
    if not ts:
        return "–"
    t = time.localtime(float(ts))
    if time.time() - float(ts) < 20 * 3600:
        return time.strftime("%H:%M:%S", t)
    return time.strftime("%d.%m %H:%M", t)


def parse_since(text: str | None, default_s: float) -> float:
    if not text:
        return time.time() - default_s
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw])", text.strip())
    if not m:
        raise SystemExit(f"--since: expected e.g. 90m, 24h, 7d — got {text!r}")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return time.time() - float(m.group(1)) * mult


BLOCKS = "▁▂▃▄▅▆▇█"


def spark(vals) -> str:
    vals = [float(v or 0) for v in vals]
    if not vals or max(vals) <= 0:
        return " " * len(vals)
    top = max(vals)
    return "".join(" " if v <= 0 else BLOCKS[min(7, int(v / top * 7.999))] for v in vals)


def _visible(s: str) -> int:
    return len(re.sub(r"\x1b\[[0-9;]*m", "", s))


def table(rows: list[list[str]], headers: list[str], align: str | None = None, max_width: int | None = None) -> str:
    """align: one char per column, 'l' or 'r'. The last left-aligned column absorbs overflow."""
    width = max_width or shutil.get_terminal_size((160, 40)).columns
    cols = len(headers)
    align = align or "l" * cols
    cells = [[str(x) for x in headers]] + [[str(x) for x in r] for r in rows]
    widths = [max(_visible(r[i]) for r in cells) for i in range(cols)]
    total = sum(widths) + 2 * (cols - 1)
    if total > width:
        flex = max((i for i in range(cols) if align[i] == "l"), default=cols - 1)
        widths[flex] = max(12, widths[flex] - (total - width))
    out = []
    for ri, r in enumerate(cells):
        parts = []
        for i, cell in enumerate(r):
            vis = _visible(cell)
            if vis > widths[i]:
                cell = re.sub(r"\x1b\[[0-9;]*m", "", cell)[: widths[i] - 1] + "…"
                vis = widths[i]
            pad = " " * (widths[i] - vis)
            parts.append(pad + cell if align[i] == "r" else cell + pad)
        line = "  ".join(parts).rstrip()
        out.append(c(line, "1") if ri == 0 else line)
    return "\n".join(out)
