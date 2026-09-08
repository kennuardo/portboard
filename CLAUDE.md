# Portboard

Local port/instance registry for dev servers. Python 3.14, standard library
only — no third-party packages, ever.

Run tests: `python3 -m unittest -v` (or `python3 -m unittest discover -s
tests -v`) from the repo root.

Tests must set `PORTBOARD_STATE_DIR` to a temp dir *before* importing
`portboard` (config resolves `DB_PATH` at import time).

GUI + API + MCP: `http://localhost:8790/` once `portboard serve` or the
socket-activated daemon is up.

NEVER call `install.run()` without `dry_run=True` in tests, and never point
`install.SYSTEMD_USER_DIR` / `LOCAL_BIN` / `CLAUDE_SETTINGS` at the real home.

Commit conventions are global (see `~/.claude/CLAUDE.md`).
