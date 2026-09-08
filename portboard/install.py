"""Systemd units/timers/socket, CLI symlink, Claude Code hooks and MCP registration.

SAFETY: this module must never touch the real ~/.config, ~/.claude or
~/.local/bin while developing or testing. All real filesystem locations are
module-level Path variables (SYSTEMD_USER_DIR, LOCAL_BIN, CLAUDE_SETTINGS)
so tests can monkeypatch them to a temp dir. Callers must pass dry_run=True
unless they really mean to touch the running system (only the orchestrator's
real `portboard install` invocation should omit it).
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import config

# Real locations. Tests monkeypatch these to temp-dir equivalents.
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
LOCAL_BIN = Path.home() / ".local" / "bin" / "portboard"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"

REPO_DIR = Path(__file__).resolve().parent.parent

# (settings-json event name, matcher or None, portboard hook-cli event name, timeout seconds)
# WorktreeCreate is deliberately not registered: per the Claude Code docs it
# "replaces default git behavior" (the hook must create the worktree itself
# and print its path; a non-zero exit aborts creation) so Portboard must never
# own it. SessionStart is left without a matcher on purpose: its matcher
# values are startup|resume|clear|compact|fork and we want all of them.
_HOOK_EVENTS: list[tuple[str, str | None, str, int]] = [
    ("SessionStart", None, "session-start", 10),
    ("SessionEnd", None, "session-end", 5),
    ("PreToolUse", "Bash", "pre-tool-use", 10),
    ("CwdChanged", None, "cwd-changed", 5),
    ("WorktreeRemove", None, "worktree-remove", 30),
]

_SYSTEMD_TIMEOUT = 30


def detect_node_path() -> str | None:
    """Dir of `which node` if it lives inside ~/.nvm, else the newest
    ~/.nvm/versions/node/*/bin, else the dir of `which node`, else None."""
    import shutil

    home = Path.home()
    nvm_versions = home / ".nvm" / "versions" / "node"

    node = shutil.which("node")
    if node:
        node_path = Path(node).resolve()
        try:
            node_path.relative_to((home / ".nvm").resolve())
            return str(node_path.parent)
        except (ValueError, OSError):
            pass

    if nvm_versions.is_dir():
        candidates = [p for p in nvm_versions.iterdir() if p.is_dir()]

        def _version_key(p: Path) -> tuple[int, ...]:
            name = p.name.lstrip("v")
            parts = []
            for chunk in name.split("."):
                digits = "".join(ch for ch in chunk if ch.isdigit())
                parts.append(int(digits) if digits else 0)
            return tuple(parts) if parts else (0,)

        candidates.sort(key=_version_key)
        for candidate in reversed(candidates):
            bin_dir = candidate / "bin"
            if bin_dir.is_dir():
                return str(bin_dir)

    if node:
        return str(Path(node).parent)

    return None


def unit_files(settings: dict, repo_dir: str, python: str) -> dict[str, str]:
    """Pure: filename -> unit file content. Writes nothing."""
    daemon_port = settings.get("daemon_port", str(config.DAEMON_PORT))
    evening = settings.get("evening_stop", config.DEFAULT_SETTINGS["evening_stop"])
    morning = settings.get("morning_start", config.DEFAULT_SETTINGS["morning_start"])
    morning_days = settings.get("morning_days", config.DEFAULT_SETTINGS["morning_days"])

    bin_path = f"{repo_dir}/bin/portboard"

    files: dict[str, str] = {}

    files["portboard.socket"] = (
        "[Unit]\n"
        "Description=Portboard socket\n\n"
        "[Socket]\n"
        f"ListenStream=127.0.0.1:{daemon_port}\n"
        "NoDelay=true\n\n"
        "[Install]\n"
        "WantedBy=sockets.target\n"
    )

    files["portboard.service"] = (
        "[Unit]\n"
        "Description=Portboard daemon\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={python} {bin_path} serve\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Nice=5\n"
        "MemoryMax=300M\n"
        "StandardOutput=journal\n"
    )

    files["portboard-evening.service"] = (
        "[Unit]\n"
        "Description=Portboard evening stop\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={python} {bin_path} schedule stop\n"
    )
    files["portboard-evening.timer"] = (
        "[Unit]\n"
        "Description=Portboard evening stop timer\n\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {evening}:00\n"
        "Persistent=false\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    files["portboard-morning.service"] = (
        "[Unit]\n"
        "Description=Portboard morning start\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={python} {bin_path} schedule start\n"
    )
    files["portboard-morning.timer"] = (
        "[Unit]\n"
        "Description=Portboard morning start timer\n\n"
        "[Timer]\n"
        f"OnCalendar={morning_days} *-*-* {morning}:00\n"
        "Persistent=false\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    files["portboard-tick.service"] = (
        "[Unit]\n"
        "Description=Portboard tick (reconcile + idle rule)\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={python} {bin_path} tick\n"
    )
    files["portboard-tick.timer"] = (
        "[Unit]\n"
        "Description=Portboard tick timer\n\n"
        "[Timer]\n"
        "OnBootSec=10min\n"
        "OnUnitActiveSec=30min\n"
        "AccuracySec=5min\n"
        "Persistent=false\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    return files


def merge_hooks(settings_json: dict, command_prefix: str = "portboard") -> tuple[dict, list[str]]:
    """Pure: merge Portboard hook entries into a Claude Code settings dict.

    Returns (new_settings, added) where `added` describes the entries that
    were newly inserted (empty when everything was already present)."""
    result = copy.deepcopy(settings_json) if settings_json else {}
    hooks_root = result.setdefault("hooks", {})
    added: list[str] = []

    for event_name, matcher, cli_event, timeout in _HOOK_EVENTS:
        command = f"{command_prefix} hook {cli_event}"
        groups = hooks_root.setdefault(event_name, [])

        target_group = None
        for group in groups:
            if group.get("matcher") == matcher or (matcher is None and "matcher" not in group):
                target_group = group
                break

        if target_group is None:
            target_group = {"hooks": []}
            if matcher is not None:
                target_group["matcher"] = matcher
            groups.append(target_group)

        existing_hooks = target_group.setdefault("hooks", [])
        if not any(h.get("command") == command for h in existing_hooks):
            existing_hooks.append({"type": "command", "command": command, "timeout": timeout})
            added.append(f"{event_name} ({matcher or 'no matcher'}): {command}")

    return result, added


def _load_settings() -> dict:
    try:
        from . import db

        conn = db.connect()
        try:
            return db.all_settings(conn)
        finally:
            conn.close()
    except Exception:
        return dict(config.DEFAULT_SETTINGS)


def _install_units(settings: dict, dry_run: bool) -> None:
    files = unit_files(settings, str(REPO_DIR), sys.executable or "python3")

    if dry_run:
        print("-- dry run: would write unit files to " + str(SYSTEMD_USER_DIR) + " --")
        for name, content in files.items():
            print(f"### {name}")
            print(content)
        print(
            "would run: systemctl --user daemon-reload; systemctl --user enable --now "
            "portboard.socket portboard-evening.timer portboard-morning.timer portboard-tick.timer"
        )
        return

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = SYSTEMD_USER_DIR / name
        path.write_text(content)
        print(f"wrote {path}")

    reload_result = subprocess.run(
        ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=_SYSTEMD_TIMEOUT
    )
    if reload_result.returncode != 0:
        print(f"systemctl daemon-reload failed: {reload_result.stderr.strip()}", file=sys.stderr)

    enable_result = subprocess.run(
        [
            "systemctl", "--user", "enable", "--now",
            "portboard.socket", "portboard-evening.timer", "portboard-morning.timer", "portboard-tick.timer",
        ],
        capture_output=True, text=True, timeout=_SYSTEMD_TIMEOUT,
    )
    if enable_result.returncode != 0:
        print(f"systemctl enable --now failed: {enable_result.stderr.strip()}", file=sys.stderr)
    else:
        print("enabled portboard.socket, portboard-evening.timer, portboard-morning.timer, portboard-tick.timer")


def _install_symlink(dry_run: bool) -> int:
    target = REPO_DIR / "bin" / "portboard"
    if dry_run:
        print(f"would symlink {LOCAL_BIN} -> {target}")
        return 0

    LOCAL_BIN.parent.mkdir(parents=True, exist_ok=True)
    if LOCAL_BIN.is_symlink() or not LOCAL_BIN.exists():
        if LOCAL_BIN.is_symlink() or LOCAL_BIN.exists():
            LOCAL_BIN.unlink()
        LOCAL_BIN.symlink_to(target)
        print(f"symlinked {LOCAL_BIN} -> {target}")
        return 0

    print(f"refusing to overwrite existing file {LOCAL_BIN}", file=sys.stderr)
    return 1


def _install_node_path(dry_run: bool) -> None:
    node_path = detect_node_path()
    if not node_path:
        print("node_path: not detected")
        return
    print(f"node_path: {node_path}")
    if dry_run:
        return
    try:
        from . import db

        conn = db.connect()
        try:
            db.set_setting(conn, "node_path", node_path)
        finally:
            conn.close()
    except Exception as exc:
        print(f"failed to store node_path: {exc}", file=sys.stderr)


def _install_mcp(settings: dict, dry_run: bool) -> None:
    port = settings.get("daemon_port", str(config.DAEMON_PORT))
    url = f"http://127.0.0.1:{port}/mcp"

    if dry_run:
        print(f"would check: claude mcp get portboard")
        print(f"would run (if missing): claude mcp add --transport http --scope user portboard {url}")
        return

    check = subprocess.run(["claude", "mcp", "get", "portboard"], capture_output=True, text=True, timeout=15)
    if check.returncode == 0:
        print("mcp: portboard already registered")
        return

    add = subprocess.run(
        ["claude", "mcp", "add", "--transport", "http", "--scope", "user", "portboard", url],
        capture_output=True, text=True, timeout=15,
    )
    if add.returncode == 0:
        print(f"mcp: registered portboard at {url}")
    else:
        print(f"mcp: registration failed: {add.stderr.strip()}", file=sys.stderr)


def _install_hooks(dry_run: bool) -> None:
    try:
        existing = json.loads(CLAUDE_SETTINGS.read_text()) if CLAUDE_SETTINGS.exists() else {}
    except Exception:
        existing = {}

    merged, added = merge_hooks(existing)

    if dry_run:
        if added:
            print("would add hook entries:")
            for entry in added:
                print(f"  {entry}")
        else:
            print("hooks: already present, nothing to add")
        return

    if CLAUDE_SETTINGS.exists():
        backup = CLAUDE_SETTINGS.with_name(
            CLAUDE_SETTINGS.name + f".bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        backup.write_text(CLAUDE_SETTINGS.read_text())
        print(f"backed up {CLAUDE_SETTINGS} -> {backup}")

    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(merged, indent=2) + "\n")
    if added:
        for entry in added:
            print(f"hooks: added {entry}")
    else:
        print("hooks: already present, nothing to add")


def _install_discover(dry_run: bool) -> None:
    from . import discover as discover_mod
    from . import registry, db

    try:
        found = discover_mod.scan(config.PROJECTS_ROOT)
    except Exception as exc:
        print(f"discover: scan failed: {exc}", file=sys.stderr)
        return

    conn = db.connect()
    try:
        existing_paths = {p["path"] for p in registry.list_projects(conn)}
        for item in found:
            if item.get("confidence") not in ("high", "medium"):
                continue
            path = item.get("path")
            if not path or path in existing_paths:
                continue
            if dry_run:
                print(f"would register {path} ({item.get('name')}, kind={item.get('kind')})")
                continue
            registry.add_project(
                conn,
                path,
                name=item.get("name"),
                kind=item.get("kind", "transient"),
                start_cmd=item.get("start_cmd"),
                base_port=item.get("base_port"),
                port_mode=item.get("port_mode", "env"),
                source="discover",
                allow_busy=True,
            )
            print(f"discover: registered {path} ({item.get('name')})")
    finally:
        conn.close()


def run(mcp: bool = False, hooks: bool = False, discover: bool = False, all: bool = False, dry_run: bool = False) -> int:
    if all:
        mcp = hooks = discover = True

    settings = _load_settings()

    _install_units(settings, dry_run)
    rc = _install_symlink(dry_run)
    _install_node_path(dry_run)

    if mcp:
        _install_mcp(settings, dry_run)
    if hooks:
        _install_hooks(dry_run)
    if discover:
        _install_discover(dry_run)

    return rc
