"""Tests for portboard.server.

The registry/runner/reconcile/discover/schedule modules are written by other
agents, so they are replaced by fake modules in sys.modules: the server imports
them lazily with importlib, which honours sys.modules.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="portboard-test-server-")
os.environ["PORTBOARD_STATE_DIR"] = _STATE_DIR

from portboard import config, db, server  # noqa: E402

from tests import assert_isolated  # noqa: E402

# Keep unittest output clean: the server logs handled errors on purpose.
logging.getLogger("portboard").addHandler(logging.NullHandler())
logging.getLogger("portboard").propagate = False


class RegistryError(Exception):
    pass


class RunnerError(Exception):
    pass


def make_fake_modules() -> dict[str, types.ModuleType]:
    """Stand-ins for the modules server.py calls, all mocks except the errors."""
    registry = types.ModuleType("portboard.registry")
    registry.RegistryError = RegistryError
    registry.state_snapshot = mock.MagicMock(return_value={
        "projects": [], "observed": [], "reserved": [], "settings": {}, "reconciled_at": None,
    })
    registry.get_project = mock.MagicMock(return_value=None)
    registry.reorder_projects = mock.MagicMock(return_value=[])
    registry.add_project = mock.MagicMock(return_value={"id": 1, "name": "demo"})
    registry.update_project = mock.MagicMock(return_value={"id": 1, "name": "demo"})
    registry.delete_project = mock.MagicMock(return_value=None)
    registry.list_instances = mock.MagicMock(return_value=[])
    registry.get_instance = mock.MagicMock(return_value=None)
    registry.ensure_instance = mock.MagicMock(return_value={"id": 7, "label": "main"})
    registry.update_instance = mock.MagicMock(return_value={"id": 7, "label": "main"})
    registry.delete_instance = mock.MagicMock(return_value=None)
    registry.release_owner = mock.MagicMock(return_value=None)

    runner = types.ModuleType("portboard.runner")
    runner.RunnerError = RunnerError
    runner.start = mock.MagicMock(return_value={"id": 7, "state": "running"})
    runner.stop = mock.MagicMock(return_value={"id": 7, "state": "stopped"})
    runner.restart = mock.MagicMock(return_value={"id": 7, "state": "running"})
    runner.logs = mock.MagicMock(return_value="log line\n")

    reconcile = types.ModuleType("portboard.reconcile")
    reconcile.reconcile = mock.MagicMock(return_value={"listeners": 0, "matched": 0, "unknown": 0})

    discover = types.ModuleType("portboard.discover")
    discover.suggest = mock.MagicMock(return_value={"name": "demo", "kind": "transient"})
    discover.looks_like_project = mock.MagicMock(return_value=True)

    schedule = types.ModuleType("portboard.schedule")
    schedule.evening_stop = mock.MagicMock(return_value={"stopped": [1], "started": [], "errors": []})
    schedule.morning_start = mock.MagicMock(return_value={"stopped": [], "started": [1], "errors": []})

    return {
        "portboard.registry": registry,
        "portboard.runner": runner,
        "portboard.reconcile": reconcile,
        "portboard.discover": discover,
        "portboard.schedule": schedule,
    }


class ShouldExitTest(unittest.TestCase):
    def test_never_exits_when_not_socket_activated(self):
        self.assertFalse(server.should_exit(10_000.0, 0.0, 0, 600, False))

    def test_exits_after_idle_window(self):
        self.assertTrue(server.should_exit(700.0, 0.0, 0, 600, True))

    def test_stays_inside_idle_window(self):
        self.assertFalse(server.should_exit(500.0, 0.0, 0, 600, True))

    def test_inflight_request_blocks_exit(self):
        self.assertFalse(server.should_exit(10_000.0, 0.0, 1, 600, True))

    def test_zero_or_none_disables(self):
        self.assertFalse(server.should_exit(10_000.0, 0.0, 0, 0, True))
        self.assertFalse(server.should_exit(10_000.0, 0.0, 0, None, True))


class HostGuardUnitTest(unittest.TestCase):
    def test_allowed(self):
        for host in ("localhost", "localhost:8790", "127.0.0.1:8790", "[::1]:8790", None, ""):
            self.assertTrue(server.host_allowed(host), host)

    def test_rejected(self):
        for host in ("evil.example", "portboard.attacker.test:8790", "10.0.0.5:8790"):
            self.assertFalse(server.host_allowed(host), host)


class ServerTestCase(unittest.TestCase):
    """Base: a real server on an ephemeral port with the fake modules patched in."""

    def setUp(self):
        self.fakes = make_fake_modules()
        self.registry = self.fakes["portboard.registry"]
        self.runner = self.fakes["portboard.runner"]
        self.reconcile = self.fakes["portboard.reconcile"]
        self.discover = self.fakes["portboard.discover"]
        self.schedule = self.fakes["portboard.schedule"]
        self.modpatch = mock.patch.dict("sys.modules", self.fakes)
        self.modpatch.start()
        self.addCleanup(self.modpatch.stop)

        self.srv = server.make_server("127.0.0.1", 0)
        self.srv.idle_exit_seconds = 600
        self.port = self.srv.server_port
        import threading
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.stopping = True
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)

    def req(self, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, dict(exc.headers), exc.read()

    def json_req(self, method, path, body=None, headers=None):
        status, hdrs, raw = self.req(method, path, body, headers)
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return status, hdrs, payload


class HealthAndStateTest(ServerTestCase):
    def test_healthz(self):
        status, headers, payload = self.json_req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], config.VERSION)
        self.assertEqual(payload["pid"], os.getpid())
        self.assertFalse(payload["socket_activated"])
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_state_shape(self):
        self.registry.state_snapshot.return_value = {
            "projects": [{"id": 1, "name": "demo", "instances": []}],
            "observed": [], "reserved": [], "settings": {"pool_start": "4000"},
            "reconciled_at": "2026-09-08T10:00:00",
        }
        status, _, payload = self.json_req("GET", "/api/state")
        self.assertEqual(status, 200)
        for key in ("projects", "observed", "reserved", "settings", "reconciled_at", "daemon", "projects_root"):
            self.assertIn(key, payload)
        self.assertEqual(payload["projects_root"], str(config.PROJECTS_ROOT))
        daemon = payload["daemon"]
        self.assertEqual(set(daemon), {"pid", "version", "socket_activated", "uptime_s", "idle_exit_in_s"})
        self.assertIsNone(daemon["idle_exit_in_s"])  # not socket activated
        self.registry.state_snapshot.assert_called()

    def test_reconcile_returns_state_plus_summary(self):
        status, _, payload = self.json_req("POST", "/api/reconcile", {"quick": True, "adopt_unknown": True})
        self.assertEqual(status, 200)
        self.assertIn("summary", payload)
        self.assertIn("daemon", payload)
        self.reconcile.reconcile.assert_called_once()
        _, kwargs = self.reconcile.reconcile.call_args
        self.assertTrue(kwargs["quick"])
        self.assertTrue(kwargs["adopt_unknown"])

    def test_project_order_calls_registry_and_returns_state(self):
        status, _, payload = self.json_req("POST", "/api/projects/order", {"ids": [3, 1, 2]})
        self.assertEqual(status, 200)
        self.assertIn("projects", payload)
        args, _ = self.registry.reorder_projects.call_args
        self.assertEqual(args[1], [3, 1, 2])

    def test_project_order_rejects_non_list(self):
        status, _, payload = self.json_req("POST", "/api/projects/order", {"ids": "3,1,2"})
        self.assertEqual(status, 400)
        self.assertIn("ids", payload["error"])
        self.registry.reorder_projects.assert_not_called()

    def test_state_quick_reconciles_a_stale_snapshot(self):
        self.registry.state_snapshot.return_value["reconciled_at"] = "2026-09-08T10:00:00"
        status, _, _ = self.json_req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.reconcile.reconcile.assert_called_once()
        _, kwargs = self.reconcile.reconcile.call_args
        self.assertTrue(kwargs["quick"])

    def test_state_skips_reconcile_when_snapshot_is_fresh(self):
        from datetime import datetime
        conn = db.connect()
        try:
            db.set_setting(conn, "reconciled_at", datetime.now().replace(microsecond=0).isoformat())
        finally:
            conn.close()
        status, _, _ = self.json_req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.reconcile.reconcile.assert_not_called()

    def test_state_survives_a_failing_quick_reconcile(self):
        self.reconcile.reconcile.side_effect = RuntimeError("ss exploded")
        status, _, payload = self.json_req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("projects", payload)

    def test_project_add_reconciles_quickly(self):
        status, _, payload = self.json_req("POST", "/api/projects", {"path": "/repo/new"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["name"], "demo")
        self.reconcile.reconcile.assert_called_once()
        self.assertTrue(self.reconcile.reconcile.call_args.kwargs["quick"])

    def test_daemon_reports_idle_countdown_when_socket_activated(self):
        self.srv.socket_activated = True
        _, _, payload = self.json_req("GET", "/api/state")
        self.assertIsInstance(payload["daemon"]["idle_exit_in_s"], (int, float))
        self.srv.socket_activated = False


class ErrorMappingTest(ServerTestCase):
    def test_unknown_endpoint_is_json_404(self):
        status, headers, payload = self.json_req("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_registry_error_is_400(self):
        self.registry.add_project.side_effect = RegistryError("path is not a directory")
        status, _, payload = self.json_req("POST", "/api/projects", {"path": "/nope"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "path is not a directory")

    def test_runner_error_is_400(self):
        self.registry.get_instance.return_value = {"id": 7, "label": "main", "slot": 0}
        self.runner.start.side_effect = RunnerError("no start command")
        status, _, payload = self.json_req("POST", "/api/instances/7/start")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "no start command")

    def test_missing_row_is_404(self):
        self.registry.get_instance.return_value = None
        status, _, payload = self.json_req("POST", "/api/instances/99/stop")
        self.assertEqual(status, 404)
        self.assertIn("99", payload["error"])

    def test_unexpected_error_is_500(self):
        self.registry.state_snapshot.side_effect = RuntimeError("boom")
        status, _, payload = self.json_req("GET", "/api/state")
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "boom")

    def test_value_error_is_400(self):
        status, _, payload = self.json_req("POST", "/api/projects", {})
        self.assertEqual(status, 400)
        self.assertIn("path", payload["error"])

    def test_wrong_method_is_405(self):
        status, _, payload = self.json_req("GET", "/api/reconcile")
        self.assertEqual(status, 405)
        self.assertIn("error", payload)

    def test_invalid_json_body_is_400(self):
        url = f"http://127.0.0.1:{self.port}/api/settings"
        request = urllib.request.Request(url, data=b"{not json", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            with exc:
                status = exc.code
        self.assertEqual(status, 400)


class HostGuardTest(ServerTestCase):
    def test_foreign_host_header_is_403(self):
        status, _, payload = self.json_req("GET", "/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("Host", payload["error"])

    def test_healthz_is_exempt(self):
        status, _, payload = self.json_req("GET", "/healthz", headers={"Host": "evil.example"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_localhost_host_header_passes(self):
        status, _, _ = self.json_req("GET", "/api/state", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)


class StaticTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.static = tempfile.mkdtemp(prefix="portboard-static-")
        patcher = mock.patch.object(server, "STATIC_DIR", __import__("pathlib").Path(self.static))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_index_served_from_disk(self):
        with open(os.path.join(self.static, "index.html"), "w", encoding="utf-8") as fh:
            fh.write("<h1>portboard</h1>")
        status, headers, raw = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"portboard", raw)

    def test_index_reread_every_request(self):
        path = os.path.join(self.static, "index.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("one")
        self.assertIn(b"one", self.req("GET", "/")[2])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("two")
        self.assertIn(b"two", self.req("GET", "/")[2])

    def test_missing_index_is_404(self):
        status, _, payload = self.json_req("GET", "/")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_static_css(self):
        with open(os.path.join(self.static, "app.css"), "w", encoding="utf-8") as fh:
            fh.write("body{}")
        status, headers, raw = self.req("GET", "/static/app.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(raw, b"body{}")

    def test_static_rejects_other_extensions(self):
        with open(os.path.join(self.static, "secrets.txt"), "w", encoding="utf-8") as fh:
            fh.write("nope")
        status, _, _ = self.json_req("GET", "/static/secrets.txt")
        self.assertEqual(status, 404)

    def test_static_rejects_traversal(self):
        status, _, _ = self.json_req("GET", "/static/..%2f..%2fetc%2fpasswd")
        self.assertEqual(status, 404)


class ApiActionsTest(ServerTestCase):
    def test_project_crud_paths(self):
        self.registry.add_project.return_value = {"id": 3, "name": "demo"}
        status, _, payload = self.json_req("POST", "/api/projects",
                                           {"path": "/mnt/hyper/Projects/Demo", "name": "demo"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["id"], 3)
        args, kwargs = self.registry.add_project.call_args
        self.assertEqual(args[1], "/mnt/hyper/Projects/Demo")
        self.assertEqual(kwargs["name"], "demo")
        self.assertEqual(kwargs["source"], "gui")

        self.registry.update_project.return_value = {"id": 3, "pinned": 1}
        status, _, payload = self.json_req("PATCH", "/api/projects/3", {"pinned": 1})
        self.assertEqual(status, 200)
        self.assertEqual(payload["pinned"], 1)

        self.registry.get_project.return_value = {"id": 3, "name": "demo", "path": "/p"}
        self.registry.list_instances.return_value = [{"id": 7, "state": "running", "managed": 1}]
        status, _, payload = self.json_req("DELETE", "/api/projects/3")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.runner.stop.assert_called()
        self.registry.delete_project.assert_called_once()

    def test_add_worktree_instance_by_label(self):
        self.registry.get_project.return_value = {"id": 3, "name": "demo", "path": "/repo"}
        self.registry.ensure_instance.return_value = {"id": 9, "label": "fix-login"}
        status, _, payload = self.json_req("POST", "/api/projects/3/instances", {"label": "fix-login"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["id"], 9)
        args, _ = self.registry.ensure_instance.call_args
        self.assertEqual(args[2], os.path.join("/repo", config.WORKTREE_DIRNAME, "fix-login"))

    def test_instance_release_and_delete_rules(self):
        self.registry.get_instance.return_value = {"id": 7, "label": "main", "slot": 0}
        status, _, _ = self.json_req("POST", "/api/instances/7/release")
        self.assertEqual(status, 200)
        self.registry.release_owner.assert_called_once()

        status, _, payload = self.json_req("DELETE", "/api/instances/7")
        self.assertEqual(status, 400)
        self.assertIn("main", payload["error"])

        self.registry.get_instance.return_value = {"id": 8, "label": "wt", "slot": 1, "state": "stopped"}
        status, _, payload = self.json_req("DELETE", "/api/instances/8")
        self.assertEqual(status, 200)
        self.registry.delete_instance.assert_called_once()

    def test_logs(self):
        self.registry.get_instance.return_value = {"id": 7, "label": "main"}
        status, _, payload = self.json_req("GET", "/api/instances/7/logs?lines=25")
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "log line\n")
        _, kwargs = self.runner.logs.call_args
        self.assertEqual(kwargs["lines"], 25)

    def test_schedule_endpoints(self):
        status, _, payload = self.json_req("POST", "/api/schedule/stop")
        self.assertEqual(status, 200)
        self.assertEqual(payload["stopped"], [1])
        status, _, payload = self.json_req("POST", "/api/schedule/start")
        self.assertEqual(status, 200)
        self.assertEqual(payload["started"], [1])

    def test_discover_requires_path(self):
        status, _, payload = self.json_req("GET", "/api/discover")
        self.assertEqual(status, 400)
        status, _, payload = self.json_req("GET", "/api/discover?path=/mnt/hyper/Projects/Demo")
        self.assertEqual(status, 200)
        self.assertEqual(payload["name"], "demo")

    def test_settings_roundtrip_and_validation(self):
        status, _, payload = self.json_req("POST", "/api/settings", {"idle_minutes": "45"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["idle_minutes"], "45")
        status, _, payload = self.json_req("POST", "/api/settings", {"totally_made_up": "1"})
        self.assertEqual(status, 400)
        self.assertIn("totally_made_up", payload["error"])


class DbBackedTest(ServerTestCase):
    """Endpoints that read the database directly, seeded with plain SQL."""

    def setUp(self):
        super().setUp()
        assert_isolated()
        conn = db.connect()
        try:
            conn.execute("DELETE FROM observed")
            conn.execute("DELETE FROM instances")
            conn.execute("DELETE FROM projects")
            conn.execute("DELETE FROM events")
            now = db.now()
            conn.execute(
                "INSERT INTO projects(id, name, path, kind, base_port, created_at, updated_at) "
                "VALUES (1, 'demo', '/repo/demo', 'transient', 4000, ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO instances(id, project_id, label, slot, path, port, state, created_at, updated_at) "
                "VALUES (5, 1, 'main', 0, '/repo/demo', 4000, 'stopped', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO observed(port, proto, bind, pid, comm, cwd, unit, project_id, seen_at) "
                "VALUES (4000, 'tcp', '127.0.0.1', 4242, 'node', '/repo/demo', 'x.service', 1, ?)", (now,))
            db.add_event(conn, "test.seed", {"n": 1})
        finally:
            conn.close()

    def test_events(self):
        status, _, payload = self.json_req("GET", "/api/events?limit=10")
        self.assertEqual(status, 200)
        self.assertTrue(any(e["kind"] == "test.seed" for e in payload))

    def test_observed_adopt(self):
        self.registry.get_project.return_value = {"id": 1, "name": "demo", "path": "/repo/demo"}
        self.registry.ensure_instance.return_value = {"id": 5, "label": "main", "port": None}
        self.registry.update_instance.return_value = {"id": 5, "label": "main", "managed": 0, "state": "running"}
        status, _, payload = self.json_req("POST", "/api/observed/4000/adopt")
        self.assertEqual(status, 200)
        self.assertEqual(payload["managed"], 0)
        _, kwargs = self.registry.update_instance.call_args
        self.assertEqual(kwargs["managed"], 0)
        self.assertEqual(kwargs["state"], "running")
        self.assertEqual(kwargs["pid"], 4242)
        self.assertEqual(kwargs["actual_port"], 4000)
        self.assertEqual(kwargs["unit"], "x.service")

    def test_observed_adopt_unknown_port_is_404(self):
        status, _, payload = self.json_req("POST", "/api/observed/9999/adopt")
        self.assertEqual(status, 404)

    def test_observed_stop_rejects_foreign_pid(self):
        with mock.patch.object(server, "_proc_uid", return_value=os.getuid() + 1):
            status, _, payload = self.json_req("POST", "/api/observed/4000/stop")
        self.assertEqual(status, 400)
        self.assertIn("4242", payload["error"])

    def test_observed_stop_uses_docker_when_container_known(self):
        conn = db.connect()
        try:
            conn.execute("UPDATE observed SET container = 'demo-web-1' WHERE port = 4000")
        finally:
            conn.close()
        completed = mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run:
            status, _, payload = self.json_req("POST", "/api/observed/4000/stop")
        self.assertEqual(status, 200)
        self.assertEqual(payload["stopped"], "demo-web-1")
        self.assertEqual(run.call_args[0][0], ["docker", "stop", "demo-web-1"])


class McpRouteTest(ServerTestCase):
    def _rpc(self, message, expect=200):
        status, headers, raw = self.req("POST", "/mcp", message)
        self.assertEqual(status, expect)
        return headers, (json.loads(raw.decode("utf-8")) if raw else None)

    def test_get_is_405(self):
        status, _, payload = self.json_req("GET", "/mcp")
        self.assertEqual(status, 405)
        self.assertIn("POST", payload["error"])

    def test_initialize_list_and_call(self):
        headers, payload = self._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(payload["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(payload["result"]["serverInfo"]["name"], "portboard")

        _, payload = self._rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in payload["result"]["tools"]]
        self.assertIn("ports_list", names)
        self.assertEqual(len(names), 8)

        from portboard import mcp
        fake = {"content": [{"type": "text", "text": "{}"}], "isError": False}
        with mock.patch.object(mcp, "call_tool", return_value=fake) as call_tool:
            _, payload = self._rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "ports_list", "arguments": {}}})
        call_tool.assert_called_once_with("ports_list", {})
        self.assertEqual(payload["result"], fake)

    def test_notification_gets_202_and_empty_body(self):
        status, _, raw = self.req("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        self.assertEqual(raw, b"")

    def test_unknown_method_is_jsonrpc_error(self):
        _, payload = self._rpc({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        self.assertEqual(payload["error"]["code"], -32601)


class MakeServerTest(unittest.TestCase):
    def test_socket_activation_adopts_the_inherited_fd(self):
        import socket as socket_mod
        listener = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        expected_port = listener.getsockname()[1]
        dup_fd = os.dup(listener.fileno())
        srv = server.make_server("127.0.0.1", 0, socket_fd=dup_fd)
        try:
            self.assertTrue(srv.socket_activated)
            self.assertEqual(srv.server_port, expected_port)
            self.assertEqual(srv.socket.fileno(), dup_fd)
        finally:
            srv.server_close()
            listener.close()

    def test_plain_bind_is_not_socket_activated(self):
        srv = server.make_server("127.0.0.1", 0)
        try:
            self.assertFalse(srv.socket_activated)
            self.assertGreater(srv.server_port, 0)
            self.assertEqual(srv.inflight, 0)
        finally:
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
