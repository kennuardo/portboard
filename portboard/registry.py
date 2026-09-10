"""Projects, instances, port allocation and path mapping.

Every write goes through this module: it stamps created_at/updated_at, keeps
the main instance in sync with the project and records an audit event. Ports
are handed out from the pool in settings; the block base..base+slots belongs to
one project (slot 0 = main checkout, slot n = worktree n).
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
from dataclasses import dataclass
from typing import Any

from . import config, db

log = logging.getLogger("portboard.registry")

#: Linux hands out ephemeral ports from 32768 upwards; never allocate there.
MAX_PORT = 32767

MAIN_LABEL = "main"


class RegistryError(Exception):
    """Any refused registry operation (duplicate, unknown ref, busy port)."""


@dataclass
class Resolved:
    """What a filesystem path maps to."""

    project: dict | None = None
    instance: dict | None = None
    label: str | None = None
    is_worktree: bool = False
    slug: str | None = None
    path: str = ""
    group: dict | None = None   # the kind='group' parent when project is a child of one

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "instance": self.instance,
            "label": self.label,
            "is_worktree": self.is_worktree,
            "slug": self.slug,
            "path": self.path,
            "group": self.group,
        }


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

_COLUMNS: dict[str, tuple[str, ...]] = {}


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    cols = _COLUMNS.get(table)
    if cols is None:
        cols = tuple(r["name"] for r in conn.execute(f"PRAGMA table_info({table})"))
        _COLUMNS[table] = cols
    return cols


def _clean_fields(conn: sqlite3.Connection, table: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Keep only real columns; JSON-encode dicts/lists for the *_json columns."""
    known = _columns(conn, table)
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key in ("id", "created_at"):
            continue
        if key not in known:
            log.debug("ignoring unknown %s field %r", table, key)
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            value = int(value)
        out[key] = value
    return out


def normalize_path(path: str | os.PathLike[str]) -> str:
    """Absolute, symlink-free, no trailing slash."""
    real = os.path.realpath(os.path.expanduser(str(path)))
    return real.rstrip("/") or "/"


def derive_name(path: str) -> str:
    """Directory basename lowercased, everything outside [a-z0-9-] becomes '-'."""
    base = os.path.basename(normalize_path(path)).lower()
    return re.sub(r"[^a-z0-9-]", "-", base) or "project"


def safe_label(label: str | None) -> str:
    """Lowercase [a-z0-9-] form of a label, safe for systemd unit names."""
    text = re.sub(r"[^a-z0-9-]+", "-", (label or "").strip().lower())
    return re.sub(r"-{2,}", "-", text).strip("-") or "x"


def git_branch(path: str | os.PathLike[str]) -> str | None:
    """Current branch of the checkout at *path*, or None when git says nothing."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("git_branch(%s) failed: %s", path, exc)
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    return branch or None


def url_for(project: dict | None, instance: dict | None) -> str | None:
    """http://localhost:<port><open_path>, None when the instance has no port."""
    inst = instance or {}
    port = inst.get("port")
    if inst.get("state") == "running" and inst.get("actual_port"):
        port = inst["actual_port"]
    if not port:
        return None
    open_path = (project or {}).get("open_path") or "/"
    if not open_path.startswith("/"):
        open_path = "/" + open_path
    return f"http://localhost:{port}{open_path}"


def worktree_root(project_path: str) -> str:
    return os.path.join(normalize_path(project_path), config.WORKTREE_DIRNAME)


# --------------------------------------------------------------------------
# ports
# --------------------------------------------------------------------------


def _registered_ports(conn: sqlite3.Connection) -> set[int]:
    """Ports owned by registered projects/instances only (no observed/reserved)."""
    used: set[int] = set()
    for row in conn.execute("SELECT base_port FROM projects WHERE base_port IS NOT NULL"):
        used.add(int(row["base_port"]))
    for row in conn.execute("SELECT port FROM instances WHERE port IS NOT NULL"):
        used.add(int(row["port"]))
    return used


def _db_used_ports(conn: sqlite3.Connection, exclude_instance_id: int | None = None) -> set[int]:
    used: set[int] = set()
    for row in conn.execute("SELECT base_port FROM projects WHERE base_port IS NOT NULL"):
        used.add(int(row["base_port"]))
    sql = "SELECT port FROM instances WHERE port IS NOT NULL"
    params: tuple[Any, ...] = ()
    if exclude_instance_id is not None:
        sql += " AND id != ?"
        params = (exclude_instance_id,)
    for row in conn.execute(sql, params):
        used.add(int(row["port"]))
    for row in conn.execute("SELECT port FROM reserved_ports"):
        used.add(int(row["port"]))
    for row in conn.execute("SELECT port FROM observed"):
        used.add(int(row["port"]))
    if exclude_instance_id is not None:
        row = conn.execute(
            "SELECT port FROM instances WHERE id = ?", (exclude_instance_id,)
        ).fetchone()
        if row and row["port"] is not None:
            used.discard(int(row["port"]))
    return used


def _bindable(port: int) -> bool:
    """Quick bind test on both 127.0.0.1 and 0.0.0.0, SO_REUSEADDR off."""
    for host in ("127.0.0.1", "0.0.0.0"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
        except OSError:
            return False
        finally:
            sock.close()
    return True


def port_is_free(conn: sqlite3.Connection, port: int, exclude_instance_id: int | None = None) -> bool:
    """True when nothing in the database claims *port* and it can be bound."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if port <= 0 or port > MAX_PORT:
        return False
    if port in _db_used_ports(conn, exclude_instance_id):
        return False
    return _bindable(port)


def _pool(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    start = db.get_int_setting(conn, "pool_start", 4000)
    end = db.get_int_setting(conn, "pool_end", 4990)
    step = max(1, db.get_int_setting(conn, "pool_step", 10))
    slots = max(0, db.get_int_setting(conn, "slots", 9))
    return start, min(end, MAX_PORT), step, slots


def allocate_base_port(
    conn: sqlite3.Connection, preferred: int | None = None, allow_busy: bool = False
) -> int:
    """Rules 1-3 and 5 of DESIGN.md: keep a free explicit port, else scan the pool.

    allow_busy=True accepts a preferred port that is currently in use or merely
    observed, as long as no *registered* project or instance owns it. That is
    the case when a project's own dev server is already running on its
    configured port at registration time; reconcile will match it afterwards.
    """
    if preferred is not None:
        preferred = int(preferred)
        if allow_busy:
            if preferred <= 0 or preferred > MAX_PORT:
                raise RegistryError(f"port {preferred} is out of range")
            if preferred in _registered_ports(conn):
                raise RegistryError(f"port {preferred} is already registered to another project")
            if not port_is_free(conn, preferred):
                log.info("port %s is busy right now; accepting it anyway (allow_busy)", preferred)
            return preferred
        if not port_is_free(conn, preferred):
            raise RegistryError(f"port {preferred} is not free")
        return preferred
    start, end, step, slots = _pool(conn)
    used = _db_used_ports(conn)
    base = start
    while base <= end:
        block = range(base, base + slots + 1)
        if block.stop - 1 <= MAX_PORT and not (used & set(block)) and all(_bindable(p) for p in block):
            return base
        base += step
    raise RegistryError(f"no free port block of {slots + 1} ports in {start}-{end}")


def allocate_slot(conn: sqlite3.Connection, project: dict) -> tuple[int, int]:
    """Rule 4: first free slot inside the project block, else any free pool port."""
    slots = int(project.get("slots") or db.get_int_setting(conn, "slots", 9))
    base = project.get("base_port")
    used_slots = {
        int(r["slot"])
        for r in conn.execute("SELECT slot FROM instances WHERE project_id = ?", (project["id"],))
    }
    used_ports = _db_used_ports(conn)
    if base:
        base = int(base)
        for n in range(1, slots + 1):
            port = base + n
            if n in used_slots or port > MAX_PORT:
                continue
            if port not in used_ports and _bindable(port):
                return n, port
    # block exhausted (or the project has no base port): fall back to the pool
    slot = next(n for n in range(1, 10_000) if n not in used_slots)
    start, end, _step, _slots = _pool(conn)
    for port in range(start, end + 1):
        if port not in used_ports and _bindable(port):
            return slot, port
    raise RegistryError("no free port left in the pool")


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------


def _project_row(conn: sqlite3.Connection, project_id: int) -> dict | None:
    return db.row(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())


def get_project(conn: sqlite3.Connection, ref: int | str) -> dict | None:
    """Look a project up by id (int or digit string) or by name (decorated, see _decorate)."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = _project_row(conn, int(ref))
    else:
        row = db.row(conn.execute("SELECT * FROM projects WHERE name = ?", (str(ref),)).fetchone())
    if row is None:
        return None
    return _decorate(conn, [row])[0]


def list_projects(conn: sqlite3.Connection, with_instances: bool = False) -> list[dict]:
    projects = _decorate(conn, db.rows(conn.execute("SELECT * FROM projects ORDER BY sort_order, name")))
    if not with_instances:
        return projects
    by_id = {p["id"]: p for p in projects}
    for project in projects:
        project["instances"] = []
    # children first: a group's main instance mirrors its primary child's row
    ordered = sorted(projects, key=lambda p: p.get("kind") == "group")
    for project in ordered:
        for row in conn.execute("SELECT * FROM instances WHERE project_id = ? ORDER BY slot, id",
                                (project["id"],)):
            project["instances"].append(_instance_view(conn, dict(row), project))
    return projects


# --------------------------------------------------------------------------
# groups (kind='group': one directory of sibling sub-projects)
# --------------------------------------------------------------------------


def _decorate(conn: sqlite3.Connection, projects: list[dict]) -> list[dict]:
    """Add the computed group fields every consumer expects.

    * every project: ``parent`` (parent name or None)
    * kind='group': ``children`` (child ids, display order), ``child_names``,
      ``primary_child_id`` (explicit ``primary_child`` when it is still a child,
      else the discover heuristic) and ``primary`` (its name).
    """
    if not projects:
        return projects
    all_rows = db.rows(conn.execute(
        "SELECT id, name, parent_id, base_port, kind, sort_order FROM projects ORDER BY sort_order, name"))
    by_id = {r["id"]: r for r in all_rows}
    children_of: dict[int, list[dict]] = {}
    for r in all_rows:
        if r["parent_id"] is not None:
            children_of.setdefault(int(r["parent_id"]), []).append(r)
    for project in projects:
        parent = by_id.get(project.get("parent_id")) if project.get("parent_id") is not None else None
        project["parent"] = parent["name"] if parent else None
        if project.get("kind") != "group":
            continue
        kids = children_of.get(project["id"], [])
        project["children"] = [k["id"] for k in kids]
        project["child_names"] = [k["name"] for k in kids]
        primary = _pick_primary_row(project, kids)
        project["primary_child_id"] = primary["id"] if primary else None
        project["primary"] = primary["name"] if primary else None
    return projects


def _pick_primary_row(group: dict, children: list[dict]) -> dict | None:
    wanted = group.get("primary_child")
    if wanted is not None:
        for child in children:
            if int(child["id"]) == int(wanted):
                return child
    from . import discover

    return discover.pick_primary(children)


def group_children(conn: sqlite3.Connection, group_id: int) -> list[dict]:
    """Child projects of a group in display order (sort_order, name)."""
    rows = db.rows(conn.execute(
        "SELECT * FROM projects WHERE parent_id = ? ORDER BY sort_order, name", (int(group_id),)))
    return _decorate(conn, rows)


def primary_child(conn: sqlite3.Connection, group: dict) -> dict | None:
    """The child whose port/url/state the group shows (explicit or heuristic)."""
    children = group_children(conn, group["id"])
    return _pick_primary_row(group, children)


def group_start_order(conn: sqlite3.Connection, group: dict) -> list[dict]:
    """Children in start order: display order with the primary moved last."""
    children = group_children(conn, group["id"])
    primary = _pick_primary_row(group, children)
    if primary is None:
        return children
    return [c for c in children if c["id"] != primary["id"]] + [primary]


def _child_main_views(conn: sqlite3.Connection, group: dict) -> list[dict]:
    views = []
    for child in group_children(conn, group["id"]):
        main = _main_instance(conn, child["id"])
        if main is not None:
            views.append(_instance_view(conn, main, child))
    return views


def _validate_parent(conn: sqlite3.Connection, parent_id: Any, child_id: int | None = None) -> int | None:
    if parent_id is None:
        return None
    parent = _project_row(conn, int(parent_id))
    if parent is None:
        raise RegistryError(f"no project with id {parent_id} to use as a group")
    if parent.get("kind") != "group":
        raise RegistryError(f"project {parent['name']!r} is not a group (kind {parent['kind']!r})")
    if parent.get("parent_id") is not None:
        raise RegistryError("nested groups are not supported")
    if child_id is not None and int(child_id) == parent["id"]:
        raise RegistryError("a project cannot be its own parent")
    return parent["id"]


def add_group(conn: sqlite3.Connection, suggestion: dict, source: str = "cli",
              allow_busy: bool = True) -> dict:
    """Register a discover.suggest_group() result: the group and every child.

    A child whose path is already registered is re-parented (its own config is
    kept); the primary is taken from ``suggestion["primary"]`` (a child name).
    Returns the decorated group project.
    """
    data = dict(suggestion or {})
    children = list(data.pop("children", None) or [])
    primary_name = data.pop("primary", None)
    for key in ("confidence", "evidence"):
        data.pop(key, None)
    path = data.pop("path", None)
    if not path:
        raise RegistryError("group suggestion without a path")
    name = data.pop("name", None)
    path = normalize_path(path)
    group = db.row(conn.execute("SELECT * FROM projects WHERE path = ?", (path,)).fetchone())
    if group is None:
        data.pop("kind", None)
        data.pop("port_mode", None)
        data.pop("base_port", None)
        data.pop("start_cmd", None)
        group = add_project(conn, path, name=name, kind="group", port_mode="none",
                            source=source, **data)
    elif group.get("kind") != "group":
        raise RegistryError(f"{path} is already registered as {group['name']!r} (kind {group['kind']!r})")

    primary_id = None
    for child in children:
        child = dict(child)
        for key in ("confidence", "evidence", "children", "primary"):
            child.pop(key, None)
        cpath = child.pop("path", None)
        if not cpath:
            continue
        cpath = normalize_path(cpath)
        cname = child.pop("name", None)
        existing = db.row(conn.execute("SELECT * FROM projects WHERE path = ?", (cpath,)).fetchone())
        if existing is not None:
            if existing.get("parent_id") not in (None, group["id"]):
                raise RegistryError(f"{existing['name']!r} already belongs to another group")
            if existing.get("parent_id") != group["id"]:
                update_project(conn, existing["id"], parent_id=group["id"])
            row = existing
        else:
            try:
                row = add_project(
                    conn, cpath, name=cname,
                    kind=child.pop("kind", "transient"),
                    start_cmd=child.pop("start_cmd", None),
                    base_port=child.pop("base_port", None),
                    port_mode=child.pop("port_mode", "env"),
                    source=source, allow_busy=allow_busy, parent_id=group["id"], **child,
                )
            except RegistryError as exc:
                log.warning("group %s: child %s not registered: %s", group["name"], cpath, exc)
                continue
        if primary_name and (cname == primary_name or row.get("name") == primary_name):
            primary_id = row["id"]
    if primary_id is not None and group.get("primary_child") != primary_id:
        update_project(conn, group["id"], primary_child=primary_id)
    return get_project(conn, group["id"]) or group


def add_project(
    conn: sqlite3.Connection,
    path: str,
    name: str | None = None,
    kind: str = "transient",
    start_cmd: str | None = None,
    base_port: int | None = None,
    port_mode: str = "env",
    source: str = "cli",
    allow_busy: bool = False,
    parent_id: int | None = None,
    **fields: Any,
) -> dict:
    """Register a project and its 'main' instance.

    allow_busy=True keeps an explicit base_port even when something already
    listens on it (typically the project's own dev server); see allocate_base_port.
    parent_id makes the project a child of a kind='group' project; without a
    name it is called ``<group>-<basename>``. kind='group' never has a port.
    """
    if kind not in config.KINDS:
        raise RegistryError(f"unknown kind {kind!r} (expected one of {', '.join(config.KINDS)})")
    if port_mode not in config.PORT_MODES:
        raise RegistryError(
            f"unknown port_mode {port_mode!r} (expected one of {', '.join(config.PORT_MODES)})"
        )
    path = normalize_path(path)
    parent_id = _validate_parent(conn, fields.pop("parent_id", parent_id))
    if parent_id is not None and kind == "group":
        raise RegistryError("nested groups are not supported")
    if kind == "group":
        port_mode, base_port, start_cmd = "none", None, None
    if not name and parent_id is not None:
        parent = _project_row(conn, parent_id) or {}
        name = f"{parent.get('name', 'group')}-{derive_name(path)}"
    name = (name or derive_name(path)).strip()
    if not name:
        raise RegistryError("project name is empty")

    if conn.execute("SELECT 1 FROM projects WHERE path = ?", (path,)).fetchone():
        raise RegistryError(f"a project is already registered at {path}")
    if conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
        raise RegistryError(f"a project named {name!r} already exists")

    if port_mode == "none":
        base_port = None
    else:
        base_port = allocate_base_port(conn, base_port, allow_busy=allow_busy)

    ts = db.now()
    values = {
        "name": name,
        "path": path,
        "kind": kind,
        "start_cmd": start_cmd,
        "port_mode": port_mode,
        "base_port": base_port,
        "slots": db.get_int_setting(conn, "slots", 9),
        "source": source,
        "parent_id": parent_id,
        "created_at": ts,
        "updated_at": ts,
    }
    values.update(_clean_fields(conn, "projects", fields))
    values["created_at"] = ts
    values["updated_at"] = ts
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    try:
        cur = conn.execute(f"INSERT INTO projects({cols}) VALUES ({marks})", tuple(values.values()))
    except sqlite3.IntegrityError as exc:
        raise RegistryError(f"cannot register project: {exc}") from exc
    project_id = int(cur.lastrowid)
    db.add_event(conn, "project.add", {"name": name, "path": path, "base_port": base_port,
                                       "kind": kind, "source": source}, project_id=project_id)

    project = _project_row(conn, project_id)
    _create_instance(conn, project, MAIN_LABEL, 0, path, base_port, source=source)
    return _project_row(conn, project_id)  # refreshed


def update_project(conn: sqlite3.Connection, project_id: int, allow_busy: bool = False,
                   **fields: Any) -> dict:
    """Change project fields. allow_busy=True accepts a new base_port that is
    busy right now as long as no other registered project/instance owns it
    (the project's own hand-started server is listening there)."""
    project = _project_row(conn, int(project_id))
    if project is None:
        raise RegistryError(f"no project with id {project_id}")
    if "kind" in fields and fields["kind"] not in config.KINDS:
        raise RegistryError(f"unknown kind {fields['kind']!r}")
    if "port_mode" in fields and fields["port_mode"] not in config.PORT_MODES:
        raise RegistryError(f"unknown port_mode {fields['port_mode']!r}")
    if fields.get("path"):
        fields["path"] = normalize_path(fields["path"])
    if "parent_id" in fields:
        fields["parent_id"] = _validate_parent(conn, fields["parent_id"], child_id=project["id"])
        if fields["parent_id"] is not None and fields.get("kind", project["kind"]) == "group":
            raise RegistryError("nested groups are not supported")
    if "primary_child" in fields and fields["primary_child"] is not None:
        wanted = fields["primary_child"]
        child = get_project(conn, wanted) if not isinstance(wanted, int) else _project_row(conn, wanted)
        if child is None or child.get("parent_id") != project["id"]:
            raise RegistryError(f"{wanted!r} is not a child of group {project['name']!r}")
        fields["primary_child"] = child["id"]
    if fields.get("kind") == "group":
        fields["port_mode"], fields["base_port"] = "none", None

    main = _main_instance(conn, project["id"])
    new_port = fields.get("base_port", project["base_port"])
    if new_port is not None:
        new_port = int(new_port)
    if "base_port" in fields and new_port != project["base_port"]:
        if new_port is not None and allow_busy:
            if new_port <= 0 or new_port > MAX_PORT:
                raise RegistryError(f"port {new_port} is out of range")
            owned = _registered_ports(conn) - {project["base_port"], (main or {}).get("port")}
            if new_port in owned:
                raise RegistryError(f"port {new_port} is already registered to another project")
        elif new_port is not None and not port_is_free(
            conn, new_port, exclude_instance_id=(main or {}).get("id")
        ):
            raise RegistryError(f"port {new_port} is not free")

    values = _clean_fields(conn, "projects", fields)
    if not values:
        return project
    values["updated_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in values)
    try:
        conn.execute(
            f"UPDATE projects SET {sets} WHERE id = ?", (*values.values(), project["id"])
        )
    except sqlite3.IntegrityError as exc:
        raise RegistryError(f"cannot update project: {exc}") from exc

    if "base_port" in values and main is not None:
        update_instance(conn, main["id"], port=values["base_port"])
    if "path" in values and main is not None:
        update_instance(conn, main["id"], path=values["path"])
    db.add_event(conn, "project.update", {k: values[k] for k in values if k != "updated_at"},
                 project_id=project["id"])
    return _project_row(conn, project["id"])


def delete_project(conn: sqlite3.Connection, project_id: int) -> None:
    project = _project_row(conn, int(project_id))
    if project is None:
        raise RegistryError(f"no project with id {project_id}")
    for child in db.rows(conn.execute("SELECT id FROM projects WHERE parent_id = ?", (project["id"],))):
        delete_project(conn, child["id"])
    conn.execute("DELETE FROM instances WHERE project_id = ?", (project["id"],))
    conn.execute("DELETE FROM projects WHERE id = ?", (project["id"],))
    conn.execute("UPDATE observed SET project_id = NULL, instance_id = NULL WHERE project_id = ?",
                 (project["id"],))
    db.add_event(conn, "project.delete", {"name": project["name"], "path": project["path"]})


# --------------------------------------------------------------------------
# instances
# --------------------------------------------------------------------------


def _instance_view(conn: sqlite3.Connection, row: dict, project: dict | None = None) -> dict:
    """Row plus the computed fields the API and GUI expect."""
    if project is None or "parent" not in project:
        project = get_project(conn, int(row["project_id"])) or {}
    view = dict(row)
    view["project"] = project.get("name")
    view["kind"] = project.get("kind")
    view["pinned"] = project.get("pinned", 0)
    view["open_path"] = project.get("open_path", "/")
    view["worktree"] = bool(row.get("slot"))
    view["url"] = url_for(project, row)
    view["parent"] = project.get("parent")
    if project.get("kind") == "group" and not row.get("slot"):
        _mirror_primary(conn, view, project)
    return view


_MIRRORED = ("state", "port", "actual_port", "url", "pid", "started_at", "stopped_at",
             "stopped_by", "mem_bytes", "cpu_ns", "idle_since", "unit", "managed")


def _mirror_primary(conn: sqlite3.Connection, view: dict, group: dict) -> None:
    """A group's main instance shows its primary child's main instance, plus
    ``services`` (every child's main instance, display order) for the GUI."""
    if "primary_child_id" not in group:
        group = _decorate(conn, [dict(group)])[0]
    services = _child_main_views(conn, group)
    view["services"] = services
    view["primary"] = group.get("primary")
    primary = next((s for s in services if s.get("project_id") == group.get("primary_child_id")), None)
    if primary is None:
        return
    for key in _MIRRORED:
        view[key] = primary.get(key)
    view["primary_instance_id"] = primary.get("id")
    running = [s for s in services if s.get("state") == "running"]
    view["services_running"] = len(running)
    view["services_total"] = len(services)


def _main_instance(conn: sqlite3.Connection, project_id: int) -> dict | None:
    return db.row(
        conn.execute(
            "SELECT * FROM instances WHERE project_id = ? AND slot = 0", (project_id,)
        ).fetchone()
    )


def _create_instance(
    conn: sqlite3.Connection,
    project: dict,
    label: str,
    slot: int,
    path: str,
    port: int | None,
    branch: str | None = None,
    source: str = "cli",
) -> dict:
    ts = db.now()
    values = {
        "project_id": project["id"],
        "label": label,
        "slot": slot,
        "path": normalize_path(path),
        "branch": branch if branch is not None else git_branch(path),
        "port": port,
        "created_at": ts,
        "updated_at": ts,
    }
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    try:
        cur = conn.execute(f"INSERT INTO instances({cols}) VALUES ({marks})", tuple(values.values()))
    except sqlite3.IntegrityError as exc:
        raise RegistryError(f"cannot create instance {label!r}: {exc}") from exc
    instance_id = int(cur.lastrowid)
    db.add_event(
        conn,
        "instance.add",
        {"label": label, "slot": slot, "port": port, "path": values["path"], "source": source},
        project_id=project["id"],
        instance_id=instance_id,
    )
    return get_instance(conn, instance_id) or {}


def list_instances(conn: sqlite3.Connection, project_id: int | None = None) -> list[dict]:
    if project_id is None:
        rows = conn.execute("SELECT * FROM instances ORDER BY project_id, slot, id")
    else:
        rows = conn.execute(
            "SELECT * FROM instances WHERE project_id = ? ORDER BY slot, id", (int(project_id),)
        )
    projects = {p["id"]: p for p in list_projects(conn)}
    return [_instance_view(conn, dict(r), projects.get(r["project_id"])) for r in rows]


def get_instance(conn: sqlite3.Connection, instance_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM instances WHERE id = ?", (int(instance_id),)).fetchone()
    if row is None:
        return None
    return _instance_view(conn, dict(row))


def ensure_instance(
    conn: sqlite3.Connection,
    project_id: int,
    path: str,
    label: str | None = None,
    branch: str | None = None,
    source: str = "cli",
) -> dict:
    """Return (creating when needed) the instance for a checkout directory."""
    project = _project_row(conn, int(project_id))
    if project is None:
        raise RegistryError(f"no project with id {project_id}")
    path = normalize_path(path)
    ppath = normalize_path(project["path"])

    if path == ppath:
        wanted_label, slot, port = MAIN_LABEL, 0, project["base_port"]
    else:
        wt_root = worktree_root(ppath)
        if not path.startswith(wt_root + os.sep):
            raise RegistryError(f"{path} is not the checkout or a worktree of {project['name']}")
        slug = path[len(wt_root) + 1:].split(os.sep)[0]
        if not slug:
            raise RegistryError(f"{path} has no worktree slug")
        wanted_label, slot, port = (label or slug), None, None

    existing = db.row(
        conn.execute(
            "SELECT * FROM instances WHERE project_id = ? AND (path = ? OR label = ?)",
            (project["id"], path, wanted_label),
        ).fetchone()
    )
    if existing is not None:
        fresh = branch if branch is not None else git_branch(path)
        if fresh and fresh != existing["branch"]:
            return update_instance(conn, existing["id"], branch=fresh)
        return _instance_view(conn, existing, project)

    if slot is None:
        slot, port = allocate_slot(conn, project)
    return _create_instance(
        conn, project, wanted_label, slot, path, port, branch=branch, source=source
    )


def sync_worktrees(conn: sqlite3.Connection, source: str = "reconcile") -> list[dict]:
    """Create instance rows for every ``.claude/worktrees/<slug>`` directory that
    exists on disk but has no row yet (a worktree stack started by hand, or one
    created before the project was registered). One listdir per project; never
    deletes anything. Returns the new instances."""
    created: list[dict] = []
    for project in list_projects(conn):
        if project.get("kind") == "group":
            continue
        root = worktree_root(project["path"])
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            continue
        known = {r["path"] for r in conn.execute(
            "SELECT path FROM instances WHERE project_id = ?", (project["id"],))}
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                continue
            path = normalize_path(entry.path)
            if path in known:
                continue
            try:
                created.append(ensure_instance(conn, project["id"], path, source=source))
            except RegistryError as exc:
                log.warning("sync_worktrees: %s/%s: %s", project["name"], entry.name, exc)
    return created


def update_instance(conn: sqlite3.Connection, instance_id: int, **fields: Any) -> dict:
    instance = conn.execute("SELECT * FROM instances WHERE id = ?", (int(instance_id),)).fetchone()
    if instance is None:
        raise RegistryError(f"no instance with id {instance_id}")
    values = _clean_fields(conn, "instances", fields)
    values.pop("project_id", None)
    if not values:
        return get_instance(conn, instance_id) or {}
    values["updated_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in values)
    try:
        conn.execute(f"UPDATE instances SET {sets} WHERE id = ?", (*values.values(), int(instance_id)))
    except sqlite3.IntegrityError as exc:
        raise RegistryError(f"cannot update instance: {exc}") from exc
    return get_instance(conn, instance_id) or {}


def delete_instance(conn: sqlite3.Connection, instance_id: int) -> None:
    row = conn.execute("SELECT * FROM instances WHERE id = ?", (int(instance_id),)).fetchone()
    if row is None:
        raise RegistryError(f"no instance with id {instance_id}")
    if int(row["slot"]) == 0:
        raise RegistryError("the main instance cannot be deleted; delete the project instead")
    conn.execute("DELETE FROM instances WHERE id = ?", (row["id"],))
    conn.execute("UPDATE observed SET instance_id = NULL WHERE instance_id = ?", (row["id"],))
    db.add_event(
        conn,
        "instance.delete",
        {"label": row["label"], "path": row["path"], "port": row["port"]},
        project_id=row["project_id"],
    )


def set_owner(conn: sqlite3.Connection, instance_id: int, session_id: str | None) -> dict:
    instance = update_instance(
        conn, instance_id, owner_session=session_id, owner_seen_at=db.now()
    )
    db.add_event(
        conn,
        "instance.claim",
        {"session_id": session_id},
        project_id=instance.get("project_id"),
        instance_id=instance.get("id"),
    )
    return instance


def release_owner(
    conn: sqlite3.Connection, session_id: str | None = None, instance_id: int | None = None
) -> int:
    if session_id is None and instance_id is None:
        return 0
    where, params = [], []
    if session_id is not None:
        where.append("owner_session = ?")
        params.append(session_id)
    if instance_id is not None:
        where.append("id = ?")
        params.append(int(instance_id))
    clause = " AND ".join(where)
    ids = [int(r["id"]) for r in conn.execute(f"SELECT id FROM instances WHERE {clause}", params)]
    if not ids:
        return 0
    marks = ", ".join("?" for _ in ids)
    conn.execute(
        f"UPDATE instances SET owner_session = NULL, owner_seen_at = NULL, updated_at = ? "
        f"WHERE id IN ({marks})",
        (db.now(), *ids),
    )
    for iid in ids:
        db.add_event(conn, "instance.release", {"session_id": session_id}, instance_id=iid)
    return len(ids)


# --------------------------------------------------------------------------
# path mapping and lookup
# --------------------------------------------------------------------------


def resolve_path(conn: sqlite3.Connection, path: str) -> Resolved:
    """Map any absolute path to a project and, when known, an instance."""
    real = normalize_path(path)
    best: dict | None = None
    for project in list_projects(conn):
        ppath = normalize_path(project["path"])
        if real == ppath or real.startswith(ppath + os.sep):
            if best is None or len(ppath) > len(normalize_path(best["path"])):
                best = project
    if best is None:
        return Resolved(path=real)

    ppath = normalize_path(best["path"])
    wt_root = worktree_root(ppath)
    slug = None
    if real == wt_root or real.startswith(wt_root + os.sep):
        rest = real[len(wt_root):].strip(os.sep)
        slug = rest.split(os.sep)[0] if rest else None
    group = get_project(conn, int(best["parent_id"])) if best.get("parent_id") is not None else None
    if slug:
        row = conn.execute(
            "SELECT * FROM instances WHERE project_id = ? AND (label = ? OR path = ?)",
            (best["id"], slug, os.path.join(wt_root, slug)),
        ).fetchone()
        instance = _instance_view(conn, dict(row), best) if row else None
        return Resolved(project=best, instance=instance, label=slug, is_worktree=True,
                        slug=slug, path=real, group=group)
    main = _main_instance(conn, best["id"])
    return Resolved(
        project=best,
        instance=_instance_view(conn, main, best) if main else None,
        label=MAIN_LABEL,
        is_worktree=False,
        slug=None,
        path=real,
        group=group,
    )


def find_by_ref(conn: sqlite3.Connection, ref: int | str) -> dict:
    """'project', 'project@label' or an instance id -> instance dict."""
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        instance = get_instance(conn, int(ref))
        if instance is None:
            raise RegistryError(f"no instance with id {ref}")
        return instance
    text = str(ref).strip()
    if not text:
        raise RegistryError("empty reference")
    name, _, label = text.partition("@")
    label = label or MAIN_LABEL
    project = get_project(conn, name)
    if project is None:
        raise RegistryError(f"no project named {name!r}")
    row = conn.execute(
        "SELECT * FROM instances WHERE project_id = ? AND label = ?", (project["id"], label)
    ).fetchone()
    if row is None:
        raise RegistryError(f"project {name!r} has no instance {label!r}")
    return _instance_view(conn, dict(row), project)


def reorder_projects(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    """Set the manual order: ``ids`` get positions 0..n-1 in that sequence,
    projects not listed keep their relative order after them."""
    wanted: list[int] = []
    for raw in ids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"project id must be an integer, got {raw!r}")
        if pid not in wanted:
            wanted.append(pid)
    known = [r["id"] for r in conn.execute("SELECT id FROM projects ORDER BY sort_order, name")]
    missing = [pid for pid in wanted if pid not in known]
    if missing:
        raise ValueError(f"unknown project id(s): {missing}")
    sequence = wanted + [pid for pid in known if pid not in wanted]
    ts = db.now()
    with conn:
        for position, pid in enumerate(sequence):
            conn.execute("UPDATE projects SET sort_order = ?, updated_at = ? WHERE id = ? AND sort_order != ?",
                         (position, ts, pid, position))
    db.add_event(conn, "project.reorder", {"ids": sequence})
    return list_projects(conn)


# --------------------------------------------------------------------------
# snapshots, export / import
# --------------------------------------------------------------------------


def state_snapshot(conn: sqlite3.Connection) -> dict:
    """Everything /api/state needs except the daemon block."""
    return {
        "projects": list_projects(conn, with_instances=True),
        "observed": db.rows(conn.execute("SELECT * FROM observed ORDER BY port")),
        "reserved": db.rows(conn.execute("SELECT * FROM reserved_ports ORDER BY port")),
        "settings": db.all_settings(conn),
        "reconciled_at": db.get_setting(conn, "reconciled_at"),
    }


_EXPORT_SKIP_PROJECT = {"id", "created_at", "updated_at", "parent_id", "primary_child",
                        "children", "child_names", "primary_child_id", "instances"}
_EXPORT_INSTANCE_KEEP = ("label", "slot", "path", "branch", "port", "unit", "managed")


def export_json(conn: sqlite3.Connection) -> dict:
    projects = []
    # groups before their children so import can resolve ``parent`` by name
    for project in sorted(list_projects(conn), key=lambda p: p.get("parent_id") is not None):
        item = {k: v for k, v in project.items() if k not in _EXPORT_SKIP_PROJECT}
        item["parent"] = project.get("parent")            # group name or None
        item["primary"] = project.get("primary")          # groups: primary child name
        item["instances"] = [
            {k: row[k] for k in _EXPORT_INSTANCE_KEEP}
            for row in db.rows(
                conn.execute(
                    "SELECT * FROM instances WHERE project_id = ? ORDER BY slot", (project["id"],)
                )
            )
        ]
        projects.append(item)
    return {"version": config.VERSION, "exported_at": db.now(), "projects": projects}


def import_json(conn: sqlite3.Connection, data: dict, replace: bool = False) -> dict:
    """Recreate projects and their worktree instances from export_json output."""
    summary = {"projects_added": 0, "projects_updated": 0, "instances_added": 0, "errors": []}
    pending_primary: list[tuple[int, str]] = []
    if replace:
        for project in list_projects(conn):
            delete_project(conn, project["id"])
    for item in (data or {}).get("projects", []):
        item = dict(item)
        instances = item.pop("instances", [])
        path = item.pop("path", None)
        if not path:
            summary["errors"].append("project without a path skipped")
            continue
        name = item.pop("name", None)
        parent_name = item.pop("parent", None)
        primary_name = item.pop("primary", None)
        for key in ("children", "child_names", "primary_child_id"):
            item.pop(key, None)
        if parent_name:
            parent = get_project(conn, parent_name)
            if parent is None:
                summary["errors"].append(f"{name or path}: group {parent_name!r} not found")
                continue
            item["parent_id"] = parent["id"]
        existing = get_project(conn, name) if name else None
        if existing is None:
            existing = db.row(
                conn.execute(
                    "SELECT * FROM projects WHERE path = ?", (normalize_path(path),)
                ).fetchone()
            )
        try:
            if existing is not None:
                project = update_project(conn, existing["id"], **item)
                summary["projects_updated"] += 1
            else:
                wanted = item.pop("base_port", None)
                if wanted is not None and not port_is_free(conn, int(wanted)):
                    wanted = None
                project = add_project(
                    conn,
                    path,
                    name=name,
                    kind=item.pop("kind", "transient"),
                    start_cmd=item.pop("start_cmd", None),
                    base_port=wanted,
                    port_mode=item.pop("port_mode", "env"),
                    source=item.pop("source", "import"),
                    **item,
                )
                summary["projects_added"] += 1
        except RegistryError as exc:
            summary["errors"].append(f"{name or path}: {exc}")
            continue
        if primary_name and project.get("kind") == "group":
            pending_primary.append((project["id"], primary_name))
        for inst in instances:
            if int(inst.get("slot") or 0) == 0:
                continue
            try:
                ensure_instance(
                    conn,
                    project["id"],
                    inst["path"],
                    label=inst.get("label"),
                    branch=inst.get("branch"),
                    source="import",
                )
                summary["instances_added"] += 1
            except (RegistryError, KeyError) as exc:
                summary["errors"].append(f"{project['name']}/{inst.get('label')}: {exc}")
    for group_id, primary_name in pending_primary:
        try:
            update_project(conn, group_id, primary_child=primary_name)
        except RegistryError as exc:
            summary["errors"].append(f"primary of group {group_id}: {exc}")
    return summary
