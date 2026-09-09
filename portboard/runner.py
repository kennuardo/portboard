"""Start, stop, inspect and tail the instances Portboard knows about.

Four kinds of backend, one contract (see docs/DESIGN.md "Runner contract"):

* ``transient``  a ``systemd-run --user`` scope of the project's ``start_cmd``
* ``unit``       an existing ``systemd --user`` unit named in ``start_cmd``
* ``compose``    ``docker compose --project-directory <path> up -d``
* ``none``       we only observe; start is an error, stop is best effort

Everything that talks to the outside world goes through ``subprocess.run`` with
``capture_output=True, text=True`` and an explicit timeout, or through
``sysinfo``.  The only loop in this module is :func:`wait_for_listen`, the
bounded 0.5 s poll after a start; nothing else polls, ever.

Registry, sysinfo and reconcile are imported lazily inside the functions so
import order between the parallel modules never matters.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import time
from typing import Any, Iterable

from . import config, db

log = logging.getLogger("portboard.runner")

# Subprocess budgets. Deliberately generous for docker, tight for everything else.
START_TIMEOUT_CMD = 20      # systemd-run / systemctl start
COMPOSE_UP_TIMEOUT = 120    # docker compose up -d (may pull/build)
STOP_TIMEOUT = 30           # systemctl stop / docker compose stop / docker stop
LOG_TIMEOUT = 10            # journalctl / docker compose logs
POLL_INTERVAL = 0.5         # wait_for_listen tick
JOURNAL_EXCERPT_LINES = 20

_SAFE_RE = re.compile(r"[^a-z0-9-]+")


class RunnerError(Exception):
    """Anything that stopped us from starting, stopping or inspecting."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _field(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict row or an object with attributes."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def _safe(label: Any) -> str:
    """registry.safe_label with a local fallback (pure helpers stay usable)."""
    text = str(label or "")
    try:
        from . import registry

        return registry.safe_label(text)
    except Exception:  # pragma: no cover - registry always provides it in practice
        return _SAFE_RE.sub("-", text.lower()).strip("-") or "x"


def _run(argv: list[str], timeout: int, cwd: str | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """subprocess.run with the house rules; process errors become RunnerError."""
    log.debug("run %s (cwd=%s, timeout=%s)", argv, cwd, timeout)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        raise RunnerError(f"command timed out after {timeout} s: {' '.join(argv)}") from None
    except FileNotFoundError as exc:
        raise RunnerError(f"command not found: {argv[0]} ({exc})") from None
    except OSError as exc:
        raise RunnerError(f"could not run {argv[0]}: {exc}") from None


def _out(proc: subprocess.CompletedProcess) -> str:
    parts = [(proc.stderr or "").strip(), (proc.stdout or "").strip()]
    return " / ".join(p for p in parts if p) or f"exit code {proc.returncode}"


def _instance_and_project(conn: sqlite3.Connection, instance_id: int) -> tuple[dict, dict]:
    from . import registry

    inst = registry.get_instance(conn, instance_id)
    if not inst:
        raise RunnerError(f"instance {instance_id} not found")
    project = registry.get_project(conn, _field(inst, "project_id"))
    if not project:
        raise RunnerError(f"project of instance {instance_id} not found")
    return inst, project


def _kind(project: Any, instance: Any = None) -> str:
    return _field(project, "kind", None) or _field(instance, "kind", "transient") or "transient"


def _memory_max(conn: sqlite3.Connection, project: Any) -> str:
    return _field(project, "memory_max", None) or db.get_setting(conn, "memory_max", "4G") or "4G"


def _start_timeout(conn: sqlite3.Connection) -> int:
    return db.get_int_setting(conn, "start_timeout", 60)


def _journal_tail(unit: str, lines: int = JOURNAL_EXCERPT_LINES) -> str:
    """Best effort last journal lines of a unit; never raises."""
    try:
        proc = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=LOG_TIMEOUT,
        )
        return (proc.stdout or proc.stderr or "").strip()
    except Exception:  # pragma: no cover - diagnostics must never mask the real error
        return ""


# --------------------------------------------------------------------------
# pure naming / command building
# --------------------------------------------------------------------------

def unit_name(project: Any, instance: Any) -> str:
    """portboard-<project>-<label>.service"""
    return (f"{config.UNIT_PREFIX}{_safe(_field(project, 'name', 'project'))}"
            f"-{_safe(_field(instance, 'label', 'main'))}.service")


def compose_project_name(project: Any, instance: Any) -> str:
    """COMPOSE_PROJECT_NAME: the project for main, <project>-<label> for worktrees."""
    name = _safe(_field(project, "name", "project"))
    label = _field(instance, "label", "main")
    slot = _field(instance, "slot", 0)
    if label == "main" or slot in (0, "0"):
        return name
    return f"{name}-{_safe(label)}"


def systemd_unit_of(project: Any, instance: Any) -> str | None:
    """The systemd unit backing this instance, or None for compose/none."""
    kind = _kind(project, instance)
    if kind == "transient":
        return unit_name(project, instance)
    if kind == "unit":
        return _field(project, "start_cmd", None) or _field(instance, "unit", None)
    return None


def build_env(conn: sqlite3.Connection, project: Any, instance: Any) -> dict[str, str]:
    """Environment handed to the started process (``--setenv`` / compose env)."""
    port = _field(instance, "port", None)
    port_mode = _field(project, "port_mode", "env")
    env: dict[str, str] = {}
    if port_mode == "env" and port:
        env["PORT"] = str(port)
        env["NUXT_PORT"] = str(port)
        env["NITRO_PORT"] = str(port)
    env["PORTBOARD_INSTANCE"] = str(_field(instance, "id", ""))
    env["PORTBOARD_PROJECT"] = str(_field(project, "name", ""))

    prepend = _field(project, "path_prepend", None) or db.get_setting(conn, "node_path", "") or ""
    if not prepend.strip():
        # settings.node_path is filled by `portboard install`; when it is empty
        # (fresh state dir, wiped settings) look for the nvm node now rather
        # than launching `npm run dev` into a PATH where npm does not exist
        from . import install
        try:
            prepend = install.detect_node_path() or ""
        except Exception:
            prepend = ""
    prepend = prepend.strip().rstrip(":")
    base_path = "/usr/local/bin:/usr/bin:/bin"
    env["PATH"] = f"{prepend}:{base_path}" if prepend else base_path

    raw = _field(project, "env_json", "{}") or "{}"
    try:
        extra = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        log.warning("project %s has invalid env_json, ignoring", _field(project, "name", "?"))
        extra = {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            env[str(key)] = "" if value is None else str(value)
    return env


def build_command(project: Any, instance: Any) -> str:
    """start_cmd with ``{port}`` substituted when port_mode is ``arg``."""
    cmd = _field(project, "start_cmd", "") or ""
    if _field(project, "port_mode", "env") == "arg":
        port = _field(instance, "port", None)
        cmd = cmd.replace("{port}", str(port) if port else "")
    return cmd


def systemd_run_argv(conn: sqlite3.Connection, project: Any, instance: Any) -> list[str]:
    """The full ``systemd-run --user`` argv for a transient instance (pure)."""
    unit = unit_name(project, instance)
    env = build_env(conn, project, instance)
    cmd = build_command(project, instance)
    argv = [
        "systemd-run", "--user",
        f"--unit={unit}",
        f"--description=portboard {_field(project, 'name', '')} {_field(instance, 'label', '')}",
        "-p", f"WorkingDirectory={_field(instance, 'path', '')}",
        "-p", "KillMode=control-group",
        "-p", "TimeoutStopSec=15",
        "-p", f"MemoryMax={_memory_max(conn, project)}",
    ]
    argv += [f"--setenv={k}={v}" for k, v in sorted(env.items())]
    argv += ["/bin/sh", "-c", cmd]
    return argv


def compose_argv(project: Any, instance: Any, action: str = "up") -> list[str]:
    """docker compose argv for the instance's path."""
    path = _field(instance, "path", "") or _field(project, "path", "")
    base = ["docker", "compose", "--project-directory", path]
    if action == "up":
        return base + ["up", "-d"]
    if action == "stop":
        return base + ["stop"]
    raise RunnerError(f"unknown compose action {action!r}")


def compose_env(conn: sqlite3.Connection, project: Any, instance: Any) -> dict[str, str]:
    env = dict(os.environ)
    env.update(build_env(conn, project, instance))
    env["COMPOSE_PROJECT_NAME"] = compose_project_name(project, instance)
    return env


# --------------------------------------------------------------------------
# the bounded poll
# --------------------------------------------------------------------------

def wait_for_listen(conn: sqlite3.Connection, project: Any, instance: Any,
                    timeout: int) -> dict[str, Any]:
    """Poll listeners every 0.5 s until this instance owns one.

    Returns ``{"port": actual_port, "pid": pid}``.  Raises RunnerError when the
    backing unit died (message carries the last journal lines) or on timeout.
    This is the only polling loop in the code base.
    """
    from . import sysinfo

    kind = _kind(project, instance)
    unit = systemd_unit_of(project, instance)
    cpname = compose_project_name(project, instance)
    want_port = _field(instance, "port", None)
    deadline = time.monotonic() + max(1, int(timeout))
    tick = 0

    while True:
        try:
            listeners = sysinfo.listening_ports()
        except Exception as exc:  # sysinfo problems must not look like a start failure
            log.warning("listening_ports failed while waiting: %s", exc)
            listeners = []

        if kind in ("transient", "unit") and unit:
            for listener in listeners:
                pid = getattr(listener, "pid", None)
                if not pid:
                    continue
                try:
                    info = sysinfo.proc_info(pid)
                except Exception:
                    continue
                if info is not None and getattr(info, "unit", None) == unit:
                    return {"port": getattr(listener, "port", None), "pid": pid}
        elif kind == "compose":
            try:
                containers = sysinfo.docker_containers()
            except Exception as exc:
                log.warning("docker_containers failed while waiting: %s", exc)
                containers = []
            pids = set()
            host_ports = set()
            for cont in containers:
                if getattr(cont, "compose_project", None) != cpname:
                    continue
                if getattr(cont, "pid", None):
                    pids.add(cont.pid)
                for pair in (getattr(cont, "ports", None) or ()):
                    try:
                        host_ports.add(int(pair[0]))
                    except (TypeError, ValueError, IndexError):
                        continue
            for listener in listeners:
                port = getattr(listener, "port", None)
                pid = getattr(listener, "pid", None)
                if (pid and pid in pids) or (port and port in host_ports) or \
                        (want_port and port == want_port):
                    return {"port": port, "pid": pid}
        else:
            for listener in listeners:
                if want_port and getattr(listener, "port", None) == want_port:
                    return {"port": want_port, "pid": getattr(listener, "pid", None)}

        # Did the unit die on us? Check every second, not every tick.
        if unit and tick % 2 == 1:
            try:
                stats = sysinfo.unit_cgroup_stats([unit]) or {}
            except Exception:
                stats = {}
            active = (stats.get(unit) or {}).get("ActiveState")
            if active in ("inactive", "failed"):
                excerpt = _journal_tail(unit)
                raise RunnerError(
                    f"unit {unit} is {active} before it started listening"
                    + (f"\n{excerpt}" if excerpt else "")
                )

        if time.monotonic() >= deadline:
            raise RunnerError(
                f"{unit or cpname} did not start listening within {int(timeout)} s"
            )
        tick += 1
        time.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------
# start / stop / restart
# --------------------------------------------------------------------------

def _mark_failed(conn: sqlite3.Connection, instance: Any, detail: str) -> None:
    from . import registry

    try:
        registry.update_instance(conn, _field(instance, "id"), state="failed")
    except Exception as exc:  # pragma: no cover - DB errors are logged, not masked
        log.error("could not mark instance %s failed: %s", _field(instance, "id"), exc)
    db.add_event(conn, "instance.fail", detail,
                 project_id=_field(instance, "project_id"),
                 instance_id=_field(instance, "id"))


def _start_backend(conn: sqlite3.Connection, project: Any, instance: Any) -> str | None:
    """Run the launch command for the instance's kind. Returns the unit/compose name."""
    kind = _kind(project, instance)
    path = _field(instance, "path", "") or _field(project, "path", "")

    if kind == "transient":
        cmd = build_command(project, instance)
        if not cmd.strip():
            raise RunnerError("no start command configured")
        unit = unit_name(project, instance)
        # A previous run may have left the unit in 'failed'; clear it, ignore errors.
        try:
            subprocess.run(["systemctl", "--user", "reset-failed", unit],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass
        proc = _run(systemd_run_argv(conn, project, instance), START_TIMEOUT_CMD)
        if proc.returncode != 0:
            raise RunnerError(f"systemd-run failed: {_out(proc)}")
        return unit

    if kind == "unit":
        unit = _field(project, "start_cmd", None)
        if not unit:
            raise RunnerError("no start command configured")
        proc = _run(["systemctl", "--user", "start", unit], START_TIMEOUT_CMD)
        if proc.returncode != 0:
            raise RunnerError(f"systemctl start {unit} failed: {_out(proc)}")
        return unit

    if kind == "compose":
        env = compose_env(conn, project, instance)
        override = (_field(project, "start_cmd", "") or "").strip()
        if override:
            proc = _run(["/bin/sh", "-c", override], COMPOSE_UP_TIMEOUT, cwd=path, env=env)
        else:
            proc = _run(compose_argv(project, instance, "up"), COMPOSE_UP_TIMEOUT, env=env)
        if proc.returncode != 0:
            raise RunnerError(f"docker compose up failed: {_out(proc)}")
        return env["COMPOSE_PROJECT_NAME"]

    raise RunnerError("no start command configured")


def start(conn: sqlite3.Connection, instance_id: int, wait: bool = True) -> dict:
    """Start an instance and (with ``wait``) block until it listens."""
    from . import registry

    inst, project = _instance_and_project(conn, instance_id)

    if _field(inst, "state") == "running":
        try:
            status = status_many(conn, [instance_id]).get(instance_id) or {}
        except Exception as exc:
            log.warning("status check for running instance %s failed: %s", instance_id, exc)
            status = {}
        if status.get("state") == "running":
            fresh = registry.get_instance(conn, instance_id) or inst
            out = dict(fresh)
            out["note"] = "already running"
            return out

    registry.update_instance(conn, instance_id, state="starting",
                             stopped_at=None, stopped_by=None)
    inst = registry.get_instance(conn, instance_id) or inst

    try:
        unit = _start_backend(conn, project, inst)
    except RunnerError as exc:
        _mark_failed(conn, inst, str(exc))
        raise

    fields: dict[str, Any] = {"unit": unit}
    if wait:
        try:
            hit = wait_for_listen(conn, project, inst, _start_timeout(conn))
        except RunnerError as exc:
            registry.update_instance(conn, instance_id, unit=unit)
            _mark_failed(conn, inst, str(exc))
            raise
        fields.update(
            state="running",
            pid=hit.get("pid"),
            actual_port=hit.get("port"),
            started_at=db.now(),
            stopped_by=None,
            idle_since=None,
        )
    else:
        fields.update(state="starting", started_at=db.now(), stopped_by=None, idle_since=None)

    registry.update_instance(conn, instance_id, **fields)
    db.add_event(conn, "instance.start",
                 {"unit": unit, "wait": wait,
                  "port": fields.get("actual_port") or _field(inst, "port"),
                  "pid": fields.get("pid")},
                 project_id=_field(inst, "project_id"), instance_id=instance_id)
    log.info("started instance %s (%s) unit=%s port=%s", instance_id,
             _field(inst, "label"), unit, fields.get("actual_port"))
    return registry.get_instance(conn, instance_id)


def _observed_container(conn: sqlite3.Connection, instance: Any) -> tuple[str | None, int | None]:
    """Container name and pid the last reconcile saw for this instance's port."""
    port = _field(instance, "actual_port", None) or _field(instance, "port", None)
    inst_id = _field(instance, "id", None)
    try:
        row = conn.execute(
            "SELECT container, pid FROM observed WHERE instance_id = ? OR port = ? "
            "ORDER BY (instance_id IS NULL) LIMIT 1",
            (inst_id, port),
        ).fetchone()
    except sqlite3.Error as exc:
        log.warning("observed lookup failed: %s", exc)
        return None, None
    if row is None:
        return None, None
    data = dict(row)
    return data.get("container"), data.get("pid")


def _stop_unmanaged(conn: sqlite3.Connection, instance: Any) -> None:
    """kind 'none': docker stop, else SIGTERM to our own process group."""
    container, obs_pid = _observed_container(conn, instance)
    if container:
        proc = _run(["docker", "stop", container], STOP_TIMEOUT)
        if proc.returncode != 0:
            raise RunnerError(f"docker stop {container} failed: {_out(proc)}")
        return

    pid = _field(instance, "pid", None) or obs_pid
    if not pid:
        raise RunnerError("nothing to stop: no container and no pid known")
    try:
        if os.stat(f"/proc/{int(pid)}").st_uid != os.getuid():
            raise RunnerError(f"pid {pid} is not ours, refusing to signal it")
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except RunnerError:
        raise
    except (ProcessLookupError, FileNotFoundError):
        raise RunnerError(f"pid {pid} is gone") from None
    except PermissionError:
        raise RunnerError(f"not allowed to signal pid {pid}") from None
    except OSError as exc:
        raise RunnerError(f"could not signal pid {pid}: {exc}") from None


_ALREADY_GONE = ("not loaded", "not found", "no such unit", "not-found")


def _stop_backend(conn: sqlite3.Connection, project: Any, instance: Any) -> None:
    kind = _kind(project, instance)
    path = _field(instance, "path", "") or _field(project, "path", "")
    override = (_field(project, "stop_cmd", "") or "").strip()

    if override and kind != "none":
        proc = _run(["/bin/sh", "-c", override], STOP_TIMEOUT, cwd=path,
                    env=compose_env(conn, project, instance) if kind == "compose" else None)
        if proc.returncode != 0:
            raise RunnerError(f"stop command failed: {_out(proc)}")
        return

    if kind in ("transient", "unit"):
        unit = systemd_unit_of(project, instance)
        if not unit:
            raise RunnerError("no unit to stop")
        proc = _run(["systemctl", "--user", "stop", unit], STOP_TIMEOUT)
        if proc.returncode != 0:
            text = _out(proc).lower()
            if any(marker in text for marker in _ALREADY_GONE):
                log.info("unit %s was already gone", unit)
                return
            raise RunnerError(f"systemctl stop {unit} failed: {_out(proc)}")
        return

    if kind == "compose":
        proc = _run(compose_argv(project, instance, "stop"), STOP_TIMEOUT,
                    env=compose_env(conn, project, instance))
        if proc.returncode != 0:
            raise RunnerError(f"docker compose stop failed: {_out(proc)}")
        return

    _stop_unmanaged(conn, instance)


def stop(conn: sqlite3.Connection, instance_id: int, reason: str = "user") -> dict:
    """Stop an instance and record who asked for it."""
    from . import registry

    inst, project = _instance_and_project(conn, instance_id)
    _stop_backend(conn, project, inst)

    registry.update_instance(conn, instance_id, state="stopped", stopped_at=db.now(),
                             stopped_by=reason, pid=None, actual_port=None, idle_since=None)
    db.add_event(conn, "instance.stop", {"reason": reason},
                 project_id=_field(inst, "project_id"), instance_id=instance_id)
    log.info("stopped instance %s (%s), reason=%s", instance_id, _field(inst, "label"), reason)
    return registry.get_instance(conn, instance_id)


def restart(conn: sqlite3.Connection, instance_id: int) -> dict:
    """Stop (tolerating an already stopped instance) and start again."""
    from . import registry

    inst, _project = _instance_and_project(conn, instance_id)
    try:
        stop(conn, instance_id, reason="user")
    except RunnerError as exc:
        log.info("restart: stop of instance %s failed, starting anyway: %s", instance_id, exc)
    result = start(conn, instance_id, wait=True)
    db.add_event(conn, "instance.restart", None,
                 project_id=_field(inst, "project_id"), instance_id=instance_id)
    return result


# --------------------------------------------------------------------------
# status / logs
# --------------------------------------------------------------------------

_ACTIVE_STATE_MAP = {
    "active": "running",
    "activating": "starting",
    "reloading": "running",
    "deactivating": "stopped",
    "inactive": "stopped",
    "failed": "failed",
}


def _to_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    # systemd prints [not set] as a huge sentinel for MemoryMax-ish properties.
    return None if number in (0xFFFFFFFFFFFFFFFF, -1) else number


def status_many(conn: sqlite3.Connection,
                instance_ids: Iterable[int] | None = None) -> dict[int, dict]:
    """Live state of many instances with at most two subprocess groups. Read only."""
    from . import registry, sysinfo

    instances = registry.list_instances(conn) or []
    if instance_ids is not None:
        wanted = {int(i) for i in instance_ids}
        instances = [i for i in instances if int(_field(i, "id", -1)) in wanted]

    projects: dict[Any, Any] = {}

    def project_of(inst: Any) -> Any:
        pid = _field(inst, "project_id")
        if pid not in projects:
            try:
                projects[pid] = registry.get_project(conn, pid)
            except Exception as exc:
                log.warning("project %s lookup failed: %s", pid, exc)
                projects[pid] = None
        return projects[pid]

    units: dict[int, str] = {}
    compose_names: dict[int, str] = {}
    for inst in instances:
        iid = int(_field(inst, "id", -1))
        project = project_of(inst)
        kind = _kind(project, inst)
        if kind in ("transient", "unit"):
            unit = systemd_unit_of(project, inst)
            if unit:
                units[iid] = unit
        elif kind == "compose":
            compose_names[iid] = compose_project_name(project, inst)

    unit_stats: dict[str, dict] = {}
    if units:
        try:
            unit_stats = sysinfo.unit_cgroup_stats(sorted(set(units.values()))) or {}
        except Exception as exc:
            log.warning("unit_cgroup_stats failed: %s", exc)
            unit_stats = {}

    containers: list[Any] = []
    if compose_names:
        try:
            containers = sysinfo.docker_containers() or []
        except Exception as exc:
            log.warning("docker_containers failed: %s", exc)
            containers = []

    out: dict[int, dict] = {}
    for inst in instances:
        iid = int(_field(inst, "id", -1))
        row: dict[str, Any] = {
            "state": _field(inst, "state", "unknown"),
            "pid": _field(inst, "pid", None),
            "mem_bytes": None,
            "cpu_ns": None,
            "actual_port": _field(inst, "actual_port", None),
            "unit_active": None,
            "restarts": None,
            "unit": units.get(iid) or compose_names.get(iid),
        }
        if iid in units:
            info = unit_stats.get(units[iid]) or {}
            active = info.get("ActiveState")
            row["unit_active"] = active
            row["state"] = _ACTIVE_STATE_MAP.get(active, "stopped") if info else "stopped"
            row["pid"] = _to_int(info.get("MainPID")) or None
            row["mem_bytes"] = _to_int(info.get("MemoryCurrent"))
            row["cpu_ns"] = _to_int(info.get("CPUUsageNSec"))
            row["restarts"] = _to_int(info.get("NRestarts"))
        elif iid in compose_names:
            name = compose_names[iid]
            mine = [c for c in containers if getattr(c, "compose_project", None) == name]
            running = [c for c in mine if str(getattr(c, "state", "")).lower() == "running"]
            row["unit_active"] = "active" if running else ("inactive" if mine or containers else None)
            row["state"] = "running" if running else "stopped"
            if running:
                row["pid"] = getattr(running[0], "pid", None)
        out[iid] = row
    return out


def logs(conn: sqlite3.Connection, instance_id: int, lines: int = 200) -> str:
    """Tail of the instance's log: journal for systemd, docker compose logs otherwise."""
    inst, project = _instance_and_project(conn, instance_id)
    kind = _kind(project, inst)
    lines = max(1, int(lines))

    if kind in ("transient", "unit"):
        unit = systemd_unit_of(project, inst)
        if not unit:
            return "(no unit for this instance)"
        proc = _run(["journalctl", "--user", "-u", unit, "-n", str(lines),
                     "--no-pager", "-o", "short-iso"], LOG_TIMEOUT)
        return (proc.stdout or proc.stderr or "").strip()

    if kind == "compose":
        proc = _run(compose_argv(project, inst, "up")[:4] +
                    ["logs", "--tail", str(lines), "--no-color"], LOG_TIMEOUT,
                    env=compose_env(conn, project, inst))
        return (proc.stdout or proc.stderr or "").strip()

    container, _pid = _observed_container(conn, inst)
    if container:
        proc = _run(["docker", "logs", "--tail", str(lines), container], LOG_TIMEOUT)
        return (proc.stdout or proc.stderr or "").strip()
    return "(no log source for this instance)"
