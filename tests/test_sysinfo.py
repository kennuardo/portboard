"""Tests for portboard.sysinfo: pure parsers plus the subprocess wrappers."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("PORTBOARD_STATE_DIR", tempfile.mkdtemp(prefix="portboard-test-"))

from portboard import sysinfo  # noqa: E402

# Real `ss -ltnpH` output from this machine, trimmed and extended with the
# shapes we must survive: several process entries, IPv6, %lo, *:port, no users.
SS_TCP = """\
LISTEN 0      511                      127.0.0.1:34707 0.0.0.0:* users:(("code",pid=65387,fd=46))
LISTEN 0      4096                 127.0.0.53%lo:53    0.0.0.0:*
LISTEN 0      511                        0.0.0.0:3300  0.0.0.0:* users:(("node",pid=69288,fd=20),("node",pid=69290,fd=20))
LISTEN 0      4096                       0.0.0.0:22    0.0.0.0:*
LISTEN 0      4096                          [::]:3000     [::]:* users:(("docker-proxy",pid=496200,fd=7))
LISTEN 0      100                              *:8080        *:*
LISTEN 0      2048                       0.0.0.0:8765  0.0.0.0:* users:(("python",pid=305519,fd=15))

LISTEN 0      511                        0.0.0.0:3300  0.0.0.0:* users:(("node",pid=69288,fd=21))
"""

SS_UDP = """\
UNCONN 0      0                          0.0.0.0:57461 0.0.0.0:* users:(("python3",pid=116008,fd=9))
UNCONN 0      0                    127.0.0.53%lo:53    0.0.0.0:*
"""


def fake_cp(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


class ParseSsTest(unittest.TestCase):
    def setUp(self):
        self.by_port = {l.port: l for l in sysinfo.parse_ss(SS_TCP)}

    def test_counts_and_dedupe(self):
        # eight lines, but 3300 appears twice with the same (port, bind)
        self.assertEqual(len(sysinfo.parse_ss(SS_TCP)), 7)

    def test_first_pid_of_several_users(self):
        l = self.by_port[3300]
        self.assertEqual((l.pid, l.comm, l.fd), (69288, "node", 20))

    def test_ipv6_bind_loses_brackets(self):
        self.assertEqual(self.by_port[3000].bind, "::")
        self.assertEqual(self.by_port[3000].pid, 496200)

    def test_wildcard_and_interface_binds(self):
        self.assertEqual(self.by_port[8080].bind, "*")
        self.assertEqual(self.by_port[53].bind, "127.0.0.53%lo")

    def test_root_owned_socket_has_no_pid(self):
        l = self.by_port[22]
        self.assertIsNone(l.pid)
        self.assertIsNone(l.comm)

    def test_proto_defaults_to_tcp_and_is_inferred_for_udp(self):
        self.assertTrue(all(l.proto == "tcp" for l in sysinfo.parse_ss(SS_TCP)))
        self.assertTrue(all(l.proto == "udp" for l in sysinfo.parse_ss(SS_UDP)))
        self.assertTrue(all(l.proto == "udp" for l in sysinfo.parse_ss(SS_TCP, proto="udp")))

    def test_garbage_is_ignored(self):
        self.assertEqual(sysinfo.parse_ss(""), [])
        self.assertEqual(sysinfo.parse_ss("nonsense\nLISTEN 0 1 x y\n"), [])


class ListeningPortsTest(unittest.TestCase):
    def test_uses_ss_ltnph(self):
        with mock.patch.object(sysinfo, "_run", return_value=fake_cp(SS_TCP)) as run:
            ports = sysinfo.listening_ports()
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ["ss", "-ltnpH"])
        self.assertEqual(len(ports), 7)

    def test_falls_back_without_p(self):
        calls = [fake_cp("", returncode=1, stderr="ss: -p not permitted"), fake_cp(SS_TCP)]
        with mock.patch.object(sysinfo, "_run", side_effect=calls) as run:
            ports = sysinfo.listening_ports()
        self.assertEqual([c[0][0] for c in run.call_args_list], [["ss", "-ltnpH"], ["ss", "-ltnH"]])
        self.assertEqual(len(ports), 7)

    def test_udp_flags(self):
        with mock.patch.object(sysinfo, "_run", return_value=fake_cp(SS_UDP)) as run:
            ports = sysinfo.listening_ports("udp")
        self.assertEqual(run.call_args[0][0], ["ss", "-lunpH"])
        self.assertEqual({p.port for p in ports}, {53, 57461})

    def test_missing_ss_returns_empty(self):
        with mock.patch.object(sysinfo, "_run", return_value=None):
            self.assertEqual(sysinfo.listening_ports(), [])

    def test_established_count(self):
        with mock.patch.object(sysinfo, "_run", return_value=fake_cp("a\nb\n\n")) as run:
            self.assertEqual(sysinfo.established_count(3300), 2)
        self.assertEqual(run.call_args[0][0][-1], "( sport = :3300 )")
        with mock.patch.object(sysinfo, "_run", return_value=None):
            self.assertEqual(sysinfo.established_count(3300), 0)


class ParseCgroupTest(unittest.TestCase):
    def test_user_unit(self):
        text = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/portboard-sheron-main.service"
        self.assertEqual(sysinfo.parse_cgroup(text), ("portboard-sheron-main.service", None))

    def test_docker_scope(self):
        cid = "f211198aead31ffaaf73a4b87482ad3aca7bd5d67b7626e87b2cff3bd6c5d16c"
        self.assertEqual(sysinfo.parse_cgroup(f"0::/system.slice/docker-{cid}.scope"), (None, cid))

    def test_app_scope(self):
        text = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-code-1234.scope"
        self.assertEqual(sysinfo.parse_cgroup(text), ("app-gnome-code-1234.scope", None))

    def test_nested_takes_last_unit_component(self):
        text = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/foo.service/bar"
        self.assertEqual(sysinfo.parse_cgroup(text), ("foo.service", None))

    def test_cgroup_v1_multiline(self):
        text = ("12:pids:/user.slice/user-1000.slice/user@1000.service/app.slice/sheron-dev.service\n"
                "1:name=systemd:/user.slice/user-1000.slice/user@1000.service/app.slice/sheron-dev.service\n")
        self.assertEqual(sysinfo.parse_cgroup(text), ("sheron-dev.service", None))

    def test_docker_v1_path(self):
        cid = "a" * 64
        self.assertEqual(sysinfo.parse_cgroup(f"1:cpu:/docker/{cid}"), (None, cid))

    def test_empty(self):
        self.assertEqual(sysinfo.parse_cgroup(""), (None, None))


class ProcInfoTest(unittest.TestCase):
    def test_reads_own_process(self):
        info = sysinfo.proc_info(os.getpid())
        self.assertEqual(info.pid, os.getpid())
        self.assertEqual(info.uid, os.getuid())
        self.assertTrue(info.cmdline)
        self.assertLessEqual(len(info.cmdline), sysinfo.CMDLINE_MAX)
        self.assertEqual(info.cwd, os.path.realpath(os.getcwd()))
        self.assertIsInstance(info.ppid, int)

    def test_unreadable_pid_is_all_none(self):
        info = sysinfo.proc_info(999_999_98)
        self.assertIsNone(info.cwd)
        self.assertIsNone(info.cmdline)
        self.assertIsNone(info.unit)


PS_JSON = json.dumps({"ID": "b7c8213b6279", "Names": "hrm-frontend", "State": "running",
                      "Image": "hr-app-frontend", "Ports": "0.0.0.0:3000->3000/tcp, [::]:3000->3000/tcp",
                      "Labels": "com.docker.compose.project=hr-app,"
                                "com.docker.compose.project.working_dir=/mnt/hyper/Projects/hr-app"})
INSPECT_JSON = json.dumps([{
    "Id": "b7c8213b6279" + "0" * 52,
    "Name": "/hrm-frontend",
    "State": {"Status": "running", "Pid": 496039},
    "Image": "sha256:deadbeef",
    "Config": {"Image": "hr-app-frontend", "Labels": {
        "com.docker.compose.project": "hr-app",
        "com.docker.compose.project.working_dir": "/mnt/hyper/Projects/hr-app"}},
    "HostConfig": {"NetworkMode": "hrm-network"},
    "NetworkSettings": {"Ports": {"3000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3000"},
                                               {"HostIp": "::", "HostPort": "3000"}],
                                  "9000/tcp": None}},
}])


class DockerTest(unittest.TestCase):
    def setUp(self):
        sysinfo._reset_docker_cache()
        self.addCleanup(sysinfo._reset_docker_cache)

    def test_docker_available_is_cached(self):
        with mock.patch.object(sysinfo, "_run", return_value=fake_cp("28.0.1\n")) as run:
            self.assertTrue(sysinfo.docker_available())
            self.assertTrue(sysinfo.docker_available())
        self.assertEqual(run.call_count, 1)

    def test_docker_unavailable(self):
        with mock.patch.object(sysinfo, "_run", return_value=None):
            self.assertFalse(sysinfo.docker_available())
            self.assertEqual(sysinfo.docker_containers(), [])

    def test_containers_from_inspect(self):
        calls = [fake_cp("28.0.1"), fake_cp(PS_JSON + "\n"), fake_cp(INSPECT_JSON)]
        with mock.patch.object(sysinfo, "_run", side_effect=calls) as run:
            containers = sysinfo.docker_containers()
        self.assertEqual(run.call_args_list[1][0][0], ["docker", "ps", "--format", "{{json .}}"])
        self.assertEqual(run.call_args_list[2][0][0], ["docker", "inspect", "b7c8213b6279"])
        self.assertEqual(len(containers), 1)
        c = containers[0]
        self.assertEqual(c.name, "hrm-frontend")
        self.assertEqual(c.ports, [(3000, 3000)])          # deduped, unpublished port skipped
        self.assertEqual(c.compose_project, "hr-app")
        self.assertEqual(c.compose_workdir, "/mnt/hyper/Projects/hr-app")
        self.assertEqual((c.network_mode, c.pid, c.state), ("hrm-network", 496039, "running"))

    def test_falls_back_to_ps_when_inspect_fails(self):
        calls = [fake_cp("28.0.1"), fake_cp(PS_JSON + "\n"), fake_cp("boom", returncode=1)]
        with mock.patch.object(sysinfo, "_run", side_effect=calls):
            containers = sysinfo.docker_containers()
        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0].ports, [(3000, 3000)])
        self.assertEqual(containers[0].compose_project, "hr-app")


SHOW_OUT = """\
Id=sheron-dev.service
ActiveState=active
SubState=running
MainPID=69272
NRestarts=0
ExecMainStartTimestamp=Mon 2026-09-07 08:20:30 CEST
MemoryCurrent=1554714624
CPUUsageNSec=296672675000

Id=nope.service
ActiveState=inactive
SubState=dead
MainPID=0
NRestarts=0
ExecMainStartTimestamp=
MemoryCurrent=[not set]
CPUUsageNSec=[not set]
"""


class UnitStatsTest(unittest.TestCase):
    def test_one_call_for_all_units(self):
        with mock.patch.object(sysinfo, "_run", return_value=fake_cp(SHOW_OUT)) as run:
            stats = sysinfo.unit_cgroup_stats(["sheron-dev.service", "nope.service", "sheron-dev.service"])
        self.assertEqual(run.call_count, 1)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:3], ["systemctl", "--user", "show"])
        self.assertEqual(cmd[-2:], ["sheron-dev.service", "nope.service"])  # deduped
        self.assertEqual(stats["sheron-dev.service"]["MemoryCurrent"], 1554714624)
        self.assertEqual(stats["sheron-dev.service"]["ActiveState"], "active")
        self.assertIsNone(stats["nope.service"]["MemoryCurrent"])
        self.assertIsNone(stats["nope.service"]["ExecMainStartTimestamp"])
        self.assertEqual(stats["nope.service"]["MainPID"], 0)

    def test_no_units_no_subprocess(self):
        with mock.patch.object(sysinfo, "_run") as run:
            self.assertEqual(sysinfo.unit_cgroup_stats([]), {})
        run.assert_not_called()

    def test_failure_is_empty(self):
        with mock.patch.object(sysinfo, "_run", return_value=fake_cp("", returncode=1)):
            self.assertEqual(sysinfo.unit_cgroup_stats(["x.service"]), {})


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc and ss")
class LiveTest(unittest.TestCase):
    def test_listening_ports_runs(self):
        ports = sysinfo.listening_ports()
        self.assertIsInstance(ports, list)
        for p in ports:
            self.assertIsInstance(p, sysinfo.Listener)
            self.assertGreater(p.port, 0)


if __name__ == "__main__":
    unittest.main()
