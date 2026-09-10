"""Tests for portboard.reconcile against a seeded DB and a faked machine."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("PORTBOARD_STATE_DIR", tempfile.mkdtemp(prefix="portboard-test-"))

from portboard import config, db, reconcile, sysinfo  # noqa: E402


def listener(port, pid=None, comm=None, bind="0.0.0.0"):
    return sysinfo.Listener(port=port, proto="tcp", bind=bind, pid=pid, comm=comm, fd=3)


def proc(pid, cwd=None, unit=None, container=None, comm="node"):
    return sysinfo.ProcInfo(pid=pid, cwd=cwd, cmdline=f"{comm} run dev", comm=comm,
                            unit=unit, container=container, uid=1000, ppid=1)


def container(name, ports=(), workdir=None, project=None, cid=None, network="bridge", pid=None):
    return sysinfo.Container(id=cid or (name.encode().hex() + "0" * 64)[:64], name=name,
                             state="running", image="img", ports=list(ports),
                             compose_project=project, compose_workdir=workdir,
                             network_mode=network, pid=pid)


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="portboard-recon-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._db_path = config.DB_PATH
        config.DB_PATH = self.tmp / "portboard.db"
        self.addCleanup(setattr, config, "DB_PATH", self._db_path)

        self.proj = self.tmp / "demo"
        self.wt = self.proj / ".claude" / "worktrees" / "wt1"
        self.wt.mkdir(parents=True)

        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        ts = db.now()
        self.conn.execute(
            "INSERT INTO projects(id, name, path, kind, base_port, slots, created_at, updated_at)"
            " VALUES (1, 'demo', ?, 'transient', 4000, 9, ?, ?)", (str(self.proj), ts, ts))
        self.add_instance(1, "main", 0, str(self.proj), 4000, unit="demo-dev.service")
        self.add_instance(2, "wt1", 1, str(self.wt), 4001)

        self.procs = {}
        self.listeners = []
        self.containers = []
        self.stats = {}

    # ---------------------------------------------------------------- helpers
    def add_instance(self, iid, label, slot, path, port, unit=None, managed=1,
                     state="stopped", **extra):
        ts = db.now()
        cols = dict(id=iid, project_id=1, label=label, slot=slot, path=path, port=port,
                    unit=unit, managed=managed, state=state, created_at=ts, updated_at=ts)
        cols.update(extra)
        names = ", ".join(cols)
        marks = ", ".join("?" * len(cols))
        self.conn.execute(f"INSERT INTO instances({names}) VALUES ({marks})", list(cols.values()))

    def run_reconcile(self, **kwargs):
        with mock.patch.object(sysinfo, "listening_ports", return_value=self.listeners), \
             mock.patch.object(sysinfo, "docker_containers", return_value=self.containers) as docker, \
             mock.patch.object(sysinfo, "docker_available", return_value=True), \
             mock.patch.object(sysinfo, "proc_info", side_effect=lambda pid: self.procs[pid]), \
             mock.patch.object(sysinfo, "unit_cgroup_stats", return_value=self.stats):
            self.docker_mock = docker
            return reconcile.reconcile(self.conn, **kwargs)

    def instance(self, iid):
        return db.row(self.conn.execute("SELECT * FROM instances WHERE id = ?", (iid,)).fetchone())

    def observed(self, port):
        return db.row(self.conn.execute("SELECT * FROM observed WHERE port = ?", (port,)).fetchone())

    def reserved(self):
        return {r["port"]: r for r in db.rows(self.conn.execute("SELECT * FROM reserved_ports"))}

    # ------------------------------------------------------------------ tests
    def test_match_by_unit_and_start_transition(self):
        self.listeners = [listener(4000, pid=111, comm="node")]
        self.procs = {111: proc(111, cwd=str(self.proj), unit="demo-dev.service")}
        self.stats = {"demo-dev.service": {"MemoryCurrent": 1234, "CPUUsageNSec": 99, "MainPID": 111}}
        summary = self.run_reconcile(quick=True)

        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["listeners"], 1)
        self.assertIn(1, summary["started"])
        inst = self.instance(1)
        self.assertEqual(inst["state"], "running")
        self.assertEqual((inst["pid"], inst["actual_port"]), (111, 4000))
        self.assertEqual((inst["mem_bytes"], inst["cpu_ns"]), (1234, 99))
        self.assertTrue(inst["last_seen_at"] and inst["started_at"])
        row = self.observed(4000)
        self.assertEqual((row["project_id"], row["instance_id"], row["unit"]), (1, 1, "demo-dev.service"))

    def test_match_by_cwd_path_prefix_picks_the_worktree(self):
        self.listeners = [listener(4001, pid=222, comm="node")]
        self.procs = {222: proc(222, cwd=str(self.wt / "src"))}
        summary = self.run_reconcile(quick=True)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(self.instance(2)["state"], "running")
        self.assertEqual(self.instance(2)["actual_port"], 4001)
        self.assertEqual(self.observed(4001)["instance_id"], 2)

    def test_listener_nobody_here_started_becomes_unmanaged_with_its_unit(self):
        # instance 2 has no unit: the runner never started it, the user's own
        # systemd service is serving its port
        self.listeners = [listener(4001, pid=222, comm="python")]
        self.procs = {222: proc(222, cwd=str(self.wt), unit="dots-tts.service", comm="python")}
        summary = self.run_reconcile(quick=True)
        self.assertEqual(summary["started"], [2])
        inst = self.instance(2)
        self.assertEqual((inst["state"], inst["managed"], inst["unit"]), ("running", 0, "dots-tts.service"))
        # ...and the one the runner did start keeps its flag
        self.assertEqual(self.instance(1)["managed"], 1)

    def test_starting_instance_keeps_managed_flag_when_it_comes_up(self):
        self.conn.execute("UPDATE instances SET state='starting', unit=NULL WHERE id=2")
        self.listeners = [listener(4001, pid=222, comm="node")]
        self.procs = {222: proc(222, cwd=str(self.wt))}
        self.run_reconcile(quick=True)
        inst = self.instance(2)
        self.assertEqual((inst["state"], inst["managed"]), ("running", 1))

    def test_running_instance_that_vanished_is_a_crash(self):
        self.conn.execute("UPDATE instances SET state='running', pid=999 WHERE id=1")
        summary = self.run_reconcile(quick=True)
        self.assertEqual(summary["stopped"], [1])
        inst = self.instance(1)
        self.assertEqual((inst["state"], inst["stopped_by"], inst["pid"]), ("stopped", "crash", None))

    def test_recent_stop_is_not_a_crash(self):
        just_now = (datetime.now() - timedelta(seconds=5)).replace(microsecond=0).isoformat()
        self.conn.execute("UPDATE instances SET state='running', stopped_at=?, stopped_by='user' WHERE id=1",
                          (just_now,))
        self.run_reconcile(quick=True)
        inst = self.instance(1)
        self.assertEqual((inst["state"], inst["stopped_by"], inst["stopped_at"]), ("stopped", "user", just_now))

    def test_old_stop_timestamp_still_counts_as_crash(self):
        old = (datetime.now() - timedelta(minutes=5)).replace(microsecond=0).isoformat()
        self.conn.execute("UPDATE instances SET state='running', stopped_at=?, stopped_by='user' WHERE id=1", (old,))
        self.run_reconcile(quick=True)
        self.assertEqual(self.instance(1)["stopped_by"], "crash")

    def test_unmanaged_instance_that_vanished_is_stopped_without_crash(self):
        self.conn.execute("UPDATE instances SET state='running', managed=0 WHERE id=2")
        summary = self.run_reconcile(quick=True)
        self.assertEqual(summary["stopped"], [2])
        inst = self.instance(2)
        self.assertEqual(inst["state"], "stopped")
        self.assertIsNone(inst["stopped_by"])

    def test_starting_instances_are_left_alone(self):
        self.conn.execute("UPDATE instances SET state='starting' WHERE id=2")
        summary = self.run_reconcile(quick=True)
        self.assertEqual(summary["stopped"], [])
        self.assertEqual(self.instance(2)["state"], "starting")

    def test_unknown_listener_in_project_is_reported_not_adopted(self):
        wt2 = self.proj / ".claude" / "worktrees" / "wt2"
        self.listeners = [listener(4002, pid=333, comm="node")]
        self.procs = {333: proc(333, cwd=str(wt2))}
        summary = self.run_reconcile(quick=True)
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(summary["unknown_in_projects"], [{"port": 4002, "cwd": str(wt2), "project": "demo"}])
        row = self.observed(4002)
        self.assertEqual(row["project_id"], 1)
        self.assertIsNone(row["instance_id"])
        self.assertNotIn(4002, self.reserved())  # inside a project: not reserved

    def test_adopt_unknown_creates_an_instance(self):
        wt2 = self.proj / ".claude" / "worktrees" / "wt2"
        self.listeners = [listener(4002, pid=333, comm="node"), listener(4003, pid=333, comm="node")]
        self.procs = {333: proc(333, cwd=str(wt2), unit="wt2.service")}
        summary = self.run_reconcile(quick=True, adopt_unknown=True)

        inst = db.row(self.conn.execute("SELECT * FROM instances WHERE label = 'wt2'").fetchone())
        self.assertIsNotNone(inst)
        self.assertEqual((inst["managed"], inst["state"], inst["port"], inst["slot"]), (0, "running", 4002, 2))
        self.assertEqual((inst["path"], inst["unit"]), (str(wt2), "wt2.service"))
        self.assertEqual(json.loads(inst["extra_json"])["source"], "adopted")
        self.assertIn(inst["id"], summary["started"])
        # both listeners of that checkout point at the one new row
        self.assertEqual(self.observed(4002)["instance_id"], inst["id"])
        self.assertEqual(self.observed(4003)["instance_id"], inst["id"])

    def test_adopt_main_uses_slot_zero_and_survives_port_clash(self):
        self.conn.execute("DELETE FROM instances WHERE id = 1")
        self.listeners = [listener(4001, pid=444, comm="node")]  # port already taken by instance 2
        self.procs = {444: proc(444, cwd=str(self.proj))}
        self.run_reconcile(quick=True, adopt_unknown=True)
        inst = db.row(self.conn.execute("SELECT * FROM instances WHERE label = 'main'").fetchone())
        self.assertEqual((inst["slot"], inst["managed"]), (0, 0))
        self.assertIsNone(inst["port"])
        self.assertEqual(inst["actual_port"], 4001)

    def test_unknown_listener_outside_projects_is_reserved(self):
        self.conn.execute("INSERT INTO reserved_ports(port, label, source, updated_at)"
                          " VALUES (8888, 'gone', 'observed', ?)", (db.now(),))
        self.listeners = [listener(9999, pid=555, comm="redis-server"), listener(5000)]
        self.procs = {555: proc(555, cwd="/opt/redis", comm="redis-server")}
        summary = self.run_reconcile(quick=True)

        res = self.reserved()
        self.assertEqual(res[9999]["label"], "redis-server")
        self.assertEqual(res[9999]["source"], "observed")
        self.assertEqual(res[5000]["label"], "system")      # no pid at all
        self.assertNotIn(8888, res)                          # stale observed row removed
        self.assertEqual(res[22]["source"], "static")        # from settings.reserved_static
        self.assertEqual(summary["reserved"], len(res))
        self.assertEqual(summary["unknown"], 2)

    def test_static_reserved_ports_win_over_observed(self):
        self.listeners = [listener(631, comm="cupsd")]
        self.run_reconcile(quick=True)
        self.assertEqual(self.reserved()[631]["source"], "static")

    def test_docker_bridge_port_matches_through_compose_workdir(self):
        self.containers = [container("demo-frontend", ports=[(3000, 3000)], workdir=str(self.proj),
                                     project="demo-compose")]
        self.listeners = [listener(3000)]  # docker-proxy: root owned, no pid
        summary = self.run_reconcile()
        self.assertEqual(summary["matched"], 1)
        self.assertTrue(summary["docker"])
        self.assertEqual(self.instance(1)["state"], "running")
        row = self.observed(3000)
        self.assertEqual((row["container"], row["compose_project"], row["instance_id"]),
                         ("demo-frontend", "demo-compose", 1))

    def test_host_network_container_matches_through_pid_cgroup(self):
        cid = "a" * 64
        self.containers = [container("demo-host", workdir=str(self.wt), project="demo",
                                     cid=cid, network="host", pid=777)]
        self.listeners = [listener(8765, pid=777, comm="python")]
        self.procs = {777: proc(777, cwd="/app", container=cid[:12], comm="python")}
        summary = self.run_reconcile()
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(self.instance(2)["state"], "running")   # the worktree, via compose_workdir
        self.assertEqual(self.observed(8765)["container"], "demo-host")

    def test_container_name_matches_instance_unit(self):
        self.conn.execute("UPDATE instances SET unit = 'demo-host' WHERE id = 2")
        cid = "b" * 64
        self.containers = [container("demo-host", cid=cid, network="host", pid=778)]
        self.listeners = [listener(8766, pid=778, comm="python")]
        self.procs = {778: proc(778, cwd="/app", container=cid, comm="python")}
        self.assertEqual(self.run_reconcile()["matched"], 1)
        self.assertEqual(self.instance(2)["state"], "running")

    # --------------------------------------------------- kind='container'
    def add_container_project(self, pid_, name, start_cmd, base_port):
        ts = db.now()
        self.conn.execute(
            "INSERT INTO projects(id, name, path, kind, start_cmd, port_mode, base_port,"
            " slots, created_at, updated_at) VALUES (?, ?, ?, 'container', ?, 'fixed', ?, 9, ?, ?)",
            (pid_, name, str(self.tmp / name), start_cmd, base_port, ts, ts))

    def test_hand_started_container_matches_the_instance_by_name(self):
        self.add_container_project(2, "rma-admin-app", "rma-admin", 3100)
        # nobody here started it: unit is NULL, only the name is derivable
        self.add_instance(3, "main", 0, str(self.tmp / "rma-admin-app"), 3100,
                          project_id=2)
        cid = "c" * 64
        self.containers = [container("rma-admin", cid=cid, network="host", pid=900)]
        self.listeners = [listener(3100, pid=900, comm="node")]
        self.procs = {900: proc(900, cwd="/app", container=cid[:12], comm="node")}

        summary = self.run_reconcile()

        self.assertEqual(summary["matched"], 1)
        inst = self.instance(3)
        self.assertEqual((inst["state"], inst["actual_port"], inst["pid"]),
                         ("running", 3100, 900))
        # not started by us: unmanaged, and the container is remembered as its unit
        self.assertEqual((inst["managed"], inst["unit"]), (0, "rma-admin"))
        self.assertEqual(self.observed(3100)["instance_id"], 3)

    def test_hand_started_container_matches_a_worktree_by_suffix(self):
        self.add_container_project(2, "rma-admin-app", "rma-admin", 3100)
        self.add_instance(3, "main", 0, str(self.tmp / "rma-admin-app"), 3100, project_id=2)
        self.add_instance(4, "csv-attributes", 1,
                          str(self.tmp / "rma-admin-app" / ".claude" / "worktrees" / "csv-attributes"),
                          3101, project_id=2)
        cid = "d" * 64
        self.containers = [container("rma-admin-csv-attributes", cid=cid, network="host", pid=901)]
        self.listeners = [listener(3101, pid=901, comm="node")]
        self.procs = {901: proc(901, cwd="/app", container=cid[:12], comm="node")}

        self.run_reconcile()

        self.assertEqual(self.instance(4)["state"], "running")
        self.assertEqual(self.instance(3)["state"], "stopped")

    def test_container_started_by_portboard_keeps_its_managed_flag(self):
        self.add_container_project(2, "rma-admin-app", "rma-admin", 3100)
        # the runner recorded the unit before waiting: this one is ours
        self.add_instance(3, "main", 0, str(self.tmp / "rma-admin-app"), 3100,
                          project_id=2, unit="rma-admin", state="starting")
        cid = "e" * 64
        self.containers = [container("rma-admin", cid=cid, network="host", pid=902)]
        self.listeners = [listener(3100, pid=902, comm="node")]
        self.procs = {902: proc(902, cwd="/app", container=cid[:12], comm="node")}

        self.run_reconcile()

        inst = self.instance(3)
        self.assertEqual((inst["state"], inst["managed"], inst["unit"]),
                         ("running", 1, "rma-admin"))

    def test_group_instance_is_never_marked_crashed(self):
        ts = db.now()
        self.conn.execute(
            "INSERT INTO projects(id, name, path, kind, port_mode, slots, created_at, updated_at)"
            " VALUES (3, 'rma', ?, 'group', 'none', 9, ?, ?)", (str(self.tmp / "rma"), ts, ts))
        # the mirror can leave the stored row at 'running'; it has no listener
        self.add_instance(5, "main", 0, str(self.tmp / "rma"), None,
                          project_id=3, state="running")
        summary = self.run_reconcile()
        self.assertNotIn(5, summary["stopped"])
        self.assertEqual(self.instance(5)["state"], "running")

    def test_quick_skips_docker(self):
        self.listeners = []
        summary = self.run_reconcile(quick=True)
        self.docker_mock.assert_not_called()
        self.assertFalse(summary["docker"])

    def test_observed_is_rewritten_and_events_written(self):
        self.conn.execute("INSERT INTO observed(port, proto, bind, seen_at) VALUES (1111, 'tcp', '', ?)",
                          (db.now(),))
        self.listeners = [listener(4000, pid=111), listener(4000, pid=111, bind="::")]
        self.procs = {111: proc(111, cwd=str(self.proj))}
        summary = self.run_reconcile(quick=True)
        rows = db.rows(self.conn.execute("SELECT * FROM observed"))
        self.assertEqual({r["port"] for r in rows}, {4000})
        self.assertEqual(len(rows), 2)                     # both binds kept
        self.assertEqual(summary["listeners"], 2)
        self.assertEqual(summary["matched"], 1)            # one instance, two sockets
        self.assertTrue(db.get_setting(self.conn, "reconciled_at"))
        kinds = [e["kind"] for e in db.rows(self.conn.execute("SELECT * FROM events"))]
        self.assertIn("reconcile", kinds)
        self.assertIsInstance(summary["took_ms"], int)

    def test_failure_rolls_back(self):
        self.listeners = [listener(4000, pid=111)]
        self.procs = {111: proc(111, cwd=str(self.proj))}
        with mock.patch.object(reconcile.db, "add_event", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_reconcile(quick=True)
        self.assertEqual(self.instance(1)["state"], "stopped")
        self.assertEqual(db.rows(self.conn.execute("SELECT * FROM observed")), [])


if __name__ == "__main__":
    unittest.main()


class QuickModeDockerBackedTests(unittest.TestCase):
    """quick=True must never mark compose/none instances stopped: docker was not inspected."""

    def test_quick_keeps_compose_instance_running(self):
        import sqlite3, tempfile, os, json
        from unittest import mock
        from portboard import db, reconcile
        from tests import assert_isolated
        assert_isolated()
        tmp = tempfile.mkdtemp()
        conn = db.connect()
        ts = db.now()
        conn.execute("DELETE FROM instances"); conn.execute("DELETE FROM projects")
        conn.execute("INSERT INTO projects(name, path, kind, port_mode, base_port, created_at, updated_at)"
                     " VALUES ('wh', ?, 'compose', 'fixed', 18765, ?, ?)", (tmp, ts, ts))
        pid = conn.execute("SELECT id FROM projects WHERE name='wh'").fetchone()[0]
        conn.execute("INSERT INTO instances(project_id, label, slot, path, port, managed, state, extra_json,"
                     " created_at, updated_at) VALUES (?, 'main', 0, ?, 18765, 1, 'running',"
                     " '{\"container_id\": \"abcdef123456abcdef123456\"}', ?, ?)", (pid, tmp, ts, ts))
        iid = conn.execute("SELECT id FROM instances").fetchone()[0]
        # quick mode, listener invisible (bridge-published, root-owned): must stay running
        with mock.patch.object(reconcile.sysinfo, "listening_ports", return_value=[]), \
             mock.patch.object(reconcile.sysinfo, "unit_cgroup_stats", return_value={}):
            summary = reconcile.reconcile(conn, quick=True)
        self.assertEqual(summary["stopped"], [])
        self.assertEqual(conn.execute("SELECT state FROM instances WHERE id=?", (iid,)).fetchone()[0], "running")
        # quick mode, host-network container visible through its pid cgroup: matched by remembered id
        lst = reconcile.sysinfo.Listener(port=18765, proto="tcp", bind="0.0.0.0", pid=4242, comm="python")
        info = reconcile.sysinfo.ProcInfo(pid=4242, cwd=None, cmdline="python server.py", comm="python",
                                          unit=None, container="abcdef123456", uid=0, ppid=1)
        with mock.patch.object(reconcile.sysinfo, "listening_ports", return_value=[lst]), \
             mock.patch.object(reconcile.sysinfo, "proc_info", return_value=info), \
             mock.patch.object(reconcile.sysinfo, "unit_cgroup_stats", return_value={}):
            summary = reconcile.reconcile(conn, quick=True)
        row = conn.execute("SELECT state, actual_port, pid FROM instances WHERE id=?", (iid,)).fetchone()
        self.assertEqual(tuple(row), ("running", 18765, 4242))
        conn.close()
