"""The run-lens database: schema, migrations, connections.

All times are REAL unix epoch seconds (UTC), the unit state.db already uses.

Every row carries `origin` — which source wrote it: `hook` (recorded live inside the
agent process), `statedb`, `log`, `litellm`, `cron`, `kanban`. Live rows are the most
precise; the ingesters fill history and whatever ran in a process without the plugin,
and never overwrite a field a hook already recorded with a coarser value.

Several processes write here at once (the gateway, kanban workers, CLI chats, the
watchdog), so the file runs in WAL mode with a generous busy timeout, and writers keep
their transactions short.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from . import paths

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- One agent session. Compression rotates a session id into a child; `root_id` is the
-- first session of that chain, which is what a person means by "one run".
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    root_id         TEXT,
    parent_id       TEXT,
    profile         TEXT,
    source          TEXT,          -- cron, telegram, cli, tui, kanban, api_server, subagent, ...
    job_id          TEXT,
    task_id         TEXT,          -- kanban task
    board           TEXT,
    title           TEXT,
    model           TEXT,          -- the alias Hermes asked for (LiteLLM model group)
    started_at      REAL,
    ended_at        REAL,
    last_activity_at REAL,
    end_reason      TEXT,
    last_exit_reason TEXT,         -- turn_exit_reason of the latest turn
    api_calls       INTEGER DEFAULT 0,
    tool_calls      INTEGER DEFAULT 0,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    origin          TEXT,
    updated_at      REAL
);
CREATE INDEX IF NOT EXISTS ix_sessions_root ON sessions(root_id);
CREATE INDEX IF NOT EXISTS ix_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS ix_sessions_job ON sessions(job_id);

-- One conversation turn (one run_conversation).
CREATE TABLE IF NOT EXISTS turns (
    id              TEXT PRIMARY KEY,   -- turn_id, or "<session>:log:<ended_at>" from the log
    session_id      TEXT,
    started_at      REAL,
    ended_at        REAL,
    exit_reason     TEXT,
    api_calls       INTEGER,
    max_iterations  INTEGER,
    tool_turns      INTEGER,
    completed       INTEGER,
    interrupted     INTEGER,
    model           TEXT,
    platform        TEXT,
    origin          TEXT
);
CREATE INDEX IF NOT EXISTS ix_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS ix_turns_ended ON turns(ended_at);

-- One LLM API call (one attempt).
CREATE TABLE IF NOT EXISTS calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    api_request_id  TEXT UNIQUE,
    session_id      TEXT,
    turn_id         TEXT,
    seq             INTEGER,            -- call number as Hermes counts it
    retry           INTEGER DEFAULT 0,
    profile         TEXT,
    platform        TEXT,
    model           TEXT,               -- alias / model group
    served_model    TEXT,               -- the deployment LiteLLM actually used
    provider        TEXT,
    base_url        TEXT,
    started_at      REAL,
    ended_at        REAL,
    latency_s       REAL,
    ttft_s          REAL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_read_tokens INTEGER,
    reasoning_tokens INTEGER,
    message_count   INTEGER,
    tool_count      INTEGER,            -- tools offered
    tool_call_count INTEGER,            -- tools the model called in its answer
    content_chars   INTEGER,
    finish_reason   TEXT,
    status          TEXT,               -- ok, error
    error_type      TEXT,
    status_code     INTEGER,
    error_message   TEXT,
    litellm_id      TEXT UNIQUE,
    origin          TEXT
);
CREATE INDEX IF NOT EXISTS ix_calls_session ON calls(session_id, ended_at);
CREATE INDEX IF NOT EXISTS ix_calls_ended ON calls(ended_at);
CREATE INDEX IF NOT EXISTS ix_calls_model ON calls(model, ended_at);

-- One tool call.
CREATE TABLE IF NOT EXISTS tools (
    tool_call_id    TEXT PRIMARY KEY,
    session_id      TEXT,
    turn_id         TEXT,
    api_request_id  TEXT,
    name            TEXT,
    fingerprint     TEXT,               -- name + exact arguments, hashed
    shape           TEXT,               -- name + arguments with digits/long strings normalised
    args_preview    TEXT,
    status          TEXT,               -- ok, error, blocked
    error_type      TEXT,
    result_preview  TEXT,
    result_chars    INTEGER,
    duration_ms     REAL,
    started_at      REAL,
    ended_at        REAL,
    origin          TEXT
);
CREATE INDEX IF NOT EXISTS ix_tools_session ON tools(session_id, ended_at);
CREATE INDEX IF NOT EXISTS ix_tools_fp ON tools(session_id, fingerprint);
CREATE INDEX IF NOT EXISTS ix_tools_ended ON tools(ended_at);

-- LLM calls LiteLLM saw that no Hermes session claims: other clients (opencode,
-- scripts, podcast, TTS) competing for the same GPUs.
CREATE TABLE IF NOT EXISTS ext_calls (
    litellm_id      TEXT PRIMARY KEY,
    model           TEXT,
    served_model    TEXT,
    key_alias       TEXT,
    client          TEXT,
    started_at      REAL,
    ended_at        REAL,
    latency_s       REAL,
    ttft_s          REAL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    status          TEXT
);
CREATE INDEX IF NOT EXISTS ix_ext_ended ON ext_calls(ended_at);

-- Events that are not calls: compression, cron scheduler decisions, guardrails,
-- kanban lifecycle, stops.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL,
    kind            TEXT,
    session_id      TEXT,
    job_id          TEXT,
    detail          TEXT,               -- JSON
    origin          TEXT,
    dedupe          TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_events_at ON events(at);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind, at);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    profile         TEXT,
    name            TEXT,
    schedule        TEXT,
    model           TEXT,
    enabled         INTEGER,
    state           TEXT,
    no_agent        INTEGER,
    monitor         TEXT,
    script          TEXT,
    deliver         TEXT,
    last_status     TEXT,
    last_run_at     REAL,
    updated_at      REAL
);

CREATE TABLE IF NOT EXISTS executions (
    id              TEXT PRIMARY KEY,
    job_id          TEXT,
    profile         TEXT,
    source          TEXT,
    status          TEXT,
    claimed_at      REAL,
    started_at      REAL,
    finished_at     REAL,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS ix_exec_job ON executions(job_id, claimed_at);

CREATE TABLE IF NOT EXISTS kanban_runs (
    board           TEXT,
    run_id          INTEGER,
    task_id         TEXT,
    title           TEXT,
    profile         TEXT,
    status          TEXT,
    outcome         TEXT,
    started_at      REAL,
    ended_at        REAL,
    session_id      TEXT,
    error           TEXT,
    PRIMARY KEY (board, run_id)
);

CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT UNIQUE,
    kind            TEXT,
    severity        TEXT,               -- info, warn, high, critical
    session_id      TEXT,
    job_id          TEXT,
    model           TEXT,
    title           TEXT,
    detail          TEXT,
    evidence        TEXT,               -- JSON
    suggestion      TEXT,
    first_seen      REAL,
    last_seen       REAL,
    count           INTEGER DEFAULT 1,
    state           TEXT DEFAULT 'open', -- open, acked, resolved
    notified_at     REAL,
    origin          TEXT
);
CREATE INDEX IF NOT EXISTS ix_findings_state ON findings(state, last_seen);

CREATE TABLE IF NOT EXISTS stops (
    session_id      TEXT PRIMARY KEY,
    requested_at    REAL,
    requested_by    TEXT,
    reason          TEXT,
    applied_at      REAL,
    applied_by      TEXT
);

CREATE TABLE IF NOT EXISTS watermarks (
    source          TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      REAL
);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialized: set[str] = set()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """A new connection with the pragmas every writer needs."""
    p = Path(path) if path is not None else paths.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        # The store holds redacted tool arguments and session titles: owner-only, like ~/.hermes.
        p.touch(mode=0o600)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    key = str(p.resolve())
    if key not in _initialized:
        with _init_lock:
            if key not in _initialized:
                migrate(conn)
                _initialized.add(key)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))


def get_watermark(conn: sqlite3.Connection, source: str, default=None):
    row = conn.execute("SELECT value FROM watermarks WHERE source=?", (source,)).fetchone()
    return row["value"] if row else default


def set_watermark(conn: sqlite3.Connection, source: str, value) -> None:
    conn.execute(
        "INSERT INTO watermarks(source, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (source, str(value), time.time()),
    )


def upsert(conn: sqlite3.Connection, table: str, row: dict, key: tuple[str, ...],
           keep: tuple[str, ...] = ()) -> None:
    """INSERT … ON CONFLICT DO UPDATE.

    Columns in `keep` are only written when the stored value is NULL — the way a
    coarse source (the log) fills a gap without overwriting what a hook recorded.
    Other columns take the new value unless the new value is NULL.
    """
    cols = list(row)
    placeholders = ",".join("?" for _ in cols)
    sets = []
    for c in cols:
        if c in key:
            continue
        if c in keep:
            sets.append(f"{c}=COALESCE({table}.{c}, excluded.{c})")
        else:
            sets.append(f"{c}=COALESCE(excluded.{c}, {table}.{c})")
    sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({placeholders})"
    if sets:
        sql += f" ON CONFLICT({','.join(key)}) DO UPDATE SET {','.join(sets)}"
    else:
        sql += f" ON CONFLICT({','.join(key)}) DO NOTHING"
    conn.execute(sql, [row[c] for c in cols])


class Tx:
    """`with Tx(conn):` — BEGIN IMMEDIATE … COMMIT, ROLLBACK on error."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
