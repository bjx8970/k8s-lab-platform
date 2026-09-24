import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from migrations.runner import discover_migrations
from modules import db, status_cache
from modules.vm_identity import VmIdentityError, positive_id, vm_identity_key
from tests.test_security_contract import app_module


class VmIdentityHelperTests(unittest.TestCase):
    def test_positive_ids_and_stable_key(self):
        self.assertEqual(7, positive_id("7"))
        self.assertEqual("pve-7-vm-101", vm_identity_key(7, 101))
        for value in (None, True, 0, -1, "0", "01", " 7", 7.0):
            with self.subTest(value=value):
                with self.assertRaises(VmIdentityError):
                    positive_id(value)

    def test_v0002_python_and_sql_are_checksummed_without_v0001_changes(self):
        migrations = discover_migrations()
        self.assertEqual(["0001_control_plane_p0", "0002_pve_vm_identity"],
                         [migration.revision for migration in migrations])
        self.assertEqual(("v0002_pve_vm_identity.sql",), migrations[1].module.checksum_files)
        ddl = migrations[1].path.with_name("v0002_pve_vm_identity.sql").read_text(encoding="utf-8")
        self.assertIn("uq_vms_pve_server_vmid", ddl)
        self.assertIn("CHECK (pve_server_id > 0)", ddl)
        self.assertIn("pg_constraint", ddl)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            migration = migrations[1]
            (target / migration.path.name).write_bytes(migration.path.read_bytes())
            (target / "v0002_pve_vm_identity.sql").write_bytes(
                migration.path.with_suffix(".sql").read_bytes() + b"\n-- checksum drift")
            self.assertNotEqual(migration.checksum, discover_migrations(target)[0].checksum)


class VmIdentitySqliteTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("CREATE TABLE pve_servers (id INTEGER PRIMARY KEY, name VARCHAR(64) UNIQUE NOT NULL, host VARCHAR(128) NOT NULL, user VARCHAR(64) NOT NULL, token_name VARCHAR(64) NOT NULL, token_value TEXT NOT NULL)")
            connection.exec_driver_sql("CREATE TABLE clusters (id INTEGER PRIMARY KEY, name VARCHAR(32) UNIQUE NOT NULL, pve_server_id INTEGER)")
            connection.exec_driver_sql("CREATE TABLE vms (id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL REFERENCES clusters(id), vm_name VARCHAR(64) NOT NULL, vmid INTEGER NOT NULL UNIQUE, node VARCHAR(32) NOT NULL, role VARCHAR(16), mac VARCHAR(24), ip VARCHAR(16))")
            connection.exec_driver_sql("INSERT INTO pve_servers(id,name,host,user,token_name,token_value) VALUES (7,'pve-7','h7','u','t','v'),(8,'pve-8','h8','u','t','v')")
            connection.exec_driver_sql("INSERT INTO clusters(id,name,pve_server_id) VALUES (1,'k8s_1',7),(2,'k8s_2',8)")
            connection.exec_driver_sql("INSERT INTO vms(id,cluster_id,vm_name,vmid,node) VALUES (1,1,'client-a',101,'node-a')")

    def test_rebuild_allows_same_vmid_on_different_servers_and_rejects_same_server(self):
        db.upgrade_sqlite_vm_identity(self.engine)
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO vms(id,cluster_id,pve_server_id,vm_name,vmid,node) VALUES (2,2,8,'client-b',101,'node-a')"))
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(text("INSERT INTO vms(id,cluster_id,pve_server_id,vm_name,vmid,node) VALUES (3,1,7,'dup',101,'node-b')"))
        with self.engine.connect() as connection:
            rows = connection.execute(text("SELECT pve_server_id,vmid,node FROM vms ORDER BY id")).all()
        self.assertEqual([(7, 101, "node-a"), (8, 101, "node-a")], rows)

    def test_precheck_failure_keeps_original_table(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE clusters SET pve_server_id=0 WHERE id=1")
        with self.assertRaisesRegex(RuntimeError, "缺少有效 PVE server"):
            db.upgrade_sqlite_vm_identity(self.engine)
        with self.engine.connect() as connection:
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(vms)")}
            count = connection.exec_driver_sql("SELECT COUNT(*) FROM vms").scalar()
        self.assertNotIn("pve_server_id", columns)
        self.assertEqual(1, count)


class VmStatusCacheIdentityTests(unittest.TestCase):
    def setUp(self):
        status_cache._vm_cache.clear()

    def tearDown(self):
        status_cache._vm_cache.clear()

    def test_same_vmid_isolated_by_server_and_node_is_mutable_locator(self):
        status_cache.update_vm_status(7, 101, "running", node="node-a")
        status_cache.update_vm_status(8, 101, "stopped", node="node-a")
        self.assertEqual("running", status_cache.get_vm_status(7, 101)["status"])
        self.assertEqual("stopped", status_cache.get_vm_status(8, 101)["status"])
        status_cache.update_vm_status(7, 101, "running", node="node-b")
        entry = status_cache.dump_cache()["entries"]["pve-7-vm-101"]
        self.assertEqual("node-b", entry["node"])
        self.assertEqual(2, status_cache.dump_cache()["count"])


class VmHttpIdentityTests(unittest.TestCase):
    def client_for(self, user_id=1):
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        return client

    def csrf_for(self, client):
        return client.get("/api/csrf-token").get_json()["csrf_token"]

    def test_legacy_route_without_server_id_fails_before_provider(self):
        client = self.client_for()
        provider = MagicMock()
        with patch.object(app_module, "get_pve_client", return_value=provider) as get_client:
            response = client.post(
                "/api/pve/vms/node-a/101/start",
                headers={"X-CSRFToken": self.csrf_for(client)}, json={},
            )
        self.assertEqual(400, response.status_code)
        get_client.assert_not_called()
        provider.start_vm.assert_not_called()

    def test_batch_status_isolates_same_vmid_and_rejects_missing_provider(self):
        client = self.client_for(1)
        clusters = {
            7: {"name": "k8s_7", "pve_server_id": 7,
                "vms": {"client": {"pve_server_id": 7, "vmid": 101, "node": "a"}}},
            8: {"name": "k8s_8", "pve_server_id": 8,
                "vms": {"client": {"pve_server_id": 8, "vmid": 101, "node": "a"}}},
        }
        status_cache.update_vm_status(7, 101, "running", node="a")
        status_cache.update_vm_status(8, 101, "stopped", node="a")
        with (patch.object(app_module, "find_cluster_by_vm", side_effect=lambda sid, vmid: clusters.get(sid)),
              patch.object(app_module, "get_pve_client") as get_client):
            response = client.post("/api/pve/vms/status/batch",
                headers={"X-CSRFToken": self.csrf_for(client)},
                json={"vms": [{"pve_server_id": 7, "vmid": 101},
                              {"pve_server_id": 8, "vmid": 101}]})
            self.assertEqual(200, response.status_code)
            statuses = response.get_json()["statuses"]
            self.assertEqual("running", statuses["pve-7-vm-101"]["status"])
            self.assertEqual("stopped", statuses["pve-8-vm-101"]["status"])
            response = client.post("/api/pve/vms/status/batch",
                headers={"X-CSRFToken": self.csrf_for(client)},
                json={"vms": [{"vmid": 101}]})
            self.assertEqual(400, response.status_code)
            get_client.assert_not_called()
        status_cache._vm_cache.clear()

    def test_canonical_route_uses_url_server_and_provider_qualified_lookup(self):
        client = self.client_for(4)
        provider = MagicMock()
        provider.start_vm.return_value = {"ok": True}
        cluster = {"name": "k8s_10", "group_id": 10, "pve_server_id": 7,
                   "vms": {"client": {"pve_server_id": 7, "vmid": 101, "node": "node-a"}}}
        with (
            patch.object(app_module, "find_cluster_by_vm", return_value=cluster) as find_vm,
            patch.object(app_module, "get_student_group_ids", return_value=[10]),
            patch.object(app_module, "get_pve_client", return_value=provider) as get_client,
            patch.object(app_module, "update_vm_status"),
        ):
            response = client.post(
                "/api/pve/servers/7/vms/101/start?node=node-a",
                headers={"X-CSRFToken": self.csrf_for(client)}, json={},
            )
        self.assertEqual(200, response.status_code)
        find_vm.assert_called_once_with(7, 101)
        get_client.assert_called_once_with(7)
        provider.start_vm.assert_called_once_with("node-a", 101)


if __name__ == "__main__":
    unittest.main()
