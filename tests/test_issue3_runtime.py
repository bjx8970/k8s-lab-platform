"""P2 resource execution and crash-recovery acceptance on disposable PostgreSQL."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import and_, create_engine, delete, event, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from migrations.runner import run_migrations
from modules.control_plane.repositories import (
    LeaseLost, OperationAdmissionRepository, OperationExecutorRepository, RequestConflict,
    UnknownOperationBlocked, canonical_digest,
)
from modules.control_plane.tables import bindings, connections, operations, request_keys, resources
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

    def test_missing_read_only_declaration_defaults_to_mutating(self):
        handler = SimpleNamespace(actions={"start"}, execute=lambda *_: None, poll=lambda *_: None)
        registry = HandlerRegistry()
        registry.register("minimal", "1", "minimal.vm/v1", handler)
        self.assertTrue(registry.is_mutating("minimal", "1", "minimal.vm/v1", "start"))

    def test_invalid_read_only_declaration_is_rejected_at_registration(self):
        for declaration in ("observe", None, ["undeclared"], [True]):
            with self.subTest(declaration=declaration):
                registry = HandlerRegistry()
                handler = SimpleNamespace(actions={"observe"}, read_only_actions=declaration)
                with self.assertRaises(ValueError):
                    registry.register("invalid", "1", "invalid.vm/v1", handler)
                with self.assertRaises(ValueError):
                    registry.get("invalid", "1", "invalid.vm/v1")

    def test_read_only_actions_are_frozen_at_registration(self):
        handler = SimpleNamespace(actions={"start", "observe"}, read_only_actions={"observe"})
        registry = HandlerRegistry()
        registry.register("fixed", "1", "fixed.vm/v1", handler)
        handler.read_only_actions.add("start")
        self.assertTrue(registry.is_mutating("fixed", "1", "fixed.vm/v1", "start"))
        self.assertFalse(registry.is_mutating("fixed", "1", "fixed.vm/v1", "observe"))


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
            operation_ids = select(operations.c.uid).where(operations.c.connection_uid == self.connection_uid)
            session.execute(delete(request_keys).where(request_keys.c.result_uid.in_(operation_ids)))
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

    def test_execute_rejects_create_on_registered_and_provisional_resources(self):
        provisional = self.create()
        with Session(self.engine) as session, session.begin():
            registered = ResourceService(session, self.registry).register(
                connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                external_key=uuid4().hex, locator={}, identity_evidence={"source": "test"})
        for resource_uid, request_id in ((registered["uid"], uuid4().hex),
                (provisional["resource_uid"], uuid4().hex),
                (provisional["resource_uid"], provisional["request_id"])):
            with self.subTest(resource_uid=resource_uid, request_id=request_id):
                with Session(self.engine) as session, session.begin():
                    with self.assertRaisesRegex(ValueError, "专用资源创建入口"):
                        ResourceService(session, self.registry).execute(resource_uid,
                            action="create", normalized_input={"image": "x"},
                            plugin_id="fake", plugin_version="1", server_scope="test/issue3",
                            request_id=request_id, correlation_id="rejected")
        with Session(self.engine) as session:
            operation_ids = session.execute(select(operations.c.uid).where(
                operations.c.connection_uid == self.connection_uid)).scalars().all()
            request_ids = session.execute(select(request_keys.c.result_uid).where(
                request_keys.c.result_uid.in_(operation_ids))).scalars().all()
        self.assertEqual([provisional["uid"]], operation_ids)
        self.assertEqual([provisional["uid"]], request_ids)
        self.assertEqual([], self.fake.calls)

    def test_repository_rejects_create_without_pending_provisional_identity(self):
        states = [("pending", "pending", False, True), ("present", "pending", True, True),
            ("absent", "pending", True, True), ("unknown", "pending", True, True),
            ("pending", "present", True, True), ("pending", "absent", True, True),
            ("pending", "unknown", True, True), ("pending", "pending", True, False)]
        for source_type in ("direct", "plan"):
            for resource_state, binding_state, provisional, mutating in states:
                with self.subTest(source=source_type, states=(resource_state, binding_state, provisional, mutating)):
                    with Session(self.engine) as session, session.begin():
                        service = ResourceService(session, self.registry)
                        connection = service._connection(self.connection_uid)
                        resource, binding = service._bind(connection=connection,
                            resource_type="compute.vm/v1", driver_id="fake.vm/v1", external_key=uuid4().hex,
                            locator={}, identity_evidence={"source": "test"}, attributes={}, provisional=provisional)
                        session.execute(update(resources).where(resources.c.uid == resource["uid"])
                            .values(existence_state=resource_state))
                        session.execute(update(bindings).where(bindings.c.uid == binding["uid"])
                            .values(existence_state=binding_state))
                        with self.assertRaisesRegex(ValueError, "provisional Binding"):
                            OperationAdmissionRepository(session).create(server_scope="test/issue3",
                                request_id=uuid4().hex, action="create", normalized_input={},
                                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                                resource_uid=resource["uid"], binding_uid=binding["uid"], binding_revision=1,
                                connection_uid=self.connection_uid, connection_revision=1,
                                source_type=source_type, correlation_id="rejected", is_mutating=mutating,
                                operation_key=uuid4().hex if source_type == "plan" else None,
                                attempt=0 if source_type == "plan" else None)
                        self.assertEqual(0, session.scalar(select(func.count()).select_from(operations)
                            .where(operations.c.resource_uid == resource["uid"])))
        self.assertEqual([], self.fake.calls)

    def test_preallocated_plan_create_succeeds_and_replays_after_completion(self):
        request_id, operation_key = uuid4().hex, uuid4().hex
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            resource, binding = service._bind(connection=service._connection(self.connection_uid),
                resource_type="compute.vm/v1", driver_id="fake.vm/v1", external_key=uuid4().hex,
                locator={}, identity_evidence={"source": "plan"}, attributes={}, provisional=True)
            params = dict(server_scope="test/plan", action="create", normalized_input={},
                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                resource_uid=resource["uid"], binding_uid=binding["uid"], binding_revision=1,
                connection_uid=self.connection_uid, connection_revision=1, source_type="plan",
                correlation_id="plan-first", operation_key=operation_key, attempt=0)
            operation = OperationAdmissionRepository(session).create(request_id=request_id, **params)
        self.fake.execute_results = [ExecutionResult("succeeded", existence_state="present")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            current_binding = session.execute(select(bindings).where(bindings.c.uid == binding["uid"])).mappings().one()
            self.assertFalse(current_binding["provisional"])
            first_replay = OperationAdmissionRepository(session).create(request_id=request_id, **params)
            second_replay = OperationAdmissionRepository(session).create(request_id=uuid4().hex, **params)
            self.assertEqual(operation["uid"], first_replay["uid"])
            self.assertEqual(operation["uid"], second_replay["uid"])
            self.assertEqual("succeeded", second_replay["phase"])
        self.assertEqual(1, len(self.fake.calls))

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
            self.assertIsNone(session.execute(select(request_keys).where(
                request_keys.c.request_id == key)).first())
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

    def test_create_replay_uses_frozen_request_after_registration_metadata_changes(self):
        key, external = uuid4().hex, uuid4().hex
        op = self.create(request_id=key, external_key=external)
        self.fake.execute_results = [ExecutionResult("succeeded", existence_state="present")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(resources).where(resources.c.uid == op["resource_uid"])
                .values(attributes={"image": "new", "label": "updated"}, revision=2))
            session.execute(update(bindings).where(bindings.c.uid == op["binding_uid"])
                .values(external_identity={"source": "new evidence"}, revision=2, locator={"node": "n2"}))
        replay = self.create(request_id=key, external_key=external)
        self.assertEqual(op["uid"], replay["uid"])
        self.assertEqual(op["correlation_id"], replay["correlation_id"])
        with Session(self.engine) as session:
            saved = session.execute(select(request_keys).where(
                request_keys.c.result_uid == op["uid"])).mappings().one()
        self.assertEqual(canonical_digest({"connectionId": str(self.connection_uid),
            "resourceType": "compute.vm/v1", "pluginId": "fake", "pluginVersion": "1",
            "driverId": "fake.vm/v1", "externalKey": external, "locator": {"node": "n1"},
            "identityEvidence": {"source": "test"}, "attributes": {"image": "x"},
            "input": {"image": "x"}}), saved["request_digest"])
        self.assertEqual(1, len(self.fake.calls))

    def test_create_request_digest_covers_all_admission_parameters(self):
        key, external = uuid4().hex, uuid4().hex
        op = self.create(request_id=key, external_key=external)
        params = dict(connection_uid=self.connection_uid, resource_type="compute.vm/v1",
            plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
            external_key=external, locator={"node": "n1"}, identity_evidence={"source": "test"},
            attributes={"image": "x"}, normalized_input={"image": "x"},
            server_scope="test/issue3", request_id=key, correlation_id="new-correlation")
        changes = {"connection_uid": uuid4(), "resource_type": "compute.other/v1",
            "plugin_id": "other", "plugin_version": "2", "driver_id": "other.vm/v1",
            "external_key": "other", "locator": {"node": "n2"},
            "identity_evidence": {"source": "changed"}, "attributes": {"image": "y"},
            "normalized_input": {"image": "y"}}
        for field, value in changes.items():
            with self.subTest(field=field), Session(self.engine) as session, session.begin():
                with self.assertRaises(RequestConflict):
                    ResourceService(session, self.registry).create(**{**params, field: value})
        self.assertEqual("pending", self.read(op["uid"])["phase"])
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

    def test_unknown_blocks_new_direct_and_plan_mutations_but_allows_replay_and_observe(self):
        op = self.create()
        self.fake.execute_results = [RuntimeError("uncertain submission")]
        self.worker.run_once()
        self.assertEqual("unknown", self.read(op["uid"])["phase"])
        with Session(self.engine) as session, session.begin():
            replay = OperationAdmissionRepository(session).create(
                server_scope=op["server_scope"], request_id=op["request_id"],
                action=op["action"], normalized_input=op["normalized_input"],
                plugin_id=op["plugin_id"], plugin_version=op["plugin_version"],
                driver_id=op["driver_id"], resource_uid=op["resource_uid"],
                binding_uid=op["binding_uid"], binding_revision=op["binding_revision"],
                connection_uid=op["connection_uid"], connection_revision=op["connection_revision"],
                source_type="direct", correlation_id="replayed")
            self.assertEqual(op["uid"], replay["uid"])
        for action in ("start", "stop", "create", "delete"):
            with self.subTest(action=action), Session(self.engine) as session, session.begin():
                with self.assertRaises(UnknownOperationBlocked):
                    OperationAdmissionRepository(session).create(server_scope="plan/test",
                        request_id=uuid4().hex, action=action, normalized_input={},
                        plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                        resource_uid=op["resource_uid"], binding_uid=op["binding_uid"],
                        binding_revision=op["binding_revision"], connection_uid=op["connection_uid"],
                        connection_revision=op["connection_revision"], source_type="plan",
                        correlation_id="plan", operation_key=uuid4().hex, attempt=0)
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            with self.assertRaises(UnknownOperationBlocked):
                service.execute(op["resource_uid"], action="stop", normalized_input={},
                    plugin_id="fake", plugin_version="1", server_scope="test/issue3",
                    request_id=uuid4().hex, correlation_id="blocked")
            observation = service.execute(op["resource_uid"], action="observe", normalized_input={},
                plugin_id="fake", plugin_version="1", server_scope="test/issue3",
                request_id=uuid4().hex, correlation_id="observe")
        self.assertFalse(observation["is_mutating"])
        with Session(self.engine) as session:
            count = session.scalar(select(func.count()).select_from(operations).where(and_(
                operations.c.resource_uid == op["resource_uid"], operations.c.is_mutating.is_(True))))
        self.assertEqual(1, count)
        self.assertEqual(1, len(self.fake.calls))

    def test_unknown_blocks_two_concurrent_admissions(self):
        op = self.create()
        self.fake.execute_results = [RuntimeError("uncertain submission")]
        self.worker.run_once()
        ready = threading.Barrier(2)

        def admit(action):
            ready.wait(timeout=5)
            with Session(self.engine) as session, session.begin():
                try:
                    ResourceService(session, self.registry).execute(op["resource_uid"],
                        action=action, normalized_input={}, plugin_id="fake", plugin_version="1",
                        server_scope="test/issue3", request_id=uuid4().hex, correlation_id="racing")
                except UnknownOperationBlocked:
                    return "blocked"
            return "admitted"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(admit, ("start", "stop")))
        self.assertEqual(["blocked", "blocked"], outcomes)
        with Session(self.engine) as session:
            rows = session.execute(select(operations.c.uid).where(
                operations.c.resource_uid == op["resource_uid"])).scalars().all()
        self.assertEqual([op["uid"]], rows)

    def test_unknown_transition_serializes_with_new_admission(self):
        from sqlalchemy.exc import DBAPIError

        op = self.create()
        with Session(self.engine) as session, session.begin():
            claim = OperationExecutorRepository(session).claim(op["uid"], worker_id="uncertain",
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=1))

        def admit_while_locked():
            try:
                with Session(self.engine) as session, session.begin():
                    session.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    OperationAdmissionRepository(session).create(server_scope="test/issue3",
                        request_id=uuid4().hex, action="stop", normalized_input={},
                        plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                        resource_uid=op["resource_uid"], binding_uid=op["binding_uid"],
                        binding_revision=op["binding_revision"], connection_uid=op["connection_uid"],
                        connection_revision=op["connection_revision"], source_type="direct",
                        correlation_id="racing")
            except DBAPIError as exc:
                return exc.orig.args[0]["C"]
            return "admitted"

        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).update_execution(op["uid"], worker_id="uncertain",
                claim_revision=claim["claim_revision"], phase="unknown")
            with ThreadPoolExecutor(max_workers=1) as pool:
                outcome = pool.submit(admit_while_locked).result(timeout=5)
            self.assertEqual("55P03", outcome)
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(UnknownOperationBlocked):
                ResourceService(session, self.registry).execute(op["resource_uid"],
                    action="stop", normalized_input={}, plugin_id="fake", plugin_version="1",
                    server_scope="test/issue3", request_id=uuid4().hex, correlation_id="after-commit")

    def test_handler_without_read_only_actions_admits_as_mutating(self):
        handler = SimpleNamespace(actions={"start"}, execute=self.fake.execute, poll=self.fake.poll)
        self.registry.register("minimal", "1", "fake.vm/v1", handler)
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            resource = service.register(connection_uid=self.connection_uid,
                resource_type="compute.vm/v1", plugin_id="minimal", plugin_version="1",
                driver_id="fake.vm/v1", external_key=uuid4().hex, locator={},
                identity_evidence={"source": "test"})
            operation = service.execute(resource["uid"], action="start", normalized_input={},
                plugin_id="minimal", plugin_version="1", server_scope="test/issue3",
                request_id=uuid4().hex, correlation_id="minimal")
        self.assertTrue(operation["is_mutating"])
        self.fake.execute_results = [ExecutionResult("succeeded")]
        self.worker.run_once()
        self.assertEqual("succeeded", self.read(operation["uid"])["phase"])

    def _assert_direct_plan_lock_order(self, plan_is_mutating):
        with Session(self.engine) as session, session.begin():
            resource = ResourceService(session, self.registry).register(
                connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                external_key=uuid4().hex, locator={}, identity_evidence={"source": "test"})
            binding = session.execute(select(bindings).where(
                bindings.c.resource_uid == resource["uid"])).mappings().one()
        resource_locked, direct_ready, plan_finished = (threading.Event() for _ in range(3))

        class SynchronizedService(ResourceService):
            def _handler(service, *args):
                handler = super()._handler(*args)
                direct_ready.set()
                if not plan_finished.wait(timeout=5):
                    raise RuntimeError("plan admission 未能完成")
                return handler

        def plan_admission():
            try:
                with Session(self.engine) as session, session.begin():
                    session.execute(text("SET LOCAL lock_timeout = '1s'"))
                    session.execute(select(resources.c.uid).where(
                        resources.c.uid == resource["uid"]).with_for_update())
                    resource_locked.set()
                    if not direct_ready.wait(timeout=5):
                        raise RuntimeError("direct admission 未开始")
                    operation = OperationAdmissionRepository(session).create(
                        server_scope="test/plan", request_id=uuid4().hex,
                        action="start" if plan_is_mutating else "observe", normalized_input={},
                        plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                        resource_uid=resource["uid"], binding_uid=binding["uid"], binding_revision=1,
                        connection_uid=self.connection_uid, connection_revision=1,
                        source_type="plan", correlation_id="plan", operation_key=uuid4().hex,
                        attempt=0, is_mutating=plan_is_mutating)
                return operation["uid"]
            finally:
                plan_finished.set()

        def direct_admission():
            if not resource_locked.wait(timeout=5):
                raise RuntimeError("plan Resource 行锁未获取")
            try:
                with Session(self.engine) as session, session.begin():
                    session.execute(text("SET LOCAL lock_timeout = '1s'"))
                    operation = SynchronizedService(session, self.registry).execute(resource["uid"],
                        action="stop", normalized_input={}, plugin_id="fake", plugin_version="1",
                        server_scope="test/issue3", request_id=uuid4().hex, correlation_id="direct")
                return operation["uid"]
            except IntegrityError as exc:
                if exc.orig.args[0]["n"] != "uq_rf_operation_mutation":
                    raise
                return "mutation-conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            plan_future = pool.submit(plan_admission)
            direct_future = pool.submit(direct_admission)
            plan_uid = plan_future.result(timeout=10)
            direct_result = direct_future.result(timeout=10)
        if plan_is_mutating:
            self.assertEqual("mutation-conflict", direct_result)
        else:
            self.assertNotEqual(plan_uid, direct_result)
        with Session(self.engine) as session:
            mutating_count = session.scalar(select(func.count()).select_from(operations).where(and_(
                operations.c.resource_uid == resource["uid"], operations.c.is_mutating.is_(True))))
        self.assertEqual(1, mutating_count)
        self.assertEqual([], self.fake.calls)

    def test_direct_and_read_only_plan_admission_follow_the_same_lock_order(self):
        self._assert_direct_plan_lock_order(plan_is_mutating=False)

    def test_direct_and_mutating_plan_admission_preserve_mutex_without_deadlock(self):
        self._assert_direct_plan_lock_order(plan_is_mutating=True)

    def test_mutating_operation_is_serial_per_resource(self):
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            resource = service.register(connection_uid=self.connection_uid,
                resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                driver_id="fake.vm/v1", external_key=uuid4().hex, locator={},
                identity_evidence={"source": "test"})
            service.execute(resource["uid"], action="start", normalized_input={},
                plugin_id="fake", plugin_version="1", server_scope="test/issue3",
                request_id=uuid4().hex, correlation_id="first")
        with self.assertRaises(IntegrityError):
            with Session(self.engine) as session, session.begin():
                ResourceService(session, self.registry).execute(resource["uid"],
                    action="stop", normalized_input={}, plugin_id="fake", plugin_version="1",
                    server_scope="test/issue3", request_id=uuid4().hex, correlation_id="second")

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
        external = uuid4().hex
        op = self.create(external_key=external)
        with Session(self.engine) as session, session.begin():
            session.execute(update(connections).where(connections.c.uid == self.connection_uid)
                .values(revision=2, secret_version_ref="secret/v2"))
        self.worker.run_once()
        failed = self.read(op["uid"])
        self.assertEqual("failed", failed["phase"])
        self.assertEqual("TargetRevisionConflict", failed["error"]["code"])
        self.assertEqual([], self.fake.calls)
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            replacement = service.register(connection_uid=self.connection_uid,
                resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                driver_id="fake.vm/v1", external_key=external, locator={},
                identity_evidence={"source": "test"})
            service.unregister(op["resource_uid"])
            self.assertNotEqual(op["resource_uid"], replacement["uid"])
        with Session(self.engine) as session:
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
        self.assertFalse(binding["active"])
        self.assertIsNotNone(binding["closed_at"])

    def test_changed_binding_fails_before_external_call(self):
        external = uuid4().hex
        op = self.create(external_key=external)
        with Session(self.engine) as session, session.begin():
            session.execute(update(bindings).where(bindings.c.uid == op["binding_uid"])
                .values(revision=2, locator={"node": "n2"}))
        self.worker.run_once()
        self.assertEqual("failed", self.read(op["uid"])["phase"])
        self.assertEqual("TargetRevisionConflict", self.read(op["uid"])["error"]["code"])
        with Session(self.engine) as session, session.begin():
            ResourceService(session, self.registry).unregister(op["resource_uid"])
            replacement = ResourceService(session, self.registry).register(
                connection_uid=self.connection_uid, resource_type="compute.vm/v1",
                plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                external_key=external, locator={}, identity_evidence={"source": "test"})
        self.assertNotEqual(op["resource_uid"], replacement["uid"])
        self.assertEqual([], self.fake.calls)

    def test_missing_plugin_does_not_dispatch_to_another_version(self):
        external = uuid4().hex
        op = self.create(external_key=external)
        worker = OperationWorker(self.engine, HandlerRegistry(),
            worker_id="unloaded-" + uuid4().hex, lease_seconds=6)
        worker.run_once()
        failed = self.read(op["uid"])
        self.assertEqual("failed", failed["phase"])
        self.assertEqual("PluginUnavailable", failed["error"]["code"])
        self.assertEqual([], self.fake.calls)
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            service.unregister(op["resource_uid"])
            replacement = service.register(connection_uid=self.connection_uid,
                resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                driver_id="fake.vm/v1", external_key=external, locator={},
                identity_evidence={"source": "test"})
        self.assertNotEqual(op["resource_uid"], replacement["uid"])

    def test_unknown_create_keeps_provisional_identity(self):
        op = self.create()
        self.fake.execute_results = [RuntimeError("uncertain")]
        self.worker.run_once()
        unknown = self.read(op["uid"])
        self.assertEqual("unknown", unknown["phase"])
        with Session(self.engine) as session:
            resource = session.execute(select(resources).where(
                resources.c.uid == op["resource_uid"])).mappings().one()
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
        self.assertEqual("pending", resource["existence_state"])
        self.assertTrue(binding["active"])
        self.assertTrue(binding["provisional"])
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(ValueError):
                ResourceService(session, self.registry).unregister(op["resource_uid"])
        self.assertEqual(1, len(self.fake.calls))

    def test_failed_poll_without_error_code_replaces_stale_plugin_error(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("pending_external", external_task_ref="remote-1")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        missing = OperationWorker(self.engine, HandlerRegistry(), worker_id="missing", lease_seconds=6)
        missing.run_once()
        self.assertEqual("PluginUnavailable", self.read(op["uid"])["error"]["code"])
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.fake.poll_results = [ExecutionResult("failed")]
        self.worker.run_once()
        result = self.read(op["uid"])
        self.assertEqual("failed", result["phase"])
        self.assertEqual({"code": "ProviderFailed"}, result["error"])
        self.assertEqual("remote-1", result["external_task_ref"])
        self.assertIsNotNone(result["finished_at"])
        with Session(self.engine) as session:
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
        self.assertTrue(binding["active"])
        self.assertEqual(1, len([call for call in self.fake.calls if call[0] == "execute"]))

    def test_failed_poll_with_error_code_replaces_stale_poll_error(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("pending_external", external_task_ref="remote-1")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.fake.poll_results = [RuntimeError("poll temporarily unavailable")]
        self.worker.run_once()
        self.assertEqual("PollUnavailable", self.read(op["uid"])["error"]["code"])
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.fake.poll_results = [ExecutionResult("failed", error_code="InstallFailed")]
        self.worker.run_once()
        result = self.read(op["uid"])
        self.assertEqual("failed", result["phase"])
        self.assertEqual({"code": "InstallFailed"}, result["error"])
        self.assertEqual("remote-1", result["external_task_ref"])
        self.assertEqual(1, len([call for call in self.fake.calls if call[0] == "execute"]))

    def test_execute_failure_without_error_code_persists_provider_failed(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("failed")]
        self.worker.run_once()
        result = self.read(op["uid"])
        self.assertEqual("failed", result["phase"])
        self.assertEqual({"code": "ProviderFailed"}, result["error"])
        with Session(self.engine) as session:
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
        self.assertTrue(binding["active"])
        self.assertIsNone(binding["closed_at"])
        self.assertEqual(1, len(self.fake.calls))

    def test_failed_create_without_existence_blocks_new_direct_and_plan_mutations(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("failed")]
        self.worker.run_once()
        self.assertEqual("failed", self.read(op["uid"])["phase"])
        with Session(self.engine) as session:
            resource = session.execute(select(resources).where(
                resources.c.uid == op["resource_uid"])).mappings().one()
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
        self.assertEqual("pending", resource["existence_state"])
        self.assertEqual("pending", binding["existence_state"])
        self.assertTrue(binding["active"])
        self.assertTrue(binding["provisional"])
        for action in ("start", "stop", "delete"):
            with self.subTest(source="direct", action=action):
                with Session(self.engine) as session, session.begin():
                    with self.assertRaises(UnknownOperationBlocked):
                        ResourceService(session, self.registry).execute(op["resource_uid"],
                            action=action, normalized_input={}, plugin_id="fake", plugin_version="1",
                            server_scope="test/issue3", request_id=uuid4().hex, correlation_id="blocked")
        for action in ("start", "stop", "delete", "create"):
            with self.subTest(source="plan", action=action):
                with Session(self.engine) as session, session.begin():
                    with self.assertRaises(UnknownOperationBlocked):
                        OperationAdmissionRepository(session).create(
                            server_scope="test/plan", request_id=uuid4().hex, action=action,
                            normalized_input={}, plugin_id="fake", plugin_version="1",
                            driver_id="fake.vm/v1", resource_uid=op["resource_uid"],
                            binding_uid=op["binding_uid"], binding_revision=1,
                            connection_uid=self.connection_uid, connection_revision=1,
                            source_type="plan", correlation_id="blocked", operation_key=uuid4().hex,
                            attempt=0)
        with Session(self.engine) as session:
            operation_ids = session.execute(select(operations.c.uid).where(
                operations.c.resource_uid == op["resource_uid"])).scalars().all()
        self.assertEqual([op["uid"]], operation_ids)
        self.assertEqual([("execute", str(op["resource_uid"]), "create")], self.fake.calls)

    def test_failed_create_replay_and_observe_do_not_unlock_mutations(self):
        key, external = uuid4().hex, uuid4().hex
        op = self.create(request_id=key, external_key=external)
        self.fake.execute_results = [ExecutionResult("failed")]
        self.worker.run_once()
        replay = self.create(request_id=key, external_key=external)
        self.assertEqual(op["uid"], replay["uid"])
        self.assertEqual("failed", replay["phase"])
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            observation = service.execute(op["resource_uid"], action="observe", normalized_input={},
                plugin_id="fake", plugin_version="1", server_scope="test/issue3",
                request_id=uuid4().hex, correlation_id="observe")
        self.assertFalse(observation["is_mutating"])
        self.fake.execute_results = [ExecutionResult("succeeded", existence_state="present")]
        self.assertEqual(observation["uid"], self.worker.run_once())
        with Session(self.engine) as session, session.begin():
            service = ResourceService(session, self.registry)
            self.assertEqual("pending", service.get_resource(op["resource_uid"])["existence_state"])
            with self.assertRaises(UnknownOperationBlocked):
                service.execute(op["resource_uid"], action="start", normalized_input={},
                    plugin_id="fake", plugin_version="1", server_scope="test/issue3",
                    request_id=uuid4().hex, correlation_id="still-blocked")
        self.assertEqual("failed", self.read(op["uid"])["phase"])
        self.assertEqual(2, len(self.fake.calls))

    def test_failed_create_admission_resumes_after_resource_and_binding_are_confirmed(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("failed")]
        self.worker.run_once()
        # Simulate an approved fact reconciliation; observe itself must not write these facts.
        with Session(self.engine) as session, session.begin():
            session.execute(update(resources).where(resources.c.uid == op["resource_uid"])
                .values(existence_state="present"))
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(UnknownOperationBlocked):
                ResourceService(session, self.registry).execute(op["resource_uid"],
                    action="start", normalized_input={}, plugin_id="fake", plugin_version="1",
                    server_scope="test/issue3", request_id=uuid4().hex, correlation_id="binding-unconfirmed")
        with Session(self.engine) as session, session.begin():
            session.execute(select(resources.c.uid).where(
                resources.c.uid == op["resource_uid"]).with_for_update())
            session.execute(update(bindings).where(bindings.c.uid == op["binding_uid"])
                .values(existence_state="present", provisional=False))
            start = ResourceService(session, self.registry).execute(op["resource_uid"],
                action="start", normalized_input={}, plugin_id="fake", plugin_version="1",
                server_scope="test/issue3", request_id=uuid4().hex, correlation_id="confirmed")
        self.fake.execute_results = [ExecutionResult("succeeded")]
        self.assertEqual(start["uid"], self.worker.run_once())
        self.assertEqual("succeeded", self.read(start["uid"])["phase"])
        self.assertEqual("failed", self.read(op["uid"])["phase"])
        self.assertEqual(2, len(self.fake.calls))

    def test_failed_poll_without_existence_blocks_followup_mutation(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("pending_external", external_task_ref="failed-job")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.fake.poll_results = [ExecutionResult("failed")]
        self.worker.run_once()
        failed = self.read(op["uid"])
        self.assertEqual("failed", failed["phase"])
        self.assertEqual("failed-job", failed["external_task_ref"])
        with Session(self.engine) as session, session.begin():
            with self.assertRaises(UnknownOperationBlocked):
                ResourceService(session, self.registry).execute(op["resource_uid"],
                    action="stop", normalized_input={}, plugin_id="fake", plugin_version="1",
                    server_scope="test/issue3", request_id=uuid4().hex, correlation_id="blocked")
        self.assertEqual(2, len(self.fake.calls))

    def test_unknown_resource_or_binding_existence_blocks_new_mutation(self):
        for resource_state, binding_state in (("unknown", "present"), ("present", "unknown"),
                ("pending", "present"), ("present", "pending")):
            with self.subTest(resource=resource_state, binding=binding_state):
                with Session(self.engine) as session, session.begin():
                    service = ResourceService(session, self.registry)
                    resource = service.register(connection_uid=self.connection_uid,
                        resource_type="compute.vm/v1", plugin_id="fake", plugin_version="1",
                        driver_id="fake.vm/v1", external_key=uuid4().hex, locator={},
                        identity_evidence={"source": "test"})
                    session.execute(update(resources).where(resources.c.uid == resource["uid"])
                        .values(existence_state=resource_state))
                    session.execute(update(bindings).where(bindings.c.resource_uid == resource["uid"])
                        .values(existence_state=binding_state))
                    with self.assertRaises(UnknownOperationBlocked):
                        service.execute(resource["uid"], action="start", normalized_input={},
                            plugin_id="fake", plugin_version="1", server_scope="test/issue3",
                            request_id=uuid4().hex, correlation_id="blocked")
        self.assertEqual([], self.fake.calls)

    def test_poll_recovery_clears_transient_error(self):
        op = self.create()
        self.fake.execute_results = [ExecutionResult("pending_external", external_task_ref="remote-1")]
        self.worker.run_once()
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        missing = OperationWorker(self.engine, HandlerRegistry(), worker_id="missing", lease_seconds=6)
        missing.run_once()
        degraded = self.read(op["uid"])
        self.assertEqual("pending_external", degraded["phase"])
        self.assertEqual("PluginUnavailable", degraded["error"]["code"])
        with Session(self.engine) as session, session.begin():
            session.execute(update(operations).where(operations.c.uid == op["uid"])
                .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.fake.poll_results = [ExecutionResult("succeeded", existence_state="present")]
        self.worker.run_once()
        final = self.read(op["uid"])
        self.assertEqual("succeeded", final["phase"])
        self.assertIsNone(final["error"])
        self.assertEqual(1, len([call for call in self.fake.calls if call[0] == "execute"]))

    def test_cancel_release_clears_transient_error(self):
        op = self.create()
        with Session(self.engine) as session, session.begin():
            OperationExecutorRepository(session).request_cancel(op["uid"])
        self.assertIsNone(self.worker.run_once())
        self.assertEqual("cancelled", self.read(op["uid"])["phase"])

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

    def test_pending_cancel_rechecks_phase_after_waiting_for_target_lock(self):
        op = self.create()
        target_read = threading.Event()

        class CoordinatedExecutor(OperationExecutorRepository):
            def _lock_target(repo, operation, *, lock_operation):
                target_read.set()
                return super()._lock_target(operation, lock_operation=lock_operation)

        def cancel():
            with Session(self.engine) as session, session.begin():
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
                return CoordinatedExecutor(session).request_cancel(op["uid"])

        with ThreadPoolExecutor(max_workers=1) as pool:
            with Session(self.engine) as session, session.begin():
                session.execute(select(resources.c.uid).where(
                    resources.c.uid == op["resource_uid"]).with_for_update())
                future = pool.submit(cancel)
                self.assertTrue(target_read.wait(timeout=5))
                repo = OperationExecutorRepository(session)
                claim = repo.claim(op["uid"], worker_id="submitted",
                    lease_until=datetime.now(timezone.utc) + timedelta(minutes=1))
                repo.update_execution(op["uid"], worker_id="submitted",
                    claim_revision=claim["claim_revision"], phase="pending_external",
                    external_task_ref="remote-1")
            result = future.result(timeout=10)
        self.assertEqual("pending_external", result["phase"])
        self.assertTrue(result["cancellation_requested"])
        self.assertEqual("remote-1", result["external_task_ref"])
        with Session(self.engine) as session:
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
            resource = session.execute(select(resources).where(
                resources.c.uid == op["resource_uid"])).mappings().one()
        self.assertTrue(binding["active"])
        self.assertIsNone(binding["closed_at"])
        self.assertEqual("pending", resource["existence_state"])
        self.assertEqual([], self.fake.calls)

    def test_cancel_before_submit_rechecks_claim_after_waiting_for_target_lock(self):
        op = self.create()
        target_read = threading.Event()
        with Session(self.engine) as session, session.begin():
            repo = OperationExecutorRepository(session)
            old_claim = repo.claim(op["uid"], worker_id="old",
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=1))
            repo.request_cancel(op["uid"])

        class CoordinatedExecutor(OperationExecutorRepository):
            def _lock_target(repo, operation, *, lock_operation):
                target_read.set()
                return super()._lock_target(operation, lock_operation=lock_operation)

        def cancel():
            with Session(self.engine) as session, session.begin():
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
                return CoordinatedExecutor(session).cancel_before_submit(op["uid"], worker_id="old",
                    claim_revision=old_claim["claim_revision"])

        with ThreadPoolExecutor(max_workers=1) as pool:
            with Session(self.engine) as session, session.begin():
                session.execute(select(resources.c.uid).where(
                    resources.c.uid == op["resource_uid"]).with_for_update())
                future = pool.submit(cancel)
                self.assertTrue(target_read.wait(timeout=5))
                session.execute(update(operations).where(operations.c.uid == op["uid"])
                    .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
                new_claim = OperationExecutorRepository(session).claim(op["uid"], worker_id="new",
                    lease_until=datetime.now(timezone.utc) + timedelta(minutes=1))
            result = future.result(timeout=10)
        self.assertIsNone(result)
        current = self.read(op["uid"])
        self.assertEqual("running", current["phase"])
        self.assertEqual("new", current["lease_owner"])
        self.assertGreater(new_claim["claim_revision"], old_claim["claim_revision"])
        with Session(self.engine) as session:
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
            resource = session.execute(select(resources).where(
                resources.c.uid == op["resource_uid"])).mappings().one()
        self.assertTrue(binding["active"])
        self.assertIsNone(binding["closed_at"])
        self.assertEqual("pending", resource["existence_state"])
        self.assertEqual([], self.fake.calls)

    def _assert_cancel_admission_lock_order(self, *, before_submit):
        from sqlalchemy.exc import DBAPIError

        op = self.create()
        claim = None
        if before_submit:
            with Session(self.engine) as session, session.begin():
                repo = OperationExecutorRepository(session)
                claim = repo.claim(op["uid"], worker_id="waiting",
                    lease_until=datetime.now(timezone.utc) + timedelta(minutes=1))
                repo.request_cancel(op["uid"])
        cancel_at_target, admission_locked_target, cancel_lock_requested = (
            threading.Event() for _ in range(3))
        cancel_sql = []

        def record_cancel_sql(connection, cursor, statement, parameters, context, executemany):
            if "FOR UPDATE" in statement:
                for table in ("rf_resources", "rf_bindings", "rf_operations"):
                    if "FROM " + table in statement:
                        cancel_sql.append("lock:" + table)
            elif statement.startswith("UPDATE rf_operations "):
                cancel_sql.append("update:rf_operations")

        class CoordinatedExecutor(OperationExecutorRepository):
            def _lock_target(repo, operation, *, lock_operation):
                cancel_at_target.set()
                if not admission_locked_target.wait(timeout=5):
                    raise RuntimeError("admission 未能取得目标行锁")
                cancel_lock_requested.set()
                return super()._lock_target(operation, lock_operation=lock_operation)

        class CoordinatedAdmission(OperationAdmissionRepository):
            def _resolve_target(repo, *args, **kwargs):
                snapshot = super()._resolve_target(*args, **kwargs)
                admission_locked_target.set()
                if not cancel_lock_requested.wait(timeout=5):
                    raise RuntimeError("cancel 未请求目标行锁")
                return snapshot

        def cancel():
            try:
                with Session(self.engine) as session, session.begin():
                    session.execute(text("SET LOCAL lock_timeout = '5s'"))
                    session.execute(text("SET LOCAL statement_timeout = '8s'"))
                    event.listen(session.connection(), "before_cursor_execute", record_cancel_sql)
                    repo = CoordinatedExecutor(session)
                    row = repo.cancel_before_submit(op["uid"], worker_id="waiting",
                        claim_revision=claim["claim_revision"]) if before_submit else repo.request_cancel(op["uid"])
                    return row["phase"] if row is not None else "not-cancelled"
            except DBAPIError as exc:
                return exc.orig.args[0].get("C")

        def admit():
            if not cancel_at_target.wait(timeout=5):
                raise RuntimeError("cancel 未开始目标锁定")
            try:
                with Session(self.engine) as session, session.begin():
                    session.execute(text("SET LOCAL lock_timeout = '5s'"))
                    session.execute(text("SET LOCAL statement_timeout = '8s'"))
                    CoordinatedAdmission(session).create(server_scope="test/issue3",
                        request_id=uuid4().hex, action="start", normalized_input={},
                        plugin_id="fake", plugin_version="1", driver_id="fake.vm/v1",
                        resource_uid=op["resource_uid"], binding_uid=op["binding_uid"],
                        binding_revision=op["binding_revision"], connection_uid=op["connection_uid"],
                        connection_revision=op["connection_revision"], source_type="direct",
                        correlation_id="race")
                return "admitted"
            except UnknownOperationBlocked:
                return "existence-blocked"
            except IntegrityError as exc:
                self.assertEqual("uq_rf_operation_mutation", exc.orig.args[0].get("n"))
                return "mutation-conflict"
            except DBAPIError as exc:
                return exc.orig.args[0].get("C")

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel_future = pool.submit(cancel)
            admit_future = pool.submit(admit)
            cancel_result = cancel_future.result(timeout=15)
            admit_result = admit_future.result(timeout=15)
        self.assertEqual("cancelled", cancel_result)
        self.assertEqual("existence-blocked", admit_result)
        # Admission now rejects before INSERT; explicitly retain the cancellation lock-order regression.
        self.assertEqual(["lock:rf_resources", "lock:rf_bindings", "lock:rf_operations",
            "update:rf_operations"], cancel_sql[:4])
        self.assertEqual("cancelled", self.read(op["uid"])["phase"])
        with Session(self.engine) as session:
            binding = session.execute(select(bindings).where(
                bindings.c.uid == op["binding_uid"])).mappings().one()
            resource = session.execute(select(resources).where(
                resources.c.uid == op["resource_uid"])).mappings().one()
            operation_ids = session.execute(select(operations.c.uid).where(
                operations.c.resource_uid == op["resource_uid"])).scalars().all()
        self.assertFalse(binding["active"])
        self.assertIsNotNone(binding["closed_at"])
        self.assertEqual("absent", resource["existence_state"])
        self.assertEqual([op["uid"]], operation_ids)
        self.assertEqual([], self.fake.calls)

    def test_pending_cancel_and_admission_use_resource_first_lock_order(self):
        self._assert_cancel_admission_lock_order(before_submit=False)

    def test_claimed_cancel_and_admission_use_resource_first_lock_order(self):
        self._assert_cancel_admission_lock_order(before_submit=True)

    def test_failed_create_request_key_replays_terminal_operation(self):
        key, external = uuid4().hex, uuid4().hex
        op = self.create(request_id=key, external_key=external)
        with Session(self.engine) as session, session.begin():
            session.execute(update(bindings).where(bindings.c.uid == op["binding_uid"])
                .values(revision=2))
        self.worker.run_once()
        self.assertEqual("failed", self.read(op["uid"])["phase"])
        replay = self.create(request_id=key, external_key=external)
        self.assertEqual(op["uid"], replay["uid"])
        self.assertEqual("failed", replay["phase"])

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
