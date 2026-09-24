import unittest

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from modules.identity_audit import audit_vm_identity


class VmIdentityAuditTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool)
        with self.engine.begin() as c:
            c.exec_driver_sql('CREATE TABLE pve_servers(id INTEGER PRIMARY KEY)')
            c.exec_driver_sql('CREATE TABLE clusters(id INTEGER PRIMARY KEY,name TEXT,pve_server_id INTEGER)')
            c.exec_driver_sql('CREATE TABLE vms(id INTEGER PRIMARY KEY,cluster_id INTEGER,vm_name TEXT,vmid INTEGER,node TEXT)')
            c.exec_driver_sql('INSERT INTO pve_servers VALUES(7),(8)')
            c.exec_driver_sql("INSERT INTO clusters VALUES(1,'k8s_1',7),(2,'k8s_2',8),(3,'k8s_3',0)")
            c.exec_driver_sql("INSERT INTO vms VALUES(1,1,'a',101,'node-a'),(2,2,'b',101,'node-a'),(3,3,'c',102,'node-a')")

    def tearDown(self):
        self.engine.dispose()

    def test_reports_backfillable_and_problem_rows_without_writes(self):
        with self.engine.connect() as c:
            report = audit_vm_identity(c)
            self.assertEqual(3, report['vm_total'])
            self.assertEqual(2, report['backfillable'])
            self.assertEqual([3], report['row_ids']['invalid_server'])
            self.assertFalse(report['ready'])
            self.assertEqual((7,101), (report['mapping'][0]['pve_server_id'], report['mapping'][0]['vmid']))
            self.assertEqual(0, c.exec_driver_sql('SELECT COUNT(*) FROM vms WHERE id IS NULL').scalar())
            self.assertEqual({101,102}, set(c.exec_driver_sql('SELECT vmid FROM vms').scalars()))


if __name__ == '__main__':
    unittest.main()
