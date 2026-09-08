"""Tests for portboard.schedule.

runner.start/stop/status_many are patched, registry is a stub backed by the real
schema and reconcile/sysinfo are stub modules: the schedule logic is what is
under test, never the machine.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_STATE = tempfile.mkdtemp(prefix="portboard-test-schedule-")
os.environ["PORTBOARD_STATE_DIR"] = _STATE
# Keep the library's warnings out of the test output (no handler = lastResort).
logging.getLogger("portboard").addHandler(logging.NullHandler())

import portboard  # noqa: E402
from portboard import config, db, runner, schedule  # noqa: E402


def install_stub(testcase: unittest.TestCase, name: str, **attrs) -> types.ModuleType:
    full = f"portboard.{name}"
    module = types.ModuleType(full)
    for key, value in attrs.items():
        setattr(module, key, value)
    old_module = sys.modules.get(full)
    had_attr = hasattr(portboard, name)
    old_attr = getattr(portboard, name, None)
    sys.modules[full] = module
    setattr(portboard, name, module)

    def restore() -> None:
        if old_module is None:
            sys.modules.pop(full, None)
        else:
            sys.modules[full] = old_module
        if had_attr:
            setattr(portboard, name, old_attr)
        else:
            try:
                delattr(portboard, name)
            except AttributeError:
                pass

    testcase.addCleanup(restore)
    return module


def make_registry() -> dict:
    def get_project(conn, ref):
        if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (int(ref),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM projects WHERE name = ?", (ref,)).fetchone()
        return dict(row) if row else None

    def _augment(conn, row):
        data = dict(row)
        project = get_project(conn, data["project_id"]) or {}
        data["project"] = project.get("name")
        data["kind"] = project.get("kind")
        data["pinned"] = project.get("pinned")
        data["worktree"] = data["slot"] != 0
        return data

    def get_instance(conn, instance_id):
        row = conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
        return _augment(conn, row) if row else None

    def list_instances(conn, project_id=None):
        if project_id is None:
            cur = conn.execute("SELECT * FROM instances ORDER BY id")
        else:
            cur = conn.execute("SELECT * FROM instances WHERE project_id = ? ORDER BY id",
                               (project_id,))
        return [_augment(conn, r) for r in cur.fetchall()]

    def update_instance(conn, instance_id, **fields):
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(f"UPDATE instances SET {assignments}, updated_at = ? WHERE id = ?",
                         (*fields.values(), db.now(), instance_id))
        return get_instance(conn, instance_id)

    def safe_label(label):
        return re.sub(r"[^a-z0-9-]+", "-", str(label or "").lower()).strip("-")

    return {"get_project": get_project, "get_instance": get_instance,
            "list_instances": list_instances, "update_instance": update_instance,
            "safe_label": safe_label}


def ago(minutes: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


class ScheduleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="portboard-db-")
        self._old_state, self._old_db = config.STATE_DIR, config.DB_PATH
        config.STATE_DIR = Path(self.tmp)
        config.DB_PATH = Path(self.tmp) / "portboard.db"
        self.addCleanup(self._restore_paths)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.registry = install_stub(self, "registry", **make_registry())
        self.reconcile = install_stub(
            self, "reconcile",
            reconcile=mock.Mock(return_value={"listeners": 3, "matched": 2, "unknown": 1}))
        self.sysinfo = install_stub(self, "sysinfo",
                                    established_count=mock.Mock(return_value=0))

    def _restore_paths(self):
        config.STATE_DIR, config.DB_PATH = self._old_state, self._old_db

    def add_project(self, name="demo", **fields):
        row = {
            "name": name, "path": f"/tmp/{name}", "kind": "transient",
            "start_cmd": "npm run dev", "stop_cmd": None, "port_mode": "env",
            "base_port": None, "slots": 9, "health_path": "/", "open_path": "/",
            "pinned": 0, "autostart": "schedule", "env_json": "{}",
            "path_prepend": None, "memory_max": None, "source": "cli", "notes": None,
        }
        row.update(fields)
        columns = ", ".join(row) + ", created_at, updated_at"
        marks = ", ".join("?" * (len(row) + 2))
        cur = self.conn.execute(f"INSERT INTO projects({columns}) VALUES ({marks})",
                                (*row.values(), db.now(), db.now()))
        return self.registry.get_project(self.conn, cur.lastrowid)

    def add_instance(self, project, label="main", slot=0, port=None, **fields):
        row = {
            "project_id": project["id"], "label": label, "slot": slot,
            "path": project["path"] if slot == 0
                    else f"{project['path']}/.claude/worktrees/{label}",
            "branch": None, "port": port, "unit": None,
            "managed": 1, "state": "running", "pid": 100, "actual_port": port,
            "owner_session": None, "owner_seen_at": None, "started_at": db.now(),
            "stopped_at": None, "stopped_by": None, "last_seen_at": None, "mem_bytes": None,
            "cpu_ns": None, "cpu_checked_at": None, "idle_since": None, "extra_json": "{}",
        }
        row.update(fields)
        columns = ", ".join(row) + ", created_at, updated_at"
        marks = ", ".join("?" * (len(row) + 2))
        cur = self.conn.execute(f"INSERT INTO instances({columns}) VALUES ({marks})",
                                (*row.values(), db.now(), db.now()))
        return self.registry.get_instance(self.conn, cur.lastrowid)

    def row(self, instance_id):
        return self.registry.get_instance(self.conn, instance_id)

    def events(self):
        return [dict(r) for r in self.conn.execute("SELECT kind, detail FROM events ORDER BY id")]

    def remembered(self):
        return json.loads(db.get_setting(self.conn, schedule.LAST_STOPPED_KEY, "{}"))


# --------------------------------------------------------------------------
# evening
# --------------------------------------------------------------------------

class TestEveningStop(ScheduleTestCase):
    def test_stops_unpinned_managed_and_remembers_them(self):
        demo = self.add_project("demo")
        alpha = self.add_instance(demo, port=4000)
        beta = self.add_instance(demo, label="fix", slot=1, port=4001)
        self.add_instance(demo, label="off", slot=2, port=4002, state="stopped")
        pinned = self.add_project("pinnedproj", pinned=1)
        pinned_instance = self.add_instance(pinned, port=3300)
        unmanaged = self.add_project("legacy")
        unmanaged_instance = self.add_instance(unmanaged, port=5000, managed=0)

        with mock.patch.object(runner, "stop") as stop:
            result = schedule.evening_stop(self.conn)

        self.assertEqual([c.args[1] for c in stop.call_args_list], [alpha["id"], beta["id"]])
        self.assertTrue(all(c.kwargs["reason"] == "schedule" for c in stop.call_args_list))
        self.assertEqual([i["id"] for i in result["stopped"]], [alpha["id"], beta["id"]])
        self.assertEqual(result["stopped"][0]["project"], "demo")
        self.assertEqual(result["stopped"][0]["label"], "main")
        self.assertEqual(result["stopped"][0]["port"], 4000)
        self.assertEqual(result["errors"], [])

        reasons = {i["id"]: i["reason"] for i in result["skipped"]}
        self.assertEqual(reasons[pinned_instance["id"]], "pinned")
        self.assertEqual(reasons[unmanaged_instance["id"]], "unmanaged")

        remembered = self.remembered()
        self.assertEqual(remembered["ids"], [alpha["id"], beta["id"]])
        self.assertTrue(remembered["ts"])
        self.assertIn("schedule.stop", [e["kind"] for e in self.events()])

    def test_one_failure_does_not_abort_the_loop(self):
        demo = self.add_project("demo")
        bad = self.add_instance(demo, port=4000)
        good = self.add_instance(demo, label="fix", slot=1, port=4001)

        def stop(conn, instance_id, reason="user"):
            if instance_id == bad["id"]:
                raise runner.RunnerError("systemctl stop failed: boom")
            return {}

        with mock.patch.object(runner, "stop", side_effect=stop):
            result = schedule.evening_stop(self.conn)

        self.assertEqual([i["id"] for i in result["stopped"]], [good["id"]])
        self.assertEqual([i["id"] for i in result["errors"]], [bad["id"]])
        self.assertIn("boom", result["errors"][0]["reason"])
        self.assertEqual(self.remembered()["ids"], [good["id"]])

    def test_nothing_running_writes_an_empty_set(self):
        demo = self.add_project("demo")
        self.add_instance(demo, port=4000, state="stopped")
        with mock.patch.object(runner, "stop") as stop:
            result = schedule.evening_stop(self.conn)
        stop.assert_not_called()
        self.assertEqual(result["stopped"], [])
        self.assertEqual(self.remembered()["ids"], [])


# --------------------------------------------------------------------------
# morning
# --------------------------------------------------------------------------

class TestMorningStart(ScheduleTestCase):
    def _remember(self, ids):
        db.set_setting(self.conn, schedule.LAST_STOPPED_KEY,
                       json.dumps({"ts": db.now(), "ids": ids}))

    def test_starts_only_schedule_stopped_instances(self):
        demo = self.add_project("demo")
        good = self.add_instance(demo, port=4000, state="stopped", stopped_by="schedule")
        by_user = self.add_instance(demo, label="u", slot=1, port=4001,
                                    state="stopped", stopped_by="user")
        still_running = self.add_instance(demo, label="r", slot=2, port=4002,
                                          state="running", stopped_by="schedule")
        never = self.add_project("never", autostart="never")
        never_instance = self.add_instance(never, port=4100, state="stopped",
                                           stopped_by="schedule")

        self._remember([good["id"], by_user["id"], still_running["id"],
                        never_instance["id"], 9999])

        with mock.patch.object(runner, "start") as start:
            result = schedule.morning_start(self.conn)

        self.assertEqual([c.args[1] for c in start.call_args_list], [good["id"]])
        self.assertTrue(all(c.kwargs["wait"] is True for c in start.call_args_list))
        self.assertEqual([i["id"] for i in result["started"]], [good["id"]])

        reasons = {i["id"]: i["reason"] for i in result["skipped"]}
        self.assertEqual(reasons[by_user["id"]], "stopped_by=user")
        self.assertEqual(reasons[still_running["id"]], "state=running")
        self.assertEqual(reasons[never_instance["id"]], "autostart=never")
        self.assertEqual(reasons[9999], "instance is gone")
        self.assertIn("schedule.start", [e["kind"] for e in self.events()])

    def test_start_failure_is_collected(self):
        demo = self.add_project("demo")
        inst = self.add_instance(demo, port=4000, state="stopped", stopped_by="schedule")
        self._remember([inst["id"]])
        with mock.patch.object(runner, "start",
                               side_effect=runner.RunnerError("did not start listening")):
            result = schedule.morning_start(self.conn)
        self.assertEqual(result["started"], [])
        self.assertIn("did not start listening", result["errors"][0]["reason"])

    def test_missing_or_broken_memory_is_harmless(self):
        with mock.patch.object(runner, "start") as start:
            empty = schedule.morning_start(self.conn)
            db.set_setting(self.conn, schedule.LAST_STOPPED_KEY, "not json at all")
            broken = schedule.morning_start(self.conn)
        start.assert_not_called()
        self.assertEqual(empty["started"], [])
        self.assertEqual(broken["started"], [])

    def test_bare_list_memory_is_accepted(self):
        demo = self.add_project("demo")
        inst = self.add_instance(demo, port=4000, state="stopped", stopped_by="schedule")
        db.set_setting(self.conn, schedule.LAST_STOPPED_KEY, json.dumps([inst["id"]]))
        with mock.patch.object(runner, "start") as start:
            result = schedule.morning_start(self.conn)
        self.assertEqual([c.args[1] for c in start.call_args_list], [inst["id"]])
        self.assertEqual([i["id"] for i in result["started"]], [inst["id"]])


# --------------------------------------------------------------------------
# tick / idle rule
# --------------------------------------------------------------------------

class TestTick(ScheduleTestCase):
    def status(self, mapping):
        return mock.patch.object(runner, "status_many", return_value=mapping)

    def test_reconcile_runs_and_idle_zero_disables_the_rule(self):
        db.set_setting(self.conn, "idle_minutes", "0")
        demo = self.add_project("demo")
        inst = self.add_instance(demo, port=4000, idle_since=ago(500))
        with self.status({inst["id"]: {"cpu_ns": 1}}) as status, \
                mock.patch.object(runner, "stop") as stop:
            result = schedule.tick(self.conn)
        self.reconcile.reconcile.assert_called_once_with(self.conn)
        status.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(result["stopped"], [])
        self.assertEqual(result["reconcile"]["listeners"], 3)

    def test_first_idle_tick_sets_idle_since(self):
        demo = self.add_project("demo")
        inst = self.add_instance(demo, port=4000)
        with self.status({inst["id"]: {"cpu_ns": 1_000_000}}), \
                mock.patch.object(runner, "stop") as stop:
            result = schedule.tick(self.conn)
        stop.assert_not_called()
        row = self.row(inst["id"])
        self.assertIsNotNone(row["idle_since"])
        self.assertEqual(row["cpu_ns"], 1_000_000)
        self.assertIsNotNone(row["cpu_checked_at"])
        self.assertEqual(result["skipped"][0]["reason"], "idle since now")
        self.sysinfo.established_count.assert_called_once_with(4000)

    def test_connections_make_it_busy_and_clear_idle_since(self):
        demo = self.add_project("demo")
        inst = self.add_instance(demo, port=4000, idle_since=ago(30))
        self.sysinfo.established_count.return_value = 2
        with self.status({inst["id"]: {"cpu_ns": 5}}), \
                mock.patch.object(runner, "stop") as stop:
            result = schedule.tick(self.conn)
        stop.assert_not_called()
        self.assertIsNone(self.row(inst["id"])["idle_since"])
        self.assertIn("busy", result["skipped"][0]["reason"])

    def test_cpu_growth_over_two_seconds_is_busy(self):
        demo = self.add_project("demo")
        inst = self.add_instance(demo, port=4000, idle_since=ago(30), cpu_ns=1_000_000_000)
        with self.status({inst["id"]: {"cpu_ns": 1_000_000_000 + 2_500_000_000}}), \
                mock.patch.object(runner, "stop") as stop:
            schedule.tick(self.conn)
        stop.assert_not_called()
        row = self.row(inst["id"])
        self.assertIsNone(row["idle_since"])
        self.assertEqual(row["cpu_ns"], 3_500_000_000)

    def test_small_cpu_growth_stays_idle(self):
        demo = self.add_project("demo")
        inst = self.add_instance(demo, port=4000, idle_since=ago(5), cpu_ns=1_000_000_000)
        with self.status({inst["id"]: {"cpu_ns": 1_500_000_000}}), \
                mock.patch.object(runner, "stop") as stop:
            schedule.tick(self.conn)
        stop.assert_not_called()
        self.assertIsNotNone(self.row(inst["id"])["idle_since"])

    def test_stops_after_the_threshold(self):
        db.set_setting(self.conn, "idle_minutes", "90")
        demo = self.add_project("demo")
        inst = self.add_instance(demo, port=4000, idle_since=ago(120))
        with self.status({inst["id"]: {"cpu_ns": 1}}), \
                mock.patch.object(runner, "stop") as stop:
            result = schedule.tick(self.conn)
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[1], inst["id"])
        self.assertEqual(stop.call_args.kwargs["reason"], "idle")
        self.assertEqual([i["id"] for i in result["stopped"]], [inst["id"]])
        self.assertIn("idle 120 min", result["stopped"][0]["reason"])

    def test_pinned_unmanaged_and_owned_are_never_stopped(self):
        pinned = self.add_project("pinnedproj", pinned=1)
        pinned_instance = self.add_instance(pinned, port=3300, idle_since=ago(300))
        legacy = self.add_project("legacy")
        unmanaged = self.add_instance(legacy, port=5000, managed=0, idle_since=ago(300))
        owned_project = self.add_project("owned")
        owned = self.add_instance(owned_project, port=4200, idle_since=ago(300),
                                  owner_session="sess-1")

        with self.status({pinned_instance["id"]: {"cpu_ns": 1},
                          unmanaged["id"]: {"cpu_ns": 1},
                          owned["id"]: {"cpu_ns": 1}}), \
                mock.patch.object(runner, "stop") as stop:
            result = schedule.tick(self.conn)

        stop.assert_not_called()
        reasons = {i["id"]: i["reason"] for i in result["skipped"]}
        self.assertEqual(reasons[pinned_instance["id"]], "pinned")
        self.assertEqual(reasons[unmanaged["id"]], "unmanaged")
        self.assertEqual(reasons[owned["id"]], "owned")
        self.assertIsNone(self.row(owned["id"])["idle_since"])

    def test_stop_failure_is_collected(self):
        demo = self.add_project("demo")
        inst = self.add_instance(demo, port=4000, idle_since=ago(300))
        with self.status({inst["id"]: {"cpu_ns": 1}}), \
                mock.patch.object(runner, "stop",
                                  side_effect=runner.RunnerError("stop failed")):
            result = schedule.tick(self.conn)
        self.assertEqual(result["stopped"], [])
        self.assertIn("stop failed", result["errors"][0]["reason"])

    def test_reconcile_failure_does_not_break_the_tick(self):
        self.reconcile.reconcile.side_effect = RuntimeError("ss missing")
        demo = self.add_project("demo")
        inst = self.add_instance(demo, port=4000)
        with self.status({inst["id"]: {"cpu_ns": 1}}), \
                mock.patch.object(runner, "stop") as stop:
            result = schedule.tick(self.conn)
        stop.assert_not_called()
        self.assertIn("ss missing", result["errors"][0]["reason"])
        self.assertIsNotNone(self.row(inst["id"])["idle_since"])

    def test_tick_event_is_recorded(self):
        demo = self.add_project("demo")
        self.add_instance(demo, port=4000)
        with self.status({}), mock.patch.object(runner, "stop"):
            schedule.tick(self.conn)
        self.assertIn("schedule.tick", [e["kind"] for e in self.events()])


# --------------------------------------------------------------------------
# notify
# --------------------------------------------------------------------------

class TestNotify(ScheduleTestCase):
    def test_disabled_by_default(self):
        with mock.patch("portboard.schedule.subprocess.run") as run:
            schedule.notify("cokolvek", self.conn)
        run.assert_not_called()

    def test_enabled_pipes_json_to_the_hook(self):
        db.set_setting(self.conn, "notify_on_schedule", "1")
        with mock.patch("portboard.schedule.subprocess.run") as run:
            schedule.notify("Portboard vecerne zastavenie: 2", self.conn)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "python3")
        self.assertTrue(argv[1].endswith("/.claude/hooks/notify-hermes.py"))
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["message"],
                         "Portboard vecerne zastavenie: 2")
        self.assertEqual(run.call_args.kwargs["timeout"], schedule.NOTIFY_TIMEOUT)

    def test_hook_failure_is_swallowed(self):
        db.set_setting(self.conn, "notify_on_schedule", "1")
        with mock.patch("portboard.schedule.subprocess.run", side_effect=OSError("no python")):
            schedule.notify("x", self.conn)  # must not raise

    def test_evening_stop_notifies_when_enabled(self):
        db.set_setting(self.conn, "notify_on_schedule", "1")
        demo = self.add_project("demo")
        self.add_instance(demo, port=4000)
        with mock.patch.object(runner, "stop"), \
                mock.patch("portboard.schedule.subprocess.run") as run:
            schedule.evening_stop(self.conn)
        run.assert_called_once()
        message = json.loads(run.call_args.kwargs["input"])["message"]
        self.assertIn("demo@main", message)


if __name__ == "__main__":
    unittest.main()
