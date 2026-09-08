# Portboard design and module contract

Portboard is a small local service for one Linux workstation. It keeps a
registry of projects, the dev-server instances they run (main checkout and
Claude Code worktrees), and the TCP ports those instances hold. It starts and
stops instances through `systemd --user` and `docker compose`, shows a GUI,
exposes the same operations to Claude Code through MCP and hooks, and stops
projects on a schedule.

Hard constraint: near-zero idle cost. Nothing polls in a loop. The daemon is
socket-activated and exits when idle. State lives in SQLite. Reality is checked
on demand ("reconcile"), on hook events and on a 30-minute timer, never in a
tight loop.

Python 3.14, standard library only. No third-party packages.

## Layout

```
portboard/
  config.py     paths, constants, DEFAULT_SETTINGS, setup_logging()      (done)
  db.py         schema, connect(), settings helpers, add_event()         (done)
  registry.py   projects/instances CRUD, port allocation, path mapping   (agent A)
  discover.py   repo dir -> suggested project config                     (agent A)
  sysinfo.py    ss/proc/cgroup/docker readers, pure data                 (agent B)
  reconcile.py  reconcile(): observed snapshot, state updates, reserved  (agent B)
  runner.py     start/stop/restart/status/logs per kind                  (agent C)
  schedule.py   evening stop, morning start, tick (reconcile + idle)     (agent C)
  server.py     HTTP API + static GUI + socket activation + idle exit    (agent D)
  mcp.py        MCP JSON-RPC (streamable HTTP, stateless) + stdio shim   (agent D)
  hooks.py      Claude Code hook handlers                                (agent F)
  install.py    systemd units/timers/socket, CLI symlink, hooks, mcp add (agent F)
  cli.py        argparse entry point                                     (agent F)
  static/index.html  the GUI                                             (agent E)
tests/          unittest, one file per module, run: python3 -m unittest -v
bin/portboard   launcher (symlinked to ~/.local/bin/portboard by install)
```

Every module logs through `logging.getLogger("portboard.<module>")`; call
`config.setup_logging()` only from entry points (cli, server).

Subprocess calls always use `subprocess.run([...], capture_output=True,
text=True, timeout=...)` with an explicit timeout. Never `shell=True` except
for the user's `start_cmd`, which is passed to `/bin/sh -c` by systemd-run.

## Data model (see db.py for the exact schema)

* **project**: one repository. `path` is the main checkout. `kind` decides how
  the runner works: `transient` (systemd-run of `start_cmd`), `unit` (an
  existing systemd --user unit named in `start_cmd`, e.g. `sheron-dev.service`),
  `compose` (docker compose in `path`), `none` (we only observe, e.g. containers
  started by hand). `base_port` is the main checkout's port; worktree slot `n`
  gets `base_port + n`. `pinned=1` means schedule and idle rule never stop it.
* **instance**: one running location of a project: label `main` (slot 0, path =
  project.path) or a worktree slug (slot 1..slots, path =
  `<project.path>/.claude/worktrees/<slug>`). `managed=0` marks instances we
  discovered rather than started. `port` is the assignment, `actual_port` what
  reconcile saw last time.
* **observed**: last reconcile snapshot of every listening TCP port, with the
  owning pid, cwd, systemd unit or docker container, and the project/instance
  it was matched to (NULL = unknown).
* **reserved_ports**: ports the allocator must skip: static list from settings
  plus every observed port that matched no project.
* **events**: bounded audit log (2000 rows).
* **settings**: key/value strings; defaults in `config.DEFAULT_SETTINGS`.

JSON shapes returned by the API and MCP are the table rows as dicts (`db.rows`)
plus computed fields:

* instance gets `project` (name), `url` (`http://localhost:<port><open_path>`),
  `kind`, `pinned`, `worktree` (bool).
* project gets `instances` (list) when returned by `/api/state`.

Timestamps are local ISO strings from `db.now()`.

## Port allocation rules (registry.py)

1. A project registered with an explicit `base_port` keeps it if free.
2. Otherwise the allocator walks `pool_start .. pool_end` in `pool_step`
   increments and takes the first base whose whole block
   `base .. base+slots` is free.
3. "Free" means: not `projects.base_port` of another project, not
   `instances.port` of another instance, not in `reserved_ports`, not in
   `observed`, not currently bindable-fail (a quick `socket.bind` test on
   127.0.0.1 and 0.0.0.0 with SO_REUSEADDR off).
4. Worktree slot: first `n` in `1..slots` whose `base_port + n` is free by the
   same test; if the whole block is exhausted, fall back to any free port in
   the pool range (still recorded on the instance).
5. Ports must stay below 32768 (Linux ephemeral range starts there).

## Path mapping (registry.py)

`resolve_path(conn, path)` returns `(project, instance_or_None, label, slot_hint)`
for any absolute path: walk up from `path` until a directory equals a
`projects.path` or matches `<projects.path>/.claude/worktrees/<slug>`. Worktree
paths map to label `slug`; anything else inside the project maps to `main`.
Symlinks are resolved with `os.path.realpath` on both sides.

## Runner contract (runner.py)

```python
start(conn, instance_id, wait=True) -> dict   # returns refreshed instance dict
stop(conn, instance_id, reason="user") -> dict
restart(conn, instance_id) -> dict
status_many(conn, instance_ids=None) -> dict[int, dict]  # {id: {state, pid, mem_bytes, cpu_ns, actual_port, unit_active, ...}}
logs(conn, instance_id, lines=200) -> str
unit_name(project, instance) -> str            # portboard-<project>-<label>.service
```

* `transient`: `systemd-run --user --unit=<unit> --description="portboard <project> <label>"
  -p WorkingDirectory=<instance.path> -p KillMode=control-group -p TimeoutStopSec=15
  -p MemoryMax=<memory_max> --setenv=... /bin/sh -c '<start_cmd>'`.
  Do not pass `--collect`: a failed unit must stay visible as `failed` until we
  call `systemctl --user reset-failed <unit>`, which the runner does right
  before every start. Environment passed with `--setenv`: `PORT`, `NUXT_PORT`,
  `NITRO_PORT` (all = the instance port when `port_mode == "env"`),
  `PORTBOARD_INSTANCE=<id>`, `PORTBOARD_PROJECT=<name>`,
  `PATH=<path_prepend or settings.node_path>:/usr/local/bin:/usr/bin:/bin`,
  plus every key of `env_json`. Do not force HOST. When `port_mode == "arg"`
  replace `{port}` in `start_cmd` with the port instead of setting the env vars.
* `unit`: `systemctl --user start|stop <start_cmd>`; the port is `fixed`.
* `compose`: `docker compose --project-directory <path> up -d` (or `start_cmd`
  override) with env `PORT=<port>`, `COMPOSE_PROJECT_NAME=<project>` for main and
  `<project>-<label>` for worktrees. Stop: `docker compose ... stop` (not down).
* `none`: start raises `RunnerError("no start command")`; stop tries
  `docker stop <container>` when observed knows the container, else SIGTERM to
  the pid's process group, else error.
* After start with `wait=True`: poll `sysinfo.listening_ports()` every 0.5 s up
  to `settings.start_timeout` seconds until something owned by the unit's
  cgroup (transient/unit) or the compose project listens; record `actual_port`,
  `pid`, `state=running`, `started_at`. If the unit died: `state=failed`,
  include the last 20 journal lines in `RunnerError`. This bounded poll is the
  only polling loop in the code base.
* `status_many`: one `systemctl --user show -p Id,ActiveState,SubState,MainPID,
  MemoryCurrent,CPUUsageNSec,NRestarts,ExecMainStartTimestamp <units...>` call
  for all systemd-backed instances and one `docker ps --format '{{json .}}'`
  for compose ones. Never one subprocess per instance.
* `logs`: `journalctl --user -u <unit> -n <lines> --no-pager -o short-iso` or
  `docker compose ... logs --tail <lines> --no-color`.
* Every state change writes the instance row and an event.

`class RunnerError(Exception)` with a readable message.

## Reconcile contract (reconcile.py, sysinfo.py)

```python
sysinfo.listening_ports() -> list[Listener]      # dataclass: port, proto, bind, pid, comm
sysinfo.proc_info(pid) -> ProcInfo               # cwd, cmdline, unit, container (from /proc/<pid>/cgroup), uid
sysinfo.docker_containers() -> list[Container]   # name, id, state, ports [(host_port, container_port)], compose_project, compose_workdir, network_mode
sysinfo.established_count(port) -> int           # ss -tnH state established '( sport = :PORT )'
reconcile.reconcile(conn, quick=False) -> dict   # summary: {listeners, matched, unknown, changed, started, stopped, reserved}
```

* `listening_ports` parses `ss -ltnpH` (fall back to `-ltnH` when `-p` is
  unavailable). Root-owned sockets have no pid; keep them with pid None.
* `proc_info` reads `/proc/<pid>/cgroup`: a component ending in `.service` or
  `.scope` under `user@<uid>.service` is the systemd user unit; `docker-<id>.scope`
  is a container id. `cwd` via `os.readlink`, may raise PermissionError: return None.
* `docker_containers` runs `docker ps --format '{{json .}}'` once and
  `docker inspect` once for all ids (labels `com.docker.compose.project`,
  `com.docker.compose.project.working_dir`, `HostConfig.NetworkMode`,
  `NetworkSettings.Ports`). If docker is unavailable, return [] and log once.
  `quick=True` skips docker entirely (used by hooks to stay under 300 ms).
* Matching order per listener: instance whose `unit` equals the pid's unit or
  container/compose project; else instance/project by path prefix of `cwd` or
  `compose_workdir`; else unknown.
* Effects: rewrite `observed`; for matched instances set `state=running`,
  `pid`, `actual_port`, `last_seen_at`; for managed instances previously
  `running` but no longer seen set `state=stopped`, `stopped_by='crash'` unless
  a stop was recorded in the last 30 s; unknown listeners in a project
  directory become `managed=0` instances (label from the worktree slug or
  `main`, source `adopted`) ONLY when `adopt_unknown=True` (GUI adopt button);
  otherwise they stay in `observed` with `project_id` set and `instance_id`
  NULL. Unknown listeners outside any project refresh `reserved_ports`
  (source `observed`, label = comm or container). Rows in `reserved_ports`
  with source `observed` that were not seen this time are deleted.

## Schedule contract (schedule.py)

```python
evening_stop(conn) -> dict   # stops every running, unpinned, managed instance; stopped_by='schedule'; remembers ids in settings["schedule_last_stopped"] (JSON list)
morning_start(conn) -> dict  # starts the remembered ids whose project.autostart == 'schedule' and which are still stopped with stopped_by='schedule'
tick(conn) -> dict           # reconcile(); then idle rule: for running, unpinned, managed, unowned instances:
                             #   busy if established_count(port) > 0 or cpu_ns grew by more than 2 s since last tick;
                             #   idle_since set on first idle tick, cleared when busy; stop when idle for >= idle_minutes (0 disables)
```

Each returns `{stopped: [...], started: [...], skipped: [...], errors: [...]}`.
When `settings.notify_on_schedule == "1"` pipe a one-paragraph Slovak summary
to `python3 ~/.claude/hooks/notify-hermes.py` (stdin JSON `{"message": ...}`),
best effort, timeout 10 s.

## HTTP API (server.py)

Bound to `127.0.0.1:<daemon_port>` or inherited from systemd socket activation
(`LISTEN_FDS=1`, fd 3). `ThreadingHTTPServer`, no access log (override
`log_message`; log only errors). JSON bodies in and out, `{"error": "..."}`
with 4xx/5xx on failure. Idle exit: a daemon thread wakes every 60 s and calls
`shutdown()` when socket-activated, no request is in flight and the last
request was more than `idle_exit_seconds` ago.

```
GET  /                         GUI (static/index.html)
GET  /healthz                  {"ok": true, "version": ..., "pid": ..., "socket_activated": bool}
GET  /api/state                {projects:[{..., instances:[...]}], observed:[...], reserved:[...],
                                settings:{...}, reconciled_at, daemon:{...}}
POST /api/reconcile            {"quick": false, "adopt_unknown": false} -> same as /api/state
GET  /api/events?limit=100     [...]
POST /api/projects             project fields -> project
PATCH /api/projects/<id>       partial fields -> project
DELETE /api/projects/<id>      {"ok": true}   (stops instances first)
POST /api/projects/<id>/instances   {"path": "..."} or {"label": "..."} -> instance (worktree slot)
POST /api/instances/<id>/start      -> instance
POST /api/instances/<id>/stop       -> instance
POST /api/instances/<id>/restart    -> instance
POST /api/instances/<id>/release    -> instance (clears owner_session)
DELETE /api/instances/<id>          {"ok": true} (stop first; main instance cannot be deleted)
GET  /api/instances/<id>/logs?lines=200   {"text": "..."}
POST /api/observed/<port>/adopt     {"project_id": optional} -> instance
POST /api/observed/<port>/stop      {"ok": true}
POST /api/schedule/stop             schedule.evening_stop result
POST /api/schedule/start            schedule.morning_start result
GET  /api/discover?path=/abs/dir    discover.suggest(path)
POST /api/settings                  {key: value, ...} -> settings
POST /mcp                           MCP JSON-RPC (see below)
GET  /mcp                           405
```

## MCP (mcp.py)

Streamable HTTP, stateless: every POST carries one JSON-RPC request (or a
notification, answered with 202 and empty body). Respond with
`Content-Type: application/json`. Never require `Mcp-Session-Id`. Supported
methods: `initialize` (echo the client's `protocolVersion` if it is one of
`2025-06-18`, `2025-03-26`, `2024-11-05`, else `2025-06-18`; capabilities
`{"tools": {}}`; serverInfo name `portboard`), `notifications/initialized`,
`ping`, `tools/list`, `tools/call`. Unknown method: JSON-RPC error -32601.
Tool results: `{"content": [{"type": "text", "text": <json string>}],
"isError": bool}`.

Tools (all parameters optional unless noted; `cwd` is always an absolute path
the caller passes explicitly, the server cannot know it):

| tool | params | returns |
|---|---|---|
| `ports_list` | | observed + instances, compact |
| `port_whois` | `port` (required) | observed row + matched project/instance |
| `project_status` | `cwd` (required) | project, instances, `this` = the instance for cwd if any, assigned port |
| `project_claim` | `cwd` (required), `session_id` | registers unknown repos via discover, ensures an instance row for cwd (worktree slot if needed), sets owner; returns instance incl. url and a `start_hint` |
| `instance_start` | `cwd` or `instance_id`, `session_id` | started instance (waits for LISTEN) |
| `instance_stop` | `cwd` or `instance_id` | stopped instance |
| `reconcile` | `quick` | reconcile summary |
| `project_register` | `path` (required), `name`, `kind`, `start_cmd`, `base_port`, `port_mode` | project |

`mcp.py` also provides `stdio_main()` for `portboard mcp-stdio`: newline
delimited JSON-RPC on stdin/stdout, same handler, as a fallback transport.

## Hooks (hooks.py)

`portboard hook <event>` reads the hook JSON from stdin. Handlers never raise:
on any exception log it and exit 0 with no output, a broken hook must not block
Claude Code. Keep SessionStart under 300 ms: direct DB access, `reconcile(quick=True)`.

* `session-start`: fields `session_id`, `cwd`, `source`. Resolve cwd. Unknown
  git repo that looks like a project (package.json, compose file,
  pyproject.toml, requirements.txt, Makefile with a run target) -> register via
  discover with `source='hook'`. Ensure an instance row for cwd (main or
  worktree slot). Print plain text to stdout:

  ```
  [portboard] project sheron, checkout worktree fix-login (branch worktree-fix-login)
  assigned port 3301, test URL http://localhost:3301/
  running: main@3300 (unit sheron-dev.service, since 09:12, owner none); fix-login@3301 not running
  start it with the MCP tool instance_start (cwd=...) or: portboard start --cwd .
  ```
  Unknown non-project directory: print nothing.
* `session-end`: clear `owner_session` where it equals `session_id`.
* `pre-tool-use` (matcher Bash): `tool_input.command`. Detect dev-server
  launches with a regex over: `npm|pnpm|yarn|bun (run )?(dev|start|serve|preview)`,
  `nuxt dev|nuxi dev`, `vite`, `next dev`, `astro dev`, `uvicorn`, `flask run`,
  `docker compose up`, `python -m http.server`. Extract a port from `--port N`,
  `-p N`, `PORT=N`. If cwd maps to a project and (the explicit port belongs to a
  different instance or project, or no port is given and the project's assigned
  port for this checkout is already held by something else) -> deny:
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
  "permissionDecisionReason": "<why>. Use MCP instance_start or run: PORT=<n> <cmd>"}}`.
  Otherwise exit 0 silently.
* `cwd-changed` (event `CwdChanged`, fires on `cd` and on entering a worktree;
  no matcher, notification only): read `cwd` (also accept `new_cwd` if present;
  log the payload keys at INFO once). If cwd resolves to a known project and is
  a worktree path without an instance row, create the row (slot + port) so the
  GUI shows it before anything runs. Print nothing.
* `worktree-remove` (event `WorktreeRemove`, informational, fields
  `session_id`, `cwd`, `worktree_path`): stop and delete the instance whose
  path equals `worktree_path`; also `docker compose stop` is covered by the
  runner for compose instances. Print nothing.
* NOT used: `WorktreeCreate`. Per the docs it "replaces default git behavior":
  a registered hook must create the worktree itself and print its path, and any
  non-zero exit aborts creation. Portboard must never own worktree creation.

## Install (install.py)

`portboard install` is idempotent and prints what it did:

1. symlink `~/.local/bin/portboard` -> `bin/portboard`.
2. detect `node_path` (dir of `which node` from the caller's env, or the newest
   `~/.nvm/versions/node/*/bin`) into settings.
3. write units to `~/.config/systemd/user/`:
   `portboard.socket` (ListenStream=127.0.0.1:<port>, NoDelay=true),
   `portboard.service` (ExecStart=<python3> <repo>/bin/portboard serve,
   Environment=PYTHONUNBUFFERED=1, Nice=5, MemoryMax=300M, no Restart),
   `portboard-evening.service/.timer` (OnCalendar=*-*-* <evening_stop>:00),
   `portboard-morning.service/.timer` (OnCalendar=<morning_days> *-*-* <morning_start>:00),
   `portboard-tick.service/.timer` (OnBootSec=10min, OnUnitActiveSec=30min, AccuracySec=5min),
   all with Persistent=false; then `daemon-reload`, `enable --now` the socket
   and timers.
4. `--mcp`: `claude mcp add --transport http --scope user portboard http://127.0.0.1:<port>/mcp`
   (skip if already registered: check `claude mcp get portboard`).
5. `--hooks`: merge into `~/.claude/settings.json` (backup to
   `settings.json.bak-<ts>` first) hook entries for SessionStart, SessionEnd,
   PreToolUse (matcher `Bash`), WorktreeCreate, WorktreeRemove, each
   `{"type": "command", "command": "portboard hook <event>", "timeout": 10}`.
   Never duplicate an entry that already has the same command.
6. `--discover`: run discover over PROJECTS_ROOT and register what has a clear
   stack, without starting anything.

## GUI (static/index.html)

Single file, vanilla JS, no CDN. Fetches `/api/state` on load, on the
Reconcile button, after every action and on `visibilitychange` to visible (with
a 5 s floor). No timers otherwise. Sections: header (daemon status, last
reconcile, Reconcile button, "Stop all now" and "Start morning set" buttons),
Projects (cards with instances: label, branch, port, state, RAM, CPU, owner,
buttons Start/Stop/Restart/Open/Copy link/Logs/Pin), Ports (table of observed
listeners: port, bind, process, project/instance or "unknown", Adopt/Stop),
Events (last 50). Open uses `target="_blank" rel="noopener"`. Light and dark
via `prefers-color-scheme`. Register-project form (path, name, kind, start
command, port). Settings drawer for evening/morning times, idle minutes,
pool range. Copy link falls back to a prompt when clipboard API is missing.

## Discover (discover.py)

`suggest(path) -> dict | None` with keys matching project columns plus
`confidence` and `evidence` (list of strings). Rules, first match wins:

* compose file (`compose.yaml|yml`, `docker-compose.yaml|yml`) at root and no
  package.json dev script -> kind `compose`, port_mode `fixed`, base_port =
  first published host port of a service named web/frontend/app/api, else the
  lowest published port.
* package.json with `scripts.dev` -> kind `transient`, start_cmd `npm run dev`
  (`pnpm`/`yarn`/`bun` when their lockfile exists), port_mode `env`; base_port
  from `nuxt.config.*` `devServer.port`, `vite.config.*` `server.port`, a
  `-p N`/`--port N` in the dev script, or `.env` `PORT=` (read only that key);
  else None (allocator decides). Nuxt 2 (`nuxt` dep < 3) -> also env.
* pyproject/requirements with uvicorn/fastapi -> `transient`, start_cmd
  `<venv python> -m uvicorn <app.module:app> --port {port}` when an obvious
  `app/main.py` or `main.py` with `app = FastAPI()` exists, port_mode `arg`;
  `run.sh` at root -> start_cmd `./run.sh`, port_mode `env`.
* `Makefile` with `run`/`dev`/`up` target -> `transient`, `make <target>`, env.
* else None.

`name` = directory basename lowercased, non `[a-z0-9-]` replaced by `-`.

## CLI (cli.py)

```
portboard list [--json]                 instances + observed summary
portboard whois <port>
portboard status [--cwd DIR]
portboard claim --cwd DIR [--session ID]
portboard start|stop|restart [<project>[@<label>] | --cwd DIR | --id N] [--no-wait]
portboard open [<project>[@label] | --cwd DIR]        xdg-open the url
portboard logs [<project>[@label] | --id N] [-n 200]
portboard reconcile [--quick] [--adopt]
portboard adopt <port> [--project NAME]
portboard project add PATH [--name N --kind K --start CMD --port P --port-mode M --pinned]
portboard project edit NAME [same flags]
portboard project rm NAME
portboard project pin|unpin NAME
portboard project show NAME
portboard discover [ROOT] [--apply]
portboard schedule stop|start
portboard tick
portboard serve [--port N]
portboard mcp-stdio
portboard hook <session-start|session-end|pre-tool-use|worktree-create|worktree-remove>
portboard install [--mcp] [--hooks] [--discover] [--all]
portboard export > file.json / portboard import file.json
```

Exit code 0 on success, 1 on a handled error (message on stderr), `--json`
prints machine-readable output on stdout.
