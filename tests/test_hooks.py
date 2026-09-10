"""Unit tests for portboard.hooks.

The state directory is redirected to a temporary directory BEFORE portboard is
imported, because config resolves DB_PATH at import time.

hooks.py imports sibling modules lazily (`from . import registry` inside each
function). To intercept that from a test we patch the attribute on the
`portboard` package object (what `IMPORT_FROM` looks up first), not just
sys.modules — patching only sys.modules is a well-known miss once the real
submodule has already been imported and cached as a package attribute.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("PORTBOARD_STATE_DIR", tempfile.mkdtemp(prefix="portboard-test-hooks-"))

import portboard  # noqa: E402
from portboard import config, db, hooks  # noqa: E402

from tests import assert_isolated  # noqa: E402


def _patch_module(name: str, fake) -> contextlib._GeneratorContextManager:
    return mock.patch.object(portboard, name, fake, create=True)


class DetectDevServerTests(unittest.TestCase):
    CASES = [
        ("npm run dev", "npm-dev", None),
        ("pnpm dev", "npm-dev", None),
        ("PORT=3301 npm run dev", "npm-dev", 3301),
        ("npx nuxi dev --port 3302", "nuxt", 3302),
        ("uvicorn app:app --port 8000", "uvicorn", 8000),
        ("docker compose up -d", "compose", None),
        ('echo "npm run dev"', None, None),
        ('git commit -m "vite"', None, None),
    ]

    def test_table(self) -> None:
        for command, expected_kind, expected_port in self.CASES:
            with self.subTest(command=command):
                result = hooks.detect_dev_server(command)
                if expected_kind is None:
                    self.assertIsNone(result, command)
                else:
                    self.assertIsNotNone(result, command)
                    self.assertEqual(result["kind"], expected_kind)
                    self.assertEqual(result["port"], expected_port)


class PreToolUseTests(unittest.TestCase):
    def setUp(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
            if path.exists():
                path.unlink()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def _fake_registry(self, resolved, instances):
        return types.SimpleNamespace(
            resolve_path=mock.Mock(return_value=resolved),
            list_instances=mock.Mock(return_value=instances),
        )

    def test_deny_when_explicit_port_taken(self) -> None:
        resolved = types.SimpleNamespace(project={"id": 1, "name": "sheron"}, instance={"id": 10, "port": 3300})
        other = {"id": 11, "project": "other", "label": "main", "port": 3302, "state": "running"}
        with _patch_module("registry", self._fake_registry(resolved, [other])):
            result = hooks.pre_tool_use(
                self.conn, {"cwd": "/x", "tool_input": {"command": "npm run dev -- --port 3302"}}
            )
        self.assertIsNotNone(result)
        deny = result["hookSpecificOutput"]
        self.assertEqual(deny["permissionDecision"], "deny")
        self.assertEqual(deny["hookEventName"], "PreToolUse")

    def test_allow_when_port_free(self) -> None:
        resolved = types.SimpleNamespace(project={"id": 1, "name": "sheron"}, instance={"id": 10, "port": 3300})
        with _patch_module("registry", self._fake_registry(resolved, [])):
            result = hooks.pre_tool_use(
                self.conn, {"cwd": "/x", "tool_input": {"command": "npm run dev -- --port 3999"}}
            )
        self.assertIsNone(result)

    def test_allow_when_cwd_not_a_project(self) -> None:
        resolved = types.SimpleNamespace(project=None, instance=None)
        with _patch_module("registry", self._fake_registry(resolved, [])):
            result = hooks.pre_tool_use(self.conn, {"cwd": "/x", "tool_input": {"command": "npm run dev"}})
        self.assertIsNone(result)

    def test_allow_when_no_dev_server_detected(self) -> None:
        result = hooks.pre_tool_use(self.conn, {"cwd": "/x", "tool_input": {"command": "ls -la"}})
        self.assertIsNone(result)


class SessionStartTests(unittest.TestCase):
    def setUp(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
            if path.exists():
                path.unlink()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def test_project_and_worktree_case(self) -> None:
        project = {"id": 1, "name": "sheron", "kind": "transient"}
        instance = {
            "id": 2,
            "label": "fix-login",
            "branch": "worktree-fix-login",
            "port": 3301,
            "url": "http://localhost:3301/",
            "state": "stopped",
        }
        resolved = types.SimpleNamespace(project=project, instance=instance, label="fix-login", is_worktree=True)
        fake_registry = types.SimpleNamespace(
            resolve_path=mock.Mock(return_value=resolved),
            list_instances=mock.Mock(return_value=[instance]),
            ensure_instance=mock.Mock(return_value=instance),
            git_branch=mock.Mock(return_value="worktree-fix-login"),
        )
        fake_reconcile = types.SimpleNamespace(reconcile=mock.Mock(return_value={}))
        with _patch_module("registry", fake_registry), _patch_module("reconcile", fake_reconcile):
            text = hooks.session_start(self.conn, {"session_id": "s1", "cwd": "/repo/.claude/worktrees/fix-login"})
        self.assertIsNotNone(text)
        self.assertIn("sheron", text)
        self.assertIn("worktree fix-login", text)
        self.assertIn("3301", text)

    def test_unknown_dir_returns_none(self) -> None:
        resolved = types.SimpleNamespace(project=None, instance=None)
        fake_registry = types.SimpleNamespace(resolve_path=mock.Mock(return_value=resolved))
        fake_discover = types.SimpleNamespace(
            looks_like_project=mock.Mock(return_value=False),
            suggest=mock.Mock(return_value=None),
        )
        fake_reconcile = types.SimpleNamespace(reconcile=mock.Mock(return_value={}))
        with _patch_module("registry", fake_registry), _patch_module("discover", fake_discover), _patch_module(
            "reconcile", fake_reconcile
        ):
            text = hooks.session_start(self.conn, {"session_id": "s1", "cwd": "/tmp/not-a-project"})
        self.assertIsNone(text)

    def test_no_cwd_returns_none(self) -> None:
        text = hooks.session_start(self.conn, {"session_id": "s1"})
        self.assertIsNone(text)


def _make_group_tree(prefix: str = "portboard-test-hooks-group-") -> tuple[str, str]:
    """(base, group dir) — a directory of sibling node repos plus a plain sub-dir.

    The group root deliberately has no .git; every child has one plus a
    package.json with a dev script, which is what discover.suggest_group wants.
    """
    base = tempfile.mkdtemp(prefix=prefix)
    group = os.path.join(base, "rma")
    for child in ("admin-app", "customer-app", "server-side"):
        child_dir = os.path.join(group, child)
        os.makedirs(os.path.join(child_dir, ".git"))
        with open(os.path.join(child_dir, "package.json"), "w") as fh:
            json.dump({"scripts": {"dev": "nuxt"}}, fh)
    os.makedirs(os.path.join(group, "tools"))  # a plain dir, not a repo
    return base, group


class GroupFormattingTests(unittest.TestCase):
    """Pure formatting: no database, no discover."""

    GROUP = {"id": 1, "name": "rma", "path": "/p/rma", "kind": "group", "primary": "rma-admin-app"}
    VIEW = {
        "id": 10, "label": "main", "slot": 0, "port": 3100, "state": "running",
        "url": "http://localhost:3100/", "primary": "rma-admin-app",
        "services": [
            {"project": "rma-admin-app", "port": 3100, "state": "running", "url": "http://localhost:3100/"},
            {"project": "rma-server-side", "port": 8080, "state": "running"},
            {"project": "rma-customer-app", "port": 3101, "state": "stopped"},
        ],
    }

    def test_group_line_names_primary_and_others(self) -> None:
        line = hooks.format_group_line(self.GROUP, [self.VIEW])
        self.assertEqual(
            line,
            "part of group rma: primary rma-admin-app :3100 (running); "
            "also rma-server-side :8080 (running), rma-customer-app :3101 (not running)",
        )

    def test_group_line_none_without_services(self) -> None:
        self.assertIsNone(hooks.format_group_line(self.GROUP, [{"label": "main", "slot": 0}]))

    def test_group_session_start_block(self) -> None:
        text = hooks.format_group_session_start(self.GROUP, [self.VIEW])
        lines = text.splitlines()
        self.assertEqual(lines[0], "[portboard] group rma at /p/rma (3 services)")
        self.assertEqual(lines[1], "frontend: rma-admin-app :3100, test URL http://localhost:3100/")
        self.assertTrue(lines[2].startswith("services: rma-admin-app :3100 (running)"))
        self.assertIn("instance_start (cwd=/p/rma)", lines[3])
        self.assertIn("portboard start rma", lines[3])


@unittest.skipUnless(hasattr(portboard, "__file__"), "portboard package required")
class GroupSessionStartTests(unittest.TestCase):
    """End-to-end against the real registry and discover, reconcile faked."""

    def setUp(self) -> None:
        assert_isolated()
        for suffix in ("", "-wal", "-shm"):
            path = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
            if path.exists():
                path.unlink()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.base, self.group_dir = _make_group_tree()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.fake_reconcile = types.SimpleNamespace(reconcile=mock.Mock(return_value={}))

    def _session_start(self, cwd: str) -> str | None:
        with _patch_module("reconcile", self.fake_reconcile):
            return hooks.session_start(self.conn, {"session_id": "s1", "cwd": cwd})

    def test_child_repo_registers_the_whole_group(self) -> None:
        from portboard import registry

        text = self._session_start(os.path.join(self.group_dir, "admin-app"))
        self.assertIsNotNone(text)
        lines = text.splitlines()
        self.assertEqual(lines[0], "[portboard] project rma-admin-app, main checkout")
        self.assertTrue(lines[1].startswith("assigned port "))
        self.assertTrue(lines[2].startswith("part of group rma: primary rma-admin-app :"))
        self.assertIn("also rma-customer-app :", lines[2])
        self.assertIn("rma-server-side :", lines[2])

        group = registry.get_project(self.conn, "rma")
        self.assertIsNotNone(group)
        self.assertEqual(group["kind"], "group")
        self.assertIsNone(group["base_port"])
        self.assertEqual(
            group["child_names"], ["rma-admin-app", "rma-customer-app", "rma-server-side"]
        )
        self.assertEqual(group["primary"], "rma-admin-app")
        self.assertEqual(registry.get_project(self.conn, "rma-server-side")["parent"], "rma")

    def test_group_directory_prints_the_group_summary(self) -> None:
        text = self._session_start(self.group_dir)
        self.assertIsNotNone(text)
        lines = text.splitlines()
        self.assertEqual(lines[0], f"[portboard] group rma at {self.group_dir} (3 services)")
        self.assertTrue(lines[1].startswith("frontend: rma-admin-app :"))
        self.assertIn("test URL http://localhost:", lines[1])
        self.assertTrue(lines[2].startswith("services: "))
        self.assertIn("rma-server-side :", lines[2])
        self.assertIn(f"instance_start (cwd={self.group_dir})", lines[3])
        self.assertIn("portboard start rma", lines[3])
        self.assertNotIn("assigned port", text)

    def test_plain_subdirectory_of_a_group_registers_it_too(self) -> None:
        from portboard import registry

        text = self._session_start(os.path.join(self.group_dir, "tools"))
        self.assertIsNotNone(text)
        self.assertTrue(text.startswith(f"[portboard] group rma at {self.group_dir}"))
        self.assertIsNotNone(registry.get_project(self.conn, "rma"))

    def test_lonely_repo_is_still_a_plain_project(self) -> None:
        from portboard import registry

        base = tempfile.mkdtemp(prefix="portboard-test-hooks-solo-")
        self.addCleanup(shutil.rmtree, base, True)
        repo = os.path.join(base, "solo")
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, "package.json"), "w") as fh:
            json.dump({"scripts": {"dev": "nuxt"}}, fh)

        text = self._session_start(repo)
        self.assertIsNotNone(text)
        self.assertEqual(text.splitlines()[0], "[portboard] project solo, main checkout")
        self.assertNotIn("part of group", text)
        self.assertEqual(registry.get_project(self.conn, "solo")["parent"], None)

    def test_pre_tool_use_resolves_a_cwd_inside_a_child(self) -> None:
        from portboard import registry

        self._session_start(os.path.join(self.group_dir, "admin-app"))
        sibling = registry.get_project(self.conn, "rma-server-side")
        sibling_port = registry.list_instances(self.conn, project_id=sibling["id"])[0]["port"]

        result = hooks.pre_tool_use(
            self.conn,
            {
                "cwd": os.path.join(self.group_dir, "admin-app"),
                "tool_input": {"command": f"npm run dev -- --port {sibling_port}"},
            },
        )
        self.assertIsNotNone(result)
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("rma-server-side", reason)

        own_port = registry.list_instances(
            self.conn, project_id=registry.get_project(self.conn, "rma-admin-app")["id"]
        )[0]["port"]
        self.assertIsNone(
            hooks.pre_tool_use(
                self.conn,
                {
                    "cwd": os.path.join(self.group_dir, "admin-app"),
                    "tool_input": {"command": f"npm run dev -- --port {own_port}"},
                },
            )
        )

    def test_cwd_changed_and_worktree_remove_on_a_child_worktree(self) -> None:
        from portboard import registry

        self._session_start(os.path.join(self.group_dir, "admin-app"))
        child = registry.get_project(self.conn, "rma-admin-app")
        wt = os.path.join(self.group_dir, "admin-app", config.WORKTREE_DIRNAME, "fix-login")
        os.makedirs(wt)

        hooks.cwd_changed(self.conn, {"cwd": wt})
        labels = {i["label"] for i in registry.list_instances(self.conn, project_id=child["id"])}
        self.assertIn("fix-login", labels)
        resolved = registry.resolve_path(self.conn, wt)
        self.assertEqual(resolved.project["name"], "rma-admin-app")
        self.assertTrue(resolved.is_worktree)
        self.assertEqual(resolved.group["name"], "rma")

        fake_runner = types.SimpleNamespace(stop=mock.Mock(return_value={}))
        with _patch_module("runner", fake_runner):
            hooks.worktree_remove(self.conn, {"worktree_path": wt})
        self.assertTrue(fake_runner.stop.called)
        labels = {i["label"] for i in registry.list_instances(self.conn, project_id=child["id"])}
        self.assertNotIn("fix-login", labels)


class RunSwallowsExceptionsTests(unittest.TestCase):
    def test_run_returns_zero_on_exception(self) -> None:
        with mock.patch("portboard.hooks.session_start", side_effect=RuntimeError("boom")):
            rc = hooks.run("session-start", {"cwd": "/x"})
        self.assertEqual(rc, 0)

    def test_run_unknown_event_returns_zero(self) -> None:
        rc = hooks.run("no-such-event", {})
        self.assertEqual(rc, 0)

    def test_run_db_connect_failure_returns_zero(self) -> None:
        with mock.patch("portboard.db.connect", side_effect=RuntimeError("no db")):
            rc = hooks.run("session-start", {"cwd": "/x"})
        self.assertEqual(rc, 0)


class ReadPayloadTests(unittest.TestCase):
    def test_empty_stdin(self) -> None:
        with mock.patch("sys.stdin.read", return_value=""):
            self.assertEqual(hooks.read_payload(), {})

    def test_invalid_json(self) -> None:
        with mock.patch("sys.stdin.read", return_value="not json"):
            self.assertEqual(hooks.read_payload(), {})

    def test_valid_json(self) -> None:
        with mock.patch("sys.stdin.read", return_value='{"a": 1}'):
            self.assertEqual(hooks.read_payload(), {"a": 1})


if __name__ == "__main__":
    unittest.main()
