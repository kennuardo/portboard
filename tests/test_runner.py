"""Tests for portboard.runner.

registry, sysinfo and reconcile are written by other agents; here they are
replaced by stub modules installed into ``sys.modules`` (the runner imports them
lazily, so the stub is what it sees).  The fake registry is backed by the real
SQLite schema, so state transitions are asserted against real rows.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_STATE = tempfile.mkdtemp(prefix="portboard-test-runner-")
os.environ["PORTBOARD_STATE_DIR"] = _STATE
# Keep the library's warnings out of the test output (no handler = lastResort).
logging.getLogger("portboard").addHandler(logging.NullHandler())

import portboard  # noqa: E402
from portboard import config, db, runner  # noqa: E402


# --------------------------------------------------------------------------
# stub plumbing
# --------------------------------------------------------------------------

def install_stub(testcase: unittest.TestCase, name: str, **attrs) -> types.ModuleType:
    """Install portboard.<name> as a stub module for the duration of one test."""
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


def safe_label(label: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(label or "").lower()).strip("-")


def make_registry() -> dict:
    """Fake registry.* functions on top of the real schema."""

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
        port = data.get("port")
        data["url"] = f"http://localhost:{port}{project.get('open_path', '/')}" if port else None
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
            conn.execute(
                f"UPDATE instances SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), db.now(), instance_id),
            )
        return get_instance(conn, instance_id)

    return {
        "get_project": get_project,
        "get_instance": get_instance,
        "list_instances": list_instances,
        "update_instance": update_instance,
        "safe_label": safe_label,
    }


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeRun:
    """Records every subprocess.run call and answers from a rule list."""

    def __init__(self, rules=None, default=(0, "", "")):
        self.calls: list[dict] = []
        self.rules = rules or []          # [(substring, (rc, out, err)), ...]
        self.default = default

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        joined = " ".join(str(a) for a in argv)
        for needle, answer in self.rules:
            if needle in joined:
                return completed(argv, *answer)
        return completed(argv, *self.default)

    def argvs(self):
        return [c["argv"] for c in self.calls]

    def find(self, needle):
        for call in self.calls:
            if needle in " ".join(str(a) for a in call["argv"]):
                return call
        return None


class Listener:
    def __init__(self, port, pid=None, proto="tcp", bind="127.0.0.1", comm="python3"):
        self.port, self.pid, self.proto, self.bind, self.comm = port, pid, proto, bind, comm


class ProcInfo:
    def __init__(self, pid, unit=None, container=None, cwd=None):
        self.pid, self.unit, self.container, self.cwd = pid, unit, container, cwd
        self.cmdline = ""
        self.comm = "python3"
        self.uid = os.getuid()
        self.ppid = 1


class Container:
    def __init__(self, name, compose_project, state="running", pid=None, ports=()):
        self.name, self.compose_project, self.state = name, compose_project, state
        self.pid, self.ports = pid, list(ports)
        self.id = name
        self.image = "img"
        self.compose_workdir = "/tmp"
        self.network_mode = "bridge"


# --------------------------------------------------------------------------
# base case
# --------------------------------------------------------------------------

class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="portboard-db-")
        self._old_state, self._old_db = config.STATE_DIR, config.DB_PATH
        config.STATE_DIR = Path(self.tmp)
        config.DB_PATH = Path(self.tmp) / "portboard.db"
        self.addCleanup(self._restore_paths)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.registry = install_stub(self, "registry", **make_registry())

    def _restore_paths(self):
        config.STATE_DIR, config.DB_PATH = self._old_state, self._old_db

    # seeding -------------------------------------------------------------
    def add_project(self, name="demo", **fields):
        row = {
            "name": name, "path": f"/tmp/{name}", "kind": "transient",
            "start_cmd": "npm run dev", "stop_cmd": None, "port_mode": "env",
            "base_port": 4000, "slots": 9, "health_path": "/", "open_path": "/",
            "pinned": 0, "autostart": "schedule", "env_json": "{}",
            "path_prepend": None, "memory_max": None, "source": "cli", "notes": None,
        }
        row.update(fields)
        columns = ", ".join(row) + ", created_at, updated_at"
        marks = ", ".join("?" * (len(row) + 2))
        cur = self.conn.execute(
            f"INSERT INTO projects({columns}) VALUES ({marks})",
            (*row.values(), db.now(), db.now()),
        )
        return self.registry.get_project(self.conn, cur.lastrowid)

    def add_instance(self, project, label="main", slot=0, port=4000, **fields):
        row = {
            "project_id": project["id"], "label": label, "slot": slot,
            "path": project["path"] if slot == 0 else f"{project['path']}/.claude/worktrees/{label}",
            "branch": None, "port": port, "unit": None, "managed": 1, "state": "stopped",
            "pid": None, "actual_port": None, "owner_session": None, "owner_seen_at": None,
            "started_at": None, "stopped_at": None, "stopped_by": None, "last_seen_at": None,
            "mem_bytes": None, "cpu_ns": None, "cpu_checked_at": None, "idle_since": None,
            "extra_json": "{}",
        }
        row.update(fields)
        columns = ", ".join(row) + ", created_at, updated_at"
        marks = ", ".join("?" * (len(row) + 2))
        cur = self.conn.execute(
            f"INSERT INTO instances({columns}) VALUES ({marks})",
            (*row.values(), db.now(), db.now()),
        )
        return self.registry.get_instance(self.conn, cur.lastrowid)

    def row(self, instance_id):
        return self.registry.get_instance(self.conn, instance_id)

    def events(self):
        return [dict(r) for r in self.conn.execute("SELECT kind, detail FROM events ORDER BY id")]

    def sysinfo(self, **attrs):
        defaults = {
            "listening_ports": mock.Mock(return_value=[]),
            "proc_info": mock.Mock(return_value=None),
            "docker_containers": mock.Mock(return_value=[]),
            "established_count": mock.Mock(return_value=0),
            "unit_cgroup_stats": mock.Mock(return_value={}),
        }
        defaults.update(attrs)
        return install_stub(self, "sysinfo", **defaults)


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

class TestNaming(RunnerTestCase):
    def test_unit_name_uses_prefix_and_safe_parts(self):
        project = self.add_project(name="Sheron World")
        instance = self.add_instance(project, label="fix/Login", slot=1)
        self.assertEqual(runner.unit_name(project, instance),
                         "portboard-sheron-world-fix-login.service")
        self.assertTrue(runner.unit_name(project, instance).startswith(config.UNIT_PREFIX))

    def test_compose_project_name_main_vs_worktree(self):
        project = self.add_project(name="shop", kind="compose")
        main = self.add_instance(project, label="main", slot=0)
        work = self.add_instance(project, label="fix-login", slot=1, port=4001)
        self.assertEqual(runner.compose_project_name(project, main), "shop")
        self.assertEqual(runner.compose_project_name(project, work), "shop-fix-login")


class TestBuildEnv(RunnerTestCase):
    def test_env_mode_sets_port_trio(self):
        project = self.add_project(port_mode="env")
        instance = self.add_instance(project, port=4310)
        env = runner.build_env(self.conn, project, instance)
        self.assertEqual(env["PORT"], "4310")
        self.assertEqual(env["NUXT_PORT"], "4310")
        self.assertEqual(env["NITRO_PORT"], "4310")
        self.assertEqual(env["PORTBOARD_PROJECT"], "demo")
        self.assertEqual(env["PORTBOARD_INSTANCE"], str(instance["id"]))
        self.assertNotIn("HOST", env)

    def test_arg_mode_leaves_port_out_of_env(self):
        project = self.add_project(port_mode="arg", start_cmd="uvicorn app:app --port {port}")
        instance = self.add_instance(project, port=4310)
        env = runner.build_env(self.conn, project, instance)
        for key in ("PORT", "NUXT_PORT", "NITRO_PORT"):
            self.assertNotIn(key, env)

    def test_path_prefers_project_prepend_then_node_path(self):
        db.set_setting(self.conn, "node_path", "/home/u/.nvm/versions/node/v22/bin")
        project = self.add_project()
        instance = self.add_instance(project)
        self.assertEqual(runner.build_env(self.conn, project, instance)["PATH"],
                         "/home/u/.nvm/versions/node/v22/bin:/usr/local/bin:/usr/bin:/bin")

        other = self.add_project(name="other", path="/tmp/other", base_port=4100,
                                 path_prepend="/opt/tools/bin")
        other_instance = self.add_instance(other, port=4100)
        self.assertEqual(runner.build_env(self.conn, other, other_instance)["PATH"],
                         "/opt/tools/bin:/usr/local/bin:/usr/bin:/bin")

    def test_path_without_any_prepend(self):
        from unittest import mock
        from portboard import install
        db.set_setting(self.conn, "node_path", "")
        project = self.add_project()
        instance = self.add_instance(project)
        with mock.patch.object(install, "detect_node_path", return_value=None):
            self.assertEqual(runner.build_env(self.conn, project, instance)["PATH"],
                             "/usr/local/bin:/usr/bin:/bin")

    def test_empty_node_path_setting_falls_back_to_detection(self):
        from unittest import mock
        from portboard import install
        db.set_setting(self.conn, "node_path", "")
        project = self.add_project()
        instance = self.add_instance(project)
        with mock.patch.object(install, "detect_node_path", return_value="/home/u/.nvm/versions/node/v20/bin"):
            self.assertEqual(runner.build_env(self.conn, project, instance)["PATH"],
                             "/home/u/.nvm/versions/node/v20/bin:/usr/local/bin:/usr/bin:/bin")

    def test_env_json_is_merged_last_and_can_override(self):
        project = self.add_project(env_json='{"PORT": "9999", "API_URL": "http://x", "N": 3}')
        instance = self.add_instance(project, port=4310)
        env = runner.build_env(self.conn, project, instance)
        self.assertEqual(env["PORT"], "9999")
        self.assertEqual(env["API_URL"], "http://x")
        self.assertEqual(env["N"], "3")

    def test_broken_env_json_is_ignored(self):
        project = self.add_project(env_json="{not json")
        instance = self.add_instance(project, port=4310)
        self.assertEqual(runner.build_env(self.conn, project, instance)["PORT"], "4310")


class TestBuildCommand(RunnerTestCase):
    def test_arg_mode_substitutes_port(self):
        project = self.add_project(port_mode="arg",
                                   start_cmd="python -m uvicorn app:app --port {port}")
        instance = self.add_instance(project, port=4444)
        self.assertEqual(runner.build_command(project, instance),
                         "python -m uvicorn app:app --port 4444")

    def test_env_mode_keeps_command_verbatim(self):
        project = self.add_project(port_mode="env", start_cmd="npm run dev")
        instance = self.add_instance(project)
        self.assertEqual(runner.build_command(project, instance), "npm run dev")


class TestSystemdRunArgv(RunnerTestCase):
    def test_shape(self):
        project = self.add_project(name="demo", memory_max="2G", env_json='{"FOO": "bar"}')
        instance = self.add_instance(project, port=4310)
        argv = runner.systemd_run_argv(self.conn, project, instance)

        self.assertEqual(argv[:2], ["systemd-run", "--user"])
        self.assertEqual(argv[2], "--unit=portboard-demo-main.service")
        self.assertTrue(argv[3].startswith("--description=portboard demo main"))
        self.assertNotIn("--collect", argv)
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "npm run dev"])

        pairs = [f"{argv[i]} {argv[i + 1]}" for i, a in enumerate(argv) if a == "-p"]
        self.assertIn("-p WorkingDirectory=/tmp/demo", pairs)
        self.assertIn("-p KillMode=control-group", pairs)
        self.assertIn("-p TimeoutStopSec=15", pairs)
        self.assertIn("-p MemoryMax=2G", pairs)

        setenvs = [a for a in argv if a.startswith("--setenv=")]
        self.assertIn("--setenv=PORT=4310", setenvs)
        self.assertIn("--setenv=FOO=bar", setenvs)
        self.assertIn(f"--setenv=PORTBOARD_INSTANCE={instance['id']}", setenvs)
        self.assertEqual(setenvs, sorted(setenvs))

    def test_memory_max_falls_back_to_settings(self):
        db.set_setting(self.conn, "memory_max", "1G")
        project = self.add_project()
        instance = self.add_instance(project)
        self.assertIn("MemoryMax=1G", runner.systemd_run_argv(self.conn, project, instance))


# --------------------------------------------------------------------------
# start / stop
# --------------------------------------------------------------------------

class TestStartTransient(RunnerTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.add_project()
        self.instance = self.add_instance(self.project, port=4310)
        self.unit = "portboard-demo-main.service"

    def test_success_records_state_pid_and_event(self):
        fake = FakeRun()
        self.sysinfo(
            listening_ports=mock.Mock(return_value=[Listener(4310, pid=4242)]),
            proc_info=mock.Mock(return_value=ProcInfo(4242, unit=self.unit)),
        )
        with mock.patch("portboard.runner.subprocess.run", fake):
            result = runner.start(self.conn, self.instance["id"])

        self.assertEqual(result["state"], "running")
        self.assertEqual(result["pid"], 4242)
        self.assertEqual(result["actual_port"], 4310)
        self.assertEqual(result["unit"], self.unit)
        self.assertIsNotNone(result["started_at"])
        self.assertIsNone(result["stopped_by"])
        self.assertIsNone(result["idle_since"])

        self.assertIsNotNone(fake.find("reset-failed"))
        systemd_run = fake.find("systemd-run")
        self.assertIsNotNone(systemd_run)
        self.assertEqual(systemd_run["timeout"], runner.START_TIMEOUT_CMD)
        self.assertTrue(systemd_run["capture_output"])
        self.assertTrue(systemd_run["text"])
        self.assertIn("instance.start", [e["kind"] for e in self.events()])

    def test_reset_failed_failure_is_ignored(self):
        fake = FakeRun(rules=[("reset-failed", (1, "", "Unit not loaded"))])
        self.sysinfo(
            listening_ports=mock.Mock(return_value=[Listener(4310, pid=7)]),
            proc_info=mock.Mock(return_value=ProcInfo(7, unit=self.unit)),
        )
        with mock.patch("portboard.runner.subprocess.run", fake):
            self.assertEqual(runner.start(self.conn, self.instance["id"])["state"], "running")

    def test_launch_failure_marks_failed_and_raises(self):
        fake = FakeRun(rules=[("systemd-run", (1, "", "Unit already exists"))])
        self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", fake):
            with self.assertRaises(runner.RunnerError) as ctx:
                runner.start(self.conn, self.instance["id"])
        self.assertIn("Unit already exists", str(ctx.exception))
        self.assertEqual(self.row(self.instance["id"])["state"], "failed")
        self.assertIn("instance.fail", [e["kind"] for e in self.events()])

    def test_unit_dies_during_wait_gives_journal_excerpt(self):
        fake = FakeRun(rules=[("journalctl", (0, "Error: listen EADDRINUSE 4310", ""))])
        self.sysinfo(
            listening_ports=mock.Mock(return_value=[]),
            unit_cgroup_stats=mock.Mock(return_value={self.unit: {"ActiveState": "failed"}}),
        )
        with mock.patch.object(runner, "POLL_INTERVAL", 0.0), \
                mock.patch("portboard.runner.subprocess.run", fake):
            with self.assertRaises(runner.RunnerError) as ctx:
                runner.start(self.conn, self.instance["id"])

        message = str(ctx.exception)
        self.assertIn("failed", message)
        self.assertIn("EADDRINUSE", message)
        self.assertEqual(self.row(self.instance["id"])["state"], "failed")

    def test_timeout_message(self):
        db.set_setting(self.conn, "start_timeout", "1")
        fake = FakeRun()
        self.sysinfo(listening_ports=mock.Mock(return_value=[Listener(9999, pid=1)]),
                     proc_info=mock.Mock(return_value=ProcInfo(1, unit="other.service")))
        with mock.patch.object(runner, "POLL_INTERVAL", 0.01), \
                mock.patch("portboard.runner.subprocess.run", fake):
            with self.assertRaises(runner.RunnerError) as ctx:
                runner.start(self.conn, self.instance["id"])
        self.assertIn("did not start listening within 1 s", str(ctx.exception))
        self.assertEqual(self.row(self.instance["id"])["state"], "failed")

    def test_no_wait_does_not_poll(self):
        fake = FakeRun()
        info = self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", fake):
            result = runner.start(self.conn, self.instance["id"], wait=False)
        self.assertEqual(result["state"], "starting")
        info.listening_ports.assert_not_called()

    def test_already_running_returns_note_without_starting(self):
        self.registry.update_instance(self.conn, self.instance["id"],
                                      state="running", pid=11, actual_port=4310)
        fake = FakeRun()
        self.sysinfo(unit_cgroup_stats=mock.Mock(
            return_value={self.unit: {"ActiveState": "active", "MainPID": "11"}}))
        with mock.patch("portboard.runner.subprocess.run", fake):
            result = runner.start(self.conn, self.instance["id"])
        self.assertEqual(result["note"], "already running")
        self.assertEqual(fake.calls, [])

    def test_missing_start_cmd_is_an_error(self):
        self.conn.execute("UPDATE projects SET start_cmd = NULL WHERE id = ?",
                          (self.project["id"],))
        self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", FakeRun()):
            with self.assertRaises(runner.RunnerError) as ctx:
                runner.start(self.conn, self.instance["id"])
        self.assertIn("no start command configured", str(ctx.exception))


class TestStartOtherKinds(RunnerTestCase):
    def test_unit_kind_starts_named_unit(self):
        project = self.add_project(kind="unit", start_cmd="sheron-dev.service",
                                   port_mode="fixed")
        instance = self.add_instance(project, port=3300)
        fake = FakeRun()
        self.sysinfo(
            listening_ports=mock.Mock(return_value=[Listener(3300, pid=99)]),
            proc_info=mock.Mock(return_value=ProcInfo(99, unit="sheron-dev.service")),
        )
        with mock.patch("portboard.runner.subprocess.run", fake):
            result = runner.start(self.conn, instance["id"])
        self.assertEqual(fake.argvs()[0],
                         ["systemctl", "--user", "start", "sheron-dev.service"])
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["unit"], "sheron-dev.service")

    def test_compose_kind_argv_and_env(self):
        project = self.add_project(name="shop", path="/tmp/shop", kind="compose",
                                   start_cmd=None, port_mode="fixed", base_port=8080)
        instance = self.add_instance(project, label="fix-login", slot=1, port=8081)
        fake = FakeRun()
        self.sysinfo(
            listening_ports=mock.Mock(return_value=[Listener(8081, pid=555)]),
            docker_containers=mock.Mock(return_value=[
                Container("shop-fix-login-web-1", "shop-fix-login", pid=555,
                          ports=[(8081, 80)])]),
        )
        with mock.patch("portboard.runner.subprocess.run", fake):
            result = runner.start(self.conn, instance["id"])
        call = fake.find("docker compose")
        self.assertEqual(call["argv"],
                         ["docker", "compose", "--project-directory",
                          "/tmp/shop/.claude/worktrees/fix-login", "up", "-d"])
        self.assertEqual(call["env"]["COMPOSE_PROJECT_NAME"], "shop-fix-login")
        self.assertEqual(call["timeout"], runner.COMPOSE_UP_TIMEOUT)
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["unit"], "shop-fix-login")

    def test_compose_start_cmd_override_runs_in_path(self):
        project = self.add_project(name="shop", path="/tmp/shop", kind="compose",
                                   start_cmd="docker compose up -d --build",
                                   port_mode="fixed", base_port=8080)
        instance = self.add_instance(project, port=8080)
        fake = FakeRun()
        self.sysinfo(
            listening_ports=mock.Mock(return_value=[Listener(8080, pid=1)]),
            docker_containers=mock.Mock(return_value=[
                Container("shop-web-1", "shop", pid=1, ports=[(8080, 80)])]),
        )
        with mock.patch("portboard.runner.subprocess.run", fake):
            runner.start(self.conn, instance["id"])
        call = fake.calls[0]
        self.assertEqual(call["argv"], ["/bin/sh", "-c", "docker compose up -d --build"])
        self.assertEqual(call["cwd"], "/tmp/shop")

    def test_none_kind_cannot_start(self):
        project = self.add_project(kind="none", start_cmd=None, port_mode="none")
        instance = self.add_instance(project, port=None)
        self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", FakeRun()):
            with self.assertRaises(runner.RunnerError) as ctx:
                runner.start(self.conn, instance["id"])
        self.assertEqual(str(ctx.exception), "no start command configured")
        self.assertEqual(self.row(instance["id"])["state"], "failed")


class TestStop(RunnerTestCase):
    def test_transient_stop_clears_runtime_fields(self):
        project = self.add_project()
        instance = self.add_instance(project, port=4310, state="running", pid=42,
                                     actual_port=4310, idle_since=db.now())
        fake = FakeRun()
        self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", fake):
            result = runner.stop(self.conn, instance["id"], reason="schedule")
        self.assertEqual(fake.argvs()[0],
                         ["systemctl", "--user", "stop", "portboard-demo-main.service"])
        self.assertEqual(fake.calls[0]["timeout"], runner.STOP_TIMEOUT)
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["stopped_by"], "schedule")
        self.assertIsNone(result["pid"])
        self.assertIsNone(result["actual_port"])
        self.assertIsNotNone(result["stopped_at"])
        self.assertIn("instance.stop", [e["kind"] for e in self.events()])

    def test_unit_not_loaded_counts_as_stopped(self):
        project = self.add_project()
        instance = self.add_instance(project, state="running", pid=42)
        fake = FakeRun(rules=[("systemctl", (5, "", "Failed to stop x: Unit x not loaded."))])
        self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", fake):
            result = runner.stop(self.conn, instance["id"])
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["stopped_by"], "user")

    def test_real_stop_failure_raises_and_keeps_state(self):
        project = self.add_project()
        instance = self.add_instance(project, state="running", pid=42)
        fake = FakeRun(rules=[("systemctl", (1, "", "Interactive authentication required"))])
        self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", fake):
            with self.assertRaises(runner.RunnerError):
                runner.stop(self.conn, instance["id"])
        self.assertEqual(self.row(instance["id"])["state"], "running")

    def test_compose_stop_uses_stop_not_down(self):
        project = self.add_project(name="shop", path="/tmp/shop", kind="compose",
                                   start_cmd=None, port_mode="fixed", base_port=8080)
        instance = self.add_instance(project, port=8080, state="running")
        fake = FakeRun()
        self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", fake):
            runner.stop(self.conn, instance["id"])
        self.assertEqual(fake.calls[0]["argv"],
                         ["docker", "compose", "--project-directory", "/tmp/shop", "stop"])
        self.assertNotIn("down", " ".join(fake.calls[0]["argv"]))

    def test_stop_cmd_override(self):
        project = self.add_project(stop_cmd="pkill -f 'npm run dev'")
        instance = self.add_instance(project, state="running")
        fake = FakeRun()
        self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", fake):
            runner.stop(self.conn, instance["id"])
        self.assertEqual(fake.calls[0]["argv"], ["/bin/sh", "-c", "pkill -f 'npm run dev'"])
        self.assertEqual(fake.calls[0]["cwd"], "/tmp/demo")

    def test_none_kind_stops_observed_container(self):
        project = self.add_project(kind="none", start_cmd=None, port_mode="fixed")
        instance = self.add_instance(project, port=5000, state="running")
        self.conn.execute(
            "INSERT INTO observed(port, proto, bind, pid, comm, container, instance_id, seen_at) "
            "VALUES (5000, 'tcp', '0.0.0.0', 321, 'docker-proxy', 'legacy-api', ?, ?)",
            (instance["id"], db.now()),
        )
        fake = FakeRun()
        self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", fake):
            result = runner.stop(self.conn, instance["id"])
        self.assertEqual(fake.calls[0]["argv"], ["docker", "stop", "legacy-api"])
        self.assertEqual(result["state"], "stopped")

    def test_none_kind_without_container_or_pid_errors(self):
        project = self.add_project(kind="none", start_cmd=None, port_mode="fixed")
        instance = self.add_instance(project, port=5001, state="running")
        self.sysinfo()
        with mock.patch("portboard.runner.subprocess.run", FakeRun()):
            with self.assertRaises(runner.RunnerError) as ctx:
                runner.stop(self.conn, instance["id"])
        self.assertIn("nothing to stop", str(ctx.exception))

    def test_none_kind_signals_own_process_group(self):
        project = self.add_project(kind="none", start_cmd=None, port_mode="fixed")
        instance = self.add_instance(project, port=5002, state="running", pid=os.getpid())
        self.sysinfo()
        killed = []
        with mock.patch("portboard.runner.subprocess.run", FakeRun()), \
                mock.patch("portboard.runner.os.killpg", lambda pgid, sig: killed.append((pgid, sig))):
            result = runner.stop(self.conn, instance["id"])
        self.assertEqual(killed, [(os.getpgid(os.getpid()), 15)])
        self.assertEqual(result["state"], "stopped")


class TestRestart(RunnerTestCase):
    def test_restart_stops_then_starts(self):
        project = self.add_project()
        instance = self.add_instance(project, port=4310, state="running", pid=1)
        fake = FakeRun()
        self.sysinfo(
            listening_ports=mock.Mock(return_value=[Listener(4310, pid=77)]),
            proc_info=mock.Mock(return_value=ProcInfo(77, unit="portboard-demo-main.service")),
        )
        with mock.patch("portboard.runner.subprocess.run", fake):
            result = runner.restart(self.conn, instance["id"])
        joined = [" ".join(a) for a in fake.argvs()]
        self.assertTrue(joined[0].startswith("systemctl --user stop"))
        self.assertTrue(any(j.startswith("systemd-run") for j in joined))
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["pid"], 77)
        kinds = [e["kind"] for e in self.events()]
        self.assertEqual(kinds[:3], ["instance.stop", "instance.start", "instance.restart"])


# --------------------------------------------------------------------------
# status / logs
# --------------------------------------------------------------------------

class TestStatusMany(RunnerTestCase):
    def test_one_call_per_backend_and_no_db_writes(self):
        project = self.add_project()
        transient = self.add_instance(project, port=4310, state="stopped")
        unit_project = self.add_project(name="sheron", path="/tmp/sheron", kind="unit",
                                        start_cmd="sheron-dev.service", base_port=3300)
        unit_instance = self.add_instance(unit_project, port=3300)
        compose_project = self.add_project(name="shop", path="/tmp/shop", kind="compose",
                                           start_cmd=None, base_port=8080)
        compose_instance = self.add_instance(compose_project, port=8080)

        stats = mock.Mock(return_value={
            "portboard-demo-main.service": {
                "ActiveState": "active", "SubState": "running", "MainPID": "1234",
                "MemoryCurrent": "10485760", "CPUUsageNSec": "5000000000", "NRestarts": "2"},
            "sheron-dev.service": {"ActiveState": "failed", "MainPID": "0"},
        })
        containers = mock.Mock(return_value=[Container("shop-web-1", "shop", pid=999)])
        info = self.sysinfo(unit_cgroup_stats=stats, docker_containers=containers)

        before = dict(self.conn.execute("SELECT * FROM instances WHERE id = ?",
                                        (transient["id"],)).fetchone())
        result = runner.status_many(self.conn)

        self.assertEqual(stats.call_count, 1)
        self.assertEqual(containers.call_count, 1)
        self.assertEqual(sorted(stats.call_args[0][0]),
                         ["portboard-demo-main.service", "sheron-dev.service"])
        info.listening_ports.assert_not_called()

        self.assertEqual(result[transient["id"]]["state"], "running")
        self.assertEqual(result[transient["id"]]["pid"], 1234)
        self.assertEqual(result[transient["id"]]["mem_bytes"], 10485760)
        self.assertEqual(result[transient["id"]]["cpu_ns"], 5000000000)
        self.assertEqual(result[transient["id"]]["restarts"], 2)
        self.assertEqual(result[transient["id"]]["unit_active"], "active")
        self.assertEqual(result[unit_instance["id"]]["state"], "failed")
        self.assertEqual(result[compose_instance["id"]]["state"], "running")
        self.assertEqual(result[compose_instance["id"]]["pid"], 999)

        after = dict(self.conn.execute("SELECT * FROM instances WHERE id = ?",
                                       (transient["id"],)).fetchone())
        self.assertEqual(before, after)

    def test_filters_by_ids_and_reports_missing_unit_as_stopped(self):
        project = self.add_project()
        first = self.add_instance(project, port=4310, state="running")
        self.add_instance(project, label="w1", slot=1, port=4311)
        self.sysinfo(unit_cgroup_stats=mock.Mock(return_value={}))
        result = runner.status_many(self.conn, [first["id"]])
        self.assertEqual(list(result), [first["id"]])
        self.assertEqual(result[first["id"]]["state"], "stopped")

    def test_no_docker_call_without_compose_instances(self):
        project = self.add_project()
        self.add_instance(project, port=4310)
        info = self.sysinfo()
        runner.status_many(self.conn)
        info.docker_containers.assert_not_called()


class TestLogs(RunnerTestCase):
    def test_journalctl_for_transient(self):
        project = self.add_project()
        instance = self.add_instance(project)
        fake = FakeRun(default=(0, "log line", ""))
        with mock.patch("portboard.runner.subprocess.run", fake):
            text = runner.logs(self.conn, instance["id"], lines=50)
        self.assertEqual(fake.calls[0]["argv"],
                         ["journalctl", "--user", "-u", "portboard-demo-main.service",
                          "-n", "50", "--no-pager", "-o", "short-iso"])
        self.assertEqual(fake.calls[0]["timeout"], runner.LOG_TIMEOUT)
        self.assertEqual(text, "log line")

    def test_docker_compose_logs(self):
        project = self.add_project(name="shop", path="/tmp/shop", kind="compose",
                                   start_cmd=None, base_port=8080)
        instance = self.add_instance(project, port=8080)
        fake = FakeRun(default=(0, "web-1 | up", ""))
        with mock.patch("portboard.runner.subprocess.run", fake):
            text = runner.logs(self.conn, instance["id"], lines=20)
        self.assertEqual(fake.calls[0]["argv"],
                         ["docker", "compose", "--project-directory", "/tmp/shop",
                          "logs", "--tail", "20", "--no-color"])
        self.assertEqual(text, "web-1 | up")


if __name__ == "__main__":
    unittest.main()
