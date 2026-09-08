"""Unit tests for portboard.install.

SAFETY: every test here points SYSTEMD_USER_DIR / LOCAL_BIN / CLAUDE_SETTINGS
at temp-dir paths before calling anything, and dry_run=True tests additionally
assert subprocess.run is never invoked. Never call install.run() against the
real home in this file.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("PORTBOARD_STATE_DIR", tempfile.mkdtemp(prefix="portboard-test-install-"))

from portboard import config, install  # noqa: E402


class UnitFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = dict(config.DEFAULT_SETTINGS)
        self.settings.update({"daemon_port": "8790", "evening_stop": "16:30", "morning_start": "07:30", "morning_days": "Mon..Fri"})
        self.files = install.unit_files(self.settings, "/repo", "/usr/bin/python3")

    def test_all_files_present(self) -> None:
        expected = {
            "portboard.socket",
            "portboard.service",
            "portboard-evening.service",
            "portboard-evening.timer",
            "portboard-morning.service",
            "portboard-morning.timer",
            "portboard-tick.service",
            "portboard-tick.timer",
        }
        self.assertEqual(set(self.files), expected)

    def test_socket_listen_stream(self) -> None:
        content = self.files["portboard.socket"]
        self.assertIn("ListenStream=127.0.0.1:8790", content)
        self.assertIn("NoDelay=true", content)
        self.assertIn("WantedBy=sockets.target", content)

    def test_service_has_no_restart(self) -> None:
        content = self.files["portboard.service"]
        self.assertNotIn("Restart=", content)
        self.assertIn("ExecStart=/usr/bin/python3 /repo/bin/portboard serve", content)
        self.assertIn("MemoryMax=300M", content)
        self.assertIn("Nice=5", content)
        self.assertIn("Environment=PYTHONUNBUFFERED=1", content)

    def test_evening_timer_oncalendar(self) -> None:
        content = self.files["portboard-evening.timer"]
        self.assertIn("OnCalendar=*-*-* 16:30:00", content)
        self.assertIn("Persistent=false", content)

    def test_morning_timer_oncalendar(self) -> None:
        content = self.files["portboard-morning.timer"]
        self.assertIn("OnCalendar=Mon..Fri *-*-* 07:30:00", content)

    def test_tick_timer(self) -> None:
        content = self.files["portboard-tick.timer"]
        self.assertIn("OnBootSec=10min", content)
        self.assertIn("OnUnitActiveSec=30min", content)
        self.assertIn("AccuracySec=5min", content)

    def test_evening_and_morning_services_are_oneshot(self) -> None:
        self.assertIn("Type=oneshot", self.files["portboard-evening.service"])
        self.assertIn("schedule stop", self.files["portboard-evening.service"])
        self.assertIn("Type=oneshot", self.files["portboard-morning.service"])
        self.assertIn("schedule start", self.files["portboard-morning.service"])
        self.assertIn("Type=oneshot", self.files["portboard-tick.service"])
        self.assertIn(" tick", self.files["portboard-tick.service"])


class MergeHooksTests(unittest.TestCase):
    def _existing_settings(self) -> dict:
        return {
            "hooks": {
                "Notification": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/notify-hermes.py", "timeout": 30}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/notify-hermes.py", "timeout": 60}]}],
            }
        }

    def test_adds_five_events(self) -> None:
        merged, added = install.merge_hooks(self._existing_settings())
        for event in ("SessionStart", "SessionEnd", "PreToolUse", "CwdChanged", "WorktreeRemove"):
            self.assertIn(event, merged["hooks"], event)
        self.assertEqual(len(added), 5)

    def test_pre_tool_use_has_bash_matcher(self) -> None:
        merged, _ = install.merge_hooks(self._existing_settings())
        group = merged["hooks"]["PreToolUse"][0]
        self.assertEqual(group.get("matcher"), "Bash")
        self.assertEqual(group["hooks"][0]["command"], "portboard hook pre-tool-use")

    def test_session_start_has_no_matcher(self) -> None:
        merged, _ = install.merge_hooks(self._existing_settings())
        group = merged["hooks"]["SessionStart"][0]
        self.assertNotIn("matcher", group)

    def test_timeouts(self) -> None:
        merged, _ = install.merge_hooks(self._existing_settings())
        timeouts = {
            "SessionStart": 10,
            "SessionEnd": 5,
            "PreToolUse": 10,
            "CwdChanged": 5,
            "WorktreeRemove": 30,
        }
        for event, timeout in timeouts.items():
            group = merged["hooks"][event][0]
            self.assertEqual(group["hooks"][0]["timeout"], timeout, event)

    def test_preserves_existing_notification_and_stop_hooks(self) -> None:
        existing = self._existing_settings()
        merged, _ = install.merge_hooks(existing)
        self.assertEqual(merged["hooks"]["Notification"], existing["hooks"]["Notification"])
        self.assertEqual(merged["hooks"]["Stop"], existing["hooks"]["Stop"])

    def test_idempotent(self) -> None:
        merged_once, added_once = install.merge_hooks(self._existing_settings())
        merged_twice, added_twice = install.merge_hooks(merged_once)
        self.assertEqual(merged_once, merged_twice)
        self.assertEqual(added_twice, [])
        self.assertEqual(len(added_once), 5)

    def test_does_not_mutate_input(self) -> None:
        existing = self._existing_settings()
        original = json.loads(json.dumps(existing))
        install.merge_hooks(existing)
        self.assertEqual(existing, original)


class DetectNodePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="portboard-test-home-")
        self.addCleanup(self._cleanup)
        self._home_patch = mock.patch.dict(os.environ, {"HOME": self.tmp})
        self._home_patch.start()

    def _cleanup(self) -> None:
        self._home_patch.stop()

    def test_node_inside_nvm_on_path(self) -> None:
        bin_dir = Path(self.tmp) / ".nvm" / "versions" / "node" / "v18.17.0" / "bin"
        bin_dir.mkdir(parents=True)
        node_bin = bin_dir / "node"
        node_bin.write_text("#!/bin/sh\n")
        with mock.patch("shutil.which", return_value=str(node_bin)):
            result = install.detect_node_path()
        self.assertEqual(result, str(bin_dir))

    def test_newest_nvm_version_when_node_not_on_path(self) -> None:
        versions = Path(self.tmp) / ".nvm" / "versions" / "node"
        for name in ("v14.0.0", "v20.5.1", "v16.2.0"):
            (versions / name / "bin").mkdir(parents=True)
        with mock.patch("shutil.which", return_value=None):
            result = install.detect_node_path()
        self.assertEqual(result, str(versions / "v20.5.1" / "bin"))

    def test_node_outside_nvm(self) -> None:
        with mock.patch("shutil.which", return_value="/usr/bin/node"):
            result = install.detect_node_path()
        self.assertEqual(result, "/usr/bin")

    def test_nothing_found(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            result = install.detect_node_path()
        self.assertIsNone(result)


class DryRunInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="portboard-test-install-dry-")
        self.systemd_dir = Path(self.tmp) / "config" / "systemd" / "user"
        self.local_bin = Path(self.tmp) / "local" / "bin" / "portboard"
        self.claude_settings = Path(self.tmp) / "claude" / "settings.json"

        self._patches = [
            mock.patch.object(install, "SYSTEMD_USER_DIR", self.systemd_dir),
            mock.patch.object(install, "LOCAL_BIN", self.local_bin),
            mock.patch.object(install, "CLAUDE_SETTINGS", self.claude_settings),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_dry_run_writes_nothing(self) -> None:
        with mock.patch("subprocess.run") as run_mock:
            rc = install.run(dry_run=True)
        self.assertEqual(rc, 0)
        run_mock.assert_not_called()
        self.assertFalse(self.systemd_dir.exists())
        self.assertFalse(self.local_bin.exists())
        self.assertFalse(self.claude_settings.exists())

    def test_dry_run_all_writes_nothing(self) -> None:
        empty_root = Path(self.tmp) / "empty-projects-root"
        empty_root.mkdir()
        with mock.patch("subprocess.run") as run_mock, mock.patch.object(config, "PROJECTS_ROOT", empty_root):
            rc = install.run(all=True, dry_run=True)
        self.assertEqual(rc, 0)
        run_mock.assert_not_called()
        self.assertFalse(self.systemd_dir.exists())
        self.assertFalse(self.local_bin.exists())
        self.assertFalse(self.claude_settings.exists())


if __name__ == "__main__":
    unittest.main()
