"""MCP server for Portboard: stateless streamable HTTP plus a stdio shim.

Every POST to /mcp carries exactly one JSON-RPC 2.0 message. Notifications are
answered with 202 and an empty body, requests with 200 and a JSON object. No
session header is ever required, so a client may reconnect at any time - which
is exactly what a socket-activated daemon that exits when idle needs.

`stdio_main()` runs the same handler over newline-delimited JSON on stdin and
stdout for `portboard mcp-stdio`.

Registry / runner / reconcile / discover are imported lazily inside the tool
implementations so that importing this module stays cheap and tests can patch
them.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import traceback
from typing import Any

from . import config, db

log = logging.getLogger("portboard.mcp")

SERVER_INFO = {"name": "portboard", "version": config.VERSION}
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_CWD_DOC = ("Absolute path of the directory in question (the caller's cwd - the server "
            "cannot know it, so always pass it explicitly, never a relative path).")


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "ports_list",
        "description": (
            "List every dev-server instance Portboard knows about, every TCP port that is "
            "currently listening on this machine and the ports reserved by other software. "
            "Reconciles with reality first, so the answer is fresh. Use it to find out what "
            "is running and which ports are already taken before starting anything."
        ),
        "inputSchema": _schema({}),
    },
    {
        "name": "port_whois",
        "description": (
            "Explain who holds a TCP port: the listening process, its systemd unit or docker "
            "container and the Portboard project/instance it belongs to. Returns {\"free\": true} "
            "when nothing listens on it."
        ),
        "inputSchema": _schema(
            {"port": {"type": "integer", "minimum": 1, "maximum": 65535,
                      "description": "TCP port number to look up."}},
            ["port"],
        ),
    },
    {
        "name": "project_status",
        "description": (
            "Status of the project that owns a directory: the project, all of its instances "
            "(main checkout and worktrees), the instance for this exact directory, its assigned "
            "port and URL. Tells you whether the directory is registered at all."
        ),
        "inputSchema": _schema(
            {"cwd": {"type": "string", "description": _CWD_DOC}},
            ["cwd"],
        ),
    },
    {
        "name": "project_claim",
        "description": (
            "Register the repository containing this directory if it is not known yet, make sure "
            "an instance row exists for this checkout (main checkout or Claude Code worktree), "
            "assign it a port and optionally mark the session as its owner. Returns the project, "
            "the instance, its port and URL. Call this once at the start of a session before "
            "starting a dev server."
        ),
        "inputSchema": _schema(
            {
                "cwd": {"type": "string", "description": _CWD_DOC},
                "session_id": {"type": "string",
                               "description": "Claude Code session id that takes ownership of the instance."},
            },
            ["cwd"],
        ),
    },
    {
        "name": "instance_start",
        "description": (
            "Start the dev server for a checkout and WAIT until its port is actually listening, "
            "then return the instance including the URL to open. Identify the instance either by "
            "cwd (absolute path) or by instance_id. On failure the result is an error containing "
            "the last log lines."
        ),
        "inputSchema": _schema(
            {
                "cwd": {"type": "string", "description": _CWD_DOC},
                "instance_id": {"type": "integer", "description": "Instance id from ports_list or project_status."},
                "session_id": {"type": "string", "description": "Claude Code session id that takes ownership."},
            },
        ),
    },
    {
        "name": "instance_stop",
        "description": (
            "Stop the dev server of a checkout. Identify the instance either by cwd (absolute "
            "path) or by instance_id."
        ),
        "inputSchema": _schema(
            {
                "cwd": {"type": "string", "description": _CWD_DOC},
                "instance_id": {"type": "integer", "description": "Instance id from ports_list or project_status."},
            },
        ),
    },
    {
        "name": "reconcile",
        "description": (
            "Re-read reality (listening sockets, systemd units, docker containers) and update the "
            "registry. Returns a summary. Use quick=true for a fast pass that skips docker."
        ),
        "inputSchema": _schema(
            {"quick": {"type": "boolean", "description": "Skip docker for a faster pass. Default false."}},
        ),
    },
    {
        "name": "project_register",
        "description": (
            "Register a repository as a Portboard project. Missing fields are guessed from the "
            "repository contents (package.json, compose file, pyproject, Makefile). path must be "
            "an absolute path to the main checkout."
        ),
        "inputSchema": _schema(
            {
                "path": {"type": "string", "description": "Absolute path of the repository's main checkout."},
                "name": {"type": "string", "description": "Project name; defaults to the directory basename."},
                "kind": {"type": "string", "enum": list(config.KINDS),
                         "description": "transient (systemd-run of start_cmd), unit, compose or none."},
                "start_cmd": {"type": "string",
                              "description": "Shell command for transient projects, unit name for kind=unit."},
                "base_port": {"type": "integer", "minimum": 1, "maximum": 32767,
                              "description": "Port of the main checkout; worktree slot n uses base_port+n."},
                "port_mode": {"type": "string", "enum": list(config.PORT_MODES),
                              "description": "env (PORT is injected), arg ({port} in start_cmd), fixed or none."},
            },
            ["path"],
        ),
    },
]

TOOL_NAMES = [t["name"] for t in TOOLS]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _lazy(name: str):
    return importlib.import_module(f".{name}", __package__)


def _open_path(project: dict | None) -> str:
    path = (project or {}).get("open_path") or "/"
    return path if path.startswith("/") else "/" + path


def _url(inst: dict, project: dict | None = None) -> str | None:
    if inst.get("url"):
        return inst["url"]
    port = inst.get("port") or inst.get("actual_port")
    if not port:
        return None
    return f"http://localhost:{port}{_open_path(project)}"


def _compact(inst: dict, project: dict | None = None) -> dict[str, Any]:
    worktree = inst.get("worktree")
    if worktree is None:
        worktree = bool(inst.get("slot"))
    return {
        "id": inst.get("id"),
        "project": inst.get("project") or (project or {}).get("name"),
        "label": inst.get("label"),
        "port": inst.get("port"),
        "actual_port": inst.get("actual_port"),
        "state": inst.get("state"),
        "url": _url(inst, project),
        "owner_session": inst.get("owner_session"),
        "worktree": bool(worktree),
    }


def _abs(path: Any, field: str = "cwd") -> str:
    if not isinstance(path, str) or not path:
        raise ValueError(f"{field} is required")
    if not os.path.isabs(path):
        raise ValueError(f"{field} must be an absolute path, got {path!r}")
    return os.path.realpath(path)


def _git_root(path: str) -> str | None:
    current = os.path.realpath(path)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _resolved(conn, path: str):
    """registry.resolve_path with a tolerant unpacking (dataclass or tuple)."""
    res = _lazy("registry").resolve_path(conn, path)
    if res is None:
        return None
    if isinstance(res, tuple) and not hasattr(res, "project"):
        project = res[0] if len(res) > 0 else None
        instance = res[1] if len(res) > 1 else None
        label = res[2] if len(res) > 2 else None
        return _Res(project, instance, label)
    return res


class _Res:
    def __init__(self, project, instance, label):
        self.project = project
        self.instance = instance
        self.label = label
        self.is_worktree = bool(label and label != "main")
        self.slug = label
        self.path = None


def _project_of(conn, inst: dict) -> dict | None:
    if not inst:
        return None
    return _lazy("registry").get_project(conn, inst["project_id"]) if inst.get("project_id") else None


# --------------------------------------------------------------------------
# tool implementations
# --------------------------------------------------------------------------

def _tool_ports_list(conn, args: dict) -> dict:
    registry = _lazy("registry")
    _lazy("reconcile").reconcile(conn, quick=True)
    instances = [_compact(i) for i in registry.list_instances(conn)]
    observed = db.rows(conn.execute(
        "SELECT o.port, o.bind, o.comm, o.unit, o.container, o.instance_id, p.name AS project "
        "FROM observed o LEFT JOIN projects p ON p.id = o.project_id ORDER BY o.port"
    ))
    reserved = db.rows(conn.execute("SELECT port, label FROM reserved_ports ORDER BY port"))
    return {"instances": instances, "observed": observed, "reserved": reserved}


def _tool_port_whois(conn, args: dict) -> dict:
    registry = _lazy("registry")
    port = args.get("port")
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValueError("port must be an integer")
    _lazy("reconcile").reconcile(conn, quick=True)
    row = conn.execute(
        "SELECT * FROM observed WHERE port = ? ORDER BY seen_at DESC LIMIT 1", (port,)
    ).fetchone()
    if row is None:
        return {"port": port, "free": True}
    result: dict[str, Any] = {"free": False, "observed": dict(row)}
    obs = dict(row)
    if obs.get("project_id"):
        result["project"] = registry.get_project(conn, obs["project_id"])
    if obs.get("instance_id"):
        inst = registry.get_instance(conn, obs["instance_id"])
        if inst:
            result["instance"] = _compact(inst, _project_of(conn, inst))
    return result


def _tool_project_status(conn, args: dict) -> dict:
    registry = _lazy("registry")
    cwd = _abs(args.get("cwd"))
    res = _resolved(conn, cwd)
    project = getattr(res, "project", None) if res else None
    if not project:
        return {"registered": False, "cwd": cwd, "hint": "call project_claim to register"}
    instances = registry.list_instances(conn, project_id=project["id"])
    this = getattr(res, "instance", None)
    compact_this = _compact(this, project) if this else None
    return {
        "registered": True,
        "cwd": cwd,
        "project": project,
        "instances": [_compact(i, project) for i in instances],
        "this": compact_this,
        "assigned_port": (this or {}).get("port") if this else project.get("base_port"),
        "url": _url(this, project) if this else None,
        "running": [_compact(i, project) for i in instances if i.get("state") == "running"],
    }


def _tool_project_claim(conn, args: dict) -> dict:
    registry = _lazy("registry")
    cwd = _abs(args.get("cwd"))
    session_id = args.get("session_id")
    res = _resolved(conn, cwd)
    project = getattr(res, "project", None) if res else None
    if not project:
        discover = _lazy("discover")
        root = _git_root(cwd) or cwd
        if not discover.looks_like_project(root):
            raise ValueError(
                f"{cwd} is not a recognizable project (no git repo with a known stack at {root}); "
                "register it explicitly with project_register if it is one"
            )
        suggestion = dict(discover.suggest(root) or {})
        suggestion.pop("confidence", None)
        suggestion.pop("evidence", None)
        path = suggestion.pop("path", root)
        project = registry.add_project(conn, path, source="mcp", allow_busy=True, **suggestion)
        res = _resolved(conn, cwd)

    inst = getattr(res, "instance", None) if res else None
    if not inst:
        label = getattr(res, "label", None) if res else None
        inst = registry.ensure_instance(conn, project["id"], cwd, label=label, source="mcp")
    if session_id:
        registry.set_owner(conn, inst["id"], session_id)
        inst = registry.get_instance(conn, inst["id"]) or inst
    return {
        "project": project,
        "instance": _compact(inst, project),
        "port": inst.get("port"),
        "url": _url(inst, project),
        "start_hint": f"instance_start(cwd={cwd!r})",
    }


def _find_instance(conn, args: dict, create: bool) -> dict:
    registry = _lazy("registry")
    instance_id = args.get("instance_id")
    if instance_id is not None:
        if not isinstance(instance_id, int) or isinstance(instance_id, bool):
            raise ValueError("instance_id must be an integer")
        inst = registry.get_instance(conn, instance_id)
        if inst is None:
            raise ValueError(f"no instance with id {instance_id}")
        return inst
    cwd = _abs(args.get("cwd"))
    res = _resolved(conn, cwd)
    project = getattr(res, "project", None) if res else None
    if not project:
        raise ValueError(f"{cwd} belongs to no registered project; call project_claim first")
    inst = getattr(res, "instance", None) if res else None
    if inst:
        return inst
    if not create:
        raise ValueError(f"no instance registered for {cwd}; call project_claim first")
    label = getattr(res, "label", None) if res else None
    return registry.ensure_instance(conn, project["id"], cwd, label=label, source="mcp")


def _tool_instance_start(conn, args: dict) -> dict:
    registry = _lazy("registry")
    runner = _lazy("runner")
    inst = _find_instance(conn, args, create=True)
    session_id = args.get("session_id")
    if session_id:
        registry.set_owner(conn, inst["id"], session_id)
    try:
        started = runner.start(conn, inst["id"], wait=True)
    except Exception as exc:
        tail = ""
        try:
            tail = runner.logs(conn, inst["id"], lines=10)
        except Exception:
            tail = ""
        raise ToolError({
            "error": str(exc),
            "instance": _compact(registry.get_instance(conn, inst["id"]) or inst),
            "logs_tail": (tail or "").strip().splitlines()[-10:],
        })
    started = registry.get_instance(conn, inst["id"]) or started
    project = _project_of(conn, started)
    result = dict(started)
    result["url"] = _url(started, project)
    result["project"] = (project or {}).get("name") or started.get("project")
    return result


def _tool_instance_stop(conn, args: dict) -> dict:
    registry = _lazy("registry")
    runner = _lazy("runner")
    inst = _find_instance(conn, args, create=False)
    stopped = runner.stop(conn, inst["id"], reason="mcp")
    stopped = registry.get_instance(conn, inst["id"]) or stopped
    project = _project_of(conn, stopped)
    result = dict(stopped)
    result["url"] = _url(stopped, project)
    result["project"] = (project or {}).get("name") or stopped.get("project")
    return result


def _tool_reconcile(conn, args: dict) -> dict:
    return _lazy("reconcile").reconcile(conn, quick=bool(args.get("quick", False)))


def _tool_project_register(conn, args: dict) -> dict:
    registry = _lazy("registry")
    path = _abs(args.get("path"), "path")
    fields: dict[str, Any] = {}
    try:
        suggestion = _lazy("discover").suggest(path) or {}
    except Exception as exc:
        log.info("discover.suggest(%s) failed: %s", path, exc)
        suggestion = {}
    for key, value in suggestion.items():
        if key in ("confidence", "evidence", "path"):
            continue
        fields[key] = value
    for key in ("name", "kind", "start_cmd", "base_port", "port_mode"):
        if args.get(key) is not None:
            fields[key] = args[key]
    # A port discovered in the repo may already be held by the project's own
    # dev server; only an explicit caller-supplied port is checked strictly.
    allow_busy = args.get("base_port") is None
    return registry.add_project(conn, path, source="mcp", allow_busy=allow_busy, **fields)


TOOL_IMPLS = {
    "ports_list": _tool_ports_list,
    "port_whois": _tool_port_whois,
    "project_status": _tool_project_status,
    "project_claim": _tool_project_claim,
    "instance_start": _tool_instance_start,
    "instance_stop": _tool_instance_stop,
    "reconcile": _tool_reconcile,
    "project_register": _tool_project_register,
}


class ToolError(Exception):
    """Tool failure whose payload is returned as an isError tool result."""

    def __init__(self, payload: Any):
        super().__init__(payload if isinstance(payload, str) else json.dumps(payload, default=str))
        self.payload = payload


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one tool. Always returns an MCP tool result, never raises."""
    args = dict(arguments or {})
    if name not in TOOL_IMPLS:
        return _tool_result({"error": f"unknown tool: {name}", "available": TOOL_NAMES}, True)
    conn = None
    try:
        conn = db.connect()
        result = TOOL_IMPLS[name](conn, args)
        return _tool_result(result, False)
    except ToolError as exc:
        return _tool_result(exc.payload, True)
    except Exception as exc:
        log.warning("tool %s failed: %s\n%s", name, exc, traceback.format_exc())
        return _tool_result({"error": f"{type(exc).__name__}: {exc}", "tool": name}, True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _tool_result(payload: Any, is_error: bool) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------

def _result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def handle_jsonrpc(message: dict) -> dict | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "request must be a JSON object")
    msg_id = message.get("id")
    is_notification = "id" not in message
    method = message.get("method")

    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return None if is_notification else _error(msg_id, INVALID_REQUEST, "invalid JSON-RPC 2.0 request")

    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return None if is_notification else _error(msg_id, INVALID_PARAMS, "params must be an object")

    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
            result: Any = {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not name:
                return None if is_notification else _error(msg_id, INVALID_PARAMS, "tools/call requires a tool name")
            arguments = params.get("arguments", {})
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return None if is_notification else _error(msg_id, INVALID_PARAMS, "arguments must be an object")
            result = call_tool(name, arguments)
        elif method.startswith("notifications/"):
            return None
        else:
            return None if is_notification else _error(msg_id, METHOD_NOT_FOUND, f"unknown method: {method}")
    except Exception as exc:
        log.error("JSON-RPC %s failed: %s\n%s", method, exc, traceback.format_exc())
        return None if is_notification else _error(msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    if is_notification:
        return None
    return _result(msg_id, result)


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

def http_post(body_bytes: bytes, headers: Any = None) -> tuple[int, dict[str, str], bytes]:
    """Answer one POST /mcp. Returns (status, headers, body)."""
    json_headers = {"Content-Type": "application/json; charset=utf-8"}
    raw = body_bytes or b""
    if not raw.strip():
        return 200, json_headers, _encode(_error(None, INVALID_REQUEST, "empty request body"))
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return 200, json_headers, _encode(_error(None, PARSE_ERROR, f"parse error: {exc}"))
    if isinstance(message, list):
        return 200, json_headers, _encode(
            _error(None, INVALID_REQUEST, "batched JSON-RPC requests are not supported; send one message per POST")
        )
    response = handle_jsonrpc(message)
    if response is None:
        return 202, {}, b""
    return 200, json_headers, _encode(response)


def _encode(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


def stdio_main(stdin=None, stdout=None) -> int:
    """Newline-delimited JSON-RPC on stdin/stdout for `portboard mcp-stdio`."""
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(out, _error(None, PARSE_ERROR, f"parse error: {exc}"))
            continue
        if isinstance(message, list):
            _write(out, _error(None, INVALID_REQUEST, "batched JSON-RPC requests are not supported"))
            continue
        response = handle_jsonrpc(message)
        if response is not None:
            _write(out, response)
    return 0


def _write(out, obj: Any) -> None:
    out.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    try:
        out.flush()
    except Exception:
        pass
