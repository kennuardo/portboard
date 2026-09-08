# Portboard

Portboard is a small local service for one Linux workstation. It keeps a
registry of projects, the dev-server instances they run (main checkout and
Claude Code worktrees), and the TCP ports those instances hold. It starts and
stops instances through `systemd --user` and `docker compose`, shows a GUI,
exposes the same operations to Claude Code through MCP and hooks, and stops
projects on a schedule so idle dev servers don't sit around burning RAM.

Python 3.14, standard library only. No third-party packages.

## How it works

- **Registry** (`portboard/registry.py`): a SQLite database of projects and
  instances. A project is one repository; an instance is one running location
  of it (the main checkout, or a Claude Code worktree at
  `<repo>/.claude/worktrees/<slug>`). Each instance gets its own port —
  worktree slot `n` gets `base_port + n`.
- **Reconcile** (`portboard/reconcile.py`, `portboard/sysinfo.py`): on demand,
  reads `ss`, `/proc`, and `docker ps` once, matches what's actually listening
  against the registry, and updates instance state. Never runs in a loop.
- **Runner** (`portboard/runner.py`): starts and stops instances via
  `systemd-run --user` (transient units), `systemctl --user` (existing
  units), or `docker compose` — whichever the project's `kind` says.
- **Socket-activated daemon** (`portboard/server.py`): a `systemd --user`
  socket starts the HTTP API + GUI + MCP endpoint on first connection and the
  daemon exits after a period of inactivity. Nothing runs when idle.
- **MCP** (`portboard/mcp.py`): the same operations exposed as MCP tools over
  streamable HTTP (and a stdio shim), so Claude Code can look up and manage
  ports directly.
- **Hooks** (`portboard/hooks.py`): fast, best-effort Claude Code hooks that
  announce a project's assigned port at session start, keep worktree
  instances in sync, and stop a Bash dev-server command from clobbering a
  port another instance already owns.
- **Timers**: an evening stop (unpinned instances) and morning restart
  (whatever was running at evening-stop time), plus a 30-minute tick that
  reconciles reality and stops instances that have been idle for a while.

## Install

```
python3 bin/portboard install --all
```

This is idempotent and prints what it does:

1. Symlinks `~/.local/bin/portboard` to `bin/portboard`.
2. Detects your `node_path` (nvm-aware) and stores it in settings, so
   transient units can find `npm`/`npx`/etc.
3. Writes `portboard.socket`, `portboard.service`, and the
   evening/morning/tick service+timer pairs to
   `~/.config/systemd/user/`, then `daemon-reload`s and enables the socket
   and timers.
4. `--mcp` registers `portboard` as an HTTP MCP server with
   `claude mcp add` (skipped if already registered).
5. `--hooks` merges `SessionStart`, `SessionEnd`, `PreToolUse` (matcher
   `Bash`), `CwdChanged` and `WorktreeRemove` hook entries into
   `~/.claude/settings.json`, after backing it up. Never duplicates an
   entry with the same command.
6. `--discover` scans `PORTBOARD_PROJECTS_ROOT` (default
   `/mnt/hyper/Projects`) and registers repositories with an obvious dev
   stack, without starting anything.

Run individual flags (`--mcp`, `--hooks`, `--discover`) instead of `--all` to
opt into only some of these. Nothing here starts a dev server.

## CLI cheat sheet

```
portboard list [--json]                          instances + observed ports
portboard whois <port>                           what's listening on a port
portboard status [--cwd DIR]                      project mapped to a directory
portboard claim --cwd DIR [--session ID]          register/ensure + set owner
portboard start|stop|restart [ref | --cwd D | --id N] [--no-wait]
portboard open [ref | --cwd DIR]                  xdg-open the instance's url
portboard logs [ref | --id N] [-n 200]
portboard reconcile [--quick] [--adopt]
portboard adopt <port> [--project NAME]
portboard project add PATH [--name --kind --start --port --port-mode --pinned]
portboard project edit|rm|pin|unpin|show NAME
portboard discover [ROOT] [--apply]
portboard schedule stop|start
portboard tick
portboard serve [--port N]
portboard mcp-stdio
portboard hook <session-start|session-end|pre-tool-use|cwd-changed|worktree-remove>
portboard install [--mcp] [--hooks] [--discover] [--all]
portboard export > file.json / portboard import file.json
```

`ref` is `project` or `project@label`. `--json` before the subcommand switches
to machine-readable output. Exit code 0 on success, 1 on a handled error
(message on stderr), 2 on a bad command line.

## How Claude Code uses it

**Hooks** (via `portboard install --hooks`):

| event | what it does |
|---|---|
| SessionStart | resolves `cwd`, auto-registers an unknown git repo that looks like a project, prints the assigned port and run status |
| SessionEnd | releases any instance owned by this session |
| PreToolUse (Bash) | denies a dev-server command that would grab a port another instance already holds |
| CwdChanged | backfills the instance row for a worktree path so it shows in the GUI before anything runs |
| WorktreeRemove | stops and deletes the instance for a removed worktree |

**MCP tools** (via `portboard install --mcp`, HTTP at `/mcp`):

| tool | purpose |
|---|---|
| `ports_list` | observed + registered instances, compact |
| `port_whois` | what's on a given port |
| `project_status` | project + instances for a `cwd` |
| `project_claim` | register (if unknown) + ensure instance + set owner |
| `instance_start` / `instance_stop` | start or stop, waits for LISTEN |
| `reconcile` | re-check reality against the registry |
| `project_register` | register a project explicitly |

## Acceptance behaviours

1. **Evening stop**: unpinned, managed, running instances stop at
   `settings.evening_stop`; the stopped set is remembered.
2. **Morning restart**: the same set restarts at `settings.morning_start` on
   `settings.morning_days`, provided the project's `autostart` is `schedule`
   and nothing else started it meanwhile.
3. **Pinned untouched**: `pinned=1` projects are never stopped by schedule or
   the idle rule.
4. **One-click start**: the GUI or CLI starts an instance on its assigned
   port and waits for it to actually listen before reporting success.
5. **Auto-registration**: opening Claude Code in an unregistered git repo
   that looks like a project registers it at `SessionStart`.
6. **GUI overview**: `http://localhost:8790/` lists every project, instance,
   port, and unmatched listener.
7. **Worktree link**: a worktree instance's `url` opens the same app on its
   own port in a new tab.

## Resource design

Nothing runs when idle:

- The daemon is socket-activated (`portboard.socket`) and exits after
  `idle_exit_seconds` of no requests — it is not resident otherwise.
- The only recurring work is three `systemd --user` timers: evening stop,
  morning start, and a tick every 30 minutes (`OnUnitActiveSec=30min`,
  `AccuracySec=5min`) that reconciles reality and applies the idle-stop rule.
- Reconcile makes a small, fixed number of subprocess calls (one `ss`, one
  `docker ps` + one `docker inspect`, one `systemctl --user show` for all
  units) — never one call per instance, never a polling loop.
- The only loop anywhere in the codebase is the bounded 0.5 s poll after
  `start()`, capped at `settings.start_timeout` seconds.
- Registered dev servers themselves are the only things that keep running
  between ticks, and only the unpinned ones the schedule/idle rule allows.

## Troubleshooting

- Daemon logs: `journalctl --user -u portboard.service`
- Portboard's own log file: `~/.local/state/portboard/portboard.log` (rotated,
  1 MB x 3; overridable with `PORTBOARD_STATE_DIR`)
- Timers: `systemctl --user list-timers 'portboard-*'`
- Force a reality check: `portboard reconcile` (`--quick` skips docker,
  `--adopt` turns unmatched listeners into `managed=0` instances)
- Socket not activating: `systemctl --user status portboard.socket`
