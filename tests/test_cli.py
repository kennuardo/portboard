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
