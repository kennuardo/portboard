"""SQLite access for Portboard.

One connection per operation (open, use, close). WAL mode, foreign keys on,
busy timeout so the CLI, hooks and the daemon can touch the same file.
The schema below is the contract every module shares; change it here only.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,
  path         TEXT NOT NULL UNIQUE,          -- main checkout, absolute, no trailing slash
  kind         TEXT NOT NULL DEFAULT 'transient',  -- transient | unit | compose | none
  start_cmd    TEXT,                          -- transient: shell command ({port} placeholder allowed)
                                              -- unit: existing systemd --user unit name (e.g. sheron-dev.service)
                                              -- compose: optional override of "docker compose up -d"
  stop_cmd     TEXT,                          -- optional override for stop
  port_mode    TEXT NOT NULL DEFAULT 'env',   -- env (PORT/NUXT_PORT/NITRO_PORT injected) | arg ({port} in start_cmd)
                                              -- fixed (unit/compose decide themselves) | none (no port)
  base_port    INTEGER UNIQUE,                -- port of the main checkout; worktree slot n uses base_port+n
  slots        INTEGER NOT NULL DEFAULT 9,
  health_path  TEXT NOT NULL DEFAULT '/',
  open_path    TEXT NOT NULL DEFAULT '/',     -- appended to http://localhost:<port> for the GUI link
  pinned       INTEGER NOT NULL DEFAULT 0,    -- 1 = never stopped by schedule or idle rule
  autostart    TEXT NOT NULL DEFAULT 'schedule',  -- schedule (morning restart if evening stopped it) | never
  env_json     TEXT NOT NULL DEFAULT '{}',    -- extra environment for the runner
  path_prepend TEXT,                          -- PATH prefix for the runner (defaults to settings.node_path)
  memory_max   TEXT,                          -- MemoryMax override (defaults to settings.memory_max)
  source       TEXT NOT NULL DEFAULT 'cli',   -- cli | gui | mcp | hook | discover | adopted
  notes        TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  label          TEXT NOT NULL,               -- 'main' or the worktree slug
  slot           INTEGER NOT NULL,            -- 0 = main checkout, 1..slots = worktrees
  path           TEXT NOT NULL,               -- checkout directory of this instance
  branch         TEXT,
  port           INTEGER UNIQUE,              -- assigned port (NULL for port_mode none)
  unit           TEXT,                        -- systemd unit / compose project name / container name
  managed        INTEGER NOT NULL DEFAULT 1,  -- 0 = detected or adopted, not started by us
  state          TEXT NOT NULL DEFAULT 'stopped',  -- stopped | starting | running | failed | unknown
  pid            INTEGER,
  actual_port    INTEGER,                     -- what reconcile really saw (frameworks may drift)
  owner_session  TEXT,                        -- Claude Code session id that claimed it
  owner_seen_at  TEXT,
  started_at     TEXT,
  stopped_at     TEXT,
  stopped_by     TEXT,                        -- user | schedule | idle | session | crash | hook
  last_seen_at   TEXT,
  mem_bytes      INTEGER,
  cpu_ns         INTEGER,
  cpu_checked_at TEXT,
  idle_since     TEXT,                        -- first tick that found it idle; NULL when busy
  extra_json     TEXT NOT NULL DEFAULT '{}',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  UNIQUE(project_id, path),
  UNIQUE(project_id, label)
);

CREATE TABLE IF NOT EXISTS observed (
  port            INTEGER NOT NULL,
  proto           TEXT NOT NULL DEFAULT 'tcp',
  bind            TEXT NOT NULL DEFAULT '',
  pid             INTEGER,
  comm            TEXT,
  cwd             TEXT,
  cmdline         TEXT,
  unit            TEXT,                       -- systemd unit from the pid's cgroup, if any
  container       TEXT,                       -- docker container name, if any
  compose_project TEXT,
  compose_workdir TEXT,
  project_id      INTEGER,
  instance_id     INTEGER,
  seen_at         TEXT NOT NULL,
  PRIMARY KEY (port, proto, bind)
);

CREATE TABLE IF NOT EXISTS reserved_ports (
  port       INTEGER PRIMARY KEY,
  label      TEXT,
  source     TEXT NOT NULL DEFAULT 'observed',   -- observed | static
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  kind        TEXT NOT NULL,                   -- e.g. project.add, instance.start, schedule.stop, reconcile
  project_id  INTEGER,
  instance_id INTEGER,
  detail      TEXT                             -- JSON or plain text
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""

EVENTS_KEEP = 2000


def now() -> str:
    """Local time, ISO without microseconds. Used for every timestamp column."""
    return datetime.now().replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    """Open the database, creating schema and default settings when missing."""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return config.DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def get_int_setting(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        return int(get_setting(conn, key, str(default)) or default)
    except ValueError:
        return default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    merged = dict(config.DEFAULT_SETTINGS)
    merged.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    return merged


def add_event(
    conn: sqlite3.Connection,
    kind: str,
    detail: Any = None,
    project_id: int | None = None,
    instance_id: int | None = None,
) -> None:
    """Append an audit event and keep the table bounded."""
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    conn.execute(
        "INSERT INTO events(ts, kind, project_id, instance_id, detail) VALUES (?, ?, ?, ?, ?)",
        (now(), kind, project_id, instance_id, detail),
    )
    conn.execute(
        "DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?",
        (EVENTS_KEEP,),
    )


def rows(cursor_or_rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in cursor_or_rows]


def row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if r is None else dict(r)
