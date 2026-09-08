"""Tests for portboard.mcp (JSON-RPC handler, tool implementations, transports).

registry / runner / reconcile / discover are written by other agents, so they
are replaced by fake modules in sys.modules; mcp.py imports them lazily with
importlib, which honours sys.modules.
"""
from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import types
import unittest
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="portboard-test-mcp-")
os.environ["PORTBOARD_STATE_DIR"] = _STATE_DIR

from portboard import config, db, mcp  # noqa: E402

logging.getLogger("portboard").addHandler(logging.NullHandler())
logging.getLogger("portboard").propagate = False


class RegistryError(Exception):
    pass


class RunnerError(Exception):
    pass


class Resolved:
    """Stand-in for registry.Resolved."""

    def __init__(self, project=None, instance=None, label=None, is_worktree=False, slug=None, path=None):
        self.project = project
        self.instance = instance
        self.label = label
        self.is_worktree = is_worktree
        self.slug = slug
        self.path = path


PROJECT = {"id": 1, "name": "demo", "path": "/repo/demo", "kind": "transient",
           "base_port": 4000, "open_path": "/"}
MAIN = {"id": 5, "project_id": 1, "label": "main", "slot": 0, "path": "/repo/demo",
        "port": 4000, "actual_port": None, "state": "stopped", "owner_session": None}
WORKTREE = {"id": 6, "project_id": 1, "label": "fix-login", "slot": 1,
            "path": "/repo/demo/.claude/worktrees/fix-login", "port": 4001,
            "actual_port": 4001, "state": "running", "owner_session": "sess-1"}


def make_fake_modules():
    registry = types.ModuleType("portboard.registry")
    registry.RegistryError = RegistryError
    registry.Resolved = Resolved
    registry.resolve_path = mock.MagicMock(return_value=None)
    registry.get_project = mock.MagicMock(return_value=PROJECT)
    registry.get_instance = mock.MagicMock(return_value=MAIN)
    registry.list_instances = mock.MagicMock(return_value=[MAIN, WORKTREE])
    registry.add_project = mock.MagicMock(return_value=PROJECT)
    registry.ensure_instance = mock.MagicMock(return_value=MAIN)
    registry.set_owner = mock.MagicMock(return_value=None)

    runner = types.ModuleType("portboard.runner")
    runner.RunnerError = RunnerError
    runner.start = mock.MagicMock(return_value=dict(MAIN, state="running", actual_port=4000))
    runner.stop = mock.MagicMock(return_value=dict(MAIN, state="stopped"))
    runner.logs = mock.MagicMock(return_value="\n".join(f"line {i}" for i in range(30)))

    reconcile = types.ModuleType("portboard.reconcile")
    reconcile.reconcile = mock.MagicMock(return_value={"listeners": 3, "matched": 1, "unknown": 2})

    discover = types.ModuleType("portboard.discover")
    discover.looks_like_project = mock.MagicMock(return_value=True)
    discover.suggest = mock.MagicMock(return_value={
        "name": "demo", "kind": "transient", "start_cmd": "npm run dev",
        "port_mode": "env", "base_port": None, "confidence": 0.9, "evidence": ["package.json"],
    })

    return {
        "portboard.registry": registry,
        "portboard.runner": runner,
        "portboard.reconcile": reconcile,
        "portboard.discover": discover,
    }


class FakeModuleTestCase(unittest.TestCase):
    def setUp(self):
        self.fakes = make_fake_modules()
        self.registry = self.fakes["portboard.registry"]
        self.runner = self.fakes["portboard.runner"]
        self.reconcile = self.fakes["portboard.reconcile"]
        self.discover = self.fakes["portboard.discover"]
        patcher = mock.patch.dict("sys.modules", self.fakes)
        patcher.start()
        self.addCleanup(patcher.stop)


# --------------------------------------------------------------------------
# tool declarations
# --------------------------------------------------------------------------

class ToolDeclarationTest(unittest.TestCase):
    EXPECTED = {"ports_list", "port_whois", "project_status", "project_claim",
                "instance_start", "instance_stop", "reconcile", "project_register"}

    def test_eight_tools_matching_the_design(self):
        self.assertEqual(len(mcp.TOOLS), 8)
        self.assertEqual({t["name"] for t in mcp.TOOLS}, self.EXPECTED)
        self.assertEqual(set(mcp.TOOL_IMPLS), self.EXPECTED)

    def test_schemas_are_valid_json_schema_objects(self):
        for tool in mcp.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["description"].strip())
                schema = tool["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema["additionalProperties"], False)
                self.assertIsInstance(schema["properties"], dict)
                for name, prop in schema["properties"].items():
                    self.assertIn("type", prop, name)
                    self.assertIn("description", prop, name)
                for required in schema["required"]:
                    self.assertIn(required, schema["properties"])
                json.dumps(tool)  # must be serializable

    def test_required_parameters_follow_the_design(self):
        required = {t["name"]: set(t["inputSchema"]["required"]) for t in mcp.TOOLS}
        self.assertEqual(required["port_whois"], {"port"})
        self.assertEqual(required["project_status"], {"cwd"})
        self.assertEqual(required["project_claim"], {"cwd"})
        self.assertEqual(required["project_register"], {"path"})
        self.assertEqual(required["ports_list"], set())
        self.assertEqual(required["instance_start"], set())

    def test_descriptions_mention_absolute_cwd_and_the_start_wait(self):
        by_name = {t["name"]: t for t in mcp.TOOLS}
        for name in ("project_status", "project_claim", "instance_start", "instance_stop"):
            text = by_name[name]["inputSchema"]["properties"]["cwd"]["description"].lower()
            self.assertIn("absolute", text, name)
        start = by_name["instance_start"]["description"].lower()
        self.assertIn("wait", start)
        self.assertIn("url", start)
        self.assertIn("absolute", by_name["project_register"]["inputSchema"]["properties"]["path"]["description"].lower())


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------

class JsonRpcTest(unittest.TestCase):
    def test_initialize_echoes_a_supported_protocol(self):
        for version in mcp.SUPPORTED_PROTOCOLS:
            resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": version}})
            self.assertEqual(resp["result"]["protocolVersion"], version)

    def test_initialize_falls_back_for_unknown_protocol(self):
        resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(resp["result"]["protocolVersion"], mcp.DEFAULT_PROTOCOL)
        resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(resp["result"]["protocolVersion"], mcp.DEFAULT_PROTOCOL)

    def test_initialize_announces_tools_capability_and_server_info(self):
        resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["result"]["capabilities"], {"tools": {}})
        self.assertEqual(resp["result"]["serverInfo"], {"name": "portboard", "version": config.VERSION})
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)

    def test_notifications_return_none(self):
        self.assertIsNone(mcp.handle_jsonrpc({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(mcp.handle_jsonrpc({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                              "params": {"requestId": 1}}))
        self.assertIsNone(mcp.handle_jsonrpc({"jsonrpc": "2.0", "method": "ping"}))

    def test_ping(self):
        resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(resp["result"], {})

    def test_tools_list(self):
        resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(resp["result"]["tools"]), 8)

    def test_unknown_method(self):
        resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        self.assertEqual(resp["error"]["code"], mcp.METHOD_NOT_FOUND)
        self.assertEqual(resp["id"], 3)

    def test_invalid_request(self):
        resp = mcp.handle_jsonrpc({"id": 4, "method": "ping"})
        self.assertEqual(resp["error"]["code"], mcp.INVALID_REQUEST)
        resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 4})
        self.assertEqual(resp["error"]["code"], mcp.INVALID_REQUEST)
        self.assertEqual(mcp.handle_jsonrpc(["not", "an", "object"])["error"]["code"], mcp.INVALID_REQUEST)

    def test_invalid_params(self):
        resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {}})
        self.assertEqual(resp["error"]["code"], mcp.INVALID_PARAMS)
        resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                   "params": {"name": "ports_list", "arguments": "nope"}})
        self.assertEqual(resp["error"]["code"], mcp.INVALID_PARAMS)
        resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 5, "method": "ping", "params": []})
        self.assertEqual(resp["error"]["code"], mcp.INVALID_PARAMS)

    def test_internal_error_is_reported(self):
        with mock.patch.object(mcp, "call_tool", side_effect=RuntimeError("boom")):
            resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                       "params": {"name": "ports_list"}})
        self.assertEqual(resp["error"]["code"], mcp.INTERNAL_ERROR)

    def test_tools_call_dispatches(self):
        result = {"content": [{"type": "text", "text": "{}"}], "isError": False}
        with mock.patch.object(mcp, "call_tool", return_value=result) as call_tool:
            resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                       "params": {"name": "reconcile", "arguments": {"quick": True}}})
        call_tool.assert_called_once_with("reconcile", {"quick": True})
        self.assertEqual(resp["result"], result)


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

class HttpPostTest(unittest.TestCase):
    def test_request_gets_200_json(self):
        status, headers, body = mcp.http_post(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(), {})
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(json.loads(body)["result"], {})

    def test_notification_gets_202_empty(self):
        status, headers, body = mcp.http_post(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(), {})
        self.assertEqual(status, 202)
        self.assertEqual(body, b"")
        self.assertEqual(headers, {})

    def test_parse_error(self):
        status, _, body = mcp.http_post(b"{ not json", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"]["code"], mcp.PARSE_ERROR)

    def test_empty_body_is_invalid_request(self):
        status, _, body = mcp.http_post(b"", {})
        self.assertEqual(json.loads(body)["error"]["code"], mcp.INVALID_REQUEST)

    def test_batch_is_rejected(self):
        status, _, body = mcp.http_post(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).encode(), {})
        self.assertEqual(json.loads(body)["error"]["code"], mcp.INVALID_REQUEST)

    def test_no_session_header_required(self):
        status, _, _ = mcp.http_post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(), {})
        self.assertEqual(status, 200)


class StdioTest(unittest.TestCase):
    def test_stdio_roundtrip(self):
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05"}}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        )
        stdout = io.StringIO()
        self.assertEqual(mcp.stdio_main(stdin, stdout), 0)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)  # the notification and the blank line produce nothing
        self.assertEqual(lines[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(len(lines[1]["result"]["tools"]), 8)

    def test_stdio_reports_parse_errors_and_keeps_going(self):
        stdin = io.StringIO("{ broken\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        stdout = io.StringIO()
        mcp.stdio_main(stdin, stdout)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(lines[0]["error"]["code"], mcp.PARSE_ERROR)
        self.assertEqual(lines[1]["result"], {})

    def test_stdio_returns_zero_on_empty_input(self):
        self.assertEqual(mcp.stdio_main(io.StringIO(""), io.StringIO()), 0)


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def payload_of(result: dict):
    return json.loads(result["content"][0]["text"])


class CallToolTest(FakeModuleTestCase):
    def test_unknown_tool_is_error_result_not_exception(self):
        result = mcp.call_tool("does_not_exist", {})
        self.assertTrue(result["isError"])
        self.assertIn("unknown tool", payload_of(result)["error"])
        self.assertEqual(result["content"][0]["type"], "text")

    def test_result_envelope(self):
        result = mcp.call_tool("reconcile", {"quick": True})
        self.assertFalse(result["isError"])
        self.assertEqual(payload_of(result), {"listeners": 3, "matched": 1, "unknown": 2})
        _, kwargs = self.reconcile.reconcile.call_args
        self.assertTrue(kwargs["quick"])

    def test_ports_list_reconciles_quickly_first(self):
        conn = db.connect()
        try:
            conn.execute("DELETE FROM observed")
            conn.execute("DELETE FROM reserved_ports")
            conn.execute("DELETE FROM projects")
            now = db.now()
            conn.execute("INSERT INTO projects(id, name, path, created_at, updated_at) "
                         "VALUES (1, 'demo', '/repo/demo', ?, ?)", (now, now))
            conn.execute("INSERT INTO observed(port, proto, bind, pid, comm, unit, project_id, seen_at) "
                         "VALUES (4000, 'tcp', '127.0.0.1', 42, 'node', 'x.service', 1, ?)", (now,))
            conn.execute("INSERT INTO reserved_ports(port, label, source, updated_at) "
                         "VALUES (1234, 'lmstudio', 'observed', ?)", (now,))
        finally:
            conn.close()

        result = mcp.call_tool("ports_list", {})
        self.assertFalse(result["isError"])
        data = payload_of(result)
        _, kwargs = self.reconcile.reconcile.call_args
        self.assertTrue(kwargs["quick"])
        self.assertEqual({i["id"] for i in data["instances"]}, {5, 6})
        compact = data["instances"][0]
        self.assertEqual(set(compact), {"id", "project", "label", "port", "actual_port",
                                        "state", "url", "owner_session", "worktree"})
        self.assertEqual(compact["url"], "http://localhost:4000/")
        self.assertFalse(compact["worktree"])
        self.assertTrue(data["instances"][1]["worktree"])
        self.assertEqual(data["observed"][0]["port"], 4000)
        self.assertEqual(data["observed"][0]["project"], "demo")
        self.assertEqual(data["reserved"], [{"port": 1234, "label": "lmstudio"}])

    def test_port_whois_free_port(self):
        conn = db.connect()
        try:
            conn.execute("DELETE FROM observed")
        finally:
            conn.close()
        data = payload_of(mcp.call_tool("port_whois", {"port": 4999}))
        self.assertEqual(data, {"port": 4999, "free": True})

    def test_port_whois_taken_port(self):
        conn = db.connect()
        try:
            conn.execute("DELETE FROM observed")
            conn.execute("INSERT INTO observed(port, proto, bind, pid, comm, project_id, instance_id, seen_at) "
                         "VALUES (4000, 'tcp', '*', 42, 'node', 1, 5, ?)", (db.now(),))
        finally:
            conn.close()
        data = payload_of(mcp.call_tool("port_whois", {"port": 4000}))
        self.assertFalse(data["free"])
        self.assertEqual(data["observed"]["comm"], "node")
        self.assertEqual(data["project"]["name"], "demo")
        self.assertEqual(data["instance"]["id"], 5)

    def test_port_whois_requires_an_integer(self):
        result = mcp.call_tool("port_whois", {"port": "4000"})
        self.assertTrue(result["isError"])

    def test_project_status_unregistered(self):
        self.registry.resolve_path.return_value = None
        data = payload_of(mcp.call_tool("project_status", {"cwd": "/tmp/not-a-project"}))
        self.assertFalse(data["registered"])
        self.assertEqual(data["hint"], "call project_claim to register")

    def test_project_status_registered(self):
        self.registry.resolve_path.return_value = Resolved(PROJECT, WORKTREE, "fix-login", True, "fix-login")
        data = payload_of(mcp.call_tool("project_status", {"cwd": "/repo/demo/.claude/worktrees/fix-login"}))
        self.assertTrue(data["registered"])
        self.assertEqual(data["project"]["name"], "demo")
        self.assertEqual(data["this"]["id"], 6)
        self.assertEqual(data["assigned_port"], 4001)
        self.assertEqual(data["url"], "http://localhost:4001/")
        self.assertEqual([i["id"] for i in data["running"]], [6])

    def test_relative_cwd_is_rejected(self):
        result = mcp.call_tool("project_status", {"cwd": "relative/path"})
        self.assertTrue(result["isError"])
        self.assertIn("absolute", payload_of(result)["error"])

    def test_project_claim_registers_unknown_repo(self):
        self.registry.resolve_path.side_effect = [None, Resolved(PROJECT, MAIN, "main")]
        with mock.patch.object(mcp, "_git_root", return_value="/repo/demo"):
            data = payload_of(mcp.call_tool("project_claim", {"cwd": "/repo/demo", "session_id": "sess-9"}))
        self.discover.looks_like_project.assert_called_once_with("/repo/demo")
        _, kwargs = self.registry.add_project.call_args
        self.assertEqual(kwargs["source"], "mcp")
        self.assertNotIn("confidence", kwargs)
        self.assertNotIn("evidence", kwargs)
        self.assertEqual(kwargs["start_cmd"], "npm run dev")
        self.registry.set_owner.assert_called_once_with(mock.ANY, 5, "sess-9")
        self.assertEqual(data["project"]["name"], "demo")
        self.assertEqual(data["port"], 4000)
        self.assertEqual(data["url"], "http://localhost:4000/")
        self.assertIn("instance_start", data["start_hint"])

    def test_project_claim_on_a_non_project_is_an_error(self):
        self.registry.resolve_path.return_value = None
        self.discover.looks_like_project.return_value = False
        with mock.patch.object(mcp, "_git_root", return_value=None):
            result = mcp.call_tool("project_claim", {"cwd": "/tmp/somewhere"})
        self.assertTrue(result["isError"])
        self.assertIn("not a recognizable project", payload_of(result)["error"])
        self.registry.add_project.assert_not_called()

    def test_project_claim_creates_the_instance_when_missing(self):
        self.registry.resolve_path.return_value = Resolved(PROJECT, None, "fix-login")
        data = payload_of(mcp.call_tool("project_claim", {"cwd": "/repo/demo/.claude/worktrees/fix-login"}))
        args, kwargs = self.registry.ensure_instance.call_args
        self.assertEqual(args[1], 1)
        self.assertEqual(kwargs["label"], "fix-login")
        self.assertEqual(kwargs["source"], "mcp")
        self.registry.add_project.assert_not_called()
        self.assertEqual(data["instance"]["id"], 5)

    def test_instance_start_by_cwd(self):
        self.registry.resolve_path.return_value = Resolved(PROJECT, MAIN, "main")
        self.registry.get_instance.return_value = dict(MAIN, state="running", actual_port=4000)
        data = payload_of(mcp.call_tool("instance_start", {"cwd": "/repo/demo", "session_id": "s1"}))
        self.runner.start.assert_called_once_with(mock.ANY, 5, wait=True)
        self.registry.set_owner.assert_called_once_with(mock.ANY, 5, "s1")
        self.assertEqual(data["state"], "running")
        self.assertEqual(data["url"], "http://localhost:4000/")
        self.assertEqual(data["project"], "demo")

    def test_instance_start_by_id(self):
        payload_of(mcp.call_tool("instance_start", {"instance_id": 5}))
        self.registry.resolve_path.assert_not_called()
        self.runner.start.assert_called_once()

    def test_instance_start_failure_carries_a_log_tail(self):
        self.registry.resolve_path.return_value = Resolved(PROJECT, MAIN, "main")
        self.runner.start.side_effect = RunnerError("unit failed")
        result = mcp.call_tool("instance_start", {"cwd": "/repo/demo"})
        self.assertTrue(result["isError"])
        data = payload_of(result)
        self.assertEqual(data["error"], "unit failed")
        self.assertEqual(len(data["logs_tail"]), 10)
        self.assertEqual(data["logs_tail"][-1], "line 29")

    def test_instance_start_on_unregistered_cwd_is_an_error(self):
        self.registry.resolve_path.return_value = None
        result = mcp.call_tool("instance_start", {"cwd": "/tmp/elsewhere"})
        self.assertTrue(result["isError"])
        self.assertIn("project_claim", payload_of(result)["error"])

    def test_instance_stop(self):
        self.registry.resolve_path.return_value = Resolved(PROJECT, MAIN, "main")
        self.registry.get_instance.return_value = dict(MAIN, state="stopped")
        data = payload_of(mcp.call_tool("instance_stop", {"cwd": "/repo/demo"}))
        self.runner.stop.assert_called_once_with(mock.ANY, 5, reason="mcp")
        self.assertEqual(data["state"], "stopped")

    def test_instance_stop_never_creates_an_instance(self):
        self.registry.resolve_path.return_value = Resolved(PROJECT, None, "main")
        result = mcp.call_tool("instance_stop", {"cwd": "/repo/demo"})
        self.assertTrue(result["isError"])
        self.registry.ensure_instance.assert_not_called()

    def test_project_register_merges_discovery_with_explicit_fields(self):
        data = payload_of(mcp.call_tool("project_register", {"path": "/repo/demo", "base_port": 3300}))
        _, kwargs = self.registry.add_project.call_args
        self.assertEqual(kwargs["source"], "mcp")
        self.assertEqual(kwargs["base_port"], 3300)
        self.assertEqual(kwargs["start_cmd"], "npm run dev")
        self.assertNotIn("confidence", kwargs)
        self.assertEqual(data["name"], "demo")

    def test_project_register_survives_discovery_failure(self):
        self.discover.suggest.side_effect = OSError("permission denied")
        result = mcp.call_tool("project_register", {"path": "/repo/demo", "kind": "compose"})
        self.assertFalse(result["isError"])
        _, kwargs = self.registry.add_project.call_args
        self.assertEqual(kwargs["kind"], "compose")

    def test_registry_errors_become_error_results(self):
        self.registry.add_project.side_effect = RegistryError("path already registered")
        result = mcp.call_tool("project_register", {"path": "/repo/demo"})
        self.assertTrue(result["isError"])
        self.assertIn("path already registered", payload_of(result)["error"])


class GitRootTest(unittest.TestCase):
    def test_finds_the_enclosing_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            os.mkdir(os.path.join(root, ".git"))
            deep = os.path.join(root, "a", "b")
            os.makedirs(deep)
            self.assertEqual(mcp._git_root(deep), root)

    def test_returns_none_outside_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(mcp._git_root(os.path.join(tmp)))


if __name__ == "__main__":
    unittest.main()
