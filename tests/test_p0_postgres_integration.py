"""Issue #5 PostgreSQL acceptance tests.

Set K8S_LAB_TEST_POSTGRES_URL to a disposable PostgreSQL database.  Each run
creates and drops only a uniquely named schema inside that database.
"""

import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from migrations.runner import run_migrations
from modules.control_plane.repositories import (
    FINALIZER, EnvironmentApiRepository, EnvironmentStatusRepository, LeaseLost,
    OperationAdmissionRepository, OperationExecutorRepository, ResourceVersionConflict,
)
from modules.control_plane.tables import bindings, operations, outbox


POSTGRES_URL = os.getenv("K8S_LAB_TEST_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "需要 K8S_LAB_TEST_POSTGRES_URL 指向一次性 PostgreSQL 数据库")
class P0PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "p0_" + uuid4().hex
        cls.admin = create_engine(POSTGRES_URL, hide_parameters=True)
        with cls.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{cls.schema}"')
        cls.engine = create_engine(POSTGRES_URL, hide_parameters=True)

        @event.listens_for(cls.engine, "connect")
        def set_search_path(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute(f'SET search_path TO "{cls.schema}", public')
            cursor.close()

        run_migrations(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{cls.schema}" CASCADE')
        cls.admin.dispose()

    # ---- helpers ----

    def _seed_connection(self, uid, domain_id):
        with self.engine.begin() as c:
            c.exec_driver_sql("INSERT INTO rf_connections(uid,domain_id,connection_type,credential_ref,revision,active) VALUES "
                f"('{uid}','{domain_id}','pve','cred-ref',1,TRUE)")

    def _seed_resource(self, uid):
        with self.engine.begin() as c:
            c.exec_driver_sql("INSERT INTO rf_resources(uid,resource_type,driver_id) VALUES "
                f"('{uid}','compute.vm/v1','pve.qemu/v1')")

    def _seed_binding(self, uid, resource_uid, connection_uid, domain_id, driver_id, external_key):
        with self.engine.begin() as c:
            c.exec_driver_sql("INSERT INTO rf_bindings(uid,resource_uid,connection_uid,domain_id,driver_id,external_key,"
                "external_identity_digest,active) VALUES "
                f"('{uid}','{resource_uid}','{connection_uid}','{domain_id}','{driver_id}','{external_key}','{'0'*64}',TRUE)")

    def _seed_env(self, uid, name="e"):
        with self.engine.begin() as c:
            c.exec_driver_sql("INSERT INTO cp_environments(uid,name,spec,owner_scope,owner_id,authorization_ref) VALUES "
                f"('{uid}','{name}','{{}}','test','1','opaque')")

    # ---- migration ----

    def test_empty_baseline_and_rerun_are_stable(self):
        run_migrations(self.engine)
        with self.engine.connect() as connection:
            rows = connection.execute(text("SELECT revision, checksum FROM cp_schema_migrations")).all()
            self.assertEqual(1, len(rows))
            self.assertRegex(rows[0].checksum, re.compile(r"^[0-9a-f]{64}$"))

    # ---- environment boundaries ----

    def test_spec_status_boundary_version_conflict_and_atomic_outbox(self):
        uid = uuid4()
        with Session(self.engine) as session, session.begin():
            created = EnvironmentApiRepository(session).create(uid=uid, name="env-" + uid.hex,
                spec={"desiredState":"running"}, owner_scope="class/1", owner_id="1", authorization_ref="opaque")
        with Session(self.engine) as session, session.begin():
            updated = EnvironmentStatusRepository(session).update(uid, status={"phase":"scheduling"},
                observed_generation=0, expected_resource_version=created["resource_version"])
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ResourceVersionConflict):
                EnvironmentApiRepository(session).update_spec(uid,{"desiredState":"stopped"},created["resource_version"])
        with Session(self.engine) as session:
            self.assertEqual(2, session.scalar(select(text("count(*)")).select_from(outbox).where(outbox.c.object_uid==uid)))
            self.assertGreater(updated["resource_version"],created["resource_version"])

        rolled_back = uuid4()
        with self.assertRaisesRegex(RuntimeError,"rollback"):
            with Session(self.engine) as session, session.begin():
                EnvironmentApiRepository(session).create(uid=rolled_back,name="env-"+rolled_back.hex,
                    spec={},owner_scope="class/1",owner_id="1",authorization_ref="opaque")
                raise RuntimeError("rollback")
        with Session(self.engine) as session:
            self.assertEqual(0, session.scalar(select(text("count(*)")).select_from(outbox).where(outbox.c.object_uid==rolled_back)))

    # ---- finalizer ----

    def test_cleanup_finalizer_removes_only_target_finalizer(self):
        uid = uuid4()
        with Session(self.engine) as session, session.begin():
            env = EnvironmentApiRepository(session).create(uid=uid, name="env-multi",
                spec={}, owner_scope="test", owner_id="1", authorization_ref="opaque")
            # Override finalizers after create with an extra one
            session.execute(text("UPDATE cp_environments SET finalizers = $1 WHERE uid = $2"),
                            [["foreign/other", FINALIZER], uid])
            rv_after = session.execute(select(text("resource_version")).select_from(
                text("cp_environments")).where(text("uid=:uid")), {"uid":uid}).scalar()
        with Session(self.engine) as session, session.begin():
            EnvironmentApiRepository(session).mark_for_deletion(uid, rv_after)
        with Session(self.engine) as session:
            rv = session.execute(select(text("resource_version")).select_from(
                text("cp_environments")).where(text("uid=:uid")), {"uid":uid}).scalar()
        with Session(self.engine) as session, session.begin():
            result = EnvironmentStatusRepository(session).remove_cleanup_finalizer(uid,
                expected_resource_version=rv, cleanup_confirmed=True)
            self.assertNotIn(FINALIZER, result["finalizers"])
        with Session(self.engine) as session:
            finalizers_after = session.execute(select(text("finalizers")).select_from(
                text("cp_environments")).where(text("uid=:uid")), {"uid":uid}).scalar()
            self.assertNotIn(FINALIZER, finalizers_after)

    # ---- allocation ----

    def test_allocation_unique_and_lease_fencing(self):
        env, placement = uuid4(), uuid4()
        with self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO cp_environments(uid,name,spec,owner_scope,owner_id,authorization_ref) VALUES "
                f"('{env}','e-{env.hex}','{{}}','test','1','opaque')")
            connection.exec_driver_sql("INSERT INTO cp_placements(uid,environment_uid,revision,scheduling_input_digest,binding_result,phase) VALUES "
                f"('{placement}','{env}',1,'{'0'*64}','{{}}','bound')")
            connection.exec_driver_sql("INSERT INTO cp_allocations(uid,scope,kind,value,environment_uid,placement_uid,state) VALUES "
                f"('{uuid4()}','pve/a','vmid','100','{env}','{placement}','reserved')")
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(text("INSERT INTO cp_allocations(uid,scope,kind,value,environment_uid,placement_uid,state) "
                    "VALUES(:uid,'pve/a','vmid','100',:env,:placement,'reserved')"),
                    {"uid":uuid4(),"env":env,"placement":placement})

        operation = uuid4()
        with Session(self.engine) as session, session.begin():
            OperationAdmissionRepository(session).create(uid=operation,server_scope="test/1",request_id=operation.hex,
                action="probe",normalized_input={},target_snapshot={},plugin_id="test",plugin_version="1",driver_id="test",is_mutating=False)
        with Session(self.engine) as session, session.begin():
            claimed=OperationExecutorRepository(session).claim(operation,worker_id="worker-a",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(LeaseLost):
                OperationExecutorRepository(session).update_execution(operation,worker_id="worker-old",
                    claim_revision=claimed["claim_revision"],phase="succeeded")

    # ---- operation admission: target snapshot validation ----

    def test_admission_rejects_mismatched_binding(self):
        domain_id = "domain-a"
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-100")
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                # wrong binding_uid — doesn't exist
                OperationAdmissionRepository(session).create(
                    server_scope="test/1", request_id=uuid4().hex, action="start",
                    normalized_input={}, target_snapshot={}, plugin_id="test", plugin_version="1",
                    driver_id="pve.qemu/v1", resource_uid=res_id,
                    binding_uid=uuid4(), binding_revision=1, connection_uid=conn_id, connection_revision=1)

    def test_admission_rejects_wrong_connection_for_binding(self):
        domain_id = "domain-a"
        conn_a, conn_b = uuid4(), uuid4()
        res_id, bind_id = uuid4(), uuid4()
        self._seed_connection(conn_a, domain_id)
        self._seed_connection(conn_b, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_a, domain_id, "pve.qemu/v1", "vmid-100")
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                OperationAdmissionRepository(session).create(
                    server_scope="test/1", request_id=uuid4().hex, action="start",
                    normalized_input={}, target_snapshot={}, plugin_id="test", plugin_version="1",
                    driver_id="pve.qemu/v1", resource_uid=res_id,
                    binding_uid=bind_id, binding_revision=1, connection_uid=conn_b, connection_revision=1)

    # ---- operation: dual dedup ----

    def test_transport_dedup_same_digest_returns_same_operation(self):
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            first = OperationAdmissionRepository(session).create(uid=op_id, server_scope="test/scope",
                request_id=op_id.hex, action="probe", normalized_input={}, target_snapshot={},
                plugin_id="test", plugin_version="1", driver_id="test", is_mutating=False)
        with Session(self.engine) as session, session.begin():
            second = OperationAdmissionRepository(session).create(server_scope="test/scope",
                request_id=op_id.hex, action="probe", normalized_input={}, target_snapshot={},
                plugin_id="test", plugin_version="1", driver_id="test", is_mutating=False)
            self.assertEqual(first["uid"], second["uid"])

    def test_plan_item_dedup(self):
        key = "env/uid/intent/1/plan/puid/item/create-vm/attempt/0"
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            first = OperationAdmissionRepository(session).create(uid=op_id, server_scope="test/scope",
                request_id=uuid4().hex, operation_key=key, attempt=0,
                action="create", normalized_input={}, target_snapshot={},
                plugin_id="test", plugin_version="1", driver_id="test", is_mutating=True)
        with Session(self.engine) as session, session.begin():
            second = OperationAdmissionRepository(session).create(server_scope="test/scope",
                request_id=uuid4().hex, operation_key=key, attempt=0,
                action="create", normalized_input={}, target_snapshot={},
                plugin_id="test", plugin_version="1", driver_id="test", is_mutating=True)
            self.assertEqual(first["uid"], second["uid"])

    # ---- executor: claim safety ----

    def test_cancelling_not_claimable(self):
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            OperationAdmissionRepository(session).create(uid=op_id, server_scope="test/scope",
                request_id=op_id.hex, action="delete", normalized_input={}, target_snapshot={},
                plugin_id="test", plugin_version="1", driver_id="test", is_mutating=True)
        # Manually set to cancelling
        with self.engine.begin() as c:
            c.exec_driver_sql(f"UPDATE rf_operations SET phase='cancelling' WHERE uid='{op_id}'")
        with Session(self.engine) as session, session.begin():
            result = OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
            self.assertIsNone(result)

    def test_pending_external_claim_preserves_phase(self):
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            OperationAdmissionRepository(session).create(uid=op_id, server_scope="test/scope",
                request_id=op_id.hex, action="create", normalized_input={}, target_snapshot={},
                plugin_id="test", plugin_version="1", driver_id="test", is_mutating=True)
        # Set to pending_external with external_task_ref (required by DDL CHECK)
        with self.engine.begin() as c:
            c.exec_driver_sql(f"UPDATE rf_operations SET phase='pending_external', external_task_ref='pve-upid-123' WHERE uid='{op_id}'")
        with Session(self.engine) as session, session.begin():
            claimed = OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
            self.assertIsNotNone(claimed)
            self.assertEqual("pending_external", claimed["phase"])

    def test_started_at_preserved_on_reclaim(self):
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            OperationAdmissionRepository(session).create(uid=op_id, server_scope="test/scope",
                request_id=op_id.hex, action="probe", normalized_input={}, target_snapshot={},
                plugin_id="test", plugin_version="1", driver_id="test", is_mutating=False)
        # First claim
        with Session(self.engine) as session, session.begin():
            first = OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(seconds=1))
            first_started = first["started_at"]
            self.assertIsNotNone(first_started)
        # Expire lease
        import time; time.sleep(2)
        # Re-claim
        with Session(self.engine) as session, session.begin():
            second = OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
            self.assertIsNotNone(second)
            self.assertEqual(first_started, second["started_at"])

    # ---- executor: externalTaskRef preservation ----

    def test_external_task_ref_preserved_when_not_provided(self):
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            OperationAdmissionRepository(session).create(uid=op_id, server_scope="test/scope",
                request_id=op_id.hex, action="create", normalized_input={}, target_snapshot={},
                plugin_id="test", plugin_version="1", driver_id="test", is_mutating=True)
        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
        # Set external_task_ref via direct SQL
        with self.engine.begin() as c:
            c.exec_driver_sql(f"UPDATE rf_operations SET external_task_ref='pve-upid-456' WHERE uid='{op_id}'")
        # Now update_execution without providing external_task_ref
        with Session(self.engine) as session, session.begin():
            ops = session.execute(select(operations).where(operations.c.uid==op_id)).mappings().one()
            result = OperationExecutorRepository(session).update_execution(op_id, worker_id="w",
                claim_revision=ops["claim_revision"], phase="succeeded")
            self.assertEqual("pve-upid-456", result["external_task_ref"])

    # ---- executor: binding fencing ----

    def test_old_binding_cannot_write_resource_fact(self):
        domain_id = "domain-b"
        conn_id, res_id, bind_old = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_old, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-200")
        # Create operation with old binding
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            OperationAdmissionRepository(session).create(uid=op_id, server_scope="test/scope",
                request_id=op_id.hex, action="observe", normalized_input={}, target_snapshot={},
                plugin_id="test", plugin_version="1", driver_id="pve.qemu/v1",
                resource_uid=res_id, binding_uid=bind_old, binding_revision=1,
                connection_uid=conn_id, connection_revision=1, is_mutating=False)
        # Claim
        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
        # Activate a new binding (deactivate old)
        bind_new = uuid4()
        with self.engine.begin() as c:
            c.exec_driver_sql(f"UPDATE rf_bindings SET active=FALSE WHERE uid='{bind_old}'")
        self._seed_binding(bind_new, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-200")
        # Old operation should not be able to write resource fact
        with Session(self.engine) as session, session.begin():
            ops = session.execute(select(operations).where(operations.c.uid==op_id)).mappings().one()
            with self.assertRaises(LeaseLost):
                OperationExecutorRepository(session).update_resource_fact(res_id,
                    operation_uid=op_id, worker_id="w", claim_revision=ops["claim_revision"],
                    existence_state="present", status={})

    # ---- log constraints ----

    def test_log_byte_count_must_match_octet_length(self):
        op_id = uuid4()
        with self.engine.begin() as c:
            c.exec_driver_sql(f"INSERT INTO rf_operations(uid,action,phase,is_mutating,normalized_input,server_scope,request_id,request_digest,target_snapshot,plugin_id,plugin_version,driver_id) VALUES ('{op_id}','probe','succeeded',FALSE,'{{}}','test','{op_id.hex}','{'0'*64}','{{}}','test','1','test')")
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as c:
                c.exec_driver_sql(f"INSERT INTO cp_logs(operation_uid,sequence,chunk,byte_count,redacted) VALUES ('{op_id}',0,'hello',999,TRUE)")

    def test_log_exactly_one_owner_violation_fails(self):
        op_id = uuid4()
        with self.engine.begin() as c:
            c.exec_driver_sql(f"INSERT INTO rf_operations(uid,action,phase,is_mutating,normalized_input,server_scope,request_id,request_digest,target_snapshot,plugin_id,plugin_version,driver_id) VALUES ('{op_id}','probe','succeeded',FALSE,'{{}}','test','{op_id.hex}','{'0'*64}','{{}}','test','1','test')")
        env_id = uuid4()
        with self.engine.begin() as c:
            c.exec_driver_sql(f"INSERT INTO cp_environments(uid,name,spec,owner_scope,owner_id,authorization_ref) VALUES ('{env_id}','env-log','{{}}','test','1','opaque')")
        task_id = uuid4()
        with self.engine.begin() as c:
            c.exec_driver_sql(f"INSERT INTO cp_task_views(uid,environment_uid,phase) VALUES ('{task_id}','{env_id}','running')")
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as c:
                c.exec_driver_sql(f"INSERT INTO cp_logs(operation_uid,task_uid,sequence,chunk,byte_count,redacted) VALUES ('{op_id}','{task_id}',0,'x',1,TRUE)")

    def test_log_redacted_must_be_true(self):
        op_id = uuid4()
        with self.engine.begin() as c:
            c.exec_driver_sql(f"INSERT INTO rf_operations(uid,action,phase,is_mutating,normalized_input,server_scope,request_id,request_digest,target_snapshot,plugin_id,plugin_version,driver_id) VALUES ('{op_id}','probe','succeeded',FALSE,'{{}}','test','{op_id.hex}','{'0'*64}','{{}}','test','1','test')")
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as c:
                c.exec_driver_sql(f"INSERT INTO cp_logs(operation_uid,sequence,chunk,byte_count,redacted) VALUES ('{op_id}',0,'x',1,FALSE)")

    # ---- binding identity ----

    def test_binding_identity_unique_per_domain_driver_external_key(self):
        domain_id = "domain-c"
        conn_a, conn_b = uuid4(), uuid4()
        res_a, res_b = uuid4(), uuid4()
        self._seed_connection(conn_a, domain_id)
        self._seed_connection(conn_b, domain_id)
        self._seed_resource(res_a)
        self._seed_resource(res_b)
        bind_a = uuid4()
        self._seed_binding(bind_a, res_a, conn_a, domain_id, "pve.qemu/v1", "vmid-300")
        with self.assertRaises(IntegrityError):
            # Same (domain_id, driver_id, external_key) via different connection
            bind_b = uuid4()
            with self.engine.begin() as c:
                c.exec_driver_sql("INSERT INTO rf_bindings(uid,resource_uid,connection_uid,domain_id,driver_id,external_key,"
                    f"external_identity_digest,active) VALUES ('{bind_b}','{res_b}','{conn_b}','{domain_id}','pve.qemu/v1','vmid-300','{'0'*64}',TRUE)")

    # ---- observed generation constraint ----

    def test_observed_generation_cannot_exceed_generation(self):
        uid = uuid4()
        with Session(self.engine) as session, session.begin():
            EnvironmentApiRepository(session).create(uid=uid, name="env-og", spec={"desiredState":"running"},
                owner_scope="test", owner_id="1", authorization_ref="opaque")
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ResourceVersionConflict):
                EnvironmentStatusRepository(session).update(uid, status={},
                    observed_generation=99, expected_resource_version=1)


if __name__ == "__main__":
    unittest.main()