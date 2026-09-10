"""Compare the machine with the registry: one snapshot, one transaction.

``reconcile(conn)`` reads every listening TCP socket (and, unless ``quick``,
the running containers), matches each listener to a known instance, rewrites
the ``observed`` table, moves instance state to running/stopped and refreshes
``reserved_ports``. It never starts or stops anything.

Nothing here imports ``registry`` (that module imports us); the small piece of
path mapping we need is reimplemented locally.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta

from . import config, db, sysinfo

log = logging.getLogger("portboard.reconcile")

CRASH_GRACE_SECONDS = 30
_WORKTREE_PARTS = tuple(config.WORKTREE_DIRNAME.strip("/").split("/"))


# ------------------------------------------------------------------ helpers

def _realpath(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return os.path.realpath(path).rstrip("/") or "/"
    except OSError:  # pragma: no cover - realpath is lexical, but be safe
        return path.rstrip("/")


def _is_systemd_unit(name: str | None) -> bool:
    return bool(name) and (name.endswith(".service") or name.endswith(".scope"))


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _container_name(project: dict | None, instance: dict) -> str:
    """Container backing a kind='container' instance (runner.container_name).

    ``runner`` only imports ``config`` and ``db`` at module level, so borrowing
    its pure naming helper keeps the two in step without a cycle.
    """
    from . import runner

    return runner.container_name(project or {}, instance)


def _static_ports(conn: sqlite3.Connection) -> list[int]:
    raw = db.get_setting(conn, "reserved_static", "") or ""
    out = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.append(int(chunk))
    return out


class _Machine:
    """The bits of the machine one reconcile run looks at."""

    def __init__(self, quick: bool):
        self.listeners = sysinfo.listening_ports()
        self.containers = [] if quick else sysinfo.docker_containers()
        self.docker = False if quick else sysinfo.docker_available()
        self._procs: dict[int, sysinfo.ProcInfo] = {}
        self.by_cid: dict[str, sysinfo.Container] = {}
        self.by_hostport: dict[int, sysinfo.Container] = {}
        for c in self.containers:
            if c.id:
                self.by_cid[c.id] = c
            for host_port, _cport in c.ports:
                self.by_hostport.setdefault(host_port, c)

    def proc(self, pid: int | None) -> sysinfo.ProcInfo | None:
        if not pid:
            return None
        info = self._procs.get(pid)
        if info is None:
            info = sysinfo.proc_info(pid)
            self._procs[pid] = info
        return info

    def container_by_id(self, cid: str | None) -> sysinfo.Container | None:
        if not cid:
            return None
        hit = self.by_cid.get(cid)
        if hit is not None:
            return hit
        for key, value in self.by_cid.items():
            if key.startswith(cid) or cid.startswith(key):
                return value
        return None


class _Registry:
    """Projects and instances as reconcile needs them (plain SELECTs)."""

    def __init__(self, conn: sqlite3.Connection):
        self.projects = db.rows(conn.execute("SELECT * FROM projects"))
        self.instances = db.rows(conn.execute("SELECT * FROM instances"))
        self.project_by_id = {p["id"]: p for p in self.projects}
        self.instance_by_id = {i["id"]: i for i in self.instances}
        # longest path first so a worktree wins over its project
        self._project_paths = sorted(
            ((_realpath(p["path"]), p) for p in self.projects if p["path"]),
            key=lambda pair: len(pair[0] or ""), reverse=True,
        )
        self.by_unit: dict[str, dict] = {}
        self.by_cid: dict[str, dict] = {}   # container id remembered from an earlier full reconcile
        # port -> (container name, instance) for kind='container' instances: a
        # --network host container runs as root, so ss shows no pid and docker
        # publishes no port; the assigned port is the only handle we have
        self.container_by_port: dict[int, tuple[str, dict]] = {}
        for inst in self.instances:
            if inst["unit"]:
                self.by_unit.setdefault(inst["unit"], inst)
            project = self.project_by_id.get(inst["project_id"]) or {}
            if project.get("kind") == "container":
                # a container started by hand never got a unit written, but its
                # name is derivable: index it so its listener still matches
                name = _container_name(project, inst)
                if name:
                    self.by_unit.setdefault(name, inst)
                    if inst["port"]:
                        self.container_by_port[int(inst["port"])] = (name, inst)
            try:
                cid = json.loads(inst.get("extra_json") or "{}").get("container_id")
            except (TypeError, ValueError):
                cid = None
            if cid:
                self.by_cid.setdefault(cid, inst)
        self.by_project_label = {(i["project_id"], i["label"]): i for i in self.instances}

    def resolve(self, path: str | None):
        """(project, label, instance_path) for an absolute path, else None."""
        rp = _realpath(path)
        if not rp:
            return None
        for ppath, project in self._project_paths:
            if not ppath:
                continue
            if rp == ppath or rp.startswith(ppath + os.sep):
                label, ipath = "main", ppath
                rel = os.path.relpath(rp, ppath).split(os.sep)
                if len(rel) > len(_WORKTREE_PARTS) and tuple(rel[:len(_WORKTREE_PARTS)]) == _WORKTREE_PARTS:
                    label = rel[len(_WORKTREE_PARTS)]
                    ipath = os.path.join(ppath, *_WORKTREE_PARTS, label)
                return project, label, ipath
        return None

    def instance_by_cid(self, cid: str | None) -> dict | None:
        if not cid:
            return None
        hit = self.by_cid.get(cid)
        if hit is not None:
            return hit
        for key, inst in self.by_cid.items():
            if key.startswith(cid) or cid.startswith(key):
                return inst
        return None

    def instance_for(self, project: dict, label: str) -> dict | None:
        return self.by_project_label.get((project["id"], label))

    def next_slot(self, project_id: int) -> int:
        used = {i["slot"] for i in self.instances if i["project_id"] == project_id}
        limit = 9
        project = self.project_by_id.get(project_id) or {}
        if project.get("slots"):
            limit = int(project["slots"])
        for n in range(1, limit + 1):
            if n not in used:
                return n
        return (max(used) + 1) if used else 1


# ------------------------------------------------------------------ matching

def _describe(machine: _Machine, listener: sysinfo.Listener) -> dict:
    """Everything we know about one listener before looking at the registry."""
    info = machine.proc(listener.pid)
    unit = info.unit if info else None
    cid = info.container if info else None
    container = machine.container_by_id(cid)
    if container is None and listener.pid is None:
        # docker-proxy publishes bridge ports as root: no pid in ss output
        container = machine.by_hostport.get(listener.port)
    return {
        "port": listener.port,
        "proto": listener.proto,
        "bind": listener.bind,
        "pid": listener.pid,
        "comm": (info.comm if info else None) or listener.comm,
        "cwd": info.cwd if info else None,
        "cmdline": info.cmdline if info else None,
        "unit": unit,
        "container": container.name if container else (cid[:12] if cid else None),
        "compose_project": container.compose_project if container else None,
        "compose_workdir": container.compose_workdir if container else None,
        "_container": container,
        "_cid": (container.id if container else None) or cid,
        "_in_container": bool(container or cid),
    }


def _match(reg: _Registry, seen: dict):
    """(instance, project, label, path) - any of them may be None."""
    for key in (seen["unit"], seen["container"], seen["compose_project"]):
        inst = reg.by_unit.get(key) if key else None
        if inst is not None:
            return inst, reg.project_by_id.get(inst["project_id"]), inst["label"], inst["path"]
    inst = reg.instance_by_cid(seen.get("_cid"))
    if inst is not None:
        return inst, reg.project_by_id.get(inst["project_id"]), inst["label"], inst["path"]
    # a process inside a container reports the container's cwd (/app), useless here
    candidates = [seen["compose_workdir"]] if seen["_in_container"] else [seen["cwd"]]
    for path in candidates:
        resolved = reg.resolve(path)
        if resolved is None:
            continue
        project, label, ipath = resolved
        inst = reg.instance_for(project, label)
        return inst, project, label, path
    return None, None, None, None


# ------------------------------------------------------------------- writing

_INSTANCE_COLUMNS = (
    "project_id, label, slot, path, port, unit, managed, state, pid, actual_port, "
    "started_at, last_seen_at, extra_json, created_at, updated_at"
)


def _adopt(conn: sqlite3.Connection, reg: _Registry, plan: dict, ts: str) -> int | None:
    """Create a managed=0 instance row for an unknown listener in a project."""
    project, label = plan["project"], plan["label"]
    existing = reg.instance_for(project, label)
    if existing is not None:  # a second listener in the same checkout
        return existing["id"]
    slot = 0 if label == "main" else reg.next_slot(project["id"])
    extra = json.dumps({"source": "adopted", "adopted_at": ts})
    values = [project["id"], label, slot, plan["path"], plan["port"], plan["unit"], 0,
              "running", plan["pid"], plan["port"], ts, ts, extra, ts, ts]
    sql = f"INSERT INTO instances({_INSTANCE_COLUMNS}) VALUES ({','.join('?' * len(values))})"
    try:
        cur = conn.execute(sql, values)
    except sqlite3.IntegrityError:
        # the port is already assigned elsewhere: keep the row, drop the port
        values[4] = None
        try:
            cur = conn.execute(sql, values)
        except sqlite3.IntegrityError as exc:
            log.warning("cannot adopt port %s in %s: %s", plan["port"], project["name"], exc)
            return None
    new_id = cur.lastrowid
    row = db.row(conn.execute("SELECT * FROM instances WHERE id = ?", (new_id,)).fetchone())
    if row:  # keep the in-memory registry consistent for later listeners
        reg.instances.append(row)
        reg.instance_by_id[new_id] = row
        reg.by_project_label[(project["id"], label)] = row
        if row["unit"]:
            reg.by_unit.setdefault(row["unit"], row)
    db.add_event(conn, "instance.adopt", {"port": plan["port"], "label": label, "path": plan["path"]},
                 project_id=project["id"], instance_id=new_id)
    return new_id


def reconcile(conn: sqlite3.Connection, quick: bool = False, adopt_unknown: bool = False) -> dict:
    """Refresh observed/instances/reserved_ports from reality. See DESIGN.md."""
    t0 = time.monotonic()
    ts = db.now()
    machine = _Machine(quick)
    reg = _Registry(conn)

    observed_rows: list[dict] = []           # what goes into `observed`
    matched_ports: dict[int, list[int]] = {}  # instance id -> ports seen
    matched_meta: dict[int, dict] = {}        # instance id -> the winning listener
    unknown_in_projects: list[dict] = []
    adopt_plans: list[dict] = []
    reserved_seen: dict[int, str] = {}
    unknown = 0

    running_containers = {c.name for c in machine.containers
                          if str(getattr(c, "state", "")).lower() == "running"}
    for listener in machine.listeners:
        seen = _describe(machine, listener)
        inst, project, label, path = _match(reg, seen)
        if inst is None and seen["pid"] is None and listener.port in reg.container_by_port:
            name, candidate = reg.container_by_port[listener.port]
            if name in running_containers:
                inst = candidate
                project = reg.project_by_id.get(inst["project_id"])
                label, path = inst["label"], inst["path"]
                seen["container"] = name
                seen["_in_container"] = True
        row = {k: v for k, v in seen.items() if not k.startswith("_")}
        row["project_id"] = project["id"] if project else None
        row["instance_id"] = inst["id"] if inst else None
        row["seen_at"] = ts
        row["_adopt_key"] = None
        if inst is not None:
            matched_ports.setdefault(inst["id"], []).append(listener.port)
            matched_meta.setdefault(inst["id"], seen)
        elif project is not None:
            unknown += 1
            hint = {"port": listener.port, "cwd": path, "project": project["name"]}
            if hint not in unknown_in_projects:  # one entry per port, not per socket
                unknown_in_projects.append(hint)
            if adopt_unknown:
                key = (project["id"], label)
                row["_adopt_key"] = key
                if key not in {p["key"] for p in adopt_plans}:
                    resolved = reg.resolve(path)
                    ipath = resolved[2] if resolved else path
                    adopt_plans.append({
                        "key": key, "project": project, "label": label, "path": ipath,
                        "port": listener.port, "pid": listener.pid,
                        "unit": seen["unit"] or seen["container"],
                    })
        else:
            unknown += 1
            reserved_seen.setdefault(
                listener.port, seen["comm"] or seen["container"] or "system")
        observed_rows.append(row)

    # one systemctl call for every matched instance backed by a systemd unit
    units = {reg.instance_by_id[i]["unit"] for i in matched_ports
             if _is_systemd_unit(reg.instance_by_id[i]["unit"])}
    stats = sysinfo.unit_cgroup_stats(sorted(units)) if units else {}

    started: list[int] = []
    stopped: list[int] = []
    static = _static_ports(conn)
    static_set = set(static)

    conn.execute("BEGIN")
    try:
        for port in static:
            conn.execute(
                "INSERT INTO reserved_ports(port, label, source, updated_at) VALUES (?, 'static', 'static', ?) "
                "ON CONFLICT(port) DO UPDATE SET source = 'static', label = 'static', updated_at = excluded.updated_at",
                (port, ts))

        adopted: dict[tuple, int | None] = {}
        for plan in adopt_plans:
            new_id = _adopt(conn, reg, plan, ts)
            adopted[plan["key"]] = new_id
            if new_id is not None:
                started.append(new_id)

        conn.execute("DELETE FROM observed")
        for row in observed_rows:
            key = row.pop("_adopt_key")
            if key is not None and adopted.get(key):
                row["instance_id"] = adopted[key]
            conn.execute(
                "INSERT OR REPLACE INTO observed(port, proto, bind, pid, comm, cwd, cmdline, unit, container,"
                " compose_project, compose_workdir, project_id, instance_id, seen_at)"
                " VALUES (:port, :proto, :bind, :pid, :comm, :cwd, :cmdline, :unit, :container,"
                " :compose_project, :compose_workdir, :project_id, :instance_id, :seen_at)", row)

        for inst_id, ports in matched_ports.items():
            inst = reg.instance_by_id[inst_id]
            meta = matched_meta[inst_id]
            actual = inst["port"] if inst["port"] in ports else min(ports)
            stat = stats.get(inst["unit"] or "", {})
            fields = {
                "state": "running",
                "pid": meta["pid"] or (stat.get("MainPID") or None),
                "actual_port": actual,
                "last_seen_at": ts,
                "mem_bytes": stat.get("MemoryCurrent", inst["mem_bytes"]),
                "cpu_ns": stat.get("CPUUsageNSec", inst["cpu_ns"]),
                "updated_at": ts,
            }
            if stat:
                fields["cpu_checked_at"] = ts
            cid = meta.get("_cid")
            if cid and len(cid) >= 12:
                try:
                    extra = json.loads(inst.get("extra_json") or "{}")
                except (TypeError, ValueError):
                    extra = {}
                if extra.get("container_id") != cid:
                    extra["container_id"] = cid
                    fields["extra_json"] = json.dumps(extra)
            if inst["state"] != "running":
                started.append(inst_id)
                fields["stopped_at"] = None
                fields["stopped_by"] = None
                fields["started_at"] = inst["started_at"] or ts if inst["state"] == "starting" else ts
                if inst["state"] != "starting" and not inst["unit"]:
                    # nobody here started it (the runner always records its unit
                    # first): it is the user's own service, so remember what runs
                    # it and never treat its disappearance as a crash
                    fields["managed"] = 0
                    backing = meta.get("unit") or meta.get("container")
                    if backing:
                        fields["unit"] = backing
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE instances SET {sets} WHERE id = ?", [*fields.values(), inst_id])

        limit = datetime.now() - timedelta(seconds=CRASH_GRACE_SECONDS)
        for inst in reg.instances:
            if inst["id"] in matched_ports or inst["id"] in adopted.values():
                continue
            if inst["state"] != "running":
                continue  # 'starting' belongs to the runner, stopped/failed stay
            kind = (reg.project_by_id.get(inst["project_id"]) or {}).get("kind")
            if kind == "group":
                # a group has no listener of its own; its row mirrors the
                # primary child and must never be marked crashed
                continue
            if not machine.docker and kind in ("compose", "none", "container"):
                # quick mode did not look at docker: a bridge-published or
                # host-network container is simply invisible here, not gone
                continue
            recent_stop = _parse_ts(inst["stopped_at"])
            fields = {"state": "stopped", "pid": None, "actual_port": None, "updated_at": ts}
            if inst["managed"] and not (recent_stop and recent_stop >= limit):
                fields["stopped_at"] = ts
                fields["stopped_by"] = "crash"
            elif not inst["managed"]:
                fields["stopped_at"] = inst["stopped_at"] or ts
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE instances SET {sets} WHERE id = ?", [*fields.values(), inst["id"]])
            stopped.append(inst["id"])
            db.add_event(conn, "instance.lost", {"port": inst["port"], "by": fields.get("stopped_by")},
                         project_id=inst["project_id"], instance_id=inst["id"])

        for port, label in reserved_seen.items():
            if port in static_set:
                continue
            conn.execute(
                "INSERT INTO reserved_ports(port, label, source, updated_at) VALUES (?, ?, 'observed', ?) "
                "ON CONFLICT(port) DO UPDATE SET label = excluded.label, updated_at = excluded.updated_at "
                "WHERE reserved_ports.source = 'observed'", (port, label, ts))
        keep = [p for p in reserved_seen if p not in static_set]
        if keep:
            marks = ",".join("?" * len(keep))
            conn.execute(f"DELETE FROM reserved_ports WHERE source = 'observed' AND port NOT IN ({marks})", keep)
        else:
            conn.execute("DELETE FROM reserved_ports WHERE source = 'observed'")
        reserved = conn.execute("SELECT COUNT(*) FROM reserved_ports").fetchone()[0]

        summary = {
            "listeners": len(observed_rows),
            "matched": len(matched_ports),
            "unknown": unknown,
            "unknown_in_projects": unknown_in_projects,
            "started": started,
            "stopped": stopped,
            "reserved": reserved,
            "docker": machine.docker,
            "took_ms": int((time.monotonic() - t0) * 1000),
        }
        db.set_setting(conn, "reconciled_at", ts)
        db.add_event(conn, "reconcile", summary)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    summary["took_ms"] = int((time.monotonic() - t0) * 1000)
    log.debug("reconcile %s", summary)
    return summary
