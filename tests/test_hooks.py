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
import os
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("PORTBOARD_STATE_DIR", tempfile.mkdtemp(prefix="portboard-test-hooks-"))

import portboard  # noqa: E402
from portboard import config, db, hooks  # noqa: E402


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
