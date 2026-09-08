"""Evening stop, morning start and the 30-minute tick.

Timers (installed by install.py) call exactly three entry points; none of them
loops, none of them runs longer than the work it has:

* :func:`evening_stop`  stop every running, managed, unpinned instance and
  remember which ones, so the morning can bring the same set back.
* :func:`morning_start`  start the remembered set again.
* :func:`tick`  reconcile reality, then apply the idle rule.

Every function returns ``{"stopped": [...], "started": [...], "skipped": [...],
"errors": [...]}``; list items are ``{"id", "project", "label", "port"}`` plus a
``reason`` where one applies.  One failing instance never aborts the loop.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from datetime import datetime
from typing import Any

from . import db

log = logging.getLogger("portboard.schedule")

LAST_STOPPED_KEY = "schedule_last_stopped"
# The idle rule calls an instance busy when its CPU time grew by more than this
# between two ticks (2 seconds of CPU).
CPU_BUSY_NS = 2_000_000_000
NOTIFY_SCRIPT = "~/.claude/hooks/notify-hermes.py"
NOTIFY_TIMEOUT = 10


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    value = obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    return default if value is None else value


def _item(instance: Any, reason: str | None = None, project: Any = None) -> dict:
    item = {
        "id": _field(instance, "id"),
        "project": _field(instance, "project", None) or _field(project, "name", None),
        "label": _field(instance, "label", None),
        "port": _field(instance, "actual_port", None) or _field(instance, "port", None),
    }
    if reason:
        item["reason"] = reason
    return item


def _empty() -> dict:
    return {"stopped": [], "started": [], "skipped": [], "errors": []}


def _error(instance: Any, exc: Exception, project: Any = None) -> dict:
    item = _item(instance, str(exc), project)
    return item


def notify(message: str, conn: sqlite3.Connection | None = None) -> None:
    """Best-effort Telegram ping through the user's hermes hook. Never raises."""
    try:
        own = conn is None
        connection = conn or db.connect()
        try:
            enabled = db.get_setting(connection, "notify_on_schedule", "0") == "1"
        finally:
            if own:
                connection.close()
        if not enabled:
            return
        subprocess.run(
            ["python3", os.path.expanduser(NOTIFY_SCRIPT)],
            input=json.dumps({"message": message}),
            capture_output=True, text=True, timeout=NOTIFY_TIMEOUT,
        )
    except Exception as exc:  # notification problems are never the caller's problem
        log.warning("notify failed: %s", exc)


def _summary_line(prefix: str, result: dict) -> str:
    def names(key: str) -> str:
        return ", ".join(
            f"{i.get('project') or '?'}@{i.get('label') or '?'}" for i in result[key]
        ) or "nic"

    return (f"{prefix}: zastavene {len(result['stopped'])} ({names('stopped')}), "
            f"spustene {len(result['started'])} ({names('started')}), "
            f"preskocene {len(result['skipped'])}, chyby {len(result['errors'])}.")


# --------------------------------------------------------------------------
# evening / morning
# --------------------------------------------------------------------------

def evening_stop(conn: sqlite3.Connection) -> dict:
    """Stop every running, managed, unpinned instance; remember the set."""
    from . import registry, runner

    result = _empty()
    stopped_ids: list[int] = []

    for inst in registry.list_instances(conn) or []:
        if _field(inst, "state") != "running":
            continue
        project = registry.get_project(conn, _field(inst, "project_id"))
        pinned = _field(inst, "pinned", None)
        if pinned is None:
            pinned = _field(project, "pinned", 0)
        if int(pinned or 0):
            result["skipped"].append(_item(inst, "pinned", project))
            continue
        if not int(_field(inst, "managed", 1) or 0):
            result["skipped"].append(_item(inst, "unmanaged", project))
            continue
        try:
            runner.stop(conn, _field(inst, "id"), reason="schedule")
        except Exception as exc:
            log.warning("evening stop of instance %s failed: %s", _field(inst, "id"), exc)
            result["errors"].append(_error(inst, exc, project))
            continue
        stopped_ids.append(_field(inst, "id"))
        result["stopped"].append(_item(inst, None, project))

    db.set_setting(conn, LAST_STOPPED_KEY,
                   json.dumps({"ts": db.now(), "ids": stopped_ids}))
    db.add_event(conn, "schedule.stop",
                 {"stopped": stopped_ids,
                  "skipped": len(result["skipped"]),
                  "errors": len(result["errors"])})
    notify(_summary_line("Portboard vecerne zastavenie", result), conn)
    return result


def morning_start(conn: sqlite3.Connection) -> dict:
    """Start what the evening stopped, if the project still wants it."""
    from . import registry, runner

    result = _empty()
    raw = db.get_setting(conn, LAST_STOPPED_KEY, "") or ""
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except ValueError:
        log.warning("%s is not valid JSON, ignoring", LAST_STOPPED_KEY)
        parsed = {}
    ids = parsed.get("ids", []) if isinstance(parsed, dict) else parsed
    if not isinstance(ids, list):
        ids = []

    started_ids: list[int] = []
    for raw_id in ids:
        try:
            inst_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        inst = registry.get_instance(conn, inst_id)
        if not inst:
            result["skipped"].append({"id": inst_id, "project": None, "label": None,
                                      "port": None, "reason": "instance is gone"})
            continue
        project = registry.get_project(conn, _field(inst, "project_id"))
        if _field(inst, "state") != "stopped":
            result["skipped"].append(_item(inst, f"state={_field(inst, 'state')}", project))
            continue
        if _field(inst, "stopped_by") != "schedule":
            result["skipped"].append(
                _item(inst, f"stopped_by={_field(inst, 'stopped_by', 'none')}", project))
            continue
        if _field(project, "autostart", "schedule") != "schedule":
            result["skipped"].append(
                _item(inst, f"autostart={_field(project, 'autostart', 'never')}", project))
            continue
        try:
            runner.start(conn, inst_id, wait=True)
        except Exception as exc:
            log.warning("morning start of instance %s failed: %s", inst_id, exc)
            result["errors"].append(_error(inst, exc, project))
            continue
        started_ids.append(inst_id)
        result["started"].append(_item(inst, None, project))

    db.add_event(conn, "schedule.start",
                 {"started": started_ids,
                  "skipped": len(result["skipped"]),
                  "errors": len(result["errors"])})
    notify(_summary_line("Portboard ranne spustenie", result), conn)
    return result


# --------------------------------------------------------------------------
# tick: reconcile + idle rule
# --------------------------------------------------------------------------

def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _idle_minutes(since: Any, now: datetime) -> float:
    started = _parse_ts(since)
    if started is None:
        return 0.0
    return max(0.0, (now - started).total_seconds() / 60.0)


def tick(conn: sqlite3.Connection) -> dict:
    """Reconcile, then stop instances that nobody has been using."""
    from . import reconcile, registry, runner

    result = _empty()
    try:
        summary = reconcile.reconcile(conn)
    except Exception as exc:
        log.warning("reconcile during tick failed: %s", exc)
        summary = {"error": str(exc)}
        result["errors"].append({"id": None, "project": None, "label": None,
                                 "port": None, "reason": f"reconcile: {exc}"})
    result["reconcile"] = summary

    idle_minutes = db.get_int_setting(conn, "idle_minutes", 90)
    instances = [i for i in (registry.list_instances(conn) or [])
                 if _field(i, "state") == "running"]
    if idle_minutes <= 0 or not instances:
        db.add_event(conn, "schedule.tick",
                     {"idle_minutes": idle_minutes, "running": len(instances),
                      "stopped": 0, "disabled": idle_minutes <= 0})
        return result

    try:
        statuses = runner.status_many(conn, [_field(i, "id") for i in instances])
    except Exception as exc:
        log.warning("status_many during tick failed: %s", exc)
        statuses = {}

    now = datetime.now().replace(microsecond=0)
    stopped_ids: list[int] = []

    for inst in instances:
        inst_id = _field(inst, "id")
        project = registry.get_project(conn, _field(inst, "project_id"))
        pinned = _field(inst, "pinned", None)
        if pinned is None:
            pinned = _field(project, "pinned", 0)
        if int(pinned or 0):
            result["skipped"].append(_item(inst, "pinned", project))
            continue
        if not int(_field(inst, "managed", 1) or 0):
            result["skipped"].append(_item(inst, "unmanaged", project))
            continue

        status = statuses.get(inst_id) or {}
        cpu_ns = status.get("cpu_ns")
        prev_cpu = _field(inst, "cpu_ns", None)
        fields: dict[str, Any] = {}
        if cpu_ns is not None:
            fields["cpu_ns"] = cpu_ns
            fields["cpu_checked_at"] = db.now()

        if _field(inst, "owner_session", None):
            if _field(inst, "idle_since", None):
                fields["idle_since"] = None
            if fields:
                registry.update_instance(conn, inst_id, **fields)
            result["skipped"].append(_item(inst, "owned", project))
            continue

        port = _field(inst, "actual_port", None) or _field(inst, "port", None)
        connections = 0
        if port:
            try:
                from . import sysinfo

                connections = int(sysinfo.established_count(port) or 0)
            except Exception as exc:
                log.warning("established_count(%s) failed: %s", port, exc)
                connections = 0
        cpu_grew = (cpu_ns is not None and prev_cpu is not None
                    and (cpu_ns - prev_cpu) > CPU_BUSY_NS)
        busy = connections > 0 or cpu_grew

        if busy:
            if _field(inst, "idle_since", None):
                fields["idle_since"] = None
            if fields:
                registry.update_instance(conn, inst_id, **fields)
            result["skipped"].append(
                _item(inst, f"busy (conns={connections}, cpu_grew={cpu_grew})", project))
            continue

        idle_since = _field(inst, "idle_since", None)
        if not idle_since:
            fields["idle_since"] = db.now()
            registry.update_instance(conn, inst_id, **fields)
            result["skipped"].append(_item(inst, "idle since now", project))
            continue

        if fields:
            registry.update_instance(conn, inst_id, **fields)
        minutes = _idle_minutes(idle_since, now)
        if minutes < idle_minutes:
            result["skipped"].append(
                _item(inst, f"idle {minutes:.0f}/{idle_minutes} min", project))
            continue

        try:
            runner.stop(conn, inst_id, reason="idle")
        except Exception as exc:
            log.warning("idle stop of instance %s failed: %s", inst_id, exc)
            result["errors"].append(_error(inst, exc, project))
            continue
        stopped_ids.append(inst_id)
        result["stopped"].append(_item(inst, f"idle {minutes:.0f} min", project))

    db.add_event(conn, "schedule.tick",
                 {"idle_minutes": idle_minutes, "running": len(instances),
                  "stopped": stopped_ids, "errors": len(result["errors"])})
    if result["stopped"] or result["errors"]:
        notify(_summary_line("Portboard idle kontrola", result), conn)
    return result
