"""Read-only views of the machine: sockets, processes, cgroups, docker, units.

Pure readers. Nothing here touches the database and nothing here starts, stops
or changes anything. Every external command goes through
``subprocess.run(capture_output=True, text=True, timeout=...)``; never
``shell=True``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger("portboard.sysinfo")

SS_TIMEOUT = 3
DOCKER_VERSION_TIMEOUT = 3
DOCKER_TIMEOUT = 10
SYSTEMCTL_TIMEOUT = 5
CMDLINE_MAX = 300


@dataclass
class Listener:
    port: int
    proto: str
    bind: str
    pid: int | None
    comm: str | None
    fd: int | None = None


@dataclass
class ProcInfo:
    pid: int
    cwd: str | None
    cmdline: str | None
    comm: str | None
    unit: str | None
    container: str | None
    uid: int | None
    ppid: int | None


@dataclass
class Container:
    id: str
    name: str
    state: str
    image: str
    ports: list[tuple[int, int]] = field(default_factory=list)  # (host_port, container_port)
    compose_project: str | None = None
    compose_workdir: str | None = None
    network_mode: str | None = None
    pid: int | None = None
    cmd_port: int | None = None   # --port N / PORT=N found in the container's command or env


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """Run a command, return None when it could not run at all."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        log.debug("command not found: %s", cmd[0])
    except subprocess.TimeoutExpired:
        log.warning("command timed out after %ss: %s", timeout, " ".join(cmd))
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("command failed: %s (%s)", " ".join(cmd), exc)
    return None


# --------------------------------------------------------------------------- ss

# users:(("node",pid=69288,fd=20),("node",pid=69290,fd=20))
_USERS_MARK = "users:(("
_USER_ENTRY_RE = re.compile(r'\("([^"]*)",pid=(\d+)(?:,fd=(\d+))?')


def parse_ss(text: str, proto: str | None = None) -> list[Listener]:
    """Parse ``ss -ltnpH`` / ``ss -lunpH`` output. Pure, unit-testable.

    Handles several process entries (the first pid wins), IPv6 ``[::]:3000``,
    ``*:8080``, ``127.0.0.53%lo:53`` and lines without a ``users:`` part
    (root-owned sockets seen as a normal user).
    """
    out: list[Listener] = []
    seen: set[tuple[str, int, str]] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        idx = line.find(_USERS_MARK)
        head, procs = (line[:idx], line[idx:]) if idx >= 0 else (line, "")
        fields = head.split()
        if len(fields) < 4:
            continue
        state = fields[0].upper()
        line_proto = proto or ("udp" if state.startswith("UNCONN") else "tcp")
        local = fields[-2]  # State Recv-Q Send-Q Local:Port Peer:Port
        bind, sep, port_s = local.rpartition(":")
        if not sep or not port_s.isdigit():
            continue
        port = int(port_s)
        bind = bind.strip()
        if bind.startswith("[") and bind.endswith("]"):
            bind = bind[1:-1]
        pid = comm = fd = None
        if procs:
            m = _USER_ENTRY_RE.search(procs)
            if m:
                comm = m.group(1) or None
                pid = int(m.group(2))
                fd = int(m.group(3)) if m.group(3) else None
        key = (line_proto, port, bind)
        if key in seen:
            continue
        seen.add(key)
        out.append(Listener(port=port, proto=line_proto, bind=bind, pid=pid, comm=comm, fd=fd))
    out.sort(key=lambda l: (l.port, l.bind))
    return out


def listening_ports(proto: str = "tcp") -> list[Listener]:
    """Every listening socket of ``proto``. Falls back to ``ss`` without -p."""
    flags = "-ltnpH" if proto == "tcp" else "-lunpH"
    cp = _run(["ss", flags], SS_TIMEOUT)
    if cp is None:
        return []
    if cp.returncode != 0 or (not cp.stdout.strip() and cp.stderr.strip()):
        log.debug("ss %s failed (%s), retrying without -p", flags, (cp.stderr or "").strip()[:120])
        cp = _run(["ss", flags.replace("p", "")], SS_TIMEOUT)
        if cp is None or cp.returncode != 0:
            return []
    return parse_ss(cp.stdout, proto=proto)


def established_count(port: int) -> int:
    """Number of ESTABLISHED connections whose source port is ``port``."""
    cp = _run(["ss", "-tnH", "state", "established", f"( sport = :{int(port)} )"], SS_TIMEOUT)
    if cp is None or cp.returncode != 0:
        return 0
    return sum(1 for line in cp.stdout.splitlines() if line.strip())


# ------------------------------------------------------------------------- proc

_CONTAINER_RE = re.compile(r"(?:docker|libpod|crio|cri-containerd)[-/]([0-9a-f]{12,64})")
_USER_MANAGER_RE = re.compile(r"^user@\d+\.service$")


def parse_cgroup(text: str) -> tuple[str | None, str | None]:
    """``(unit, container_id)`` from the content of ``/proc/<pid>/cgroup``.

    cgroup v2 has a single ``0::/path`` line, v1 one line per controller. The
    unit is the last path component ending in ``.service``/``.scope`` that is
    neither the user manager (``user@1000.service``) nor a container scope.
    """
    unit: str | None = None
    container: str | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        path = line.split(":", 2)[-1]
        m = _CONTAINER_RE.search(path)
        if m and container is None:
            container = m.group(1)
        for comp in path.split("/"):
            if not (comp.endswith(".service") or comp.endswith(".scope")):
                continue
            if _USER_MANAGER_RE.match(comp) or _CONTAINER_RE.match(comp):
                continue
            unit = comp  # deepest component wins
    return unit, container


def _read_text(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return None


def proc_info(pid: int) -> ProcInfo:
    """Everything we can learn about a pid from /proc. Missing bits stay None."""
    base = f"/proc/{int(pid)}"
    try:
        cwd = os.readlink(f"{base}/cwd")
    except OSError:  # PermissionError for other users, FileNotFoundError when gone
        cwd = None

    raw = _read_text(f"{base}/cmdline")
    cmdline = None
    if raw:
        cmdline = raw.replace("\0", " ").strip() or None
        if cmdline and len(cmdline) > CMDLINE_MAX:
            cmdline = cmdline[:CMDLINE_MAX]

    comm_raw = _read_text(f"{base}/comm")
    comm = comm_raw.strip() if comm_raw else None

    uid = ppid = None
    status = _read_text(f"{base}/status")
    if status:
        for line in status.splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    uid = int(parts[1])
            elif line.startswith("PPid:"):
                parts = line.split()
                if len(parts) > 1 and parts[1].lstrip("-").isdigit():
                    ppid = int(parts[1])

    unit, container = parse_cgroup(_read_text(f"{base}/cgroup") or "")
    return ProcInfo(pid=int(pid), cwd=cwd, cmdline=cmdline, comm=comm,
                    unit=unit, container=container, uid=uid, ppid=ppid)


# ----------------------------------------------------------------------- docker

_docker_ok: bool | None = None


def docker_available() -> bool:
    """Is a docker daemon reachable? Cached for the lifetime of the process."""
    global _docker_ok
    if _docker_ok is None:
        cp = _run(["docker", "version", "--format", "{{.Server.Version}}"], DOCKER_VERSION_TIMEOUT)
        _docker_ok = bool(cp and cp.returncode == 0 and cp.stdout.strip())
        if not _docker_ok:
            log.info("docker unavailable, container information disabled")
    return _docker_ok


def _reset_docker_cache() -> None:
    """Test helper: forget the cached docker probe."""
    global _docker_ok
    _docker_ok = None


_PS_PORT_RE = re.compile(r"(?:^|\s|,)(?:[\d.]+|\[[^\]]+\]):(\d+)->(\d+)/")


def _ports_from_ps(text: str) -> list[tuple[int, int]]:
    """Published ports out of the ``docker ps`` Ports column (fallback path)."""
    out: list[tuple[int, int]] = []
    for host, cont in _PS_PORT_RE.findall(text or ""):
        pair = (int(host), int(cont))
        if pair not in out:
            out.append(pair)
    return out


def _ports_from_inspect(pmap: dict | None) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for cport, bindings in (pmap or {}).items():
        head = str(cport).split("/")[0]
        if not head.isdigit():
            continue
        cnum = int(head)
        for binding in bindings or []:
            hp = str((binding or {}).get("HostPort") or "")
            if hp.isdigit():
                pair = (int(hp), cnum)
                if pair not in out:
                    out.append(pair)
    return out


_CMD_PORT_RE = re.compile(r"(?:--port[=\s]+|\b(?:NUXT_|NITRO_|SERVER_)?PORT=)([0-9]{2,5})\b")


def cmd_port(cmd: list | str | None, env: list | None = None) -> int | None:
    """The port a container's own command/env names (``--port 3103``, ``PORT=3103``).

    A --network host container publishes nothing, so this is the only hint of
    where it listens; the last --port wins (``yarn install; exec yarn dev --port N``).
    """
    text = " ".join(cmd) if isinstance(cmd, list) else (cmd or "")
    hits = _CMD_PORT_RE.findall(text)
    if not hits:
        for item in env or ():
            hits += _CMD_PORT_RE.findall(str(item))
    if not hits:
        return None
    try:
        return int(hits[-1])
    except ValueError:
        return None


def _container_from_inspect(c: dict) -> Container:
    cfg = c.get("Config") or {}
    labels = cfg.get("Labels") or {}
    state = c.get("State") or {}
    pid = state.get("Pid") or None
    command = list(cfg.get("Entrypoint") or []) + list(cfg.get("Cmd") or [])
    return Container(
        cmd_port=cmd_port(command, cfg.get("Env")),
        id=c.get("Id") or "",
        name=(c.get("Name") or "").lstrip("/"),
        state=state.get("Status") or "",
        image=cfg.get("Image") or (c.get("Image") or ""),
        ports=_ports_from_inspect((c.get("NetworkSettings") or {}).get("Ports")),
        compose_project=labels.get("com.docker.compose.project"),
        compose_workdir=labels.get("com.docker.compose.project.working_dir"),
        network_mode=(c.get("HostConfig") or {}).get("NetworkMode"),
        pid=int(pid) if pid else None,
    )


def _container_from_ps(p: dict) -> Container:
    labels = {}
    for item in (p.get("Labels") or "").split(","):
        key, sep, value = item.partition("=")
        if sep:
            labels[key.strip()] = value
    return Container(
        id=p.get("ID") or "",
        name=(p.get("Names") or "").split(",")[0],
        state=p.get("State") or "",
        image=p.get("Image") or "",
        ports=_ports_from_ps(p.get("Ports") or ""),
        compose_project=labels.get("com.docker.compose.project"),
        compose_workdir=labels.get("com.docker.compose.project.working_dir"),
        network_mode=None,
        pid=None,
    )


def docker_containers() -> list[Container]:
    """Running containers: one ``docker ps`` plus one ``docker inspect``.

    Host-network containers publish nothing, their listeners show up in ``ss``
    with the container's own pid and are mapped through that pid's cgroup.
    """
    if not docker_available():
        return []
    cp = _run(["docker", "ps", "--format", "{{json .}}"], DOCKER_TIMEOUT)
    if cp is None or cp.returncode != 0:
        log.warning("docker ps failed: %s", (cp.stderr or "").strip()[:200] if cp else "not run")
        return []
    ps: list[dict] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ps.append(json.loads(line))
        except ValueError:
            log.debug("unparsable docker ps line: %.80s", line)
    ids = [p["ID"] for p in ps if p.get("ID")]
    if not ids:
        return []
    insp = _run(["docker", "inspect", *ids], DOCKER_TIMEOUT)
    if insp is not None and insp.returncode == 0:
        try:
            data = json.loads(insp.stdout)
        except ValueError:
            data = None
        if isinstance(data, list) and data:
            return [_container_from_inspect(c) for c in data]
    log.warning("docker inspect unusable, falling back to docker ps fields")
    return [_container_from_ps(p) for p in ps]


# --------------------------------------------------------------------- systemd

SHOW_PROPERTIES = (
    "Id", "ActiveState", "SubState", "MainPID", "MemoryCurrent",
    "CPUUsageNSec", "NRestarts", "ExecMainStartTimestamp",
)
_INT_PROPERTIES = {"MainPID", "MemoryCurrent", "CPUUsageNSec", "NRestarts"}


def unit_cgroup_stats(units) -> dict[str, dict]:
    """One ``systemctl --user show`` for all units -> {unit: {property: value}}.

    ``[not set]`` and empty values become None, numeric properties become ints.
    Unknown units come back as a block with ActiveState=inactive.
    """
    names = [u for u in dict.fromkeys(units or ()) if u]
    if not names:
        return {}
    cp = _run(["systemctl", "--user", "show", "-p", ",".join(SHOW_PROPERTIES), *names], SYSTEMCTL_TIMEOUT)
    if cp is None or cp.returncode != 0:
        log.debug("systemctl show failed: %s", (cp.stderr or "").strip()[:200] if cp else "not run")
        return {}
    out: dict[str, dict] = {}
    blocks = [b for b in cp.stdout.split("\n\n")]
    index = 0
    for block in blocks:
        parsed: dict = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            value = value.strip()
            if value in ("", "[not set]", "n/a"):
                parsed[key] = None
            elif key in _INT_PROPERTIES:
                parsed[key] = int(value) if value.lstrip("-").isdigit() else None
            else:
                parsed[key] = value
        if not parsed:
            continue
        name = parsed.get("Id") or (names[index] if index < len(names) else None)
        index += 1
        if name:
            out[name] = parsed
    return out
