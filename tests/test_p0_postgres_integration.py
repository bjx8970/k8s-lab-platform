"""Issue #5 PostgreSQL acceptance tests.

Set K8S_LAB_TEST_POSTGRES_URL to a disposable PostgreSQL database.  Each run
creates and drops only a uniquely named schema inside that database.
"""

import json
import os
import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from migrations.runner import run_migrations
from modules.control_plane.repositories import (
    FINALIZER, EnvironmentApiRepository, EnvironmentStatusRepository, LeaseLost,
    OperationAdmissionRepository, OperationExecutorRepository, RequestConflict,
    ResourceVersionConflict,
)
from modules.control_plane.tables import bindings, operations, outbox


POSTGRES_URL = os.getenv("K8S_LAB_TEST_POSTGRES_URL")


def _pg_error(exc):
    """Extract SQLSTATE and constraint name from a SQLAlchemy/DBAPI error."""
    orig = getattr(exc, "orig", None) or exc
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    diag = getattr(orig, "diag", None)
    constraint = getattr(diag, "constraint_name", None) if diag else None
    info = None
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], dict):
        info = args[0]
    if info:
        sqlstate = sqlstate or info.get("C")
        constraint = constraint or info.get("n")
    return sqlstate, constraint


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
            # Commit so the session-level search_path survives later test rollbacks.
            dbapi_connection.commit()

        run_migrations(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{cls.schema}" CASCADE')
        cls.admin.dispose()

    # ---- helpers ----

    def _seed_connection(self, uid, domain_id, secret_version_ref="secret/v1"):
        with self.engine.begin() as c:
            c.exec_driver_sql("INSERT INTO rf_connections(uid,domain_id,connection_type,secret_version_ref,revision,active) VALUES "
                f"('{uid}','{domain_id}','pve','{secret_version_ref}',1,TRUE)")

    def _seed_resource(self, uid):
        with self.engine.begin() as c:
            c.exec_driver_sql("INSERT INTO rf_resources(uid,resource_type,driver_id) VALUES "
                f"('{uid}','compute.vm/v1','pve.qemu/v1')")

    def _seed_binding(self, uid, resource_uid, connection_uid, domain_id, driver_id, external_key,
                      active=True, locator=None):
        active_str = "TRUE" if active else "FALSE"
        locator_json = json.dumps(locator if locator is not None else {"node": "pve1"})
        with self.engine.begin() as c:
            c.execute(text(
                "INSERT INTO rf_bindings(uid,resource_uid,connection_uid,domain_id,driver_id,external_key,"
                "external_identity,locator,external_identity_digest,active) VALUES "
                "(:uid,:res,:conn,:domain,:driver,:key,'{}'::jsonb,CAST(:locator AS jsonb),:digest,:active)"),
                {"uid": uid, "res": resource_uid, "conn": connection_uid, "domain": domain_id,
                 "driver": driver_id, "key": external_key, "locator": locator_json,
                 "digest": "0" * 64, "active": active})

    def _seed_env(self, uid, name="e"):
        with self.engine.begin() as c:
            c.exec_driver_sql("INSERT INTO cp_environments(uid,name,spec,owner_scope,owner_id,authorization_ref) VALUES "
                f"('{uid}','{name}','{{}}','test','1','opaque')")

    def _create_direct_op(self, session, *, resource_uid, binding_uid, binding_revision,
                          connection_uid, connection_revision, server_scope="test/scope",
                          request_id=None, action="probe", is_mutating=False, uid=None,
                          correlation_id=None, plugin_id="test", plugin_version="1",
                          driver_id="pve.qemu/v1", normalized_input=None):
        """Helper to create a direct Operation; the repository builds the canonical snapshot."""
        return OperationAdmissionRepository(session).create(
            uid=uid or uuid4(), server_scope=server_scope,
            request_id=request_id or uuid4().hex,
            action=action, normalized_input=normalized_input if normalized_input is not None else {},
            plugin_id=plugin_id, plugin_version=plugin_version, driver_id=driver_id,
            resource_uid=resource_uid, binding_uid=binding_uid,
            binding_revision=binding_revision, connection_uid=connection_uid,
            connection_revision=connection_revision,
            source_type="direct", correlation_id=correlation_id or "corr-" + uuid4().hex[:8],
            is_mutating=is_mutating)

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
            session.execute(text("UPDATE cp_environments SET finalizers = :finalizers WHERE uid = :uid"),
                            {"finalizers": ["foreign/other", FINALIZER], "uid": uid})
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
            self.assertIn("foreign/other", finalizers_after)

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

        # Lease fencing with a proper direct Operation
        domain_id = "domain-lease-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-lease")
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                uid=op_id, is_mutating=False)
        with Session(self.engine) as session, session.begin():
            claimed = OperationExecutorRepository(session).claim(op_id, worker_id="worker-a",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(LeaseLost):
                OperationExecutorRepository(session).update_execution(op_id, worker_id="worker-old",
                    claim_revision=claimed["claim_revision"], phase="succeeded")

    # ---- operation admission: target snapshot validation ----

    def test_admission_rejects_mismatched_binding(self):
        domain_id = "domain-a-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-100")
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                self._create_direct_op(session, resource_uid=res_id,
                    binding_uid=uuid4(), binding_revision=1,
                    connection_uid=conn_id, connection_revision=1)

    def test_admission_rejects_wrong_connection_for_binding(self):
        domain_id = "domain-a-" + uuid4().hex[:8]
        conn_a, conn_b = uuid4(), uuid4()
        res_id, bind_id = uuid4(), uuid4()
        self._seed_connection(conn_a, domain_id)
        self._seed_connection(conn_b, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_a, domain_id, "pve.qemu/v1", "vmid-100")
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                self._create_direct_op(session, resource_uid=res_id,
                    binding_uid=bind_id, binding_revision=1,
                    connection_uid=conn_b, connection_revision=1)

    # ---- operation: source_type contract ----

    def test_plan_operation_requires_key_and_attempt(self):
        domain_id = "domain-src-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-src")
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                OperationAdmissionRepository(session).create(
                    server_scope="test/scope", request_id=uuid4().hex,
                    action="create", normalized_input={},
                    plugin_id="test", plugin_version="1", driver_id="pve.qemu/v1",
                    resource_uid=res_id, binding_uid=bind_id, binding_revision=1,
                    connection_uid=conn_id, connection_revision=1,
                    source_type="plan", correlation_id="corr-1")
                # missing operation_key and attempt

    def test_direct_operation_rejects_plan_fields(self):
        domain_id = "domain-src-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-src")
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                OperationAdmissionRepository(session).create(
                    server_scope="test/scope", request_id=uuid4().hex,
                    action="create", normalized_input={},
                    plugin_id="test", plugin_version="1", driver_id="pve.qemu/v1",
                    resource_uid=res_id, binding_uid=bind_id, binding_revision=1,
                    connection_uid=conn_id, connection_revision=1,
                    source_type="direct", correlation_id="corr-1",
                    operation_key="some/key", attempt=0)

    def test_correlation_id_persisted(self):
        domain_id = "domain-corr-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-corr")
        corr = "corr-" + uuid4().hex[:8]
        with Session(self.engine) as session, session.begin():
            op = self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                correlation_id=corr)
            self.assertEqual(corr, op["correlation_id"])
        with Session(self.engine) as session:
            row = session.execute(select(operations.c.correlation_id).where(
                operations.c.uid == op["uid"])).scalar()
            self.assertEqual(corr, row)

    def test_correlation_id_not_part_of_digest_and_replay_keeps_original(self):
        domain_id = "domain-corr2-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-corr2")
        req_id = uuid4().hex
        first_corr = "corr-first"
        with Session(self.engine) as session, session.begin():
            first = self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                request_id=req_id, correlation_id=first_corr)
        with Session(self.engine) as session, session.begin():
            replay = self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                request_id=req_id, correlation_id="corr-second")
            self.assertEqual(first["uid"], replay["uid"])
            self.assertEqual(first_corr, replay["correlation_id"])

    def test_snapshot_matches_verified_target(self):
        domain_id = "domain-snap-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id, secret_version_ref="secret/v9")
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-snap",
                           locator={"node": "pve9"})
        with Session(self.engine) as session, session.begin():
            op = self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1)
            snap = op["target_snapshot"]
            self.assertEqual(str(res_id), snap["resourceId"])
            self.assertEqual(str(bind_id), snap["bindingId"])
            self.assertEqual(1, snap["bindingRevision"])
            self.assertEqual(domain_id, snap["domainId"])
            self.assertEqual(str(conn_id), snap["connectionId"])
            self.assertEqual(1, snap["connectionRevision"])
            self.assertEqual("secret/v9", snap["secretVersionRef"])
            self.assertEqual("test", snap["pluginId"])
            self.assertEqual("1", snap["pluginVersion"])
            self.assertEqual("pve.qemu/v1", snap["driverId"])
            self.assertEqual({"externalKey": "vmid-snap"}, snap["externalIdentity"])
            self.assertEqual({"node": "pve9"}, snap["locator"])
            # Split columns mirror the snapshot
            self.assertEqual(snap["secretVersionRef"], op["secret_version_ref"])
            self.assertEqual(domain_id, op["target_snapshot"]["domainId"])

    def test_admission_rejects_driver_mismatch(self):
        domain_id = "domain-driver-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-driver")
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                    binding_revision=1, connection_uid=conn_id, connection_revision=1,
                    driver_id="pve.other/v1")

    # ---- operation: dual dedup ----

    def test_transport_dedup_same_digest_returns_same_operation(self):
        domain_id = "domain-dedup-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-dedup")
        req_id = uuid4().hex
        with Session(self.engine) as session, session.begin():
            first = self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                request_id=req_id)
        with Session(self.engine) as session, session.begin():
            second = self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                request_id=req_id)
            self.assertEqual(first["uid"], second["uid"])

    def test_transport_dedup_different_digest_raises_conflict(self):
        domain_id = "domain-dedup2-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-dedup2")
        req_id = uuid4().hex
        with Session(self.engine) as session, session.begin():
            self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                request_id=req_id, action="start")
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(RequestConflict):
                self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                    binding_revision=1, connection_uid=conn_id, connection_revision=1,
                    request_id=req_id, action="stop")

    def test_plan_item_dedup(self):
        domain_id = "domain-plan-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-plan")
        key = "env/uid/intent/1/plan/puid/item/create-vm"

        def _plan_op(session, request_id, correlation_id):
            return OperationAdmissionRepository(session).create(
                server_scope="test/scope", request_id=request_id,
                action="create", normalized_input={},
                plugin_id="test", plugin_version="1", driver_id="pve.qemu/v1",
                resource_uid=res_id, binding_uid=bind_id, binding_revision=1,
                connection_uid=conn_id, connection_revision=1,
                source_type="plan", correlation_id=correlation_id,
                operation_key=key, attempt=0, is_mutating=True)

        with Session(self.engine) as session, session.begin():
            first = _plan_op(session, uuid4().hex, "corr-plan")
        with Session(self.engine) as session, session.begin():
            second = _plan_op(session, uuid4().hex, "corr-plan")
            self.assertEqual(first["uid"], second["uid"])
        # Same plan key + attempt with a different input must conflict, not silently reuse.
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(RequestConflict):
                OperationAdmissionRepository(session).create(
                    server_scope="test/scope", request_id=uuid4().hex,
                    action="delete", normalized_input={},
                    plugin_id="test", plugin_version="1", driver_id="pve.qemu/v1",
                    resource_uid=res_id, binding_uid=bind_id, binding_revision=1,
                    connection_uid=conn_id, connection_revision=1,
                    source_type="plan", correlation_id="corr-plan",
                    operation_key=key, attempt=0, is_mutating=True)

    def test_same_request_id_different_target_conflicts(self):
        domain_id = "domain-replay-" + uuid4().hex[:8]
        conn_id, res_a, res_b, bind_a, bind_b = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_a)
        self._seed_resource(res_b)
        self._seed_binding(bind_a, res_a, conn_id, domain_id, "pve.qemu/v1", "vmid-replay-a")
        self._seed_binding(bind_b, res_b, conn_id, domain_id, "pve.qemu/v1", "vmid-replay-b")
        req_id = uuid4().hex
        with Session(self.engine) as session, session.begin():
            self._create_direct_op(session, resource_uid=res_a, binding_uid=bind_a,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                request_id=req_id)
        for kwargs in (
            {"resource_uid": res_b, "binding_uid": bind_b},
            {"resource_uid": res_a, "binding_uid": bind_a, "plugin_version": "2"},
            {"resource_uid": res_a, "binding_uid": bind_a, "driver_id": "pve.other/v1"},
        ):
            with self.subTest(kwargs=kwargs):
                base = dict(resource_uid=res_a, binding_uid=bind_a, binding_revision=1,
                            connection_uid=conn_id, connection_revision=1, request_id=req_id)
                base.update(kwargs)
                with Session(self.engine) as session, session.begin():
                    with self.assertRaises((RequestConflict, ValueError)):
                        self._create_direct_op(session, **base)

    # ---- executor: claim safety ----

    def test_cancelling_not_claimable(self):
        domain_id = "domain-cancel-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-cancel")
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                uid=op_id, action="delete", is_mutating=True)
        with self.engine.begin() as c:
            c.exec_driver_sql(f"UPDATE rf_operations SET phase='cancelling' WHERE uid='{op_id}'")
        with Session(self.engine) as session, session.begin():
            result = OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
            self.assertIsNone(result)

    def test_pending_external_claim_preserves_phase(self):
        domain_id = "domain-pext-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-pext")
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                uid=op_id, action="create", is_mutating=True)
        with self.engine.begin() as c:
            c.exec_driver_sql(f"UPDATE rf_operations SET phase='pending_external', external_task_ref='pve-upid-123' WHERE uid='{op_id}'")
        with Session(self.engine) as session, session.begin():
            claimed = OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
            self.assertIsNotNone(claimed)
            self.assertEqual("pending_external", claimed["phase"])

    def test_started_at_preserved_on_reclaim(self):
        domain_id = "domain-started-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-started")
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                uid=op_id, is_mutating=False)
        with Session(self.engine) as session, session.begin():
            first = OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(seconds=1))
            first_started = first["started_at"]
            self.assertIsNotNone(first_started)
        import time; time.sleep(2)
        with Session(self.engine) as session, session.begin():
            second = OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
            self.assertIsNotNone(second)
            self.assertEqual(first_started, second["started_at"])

    # ---- executor: externalTaskRef preservation ----

    def test_external_task_ref_preserved_when_not_provided(self):
        domain_id = "domain-extref-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-extref")
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                uid=op_id, action="create", is_mutating=True)
        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).claim(op_id, worker_id="w",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
        with self.engine.begin() as c:
            c.exec_driver_sql(f"UPDATE rf_operations SET external_task_ref='pve-upid-456' WHERE uid='{op_id}'")
        with Session(self.engine) as session, session.begin():
            ops = session.execute(select(operations).where(operations.c.uid==op_id)).mappings().one()
            result = OperationExecutorRepository(session).update_execution(op_id, worker_id="w",
                claim_revision=ops["claim_revision"], phase="succeeded")
            self.assertEqual("pve-upid-456", result["external_task_ref"])

    # ---- executor: binding fencing (penetration regression) ----

    def test_old_binding_cannot_write_resource_fact(self):
        """O1/B1 claim → rebind to B2 → O2/B2 created → O1 must be rejected."""
        domain_id = "domain-fence-" + uuid4().hex[:8]
        conn_id, res_id = uuid4(), uuid4()
        bind_old, bind_new = uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_old, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-fence")

        # O1 claims with old binding
        op1_id = uuid4()
        with Session(self.engine) as session, session.begin():
            self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_old,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                uid=op1_id, is_mutating=False)
        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).claim(op1_id, worker_id="w1",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=5))

        # Rebind: deactivate B1, activate B2
        with self.engine.begin() as c:
            c.exec_driver_sql(f"UPDATE rf_bindings SET active=FALSE WHERE uid='{bind_old}'")
        self._seed_binding(bind_new, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-fence")

        # O2 with new binding
        op2_id = uuid4()
        with Session(self.engine) as session, session.begin():
            self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_new,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                uid=op2_id, is_mutating=False)
        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).claim(op2_id, worker_id="w2",
                lease_until=datetime.now(timezone.utc)+timedelta(minutes=5))

        # O1 must be rejected — its binding is no longer active
        with Session(self.engine) as session, session.begin():
            ops1 = session.execute(select(operations).where(operations.c.uid==op1_id)).mappings().one()
            with self.assertRaises(LeaseLost):
                OperationExecutorRepository(session).update_resource_fact(res_id,
                    operation_uid=op1_id, worker_id="w1", claim_revision=ops1["claim_revision"],
                    existence_state="present", status={})

        # O2 should succeed — proves the rejection is due to stale binding, not fixture failure
        with Session(self.engine) as session, session.begin():
            ops2 = session.execute(select(operations).where(operations.c.uid==op2_id)).mappings().one()
            result = OperationExecutorRepository(session).update_resource_fact(res_id,
                operation_uid=op2_id, worker_id="w2", claim_revision=ops2["claim_revision"],
                existence_state="present", status={"node": "pve1"})
            self.assertEqual("present", result["existence_state"])

    # ---- log constraints ----

    def _insert_log(self, c, *, operation_uid=None, task_uid=None, sequence=0,
                    chunk="hello", byte_count=None, redacted=True,
                    expires_at=None):
        """Insert a log row satisfying all non-target constraints."""
        if byte_count is None:
            byte_count = len(chunk.encode("utf-8"))
        if expires_at is None:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        c.execute(text(
            "INSERT INTO cp_logs(operation_uid,task_uid,sequence,chunk,byte_count,redacted,expires_at) "
            "VALUES (:operation_uid,:task_uid,:sequence,:chunk,:byte_count,:redacted,CAST(:expires_at AS timestamptz))"),
            {"operation_uid": operation_uid, "task_uid": task_uid, "sequence": sequence,
             "chunk": chunk, "byte_count": byte_count, "redacted": redacted,
             "expires_at": expires_at})

    def _seed_op_for_logs(self):
        """Create a minimal operation row for log FK."""
        domain_id = "domain-log-" + uuid4().hex[:8]
        conn_id, res_id, bind_id = uuid4(), uuid4(), uuid4()
        self._seed_connection(conn_id, domain_id)
        self._seed_resource(res_id)
        self._seed_binding(bind_id, res_id, conn_id, domain_id, "pve.qemu/v1", "vmid-log")
        op_id = uuid4()
        with Session(self.engine) as session, session.begin():
            self._create_direct_op(session, resource_uid=res_id, binding_uid=bind_id,
                binding_revision=1, connection_uid=conn_id, connection_revision=1,
                uid=op_id, is_mutating=False)
        return op_id

    def test_log_byte_count_must_match_octet_length(self):
        op_id = self._seed_op_for_logs()
        with self.assertRaises(DBAPIError) as ctx:
            with self.engine.begin() as c:
                self._insert_log(c, operation_uid=op_id, chunk="hello", byte_count=999)
        sqlstate, constraint = _pg_error(ctx.exception)
        self.assertEqual("23514", sqlstate)
        self.assertEqual("ck_cp_log_byte_count", constraint)

    def test_log_exactly_one_owner_violation_fails(self):
        op_id = self._seed_op_for_logs()
        env_id = uuid4()
        with self.engine.begin() as c:
            c.exec_driver_sql(f"INSERT INTO cp_environments(uid,name,spec,owner_scope,owner_id,authorization_ref) VALUES ('{env_id}','env-log','{{}}','test','1','opaque')")
        task_id = uuid4()
        with self.engine.begin() as c:
            c.exec_driver_sql(f"INSERT INTO cp_task_views(uid,environment_uid,phase) VALUES ('{task_id}','{env_id}','running')")
        with self.assertRaises(DBAPIError) as ctx:
            with self.engine.begin() as c:
                self._insert_log(c, operation_uid=op_id, task_uid=task_id, chunk="x")
        sqlstate, constraint = _pg_error(ctx.exception)
        self.assertEqual("23514", sqlstate)
        self.assertEqual("ck_cp_log_exactly_one_owner", constraint)

    def test_log_redacted_must_be_true(self):
        op_id = self._seed_op_for_logs()
        with self.assertRaises(DBAPIError) as ctx:
            with self.engine.begin() as c:
                self._insert_log(c, operation_uid=op_id, chunk="x", redacted=False)
        sqlstate, constraint = _pg_error(ctx.exception)
        self.assertEqual("23514", sqlstate)
        self.assertEqual("ck_cp_log_redacted", constraint)

    def test_log_quota_exceeded(self):
        op_id = self._seed_op_for_logs()
        self._prefill_to_quota_boundary(op_id)
        # Counter is exactly 16 MiB - 1; two more bytes exceed the quota.
        with self.assertRaises(DBAPIError) as ctx:
            with self.engine.begin() as c:
                self._insert_log(c, operation_uid=op_id, sequence=1000, chunk="yy")
        sqlstate, constraint = _pg_error(ctx.exception)
        self.assertEqual("23514", sqlstate)
        self.assertEqual("ck_cp_log_owner_quota", constraint)

    def test_log_update_is_rejected_and_cannot_bypass_quota(self):
        op_id = self._seed_op_for_logs()
        with self.engine.begin() as c:
            self._insert_log(c, operation_uid=op_id, sequence=0, chunk="x")
        with self.assertRaises(DBAPIError) as ctx:
            with self.engine.begin() as c:
                # byte_count matches octet_length, so only append-only may reject this.
                c.exec_driver_sql(
                    "UPDATE cp_logs SET chunk = repeat('x', 65536), byte_count = 65536 "
                    f"WHERE operation_uid = '{op_id}' AND sequence = 0")
        sqlstate, constraint = _pg_error(ctx.exception)
        self.assertEqual("55000", sqlstate)
        self.assertEqual("ck_cp_log_append_only", constraint)
        # Row, counter and SUM stay at 1 byte.
        with self.engine.connect() as c:
            self.assertEqual(1, c.exec_driver_sql(
                f"SELECT byte_count FROM cp_logs WHERE operation_uid = '{op_id}'").scalar())
            self.assertEqual(1, c.exec_driver_sql(
                f"SELECT used_bytes FROM cp_log_owner_counters WHERE owner_uid = '{op_id}'").scalar())
            self.assertEqual(1, c.exec_driver_sql(
                f"SELECT COALESCE(SUM(byte_count),0) FROM cp_logs WHERE operation_uid = '{op_id}'").scalar())

    def test_concurrent_inserts_at_quota_boundary(self):
        op_id = self._seed_op_for_logs()
        self._prefill_to_quota_boundary(op_id)
        barrier = threading.Barrier(2)
        outcomes = []

        def _worker(sequence):
            try:
                with self.engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO cp_logs(operation_uid,sequence,chunk,byte_count,redacted,expires_at) "
                        "VALUES (:owner,:seq,'z',1,TRUE,now() + interval '30 days')"),
                        {"owner": op_id, "seq": sequence})
                barrier.wait(timeout=30)
                outcomes.append(("ok", None))
            except Exception as exc:  # noqa: BLE001 - record the failure for assertion
                barrier.wait(timeout=30)
                outcomes.append(("error", exc))

        threads = [threading.Thread(target=_worker, args=(2000 + i,)) for i in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=60)

        successes = [o for o in outcomes if o[0] == "ok"]
        failures = [o for o in outcomes if o[0] == "error"]
        self.assertEqual(1, len(successes), outcomes)
        self.assertEqual(1, len(failures), outcomes)
        sqlstate, constraint = _pg_error(failures[0][1])
        self.assertEqual("23514", sqlstate)
        self.assertEqual("ck_cp_log_owner_quota", constraint)

        with self.engine.connect() as c:
            total = c.exec_driver_sql(
                f"SELECT COALESCE(SUM(byte_count),0) FROM cp_logs WHERE operation_uid = '{op_id}'").scalar()
            used = c.exec_driver_sql(
                f"SELECT used_bytes FROM cp_log_owner_counters WHERE owner_uid = '{op_id}'").scalar()
            self.assertEqual(16777216, total)
            self.assertEqual(total, used)

    def test_delete_releases_quota_consistently(self):
        op_id = self._seed_op_for_logs()
        self._prefill_to_quota_boundary(op_id)
        with self.engine.begin() as c:
            self._insert_log(c, operation_uid=op_id, sequence=1000, chunk="z")
        with self.engine.begin() as c:
            c.execute(text("DELETE FROM cp_logs WHERE operation_uid = :o AND sequence = :s"),
                      {"o": op_id, "s": 1000})
        with self.engine.connect() as c:
            used = c.exec_driver_sql(
                f"SELECT used_bytes FROM cp_log_owner_counters WHERE owner_uid = '{op_id}'").scalar()
            total = c.exec_driver_sql(
                f"SELECT COALESCE(SUM(byte_count),0) FROM cp_logs WHERE operation_uid = '{op_id}'").scalar()
            self.assertEqual(16777215, used)
            self.assertEqual(total, used)
        # The freed byte is reusable.
        with self.engine.begin() as c:
            self._insert_log(c, operation_uid=op_id, sequence=1001, chunk="z")

    def _prefill_to_quota_boundary(self, op_id):
        """Fill the owner to exactly 16 MiB - 1 in the database, not over the wire."""
        with self.engine.begin() as c:
            c.execute(text(
                "INSERT INTO cp_logs(operation_uid,sequence,chunk,byte_count,redacted,expires_at) "
                "SELECT :owner, gs, repeat('x', 65536), 65536, TRUE, now() + interval '30 days' "
                "FROM generate_series(1, 255) AS gs"), {"owner": op_id})
            self._insert_log(c, operation_uid=op_id, sequence=256,
                             chunk="x" * 65535, byte_count=65535)
        with self.engine.connect() as c:
            self.assertEqual(16777215, c.exec_driver_sql(
                f"SELECT used_bytes FROM cp_log_owner_counters WHERE owner_uid = '{op_id}'").scalar())

    # ---- binding identity ----

    def test_binding_identity_unique_per_domain_driver_external_key(self):
        domain_id = "domain-c-" + uuid4().hex[:8]
        conn_a, conn_b = uuid4(), uuid4()
        res_a, res_b = uuid4(), uuid4()
        self._seed_connection(conn_a, domain_id)
        self._seed_connection(conn_b, domain_id)
        self._seed_resource(res_a)
        self._seed_resource(res_b)
        bind_a = uuid4()
        self._seed_binding(bind_a, res_a, conn_a, domain_id, "pve.qemu/v1", "vmid-300")
        with self.assertRaises(IntegrityError):
            bind_b = uuid4()
            with self.engine.begin() as c:
                c.exec_driver_sql("INSERT INTO rf_bindings(uid,resource_uid,connection_uid,domain_id,driver_id,external_key,"
                    f"external_identity_digest,active) VALUES ('{bind_b}','{res_b}','{conn_b}','{domain_id}','pve.qemu/v1','vmid-300','{'0'*64}',TRUE)")

    # ---- observed generation constraint ----

    def test_observed_generation_cannot_exceed_generation(self):
        uid = uuid4()
        with Session(self.engine) as session, session.begin():
            created = EnvironmentApiRepository(session).create(uid=uid, name="env-og",
                spec={"desiredState":"running"}, owner_scope="test", owner_id="1",
                authorization_ref="opaque")
        # Use the REAL resource_version from create, not a hardcoded value
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ResourceVersionConflict):
                EnvironmentStatusRepository(session).update(uid, status={},
                    observed_generation=99,
                    expected_resource_version=created["resource_version"])
        # Verify the object was not modified
        with Session(self.engine) as session:
            row = session.execute(select(text("observed_generation,resource_version")).select_from(
                text("cp_environments")).where(text("uid=:uid")), {"uid":uid}).one()
            self.assertEqual(0, row[0])
            self.assertEqual(created["resource_version"], row[1])


@unittest.skipUnless(POSTGRES_URL, "需要 K8S_LAB_TEST_POSTGRES_URL 指向一次性 PostgreSQL 数据库")
class PreP0MigrationTests(unittest.TestCase):
    """Upgrade a realistic pre-P0 database and verify the frozen data migration."""

    PRE_P0_DDL = (
        "CREATE TABLE config (key VARCHAR(64) PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE pve_servers (id SERIAL PRIMARY KEY, name VARCHAR(64) NOT NULL UNIQUE, "
        "host VARCHAR(128) NOT NULL, port INTEGER, \"user\" VARCHAR(64) NOT NULL, "
        "token_name VARCHAR(64) NOT NULL, token_value TEXT NOT NULL, node VARCHAR(64), "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE users (id SERIAL PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE, "
        "password_hash VARCHAR(256) NOT NULL, role VARCHAR(16) NOT NULL DEFAULT 'student', "
        "is_active BOOLEAN DEFAULT TRUE, created_by INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    )

    def setUp(self):
        self.schema = "prep0_" + uuid4().hex
        self.admin = create_engine(POSTGRES_URL, hide_parameters=True)
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.engine = create_engine(POSTGRES_URL, hide_parameters=True)

        @event.listens_for(self.engine, "connect")
        def set_search_path(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute(f'SET search_path TO "{self.schema}", public')
            cursor.close()
            dbapi_connection.commit()

        with self.engine.begin() as c:
            for ddl in self.PRE_P0_DDL:
                c.exec_driver_sql(ddl)

    def tearDown(self):
        self.engine.dispose()
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.dispose()

    @staticmethod
    def _cipher(key, plaintext):
        from cryptography.fernet import Fernet
        return "enc:v1:" + Fernet(key).encrypt(plaintext.encode()).decode("ascii")

    def _run_with_key(self, key):
        import os
        previous = os.environ.get("K8S_LAB_CREDENTIAL_KEY")
        os.environ["K8S_LAB_CREDENTIAL_KEY"] = key.decode("ascii")
        try:
            run_migrations(self.engine)
        finally:
            if previous is None:
                os.environ.pop("K8S_LAB_CREDENTIAL_KEY", None)
            else:
                os.environ["K8S_LAB_CREDENTIAL_KEY"] = previous

    def test_pve_and_openwrt_config_are_migrated(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        token = self._cipher(key, "legacy-token")
        password = self._cipher(key, "legacy-password")
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO config(key,value) VALUES ('pve',:v)"), {"v": json.dumps({
                "host": "10.0.0.5", "port": "8006", "user": "root@pam",
                "token_name": "lab-token", "token_value": token, "node": "pve1"})})
            c.execute(text("INSERT INTO config(key,value) VALUES ('openwrt',:v)"), {"v": json.dumps({
                "host": "10.0.0.9", "port": 22, "username": "root", "password": password})})
            c.execute(text("INSERT INTO users(username,password_hash) VALUES ('legacy-user','x')"))

        self._run_with_key(key)

        with self.engine.connect() as c:
            server = c.execute(text("SELECT name,host,port,\"user\",token_name,token_value,node,template_vmid,"
                                    "ow_host,ow_port,ow_username,ow_password FROM pve_servers")).mappings().one()
            self.assertEqual("default", server["name"])
            self.assertEqual("10.0.0.5", server["host"])
            self.assertEqual(8006, server["port"])
            self.assertEqual("root@pam", server["user"])
            self.assertEqual("lab-token", server["token_name"])
            self.assertEqual(token, server["token_value"])
            self.assertEqual("pve1", server["node"])
            self.assertEqual(9000, server["template_vmid"])
            self.assertEqual("10.0.0.9", server["ow_host"])
            self.assertEqual(22, server["ow_port"])
            self.assertEqual("root", server["ow_username"])
            self.assertEqual(password, server["ow_password"])
            self.assertIsNone(c.execute(text("SELECT value FROM config WHERE key='pve'")).scalar())
            self.assertIsNotNone(c.execute(text("SELECT value FROM config WHERE key='openwrt'")).scalar())
            self.assertEqual(1, c.execute(text("SELECT COUNT(*) FROM users")).scalar())
            revisions = c.execute(text("SELECT revision FROM cp_schema_migrations")).scalars().all()
            self.assertEqual(["0001_control_plane_p0"], revisions)

        # Ciphertext stays decryptable with the frozen protocol.
        from cryptography.fernet import Fernet as F
        self.assertEqual(b"legacy-token", F(key).decrypt(token[len("enc:v1:"):].encode()))

        # Rerun is stable.
        self._run_with_key(key)
        with self.engine.connect() as c:
            self.assertEqual(1, c.execute(text("SELECT COUNT(*) FROM pve_servers")).scalar())
            self.assertEqual(token, c.execute(text("SELECT token_value FROM pve_servers")).scalar())

    def test_existing_server_is_not_replaced(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        password = self._cipher(key, "ow-password")
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO pve_servers(name,host,port,\"user\",token_name,token_value,node) "
                           "VALUES ('existing','10.0.0.1',8006,'root@pam','t','enc:v1:whatever','pve1')"))
            c.execute(text("INSERT INTO config(key,value) VALUES ('pve',:v)"),
                      {"v": json.dumps({"host": "should-not-apply", "token_value": self._cipher(key, "x")})})
            c.execute(text("INSERT INTO config(key,value) VALUES ('openwrt',:v)"), {"v": json.dumps({
                "host": "10.0.0.9", "port": 22, "username": "root", "password": password})})

        # The pre-existing server's token is not valid ciphertext for this key, so backfill must skip validation
        # only when there is nothing to backfill; here OpenWrt should fill the empty ow_host.
        self._run_with_key(key)
        with self.engine.connect() as c:
            self.assertEqual(1, c.execute(text("SELECT COUNT(*) FROM pve_servers")).scalar())
            self.assertEqual("existing", c.execute(text("SELECT name FROM pve_servers")).scalar())
            self.assertIsNotNone(c.execute(text("SELECT value FROM config WHERE key='pve'")).scalar())
            self.assertEqual("10.0.0.9", c.execute(text("SELECT ow_host FROM pve_servers")).scalar())
            self.assertEqual(password, c.execute(text("SELECT ow_password FROM pve_servers")).scalar())

    def test_wrong_key_fails_closed_and_rolls_back(self):
        from cryptography.fernet import Fernet
        good = Fernet.generate_key()
        wrong = Fernet.generate_key()
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO config(key,value) VALUES ('pve',:v)"), {"v": json.dumps({
                "host": "10.0.0.5", "token_value": self._cipher(good, "secret")})})

        with self.assertRaises(RuntimeError) as ctx:
            self._run_with_key(wrong)
        message = str(ctx.exception)
        self.assertNotIn("secret", message)
        self.assertNotIn("enc:v1:", message)

        with self.engine.connect() as c:
            self.assertIsNotNone(c.execute(text("SELECT value FROM config WHERE key='pve'")).scalar())
            self.assertEqual(0, c.execute(text("SELECT COUNT(*) FROM pve_servers")).scalar())
            self.assertFalse(c.execute(text("SELECT to_regclass('cp_schema_migrations') IS NOT NULL")).scalar())
            self.assertFalse(c.execute(text("SELECT to_regclass('rf_operations') IS NOT NULL")).scalar())

        # Recovery with the correct key succeeds.
        self._run_with_key(good)
        with self.engine.connect() as c:
            self.assertEqual(1, c.execute(text("SELECT COUNT(*) FROM pve_servers")).scalar())

    def test_plaintext_credential_fails_closed(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO config(key,value) VALUES ('pve',:v)"), {"v": json.dumps({
                "host": "10.0.0.5", "token_value": "plaintext-secret"})})
        with self.assertRaises(RuntimeError) as ctx:
            self._run_with_key(key)
        self.assertNotIn("plaintext-secret", str(ctx.exception))
        with self.engine.connect() as c:
            self.assertEqual(0, c.execute(text("SELECT COUNT(*) FROM pve_servers")).scalar())


if __name__ == "__main__":
    unittest.main()