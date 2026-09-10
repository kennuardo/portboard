#!/usr/bin/env python3
"""Mock Portboard backend for exercising the GUI by hand.

Serves ``portboard/static/index.html`` at ``/`` and answers every route of the
HTTP API contract in docs/DESIGN.md with a realistic in-memory fixture, so the
page can be clicked through without the real daemon, systemd or docker.

    python3 tests/gui_mock_server.py 8791

The fixture mutates: start/stop/restart/release/adopt/pin/settings/register all
change the in-memory state, so buttons visibly do something. Nothing touches
the disk, the database or any real process. Stdlib only.

``GET /?shot=1`` serves the same page with a deliberately slow 1x1 image
appended: it holds back the window load event for ~2.5 s so that headless
screenshot tools (``firefox --headless --screenshot``) capture the page after
/api/state has arrived and rendered. Test aid only, never used by the GUI.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(REPO, "portboard", "static", "index.html")

LOCK = threading.Lock()


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def ago(**kw) -> str:
    return (datetime.now() - timedelta(**kw)).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# Fixture
# --------------------------------------------------------------------------

PROJECTS: list[dict] = [
    {
        "id": 1,
        "name": "sheron",
        "path": "/mnt/hyper/Projects/sheron-world",
        "kind": "transient",
        "start_cmd": "npm run dev",
        "stop_cmd": None,
        "port_mode": "env",
        "base_port": 3300,
        "slots": 9,
        "health_path": "/",
        "open_path": "/sheron-world/magazin",
        "pinned": 0,
        "autostart": "schedule",
        "env_json": '{"NUXT_TELEMETRY_DISABLED": "1"}',
        "path_prepend": "/home/miro/.nvm/versions/node/v20.20.2/bin",
        "memory_max": None,
        "source": "discover",
        "notes": "nuxt 3 <dev only> & staging content",
        "created_at": ago(days=41),
        "updated_at": ago(hours=3),
    },
    {
        "id": 2,
        "name": "invoicer",
        "path": "/mnt/hyper/Projects/invoicer",
        "kind": "compose",
        "start_cmd": None,
        "stop_cmd": None,
        "port_mode": "fixed",
        "base_port": 4010,
        "slots": 9,
        "health_path": "/healthz",
        "open_path": "/",
        "pinned": 1,
        "autostart": "never",
        "env_json": "{}",
        "path_prepend": None,
        "memory_max": "2G",
        "source": "cli",
        "notes": "postgres + api + web, pinned so the schedule leaves it alone",
        "created_at": ago(days=120),
        "updated_at": ago(days=2),
    },
    {
        "id": 3,
        "name": "legacy-crm",
        "path": "/mnt/hyper/Projects/legacy-crm",
        "kind": "none",
        "start_cmd": None,
        "stop_cmd": None,
        "port_mode": "fixed",
        "base_port": 9000,
        "slots": 9,
        "health_path": "/",
        "open_path": "/",
        "pinned": 0,
        "autostart": "never",
        "env_json": "{}",
        "path_prepend": None,
        "memory_max": None,
        "source": "adopted",
        "notes": "started by hand in a tmux, we only observe it",
        "created_at": ago(days=8),
        "updated_at": ago(days=8),
    },
]

INSTANCES: list[dict] = [
    {
        "id": 1,
        "project_id": 1,
        "label": "main",
        "slot": 0,
        "path": "/mnt/hyper/Projects/sheron-world",
        "branch": "main",
        "port": 3300,
        "unit": "portboard-sheron-main.service",
        "managed": 1,
        "state": "running",
        "pid": 214233,
        "actual_port": 3300,
        "owner_session": "9f31c0ab-4d7e-4a10-9f01-2b7c6de11c45",
        "owner_seen_at": ago(minutes=6),
        "started_at": ago(hours=3, minutes=12),
        "stopped_at": ago(days=1, hours=4),
        "stopped_by": "schedule",
        "last_seen_at": ago(minutes=1),
        "mem_bytes": 1_284_308_992,
        "cpu_ns": 184_400_000_000,
        "cpu_checked_at": ago(minutes=1),
        "idle_since": None,
        "extra_json": "{}",
        "created_at": ago(days=41),
        "updated_at": ago(minutes=1),
    },
    {
        "id": 2,
        "project_id": 1,
        "label": "fix-login-timeout",
        "slot": 1,
        "path": "/mnt/hyper/Projects/sheron-world/.claude/worktrees/fix-login-timeout",
        "branch": "worktree-fix-login-timeout",
        "port": 3301,
        "unit": "portboard-sheron-fix-login-timeout.service",
        "managed": 1,
        "state": "stopped",
        "pid": None,
        "actual_port": None,
        "owner_session": None,
        "owner_seen_at": None,
        "started_at": ago(days=1, hours=9),
        "stopped_at": ago(hours=19),
        "stopped_by": "idle",
        "last_seen_at": ago(hours=19),
        "mem_bytes": None,
        "cpu_ns": 41_200_000_000,
        "cpu_checked_at": ago(hours=19),
        "idle_since": None,
        "extra_json": "{}",
        "created_at": ago(days=2),
        "updated_at": ago(hours=19),
    },
    {
        "id": 3,
        "project_id": 2,
        "label": "main",
        "slot": 0,
        "path": "/mnt/hyper/Projects/invoicer",
        "branch": "production",
        "port": 4010,
        "unit": "invoicer",
        "managed": 1,
        "state": "running",
        "pid": 88214,
        "actual_port": 4011,
        "owner_session": None,
        "owner_seen_at": None,
        "started_at": ago(days=2, hours=1),
        "stopped_at": None,
        "stopped_by": None,
        "last_seen_at": ago(minutes=1),
        "mem_bytes": 402_653_184,
        "cpu_ns": 9_600_000_000,
        "cpu_checked_at": ago(minutes=1),
        "idle_since": ago(minutes=44),
        "extra_json": '{"container": "invoicer-web-1"}',
        "created_at": ago(days=120),
        "updated_at": ago(minutes=1),
    },
    {
        "id": 4,
        "project_id": 3,
        "label": "main",
        "slot": 0,
        "path": "/mnt/hyper/Projects/legacy-crm",
        "branch": "master",
        "port": 9000,
        "unit": None,
        "managed": 0,
        "state": "running",
        "pid": 30122,
        "actual_port": 9000,
        "owner_session": None,
        "owner_seen_at": None,
        "started_at": None,
        "stopped_at": None,
        "stopped_by": None,
        "last_seen_at": ago(minutes=1),
        "mem_bytes": 168_820_736,
        "cpu_ns": 1_450_000_000,
        "cpu_checked_at": ago(minutes=1),
        "idle_since": None,
        "extra_json": "{}",
        "created_at": ago(days=8),
        "updated_at": ago(minutes=1),
    },
    {
        "id": 5,
        "project_id": 2,
        "label": "vat-rewrite",
        "slot": 1,
        "path": "/mnt/hyper/Projects/invoicer/.claude/worktrees/vat-rewrite",
        "branch": "worktree-vat-rewrite",
        "port": 4011,
        "unit": "invoicer-vat-rewrite",
        "managed": 1,
        "state": "failed",
        "pid": None,
        "actual_port": None,
        "owner_session": "c7d2be40-0e55-49aa-b6f0-118aa8c9d3e1",
        "owner_seen_at": ago(minutes=32),
        "started_at": ago(minutes=35),
        "stopped_at": ago(minutes=34),
        "stopped_by": "crash",
        "last_seen_at": ago(minutes=34),
        "mem_bytes": None,
        "cpu_ns": 900_000_000,
        "cpu_checked_at": ago(minutes=34),
        "idle_since": None,
        "extra_json": "{}",
        "created_at": ago(minutes=40),
        "updated_at": ago(minutes=34),
    },
]

# --------------------------------------------------------------------------
# kind 'group': /mnt/hyper/Projects/rma, one app spread over five repos.
# The group owns no port: its single main instance mirrors the primary child
# (rma-admin-app :3100) and carries `services`, one main instance per child.
# --------------------------------------------------------------------------


def _rma_child(pid: int, name: str, kind: str, port: int, start_cmd=None,
               port_mode: str = "fixed", notes=None) -> dict:
    return {
        "id": pid, "name": name,
        "path": "/mnt/hyper/Projects/rma/" + name[len("rma-"):],
        "kind": kind, "start_cmd": start_cmd, "stop_cmd": None,
        "port_mode": port_mode, "base_port": port, "slots": 9,
        "health_path": "/", "open_path": "/", "pinned": 0, "autostart": "schedule",
        "env_json": "{}", "path_prepend": None, "memory_max": None,
        "source": "discover", "notes": notes, "parent_id": 4, "primary_child": None,
        "created_at": ago(days=6), "updated_at": ago(hours=2),
    }


PROJECTS += [
    {
        "id": 4, "name": "rma", "path": "/mnt/hyper/Projects/rma", "kind": "group",
        "start_cmd": None, "stop_cmd": None, "port_mode": "none", "base_port": None,
        "slots": 1, "health_path": "/", "open_path": "/", "pinned": 0,
        "autostart": "schedule", "env_json": "{}", "path_prepend": None,
        "memory_max": None, "source": "discover",
        "notes": "multi-repo app: two front ends, an api, mariadb and a mail catcher",
        "parent_id": None, "primary_child": None,
        "created_at": ago(days=6), "updated_at": ago(hours=2),
    },
    _rma_child(5, "rma-mariadb", "container", 3306, start_cmd="rma-mariadb",
               notes="existing docker container, started by name"),
    _rma_child(6, "rma-mailpit", "container", 8025, start_cmd="rma-mailpit"),
    _rma_child(7, "rma-server-side", "transient", 8080,
               start_cmd="./mvnw quarkus:dev -Dquarkus.http.port={port}", port_mode="arg"),
    _rma_child(8, "rma-customer-app", "none", 3101, notes="observed only for now"),
    _rma_child(9, "rma-admin-app", "transient", 3100, start_cmd="npm run dev", port_mode="env",
               notes="the primary: the group card shows this port and state"),
]


def _rma_instance(iid: int, project_id: int, port, label: str = "main", slot: int = 0,
                  state: str = "stopped", unit=None, path=None, branch="main") -> dict:
    proj = next(p for p in PROJECTS if p["id"] == project_id)
    running = state == "running"
    return {
        "id": iid, "project_id": project_id, "label": label, "slot": slot,
        "path": path or proj["path"], "branch": branch, "port": port,
        "unit": unit, "managed": 0 if proj["kind"] == "none" else 1,
        "state": state, "pid": 40000 + iid if running else None,
        "actual_port": port if running else None,
        "owner_session": None, "owner_seen_at": None,
        "started_at": ago(hours=2) if running else ago(days=1),
        "stopped_at": None if running else ago(hours=13),
        "stopped_by": None if running else "schedule",
        "last_seen_at": ago(minutes=1) if running else ago(hours=13),
        "mem_bytes": 210 * 1024 * 1024 if running else None,
        "cpu_ns": 8 * 10 ** 9 if running else 1 * 10 ** 9,
        "cpu_checked_at": ago(minutes=1), "idle_since": None, "extra_json": "{}",
        "created_at": ago(days=6), "updated_at": ago(minutes=1),
    }


INSTANCES += [
    _rma_instance(6, 5, 3306, state="running", unit="docker-rma-mariadb.scope"),
    _rma_instance(7, 6, 8025, state="running", unit="docker-rma-mailpit.scope"),
    _rma_instance(8, 7, 8080, state="running", unit="portboard-rma-server-side-main.service"),
    _rma_instance(9, 8, 3101),
    _rma_instance(10, 9, 3100, unit="portboard-rma-admin-app-main.service"),
    _rma_instance(11, 4, None, unit=None),          # the group's mirror instance
    _rma_instance(12, 9, 3102, label="csv-attributes", slot=1,
                  path="/mnt/hyper/Projects/rma/admin-app/.claude/worktrees/csv-attributes",
                  branch="worktree-csv-attributes",
                  unit="portboard-rma-admin-app-csv-attributes.service"),
]

OBSERVED: list[dict] = [
    {
        "port": 3306, "proto": "tcp", "bind": "0.0.0.0", "pid": 40006,
        "comm": "docker-proxy", "cwd": None,
        "cmdline": "/usr/bin/docker-proxy -proto tcp -host-port 3306",
        "unit": "docker.service", "container": "rma-mariadb",
        "compose_project": None, "compose_workdir": "/mnt/hyper/Projects/rma/mariadb",
        "project_id": 5, "instance_id": 6, "seen_at": ago(minutes=1),
    },
    {
        "port": 8025, "proto": "tcp", "bind": "0.0.0.0", "pid": 40007,
        "comm": "docker-proxy", "cwd": None,
        "cmdline": "/usr/bin/docker-proxy -proto tcp -host-port 8025",
        "unit": "docker.service", "container": "rma-mailpit",
        "compose_project": None, "compose_workdir": "/mnt/hyper/Projects/rma/mailpit",
        "project_id": 6, "instance_id": 7, "seen_at": ago(minutes=1),
    },
    {
        "port": 8080, "proto": "tcp", "bind": "127.0.0.1", "pid": 40008,
        "comm": "java", "cwd": "/mnt/hyper/Projects/rma/server-side",
        "cmdline": "java -jar quarkus-run.jar",
        "unit": "portboard-rma-server-side-main.service", "container": None,
        "compose_project": None, "compose_workdir": None,
        "project_id": 7, "instance_id": 8, "seen_at": ago(minutes=1),
    },
    {
        "port": 3300, "proto": "tcp", "bind": "127.0.0.1", "pid": 214233,
        "comm": "node", "cwd": "/mnt/hyper/Projects/sheron-world",
        "cmdline": "node /mnt/hyper/Projects/sheron-world/node_modules/.bin/nuxt dev",
        "unit": "portboard-sheron-main.service", "container": None,
        "compose_project": None, "compose_workdir": None,
        "project_id": 1, "instance_id": 1, "seen_at": ago(minutes=1),
    },
    {
        "port": 3399, "proto": "tcp", "bind": "0.0.0.0", "pid": 215871,
        "comm": "node", "cwd": "/mnt/hyper/Projects/sheron-world/tools/preview",
        "cmdline": "node tools/preview/server.mjs --port 3399",
        "unit": "vte-spawn-4b1f.scope", "container": None,
        "compose_project": None, "compose_workdir": None,
        "project_id": 1, "instance_id": None, "seen_at": ago(minutes=1),
    },
    {
        "port": 4011, "proto": "tcp", "bind": "0.0.0.0", "pid": 88214,
        "comm": "docker-proxy", "cwd": None,
        "cmdline": "/usr/bin/docker-proxy -proto tcp -host-ip 0.0.0.0 -host-port 4011",
        "unit": "docker.service", "container": "invoicer-web-1",
        "compose_project": "invoicer", "compose_workdir": "/mnt/hyper/Projects/invoicer",
        "project_id": 2, "instance_id": 3, "seen_at": ago(minutes=1),
    },
    {
        "port": 9000, "proto": "tcp", "bind": "127.0.0.1", "pid": 30122,
        "comm": "php-fpm", "cwd": "/mnt/hyper/Projects/legacy-crm",
        "cmdline": "php -S 127.0.0.1:9000 -t public",
        "unit": None, "container": None,
        "compose_project": None, "compose_workdir": None,
        "project_id": 3, "instance_id": 4, "seen_at": ago(minutes=1),
    },
    {
        "port": 1234, "proto": "tcp", "bind": "0.0.0.0", "pid": 4421,
        "comm": "lms", "cwd": "/home/miro",
        "cmdline": "/home/miro/.lmstudio/bin/lms server start",
        "unit": "llmster.service", "container": None,
        "compose_project": None, "compose_workdir": None,
        "project_id": None, "instance_id": None, "seen_at": ago(minutes=1),
    },
    {
        "port": 53, "proto": "tcp", "bind": "127.0.0.53", "pid": None,
        "comm": None, "cwd": None, "cmdline": None,
        "unit": None, "container": None,
        "compose_project": None, "compose_workdir": None,
        "project_id": None, "instance_id": None, "seen_at": ago(minutes=1),
    },
    {
        "port": 8644, "proto": "tcp", "bind": "0.0.0.0", "pid": 2210,
        "comm": "python3", "cwd": "/mnt/hyper/Projects/Notify",
        "cmdline": "python3 -m notify.server",
        "unit": "notify-tts.service", "container": None,
        "compose_project": None, "compose_workdir": None,
        "project_id": None, "instance_id": None, "seen_at": ago(minutes=1),
    },
]

RESERVED: list[dict] = [
    {"port": 22, "label": "static", "source": "static", "updated_at": ago(days=8)},
    {"port": 53, "label": "static", "source": "static", "updated_at": ago(days=8)},
    {"port": 631, "label": "static", "source": "static", "updated_at": ago(days=8)},
    {"port": 1234, "label": "lms", "source": "observed", "updated_at": ago(minutes=1)},
    {"port": 8644, "label": "notify-tts", "source": "observed", "updated_at": ago(minutes=1)},
    {"port": 8790, "label": "static", "source": "static", "updated_at": ago(days=8)},
    {"port": 5432, "label": "docker: postgres-dev", "source": "observed", "updated_at": ago(minutes=1)},
]

SETTINGS: dict[str, str] = {
    "daemon_port": "8790",
    "pool_start": "4000",
    "pool_end": "4990",
    "pool_step": "10",
    "slots": "9",
    "memory_max": "4G",
    "idle_minutes": "90",
    "evening_stop": "16:30",
    "morning_start": "07:30",
    "morning_days": "Mon..Fri",
    "idle_exit_seconds": "600",
    "node_path": "/home/miro/.nvm/versions/node/v20.20.2/bin",
    "notify_on_schedule": "0",
    "reserved_static": "22,53,631,1234,8644,8790",
    "start_timeout": "60",
}

EVENTS: list[dict] = [
    {"id": 812, "ts": ago(minutes=1), "kind": "reconcile", "project_id": None, "instance_id": None,
     "detail": '{"listeners": 7, "matched": 4, "unknown": 3, "changed": 1, "reserved": 7}'},
    {"id": 811, "ts": ago(minutes=34), "kind": "instance.failed", "project_id": 2, "instance_id": 5,
     "detail": '{"unit": "invoicer-vat-rewrite", "exit": 1, "reason": "port 4011 already in use"}'},
    {"id": 810, "ts": ago(minutes=35), "kind": "instance.start", "project_id": 2, "instance_id": 5,
     "detail": '{"port": 4011, "source": "mcp", "session": "c7d2be40"}'},
    {"id": 809, "ts": ago(hours=3, minutes=12), "kind": "instance.start", "project_id": 1, "instance_id": 1,
     "detail": '{"port": 3300, "unit": "portboard-sheron-main.service", "waited_ms": 4210}'},
    {"id": 808, "ts": ago(hours=3, minutes=13), "kind": "schedule.start", "project_id": None, "instance_id": None,
     "detail": '{"started": [1], "skipped": [3], "errors": []}'},
    {"id": 807, "ts": ago(hours=19), "kind": "instance.stop", "project_id": 1, "instance_id": 2,
     "detail": '{"reason": "idle", "idle_minutes": 92}'},
    {"id": 806, "ts": ago(days=1, hours=4), "kind": "schedule.stop", "project_id": None, "instance_id": None,
     "detail": '{"stopped": [1, 2], "skipped": [3], "errors": []}'},
    {"id": 805, "ts": ago(days=1, hours=6), "kind": "port.reserved", "project_id": None, "instance_id": None,
     "detail": "port 5432 held by docker: postgres-dev"},
    {"id": 804, "ts": ago(days=2), "kind": "project.add", "project_id": 3, "instance_id": None,
     "detail": '{"name": "legacy-crm", "source": "adopted", "path": "/mnt/hyper/Projects/legacy-crm"}'},
    {"id": 803, "ts": ago(days=2, hours=1), "kind": "instance.adopt", "project_id": 3, "instance_id": 4,
     "detail": '{"port": 9000, "comm": "php-fpm", "managed": 0}'},
]

RECONCILED_AT = [ago(minutes=1)]
NEXT_ID = [900]


def next_id() -> int:
    NEXT_ID[0] += 1
    return NEXT_ID[0]


def event(kind: str, detail, project_id=None, instance_id=None) -> None:
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    EVENTS.insert(0, {"id": next_id(), "ts": now(), "kind": kind,
                      "project_id": project_id, "instance_id": instance_id, "detail": detail})
    del EVENTS[200:]


def project_by_id(pid):
    return next((p for p in PROJECTS if p["id"] == pid), None)


def instance_by_id(iid):
    return next((i for i in INSTANCES if i["id"] == iid), None)


def children_of(group_id) -> list[dict]:
    """Child projects of a group, in display order (the fixture order)."""
    return [p for p in PROJECTS if p.get("parent_id") == group_id]


def main_instance_of(project_id):
    return next((i for i in INSTANCES if i["project_id"] == project_id and i["slot"] == 0), None)


def pick_primary(group: dict):
    """Explicit primary_child, else the front end a human would open."""
    kids = children_of(group["id"])
    if group.get("primary_child"):
        explicit = next((k for k in kids if k["id"] == int(group["primary_child"])), None)
        if explicit:
            return explicit
    if not kids:
        return None
    return sorted(kids, key=lambda k: (0 if k["kind"] not in ("container", "none") else 1,
                                       k.get("base_port") or 99_999, k["name"]))[0]


def decorate(project: dict) -> dict:
    """Project row plus the computed group fields registry._decorate() adds."""
    row = dict(project)
    row.setdefault("parent_id", None)
    row.setdefault("primary_child", None)
    parent = project_by_id(row["parent_id"]) if row["parent_id"] is not None else None
    row["parent"] = parent["name"] if parent else None
    if row.get("kind") == "group":
        kids = children_of(row["id"])
        row["children"] = [k["id"] for k in kids]
        row["child_names"] = [k["name"] for k in kids]
        primary = pick_primary(row)
        row["primary_child_id"] = primary["id"] if primary else None
        row["primary"] = primary["name"] if primary else None
    return row


_MIRRORED = ("state", "port", "actual_port", "url", "pid", "started_at", "stopped_at",
             "stopped_by", "mem_bytes", "cpu_ns", "idle_since", "unit", "managed")


def mirror_primary(view: dict, group: dict) -> None:
    """A group's main instance shows its primary child's main instance, plus
    ``services`` (every child's main instance, display order) for the GUI."""
    group = decorate(group)
    services = []
    for kid in children_of(group["id"]):
        main = main_instance_of(kid["id"])
        if main is not None:
            services.append(inst_json(main))
    view["services"] = services
    view["primary"] = group.get("primary")
    view["services_running"] = sum(1 for s in services if s["state"] == "running")
    view["services_total"] = len(services)
    primary = next((s for s in services if s["project_id"] == group.get("primary_child_id")), None)
    if primary is None:
        return
    for key in _MIRRORED:
        view[key] = primary.get(key)
    view["primary_instance_id"] = primary["id"]


def inst_json(inst: dict) -> dict:
    """Instance row plus the computed fields the API contract promises."""
    proj = project_by_id(inst["project_id"]) or {}
    out = dict(inst)
    port = inst.get("actual_port") or inst.get("port")
    out["project"] = proj.get("name")
    out["kind"] = proj.get("kind")
    out["pinned"] = proj.get("pinned", 0)
    out["worktree"] = inst.get("slot", 0) > 0
    out["parent"] = decorate(proj).get("parent") if proj else None
    out["url"] = (
        f"http://localhost:{port}{proj.get('open_path', '/')}"
        if port and inst.get("state") == "running" and proj.get("port_mode") != "none"
        else None
    )
    if proj.get("kind") == "group" and not inst.get("slot"):
        mirror_primary(out, proj)
    return out


def state_json() -> dict:
    projects = []
    for p in PROJECTS:
        row = decorate(p)
        row["instances"] = [inst_json(i) for i in INSTANCES if i["project_id"] == p["id"]]
        row["instances"].sort(key=lambda i: i["slot"])
        projects.append(row)
    return {
        "projects": projects,
        "observed": sorted(OBSERVED, key=lambda o: o["port"]),
        "reserved": sorted(RESERVED, key=lambda r: r["port"]),
        "settings": dict(SETTINGS),
        "reconciled_at": RECONCILED_AT[0],
        "daemon": {
            "pid": os.getpid(),
            "version": "0.1.0-mock",
            "socket_activated": True,
            "port": 8790,
            "idle_exit_seconds": int(SETTINGS["idle_exit_seconds"]),
            "started_at": ago(minutes=12),
        },
    }


def observed_for_instance(inst: dict) -> dict | None:
    return next((o for o in OBSERVED if o.get("instance_id") == inst["id"]), None)


def group_start_order(group: dict) -> list[dict]:
    """Children in start order: display order with the primary (the front end)
    moved last, so the backing services are up before it boots."""
    kids = children_of(group["id"])
    primary = pick_primary(group)
    if primary is None:
        return kids
    return [k for k in kids if k["id"] != primary["id"]] + [primary]


def do_group_start(inst: dict, group: dict) -> dict:
    started = []
    for kid in group_start_order(group):
        main = main_instance_of(kid["id"])
        if main is None or kid["kind"] == "none" or main["state"] == "running":
            continue
        do_start(main)
        started.append(kid["name"])
    event("group.start", {"started": started, "source": "gui"}, group["id"], inst["id"])
    return inst_json(inst)


def do_group_stop(inst: dict, group: dict, reason: str = "user") -> dict:
    stopped = []
    for kid in reversed(group_start_order(group)):
        main = main_instance_of(kid["id"])
        if main is None or main["state"] == "stopped":
            continue
        do_stop(main, reason)
        stopped.append(kid["name"])
    event("group.stop", {"stopped": stopped, "reason": reason}, group["id"], inst["id"])
    return inst_json(inst)


def do_start(inst: dict) -> dict:
    proj = project_by_id(inst["project_id"]) or {}
    if proj.get("kind") == "group" and not inst.get("slot"):
        return do_group_start(inst, proj)
    if proj.get("kind") == "none":
        raise ApiError(400, "no start command: project kind is 'none'")
    inst.update(
        state="running",
        pid=random.randint(10000, 99999),
        actual_port=inst["port"],
        started_at=now(),
        stopped_at=None,
        stopped_by=None,
        last_seen_at=now(),
        mem_bytes=random.randint(180, 900) * 1024 * 1024,
        cpu_ns=(inst.get("cpu_ns") or 0) + random.randint(1, 9) * 10 ** 9,
        cpu_checked_at=now(),
        idle_since=None,
        updated_at=now(),
    )
    if not observed_for_instance(inst):
        OBSERVED.append({
            "port": inst["port"], "proto": "tcp", "bind": "127.0.0.1", "pid": inst["pid"],
            "comm": "node", "cwd": inst["path"],
            "cmdline": (proj.get("start_cmd") or "dev server"),
            "unit": inst.get("unit"), "container": None,
            "compose_project": None, "compose_workdir": None,
            "project_id": proj.get("id"), "instance_id": inst["id"], "seen_at": now(),
        })
    event("instance.start", {"port": inst["port"], "source": "gui"}, proj.get("id"), inst["id"])
    return inst_json(inst)


def do_stop(inst: dict, reason: str = "user") -> dict:
    proj = project_by_id(inst["project_id"]) or {}
    if proj.get("kind") == "group" and not inst.get("slot"):
        return do_group_stop(inst, proj, reason)
    inst.update(state="stopped", pid=None, actual_port=None, stopped_at=now(),
                stopped_by=reason, mem_bytes=None, idle_since=None, updated_at=now())
    row = observed_for_instance(inst)
    if row:
        OBSERVED.remove(row)
    event("instance.stop", {"reason": reason}, inst["project_id"], inst["id"])
    return inst_json(inst)


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def route(method: str, path: str, query: dict, body: dict):
    def m(pattern):
        return re.fullmatch(pattern, path)

    if method == "GET" and path == "/healthz":
        return {"ok": True, "version": "0.1.0-mock", "pid": os.getpid(), "socket_activated": True}

    if method == "GET" and path == "/api/state":
        return state_json()

    if method == "POST" and path == "/api/reconcile":
        RECONCILED_AT[0] = now()
        quick = bool(body.get("quick"))
        event("reconcile", {"listeners": len(OBSERVED), "matched": sum(1 for o in OBSERVED if o["instance_id"]),
                            "unknown": sum(1 for o in OBSERVED if not o["instance_id"]),
                            "quick": quick, "docker": not quick})
        return state_json()

    if method == "GET" and path == "/api/events":
        limit = int((query.get("limit") or ["100"])[0])
        return EVENTS[:limit]

    if method == "GET" and path == "/api/discover":
        p = (query.get("path") or [""])[0].rstrip("/")
        if not p or not p.startswith("/"):
            raise ApiError(400, "path must be absolute")
        if "nothing" in p or p.endswith("/empty"):
            return None
        name = re.sub(r"[^a-z0-9-]", "-", os.path.basename(p).lower()) or "project"
        return {
            "name": name,
            "path": p,
            "kind": "transient",
            "start_cmd": "npm run dev",
            "port_mode": "env",
            "base_port": 3400,
            "open_path": "/",
            "confidence": 0.8,
            "evidence": ["package.json scripts.dev = nuxt dev",
                         "pnpm-lock.yaml present",
                         "nuxt.config.ts devServer.port = 3400"],
        }

    if method == "POST" and path == "/api/projects":
        if not (body.get("path") or "").startswith("/"):
            raise ApiError(400, "path must be an absolute directory")
        if any(p["path"] == body["path"] for p in PROJECTS):
            raise ApiError(409, "a project with that path already exists")
        pid = max((p["id"] for p in PROJECTS), default=0) + 1
        name = body.get("name") or re.sub(r"[^a-z0-9-]", "-", os.path.basename(body["path"]).lower())
        proj = {
            "id": pid, "name": name, "path": body["path"],
            "kind": body.get("kind") or "transient",
            "start_cmd": body.get("start_cmd") or None, "stop_cmd": None,
            "port_mode": body.get("port_mode") or "env",
            "base_port": int(body["base_port"]) if body.get("base_port") else 4020 + pid * 10,
            "slots": 9, "health_path": "/", "open_path": body.get("open_path") or "/",
            "pinned": int(body.get("pinned") or 0), "autostart": "schedule",
            "env_json": "{}", "path_prepend": None, "memory_max": None,
            "source": "gui", "notes": body.get("notes") or None,
            "created_at": now(), "updated_at": now(),
        }
        PROJECTS.append(proj)
        INSTANCES.append({
            "id": max((i["id"] for i in INSTANCES), default=0) + 1, "project_id": pid,
            "label": "main", "slot": 0, "path": proj["path"], "branch": "main",
            "port": proj["base_port"], "unit": f"portboard-{name}-main.service", "managed": 1,
            "state": "stopped", "pid": None, "actual_port": None, "owner_session": None,
            "owner_seen_at": None, "started_at": None, "stopped_at": None, "stopped_by": None,
            "last_seen_at": None, "mem_bytes": None, "cpu_ns": None, "cpu_checked_at": None,
            "idle_since": None, "extra_json": "{}", "created_at": now(), "updated_at": now(),
        })
        event("project.add", {"name": name, "source": "gui"}, pid)
        return proj

    mm = m(r"/api/projects/(\d+)")
    if mm:
        proj = project_by_id(int(mm.group(1)))
        if not proj:
            raise ApiError(404, "no such project")
        if method == "PATCH":
            for key, value in body.items():
                if key in proj or key in ("notes", "open_path", "autostart"):
                    if key in ("base_port", "pinned", "slots") and value not in (None, ""):
                        value = int(value)
                    proj[key] = value if value != "" else None
            proj["updated_at"] = now()
            event("project.edit", {"fields": sorted(body)}, proj["id"])
            return proj
        if method == "DELETE":
            running = [i for i in INSTANCES if i["project_id"] == proj["id"] and i["state"] == "running"]
            if running:
                raise ApiError(409, "stop the running instances first: "
                                    + ", ".join(i["label"] for i in running))
            INSTANCES[:] = [i for i in INSTANCES if i["project_id"] != proj["id"]]
            PROJECTS.remove(proj)
            event("project.rm", {"name": proj["name"]})
            return {"ok": True}

    mm = m(r"/api/projects/(\d+)/instances")
    if mm and method == "POST":
        proj = project_by_id(int(mm.group(1)))
        if not proj:
            raise ApiError(404, "no such project")
        label = body.get("label") or os.path.basename((body.get("path") or "").rstrip("/"))
        if not label:
            raise ApiError(400, "give a worktree label or an absolute path")
        if any(i["project_id"] == proj["id"] and i["label"] == label for i in INSTANCES):
            raise ApiError(409, f"instance {label} already exists")
        used = {i["slot"] for i in INSTANCES if i["project_id"] == proj["id"]}
        slot = next(n for n in range(1, proj["slots"] + 1) if n not in used)
        inst = {
            "id": max((i["id"] for i in INSTANCES), default=0) + 1, "project_id": proj["id"],
            "label": label, "slot": slot,
            "path": body.get("path") or f"{proj['path']}/.claude/worktrees/{label}",
            "branch": f"worktree-{label}", "port": (proj["base_port"] or 4000) + slot,
            "unit": f"portboard-{proj['name']}-{label}.service", "managed": 1,
            "state": "stopped", "pid": None, "actual_port": None, "owner_session": None,
            "owner_seen_at": None, "started_at": None, "stopped_at": None, "stopped_by": None,
            "last_seen_at": None, "mem_bytes": None, "cpu_ns": None, "cpu_checked_at": None,
            "idle_since": None, "extra_json": "{}", "created_at": now(), "updated_at": now(),
        }
        INSTANCES.append(inst)
        event("instance.add", {"label": label, "port": inst["port"]}, proj["id"], inst["id"])
        return inst_json(inst)

    mm = m(r"/api/instances/(\d+)(?:/(start|stop|restart|release|logs))?")
    if mm:
        inst = instance_by_id(int(mm.group(1)))
        if not inst:
            raise ApiError(404, "no such instance")
        verb = mm.group(2)
        if method == "POST" and verb == "start":
            return do_start(inst)
        if method == "POST" and verb == "stop":
            if inst["state"] != "running" and (project_by_id(inst["project_id"]) or {}).get("kind") == "none":
                raise ApiError(400, "nothing to stop: no pid and no container")
            return do_stop(inst)
        if method == "POST" and verb == "restart":
            do_stop(inst, "restart")
            return do_start(inst)
        if method == "POST" and verb == "release":
            inst.update(owner_session=None, owner_seen_at=None, updated_at=now())
            event("instance.release", None, inst["project_id"], inst["id"])
            return inst_json(inst)
        if method == "GET" and verb == "logs":
            lines = int((query.get("lines") or ["200"])[0])
            proj = project_by_id(inst["project_id"]) or {}
            if proj.get("kind") == "group" and not inst["slot"]:
                # the group's logs are its children's logs, one block each
                blocks = []
                for kid in group_start_order(proj):
                    main = main_instance_of(kid["id"])
                    if main is None:
                        continue
                    blocks.append(f"===== {kid['name']} =====\n"
                                  + fake_logs(main, max(1, lines // max(1, len(children_of(proj['id']))))))
                return {"text": "\n".join(blocks) or "(no services)"}
            return {"text": fake_logs(inst, lines)}
        if method == "DELETE" and not verb:
            if inst["slot"] == 0:
                raise ApiError(400, "the main instance cannot be removed")
            if inst["state"] == "running":
                do_stop(inst, "user")
            INSTANCES.remove(inst)
            event("instance.rm", {"label": inst["label"]}, inst["project_id"])
            return {"ok": True}

    mm = m(r"/api/observed/(\d+)/(adopt|stop)")
    if mm and method == "POST":
        port = int(mm.group(1))
        row = next((o for o in OBSERVED if o["port"] == port), None)
        if not row:
            raise ApiError(404, f"port {port} is not in the last snapshot")
        if mm.group(2) == "stop":
            if row["pid"] is None:
                raise ApiError(400, "no pid for this listener (root-owned socket?)")
            OBSERVED.remove(row)
            inst = instance_by_id(row["instance_id"]) if row["instance_id"] else None
            if inst:
                do_stop(inst, "user")
            event("observed.stop", {"port": port, "comm": row.get("comm")})
            return {"ok": True}
        pid_ = body.get("project_id") or row.get("project_id")
        if not pid_:
            raise ApiError(400, "pick a project to adopt this port into")
        proj = project_by_id(int(pid_))
        if not proj:
            raise ApiError(404, "no such project")
        label = "main"
        cwd = row.get("cwd") or ""
        if "/.claude/worktrees/" in cwd:
            label = cwd.split("/.claude/worktrees/", 1)[1].split("/")[0]
        elif row["instance_id"] is None and any(
                i["project_id"] == proj["id"] and i["label"] == "main" for i in INSTANCES):
            label = (row.get("comm") or f"adopted-{port}")
        used = {i["slot"] for i in INSTANCES if i["project_id"] == proj["id"]}
        slot = 0 if label == "main" else next(n for n in range(1, 10) if n not in used)
        inst = {
            "id": max((i["id"] for i in INSTANCES), default=0) + 1, "project_id": proj["id"],
            "label": label, "slot": slot, "path": cwd or proj["path"], "branch": None,
            "port": port, "unit": row.get("unit") or row.get("container"), "managed": 0,
            "state": "running", "pid": row.get("pid"), "actual_port": port,
            "owner_session": None, "owner_seen_at": None, "started_at": None,
            "stopped_at": None, "stopped_by": None, "last_seen_at": now(),
            "mem_bytes": 96 * 1024 * 1024, "cpu_ns": 2 * 10 ** 9, "cpu_checked_at": now(),
            "idle_since": None, "extra_json": "{}", "created_at": now(), "updated_at": now(),
        }
        INSTANCES.append(inst)
        row["project_id"] = proj["id"]
        row["instance_id"] = inst["id"]
        event("instance.adopt", {"port": port, "label": label}, proj["id"], inst["id"])
        return inst_json(inst)

    if method == "POST" and path in ("/api/schedule/stop", "/api/schedule/start"):
        evening = path.endswith("stop")
        stopped, started, skipped = [], [], []
        for inst in INSTANCES:
            proj = project_by_id(inst["project_id"]) or {}
            if proj.get("kind") == "group":
                continue          # the mirror instance: its children are in the loop already
            if proj.get("pinned") or not inst["managed"]:
                skipped.append(inst["id"])
                continue
            if evening and inst["state"] == "running":
                do_stop(inst, "schedule")
                stopped.append(inst["id"])
            elif not evening and inst["state"] == "stopped" and inst["stopped_by"] == "schedule":
                try:
                    do_start(inst)
                    started.append(inst["id"])
                except ApiError:
                    skipped.append(inst["id"])
        event("schedule.stop" if evening else "schedule.start",
              {"stopped": stopped, "started": started, "skipped": skipped})
        return {"stopped": stopped, "started": started, "skipped": skipped, "errors": []}

    if method == "POST" and path == "/api/settings":
        for key, value in body.items():
            SETTINGS[key] = str(value)
        event("settings.set", {"keys": sorted(body)})
        return dict(SETTINGS)

    raise ApiError(404, f"no route for {method} {path}")


def fake_logs(inst: dict, lines: int) -> str:
    unit = inst.get("unit") or "portboard"
    out = [f"-- journal for {unit} (last {lines} lines) --"]
    base = datetime.now() - timedelta(seconds=3 * lines)
    for n in range(min(lines, 220)):
        ts = (base + timedelta(seconds=3 * n)).strftime("%Y-%m-%dT%H:%M:%S%z") or \
             (base + timedelta(seconds=3 * n)).strftime("%Y-%m-%dT%H:%M:%S")
        if n == 0:
            out.append(f"{ts} miro {unit}: > nuxt dev")
        elif n == 3:
            out.append(f"{ts} miro {unit}: Nuxt 3.14.1 with Nitro 2.10.4")
        elif n == 5:
            out.append(f"{ts} miro {unit}:   ➜ Local:  http://localhost:{inst.get('port')}/")
        elif n % 17 == 0:
            out.append(f"{ts} miro {unit}: WARN  [vite] slow dependency scan (1.4s)")
        elif n % 11 == 0:
            out.append(f"{ts} miro {unit}: ℹ vite server hmr update /components/Card.vue")
        else:
            out.append(f"{ts} miro {unit}: GET /sheron-world/magazin 200 in {12 + n % 90}ms")
    return "\n".join(out)


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PortboardMock/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter, but keep a one-line trace
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, payload: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "body is not valid JSON")
        return data if isinstance(data, dict) else {"value": data}

    def _serve_index(self, slow: bool = False) -> None:
        try:
            with open(INDEX, "rb") as fh:
                payload = fh.read()
        except OSError as exc:
            self._json(500, {"error": f"cannot read {INDEX}: {exc}"})
            return
        if slow:
            payload += b'\n<img src="/_slowpixel.gif" alt="" width="1" height="1">\n'
        self._send(200, payload, "text/html; charset=utf-8")

    def _serve_slow_pixel(self) -> None:
        """A 1x1 gif answered after 2.5 s, to delay the load event for screenshots."""
        import time
        time.sleep(2.5)
        gif = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!"
               b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
               b"\x00\x00\x02\x02D\x01\x00;")
        self._send(200, gif, "image/gif")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path in ("/", "/index.html"):
                self._serve_index(slow=bool(query.get("shot")))
                return
            if method == "GET" and path == "/_slowpixel.gif":
                self._serve_slow_pixel()
                return
            body = self._body() if method in ("POST", "PATCH", "PUT", "DELETE") else {}
            with LOCK:
                result = route(method, path, query, body)
            self._json(200, result)
        except ApiError as exc:
            self._json(exc.code, {"error": exc.message})
        except Exception as exc:  # a mock crash must still look like the API
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else 8791
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    print(f"portboard GUI mock on http://127.0.0.1:{port}/  (serving {INDEX})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("bye", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
