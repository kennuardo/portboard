"""Claude Code hook handlers.

`portboard hook <event>` reads a JSON payload from stdin and dispatches here.
Handlers never raise: any exception is logged and swallowed, exit code 0,
no output — a broken hook must never block Claude Code.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any

from . import config, db

log = logging.getLogger("portboard.hooks")

_DEV_SERVER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("npm-dev", re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve|preview)\b")),
    ("nuxt", re.compile(r"\bnuxi?\s+dev\b")),
    ("vite", re.compile(r"\bvite\b")),
    ("next", re.compile(r"\bnext\s+dev\b")),
    ("astro", re.compile(r"\bastro\s+dev\b")),
    ("uvicorn", re.compile(r"\buvicorn\b")),
    ("flask", re.compile(r"\bflask\s+run\b")),
    ("compose", re.compile(r"\bdocker\s+compose\s+up\b")),
    ("http.server", re.compile(r"\bpython3?\s+-m\s+http\.server\b")),
]

# Commands that only mention dev-server words inside a string argument
# (echo/quoting/searching/history), never actually launch anything.
_IGNORE_LEADERS = ("echo", "cat", "grep", "rg", "git", "ls")

_PORT_PATTERNS = [
    re.compile(r"--port[=\s]+(\d+)"),
    re.compile(r"-p\s+(\d+)"),
    re.compile(r"\bPORT=(\d+)"),
]


def read_payload() -> dict:
    """Read and parse the hook JSON from stdin. Empty dict on any failure."""
    try:
        raw = sys.stdin.read()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def run(event: str, payload: dict) -> int:
    """Dispatch `event` to its handler. Never raises; always returns 0."""
    try:
        conn = db.connect()
        try:
            if event == "session-start":
                text = session_start(conn, payload)
                if text:
                    print(text)
            elif event == "session-end":
                session_end(conn, payload)
            elif event == "pre-tool-use":
                result = pre_tool_use(conn, payload)
                if result:
                    print(json.dumps(result))
            elif event == "cwd-changed":
                cwd_changed(conn, payload)
            elif event == "worktree-remove":
                worktree_remove(conn, payload)
            else:
                log.warning("unknown hook event: %s", event)
        finally:
            conn.close()
    except Exception:
        log.exception("hook %s failed", event)
    return 0


def detect_dev_server(command: str) -> dict | None:
    """Pure classifier: {"kind": ..., "port": int|None} or None."""
    if not command:
        return None
    stripped = command.strip()
    first_word = stripped.split(None, 1)[0] if stripped else ""
    # Strip a leading path or shell-ism from the leader for the ignore check.
    leader = first_word.rsplit("/", 1)[-1]
    if leader in _IGNORE_LEADERS:
        return None
    for kind, pattern in _DEV_SERVER_PATTERNS:
        if pattern.search(command):
            port = None
            for port_pattern in _PORT_PATTERNS:
                m = port_pattern.search(command)
                if m:
                    try:
                        port = int(m.group(1))
                    except ValueError:
                        port = None
                    break
            return {"kind": kind, "port": port}
    return None


def _first_present(payload: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value:
            return value
    return None


def session_start(conn, payload: dict) -> str | None:
    start = time.monotonic()
    try:
        cwd = payload.get("cwd")
        if not cwd:
            return None

        from . import registry, reconcile

        try:
            reconcile.reconcile(conn, quick=True)
        except Exception:
            log.exception("session_start: reconcile(quick=True) failed")

        try:
            resolved = registry.resolve_path(conn, cwd)
        except Exception:
            log.exception("session_start: resolve_path failed for %s", cwd)
            return None

        project = getattr(resolved, "project", None)
        if project is None:
            try:
                from . import discover
            except Exception:
                return None
            try:
                is_repo = bool(discover.looks_like_project(cwd))
                # A directory of sibling sub-repos is ONE project: prefer the
                # group over registering the single repo the session sits in.
                group_suggestion = _group_suggestion_for(discover, cwd, is_repo)
                suggestion = None if group_suggestion else (discover.suggest(cwd) if is_repo else None)
            except Exception:
                log.exception("session_start: discover failed for %s", cwd)
                return None
            if not group_suggestion and not suggestion:
                return None
            try:
                if group_suggestion:
                    project = registry.add_group(
                        conn, group_suggestion, source="hook", allow_busy=True
                    )
                else:
                    project = registry.add_project(
                        conn,
                        suggestion.get("path", cwd),
                        name=suggestion.get("name"),
                        kind=suggestion.get("kind", "transient"),
                        start_cmd=suggestion.get("start_cmd"),
                        base_port=suggestion.get("base_port"),
                        port_mode=suggestion.get("port_mode", "env"),
                        source="hook",
                        allow_busy=True,
                    )
            except Exception:
                log.exception("session_start: registration failed for %s", cwd)
                return None
            try:  # the quick pass above ran before this project existed
                reconcile.reconcile(conn, quick=True)
            except Exception:
                log.exception("session_start: reconcile after add_project failed")
            resolved = registry.resolve_path(conn, cwd)
            project = getattr(resolved, "project", None)
            if project is None:
                return None

        label = getattr(resolved, "label", None) or "main"
        instance = getattr(resolved, "instance", None)
        if instance is None:
            try:
                instance = registry.ensure_instance(
                    conn,
                    project["id"],
                    cwd,
                    label=label,
                    branch=registry.git_branch(cwd),
                    source="hook",
                )
            except Exception:
                log.exception("session_start: ensure_instance failed for %s", cwd)
                return None

        is_worktree = bool(getattr(resolved, "is_worktree", False))
        try:
            instances = registry.list_instances(conn, project_id=project["id"])
        except Exception:
            instances = [instance]

        if project.get("kind") == "group" and not is_worktree:
            return format_group_session_start(project, instances)

        group_line = None
        group = getattr(resolved, "group", None)
        if group:
            try:
                group_instances = registry.list_instances(conn, project_id=group["id"])
            except Exception:
                log.exception("session_start: list_instances for group %s failed", group.get("name"))
                group_instances = []
            group_line = format_group_line(group, group_instances)

        return _format_session_start(project, instance, instances, is_worktree, group_line=group_line)
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        log.info("session_start took %.1f ms", elapsed_ms)


def _group_suggestion_for(discover, cwd: str, is_repo: bool) -> dict | None:
    """The group suggestion covering *cwd*, or None.

    ``discover.group_of`` answers for a repository (its parent directory) and
    for a group directory itself; a plain sub-directory of a group (rma/tools)
    is neither, so its parent is tried as well. The result is only accepted
    when it actually contains *cwd*.
    """
    try:
        real = os.path.realpath(os.path.expanduser(cwd))
    except Exception:
        return None
    group_of = getattr(discover, "group_of", None)
    if group_of is None:
        return None
    candidates = [real] if is_repo else [real, os.path.dirname(real)]
    for candidate in candidates:
        if not candidate or candidate == os.sep:
            continue
        try:
            suggestion = group_of(candidate)
        except Exception:
            log.exception("session_start: discover.group_of(%s) failed", candidate)
            continue
        if not suggestion:
            continue
        gpath = suggestion.get("path")
        if not gpath:
            continue
        if is_repo or real == gpath or real.startswith(gpath.rstrip(os.sep) + os.sep):
            return suggestion
    return None


def _service_state(service: dict) -> str:
    state = service.get("state") or "unknown"
    return state if state in ("running", "starting", "failed") else "not running"


def _service_bit(service: dict) -> str:
    name = service.get("project") or service.get("name") or "?"
    port = service.get("port") or service.get("actual_port")
    state = _service_state(service)
    return f"{name} :{port} ({state})" if port else f"{name} ({state})"


def _group_main_view(instances: list[dict]) -> dict | None:
    """The group's own main instance view (slot 0) — it mirrors the primary child."""
    for inst in instances or ():
        if not inst.get("slot") or inst.get("label") == "main":
            return inst
    return None


def format_group_line(group: dict, group_instances: list[dict]) -> str | None:
    """One line naming the group, its primary child and the other services."""
    view = _group_main_view(group_instances)
    services = list((view or {}).get("services") or [])
    if not services:
        return None
    primary_name = (view or {}).get("primary") or group.get("primary")
    primary = next((s for s in services if s.get("project") == primary_name), None)
    others = [s for s in services if s is not primary]
    name = group.get("name")
    if primary is None:
        return f"part of group {name}: " + ", ".join(_service_bit(s) for s in services)
    line = f"part of group {name}: primary {_service_bit(primary)}"
    if others:
        line += "; also " + ", ".join(_service_bit(s) for s in others)
    return line


def format_group_session_start(project: dict, instances: list[dict]) -> str:
    """Session-start block for a cwd that IS the group directory."""
    view = _group_main_view(instances)
    services = list((view or {}).get("services") or [])
    name = project.get("name")
    path = project.get("path")
    lines = [f"[portboard] group {name} at {path} ({len(services)} services)"]

    primary_name = (view or {}).get("primary") or project.get("primary")
    primary = next((s for s in services if s.get("project") == primary_name), None)
    if primary is not None:
        port = primary.get("port") or primary.get("actual_port")
        url = primary.get("url") or (f"http://localhost:{port}/" if port else "?")
        lines.append(f"frontend: {primary_name} :{port or '?'}, test URL {url}")
    else:
        lines.append("frontend: none yet, set one with: portboard project edit "
                     f"{name} --primary <child>")

    if services:
        lines.append("services: " + ", ".join(_service_bit(s) for s in services))
    else:
        lines.append("no sub-projects registered yet")

    lines.append(
        f"start everything with the MCP tool instance_start (cwd={path}) or: portboard start {name}"
    )
    return "\n".join(lines)


def _format_session_start(project: dict, instance: dict, instances: list[dict], is_worktree: bool,
                          group_line: str | None = None) -> str:
    name = project.get("name")
    label = instance.get("label", "main")
    branch = instance.get("branch")
    port = instance.get("port")
    url = instance.get("url") or (f"http://localhost:{port}/" if port else "?")

    if is_worktree:
        checkout_desc = f"worktree {label} (branch {branch})" if branch else f"worktree {label}"
    else:
        checkout_desc = "main checkout"

    lines = [f"[portboard] project {name}, {checkout_desc}"]
    if port:
        lines.append(f"assigned port {port}, test URL {url}")
    else:
        lines.append("no port assigned yet")
    if group_line:
        lines.append(group_line)

    status_bits = []
    for inst in instances:
        bit_label = inst.get("label", "main")
        inst_port = inst.get("port")
        state = inst.get("state", "unknown")
        if state == "running":
            unit = inst.get("unit") or (project.get("start_cmd") if project.get("kind") == "unit" else None) or "-"
            since = (inst.get("started_at") or "?")[11:16] if inst.get("started_at") else "?"
            owner = inst.get("owner_session") or "none"
            detail = f"running: {bit_label}@{inst_port} (unit {unit}, since {since}, owner {owner})"
        else:
            detail = f"{bit_label}@{inst_port} not running" if inst_port else f"{bit_label} not running"
        status_bits.append(detail)
    if status_bits:
        lines.append("; ".join(status_bits))

    if project.get("kind") == "none":
        lines.append("no start command configured, register one with: portboard project edit <name> --start '...'")
    else:
        lines.append("start it with the MCP tool instance_start (cwd=...) or: portboard start --cwd .")

    return "\n".join(lines)


def session_end(conn, payload: dict) -> None:
    from . import registry

    session_id = payload.get("session_id")
    if not session_id:
        return
    try:
        registry.release_owner(conn, session_id=session_id)
    except Exception:
        log.exception("session_end: release_owner failed for session %s", session_id)


def _strip_port(command: str) -> str:
    """Remove an explicit port from a dev-server command so a corrected one can be suggested."""
    out = re.sub(r"(?:^|\s)PORT=\d+\s*", " ", command)
    out = re.sub(r"\s(?:--port(?:=|\s+)|-p\s+)\d+", "", out)
    out = re.sub(r"\s--\s*$", "", out)
    return out.strip()


def pre_tool_use(conn, payload: dict) -> dict | None:
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or ""
    detected = detect_dev_server(command)
    if not detected:
        return None

    cwd = payload.get("cwd")
    if not cwd:
        return None
    log.info("pre_tool_use: %s detected in %s (%s)", detected.get("kind"), cwd, command[:120])

    from . import registry

    try:
        resolved = registry.resolve_path(conn, cwd)
    except Exception:
        log.exception("pre_tool_use: resolve_path failed for %s", cwd)
        return None

    project = getattr(resolved, "project", None)
    if project is None:
        return None

    instance = getattr(resolved, "instance", None)
    assigned_port = instance.get("port") if instance else None
    explicit_port = detected.get("port")

    def _deny(reason: str) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"{reason}. Start it through portboard instead: MCP tool instance_start(cwd=\"{cwd}\") "
                    f"or `portboard start --cwd {cwd}`"
                    + (f", or run it yourself on the assigned port: PORT={assigned_port} {_strip_port(command)}"
                       if assigned_port else "")
                ),
            }
        }

    try:
        instances = registry.list_instances(conn)
    except Exception:
        instances = []
    # A group's main instance MIRRORS its primary child (same port, same
    # server): counting it would deny the child its own port.
    instances = [i for i in instances if i.get("kind") != "group"]

    if explicit_port is not None:
        for inst in instances:
            if inst.get("port") == explicit_port and (instance is None or inst.get("id") != instance.get("id")):
                owner_desc = inst.get("project")
                return _deny(f"port {explicit_port} is already assigned to {owner_desc}@{inst.get('label')}")
        return None

    if assigned_port is not None:
        for inst in instances:
            if inst.get("port") == assigned_port and inst.get("id") != instance.get("id") and inst.get("state") == "running":
                return _deny(f"port {assigned_port} for this checkout is already held by {inst.get('project')}@{inst.get('label')}")

    return None


def cwd_changed(conn, payload: dict) -> None:
    """Notification-only: `cd` or entering a worktree. Backfill the instance
    row for a worktree path so the GUI shows it before anything runs."""
    log.info("cwd_changed payload keys: %s", sorted(payload.keys()))
    log.debug("cwd_changed payload: %r", payload)

    cwd = _first_present(payload, ("cwd", "new_cwd"))
    if not cwd:
        return

    from . import registry

    try:
        resolved = registry.resolve_path(conn, cwd)
    except Exception:
        log.exception("cwd_changed: resolve_path failed for %s", cwd)
        return

    project = getattr(resolved, "project", None)
    if project is None:
        return

    is_worktree = bool(getattr(resolved, "is_worktree", False))
    instance = getattr(resolved, "instance", None)
    if not is_worktree or instance is not None:
        return

    label = getattr(resolved, "label", None)
    try:
        registry.ensure_instance(
            conn, project["id"], cwd, label=label, branch=registry.git_branch(cwd), source="hook"
        )
    except Exception:
        log.exception("cwd_changed: ensure_instance failed for %s", cwd)


def worktree_remove(conn, payload: dict) -> None:
    """Informational: fields session_id, cwd, worktree_path. Stop and delete
    the instance whose path equals worktree_path."""
    log.info("worktree_remove payload keys: %s", sorted(payload.keys()))
    log.debug("worktree_remove payload: %r", payload)

    path = _first_present(payload, ("worktree_path", "cwd"))
    if not path:
        return

    from . import registry

    try:
        resolved = registry.resolve_path(conn, path)
    except Exception:
        log.exception("worktree_remove: resolve_path failed for %s", path)
        return
    instance = getattr(resolved, "instance", None)
    if instance is None:
        return
    try:
        from . import runner

        try:
            runner.stop(conn, instance["id"], reason="hook")
        except Exception:
            log.exception("worktree_remove: stop failed for instance %s", instance.get("id"))
    except Exception:
        log.exception("worktree_remove: runner import failed")
    try:
        registry.delete_instance(conn, instance["id"])
    except Exception:
        log.exception("worktree_remove: delete_instance failed for %s", instance.get("id"))
