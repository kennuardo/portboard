"""Unit tests for portboard.discover.

Synthetic repositories in temporary directories cover the rules; the tests at
the bottom check the real repositories on this workstation READ-ONLY and skip
themselves when a directory is missing.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("PORTBOARD_STATE_DIR", tempfile.mkdtemp(prefix="portboard-test-discover-"))

from portboard import discover  # noqa: E402  (after the env override)


class FakeRepo(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="portboard-test-repo-")

    def write(self, relative: str, text: str) -> str:
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def package(self, dev: str = "nuxt dev", **extra) -> None:
        data = {"name": "demo", "scripts": {"dev": dev, "build": "nuxt build"}}
        data.update(extra)
        self.write("package.json", json.dumps(data))

    def git(self) -> None:
        os.makedirs(os.path.join(self.root, ".git"), exist_ok=True)


class TestNode(FakeRepo):
    def test_nuxt_dev_server_port_and_pnpm(self):
        self.package()
        self.write("nuxt.config.ts", "export default defineNuxtConfig({\n  devServer: {\n    port: 3007,\n  },\n})\n")
        self.write("pnpm-lock.yaml", "lockfileVersion: 6.0\n")
        found = discover.suggest(self.root)
        self.assertEqual("transient", found["kind"])
        self.assertEqual("pnpm run dev", found["start_cmd"])
        self.assertEqual("env", found["port_mode"])
        self.assertEqual(3007, found["base_port"])
        self.assertEqual("high", found["confidence"])
        self.assertTrue(any("devServer.port=3007" in e for e in found["evidence"]))

    def test_vite_server_port_and_yarn(self):
        self.package(dev="vite")
        self.write("vite.config.ts", "export default defineConfig({\n  server: { port: 5199, strictPort: true },\n})\n")
        self.write("yarn.lock", "# yarn\n")
        found = discover.suggest(self.root)
        self.assertEqual("yarn run dev", found["start_cmd"])
        self.assertEqual(5199, found["base_port"])

    def test_port_flag_in_the_dev_script_and_bun(self):
        self.package(dev="npx kill-port 3000 && nuxt dev -p 3210")
        self.write("bun.lockb", "")
        found = discover.suggest(self.root)
        self.assertEqual("bun run dev", found["start_cmd"])
        self.assertEqual(3210, found["base_port"])

    def test_env_port_only_and_nothing_else_from_the_env_file(self):
        self.package()
        self.write(".env", "SECRET_TOKEN=hunter2\nPORT=3999\nDATABASE_URL=postgres://x\n")
        found = discover.suggest(self.root)
        self.assertEqual(3999, found["base_port"])
        self.assertNotIn("hunter2", json.dumps(found))
        self.assertNotIn("postgres", json.dumps(found))

    def test_no_port_evidence_leaves_the_port_to_the_allocator(self):
        self.package()
        self.write("README.md", "run it on http://localhost:3000\n")
        found = discover.suggest(self.root)
        self.assertIsNone(found["base_port"])
        self.assertEqual("high", found["confidence"])
        self.assertEqual("npm run dev", found["start_cmd"])

    def test_dev_script_beats_a_compose_file(self):
        self.package()
        self.write("docker-compose.yml", "services:\n  web:\n    ports:\n      - \"9001:3000\"\n")
        found = discover.suggest(self.root)
        self.assertEqual("transient", found["kind"])
        self.assertIsNone(found["base_port"])

    def test_name_is_sanitised(self):
        weird = os.path.join(self.root, "My Repo.v2")
        os.makedirs(weird)
        self.package()
        os.replace(os.path.join(self.root, "package.json"), os.path.join(weird, "package.json"))
        self.assertEqual("my-repo-v2", discover.suggest(weird)["name"])


class TestCompose(FakeRepo):
    COMPOSE = """version: '3.8'

services:

  postgres:
    image: postgres:16
    ports:
      - "5432:5432"

  frontend:
    build: ./src/frontend
    environment:
      NODE_ENV: development
    ports:
      - "${FE_PORT:-3030}:3000"
    volumes:
      - ./src:/app/src

volumes:
  data:
"""

    def test_preferred_service_wins(self):
        self.write("docker-compose.yml", self.COMPOSE)
        found = discover.suggest(self.root)
        self.assertEqual("compose", found["kind"])
        self.assertEqual("fixed", found["port_mode"])
        self.assertEqual(3030, found["base_port"])
        self.assertTrue(any("frontend" in e for e in found["evidence"]))

    def test_lowest_published_port_when_no_preferred_service(self):
        self.write("compose.yaml", "services:\n  db:\n    ports:\n      - 5432:5432\n  cache:\n    ports:\n      - \"6379:6379\"\n")
        self.assertEqual(5432, discover.suggest(self.root)["base_port"])

    def test_host_network_falls_back_to_run_sh(self):
        self.write("docker-compose.yml", "services:\n  whisper:\n    network_mode: host\n    image: whisper\n")
        self.write("run.sh", '#!/bin/bash\nPORT="${PORT:-8765}"\nexec python -m uvicorn app:app --port "$PORT"\n')
        found = discover.suggest(self.root)
        self.assertEqual("compose", found["kind"])
        self.assertEqual(8765, found["base_port"])
        self.assertEqual("medium", found["confidence"])

    def test_published_port_parsing(self):
        self.assertEqual(3000, discover._published_port('"3000:3000"'))
        self.assertEqual(8080, discover._published_port(" 127.0.0.1:8080:80 "))
        self.assertEqual(3030, discover._published_port('"${GALLERY_PORT:-3030}:3000"'))
        self.assertEqual(5000, discover._published_port("5000:5000/udp"))
        self.assertIsNone(discover._published_port("3000"))
        self.assertIsNone(discover._published_port('"${HOST_PORT}:3000"'))

    def test_compose_ports_lists_every_service(self):
        pairs = discover.compose_ports(self.COMPOSE)
        self.assertEqual([("postgres", 5432), ("frontend", 3030)], pairs)


class TestPython(FakeRepo):
    def test_fastapi_uvicorn(self):
        self.write("requirements.txt", "fastapi==0.115.0\nuvicorn[standard]==0.30.6\n")
        self.write("app/main.py", "from fastapi import FastAPI\n\napp = FastAPI(title='x')\n")
        self.write("CLAUDE.md", "Server (dev): `python -m uvicorn app.main:app --port 8123`\n")
        found = discover.suggest(self.root)
        self.assertEqual("transient", found["kind"])
        self.assertEqual("arg", found["port_mode"])
        self.assertIn("app.main:app", found["start_cmd"])
        self.assertIn("--port {port}", found["start_cmd"])
        self.assertEqual(8123, found["base_port"])

    def test_venv_python_is_preferred(self):
        self.write("requirements.txt", "fastapi\nuvicorn\n")
        self.write("main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        self.write(".venv/bin/python", "#!/bin/sh\n")
        found = discover.suggest(self.root)
        self.assertTrue(found["start_cmd"].startswith("./.venv/bin/python "))
        self.assertIn("main:app", found["start_cmd"])

    def test_run_sh_without_requirements(self):
        self.write("panel.py", "print('hi')\n")
        self.write("run.sh", "#!/bin/bash\n# panel on 127.0.0.1:8788\nexec python panel.py\n")
        found = discover.suggest(self.root)
        self.assertEqual("./run.sh", found["start_cmd"])
        self.assertEqual("env", found["port_mode"])
        self.assertEqual(8788, found["base_port"])
        self.assertEqual("low", found["confidence"])

    def test_makefile_target(self):
        self.write("Makefile", ".PHONY: help\nhelp:\n\t@echo hi\nrun:\n\tgo run ./cmd\n")
        found = discover.suggest(self.root)
        self.assertEqual("make run", found["start_cmd"])
        self.assertEqual("low", found["confidence"])

    def test_nothing_recognisable(self):
        self.write("notes.txt", "hello")
        self.assertIsNone(discover.suggest(self.root))
        self.assertIsNone(discover.suggest(os.path.join(self.root, "does-not-exist")))


class TestHelpers(FakeRepo):
    def test_looks_like_project_needs_git_and_a_stack(self):
        self.package()
        self.assertFalse(discover.looks_like_project(self.root))
        self.git()
        self.assertTrue(discover.looks_like_project(self.root))

    def test_looks_like_project_false_without_a_stack(self):
        self.git()
        self.write("notes.txt", "x")
        self.assertFalse(discover.looks_like_project(self.root))

    def test_suggest_marks_a_missing_git_dir(self):
        self.package()
        self.assertTrue(any(".git" in e for e in discover.suggest(self.root)["evidence"]))

    def test_detect_port_from_repo(self):
        self.package(dev="nuxt dev")
        self.write("README.md", "open http://localhost:4321 when it is up\n")
        self.assertEqual(4321, discover.detect_port_from_repo(self.root))
        self.assertIsNone(discover.detect_port_from_repo("/nonexistent/path"))

    def test_scan(self):
        root = tempfile.mkdtemp(prefix="portboard-test-scan-")
        for name in ("one", "two", ".hidden", "node_modules", "boring"):
            os.makedirs(os.path.join(root, name, "sub"), exist_ok=True)
        for name in ("one", "two", ".hidden", "node_modules"):
            with open(os.path.join(root, name, "package.json"), "w") as fh:
                json.dump({"scripts": {"dev": "vite"}}, fh)
        with open(os.path.join(root, "boring", "notes.txt"), "w") as fh:
            fh.write("x")
        found = discover.scan(root)
        self.assertEqual(["one", "two"], sorted(f["name"] for f in found))
        self.assertTrue(all(any(".git" in e for e in f["evidence"]) for f in found))

    def test_scan_deeper(self):
        root = tempfile.mkdtemp(prefix="portboard-test-scan2-")
        inner = os.path.join(root, "mono", "web")
        os.makedirs(inner)
        with open(os.path.join(inner, "package.json"), "w") as fh:
            json.dump({"scripts": {"dev": "vite"}}, fh)
        self.assertEqual([], discover.scan(root, max_depth=1))
        self.assertEqual(["web"], [f["name"] for f in discover.scan(root, max_depth=2)])


class TestMaven(FakeRepo):
    def test_maven_spring_boot_detection(self):
        self.write("pom.xml", "<project></project>\n")
        self.write("mvnw", "#!/bin/sh\n")
        found = discover.suggest(self.root)
        self.assertEqual("transient", found["kind"])
        self.assertEqual("env", found["port_mode"])
        self.assertEqual("./mvnw spring-boot:run", found["start_cmd"])
        self.assertEqual("medium", found["confidence"])
        self.assertTrue(any("pom.xml + mvnw" in e for e in found["evidence"]))
        self.assertIsNone(found["base_port"])

    def test_maven_without_mvnw_is_not_spring_boot(self):
        self.write("pom.xml", "<project></project>\n")
        found = discover.suggest(self.root)
        self.assertNotEqual("./mvnw spring-boot:run", (found or {}).get("start_cmd"))

    def test_maven_server_port_in_application_properties(self):
        self.write("pom.xml", "<project></project>\n")
        self.write("mvnw", "#!/bin/sh\n")
        self.write("src/main/resources/application.properties", "server.port=8081\n")
        found = discover.suggest(self.root)
        self.assertEqual(8081, found["base_port"])
        self.assertEqual("medium", found["confidence"])
        self.assertTrue(any("server.port=8081" in e for e in found["evidence"]))

    def test_maven_server_port_in_application_yml(self):
        self.write("pom.xml", "<project></project>\n")
        self.write("mvnw", "#!/bin/sh\n")
        self.write("src/main/resources/application.yml", "server:\n  port: 8082\n")
        found = discover.suggest(self.root)
        self.assertEqual(8082, found["base_port"])

    def test_maven_server_port_in_config_dir(self):
        self.write("pom.xml", "<project></project>\n")
        self.write("mvnw", "#!/bin/sh\n")
        self.write("config/application.properties", "server.port=8083\n")
        found = discover.suggest(self.root)
        self.assertEqual(8083, found["base_port"])


class TestGroups(unittest.TestCase):
    """suggest_group / group_of / pick_primary: one directory of sibling repos."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="portboard-test-group-")

    def child_dir(self, name: str) -> str:
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        return path

    def git(self, path: str) -> None:
        os.makedirs(os.path.join(path, ".git"), exist_ok=True)

    def nuxt_child(self, name: str) -> str:
        """A Nuxt child: package.json with {"scripts": {"dev": "nuxt"}}, and .git."""
        path = self.child_dir(name)
        self.git(path)
        with open(os.path.join(path, "package.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": name, "scripts": {"dev": "nuxt"}}, fh)
        return path

    def spring_child(self, name: str) -> str:
        """A Spring child: pom.xml + mvnw, and .git."""
        path = self.child_dir(name)
        self.git(path)
        open(os.path.join(path, "pom.xml"), "w").close()
        open(os.path.join(path, "mvnw"), "w").close()
        return path

    def test_suggest_group_positive(self):
        self.nuxt_child("admin-app")
        self.spring_child("server-side")
        result = discover.suggest_group(self.root)
        self.assertIsNotNone(result)
        self.assertEqual("group", result["kind"])
        self.assertEqual("none", result["port_mode"])
        self.assertIsNone(result["base_port"])
        self.assertIsNone(result["start_cmd"])
        group_name = discover._name_for(self.root)
        self.assertEqual(group_name, result["name"])
        names = sorted(c["name"] for c in result["children"])
        self.assertEqual(
            sorted([f"{group_name}-admin-app", f"{group_name}-server-side"]), names
        )
        self.assertEqual(f"{group_name}-admin-app", result["primary"])
        self.assertEqual("medium", result["confidence"])

    def test_suggest_group_too_few_children(self):
        self.nuxt_child("admin-app")
        self.assertIsNone(discover.suggest_group(self.root))

    def test_suggest_group_non_project_child_does_not_count(self):
        self.nuxt_child("admin-app")
        # a git repo discover cannot recognise (no manifest at all)
        stray = self.child_dir("random-notes")
        self.git(stray)
        self.assertIsNone(discover.suggest_group(self.root))

    def test_suggest_group_refuses_a_git_directory(self):
        self.git(self.root)  # the root itself looks like a repository
        self.nuxt_child("admin-app")
        self.spring_child("server-side")
        self.assertIsNone(discover.suggest_group(self.root))

    def test_group_of_projects_root_is_none(self):
        self.nuxt_child("admin-app")
        self.spring_child("server-side")
        self.assertIsNone(discover.group_of(self.root, projects_root=self.root))

    def test_group_of_for_a_child_repo(self):
        admin = self.nuxt_child("admin-app")
        self.spring_child("server-side")
        result = discover.group_of(admin, projects_root=os.path.dirname(self.root))
        self.assertIsNotNone(result)
        self.assertEqual("group", result["kind"])
        self.assertEqual(discover._name_for(self.root), result["name"])

    def test_group_of_for_the_group_dir_itself(self):
        self.nuxt_child("admin-app")
        self.spring_child("server-side")
        result = discover.group_of(self.root, projects_root=os.path.dirname(self.root))
        self.assertIsNotNone(result)
        self.assertEqual("group", result["kind"])

    def test_group_of_plain_top_level_repo_is_none(self):
        projects_root = tempfile.mkdtemp(prefix="portboard-test-proot-")
        repo = os.path.join(projects_root, "solo-repo")
        os.makedirs(os.path.join(repo, ".git"))
        self.assertIsNone(discover.group_of(repo, projects_root=projects_root))

    def test_scan_reports_the_group_not_its_children(self):
        outer = tempfile.mkdtemp(prefix="portboard-test-scanroot-")
        group_dir = os.path.join(outer, "rma")
        os.makedirs(group_dir)
        admin = os.path.join(group_dir, "admin-app")
        os.makedirs(os.path.join(admin, ".git"))
        with open(os.path.join(admin, "package.json"), "w", encoding="utf-8") as fh:
            json.dump({"scripts": {"dev": "nuxt"}}, fh)
        server = os.path.join(group_dir, "server-side")
        os.makedirs(os.path.join(server, ".git"))
        open(os.path.join(server, "pom.xml"), "w").close()
        open(os.path.join(server, "mvnw"), "w").close()
        found = discover.scan(outer, max_depth=1)
        self.assertEqual(1, len(found))
        self.assertEqual("group", found[0]["kind"])
        self.assertEqual("rma", found[0]["name"])


class TestPrimaryPick(unittest.TestCase):
    def test_admin_beats_server(self):
        self.assertGreater(
            discover.primary_score("rma-admin-app"), discover.primary_score("rma-server-side")
        )

    def test_app_beats_api(self):
        self.assertGreater(discover.primary_score("rma-app"), discover.primary_score("rma-api"))

    def test_pick_primary_admin_over_server(self):
        children = [
            {"name": "rma-server-side", "base_port": 4010, "kind": "transient"},
            {"name": "rma-admin-app", "base_port": 4020, "kind": "transient"},
        ]
        self.assertEqual("rma-admin-app", discover.pick_primary(children)["name"])

    def test_pick_primary_ties_break_by_lowest_port(self):
        children = [
            {"name": "rma-app-a", "base_port": 4020, "kind": "transient"},
            {"name": "rma-app-b", "base_port": 4010, "kind": "transient"},
        ]
        self.assertEqual(
            discover.primary_score("rma-app-a", 4020, "transient"),
            discover.primary_score("rma-app-b", 4010, "transient"),
        )
        self.assertEqual("rma-app-b", discover.pick_primary(children)["name"])

    def test_pick_primary_empty_list(self):
        self.assertIsNone(discover.pick_primary([]))


# --------------------------------------------------------------------------
# Read-only checks against the real repositories on this workstation.
# --------------------------------------------------------------------------

REAL_REPOS = {
    "/mnt/hyper/Projects/sheron": {
        "kind": "transient", "start_cmd": "npm run dev", "port_mode": "env", "base_port": None,
    },
    "/mnt/hyper/Projects/gallery": {
        "kind": "transient", "start_cmd": "npm run dev", "port_mode": "env", "base_port": 3005,
    },
    "/mnt/hyper/Projects/hr-app": {
        "kind": "compose", "start_cmd": None, "port_mode": "fixed", "base_port": 3000,
    },
    "/mnt/hyper/Projects/valadio-accounting-cockpit": {
        "kind": "transient", "port_mode": "arg", "base_port": 8000,
    },
    "/mnt/hyper/Projects/freetoken-panel": {
        "kind": "transient", "start_cmd": "./run.sh", "port_mode": "env", "base_port": 8788,
    },
    "/mnt/hyper/Projects/Colibri/web": {
        "kind": "transient", "start_cmd": "npm run dev", "port_mode": "env", "base_port": 5173,
    },
    "/mnt/hyper/Projects/timelined-moodboard": {
        "kind": "transient", "start_cmd": "npm run dev", "port_mode": "env", "base_port": 3000,
    },
    "/mnt/hyper/Projects/whisper": {
        "kind": "compose", "port_mode": "fixed", "base_port": 8765,
    },
}


class TestRealRepositories(unittest.TestCase):
    """Never writes into the repositories and never runs their commands."""

    def test_real_repositories(self):
        checked = 0
        for path, expected in REAL_REPOS.items():
            if not os.path.isdir(path):
                continue
            with self.subTest(repo=path):
                found = discover.suggest(path)
                self.assertIsNotNone(found, f"{path} was not recognised")
                for key, value in expected.items():
                    self.assertEqual(value, found[key], f"{path}: {key}")
                self.assertTrue(found["evidence"])
                checked += 1
        if checked == 0:
            self.skipTest("none of the reference repositories exist on this machine")

    def test_valadio_uvicorn_command(self):
        path = "/mnt/hyper/Projects/valadio-accounting-cockpit"
        if not os.path.isdir(path):
            self.skipTest("valadio-accounting-cockpit is missing")
        found = discover.suggest(path)
        self.assertIn("uvicorn app.main:app", found["start_cmd"])
        self.assertIn("--port {port}", found["start_cmd"])

    def test_colibri_web_is_not_a_git_repo_of_its_own(self):
        path = "/mnt/hyper/Projects/Colibri/web"
        if not os.path.isdir(path):
            self.skipTest("Colibri/web is missing")
        self.assertFalse(discover.looks_like_project(path))
        self.assertIsNotNone(discover.suggest(path))

    def test_sheron_is_a_project(self):
        path = "/mnt/hyper/Projects/sheron"
        if not os.path.isdir(path):
            self.skipTest("sheron is missing")
        self.assertTrue(discover.looks_like_project(path))


if __name__ == "__main__":
    unittest.main()
