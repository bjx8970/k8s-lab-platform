"""Run against a disposable PostgreSQL database via K8S_LAB_TEST_POSTGRES_URL."""

import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError, DBAPIError

from migrations.runner import discover_migrations, run_migrations


URL = os.environ.get("K8S_LAB_TEST_POSTGRES_URL")


@unittest.skipUnless(URL, "需要 K8S_LAB_TEST_POSTGRES_URL 指向一次性 PostgreSQL 数据库")
class VmIdentityPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = "p1_" + uuid4().hex
        self.admin = create_engine(URL, hide_parameters=True)
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.engine = create_engine(URL, hide_parameters=True)

        @event.listens_for(self.engine, "connect")
        def set_search_path(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute(f'SET search_path TO "{self.schema}", public')
            cursor.close()
            dbapi_connection.commit()

        run_migrations(self.engine, discover_migrations()[:1])
        with self.engine.begin() as connection:
            for sid in (7, 8):
                connection.execute(text("INSERT INTO pve_servers(id,name,host,\"user\",token_name,token_value) "
                                        "VALUES(:id,:name,'mock','root@pam','test','enc:v1:fake')"),
                                   {"id": sid, "name": f"provider-{sid}"})
            for cid, sid in ((1, 7), (2, 8)):
                connection.execute(text("INSERT INTO clusters(id,name,pve_server_id) VALUES(:id,:name,:sid)"),
                                   {"id": cid, "name": f"k8s_{cid}", "sid": sid})
            connection.execute(text("INSERT INTO vms(id,cluster_id,vm_name,vmid,node) "
                                    "VALUES(1,1,'client',101,'node-a')"))

    def tearDown(self):
        self.engine.dispose()
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.dispose()

    def upgrade(self):
        run_migrations(self.engine)

    def assert_atomic_failure(self, expected):
        with self.assertRaisesRegex((RuntimeError, DBAPIError), expected):
            self.upgrade()
        with self.engine.connect() as connection:
            names = {r[0] for r in connection.exec_driver_sql(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='vms'")}
            revisions = connection.exec_driver_sql("SELECT revision FROM cp_schema_migrations ORDER BY revision").scalars().all()
        self.assertNotIn("pve_server_id", names)
        self.assertEqual(["0001_control_plane_p0"], revisions)

    def test_backfill_allows_cross_provider_duplicate_and_node_migration(self):
        self.upgrade()
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO vms(id,cluster_id,pve_server_id,vm_name,vmid,node) "
                                    "VALUES(2,2,8,'other-client',101,'node-a')"))
            connection.exec_driver_sql("UPDATE vms SET node='node-b' WHERE id=1")
        with self.engine.connect() as connection:
            self.assertEqual([(7, 101, "node-b"), (8, 101, "node-a")], connection.exec_driver_sql(
                "SELECT pve_server_id,vmid,node FROM vms ORDER BY id").all())
        for sql in (
            "INSERT INTO vms(cluster_id,pve_server_id,vm_name,vmid,node) VALUES(1,7,'dup',101,'node-a')",
            "INSERT INTO vms(cluster_id,pve_server_id,vm_name,vmid,node) VALUES(1,0,'zero',999,'node-a')",
            "INSERT INTO vms(cluster_id,pve_server_id,vm_name,vmid,node) VALUES(1,NULL,'null',999,'node-a')",
            "INSERT INTO vms(cluster_id,pve_server_id,vm_name,vmid,node) VALUES(1,999,'missing',999,'node-a')",
        ):
            with self.subTest(sql=sql), self.assertRaises(DBAPIError):
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(sql)
        self.upgrade()

    def test_composite_fk_blocks_vm_and_cluster_provider_drift(self):
        self.upgrade()
        for sql in (
            "UPDATE vms SET pve_server_id=8 WHERE id=1",
            "UPDATE clusters SET pve_server_id=8 WHERE id=1",
            "INSERT INTO vms(id,cluster_id,pve_server_id,vm_name,vmid,node) "
            "VALUES(3,1,8,'wrong',202,'node-a')",
        ):
            with self.subTest(sql=sql), self.assertRaises(DBAPIError):
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(sql)
        with self.engine.connect() as connection:
            self.assertEqual((1, 7), connection.exec_driver_sql(
                "SELECT cluster_id,pve_server_id FROM vms WHERE id=1").one())
            self.assertEqual(7, connection.exec_driver_sql(
                "SELECT pve_server_id FROM clusters WHERE id=1").scalar())

    def test_invalid_cluster_server_fails_with_no_partial_ddl(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE clusters SET pve_server_id=0 WHERE id=1")
        self.assert_atomic_failure("invalid PVE server ID")

    def test_null_server_fails(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE clusters SET pve_server_id=NULL WHERE id=1")
        self.assert_atomic_failure("invalid PVE server ID")

    def test_missing_server_fails(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE clusters SET pve_server_id=999 WHERE id=1")
        self.assert_atomic_failure("PVE server missing")

    def test_invalid_vmid_fails(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE vms SET vmid=0 WHERE id=1")
        self.assert_atomic_failure("invalid VMID")

    def test_orphan_vm_fails(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE vms DROP CONSTRAINT vms_cluster_id_fkey")
            connection.exec_driver_sql("UPDATE vms SET cluster_id=999 WHERE id=1")
        self.assert_atomic_failure("VM without Cluster")

    def test_preexisting_conflicting_identity_fails(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE vms ADD COLUMN pve_server_id INTEGER")
            connection.exec_driver_sql("UPDATE vms SET pve_server_id=8 WHERE id=1")
        with self.assertRaisesRegex((RuntimeError, DBAPIError), "VM/Cluster PVE server mismatch"):
            self.upgrade()
        with self.engine.connect() as connection:
            self.assertEqual(8, connection.exec_driver_sql("SELECT pve_server_id FROM vms WHERE id=1").scalar())
            self.assertEqual(["0001_control_plane_p0"], connection.exec_driver_sql(
                "SELECT revision FROM cp_schema_migrations").scalars().all())


if __name__ == "__main__":
    unittest.main()
