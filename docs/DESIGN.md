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
  `compose` (docker compose in `path`), `container` (an existing docker
  container named in `start_cmd`, e.g. a hand-made dev stack; worktree
  instances use `<name>-<label>`), `group` (a directory of sibling
  sub-projects, e.g. `/mnt/hyper/Projects/rma` holding `admin-app`,
  `customer-app`, `server-side`; see below), `none` (we only observe).
  `base_port` is the main checkout's port; worktree slot `n` gets
  `base_port + n`. `pinned=1` means schedule and idle rule never stop it.
* **groups** (`kind='group'`): the parent project at the group directory.
  Children are ordinary projects with `projects.parent_id` set to the group's
  id; a child registered without an explicit name is called
  `<group>-<basename>`. Groups are forced to `port_mode='none'`, `base_port =
  NULL`, `start_cmd = NULL` (registry.add_project/update_project enforce this
  whenever `kind == 'group'`); they never hold a port or start command of
  their own. Nested groups are refused (a group cannot itself have
  `parent_id` set, and a project with `parent_id` cannot have `kind='group'`).
  `projects.primary_child` optionally names the child whose port/url/state
  the group's main instance shows; when NULL, `discover.pick_primary` picks
  one by a name-token heuristic (frontend/web/admin/client/app score highest,
  api/server/backend/db.../worker/tools score negative; ties broken by
  lowest port, then name). `registry.get_project`/`list_projects` decorate
  every project with `parent` (parent name or None) and decorate groups
  further with `children` (child ids in display order), `child_names`,
  `primary_child_id` (explicit or heuristic) and `primary` (its name).
  Helpers: `group_children`, `primary_child`, `group_start_order` (display
  order with the primary moved last, for starting children frontend-last),
  `add_group(conn, suggestion, source, allow_busy)` (registers a group plus
  every child from a `discover.suggest_group()` result in one call; a child
  whose path is already registered is re-parented rather than duplicated;
  the primary is taken from `suggestion["primary"]`, a child name).
  `delete_project` on a group cascades to its children. A group's main
  **instance** is a view, not a stored row: it mirrors the primary child's
  main instance (`state`, `port`, `actual_port`, `url`, `pid`, `started_at`,
  `stopped_at`, `stopped_by`, `mem_bytes`, `cpu_ns`, `idle_since`, `unit`,
  `managed`) and adds `services` (every child's main instance view, display
  order), `primary`, `primary_instance_id`, `services_running`,
  `services_total`. The underlying `instances` row for the group (slot 0,
  port NULL) is never written to by this mirroring — only computed on read.
  `resolve_path` returns `Resolved.group` (the decorated parent group dict)
  for any path inside a child; a path inside the group directory but outside
  every child resolves to the group project itself (`group=None` on that
  result, since the group has no parent of its own). Export writes `parent`
  and `primary` as names, not ids, and orders groups before their children so
  import can resolve `parent` by name in one pass (a second pass resolves
  `primary`, since a child may be created after its group in the same
  import).
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
Symlinks are resolved with `os.path.realpath` on both sides. `Resolved.group`
carries the decorated `kind='group'` parent when `project` is a child of one
(None otherwise, including for the group project itself).

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
* `container`: `start_cmd` is the docker container name (`<name>-<label>` for
  worktree instances). start: `docker start <name>`; stop: `docker stop
  <name>`; logs: `docker logs --tail <lines> <name>`; wait (when `wait=True`)
  matches by container id/name via `sysinfo.docker_containers()` rather than
  by cgroup, since the container is not something we `systemd-run`.
* `group`: start runs `start()` on every child in
  `registry.group_start_order(conn, group)` order (display order, primary
  last, so the frontend a human opens comes up once its dependencies are
  already listening); children with `kind='none'` are skipped (no start
  command). Stop runs the children in reverse order. `logs` concatenates each
  child's logs, headed by the child's name. The group's own `instances` row
  (slot 0) is never written by the runner — its view is always the live
  mirror computed by `registry._instance_view`/`_mirror_primary`, so there is
  nothing to start/stop/reconcile for the group row itself.
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
  `compose_workdir`; else unknown. `kind='container'` instances are matched
  purely by container name (`instances.unit` = the container name), never by
  `cwd`/path prefix, since a hand-made container's working directory has no
  reliable relationship to the project path. `kind='group'` projects are
  never matched or written to directly — reconcile only ever touches their
  children's instance rows; the group's own row does not exist to reconcile
  against (see Runner contract above).
* Root-owned host-network containers: `ss` shows no pid and docker publishes
  no port, so a pid-less listener is attributed to a `kind='container'`
  instance whose container is running when its port is the instance's
  assigned port, the port the container's own command/env names
  (`sysinfo.Container.cmd_port`, `--port N` / `PORT=N`, last one wins) or,
  for worktree instances, `discover.detect_port_from_repo(instance.path)`
  (rma-dev.sh writes `server.port=` into the worktree's config). A running
  container with no visible listener is still `running` (pid from docker,
  `actual_port` NULL): docker is the truth for containers.
* Full (non-quick) reconcile first runs `registry.sync_worktrees(conn)`: one
  listdir of `<project>/.claude/worktrees` per project, creating instance rows
  (slot + port) for directories that have none, so stacks started by hand or
  before registration show up without a Claude session hook. Never deletes.
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

`kind='group'` instances (there is no real instance row for a group, see
Runner contract) are skipped by both `evening_stop`/`morning_start` and the
`tick` idle rule — iterate `projects` in both by kind, filtering out `group`
before touching instances. Their children are ordinary managed instances and
are stopped/started/idle-checked individually like any other project; a
child being pinned or unpinned is independent of its siblings and of the
group.

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
                                kind=group: fields must be a discover.suggest_group()-shaped
                                body (children, primary) -> registry.add_group(); anything
                                else with kind=group is refused (use suggest_group first)
PATCH /api/projects/<id>       partial fields -> project
                                primary_child accepts a child id or name, validated as a
                                child of that group; parent_id accepts a group id, validated
                                (not nested, not the project's own id)
DELETE /api/projects/<id>      {"ok": true}   (stops instances first; a group deletes its children too)
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
GET  /api/discover?path=/abs/dir    discover.suggest(path), or discover.suggest_group(path)
                                     when the path is a group directory (no .git, >=2 recognised
                                     sibling repos) rather than a repository itself
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
| `project_status` | `cwd` (required) | project (incl. `parent`/`group` when `cwd` is inside a group's child, and `children`/`services` when `cwd` is the group itself), instances, `this` = the instance for cwd if any, assigned port |
| `project_claim` | `cwd` (required), `session_id` | registers unknown repos via discover, ensures an instance row for cwd (worktree slot if needed), sets owner; returns instance incl. url and a `start_hint`; a `cwd` inside an as-yet-unregistered group directory registers the whole group via `discover.suggest_group` + `registry.add_group` before claiming |
| `instance_start` | `cwd` or `instance_id`, `session_id` | started instance (waits for LISTEN); starting a group's instance id starts its children in `group_start_order` |
| `instance_stop` | `cwd` or `instance_id` | stopped instance |
| `reconcile` | `quick` | reconcile summary |
| `project_register` | `path` (required), `name`, `kind`, `start_cmd`, `base_port`, `port_mode`, `parent`, `primary` | project; `kind=group` registers via `discover.suggest_group(path)` + `registry.add_group` (ignoring the port/start_cmd fields, which groups never take); `parent` (a group name) registers `path` as a child of that group; `primary` (a child name) is only meaningful together with `kind=group` |

`mcp.py` also provides `stdio_main()` for `portboard mcp-stdio`: newline
delimited JSON-RPC on stdin/stdout, same handler, as a fallback transport.

## Hooks (hooks.py)

`portboard hook <event>` reads the hook JSON from stdin. Handlers never raise:
on any exception log it and exit 0 with no output, a broken hook must not block
Claude Code. Keep SessionStart under 300 ms: direct DB access, `reconcile(quick=True)`.

* `session-start`: fields `session_id`, `cwd`, `source`. Resolve cwd. Unknown
  git repo that looks like a project (package.json, compose file,
  pyproject.toml, requirements.txt, Makefile with a run target) -> register via
  discover with `source='hook'`. A `cwd` that sits inside a directory
  `discover.group_of(cwd)` recognises as a group, and that group is not
  registered yet, auto-registers the whole group (`registry.add_group`,
  `source='hook'`) before resolving cwd's own project, so the repo the user
  opened comes up already parented. Ensure an instance row for cwd (main or
  worktree slot). Print plain text to stdout:

  ```
  [portboard] project sheron, checkout worktree fix-login (branch worktree-fix-login)
  assigned port 3301, test URL http://localhost:3301/
  running: main@3300 (unit sheron-dev.service, since 09:12, owner none); fix-login@3301 not running
  start it with the MCP tool instance_start (cwd=...) or: portboard start --cwd .
  ```
  When `resolve_path` reports a `group` for cwd's project, print an extra
  line naming it and the group's current state:

  ```
  part of group rma: primary rma-admin-app :3100 (running); also rma-api :3101, rma-mariadb :3102
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
   stack, without starting anything. `scan()` reports a group directory as one
   `kind='group'` suggestion instead of descending into its children, so this
   registers via `registry.add_group` (children included) rather than one
   `add_project` per sibling repo.

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

A `kind='group'` project renders as one card at the top-level grid, showing
the mirrored primary child's port/state/url like any other project card
(Start/Stop/Restart act on the whole group, in `group_start_order`). Inside
that card, a **Services** block lists every child from `services` (name,
port, state, its own Start/Stop/Logs) so the whole group is visible and
controllable without leaving the card; a child that is listening but is not
the primary gets an "also listening on http://localhost:<port>" notice next
to the group's main url. Children are never rendered as their own top-level
cards in the grid — `list_projects` still returns them (for `/api/projects`
callers and the CLI), but the GUI filters out any project with `parent` set
before laying out the grid, and renders it only inside its group's card.

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
* `pom.xml` + `mvnw` at root (Spring Boot, no `run.sh`) -> `transient`,
  start_cmd `./mvnw spring-boot:run`, port_mode `env` (Spring Boot reads
  `SERVER_PORT`), confidence `medium`.
* `Makefile` with `run`/`dev`/`up` target -> `transient`, `make <target>`, env.
* repository with a manifest (`package.json`, `pyproject.toml`,
  `requirements.txt`, `setup.py`/`.cfg`, `go.mod`, `Cargo.toml`,
  `composer.json`, `Gemfile`, `mix.exs`, `Procfile`, `pom.xml`,
  `build.gradle[.kts]`) but none of the above start commands -> kind `none`,
  registered anyway (port allocated if there is evidence) so it shows up and
  the user fills in a start command later.
* else None.

Port evidence, in addition to the framework-specific rules above: Spring's
`server.port` (`server.port=N` or YAML `server:\n  port: N`) in
`src/main/resources/application.properties`, `application.yml`, or
`config/application.properties` -> confidence `medium`.

`name` = directory basename lowercased, non `[a-z0-9-]` replaced by `-`.

### Groups (`suggest_group`, `group_of`)

`suggest_group(path) -> dict | None`: `path` must exist, not itself be a git
repository, and not already `suggest()` as a project (a plain directory of
sibling repos, not a repo of its own — e.g. `/mnt/hyper/Projects/rma` holding
`admin-app`, `customer-app`, `server-side` as separate checkouts). Its
immediate subdirectories that are git repositories (`has_git`) with a
`suggest()` result become `children`, each renamed `<group>-<original name>`;
fewer than `GROUP_MIN_CHILDREN` (2) such children -> None. Result: kind
`group`, port_mode `none`, `children` (the renamed `suggest()` results),
`primary` (`pick_primary(children)`'s name), confidence `medium`, evidence
listing the sub-projects and the primary guess.

`pick_primary(children) -> dict | None` / `primary_score(name, base_port,
kind) -> int`: sum `PRIMARY_TOKENS` scores of the name's `[^a-z0-9]+`-split
tokens (frontend/web/ui/admin/client/app/customer/portal/dashboard score
positive, api/server/backend/service/worker/db/database/mariadb/mysql/
postgres/redis/mail/mailpit/tools score negative or strongly negative),
`-1` when `kind == 'none'` (can't be started by us), `+1` when the child has
a `base_port`. Highest score wins; ties broken by lowest `base_port` (a
child with no port sorts last), then by name.

`group_of(path, projects_root=None) -> dict | None`: the group suggestion
for the directory holding `path` (a repo, or the group directory itself).
`None` when `path == projects_root`, when `path`'s parent is `projects_root`
or `/` (a plain top-level repo is never treated as a lone-child group), or
when the parent has fewer than 2 recognisable repos. Called with the group
directory itself (no `.git`, not a project) it returns `suggest_group(path)`
directly rather than looking at its parent.

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
                                     [--parent GROUP]                 register as a child of GROUP
                                     [--kind group]                   run discover.suggest_group(PATH)
                                                                       + registry.add_group instead of
                                                                       a plain add_project; --parent/
                                                                       --start/--port/--port-mode refused
portboard project edit NAME [same flags] [--primary CHILD]           group only: set/clear the
                                                                       explicit primary_child (name or id)
portboard project rm NAME                                            a group removes its children too
portboard project pin|unpin NAME
portboard project show NAME                                          a group also lists its children
                                                                       and which one is primary
portboard discover [ROOT] [--apply]                                  a group directory prints one
                                                                       suggestion (kind group, its
                                                                       children) instead of one per repo
portboard schedule stop|start
portboard tick
portboard serve [--port N]
portboard mcp-stdio
portboard hook <session-start|session-end|pre-tool-use|worktree-create|worktree-remove>
portboard install [--mcp] [--hooks] [--discover] [--all]
portboard export > file.json / portboard import file.json
```

`portboard list` renders a group as one line (its mirrored state/port) with
its children indented underneath, rather than the group and every child as
independent top-level rows.

Exit code 0 on success, 1 on a handled error (message on stderr), `--json`
prints machine-readable output on stdout.
