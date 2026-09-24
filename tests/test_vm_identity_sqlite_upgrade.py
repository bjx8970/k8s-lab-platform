"""SQLite legacy rebuild must preserve the original table on any failure."""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from modules.sqlite_vm_identity import upgrade_sqlite_vm_identity


class SqliteVmUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("CREATE TABLE pve_servers(id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE clusters(id INTEGER PRIMARY KEY, name TEXT NOT NULL, pve_server_id INTEGER)")
            connection.exec_driver_sql("CREATE TABLE vms(id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL REFERENCES clusters(id), "
                                       "vm_name TEXT NOT NULL, vmid INTEGER NOT NULL UNIQUE, node TEXT NOT NULL, extra TEXT)")
            connection.exec_driver_sql("CREATE INDEX ix_vm_extra ON vms(extra)")
            connection.exec_driver_sql("CREATE TABLE audit_log(message TEXT)")
            connection.exec_driver_sql("CREATE TRIGGER trg_vm_audit AFTER INSERT ON vms BEGIN INSERT INTO audit_log(message) VALUES(NEW.vm_name); END")
            connection.exec_driver_sql("INSERT INTO pve_servers(id) VALUES(7),(8)")
            connection.exec_driver_sql("INSERT INTO clusters(id,name,pve_server_id) VALUES(1,'k8s_1',7),(2,'k8s_2',8)")
            connection.exec_driver_sql("INSERT INTO vms(id,cluster_id,vm_name,vmid,node,extra) VALUES(1,1,'client',101,'node-a','retained')")

    def tearDown(self):
        self.engine.dispose()

    def test_rebuild_preserves_extra_column_index_trigger_and_idempotence(self):
        upgrade_sqlite_vm_identity(self.engine)
        upgrade_sqlite_vm_identity(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO vms(id,cluster_id,pve_server_id,vm_name,vmid,node,extra) "
                                       "VALUES(2,2,8,'second',101,'node-a','new')")
            self.assertEqual(2, connection.exec_driver_sql("SELECT COUNT(*) FROM vms WHERE vmid=101").scalar())
            self.assertEqual('retained', connection.exec_driver_sql("SELECT extra FROM vms WHERE id=1").scalar())
            self.assertEqual('second', connection.exec_driver_sql("SELECT message FROM audit_log ORDER BY rowid DESC LIMIT 1").scalar())
            self.assertIn('ix_vm_extra', {row[1] for row in connection.exec_driver_sql("PRAGMA index_list(vms)")})
            self.assertEqual([], connection.exec_driver_sql("PRAGMA foreign_key_check").all())

    def test_composite_fk_blocks_vm_and_cluster_provider_drift(self):
        upgrade_sqlite_vm_identity(self.engine)
        with self.engine.connect() as connection:
            self.assertEqual(1, connection.exec_driver_sql("PRAGMA foreign_keys").scalar())
        for sql in (
            "UPDATE vms SET pve_server_id=8 WHERE id=1",
            "UPDATE clusters SET pve_server_id=8 WHERE id=1",
            "INSERT INTO vms(id,cluster_id,pve_server_id,vm_name,vmid,node) "
            "VALUES(3,1,8,'wrong',202,'node-a')",
        ):
            with self.subTest(sql=sql), self.assertRaises(Exception):
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(sql)
        with self.engine.connect() as connection:
            self.assertEqual((1, 7), connection.exec_driver_sql(
                "SELECT cluster_id,pve_server_id FROM vms WHERE id=1").one())
            self.assertEqual(7, connection.exec_driver_sql(
                "SELECT pve_server_id FROM clusters WHERE id=1").scalar())

    def test_injected_ddl_failure_rolls_back_old_table_and_data(self):
        original_connect = self.engine.connect
        def connect_with_failure():
            connection = original_connect()
            execute = connection.exec_driver_sql
            def fail_on_swap(sql, *args, **kwargs):
                if sql == "DROP TABLE vms":
                    raise RuntimeError("injected swap failure")
                return execute(sql, *args, **kwargs)
            connection.exec_driver_sql = fail_on_swap
            return connection
        with patch.object(self.engine, "connect", side_effect=connect_with_failure):
            with self.assertRaisesRegex(RuntimeError, "injected swap failure"):
                upgrade_sqlite_vm_identity(self.engine)
        with original_connect() as connection:
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(vms)")}
            self.assertNotIn("pve_server_id", columns)
            self.assertEqual([(1, 101, 'retained')], connection.exec_driver_sql(
                "SELECT id,vmid,extra FROM vms").all())
            self.assertIsNone(connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name='vms__pve_identity_new'").scalar())
            self.assertEqual(1, connection.exec_driver_sql("PRAGMA foreign_keys").scalar())


if __name__ == "__main__":
    unittest.main()
