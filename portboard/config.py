"""Paths, constants and default settings for Portboard.

Everything that other modules need to agree on lives here. Keep it free of
imports from the rest of the package so it can be imported anywhere.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

VERSION = "0.1.0"

# State (database + log) lives outside the repo. Tests override it via env.
STATE_DIR = Path(os.environ.get("PORTBOARD_STATE_DIR", "~/.local/state/portboard")).expanduser()
DB_PATH = STATE_DIR / "portboard.db"
LOG_PATH = STATE_DIR / "portboard.log"

# Where the user keeps projects; used by discover and by path -> project mapping.
PROJECTS_ROOT = Path(os.environ.get("PORTBOARD_PROJECTS_ROOT", "/mnt/hyper/Projects"))

# Claude Code worktrees live at <repo>/.claude/worktrees/<slug>.
WORKTREE_DIRNAME = ".claude/worktrees"

# systemd --user transient units created by the runner: portboard-<project>-<label>.service
UNIT_PREFIX = "portboard-"

# The daemon (GUI + API + MCP) listens here on 127.0.0.1 only.
DAEMON_PORT = 8790
DAEMON_HOST = "127.0.0.1"

# Settings table defaults. Values are stored as strings in SQLite.
DEFAULT_SETTINGS: dict[str, str] = {
    "daemon_port": str(DAEMON_PORT),
    "pool_start": "4000",       # first base port handed to a project without its own port
    "pool_end": "4990",
    "pool_step": "10",          # base ports 4000, 4010, ... so slots 1..9 stay inside the block
    "slots": "9",               # worktree slots per project (base+1 .. base+slots)
    "memory_max": "4G",         # MemoryMax for transient units
    "idle_minutes": "90",       # unowned, connection-less, cpu-quiet for this long => idle stop (0 disables)
    "evening_stop": "16:30",    # OnCalendar time for the evening stop
    "morning_start": "07:30",   # OnCalendar time for the morning restart
    "morning_days": "Mon..Fri",
    "idle_exit_seconds": "600", # socket-activated daemon exits after this much inactivity
    "node_path": "",            # bin dir of the nvm node, detected by install; prepended to PATH in units
    "notify_on_schedule": "0",  # 1 = send a summary through ~/.claude/hooks/notify-hermes.py after schedule actions
    "reserved_static": "22,53,631,1234,8644,8790",  # never hand these out
    "start_timeout": "60",      # seconds to wait for LISTEN after start
}

# Kinds of projects and how the runner treats them.
# ``group``: a directory of sibling sub-projects (children carry parent_id); its
# main instance mirrors the primary child. ``container``: an existing docker
# container named in start_cmd (docker start/stop), e.g. hand-made dev stacks.
KINDS = ("transient", "unit", "compose", "container", "group", "none")
PORT_MODES = ("env", "arg", "fixed", "none")
STATES = ("stopped", "starting", "running", "failed", "unknown")

_LOG_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Rotating file log under STATE_DIR (1 MB x 3). Safe to call repeatedly."""
    global _LOG_CONFIGURED
    log = logging.getLogger("portboard")
    if _LOG_CONFIGURED:
        return log
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False
    _LOG_CONFIGURED = True
    return log
