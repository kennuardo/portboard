"""HTTP API, static GUI, socket activation and idle exit.

The daemon binds 127.0.0.1 only (or inherits the listening socket from
systemd socket activation) and answers JSON on /api/*, the MCP JSON-RPC
endpoint on /mcp and the single-file GUI on /.

Near-zero idle cost: no polling anywhere. One daemon thread wakes every 60 s
and shuts the server down when it was socket-activated, nothing is in flight
and the last request is older than settings.idle_exit_seconds.

Modules that touch the registry, the runner or reconcile are imported lazily
inside the handlers so importing this module stays cheap (the CLI imports it
for `portboard serve` only) and so tests can patch them.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import signal
import socket
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import config, db

log = logging.getLogger("portboard.server")

# /api/state refreshes the snapshot with a quick (ss-only, ~70 ms) reconcile when
# it is older than this. The GUI itself re-fetches only on load and on tab
# visibility (5 s floor), so this bounds the work to one ss pass per 15 s.
STATE_RECONCILE_MAX_AGE = 15

MAX_BODY = 1024 * 1024
IDLE_TICK_SECONDS = 60

# Patched by tests; read on every request so the GUI can be edited live.
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "localhost.localdomain", ""}


class NotFound(Exception):
    """Raised by a handler when the addressed row does not exist -> 404."""


class Raw:
    """A non-JSON response (static file, MCP passthrough)."""

    def __init__(self, status: int, content_type: str, body: bytes, headers: dict[str, str] | None = None):
        self.status = status
        self.content_type = content_type
        self.body = body
        self.headers = headers or {}


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

class PortboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self.lock = threading.Lock()
        self.inflight = 0
        self.last_request = time.monotonic()
        self.started_at = time.time()
        self.socket_activated = False
        self.idle_exit_seconds: int | None = None
        self.stopping = False
        super().__init__(*args, **kwargs)

    # inflight / last_request bookkeeping -----------------------------------
    def enter_request(self) -> None:
        with self.lock:
            self.inflight += 1
            self.last_request = time.monotonic()

    def leave_request(self) -> None:
        with self.lock:
            self.inflight = max(0, self.inflight - 1)
            self.last_request = time.monotonic()

    def idle_snapshot(self) -> tuple[float, int]:
        with self.lock:
            return self.last_request, self.inflight

    def daemon_info(self) -> dict[str, Any]:
        last, _ = self.idle_snapshot()
        idle_in: float | None = None
        if self.socket_activated and self.idle_exit_seconds:
            idle_in = max(0.0, self.idle_exit_seconds - (time.monotonic() - last))
            idle_in = round(idle_in, 1)
        return {
            "pid": os.getpid(),
            "version": config.VERSION,
            "socket_activated": self.socket_activated,
            "uptime_s": round(time.time() - self.started_at, 1),
            "idle_exit_in_s": idle_in,
        }


def should_exit(now: float, last_request: float, inflight: int,
                idle_seconds: int | None, socket_activated: bool) -> bool:
    """Pure idle-exit decision (kept separate so it can be tested)."""
    if not socket_activated:
        return False
    if not idle_seconds or idle_seconds <= 0:
        return False
    if inflight > 0:
        return False
    return (now - last_request) > idle_seconds


def make_server(host: str, port: int, socket_fd: int | None = None) -> PortboardServer:
    """Build the server, either binding host:port or adopting an inherited fd."""
    if socket_fd is None:
        srv = PortboardServer((host, port), Handler)
        srv.socket_activated = False
        return srv

    inherited = socket.socket(fileno=socket_fd)
    cls = type("PortboardServerFd", (PortboardServer,), {"address_family": inherited.family})
    srv = cls((host, port), Handler, bind_and_activate=False)
    try:
        srv.socket.close()
    except OSError:
        pass
    srv.socket = inherited
    addr = inherited.getsockname()
    srv.server_address = addr
    srv.server_name = str(addr[0])
    srv.server_port = int(addr[1]) if len(addr) > 1 else port
    srv.socket_activated = True
    return srv


def _idle_loop(server: PortboardServer) -> None:
    while not server.stopping:
        time.sleep(IDLE_TICK_SECONDS)
        if server.stopping:
            return
        last, inflight = server.idle_snapshot()
        if should_exit(time.monotonic(), last, inflight, server.idle_exit_seconds, server.socket_activated):
            log.info("idle for more than %ss, shutting down", server.idle_exit_seconds)
            server.stopping = True
            server.shutdown()  # safe: this is not the serve_forever thread
            return


def serve(port: int | None = None, host: str = config.DAEMON_HOST, idle_exit: int | None = None) -> int:
    """Entry point for `portboard serve`. Returns a process exit code."""
    config.setup_logging()
    activated = os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid())

    conn = db.connect()
    try:
        settings = db.all_settings(conn)
    finally:
        conn.close()
    if idle_exit is None:
        try:
            idle_exit = int(settings.get("idle_exit_seconds") or 0)
        except ValueError:
            idle_exit = 600
    if port is None:
        try:
            port = int(settings.get("daemon_port") or config.DAEMON_PORT)
        except ValueError:
            port = config.DAEMON_PORT

    server = make_server(host, port, socket_fd=3 if activated else None)
    server.idle_exit_seconds = idle_exit

    def _term(_signum, _frame):
        log.info("SIGTERM, shutting down")
        server.stopping = True
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
    except ValueError:  # not the main thread
        pass

    threading.Thread(target=_idle_loop, args=(server,), name="portboard-idle", daemon=True).start()
    log.info("serving on %s:%s (socket_activated=%s, idle_exit=%ss)",
             host, server.server_port, server.socket_activated, idle_exit)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.stopping = True
        server.server_close()
    return 0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def host_allowed(host_header: str | None) -> bool:
    """DNS-rebinding guard: only localhost names may talk to the daemon."""
    if not host_header:
        return True  # HTTP/1.0 clients may omit Host
    h = host_header.strip()
    if h.startswith("["):
        end = h.find("]")
        name = h[1:end] if end > 0 else h
    else:
        name = h.split(":", 1)[0]
    return name.lower() in ALLOWED_HOSTS


def _error_status(exc: BaseException) -> int:
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, LookupError):  # KeyError / IndexError
        return 404
    for mod_name, attr in (("registry", "RegistryError"), ("runner", "RunnerError")):
        try:
            mod = importlib.import_module(f".{mod_name}", __package__)
        except Exception:
            continue
        cls = getattr(mod, attr, None)
        if isinstance(cls, type) and isinstance(exc, cls):
            return 400
    if isinstance(exc, ValueError):
        return 400
    return 500


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc) or exc.__class__.__name__


def _lazy(name: str):
    return importlib.import_module(f".{name}", __package__)


def _int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"not an integer: {value!r}")


def _instance_url(project: dict | None, inst: dict) -> str | None:
    if inst.get("url"):
        return inst["url"]
    port = inst.get("port") or inst.get("actual_port")
    if not port:
        return None
    open_path = (project or {}).get("open_path") or "/"
    if not open_path.startswith("/"):
        open_path = "/" + open_path
    return f"http://localhost:{port}{open_path}"


# --------------------------------------------------------------------------
# route handlers: fn(handler, match) -> (status, obj) | obj | Raw
# --------------------------------------------------------------------------

def _h_index(h: "Handler", m: re.Match) -> Raw:
    path = STATIC_DIR / "index.html"
    try:
        body = path.read_bytes()
    except OSError:
        raise NotFound("static/index.html is missing")
    return Raw(200, "text/html; charset=utf-8", body)


def _h_static(h: "Handler", m: re.Match) -> Raw:
    name = m.group(1)
    ext = os.path.splitext(name)[1].lower()
    if ext not in STATIC_TYPES:
        raise NotFound(f"not served: {name}")
    base = Path(STATIC_DIR).resolve()
    target = (base / name).resolve()
    if target != base and base not in target.parents:
        raise NotFound("path traversal")
    try:
        body = target.read_bytes()
    except OSError:
        raise NotFound(name)
    return Raw(200, STATIC_TYPES[ext], body)


def _h_healthz(h: "Handler", m: re.Match):
    return {
        "ok": True,
        "version": config.VERSION,
        "pid": os.getpid(),
        "socket_activated": h.server.socket_activated,
    }


def _snapshot_stale(conn, max_age: float = STATE_RECONCILE_MAX_AGE) -> bool:
    """True when the last reconcile is missing, unparsable or older than max_age s."""
    stamp = db.get_setting(conn, "reconciled_at")
    if not stamp:
        return True
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    return (datetime.now() - then).total_seconds() > max_age


def _quick_reconcile(conn) -> None:
    """Best-effort ss-only reconcile; a failure must never break the caller's reply."""
    try:
        _lazy("reconcile").reconcile(conn, quick=True)
    except Exception:
        log.exception("quick reconcile failed")


def _state(h: "Handler", conn, refresh: bool = False) -> dict:
    if refresh and _snapshot_stale(conn):
        _quick_reconcile(conn)
    registry = _lazy("registry")
    state = dict(registry.state_snapshot(conn))
    state["daemon"] = h.server.daemon_info()
    state["projects_root"] = str(config.PROJECTS_ROOT)
    return state


def _h_state(h: "Handler", m: re.Match):
    return _state(h, h.conn(), refresh=True)


def _h_reconcile(h: "Handler", m: re.Match):
    body = h.body()
    conn = h.conn()
    summary = _lazy("reconcile").reconcile(
        conn,
        quick=bool(body.get("quick", False)),
        adopt_unknown=bool(body.get("adopt_unknown", False)),
    )
    state = _state(h, conn)
    state["summary"] = summary
    return state


def _h_events(h: "Handler", m: re.Match):
    limit = _int(h.query_one("limit"), 100) or 100
    limit = max(1, min(limit, 2000))
    conn = h.conn()
    return db.rows(conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))


def _h_project_add(h: "Handler", m: re.Match):
    body = dict(h.body())
    path = body.pop("path", None)
    if not path:
        raise ValueError("path is required")
    allow_busy = bool(body.pop("allow_busy", False))
    conn = h.conn()
    project = _lazy("registry").add_project(
        conn, path, source=body.pop("source", "gui"), allow_busy=allow_busy, **body
    )
    # match an already-running dev server to the new instance right away
    _quick_reconcile(conn)
    fresh = _lazy("registry").get_project(conn, project["id"])
    return 201, fresh if isinstance(fresh, dict) else project


def _h_project_order(h: "Handler", m: re.Match):
    body = h.body()
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise ValueError("ids must be a list of project ids")
    _lazy("registry").reorder_projects(h.conn(), ids)
    return _state(h, h.conn())


def _h_project_patch(h: "Handler", m: re.Match):
    fields = dict(h.body())
    fields.pop("id", None)
    conn = h.conn()
    project = _lazy("registry").update_project(conn, int(m.group(1)), **fields)
    if project is None:
        raise NotFound(f"project {m.group(1)} not found")
    if "base_port" in fields or "path" in fields or "kind" in fields:
        _quick_reconcile(conn)
        fresh = _lazy("registry").get_project(conn, project["id"])
        if isinstance(fresh, dict):
            project = fresh
    return project


def _h_project_delete(h: "Handler", m: re.Match):
    registry = _lazy("registry")
    runner = _lazy("runner")
    conn = h.conn()
    pid_ = int(m.group(1))
    project = registry.get_project(conn, pid_)
    if project is None:
        raise NotFound(f"project {pid_} not found")
    for inst in registry.list_instances(conn, project_id=pid_):
        if inst.get("state") in ("running", "starting") and inst.get("managed", 1):
            try:
                runner.stop(conn, inst["id"], reason="user")
            except Exception as exc:  # a dead unit must not block the delete
                log.warning("stopping instance %s before project delete failed: %s", inst["id"], exc)
    registry.delete_project(conn, pid_)
    return {"ok": True}


def _h_project_instances(h: "Handler", m: re.Match):
    registry = _lazy("registry")
    conn = h.conn()
    pid_ = int(m.group(1))
    project = registry.get_project(conn, pid_)
    if project is None:
        raise NotFound(f"project {pid_} not found")
    body = h.body()
    path = body.get("path")
    label = body.get("label")
    if not path:
        if not label:
            raise ValueError("path or label is required")
        if label == "main":
            path = project["path"]
        else:
            path = os.path.join(project["path"], config.WORKTREE_DIRNAME, label)
    return 201, registry.ensure_instance(conn, pid_, path, label=label,
                                         branch=body.get("branch"), source="gui")


def _instance_action(action: str) -> Callable:
    def handler(h: "Handler", m: re.Match):
        runner = _lazy("runner")
        conn = h.conn()
        iid = int(m.group(1))
        if _lazy("registry").get_instance(conn, iid) is None:
            raise NotFound(f"instance {iid} not found")
        if action == "start":
            return runner.start(conn, iid, wait=True)
        if action == "stop":
            return runner.stop(conn, iid, reason="user")
        return runner.restart(conn, iid)
    return handler


def _h_instance_release(h: "Handler", m: re.Match):
    registry = _lazy("registry")
    conn = h.conn()
    iid = int(m.group(1))
    if registry.get_instance(conn, iid) is None:
        raise NotFound(f"instance {iid} not found")
    registry.release_owner(conn, instance_id=iid)
    return registry.get_instance(conn, iid)


def _h_instance_delete(h: "Handler", m: re.Match):
    registry = _lazy("registry")
    conn = h.conn()
    iid = int(m.group(1))
    inst = registry.get_instance(conn, iid)
    if inst is None:
        raise NotFound(f"instance {iid} not found")
    if inst.get("label") == "main" or inst.get("slot") == 0:
        raise ValueError("the main instance cannot be deleted; delete the project instead")
    if inst.get("state") in ("running", "starting") and inst.get("managed", 1):
        try:
            _lazy("runner").stop(conn, iid, reason="user")
        except Exception as exc:
            log.warning("stopping instance %s before delete failed: %s", iid, exc)
    registry.delete_instance(conn, iid)
    return {"ok": True}


def _h_instance_logs(h: "Handler", m: re.Match):
    conn = h.conn()
    iid = int(m.group(1))
    if _lazy("registry").get_instance(conn, iid) is None:
        raise NotFound(f"instance {iid} not found")
    lines = _int(h.query_one("lines"), 200) or 200
    lines = max(1, min(lines, 5000))
    return {"text": _lazy("runner").logs(conn, iid, lines=lines)}


def _observed_row(conn, port: int) -> dict:
    row = conn.execute(
        "SELECT * FROM observed WHERE port = ? ORDER BY seen_at DESC LIMIT 1", (port,)
    ).fetchone()
    if row is None:
        raise NotFound(f"port {port} is not in the last reconcile snapshot")
    return dict(row)


def _h_observed_adopt(h: "Handler", m: re.Match):
    registry = _lazy("registry")
    conn = h.conn()
    port = int(m.group(1))
    obs = _observed_row(conn, port)
    project_id = _int(h.body().get("project_id")) or obs.get("project_id")
    if not project_id:
        raise ValueError("this listener matched no project; pass project_id")
    project = registry.get_project(conn, project_id)
    if project is None:
        raise NotFound(f"project {project_id} not found")
    path = obs.get("cwd") or obs.get("compose_workdir") or project["path"]
    inst = registry.ensure_instance(conn, project["id"], path, source="adopted")
    fields: dict[str, Any] = {
        "managed": 0,
        "state": "running",
        "pid": obs.get("pid"),
        "actual_port": port,
        "last_seen_at": db.now(),
    }
    unit = obs.get("unit") or obs.get("container") or obs.get("compose_project")
    if unit:
        fields["unit"] = unit
    if inst.get("port") != port and _port_assignable(conn, port, inst["id"]):
        fields["port"] = port
    updated = registry.update_instance(conn, inst["id"], **fields)
    db.add_event(conn, "instance.adopt", {"port": port}, project_id=project["id"], instance_id=inst["id"])
    return updated or registry.get_instance(conn, inst["id"])


def _port_assignable(conn, port: int, instance_id: int) -> bool:
    taken = conn.execute(
        "SELECT 1 FROM instances WHERE port = ? AND id != ?", (port, instance_id)
    ).fetchone()
    if taken:
        return False
    return conn.execute("SELECT 1 FROM projects WHERE base_port = ?", (port,)).fetchone() is None


def _proc_uid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _h_observed_stop(h: "Handler", m: re.Match):
    import subprocess

    conn = h.conn()
    port = int(m.group(1))
    obs = _observed_row(conn, port)
    container = obs.get("container")
    if container:
        proc = subprocess.run(["docker", "stop", container], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise ValueError(f"docker stop {container} failed: {(proc.stderr or proc.stdout).strip()}")
        db.add_event(conn, "observed.stop", {"port": port, "container": container})
        return {"ok": True, "stopped": container}
    pid = obs.get("pid")
    if not pid:
        raise ValueError(f"port {port} has no known pid or container")
    if _proc_uid(int(pid)) != os.getuid():
        raise ValueError(f"pid {pid} does not belong to this user")
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except OSError as exc:
        raise ValueError(f"could not signal pid {pid}: {exc}")
    db.add_event(conn, "observed.stop", {"port": port, "pid": pid})
    return {"ok": True, "stopped": pid}


def _h_schedule_stop(h: "Handler", m: re.Match):
    return _lazy("schedule").evening_stop(h.conn())


def _h_schedule_start(h: "Handler", m: re.Match):
    return _lazy("schedule").morning_start(h.conn())


def _h_discover(h: "Handler", m: re.Match):
    path = h.query_one("path")
    if not path:
        raise ValueError("path is required")
    return _lazy("discover").suggest(path)


def _h_settings(h: "Handler", m: re.Match):
    conn = h.conn()
    body = h.body()
    for key, value in body.items():
        if key not in config.DEFAULT_SETTINGS:
            raise ValueError(f"unknown setting: {key}")
        db.set_setting(conn, key, str(value))
    if body:
        db.add_event(conn, "settings.update", sorted(body))
    return db.all_settings(conn)


def _h_mcp_post(h: "Handler", m: re.Match) -> Raw:
    status, headers, body = _lazy("mcp").http_post(h.raw_body(), h.headers)
    return Raw(status, headers.pop("Content-Type", "application/json; charset=utf-8"), body, headers)


def _h_mcp_get(h: "Handler", m: re.Match):
    return 405, {"error": "use POST for the MCP endpoint"}


ROUTES: list[tuple[str, re.Pattern, Callable]] = [
    ("GET", re.compile(r"^/$"), _h_index),
    ("GET", re.compile(r"^/static/([^/]+)$"), _h_static),
    ("GET", re.compile(r"^/healthz$"), _h_healthz),
    ("GET", re.compile(r"^/api/state$"), _h_state),
    ("POST", re.compile(r"^/api/reconcile$"), _h_reconcile),
    ("GET", re.compile(r"^/api/events$"), _h_events),
    ("POST", re.compile(r"^/api/projects$"), _h_project_add),
    ("POST", re.compile(r"^/api/projects/order$"), _h_project_order),
    ("PATCH", re.compile(r"^/api/projects/(\d+)$"), _h_project_patch),
    ("DELETE", re.compile(r"^/api/projects/(\d+)$"), _h_project_delete),
    ("POST", re.compile(r"^/api/projects/(\d+)/instances$"), _h_project_instances),
    ("POST", re.compile(r"^/api/instances/(\d+)/start$"), _instance_action("start")),
    ("POST", re.compile(r"^/api/instances/(\d+)/stop$"), _instance_action("stop")),
    ("POST", re.compile(r"^/api/instances/(\d+)/restart$"), _instance_action("restart")),
    ("POST", re.compile(r"^/api/instances/(\d+)/release$"), _h_instance_release),
    ("DELETE", re.compile(r"^/api/instances/(\d+)$"), _h_instance_delete),
    ("GET", re.compile(r"^/api/instances/(\d+)/logs$"), _h_instance_logs),
    ("POST", re.compile(r"^/api/observed/(\d+)/adopt$"), _h_observed_adopt),
    ("POST", re.compile(r"^/api/observed/(\d+)/stop$"), _h_observed_stop),
    ("POST", re.compile(r"^/api/schedule/stop$"), _h_schedule_stop),
    ("POST", re.compile(r"^/api/schedule/start$"), _h_schedule_start),
    ("GET", re.compile(r"^/api/discover$"), _h_discover),
    ("POST", re.compile(r"^/api/settings$"), _h_settings),
    ("POST", re.compile(r"^/mcp$"), _h_mcp_post),
    ("GET", re.compile(r"^/mcp$"), _h_mcp_get),
]


# --------------------------------------------------------------------------
# request handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"portboard/{config.VERSION}"
    sys_version = ""

    # no access log at all; errors go to the portboard log
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def log_error(self, fmt: str, *args) -> None:
        log.warning("%s - %s", self.address_string(), fmt % args)

    # -- request plumbing ---------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def raw_body(self) -> bytes:
        return self._raw

    def body(self) -> dict[str, Any]:
        if self._parsed_body is None:
            raw = self._raw
            if not raw.strip():
                self._parsed_body = {}
            else:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid JSON body: {exc}")
                if not isinstance(parsed, dict):
                    raise ValueError("JSON body must be an object")
                self._parsed_body = parsed
        return self._parsed_body

    def query_one(self, key: str, default: str | None = None) -> str | None:
        values = self.query.get(key)
        return values[0] if values else default

    def conn(self):
        if self._conn is None:
            self._conn = db.connect()
        return self._conn

    def _read_raw(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("invalid Content-Length")
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length > MAX_BODY:
            raise ValueError(f"request body larger than {MAX_BODY} bytes")
        return self.rfile.read(length) if length else b""

    def _dispatch(self) -> None:
        self.server.enter_request()
        self._conn = None
        self._parsed_body = None
        self._raw = b""
        self.query = {}
        try:
            split = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(split.path)
            self.query = urllib.parse.parse_qs(split.query)
            try:
                self._raw = self._read_raw()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if path != "/healthz" and not host_allowed(self.headers.get("Host")):
                self._send_json(403, {"error": "forbidden Host header"})
                return

            matched_path = False
            for method, rx, fn in ROUTES:
                m = rx.match(path)
                if not m:
                    continue
                matched_path = True
                if method != self.command:
                    continue
                self._run(fn, m)
                return
            if matched_path:
                self._send_json(405, {"error": f"{self.command} not allowed on {path}"})
            else:
                self._send_json(404, {"error": f"no such endpoint: {path}"})
        except Exception as exc:  # never let a thread die silently
            log.error("unhandled error in dispatch: %s\n%s", exc, traceback.format_exc())
            try:
                self._send_json(500, {"error": _error_text(exc)})
            except Exception:
                pass
        finally:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            self.server.leave_request()

    def _run(self, fn: Callable, m: re.Match) -> None:
        try:
            result = fn(self, m)
        except Exception as exc:
            status = _error_status(exc)
            if status >= 500:
                log.error("%s %s failed: %s\n%s", self.command, self.path, exc, traceback.format_exc())
            else:
                log.info("%s %s -> %s: %s", self.command, self.path, status, _error_text(exc))
            self._send_json(status, {"error": _error_text(exc)})
            return
        if isinstance(result, Raw):
            self._send_raw(result.status, result.content_type, result.body, result.headers)
            return
        status = 200
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            status, result = result
        self._send_json(status, result)

    # -- responses ----------------------------------------------------------
    def _send_json(self, status: int, payload: Any) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            status = 500
            body = json.dumps({"error": f"result is not JSON serializable: {exc}"}).encode("utf-8")
        self._send_raw(status, "application/json; charset=utf-8", body)

    def _send_raw(self, status: int, content_type: str, body: bytes,
                  extra: dict[str, str] | None = None) -> None:
        try:
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
