"""P2 resource execution and crash-recovery acceptance on disposable PostgreSQL."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, delete, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from migrations.runner import run_migrations
from modules.control_plane.repositories import LeaseLost, OperationExecutorRepository, RequestConflict
from modules.control_plane.tables import bindings, connections, operations, resources
from modules.resource_framework.handlers import ExecutionResult, FakeHandler, HandlerRegistry
from modules.resource_framework.runtime import OperationWorker
from modules.resource_framework.service import ResourceService


class FakeHandlerTests(unittest.TestCase):
    def test_explicit_outcomes_and_call_recording(self):
        fake = FakeHandler()
        fake.execute_results = [ExecutionResult("pending_external", external_task_ref="job-1")]
        fake.poll_results = [ExecutionResult("succeeded", existence_state="present")]
        target = {"resourceId": "r"}
        self.assertEqual("pending_external", fake.execute(target, "create", {}).phase)
        self.assertEqual("succeeded", fake.poll(target, "job-1", {}).phase)
        self.assertEqual([("execute", "r", "create"), ("poll", "r", "job-1")], fake.calls)


@unittest.skipUnless(os.getenv("K8S_LAB_TEST_POSTGRES_URL"), "需要一次性 PostgreSQL")
class Issue3PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "p2_" + uuid4().hex
        url = os.environ["K8S_LAB_TEST_POSTGRES_URL"]
        cls.admin = create_engine(url, hide_parameters=True)
        with cls.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{cls.schema}"')
        cls.engine = create_engine(url, hide_parameters=True)

        @event.listens_for(cls.engine, "connect")
        def search_path(connection, _):
            cursor = connection.cursor()
            cursor.execute(f'SET search_path TO "{cls.schema}", public')
            cursor.close()
            connection.commit()

        run_migrations(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{cls.schema}" CASCADE')
        cls.admin.dispose()

    def setUp(self):
        self.fake = FakeHandler()
        self.registry = HandlerRegistry()
        self.registry.register("fake", "1", "fake.vm/v1", self.fake)
        self.connection_uid = uuid4()
        self.domain = "fake-" + uuid4().hex
        with Session(self.engine) as session, session.begin():
            session.execute(connections.insert().values(uid=self.connection_uid,
                domain_id=self.domain, connection_type="fake", secret_version_ref="secret/v1",
                configuration={}, revision=1, active=True))
        self.worker = OperationWorker(self.engine, self.registry, worker_id=uuid4().hex, lease_seconds=6)

    def tearDown(self):
        with Session(self.engine) as session, session.begin():
            resource_ids = session.execute(select(bindings.c.resource_uid).where(
                bindings.c.connection_uid == self.connection_uid)).scalars().all()
            session.execute(delete(operations).where(operations.c.connection_uid == self.connection_uid))
            session.execute(delete(bindings).where(bindings.c.connection_uid == self.connection_uid))
            if resource_ids:
                session.execute(delete(resources).where(resources.c.uid.in_(resource_ids)))
            session.execute(delete(connections).where(connections.c.uid == self.connection_uid))

    def create(self, *, request_id=None, external_key=None):
        with Session(self.engine) as session, session.begin():
            op = ResourceService(session, self.registry).create(
                connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                external_key=external_key or uuid4().hex, locator={"node": "n1"},
                identity_evidence={"source": "test"}, attributes={"image": "x"},
                normalized_input={"image": "x"}, server_scope="test/issue3",
                request_id=request_id or uuid4().hex, correlation_id=uuid4().hex)
            return op

    def read(self, uid):
        with Session(self.engine) as session:
            return ResourceService(session, self.registry).get_operation(uid)

    def test_create_is_atomic_and_replay_does_not_duplicate_resource(self):
        key, external = uuid4().hex, uuid4().hex
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with Session(self.engine) as session, session.begin():
                ResourceService(session, self.registry).create(
                    connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                    plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                    external_key=external, locator={}, identity_evidence={"source": "test"},
                    attributes={"image": "x"}, normalized_input={"image": "x"},
                    server_scope="test/issue3", request_id=key, correlation_id="corr")
                raise RuntimeError("rollback")
        with Session(self.engine) as session:
            self.assertEqual(0, len(session.execute(select(operations.c.uid).where(
                operations.c.request_id == key)).all()))
        op = self.create(request_id=key, external_key=external)
        replay = self.create(request_id=key, external_key=external)
        self.assertEqual(op["uid"], replay["uid"])
        with Session(self.engine) as session:
            self.assertEqual(1, len(session.execute(select(bindings.c.uid).where(
                bindings.c.external_key == external)).all()))
        self.assertEqual("pending", op["phase"])

    def test_create_conflict_and_register_adopt_no_external_call(self):
        key, external = uuid4().hex, uuid4().hex
        self.create(request_id=key, external_key=external)
        with self.assertRaises(RequestConflict):
            self.create(request_id=key, external_key=uuid4().hex)
        with self.assertRaises(IntegrityError):
            self.create(external_key=external)
        with Session(self.engine) as session, session.begin():
            registered = ResourceService(session, self.registry).register(
                connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                external_key=uuid4().hex, locator={}, identity_evidence={"source": "adopt"})
        self.assertEqual("present", registered["existence_state"])
        self.assertEqual([], self.fake.calls)

    def test_create_request_replays_after_revisions_change_and_plugin_unloads(self):
        key, external = uuid4().hex, uuid4().hex
        op = self.create(request_id=key, external_key=external)
        with Session(self.engine) as session, session.begin():
            session.execute(update(bindings).where(bindings.c.uid == op["binding_uid"])
                .values(revision=2))
            session.execute(update(connections).where(connections.c.uid == self.connection_uid)
                .values(revision=2, secret_version_ref="secret/v2", active=False))
        with Session(self.engine) as session, session.begin():
            replay = ResourceService(session, HandlerRegistry()).create(
                connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                external_key=external, locator={"node": "n1"},
                identity_evidence={"source": "test"}, attributes={"image": "x"},
                normalized_input={"image": "x"}, server_scope="test/issue3",
                request_id=key, correlation_id="replayed")
        self.assertEqual(op["uid"], replay["uid"])
        self.assertEqual(op["correlation_id"], replay["correlation_id"])
        self.assertEqual([], self.fake.calls)

    def test_registration_identity_scoped_by_domain(self):
        external = uuid4().hex
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            first = service.register(connection_uid=self.connection_uid,
                resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                driver_id="fake.vm/v1", external_key=external, locator={},
                identity_evidence={"source": "test"})
            second_connection = service.add_connection(domain_id="another-" + uuid4().hex,
                connection_type="fake", configuration={}, secret_version_ref="secret/v1")
            second = service.register(connection_uid=second_connection["uid"],
                resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                driver_id="fake.vm/v1", external_key=external, locator={},
                identity_evidence={"source": "test"})
        self.assertNotEqual(first["uid"], second["uid"])
        self.assertEqual([], self.fake.calls)

    def test_direct_request_replays_after_binding_and_connection_retire(self):
        key = uuid4().hex
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            resource = service.register(connection_uid=self.connection_uid,
                resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                driver_id="fake.vm/v1", external_key=uuid4().hex, locator={},
                identity_evidence={"source": "test"})
            op = service.execute(resource["uid"], action="start", normalized_input={},
                plugin_id="fake", plugin_version="1", server_scope="test/issue3",
                request_id=key, correlation_id="first")
        self.fake.execute_results = [ExecutionResult("succeeded")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(bindings).where(bindings.c.uid == op["binding_uid"])
                .values(active=False, revision=2))
            session.execute(update(connections).where(connections.c.uid == self.connection_uid)
                .values(active=False, revision=2, secret_version_ref="secret/v2"))
        with Session(self.engine) as session, session.begin():
            replay = ResourceService(session, HandlerRegistry()).execute(resource["uid"],
                action="start", normalized_input={}, plugin_id="fake", plugin_version="1",
                server_scope="test/issue3", request_id=key, correlation_id="later")
        self.assertEqual(op["uid"], replay["uid"])
        self.assertEqual("first", replay["correlation_id"])
        self.assertEqual(1, len(self.fake.calls))

    def test_unregister_retires_identity_only_after_pending_work_is_resolved(self):
        external = uuid4().hex
        with Session(self.engine) as session, session.begin():
            resource = ResourceService(session, self.registry).register(
                connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                external_key=external, locator={}, identity_evidence={"source": "test"})
            op = ResourceService(session, self.registry).execute(resource["uid"],
                action="start", normalized_input={}, plugin_id="fake", plugin_version="1",
                server_scope="test/issue3", request_id=uuid4().hex, correlation_id="corr")
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                ResourceService(session, self.registry).unregister(resource["uid"])
        self.fake.execute_results = [ExecutionResult("succeeded")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            service.unregister(resource["uid"])
            replacement = service.register(connection_uid=self.connection_uid,
                resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                driver_id="fake.vm/v1", external_key=external, locator={},
                identity_evidence={"source": "test"})
        self.assertNotEqual(resource["uid"], replacement["uid"])
        with Session(self.engine) as session:
            old_binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
        self.assertFalse(old_binding["active"])
        self.assertIsNotNone(old_binding["closed_at"])

    def test_mutating_operation_is_serial_per_resource(self):
        op = self.create()
        with self.assertRaises(IntegrityError):
            with Session(self.engine) as session, session.begin():
                ResourceService(session, self.registry).execute(op["resource_uid"],
                    action="start", normalized_input={}, plugin_id="fake", plugin_version="1",
                    server_scope="test/issue3", request_id=uuid4().hex, correlation_id="corr")

    def test_async_restart_polls_original_ref_and_tracks_resource_fact(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("pending_external", external_task_ref="job-123",
            exec_data={"step": "accepted"})]
        self.assertEqual(op["uid"], self.worker.run_once())
        pending = self.read(op["uid"])
        self.assertEqual("pending_external", pending["phase"])
        self.assertEqual("job-123", pending["external_task_ref"])
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.fake.poll_results = [ExecutionResult("succeeded", output={"power": "on"},
            existence_state="present")]
        restarted = OperationWorker(self.engine, self.registry,
            worker_id="restarted-" + uuid4().hex, lease_seconds=6)
        self.assertEqual(op["uid"], restarted.run_once())
        self.assertEqual("succeeded", self.read(op["uid"])["phase"])
        with Session(self.engine) as session:
            resource = session.execute(select(resources).where(
                resources.c.uid == op["resource_uid"])).mappings().one()
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
        self.assertEqual("present", resource["existence_state"])
        self.assertEqual("present", binding["existence_state"])
        self.assertEqual(1, len([call for call in self.fake.calls if call[0] == "execute"]))

    def test_create_success_requires_confirmed_existence(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("succeeded")]
        self.worker.run_once()
        result = self.read(op["uid"])
        self.assertEqual("unknown", result["phase"])
        self.assertEqual("ExistenceUnconfirmed", result["error"]["code"])
        with Session(self.engine) as session:
            resource = session.execute(select(resources).where(
                resources.c.uid == op["resource_uid"])).mappings().one()
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
        self.assertEqual("pending", resource["existence_state"])
        self.assertTrue(binding["provisional"])
        self.assertTrue(binding["active"])
        self.assertIsNone(self.worker.run_once())

    def test_observe_is_read_only_and_does_not_overwrite_resource_state(self):
        create_op = self.create()
        with Session(self.engine) as session, session.begin():
            observation = ResourceService(session, self.registry).execute(create_op["resource_uid"],
                action="observe", normalized_input={}, plugin_id="fake", plugin_version="1",
                server_scope="test/issue3", request_id=uuid4().hex, correlation_id="observe")
        self.assertFalse(observation["is_mutating"])
        self.assertEqual("pending", self.read(create_op["uid"])["phase"])
        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).claim(create_op["uid"], worker_id="other",
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=1))
        self.fake.execute_results = [ExecutionResult("succeeded", existence_state="present",
            output={"power": "on"})]
        self.assertEqual(observation["uid"], self.worker.run_once())
        self.assertEqual("succeeded", self.read(observation["uid"])["phase"])
        with Session(self.engine) as session:
            resource = session.execute(select(resources).where(
                resources.c.uid == create_op["resource_uid"])).mappings().one()
        self.assertEqual("pending", resource["existence_state"])
        self.assertEqual({}, resource["status"])

    def test_expired_running_becomes_unknown_without_reexecution(self):
        op = self.create()
        with Session(self.engine) as session, session.begin():
            claimed = OperationExecutorRepository(session).claim(op["uid"], worker_id="dead",
                lease_until=datetime.now(timezone.utc) + timedelta(seconds=10))
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.assertEqual(op["uid"], self.worker.run_once())
        self.assertEqual("unknown", self.read(op["uid"])["phase"])
        self.assertEqual([], self.fake.calls)
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(LeaseLost):
                OperationExecutorRepository(session).update_execution(op["uid"], worker_id="dead",
                    claim_revision=claimed["claim_revision"], phase="succeeded")

    def test_disconnect_during_submission_becomes_unknown_and_error_is_redacted(self):
        op = self.create()
        self.fake.execute_results = [RuntimeError("password=do-not-leak")]
        self.worker.run_once()
        result = self.read(op["uid"])
        self.assertEqual("unknown", result["phase"])
        self.assertNotIn("do-not-leak", str(result))
        self.assertEqual(1, len(self.fake.calls))
        self.assertIsNone(self.worker.run_once())

    def test_changed_connection_fails_before_external_call(self):
        op = self.create()
        with Session(self.engine) as session, session.begin():
            session.execute(update(connections).where(connections.c.uid == self.connection_uid)
                .values(revision=2, secret_version_ref="secret/v2"))
        self.worker.run_once()
        self.assertEqual("failed", self.read(op["uid"])["phase"])
        self.assertEqual([], self.fake.calls)

    def test_changed_binding_fails_before_external_call(self):
        op = self.create()
        with Session(self.engine) as session, session.begin():
            session.execute(update(bindings).where(bindings.c.uid == op["binding_uid"])
                .values(revision=2, locator={"node": "n2"}))
        self.worker.run_once()
        self.assertEqual("failed", self.read(op["uid"])["phase"])
        self.assertEqual([], self.fake.calls)

    def test_missing_plugin_does_not_dispatch_to_another_version(self):
        op = self.create()
        worker = OperationWorker(self.engine, HandlerRegistry(),
            worker_id="unloaded-" + uuid4().hex, lease_seconds=6)
        worker.run_once()
        self.assertEqual("failed", self.read(op["uid"])["phase"])
        self.assertEqual([], self.fake.calls)

    def test_delete_success_releases_binding_and_allows_registration(self):
        external = uuid4().hex
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            resource = service.register(connection_uid=self.connection_uid,
                resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                driver_id="fake.vm/v1", external_key=external, locator={},
                identity_evidence={"source": "test"})
            delete_op = service.execute(resource["uid"], action="delete", normalized_input={},
                plugin_id="fake", plugin_version="1", server_scope="test/issue3",
                request_id=uuid4().hex, correlation_id="delete")
        self.fake.execute_results = [ExecutionResult("succeeded", existence_state="absent")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            old = service.get_resource(resource["uid"])
            replacement = service.register(connection_uid=self.connection_uid,
                resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                driver_id="fake.vm/v1", external_key=external, locator={},
                identity_evidence={"source": "test"})
        self.assertEqual("closed", old["registration_state"])
        self.assertEqual("absent", old["existence_state"])
        self.assertNotEqual(resource["uid"], replacement["uid"])
        with Session(self.engine) as session:
            old_binding = session.execute(select(bindings).where(
                bindings.c.uid == delete_op["binding_uid"])).mappings().one()
        self.assertFalse(old_binding["active"])
        self.assertIsNotNone(old_binding["closed_at"])

    def test_delete_without_absence_fact_keeps_identity_reserved(self):
        external = uuid4().hex
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            resource = service.register(connection_uid=self.connection_uid,
                resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                driver_id="fake.vm/v1", external_key=external, locator={},
                identity_evidence={"source": "test"})
            delete_op = service.execute(resource["uid"], action="delete", normalized_input={},
                plugin_id="fake", plugin_version="1", server_scope="test/issue3",
                request_id=uuid4().hex, correlation_id="delete")
        self.fake.execute_results = [ExecutionResult("succeeded")]
        self.worker.run_once()
        self.assertEqual("unknown", self.read(delete_op["uid"])["phase"])
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                ResourceService(session, self.registry).unregister(resource["uid"])
        with Session(self.engine) as session:
            old_binding = session.execute(select(bindings).where(
                bindings.c.uid == delete_op["binding_uid"])).mappings().one()
        self.assertTrue(old_binding["active"])

    def test_cancel_pending_is_terminal_and_does_not_call_handler(self):
        external = uuid4().hex
        op = self.create(external_key=external)
        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).request_cancel(op["uid"])
        self.assertIsNone(self.worker.run_once())
        self.assertEqual("cancelled", self.read(op["uid"])["phase"])
        with Session(self.engine) as session, session.begin():
            replacement = ResourceService(session, self.registry).register(
                connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                external_key=external, locator={}, identity_evidence={"source": "test"})
        self.assertNotEqual(op["resource_uid"], replacement["uid"])
        with Session(self.engine) as session:
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
        self.assertFalse(binding["active"])
        self.assertEqual("absent", binding["existence_state"])
        self.assertEqual([], self.fake.calls)

    def test_cancel_after_claim_but_before_submit_releases_identity(self):
        external = uuid4().hex
        op = self.create(external_key=external)
        with Session(self.engine) as session, session.begin():
            claim = OperationExecutorRepository(session).claim(op["uid"], worker_id="waiting",
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=1))
        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).request_cancel(op["uid"])
        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).cancel_before_submit(op["uid"], worker_id="waiting",
                claim_revision=claim["claim_revision"])
        with Session(self.engine) as session, session.begin():
            replacement = ResourceService(session, self.registry).register(
                connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                external_key=external, locator={}, identity_evidence={"source": "test"})
        self.assertNotEqual(op["resource_uid"], replacement["uid"])
        self.assertEqual("cancelled", self.read(op["uid"])["phase"])
        self.assertEqual([], self.fake.calls)

    def test_cancel_during_submission_tracks_actual_success(self):
        op = self.create()

        def complete_after_cancel(*_):
            with Session(self.engine) as session, session.begin():
                OperationExecutorRepository(session).request_cancel(op["uid"])
            return ExecutionResult("succeeded", existence_state="present")

        self.fake.execute_results = [complete_after_cancel]
        self.worker.run_once()
        result = self.read(op["uid"])
        self.assertEqual("succeeded", result["phase"])
        self.assertTrue(result["cancellation_requested"])
        self.assertEqual(1, len(self.fake.calls))

    def test_saved_external_task_polls_frozen_target_after_credential_rotation(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("pending_external", external_task_ref="remote-1")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(connections).where(connections.c.uid == self.connection_uid)
                .values(revision=2, secret_version_ref="secret/v2"))
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))

        def poll_frozen(target, external_ref, _):
            self.assertEqual("remote-1", external_ref)
            self.assertEqual(1, target["connectionRevision"])
            self.assertEqual("secret/v1", target["secretVersionRef"])
            return ExecutionResult("succeeded", existence_state="present")

        self.fake.poll_results = [poll_frozen]
        restarted = OperationWorker(self.engine, self.registry,
            worker_id="rotated-" + uuid4().hex, lease_seconds=6)
        restarted.run_once()
        self.assertEqual("succeeded", self.read(op["uid"])["phase"])
        self.assertEqual(1, len([call for call in self.fake.calls if call[0] == "execute"]))

    def test_poll_after_rebind_preserves_newer_resource_fact(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("pending_external", external_task_ref="remote-1")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(bindings).where(bindings.c.uid == op["binding_uid"])
                .values(revision=2, locator={"node": "n2"}))
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))

        def poll_old_locator(target, _, __):
            self.assertEqual({"node": "n1"}, target["locator"])
            return ExecutionResult("succeeded", existence_state="present")

        self.fake.poll_results = [poll_old_locator]
        self.worker.run_once()
        self.assertEqual("succeeded", self.read(op["uid"])["phase"])
        with Session(self.engine) as session:
            resource = session.execute(select(resources).where(
                resources.c.uid == op["resource_uid"])).mappings().one()
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
        self.assertEqual("pending", resource["existence_state"])
        self.assertEqual("pending", binding["existence_state"])
        self.assertEqual(2, binding["revision"])

    def test_missing_plugin_keeps_known_external_task_recoverable(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("pending_external", external_task_ref="remote-1")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        missing = OperationWorker(self.engine, HandlerRegistry(), worker_id="missing", lease_seconds=6)
        missing.run_once()
        pending = self.read(op["uid"])
        self.assertEqual("pending_external", pending["phase"])
        self.assertEqual("remote-1", pending["external_task_ref"])
        self.assertEqual("PluginUnavailable", pending["error"]["code"])
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.fake.poll_results = [ExecutionResult("succeeded", existence_state="present")]
        self.worker.run_once()
        self.assertEqual("succeeded", self.read(op["uid"])["phase"])
        self.assertEqual(1, len([call for call in self.fake.calls if call[0] == "execute"]))

    def test_saved_external_acceptance_then_poll_failure_preserves_ref(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("pending_external", external_task_ref="remote-1")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.fake.poll_results = [RuntimeError("secret=do-not-leak")]
        self.worker.run_once()
        result = self.read(op["uid"])
        self.assertEqual("pending_external", result["phase"])
        self.assertEqual("remote-1", result["external_task_ref"])
        self.assertNotIn("do-not-leak", str(result))
        self.assertEqual(1, len([call for call in self.fake.calls if call[0] == "execute"]))

    def test_repeated_poll_can_preserve_external_ref_without_returning_it_again(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("pending_external", external_task_ref="remote-1")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.fake.poll_results = [ExecutionResult("pending_external")]
        self.worker.run_once()
        result = self.read(op["uid"])
        self.assertEqual("pending_external", result["phase"])
        self.assertEqual("remote-1", result["external_task_ref"])
        self.assertEqual(1, len([call for call in self.fake.calls if call[0] == "execute"]))

    def test_partial_failure_keeps_confirmed_external_fact_and_redacts_output(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("failed", existence_state="present",
            output={"vmid": 101, "password": "do-not-leak"}, error_code="ConfigFailed")]
        self.worker.run_once()
        result = self.read(op["uid"])
        self.assertEqual("failed", result["phase"])
        self.assertEqual(101, result["result"]["vmid"])
        self.assertNotIn("do-not-leak", str(result))
        with Session(self.engine) as session:
            resource = session.execute(select(resources).where(resources.c.uid == op["resource_uid"])).mappings().one()
        self.assertEqual("present", resource["existence_state"])

    def test_persist_failure_after_external_call_becomes_unknown_without_resubmit(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("succeeded")]
        worker = OperationWorker(self.engine, self.registry, worker_id="crashing", lease_seconds=6)

        def crash(*_):
            raise RuntimeError("simulated database outage")

        worker._persist = crash
        with self.assertRaisesRegex(RuntimeError, "database outage"):
            worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.worker.run_once()
        self.assertEqual("unknown", self.read(op["uid"])["phase"])
        self.assertEqual(1, len(self.fake.calls))

    def test_create_transport_key_is_safe_under_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor

        key, external = uuid4().hex, uuid4().hex
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.create(request_id=key, external_key=external), range(2)))
        self.assertEqual(results[0]["uid"], results[1]["uid"])
        with Session(self.engine) as session:
            self.assertEqual(1, len(session.execute(select(bindings.c.uid).where(
                bindings.c.external_key == external)).all()))

    def test_secret_in_command_is_rejected_before_admission(self):
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                ResourceService(session, self.registry).create(
                    connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                    plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                    external_key=uuid4().hex, locator={}, identity_evidence={"source": "test"},
                    attributes={}, normalized_input={"password": "plain-secret"},
                    server_scope="test/issue3", request_id=uuid4().hex, correlation_id="corr")
        self.assertEqual([], self.fake.calls)
