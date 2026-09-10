"""Unit tests for portboard.cli.

The state directory is redirected to a temporary directory BEFORE portboard is
imported, because config resolves DB_PATH at import time. Project-add/list
round-trip needs portboard.registry; guarded with skipUnless so the suite
stays green even if registry.py is not importable yet.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("PORTBOARD_STATE_DIR", tempfile.mkdtemp(prefix="portboard-test-cli-"))

from portboard import config, db  # noqa: E402
from portboard.cli import main  # noqa: E402

_REGISTRY_AVAILABLE = importlib.util.find_spec("portboard.registry") is not None


def _reset_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
        if path.exists():
            path.unlink()
    db.connect().close()


class HelpAndErrorsTests(unittest.TestCase):
    def test_help_exits_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_unknown_command_exits_two(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                main(["no-such-command"])
        self.assertEqual(cm.exception.code, 2)

    def test_no_subcommand_prints_help_and_exits_one(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main([])
        self.assertEqual(rc, 1)

    @unittest.skipUnless(_REGISTRY_AVAILABLE, "portboard.registry not importable yet")
    def test_missing_instance_reference_is_handled_error(self) -> None:
        _reset_db()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = main(["start"])
        self.assertEqual(rc, 1)
        self.assertIn("specify a project ref", stderr.getvalue())


@unittest.skipUnless(_REGISTRY_AVAILABLE, "portboard.registry not importable yet")
class ProjectAddListTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_db()
        base = tempfile.mkdtemp(prefix="portboard-test-cli-project-")
        # registry.derive_name() lowercases the basename and replaces anything
        # outside [a-z0-9-] with '-'; tempfile's random suffix can contain
        # '_', so use a fixed alnum leaf name the derived project name matches
        # exactly.
        self.tmp = os.path.join(base, "cliprojectfixture")
        os.makedirs(self.tmp)
        self.addCleanup(shutil.rmtree, base, True)

    def test_add_then_list(self) -> None:
        # Ports must stay below 32768 (DESIGN.md rule 5, enforced by
        # registry.port_is_free); use 29950 rather than a literal >= 32768.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["project", "add", self.tmp, "--port", "29950", "--kind", "none"])
        self.assertEqual(rc, 0, out.getvalue())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["list"])
        self.assertEqual(rc, 0)
        listing = out.getvalue()
        expected_name = os.path.basename(self.tmp).lower()
        self.assertIn(expected_name, listing)
        self.assertIn("29950", listing)

    def test_add_then_show_json(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            main(["project", "add", self.tmp, "--name", "demo-project", "--kind", "none"])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["--json", "project", "show", "demo-project"])
        self.assertEqual(rc, 0)
        self.assertIn('"demo-project"', out.getvalue())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_REGISTRY_AVAILABLE, "portboard.registry not importable yet")
class DiscoverApplyTests(unittest.TestCase):
    """One rejected suggestion must not abort ``discover --apply``."""

    def setUp(self) -> None:
        _reset_db()
        self.tmp = tempfile.mkdtemp(prefix="portboard-test-discover-apply-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _suggestions(self) -> list[dict]:
        rows = []
        for name, port in (("alpha", 4321), ("beta", 4321), ("gamma", 4322)):
            path = os.path.join(self.tmp, name)
            os.makedirs(path, exist_ok=True)
            rows.append({"path": path, "name": name, "kind": "transient", "confidence": "high",
                         "start_cmd": "npm run dev", "base_port": port, "port_mode": "env", "evidence": []})
        return rows

    def test_apply_skips_conflicts_and_continues(self) -> None:
        from unittest import mock
        from portboard import discover, registry

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(discover, "scan", return_value=self._suggestions()):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["discover", "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("registered 2 project(s)", out.getvalue())
        self.assertIn("skipped beta", err.getvalue())
        conn = db.connect()
        try:
            names = sorted(p["name"] for p in registry.list_projects(conn))
        finally:
            conn.close()
        self.assertEqual(names, ["alpha", "gamma"])

    def test_apply_json_lists_skipped(self) -> None:
        import json
        from unittest import mock
        from portboard import discover

        out = io.StringIO()
        with mock.patch.object(discover, "scan", return_value=self._suggestions()):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = main(["--json", "discover", "--apply"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual([p["name"] for p in payload["applied"]], ["alpha", "gamma"])
        self.assertEqual([s["name"] for s in payload["skipped"]], ["beta"])


@unittest.skipUnless(_REGISTRY_AVAILABLE, "portboard.registry not importable yet")
class GroupCliTests(unittest.TestCase):
    """`project add/edit/show`, `list`, `status` and `claim` for kind='group'."""

    def setUp(self) -> None:
        _reset_db()
        self.base = tempfile.mkdtemp(prefix="portboard-test-cli-group-")
        self.addCleanup(shutil.rmtree, self.base, True)
        # A group is a directory WITHOUT .git holding >= 2 git repos that
        # discover.suggest() recognises (package.json with a dev script).
        self.group = os.path.join(self.base, "rma")
        for child in ("admin-app", "server-side"):
            self._make_repo(os.path.join(self.group, child))

    @staticmethod
    def _make_repo(path: str) -> str:
        import json as _json

        os.makedirs(os.path.join(path, ".git"), exist_ok=True)
        with open(os.path.join(path, "package.json"), "w") as fh:
            _json.dump({"scripts": {"dev": "nuxt"}}, fh)
        return path

    @staticmethod
    def _run(argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(argv)
        return rc, out.getvalue()

    def _add_group(self) -> str:
        rc, text = self._run(["project", "add", self.group, "--kind", "group"])
        self.assertEqual(rc, 0, text)
        return text

    # -- project add --------------------------------------------------------

    def test_add_group_registers_group_and_children(self) -> None:
        from portboard import registry

        text = self._add_group()
        self.assertIn("added group rma", text)
        self.assertIn("with 2 sub-projects", text)
        self.assertIn("rma-admin-app", text)
        self.assertIn("rma-server-side", text)
        self.assertIn("(primary)", text)

        conn = db.connect()
        try:
            group = registry.get_project(conn, "rma")
            children = registry.group_children(conn, group["id"])
        finally:
            conn.close()
        self.assertEqual(group["kind"], "group")
        self.assertIsNone(group["base_port"])
        self.assertEqual(sorted(c["name"] for c in children), ["rma-admin-app", "rma-server-side"])
        self.assertEqual(group["primary"], "rma-admin-app")

    def test_add_without_kind_detects_a_group(self) -> None:
        from portboard import registry

        rc, text = self._run(["project", "add", self.group])
        self.assertEqual(rc, 0, text)
        self.assertIn("added group rma", text)
        conn = db.connect()
        try:
            self.assertEqual(registry.get_project(conn, "rma")["kind"], "group")
        finally:
            conn.close()

    def test_add_kind_group_on_a_plain_directory_errors(self) -> None:
        plain = os.path.join(self.base, "plain")
        os.makedirs(plain)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["project", "add", plain, "--kind", "group"])
        self.assertEqual(rc, 1)
        self.assertIn("is not a group", err.getvalue())

    def test_add_with_parent_makes_a_child(self) -> None:
        from portboard import registry

        self._add_group()
        extra = self._make_repo(os.path.join(self.base, "extra"))
        rc, text = self._run(["project", "add", extra, "--parent", "rma"])
        self.assertEqual(rc, 0, text)
        self.assertIn("in group rma", text)

        conn = db.connect()
        try:
            child = registry.get_project(conn, "rma-extra")
            group = registry.get_project(conn, "rma")
        finally:
            conn.close()
        self.assertIsNotNone(child)
        self.assertEqual(child["parent"], "rma")
        self.assertIn(child["id"], group["children"])

    def test_add_with_unknown_parent_errors(self) -> None:
        extra = self._make_repo(os.path.join(self.base, "extra"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["project", "add", extra, "--parent", "nope"])
        self.assertEqual(rc, 1)
        self.assertIn("no project named nope", err.getvalue())

    def test_add_with_a_non_group_parent_errors(self) -> None:
        self._add_group()
        extra = self._make_repo(os.path.join(self.base, "extra"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["project", "add", extra, "--parent", "rma-admin-app"])
        self.assertEqual(rc, 1)
        self.assertIn("is not a group", err.getvalue())

    # -- project edit -------------------------------------------------------

    def test_edit_primary_changes_the_primary_child(self) -> None:
        from portboard import registry

        self._add_group()
        rc, text = self._run(["project", "edit", "rma", "--primary", "rma-server-side"])
        self.assertEqual(rc, 0, text)
        conn = db.connect()
        try:
            group = registry.get_project(conn, "rma")
        finally:
            conn.close()
        self.assertEqual(group["primary"], "rma-server-side")

    def test_edit_primary_rejects_a_foreign_project(self) -> None:
        self._add_group()
        extra = self._make_repo(os.path.join(self.base, "extra"))
        with contextlib.redirect_stdout(io.StringIO()):
            main(["project", "add", extra, "--kind", "none"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["project", "edit", "rma", "--primary", "extra"])
        self.assertEqual(rc, 1)
        self.assertIn("not a child", err.getvalue())

    def test_edit_parent_attaches_and_detaches(self) -> None:
        from portboard import registry

        self._add_group()
        extra = self._make_repo(os.path.join(self.base, "extra"))
        with contextlib.redirect_stdout(io.StringIO()):
            main(["project", "add", extra, "--kind", "none", "--name", "extra"])

        rc, text = self._run(["project", "edit", "extra", "--parent", "rma"])
        self.assertEqual(rc, 0, text)
        conn = db.connect()
        try:
            self.assertEqual(registry.get_project(conn, "extra")["parent"], "rma")
        finally:
            conn.close()

        rc, text = self._run(["project", "edit", "extra", "--parent", "none"])
        self.assertEqual(rc, 0, text)
        conn = db.connect()
        try:
            self.assertIsNone(registry.get_project(conn, "extra")["parent"])
        finally:
            conn.close()

    def test_edit_allow_busy_accepts_a_listening_port(self) -> None:
        import socket

        from portboard import registry

        self._add_group()
        # a fixed port below 32768 (registry refuses the ephemeral range)
        port = None
        for candidate in range(29960, 30000):
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                sock.close()
                continue
            sock.listen(1)
            self.addCleanup(sock.close)
            port = candidate
            break
        if port is None:
            self.skipTest("no free port in 29960-29999 to occupy")

        rc, text = self._run(["project", "edit", "rma-admin-app", "--port", str(port), "--allow-busy"])
        self.assertEqual(rc, 0, text)
        conn = db.connect()
        try:
            self.assertEqual(registry.get_project(conn, "rma-admin-app")["base_port"], port)
        finally:
            conn.close()

    # -- project show -------------------------------------------------------

    def test_show_group_lists_children_and_child_names_parent(self) -> None:
        self._add_group()
        rc, text = self._run(["project", "show", "rma"])
        self.assertEqual(rc, 0, text)
        self.assertIn("sub-projects: 2", text)
        self.assertIn("rma-admin-app", text)
        self.assertIn("(primary)", text)
        self.assertIn("rma-server-side", text)

        rc, text = self._run(["project", "show", "rma-server-side"])
        self.assertEqual(rc, 0, text)
        self.assertIn("parent: rma", text)

    # -- list ---------------------------------------------------------------

    def _instance_table(self) -> list[str]:
        rc, text = self._run(["list"])
        self.assertEqual(rc, 0, text)
        block = text.split("\n\n", 1)[0]
        return block.splitlines()[1:]  # drop the header row

    def test_list_renders_children_indented_under_the_group_once(self) -> None:
        self._add_group()
        rows = self._instance_table()
        projects = [row.split("  ")[0] if not row.startswith("  ") else row.strip().split("  ")[0]
                    for row in rows]
        self.assertIn("rma", projects)
        self.assertIn("rma/rma-admin-app", projects)
        self.assertIn("rma/rma-server-side", projects)
        # the children appear exactly once, prefixed by their group
        self.assertEqual(projects.count("rma/rma-admin-app"), 1)
        self.assertEqual(projects.count("rma/rma-server-side"), 1)
        self.assertNotIn("rma-admin-app", projects)
        self.assertNotIn("rma-server-side", projects)
        # the group row directly precedes its children
        self.assertEqual(projects.index("rma") + 1, projects.index("rma/rma-admin-app"))

    def test_list_group_row_mirrors_the_primary_port(self) -> None:
        from portboard import registry

        self._add_group()
        conn = db.connect()
        try:
            primary_port = registry.get_project(conn, "rma-admin-app")["base_port"]
        finally:
            conn.close()
        rows = self._instance_table()
        group_row = next(r for r in rows if not r.startswith("  ") and r.split()[0] == "rma")
        self.assertIn(str(primary_port), group_row)

    def test_list_json_keeps_the_flat_shape_with_parent(self) -> None:
        import json as _json

        self._add_group()
        rc, text = self._run(["--json", "list"])
        self.assertEqual(rc, 0, text)
        payload = _json.loads(text)
        self.assertEqual(sorted(payload), ["instances", "observed"])
        by_project = {i["project"]: i for i in payload["instances"] if not i.get("slot")}
        self.assertEqual(by_project["rma-admin-app"]["parent"], "rma")
        self.assertIsNone(by_project["rma"]["parent"])

    # -- status / claim -----------------------------------------------------

    def test_status_on_the_group_directory_prints_the_group_block(self) -> None:
        self._add_group()
        rc, text = self._run(["status", "--cwd", self.group])
        self.assertEqual(rc, 0, text)
        self.assertIn("[portboard] group rma", text)
        self.assertIn("2 services", text)

    def test_status_inside_a_child_prints_the_group_line(self) -> None:
        self._add_group()
        rc, text = self._run(["status", "--cwd", os.path.join(self.group, "server-side")])
        self.assertEqual(rc, 0, text)
        self.assertIn("project rma-server-side", text)
        self.assertIn("part of group rma", text)
        self.assertIn("primary rma-admin-app", text)

    def test_claim_inside_a_child_registers_the_whole_group(self) -> None:
        from portboard import registry

        rc, text = self._run(["claim", "--cwd", os.path.join(self.group, "server-side")])
        self.assertEqual(rc, 0, text)
        self.assertIn("claimed rma-server-side", text)
        conn = db.connect()
        try:
            names = sorted(p["name"] for p in registry.list_projects(conn))
        finally:
            conn.close()
        self.assertEqual(names, ["rma", "rma-admin-app", "rma-server-side"])

    # -- discover -----------------------------------------------------------

    def test_discover_lists_children_under_the_group(self) -> None:
        rc, text = self._run(["discover", self.base])
        self.assertEqual(rc, 0, text)
        lines = text.splitlines()
        group_idx = next(i for i, line in enumerate(lines) if " rma " in f" {line} " or line.split()[1:2] == ["rma"])
        self.assertTrue(lines[group_idx + 1].split()[1].startswith("rma-"))
        self.assertIn("rma-admin-app", text)
        self.assertIn("rma-server-side", text)

    def test_discover_apply_registers_the_group_not_the_children_twice(self) -> None:
        from portboard import registry

        rc, text = self._run(["discover", self.base, "--apply"])
        self.assertEqual(rc, 0, text)
        conn = db.connect()
        try:
            projects = registry.list_projects(conn)
        finally:
            conn.close()
        names = sorted(p["name"] for p in projects)
        self.assertEqual(names, ["rma", "rma-admin-app", "rma-server-side"])
        group = next(p for p in projects if p["name"] == "rma")
        self.assertEqual(len(group["children"]), 2)
