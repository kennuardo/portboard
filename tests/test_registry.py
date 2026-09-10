"""Unit tests for portboard.registry.

The state directory is redirected to a temporary directory BEFORE portboard is
imported, because config resolves DB_PATH at import time.
"""
from __future__ import annotations

import os
import socket
import tempfile
import unittest

os.environ.setdefault("PORTBOARD_STATE_DIR", tempfile.mkdtemp(prefix="portboard-test-registry-"))

from portboard import config, db, registry  # noqa: E402  (after the env override)


def free_port(conn, start: int = 20000, end: int = 26000, skip: set[int] | None = None) -> int:
    """A port below 32768 that nothing holds right now."""
    skip = skip or set()
    for port in range(start, end):
        if port not in skip and registry.port_is_free(conn, port):
            return port
    raise unittest.SkipTest("no free port in the test range")


class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
            if path.exists():
                path.unlink()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.mkdtemp(prefix="portboard-test-tree-")

    # helpers -------------------------------------------------------------
    def make_dir(self, *parts: str) -> str:
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def add(self, name: str = "demo", **kwargs) -> dict:
        path = kwargs.pop("path", None) or self.make_dir(name)
        return registry.add_project(self.conn, path, name=name, **kwargs)


class TestProjects(RegistryTestCase):
    def test_add_allocates_from_the_pool_and_creates_main(self):
        project = self.add("alpha")
        start = db.get_int_setting(self.conn, "pool_start")
        end = db.get_int_setting(self.conn, "pool_end")
        step = db.get_int_setting(self.conn, "pool_step")
        self.assertIsNotNone(project["base_port"])
        self.assertTrue(start <= project["base_port"] <= end)
        self.assertEqual(0, (project["base_port"] - start) % step)

        instances = registry.list_instances(self.conn, project["id"])
        self.assertEqual(1, len(instances))
        main = instances[0]
        self.assertEqual("main", main["label"])
        self.assertEqual(0, main["slot"])
        self.assertEqual(project["path"], main["path"])
        self.assertEqual(project["base_port"], main["port"])
        self.assertFalse(main["worktree"])
        self.assertEqual("alpha", main["project"])
        self.assertEqual(f"http://localhost:{project['base_port']}/", main["url"])

    def test_two_projects_get_different_blocks(self):
        first = self.add("alpha")
        second = self.add("beta")
        step = db.get_int_setting(self.conn, "pool_step")
        self.assertEqual(step, second["base_port"] - first["base_port"])

    def test_explicit_port_is_kept(self):
        port = free_port(self.conn)
        project = self.add("alpha", base_port=port)
        self.assertEqual(port, project["base_port"])
        self.assertEqual(port, registry.find_by_ref(self.conn, "alpha")["port"])

    def test_explicit_busy_port_is_refused(self):
        port = free_port(self.conn)
        self.add("alpha", base_port=port)
        with self.assertRaises(registry.RegistryError):
            self.add("beta", base_port=port)

    def test_duplicate_name_and_path_are_refused(self):
        project = self.add("alpha")
        with self.assertRaises(registry.RegistryError):
            registry.add_project(self.conn, self.make_dir("elsewhere"), name="alpha")
        with self.assertRaises(registry.RegistryError):
            registry.add_project(self.conn, project["path"], name="other")

    def test_bad_kind_and_port_mode(self):
        with self.assertRaises(registry.RegistryError):
            self.add("alpha", kind="magic")
        with self.assertRaises(registry.RegistryError):
            self.add("beta", port_mode="telepathy")

    def test_name_is_derived_from_the_basename(self):
        path = self.make_dir("My Weird.Repo")
        project = registry.add_project(self.conn, path)
        self.assertEqual("my-weird-repo", project["name"])

    def test_port_mode_none_means_no_port(self):
        project = self.add("alpha", port_mode="none", kind="none")
        self.assertIsNone(project["base_port"])
        main = registry.find_by_ref(self.conn, "alpha")
        self.assertIsNone(main["port"])
        self.assertIsNone(main["url"])

    def test_unknown_fields_are_ignored(self):
        # discover.suggest() output is passed straight through by other modules
        project = self.add("alpha", confidence="high", evidence=["x"], open_path="/admin")
        self.assertEqual("/admin", project["open_path"])
        main = registry.find_by_ref(self.conn, "alpha")
        self.assertTrue(main["url"].endswith("/admin"))

    def test_update_project_moves_the_main_port(self):
        project = self.add("alpha")
        port = free_port(self.conn, skip={project["base_port"]})
        updated = registry.update_project(self.conn, project["id"], base_port=port, pinned=1)
        self.assertEqual(port, updated["base_port"])
        self.assertEqual(1, updated["pinned"])
        self.assertEqual(port, registry.find_by_ref(self.conn, "alpha")["port"])

    def test_delete_project_cascades(self):
        project = self.add("alpha")
        registry.ensure_instance(
            self.conn, project["id"],
            os.path.join(project["path"], config.WORKTREE_DIRNAME, "wt"),
        )
        registry.delete_project(self.conn, project["id"])
        self.assertIsNone(registry.get_project(self.conn, "alpha"))
        self.assertEqual([], registry.list_instances(self.conn))


class TestOrder(RegistryTestCase):
    def test_list_follows_manual_order_then_name(self):
        a, b, c = self.add("alpha"), self.add("beta"), self.add("gamma")
        self.assertEqual([p["name"] for p in registry.list_projects(self.conn)], ["alpha", "beta", "gamma"])
        registry.reorder_projects(self.conn, [c["id"], a["id"]])
        self.assertEqual([p["name"] for p in registry.list_projects(self.conn)], ["gamma", "alpha", "beta"])
        orders = {p["name"]: p["sort_order"] for p in registry.list_projects(self.conn)}
        self.assertEqual(orders, {"gamma": 0, "alpha": 1, "beta": 2})

    def test_reorder_rejects_unknown_and_bad_ids(self):
        self.add("alpha")
        with self.assertRaises(ValueError):
            registry.reorder_projects(self.conn, [999])
        with self.assertRaises(ValueError):
            registry.reorder_projects(self.conn, ["x"])

    def test_old_database_gains_the_column(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT)")
        db._migrate(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        self.assertIn("sort_order", cols)
        db._migrate(conn)  # idempotent
        conn.close()


class TestInstances(RegistryTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.add("alpha")
        self.wt_root = os.path.join(self.project["path"], config.WORKTREE_DIRNAME)

    def worktree(self, slug: str) -> str:
        path = os.path.join(self.wt_root, slug)
        os.makedirs(path, exist_ok=True)
        return path

    def test_ensure_instance_main_is_idempotent(self):
        first = registry.ensure_instance(self.conn, self.project["id"], self.project["path"])
        second = registry.ensure_instance(self.conn, self.project["id"], self.project["path"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual("main", first["label"])
        self.assertEqual(1, len(registry.list_instances(self.conn, self.project["id"])))

    def test_worktree_gets_the_next_slot(self):
        one = registry.ensure_instance(self.conn, self.project["id"], self.worktree("fix-login"))
        two = registry.ensure_instance(self.conn, self.project["id"], self.worktree("redesign"))
        self.assertEqual(("fix-login", 1, self.project["base_port"] + 1),
                         (one["label"], one["slot"], one["port"]))
        self.assertEqual(("redesign", 2, self.project["base_port"] + 2),
                         (two["label"], two["slot"], two["port"]))
        self.assertTrue(one["worktree"])
        again = registry.ensure_instance(self.conn, self.project["id"], self.worktree("fix-login"))
        self.assertEqual(one["id"], again["id"])
        self.assertEqual(one["port"], again["port"])

    def test_worktree_falls_back_when_the_block_is_exhausted(self):
        db.set_setting(self.conn, "slots", "1")
        project = self.add("narrow")
        registry.update_project(self.conn, project["id"], slots=1)
        project = registry.get_project(self.conn, "narrow")
        root = os.path.join(project["path"], config.WORKTREE_DIRNAME)
        os.makedirs(os.path.join(root, "one"), exist_ok=True)
        os.makedirs(os.path.join(root, "two"), exist_ok=True)
        one = registry.ensure_instance(self.conn, project["id"], os.path.join(root, "one"))
        two = registry.ensure_instance(self.conn, project["id"], os.path.join(root, "two"))
        self.assertEqual(project["base_port"] + 1, one["port"])
        self.assertEqual(2, two["slot"])
        self.assertNotEqual(project["base_port"] + 2, two["port"])
        self.assertTrue(
            db.get_int_setting(self.conn, "pool_start")
            <= two["port"]
            <= db.get_int_setting(self.conn, "pool_end")
        )

    def test_ensure_instance_rejects_a_foreign_path(self):
        with self.assertRaises(registry.RegistryError):
            registry.ensure_instance(self.conn, self.project["id"], self.make_dir("outside"))

    def test_main_instance_cannot_be_deleted(self):
        main = registry.find_by_ref(self.conn, "alpha")
        with self.assertRaises(registry.RegistryError):
            registry.delete_instance(self.conn, main["id"])
        worktree = registry.ensure_instance(self.conn, self.project["id"], self.worktree("tmp"))
        registry.delete_instance(self.conn, worktree["id"])
        self.assertIsNone(registry.get_instance(self.conn, worktree["id"]))

    def test_update_instance_only_touches_known_columns(self):
        main = registry.find_by_ref(self.conn, "alpha")
        updated = registry.update_instance(self.conn, main["id"], state="running", pid=4242,
                                           nonsense="ignored")
        self.assertEqual("running", updated["state"])
        self.assertEqual(4242, updated["pid"])
        self.assertNotIn("nonsense", updated)

    def test_owner_claim_and_release(self):
        main = registry.find_by_ref(self.conn, "alpha")
        claimed = registry.set_owner(self.conn, main["id"], "sess-1")
        self.assertEqual("sess-1", claimed["owner_session"])
        self.assertEqual(0, registry.release_owner(self.conn, session_id="other"))
        self.assertEqual(1, registry.release_owner(self.conn, session_id="sess-1"))
        self.assertIsNone(registry.get_instance(self.conn, main["id"])["owner_session"])
        registry.set_owner(self.conn, main["id"], "sess-2")
        self.assertEqual(1, registry.release_owner(self.conn, instance_id=main["id"]))
        self.assertEqual(0, registry.release_owner(self.conn))

    def test_find_by_ref(self):
        worktree = registry.ensure_instance(self.conn, self.project["id"], self.worktree("fix"))
        self.assertEqual(worktree["id"], registry.find_by_ref(self.conn, "alpha@fix")["id"])
        self.assertEqual(worktree["id"], registry.find_by_ref(self.conn, worktree["id"])["id"])
        self.assertEqual("main", registry.find_by_ref(self.conn, "alpha")["label"])
        with self.assertRaises(registry.RegistryError):
            registry.find_by_ref(self.conn, "alpha@nope")
        with self.assertRaises(registry.RegistryError):
            registry.find_by_ref(self.conn, "ghost")
        with self.assertRaises(registry.RegistryError):
            registry.find_by_ref(self.conn, 9999)


class TestPathMapping(RegistryTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.add("alpha")
        self.wt = os.path.join(self.project["path"], config.WORKTREE_DIRNAME, "fix-login")
        os.makedirs(os.path.join(self.wt, "app"), exist_ok=True)
        os.makedirs(os.path.join(self.project["path"], "app", "pages"), exist_ok=True)

    def test_main_path(self):
        found = registry.resolve_path(self.conn, self.project["path"])
        self.assertEqual("main", found.label)
        self.assertFalse(found.is_worktree)
        self.assertIsNone(found.slug)
        self.assertEqual(self.project["id"], found.project["id"])
        self.assertEqual(0, found.instance["slot"])

    def test_nested_file_maps_to_main(self):
        nested = os.path.join(self.project["path"], "app", "pages", "index.vue")
        open(nested, "w").close()
        found = registry.resolve_path(self.conn, nested)
        self.assertEqual("main", found.label)
        self.assertEqual(self.project["id"], found.project["id"])

    def test_worktree_path_without_a_row(self):
        found = registry.resolve_path(self.conn, os.path.join(self.wt, "app"))
        self.assertTrue(found.is_worktree)
        self.assertEqual("fix-login", found.slug)
        self.assertEqual("fix-login", found.label)
        self.assertIsNone(found.instance)

    def test_worktree_path_with_a_row(self):
        instance = registry.ensure_instance(self.conn, self.project["id"], self.wt)
        found = registry.resolve_path(self.conn, self.wt)
        self.assertIsNotNone(found.instance)
        self.assertEqual(instance["id"], found.instance["id"])
        self.assertTrue(found.instance["worktree"])

    def test_outside_path(self):
        found = registry.resolve_path(self.conn, self.make_dir("nothing-here"))
        self.assertIsNone(found.project)
        self.assertIsNone(found.instance)
        self.assertIsNone(found.label)
        self.assertTrue(found.path.endswith("nothing-here"))

    def test_nested_project_wins(self):
        inner_path = os.path.join(self.project["path"], "packages", "inner")
        os.makedirs(inner_path, exist_ok=True)
        inner = registry.add_project(self.conn, inner_path, name="inner")
        found = registry.resolve_path(self.conn, os.path.join(inner_path, "src"))
        self.assertEqual(inner["id"], found.project["id"])


class TestGroups(RegistryTestCase):
    """kind='group' projects: children carry parent_id, the group mirrors its
    primary child's main instance."""

    def make_group(self, name: str = "rma", **kwargs) -> dict:
        path = self.make_dir(name)
        return registry.add_project(self.conn, path, name=name, kind="group",
                                    port_mode="none", **kwargs)

    def child(self, group: dict, basename: str, **kwargs) -> dict:
        path = self.make_dir(group["name"], basename)
        return registry.add_project(self.conn, path, parent_id=group["id"], **kwargs)

    # naming and validation ------------------------------------------------

    def test_child_without_name_gets_group_prefixed_name(self):
        group = self.make_group("rma")
        child = self.child(group, "admin-app")
        self.assertEqual("rma-admin-app", child["name"])
        self.assertEqual(group["id"], child["parent_id"])

    def test_parent_must_be_a_group(self):
        alpha = self.add("alpha")
        with self.assertRaises(registry.RegistryError):
            registry.add_project(self.conn, self.make_dir("child"), parent_id=alpha["id"])

    def test_nested_groups_are_refused(self):
        outer = self.make_group("outer")
        with self.assertRaises(registry.RegistryError):
            registry.add_project(self.conn, self.make_dir("inner"), kind="group",
                                 parent_id=outer["id"])

    def test_child_of_a_child_is_refused(self):
        outer = self.make_group("outer")
        mid = self.child(outer, "mid")
        with self.assertRaises(registry.RegistryError):
            registry.add_project(self.conn, self.make_dir("outer", "mid", "leaf"),
                                 parent_id=mid["id"])

    def test_group_cannot_be_its_own_parent(self):
        group = self.make_group("rma")
        with self.assertRaises(registry.RegistryError):
            registry.update_project(self.conn, group["id"], parent_id=group["id"])

    def test_group_forces_no_port_and_no_start_cmd(self):
        port = free_port(self.conn)
        group = registry.add_project(
            self.conn, self.make_dir("rma3"), name="rma3", kind="group",
            port_mode="env", base_port=port, start_cmd="echo hi",
        )
        self.assertEqual("none", group["port_mode"])
        self.assertIsNone(group["base_port"])
        self.assertIsNone(group["start_cmd"])
        main = registry.find_by_ref(self.conn, "rma3")
        self.assertIsNone(main["port"])
        self.assertIsNone(main["url"])

    # decoration -------------------------------------------------------------

    def test_group_decoration_fields(self):
        group = self.make_group("rma")
        admin = self.child(group, "admin-app")
        server = self.child(group, "server-side")
        reloaded = registry.get_project(self.conn, group["id"])
        self.assertEqual(sorted([admin["id"], server["id"]]), sorted(reloaded["children"]))
        self.assertEqual(
            sorted(["rma-admin-app", "rma-server-side"]), sorted(reloaded["child_names"])
        )
        self.assertEqual("rma-admin-app", reloaded["primary"])  # heuristic: admin beats server
        self.assertEqual(admin["id"], reloaded["primary_child_id"])
        self.assertIsNone(reloaded["parent"])
        admin_reloaded = registry.get_project(self.conn, admin["id"])
        self.assertEqual("rma", admin_reloaded["parent"])

    # primary_child ----------------------------------------------------------

    def test_primary_child_explicit_overrides_heuristic(self):
        group = self.make_group("rma")
        admin = self.child(group, "admin-app")
        server = self.child(group, "server-side")
        self.assertEqual("rma-admin-app", registry.get_project(self.conn, group["id"])["primary"])

        registry.update_project(self.conn, group["id"], primary_child=server["id"])
        updated = registry.get_project(self.conn, group["id"])
        self.assertEqual(server["id"], updated["primary_child_id"])
        self.assertEqual("rma-server-side", updated["primary"])

        registry.update_project(self.conn, group["id"], primary_child="rma-admin-app")
        updated = registry.get_project(self.conn, group["id"])
        self.assertEqual(admin["id"], updated["primary_child_id"])
        self.assertEqual("rma-admin-app", updated["primary"])

    def test_primary_child_refuses_a_non_child(self):
        group = self.make_group("rma")
        self.child(group, "admin-app")
        other = self.add("other")
        with self.assertRaises(registry.RegistryError):
            registry.update_project(self.conn, group["id"], primary_child=other["id"])
        with self.assertRaises(registry.RegistryError):
            registry.update_project(self.conn, group["id"], primary_child="does-not-exist")

    def test_group_start_order_primary_last(self):
        group = self.make_group("rma")
        self.child(group, "api")
        self.child(group, "mariadb")
        self.child(group, "admin-app")
        order = registry.group_start_order(self.conn, registry.get_project(self.conn, group["id"]))
        names = [c["name"] for c in order]
        self.assertEqual({"rma-admin-app", "rma-api", "rma-mariadb"}, set(names))
        self.assertEqual("rma-admin-app", names[-1])  # heuristic primary goes last

    # instance view mirroring -------------------------------------------------

    def test_instance_view_mirrors_the_primary_child(self):
        group = self.make_group("rma")
        admin = self.child(group, "admin-app")
        self.child(group, "server-side")
        admin_main = registry.find_by_ref(self.conn, admin["name"])
        registry.update_instance(
            self.conn, admin_main["id"], state="running",
            actual_port=admin["base_port"], pid=4242,
        )
        group_main = registry.find_by_ref(self.conn, "rma")
        self.assertEqual("running", group_main["state"])
        self.assertEqual(admin["base_port"], group_main["port"])
        self.assertEqual(admin["base_port"], group_main["actual_port"])
        self.assertEqual(4242, group_main["pid"])
        self.assertEqual(f"http://localhost:{admin['base_port']}/", group_main["url"])
        self.assertEqual("rma-admin-app", group_main["primary"])
        self.assertEqual(admin_main["id"], group_main["primary_instance_id"])
        self.assertEqual(2, group_main["services_total"])
        self.assertEqual(1, group_main["services_running"])
        service_names = sorted(s["project"] for s in group_main["services"])
        self.assertEqual(["rma-admin-app", "rma-server-side"], service_names)

    # path mapping -------------------------------------------------------------

    def test_resolve_path_group(self):
        group = self.make_group("rma")
        admin = self.child(group, "admin-app")

        resolved = registry.resolve_path(self.conn, admin["path"])
        self.assertEqual(admin["id"], resolved.project["id"])
        self.assertIsNotNone(resolved.group)
        self.assertEqual(group["id"], resolved.group["id"])

        nested = os.path.join(admin["path"], "src")
        os.makedirs(nested, exist_ok=True)
        resolved_nested = registry.resolve_path(self.conn, nested)
        self.assertEqual(admin["id"], resolved_nested.project["id"])
        self.assertEqual(group["id"], resolved_nested.group["id"])

        outside_child = self.make_dir("rma", "docs")
        resolved_group = registry.resolve_path(self.conn, outside_child)
        self.assertEqual(group["id"], resolved_group.project["id"])
        self.assertIsNone(resolved_group.group)

    # delete cascade -------------------------------------------------------------

    def test_delete_group_cascades_to_children(self):
        group = self.make_group("rma")
        child = self.child(group, "admin-app")
        registry.delete_project(self.conn, group["id"])
        self.assertIsNone(registry.get_project(self.conn, group["id"]))
        self.assertIsNone(registry.get_project(self.conn, child["id"]))
        self.assertEqual([], registry.list_instances(self.conn))

    # export / import -------------------------------------------------------------

    def test_export_import_round_trip_preserves_parent_and_primary(self):
        group = self.make_group("rma")
        admin = self.child(group, "admin-app")
        server = self.child(group, "server-side")
        registry.update_project(self.conn, group["id"], primary_child=server["id"])

        dumped = registry.export_json(self.conn)
        names_order = [p["name"] for p in dumped["projects"]]
        self.assertEqual("rma", names_order[0])  # groups exported before children

        registry.delete_project(self.conn, group["id"])
        self.assertEqual([], registry.list_projects(self.conn))

        summary = registry.import_json(self.conn, dumped)
        self.assertEqual([], summary["errors"])

        restored_group = registry.get_project(self.conn, "rma")
        self.assertEqual("rma-server-side", restored_group["primary"])
        restored_admin = registry.get_project(self.conn, "rma-admin-app")
        self.assertEqual("rma", restored_admin["parent"])

    # add_group ------------------------------------------------------------------

    def test_add_group_registers_children_from_a_suggestion(self):
        group_path = self.make_dir("rma")
        admin_path = self.make_dir("rma", "admin-app")
        server_path = self.make_dir("rma", "server-side")
        suggestion = {
            "path": group_path, "name": "rma", "kind": "group", "port_mode": "none",
            "children": [
                {"path": admin_path, "name": "rma-admin-app", "kind": "transient",
                 "start_cmd": "npm run dev", "port_mode": "env"},
                {"path": server_path, "name": "rma-server-side", "kind": "transient",
                 "start_cmd": "./mvnw spring-boot:run", "port_mode": "env"},
            ],
            "primary": "rma-admin-app",
        }
        group = registry.add_group(self.conn, suggestion)
        self.assertEqual("group", group["kind"])
        self.assertIsNone(group["base_port"])
        self.assertEqual(2, len(group["children"]))
        self.assertEqual("rma-admin-app", group["primary"])

    def test_add_group_reparents_an_already_registered_child(self):
        admin_path = self.make_dir("standalone-admin")
        existing = registry.add_project(self.conn, admin_path, name="rma-admin-app")
        group_path = self.make_dir("rma")
        server_path = self.make_dir("rma", "server-side")
        suggestion = {
            "path": group_path, "name": "rma", "kind": "group", "port_mode": "none",
            "children": [
                {"path": admin_path, "name": "rma-admin-app"},
                {"path": server_path, "name": "rma-server-side", "kind": "transient",
                 "start_cmd": "true"},
            ],
            "primary": "rma-admin-app",
        }
        group = registry.add_group(self.conn, suggestion)
        reloaded_admin = registry.get_project(self.conn, existing["id"])
        self.assertEqual(group["id"], reloaded_admin["parent_id"])
        self.assertEqual("rma-admin-app", group["primary"])
        self.assertEqual(
            sorted(["rma-admin-app", "rma-server-side"]), sorted(group["child_names"])
        )


class TestPorts(RegistryTestCase):
    def test_port_is_free_sees_a_real_listener(self):
        port = free_port(self.conn)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        self.addCleanup(sock.close)
        self.assertFalse(registry.port_is_free(self.conn, port))

    def test_port_is_free_respects_the_database(self):
        project = self.add("alpha")
        self.assertFalse(registry.port_is_free(self.conn, project["base_port"]))
        main = registry.find_by_ref(self.conn, "alpha")
        self.assertTrue(
            registry.port_is_free(self.conn, project["base_port"], exclude_instance_id=main["id"])
        )
        reserved = free_port(self.conn)
        self.conn.execute(
            "INSERT INTO reserved_ports(port, label, source, updated_at) VALUES (?,?,?,?)",
            (reserved, "test", "static", db.now()),
        )
        self.assertFalse(registry.port_is_free(self.conn, reserved))

    def test_ephemeral_range_is_refused(self):
        self.assertFalse(registry.port_is_free(self.conn, 32768))
        self.assertFalse(registry.port_is_free(self.conn, 45000))
        self.assertFalse(registry.port_is_free(self.conn, 0))

    def test_observed_ports_are_not_allocated(self):
        port = free_port(self.conn)
        self.conn.execute(
            "INSERT INTO observed(port, proto, bind, seen_at) VALUES (?,?,?,?)",
            (port, "tcp", "127.0.0.1", db.now()),
        )
        self.assertFalse(registry.port_is_free(self.conn, port))


class TestMisc(RegistryTestCase):
    def test_safe_label(self):
        self.assertEqual("fix-login", registry.safe_label("fix-login"))
        self.assertEqual("fix-login-timeout", registry.safe_label("Fix Login/Timeout!"))
        self.assertEqual("a-b", registry.safe_label("  a___b  "))
        self.assertEqual("x", registry.safe_label("!!!"))
        self.assertEqual("x", registry.safe_label(None))

    def test_git_branch_of_a_non_repo(self):
        self.assertIsNone(registry.git_branch(self.make_dir("plain")))

    def test_url_for(self):
        self.assertEqual("http://localhost:3000/", registry.url_for({}, {"port": 3000}))
        self.assertEqual(
            "http://localhost:3000/admin", registry.url_for({"open_path": "/admin"}, {"port": 3000})
        )
        self.assertIsNone(registry.url_for({}, {"port": None}))

    def test_events_are_recorded(self):
        project = self.add("alpha")
        registry.delete_project(self.conn, project["id"])
        kinds = [r["kind"] for r in self.conn.execute("SELECT kind FROM events ORDER BY id")]
        self.assertEqual(["project.add", "instance.add", "project.delete"], kinds)

    def test_state_snapshot(self):
        self.add("alpha")
        snapshot = registry.state_snapshot(self.conn)
        self.assertEqual(
            {"projects", "observed", "reserved", "settings", "reconciled_at"}, set(snapshot)
        )
        self.assertEqual(1, len(snapshot["projects"]))
        self.assertEqual(1, len(snapshot["projects"][0]["instances"]))
        self.assertIn("pool_start", snapshot["settings"])

    def test_export_import_round_trip(self):
        project = self.add("alpha", start_cmd="npm run dev")
        wt = os.path.join(project["path"], config.WORKTREE_DIRNAME, "fix-login")
        os.makedirs(wt, exist_ok=True)
        registry.ensure_instance(self.conn, project["id"], wt)
        dumped = registry.export_json(self.conn)
        self.assertEqual(1, len(dumped["projects"]))
        self.assertEqual(2, len(dumped["projects"][0]["instances"]))

        registry.delete_project(self.conn, project["id"])
        summary = registry.import_json(self.conn, dumped)
        self.assertEqual(1, summary["projects_added"])
        self.assertEqual(1, summary["instances_added"])
        self.assertEqual([], summary["errors"])

        restored = registry.get_project(self.conn, "alpha")
        self.assertEqual(project["path"], restored["path"])
        self.assertEqual(project["base_port"], restored["base_port"])
        self.assertEqual("npm run dev", restored["start_cmd"])
        labels = sorted(i["label"] for i in registry.list_instances(self.conn, restored["id"]))
        self.assertEqual(["fix-login", "main"], labels)

    def test_import_replace_drops_old_projects(self):
        self.add("alpha")
        dumped = registry.export_json(self.conn)
        self.add("beta")
        summary = registry.import_json(self.conn, dumped, replace=True)
        self.assertEqual(1, summary["projects_added"])
        self.assertEqual(["alpha"], [p["name"] for p in registry.list_projects(self.conn)])


if __name__ == "__main__":
    unittest.main()
