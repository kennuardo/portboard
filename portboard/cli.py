"""Portboard CLI: `bin/portboard <command> ...`.

Modules written by other agents (registry, discover, reconcile, runner,
schedule, server, mcp) are imported lazily inside each command function so
this module stays importable even before they exist.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import config, db

HOOK_EVENTS = ("session-start", "session-end", "pre-tool-use", "cwd-changed", "worktree-remove")


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------


def _fmt(value) -> str:
    return "-" if value in (None, "") else str(value)


def _print_table(headers: list[str], rows: list[list]) -> None:
    str_rows = [[_fmt(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print(fmt_row(list(headers)))
    for row in str_rows:
        print(fmt_row(row))


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _print_instance_result(instance: dict, as_json: bool) -> None:
    if as_json:
        _print_json(instance)
        return
    print(
        f"{instance.get('project')}@{instance.get('label')}: {instance.get('state')} "
        f"port={_fmt(instance.get('port'))} url={_fmt(instance.get('url'))}"
    )


# --------------------------------------------------------------------------
# shared lookup
# --------------------------------------------------------------------------


def _instance_for_args(conn, args: argparse.Namespace) -> dict:
    """Resolve the target instance from --id, --cwd or a positional ref."""
    from . import registry

    inst_id = getattr(args, "id", None)
    if inst_id:
        instance = registry.get_instance(conn, inst_id)
        if instance is None:
            raise ValueError(f"no instance with id {inst_id}")
        return instance

    cwd = getattr(args, "cwd", None)
    if cwd:
        resolved = registry.resolve_path(conn, cwd)
        if resolved.project is None:
            raise ValueError(f"{cwd} is not a registered project")
        instance = resolved.instance
        if instance is None:
            instance = registry.ensure_instance(
                conn,
                resolved.project["id"],
                cwd,
                label=resolved.label,
                branch=registry.git_branch(cwd),
                source="cli",
            )
        return instance

    ref = getattr(args, "ref", None)
    if ref:
        return registry.find_by_ref(conn, ref)

    raise ValueError("specify a project ref, --cwd, or --id")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    from . import registry

    conn = db.connect()
    try:
        snapshot = registry.state_snapshot(conn)
    finally:
        conn.close()

    projects_by_id = {}
    instances: list[dict] = []
    for project in snapshot.get("projects", []):
        projects_by_id[project["id"]] = project
        instances.extend(project.get("instances", []))
    observed = snapshot.get("observed", [])

    if args.json:
        _print_json({"instances": instances, "observed": observed})
        return 0

    _print_table(
        ["PROJECT", "LABEL", "PORT", "ACTUAL", "STATE", "OWNER", "URL"],
        [
            [
                inst.get("project"),
                inst.get("label"),
                inst.get("port"),
                inst.get("actual_port"),
                inst.get("state"),
                inst.get("owner_session"),
                inst.get("url"),
            ]
            for inst in instances
        ],
    )
    print()
    _print_table(
        ["PORT", "BIND", "PROCESS", "PROJECT"],
        [
            [
                o.get("port"),
                o.get("bind"),
                o.get("comm") or o.get("unit") or o.get("container"),
                projects_by_id.get(o.get("project_id"), {}).get("name"),
            ]
            for o in observed
        ],
    )
    return 0


def cmd_whois(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM observed WHERE port = ? ORDER BY seen_at DESC LIMIT 1", (args.port,)
        ).fetchone()
        observed = db.row(row)
        instance = None
        project = None
        if observed:
            from . import registry

            if observed.get("instance_id"):
                instance = registry.get_instance(conn, observed["instance_id"])
            if observed.get("project_id"):
                project = registry.get_project(conn, observed["project_id"])
    finally:
        conn.close()

    if args.json:
        _print_json({"observed": observed, "instance": instance, "project": project})
        return 0

    if not observed:
        print(f"nothing observed on port {args.port}")
        return 0

    print(
        f"port {args.port}: bind={_fmt(observed.get('bind'))} pid={_fmt(observed.get('pid'))} "
        f"comm={_fmt(observed.get('comm'))} unit={_fmt(observed.get('unit'))} "
        f"container={_fmt(observed.get('container'))} project={_fmt(project and project.get('name'))}"
    )
    if instance:
        print(f"instance: {instance.get('project')}@{instance.get('label')} state={instance.get('state')}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from . import registry

    cwd = args.cwd or os.getcwd()
    conn = db.connect()
    try:
        resolved = registry.resolve_path(conn, cwd)
        instances = registry.list_instances(conn, project_id=resolved.project["id"]) if resolved.project else []
    finally:
        conn.close()

    if args.json:
        _print_json({"project": resolved.project, "instances": instances, "this": resolved.instance, "cwd": cwd})
        return 0

    if resolved.project is None:
        print(f"{cwd} is not a registered project")
        return 0

    print(f"project {resolved.project.get('name')} ({resolved.project.get('kind')}) at {resolved.project.get('path')}")
    _print_table(
        ["LABEL", "PORT", "STATE", "OWNER", "URL"],
        [[i.get("label"), i.get("port"), i.get("state"), i.get("owner_session"), i.get("url")] for i in instances],
    )
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    from . import registry

    cwd = args.cwd
    conn = db.connect()
    try:
        resolved = registry.resolve_path(conn, cwd)
        project = resolved.project
        if project is None:
            from . import discover

            if not discover.looks_like_project(cwd):
                raise ValueError(f"{cwd} does not look like a project")
            suggestion = discover.suggest(cwd)
            if not suggestion:
                raise ValueError(f"could not determine a project configuration for {cwd}")
            project = registry.add_project(
                conn,
                suggestion.get("path", cwd),
                name=suggestion.get("name"),
                kind=suggestion.get("kind", "transient"),
                start_cmd=suggestion.get("start_cmd"),
                base_port=suggestion.get("base_port"),
                port_mode=suggestion.get("port_mode", "env"),
                source="cli",
                allow_busy=True,
            )
            resolved = registry.resolve_path(conn, cwd)
            project = resolved.project

        instance = resolved.instance
        if instance is None:
            instance = registry.ensure_instance(
                conn, project["id"], cwd, label=resolved.label, branch=registry.git_branch(cwd), source="cli"
            )

        session_id = args.session or os.environ.get("CLAUDE_SESSION_ID")
        if session_id:
            instance = registry.set_owner(conn, instance["id"], session_id)
    finally:
        conn.close()

    if args.json:
        _print_json(instance)
        return 0
    print(f"claimed {instance.get('project')}@{instance.get('label')} port={_fmt(instance.get('port'))} url={_fmt(instance.get('url'))}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    from . import runner

    conn = db.connect()
    try:
        instance = _instance_for_args(conn, args)
        instance = runner.start(conn, instance["id"], wait=not args.no_wait)
    finally:
        conn.close()
    _print_instance_result(instance, args.json)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    from . import runner

    conn = db.connect()
    try:
        instance = _instance_for_args(conn, args)
        instance = runner.stop(conn, instance["id"], reason="user")
    finally:
        conn.close()
    _print_instance_result(instance, args.json)
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    from . import runner

    conn = db.connect()
    try:
        instance = _instance_for_args(conn, args)
        instance = runner.restart(conn, instance["id"])
    finally:
        conn.close()
    _print_instance_result(instance, args.json)
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        instance = _instance_for_args(conn, args)
    finally:
        conn.close()

    url = instance.get("url")
    if not url:
        raise ValueError(f"{instance.get('project')}@{instance.get('label')} has no url")

    subprocess.Popen(
        ["xdg-open", url],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    if args.json:
        _print_json({"opened": url})
    else:
        print(f"opening {url}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    from . import runner

    conn = db.connect()
    try:
        instance = _instance_for_args(conn, args)
        text = runner.logs(conn, instance["id"], lines=args.lines)
    finally:
        conn.close()

    if args.json:
        _print_json({"text": text})
    else:
        print(text)
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    from . import reconcile

    conn = db.connect()
    try:
        summary = reconcile.reconcile(conn, quick=args.quick, adopt_unknown=args.adopt)
    finally:
        conn.close()

    if args.json:
        _print_json(summary)
    else:
        for key, value in summary.items():
            print(f"{key}: {value}")
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    from . import registry

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM observed WHERE port = ? ORDER BY seen_at DESC LIMIT 1", (args.port,)
        ).fetchone()
        observed = db.row(row)
        if not observed:
            raise ValueError(f"port {args.port} is not in the last reconcile snapshot")

        project = None
        if args.project:
            project = registry.get_project(conn, args.project)
            if project is None:
                raise ValueError(f"no project named {args.project}")
        elif observed.get("project_id"):
            project = registry.get_project(conn, observed["project_id"])
        if project is None:
            raise ValueError(f"port {args.port} matched no project; pass --project NAME")

        path = observed.get("cwd") or observed.get("compose_workdir") or project["path"]
        instance = registry.ensure_instance(conn, project["id"], path, source="adopted")
        fields = {
            "managed": 0,
            "state": "running",
            "pid": observed.get("pid"),
            "actual_port": args.port,
            "last_seen_at": db.now(),
        }
        unit = observed.get("unit") or observed.get("container") or observed.get("compose_project")
        if unit:
            fields["unit"] = unit
        instance = registry.update_instance(conn, instance["id"], **fields)
        db.add_event(conn, "instance.adopt", {"port": args.port}, project_id=project["id"], instance_id=instance["id"])
    finally:
        conn.close()

    _print_instance_result(instance, args.json)
    return 0


def _reconcile_after_change(conn, registry, project: dict | None) -> dict | None:
    """Quick reconcile so an already-running server is matched at once; best effort."""
    if project is None:
        return None
    try:
        from . import reconcile
        reconcile.reconcile(conn, quick=True)
    except Exception as exc:  # the registration itself succeeded
        print(f"warning: quick reconcile failed: {exc}", file=sys.stderr)
        return project
    return registry.get_project(conn, project["id"]) or project


def cmd_project_add(args: argparse.Namespace) -> int:
    from . import registry

    conn = db.connect()
    try:
        fields = {}
        if args.pinned:
            fields["pinned"] = 1
        project = registry.add_project(
            conn,
            args.path,
            name=args.name,
            kind=args.kind or "transient",
            start_cmd=args.start,
            base_port=args.port,
            port_mode=args.port_mode or "env",
            source="cli",
            allow_busy=bool(getattr(args, "allow_busy", False)),
            **fields,
        )
        project = _reconcile_after_change(conn, registry, project)
    finally:
        conn.close()

    if args.json:
        _print_json(project)
    else:
        print(f"added project {project.get('name')} ({project.get('path')})")
    return 0


def cmd_project_edit(args: argparse.Namespace) -> int:
    from . import registry

    conn = db.connect()
    try:
        project = registry.get_project(conn, args.ref)
        if project is None:
            raise ValueError(f"no project named {args.ref}")
        fields = {}
        if args.rename:
            fields["name"] = args.rename
        if args.kind:
            fields["kind"] = args.kind
        if args.start is not None:
            fields["start_cmd"] = args.start
        if args.port is not None:
            fields["base_port"] = args.port
        if args.port_mode:
            fields["port_mode"] = args.port_mode
        if args.pinned:
            fields["pinned"] = 1
        if fields:
            project = registry.update_project(conn, project["id"], **fields)
            if "base_port" in fields or "kind" in fields:
                project = _reconcile_after_change(conn, registry, project)
    finally:
        conn.close()

    if args.json:
        _print_json(project)
    else:
        print(f"updated project {project.get('name')}")
    return 0


def cmd_project_rm(args: argparse.Namespace) -> int:
    from . import registry

    conn = db.connect()
    try:
        project = registry.get_project(conn, args.ref)
        if project is None:
            raise ValueError(f"no project named {args.ref}")
        registry.delete_project(conn, project["id"])
    finally:
        conn.close()

    if args.json:
        _print_json({"ok": True})
    else:
        print(f"removed project {args.ref}")
    return 0


def _project_set_pinned(args: argparse.Namespace, pinned: bool) -> int:
    from . import registry

    conn = db.connect()
    try:
        project = registry.get_project(conn, args.ref)
        if project is None:
            raise ValueError(f"no project named {args.ref}")
        project = registry.update_project(conn, project["id"], pinned=1 if pinned else 0)
    finally:
        conn.close()

    if args.json:
        _print_json(project)
    else:
        print(f"{'pinned' if pinned else 'unpinned'} {project.get('name')}")
    return 0


def cmd_project_pin(args: argparse.Namespace) -> int:
    return _project_set_pinned(args, True)


def cmd_project_unpin(args: argparse.Namespace) -> int:
    return _project_set_pinned(args, False)


def cmd_project_show(args: argparse.Namespace) -> int:
    from . import registry

    conn = db.connect()
    try:
        project = registry.get_project(conn, args.ref)
        if project is None:
            raise ValueError(f"no project named {args.ref}")
        instances = registry.list_instances(conn, project_id=project["id"])
    finally:
        conn.close()

    if args.json:
        _print_json({"project": project, "instances": instances})
        return 0

    print(f"{project.get('name')} ({project.get('kind')}) path={project.get('path')} pinned={bool(project.get('pinned'))}")
    _print_table(
        ["LABEL", "PORT", "STATE", "OWNER", "URL"],
        [[i.get("label"), i.get("port"), i.get("state"), i.get("owner_session"), i.get("url")] for i in instances],
    )
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    from . import discover

    root = args.root or str(config.PROJECTS_ROOT)
    suggestions = discover.scan(root)

    applied: list[dict] = []
    skipped: list[dict] = []
    if args.apply:
        from . import registry

        conn = db.connect()
        try:
            existing_paths = {p["path"] for p in registry.list_projects(conn)}
            for item in suggestions:
                if item.get("confidence") not in ("high", "medium"):
                    continue
                path = item.get("path")
                if not path or path in existing_paths:
                    continue
                try:
                    project = registry.add_project(
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
                except (ValueError, registry.RegistryError) as exc:
                    # one bad suggestion (duplicate port, clashing name) must not
                    # abort the whole import — report it and keep going
                    skipped.append({"path": path, "name": item.get("name"), "error": str(exc)})
                    continue
                applied.append(project)
        finally:
            conn.close()

    if args.json:
        _print_json({"suggestions": suggestions, "applied": applied, "skipped": skipped})
        return 0

    _print_table(
        ["PATH", "NAME", "KIND", "CONFIDENCE", "EVIDENCE"],
        [
            [s.get("path"), s.get("name"), s.get("kind"), s.get("confidence"), "; ".join(s.get("evidence") or [])]
            for s in suggestions
        ],
    )
    if applied:
        print(f"registered {len(applied)} project(s)")
    for item in skipped:
        print(f"skipped {item['name']} ({item['path']}): {item['error']}", file=sys.stderr)
    return 0


def cmd_schedule_stop(args: argparse.Namespace) -> int:
    from . import schedule

    conn = db.connect()
    try:
        result = schedule.evening_stop(conn)
    finally:
        conn.close()
    _print_json(result) if args.json else print(result)
    return 0


def cmd_schedule_start(args: argparse.Namespace) -> int:
    from . import schedule

    conn = db.connect()
    try:
        result = schedule.morning_start(conn)
    finally:
        conn.close()
    _print_json(result) if args.json else print(result)
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    from . import schedule

    conn = db.connect()
    try:
        result = schedule.tick(conn)
    finally:
        conn.close()
    _print_json(result) if args.json else print(result)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from . import server

    return server.serve(port=args.port) or 0


def cmd_mcp_stdio(args: argparse.Namespace) -> int:
    from . import mcp

    return mcp.stdio_main() or 0


def cmd_hook(args: argparse.Namespace) -> int:
    from . import hooks

    payload = hooks.read_payload()
    return hooks.run(args.event, payload)


def cmd_install(args: argparse.Namespace) -> int:
    from . import install

    return install.run(mcp=args.mcp, hooks=args.hooks, discover=args.discover, all=args.all,
                       dry_run=args.dry_run)


def cmd_export(args: argparse.Namespace) -> int:
    from . import registry

    conn = db.connect()
    try:
        data = registry.export_json(conn)
    finally:
        conn.close()
    _print_json(data)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from . import registry

    with open(args.file) as fh:
        data = json.load(fh)

    conn = db.connect()
    try:
        result = registry.import_json(conn, data, replace=args.replace)
    finally:
        conn.close()

    if args.json:
        _print_json(result)
    else:
        print(f"imported {args.file}: {result}")
    return 0


# --------------------------------------------------------------------------
# argument parser
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portboard", description="Local registry of projects, dev instances and ports.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("list", help="list instances and observed listeners")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("whois", help="show what is on a port")
    p.add_argument("port", type=int)
    p.set_defaults(func=cmd_whois)

    p = sub.add_parser("status", help="show the project mapped to a directory")
    p.add_argument("--cwd")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("claim", help="claim an instance for a Claude Code session")
    p.add_argument("--cwd", required=True)
    p.add_argument("--session")
    p.set_defaults(func=cmd_claim)

    for name, func in (("start", cmd_start), ("stop", cmd_stop), ("restart", cmd_restart)):
        p = sub.add_parser(name)
        p.add_argument("ref", nargs="?", help="project or project@label")
        p.add_argument("--cwd")
        p.add_argument("--id", type=int)
        p.add_argument("--no-wait", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("open", help="xdg-open the instance's url")
    p.add_argument("ref", nargs="?")
    p.add_argument("--cwd")
    p.add_argument("--id", type=int)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("logs")
    p.add_argument("ref", nargs="?")
    p.add_argument("--id", type=int)
    p.add_argument("-n", "--lines", type=int, default=200)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("reconcile")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--adopt", action="store_true")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("adopt")
    p.add_argument("port", type=int)
    p.add_argument("--project")
    p.set_defaults(func=cmd_adopt)

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command")

    p = project_sub.add_parser("add")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--kind", choices=list(config.KINDS))
    p.add_argument("--start")
    p.add_argument("--port", type=int)
    p.add_argument("--port-mode", dest="port_mode", choices=list(config.PORT_MODES))
    p.add_argument("--pinned", action="store_true")
    p.add_argument("--allow-busy", action="store_true",
                   help="accept --port even if something already listens on it (e.g. the project itself)")
    p.set_defaults(func=cmd_project_add)

    p = project_sub.add_parser("edit")
    p.add_argument("ref")
    p.add_argument("--name", dest="rename")
    p.add_argument("--kind", choices=list(config.KINDS))
    p.add_argument("--start")
    p.add_argument("--port", type=int)
    p.add_argument("--port-mode", dest="port_mode", choices=list(config.PORT_MODES))
    p.add_argument("--pinned", action="store_true")
    p.set_defaults(func=cmd_project_edit)

    p = project_sub.add_parser("rm")
    p.add_argument("ref")
    p.set_defaults(func=cmd_project_rm)

    p = project_sub.add_parser("pin")
    p.add_argument("ref")
    p.set_defaults(func=cmd_project_pin)

    p = project_sub.add_parser("unpin")
    p.add_argument("ref")
    p.set_defaults(func=cmd_project_unpin)

    p = project_sub.add_parser("show")
    p.add_argument("ref")
    p.set_defaults(func=cmd_project_show)

    p = sub.add_parser("discover")
    p.add_argument("root", nargs="?")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_discover)

    schedule_p = sub.add_parser("schedule")
    schedule_sub = schedule_p.add_subparsers(dest="schedule_command")
    p = schedule_sub.add_parser("stop")
    p.set_defaults(func=cmd_schedule_stop)
    p = schedule_sub.add_parser("start")
    p.set_defaults(func=cmd_schedule_start)

    p = sub.add_parser("tick")
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mcp-stdio")
    p.set_defaults(func=cmd_mcp_stdio)

    p = sub.add_parser("hook")
    p.add_argument("event", choices=list(HOOK_EVENTS))
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("install")
    p.add_argument("--dry-run", action="store_true", help="print unit files and hook entries, change nothing")
    p.add_argument("--mcp", action="store_true")
    p.add_argument("--hooks", action="store_true")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("export")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import")
    p.add_argument("file")
    p.add_argument("--replace", action="store_true")
    p.set_defaults(func=cmd_import)

    return parser


def _is_handled_error(exc: Exception) -> bool:
    if isinstance(exc, (ValueError, FileNotFoundError)):
        return True
    return type(exc).__name__ in ("RegistryError", "RunnerError")


def main(argv: list[str] | None = None) -> int:
    config.setup_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    try:
        return args.func(args) or 0
    except Exception as exc:
        if _is_handled_error(exc):
            print(f"error: {exc}", file=sys.stderr)
            return 1
        raise
