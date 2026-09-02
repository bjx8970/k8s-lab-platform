"""Independent retry acceptance through real HTTP, service and SQLite boundaries.

Source jobs are created through real services. Tests never construct private
retry descriptors, replay provider calls, or inherit another acceptance case.
"""

import importlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash

# This helper import guards the first DB import without importing a TestCase
# into this module's namespace (unittest would collect that class a second time).
from tests.test_issue1_acceptance import CaptureAudit, db


FAKE_PASSWORD = "FICTITIOUS_RETRY_PASSWORD_DO_NOT_EXPOSE"
FAKE_KEY_BODY = "FICTITIOUS_RETRY_KEY_BODY_DO_NOT_EXPOSE"
FAKE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n" + FAKE_KEY_BODY
    + "\n-----END OPENSSH PRIVATE KEY-----"
)


class Issue1RetryAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ssh = importlib.import_module("modules.ssh_terminal")
        status_cache = importlib.import_module("modules.status_cache")
        manager = importlib.import_module("modules.k8s_manager")
        previous_app = sys.modules.pop("app", None)
        callback = manager._on_task_update
        try:
            with (
                patch.object(db, "is_db_configured", return_value=False),
                patch.object(ssh, "db_get_config", return_value=None),
                patch.object(ssh.SSHManager, "_start_cleanup_thread"),
                patch.object(status_cache, "start_monitor"),
            ):
                cls.web = importlib.import_module("app")
        finally:
            manager._on_task_update = callback
            if previous_app is None:
                sys.modules.pop("app", None)
            else:
                sys.modules["app"] = previous_app
        cls.manager = manager
        cls.web.is_db_configured = db.is_db_configured
        cls.web.app.config.update(
            TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False,
            SECRET_KEY="isolated-retry-session-key",
        )

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="issue1-retry-")
        )
        engine = create_engine("sqlite:///" + str(Path(directory) / "test.sqlite"))
        self.stack.callback(engine.dispose)
        self.stack.enter_context(patch.object(db, "engine", engine))
        self.stack.enter_context(patch.object(db, "SessionLocal", sessionmaker(bind=engine)))
        self.stack.enter_context(patch.dict(os.environ, {
            "K8S_LAB_CREDENTIAL_KEY": Fernet.generate_key().decode("ascii"),
        }))
        db.Base.metadata.create_all(engine)
        self.stack.enter_context(patch.object(self.manager, "_task_store", {}))
        self.stack.enter_context(patch.object(self.manager, "_task_cancel_events", {}))
        # Isolate private state without fabricating trusted execution descriptions.
        for name, empty in (("_task_retry_descriptions", {}),
                            ("_task_retry_reservations", {}),
                            ("_task_retry_pending", set())):
            self.stack.enter_context(patch.object(self.manager, name, empty))
        self.stack.enter_context(patch.object(
            self.manager, "_on_task_update", self.web._emit_task_update,
        ))
        self.stack.enter_context(patch.object(self.web, "_allowed_origins", []))
        self.stack.enter_context(patch.object(self.web, "_setup_complete", True))
        for registry in ("_online_users", "_state_sid_users", "_webssh_sid_users",
                         "_webssh_connect_times"):
            self.stack.enter_context(patch.object(self.web, registry, {}))
        self.queued = []
        self.enqueue = self.stack.enter_context(patch.object(
            self.manager.scheduler, "enqueue",
            side_effect=lambda queue, task: self.queued.append((queue, task)),
        ))
        self.provider_guards = [
            self.stack.enter_context(patch.object(
                self.manager, name,
                side_effect=AssertionError("Real infrastructure is forbidden"),
            )) for name in ("PVEClient", "OpenWrtClient")
        ]
        self.provider_guards.append(self.stack.enter_context(patch.object(
            self.manager._SSHClient, "connect",
            side_effect=AssertionError("Real SSH is forbidden"),
        )))
        self.provider_guards.append(self.stack.enter_context(patch(
            "socket.create_connection",
            side_effect=AssertionError("Real network connections are forbidden"),
        )))
        self.provider_guards.append(self.stack.enter_context(patch(
            "socket.socket.connect",
            side_effect=AssertionError("Real network connections are forbidden"),
        )))
        self.addCleanup(self.assert_no_providers)
        password_hash = generate_password_hash("fictitious-retry-login-password")
        self.admin, self.teacher, self.other_teacher = [
            db.create_user({"username": name, "role": role, "password_hash": password_hash})
            for name, role in (("retry-admin", "admin"), ("retry-owner", "teacher"),
                               ("retry-other", "teacher"))
        ]
        self.student = db.create_user({
            "username": "retry-student", "role": "student",
            "created_by": self.teacher, "password_hash": password_hash,
        })
        self.class_id = db.create_class({"name": "retry-class", "created_by": self.teacher})
        self.group_id = db.create_group({
            "name": "retry-group", "class_id": self.class_id,
            "created_by": self.teacher, "max_students": 1,
        })
        db.add_group_member(self.group_id, self.student)
        self.cluster_name = "k8s_retry_acceptance"
        db.save_cluster(self.cluster_name, {
            "status": "running", "created_by": self.teacher,
            "group_id": self.group_id, "class_id": self.class_id,
            "pve_server_id": 7, "pve_node": "retry-node", "ssh_port": 50001,
            "ssh_private_key": FAKE_KEY, "password": FAKE_PASSWORD,
            "students": {"student1": {"password": FAKE_PASSWORD}},
            "vms": {"client": {"node": "retry-node", "vmid": 901,
                                "role": "client", "ip": "192.0.2.101"}},
        })

    def assert_no_providers(self):
        for guard in self.provider_guards:
            guard.assert_not_called()

    def client_for(self, actor=None):
        client = self.web.app.test_client()
        if actor is not None:
            with client.session_transaction() as session:
                session["_user_id"] = str(actor)
                session["_fresh"] = True
        return client

    def csrf(self, client):
        response = client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["csrf_token"]

    def mutation(self, client, method, path, payload=None, origin=None):
        headers = {"X-CSRFToken": self.csrf(client)}
        if origin is not None:
            headers["Origin"] = origin
        return client.open(path, method=method, json=payload, headers=headers)

    def retry(self, source_id, actor, payload=None, origin=None):
        return self.mutation(
            self.client_for(actor), "POST", f"/api/k8s/tasks/{source_id}/retry",
            payload=payload, origin=origin,
        )

    @staticmethod
    def run_queued(item):
        task = item[1]
        task.fn(*task.args, **task.kwargs)

    def clear_queue_capture(self):
        self.queued.clear()
        self.enqueue.reset_mock()

    def source_job(self, operation="deploy", status="error", actor=None):
        actor = self.admin if actor is None else actor
        self.clear_queue_capture()
        method = "POST" if operation == "deploy" else "DELETE"
        suffix = "/deploy" if operation == "deploy" else ""
        response = self.mutation(
            self.client_for(actor), method,
            f"/api/k8s/clusters/{self.cluster_name}{suffix}",
        )
        self.assertEqual(response.status_code, 202)
        self.enqueue.assert_called_once()
        source_id = response.get_json()["task_id"]
        queued = self.queued[0]
        if status == "error":
            target = "deploy_k8s" if operation == "deploy" else "delete_cluster"
            with patch.object(self.manager, target, side_effect=RuntimeError(
                "password=" + FAKE_PASSWORD + "\n" + FAKE_KEY
            )) as operation_mock:
                self.run_queued(queued)
            operation_mock.assert_called_once()
        elif status != "running":
            self.manager._update_task(source_id, status=status, message="synthetic terminal state")
        self.assertEqual(self.manager.get_task_status(source_id)["status"], status)
        self.clear_queue_capture()
        return source_id

    def accepted_child(self, source_id, actor=None, payload=None):
        actor = self.teacher if actor is None else actor
        response = self.retry(source_id, actor, payload=payload)
        self.assertEqual(response.status_code, 202)
        data = response.get_json()
        self.assertEqual(data["retry_of"], source_id)
        self.assertTrue(data["task_id"])
        self.assertNotEqual(data["task_id"], source_id)
        self.enqueue.assert_called_once()
        self.assertEqual(len(self.queued), 1)
        return data["task_id"], self.queued[0]

    def socket_for(self, actor):
        client = self.web.socketio.test_client(
            self.web.app, namespace="/state", flask_test_client=self.client_for(actor),
        )
        self.assertTrue(client.is_connected("/state"))
        self.stack.callback(
            lambda: client.disconnect(namespace="/state")
            if client.is_connected("/state") else None
        )
        client.get_received("/state")
        return client

    def assert_no_private_description(self, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for secret in (FAKE_PASSWORD, FAKE_KEY_BODY, "BEGIN OPENSSH PRIVATE KEY", "enc:v1:"):
            self.assertNotIn(secret, text)
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertFalse(any(word in key.lower() for word in ("descriptor", "fingerprint", "snapshot")))
                self.assert_no_private_description(item)
        elif isinstance(value, list):
            for item in value:
                self.assert_no_private_description(item)

    def test_retry_route_authentication_csrf_origin_and_missing_task(self):
        routes = [rule for rule in self.web.app.url_map.iter_rules()
                  if rule.rule == "/api/k8s/tasks/<task_id>/retry"]
        self.assertEqual(len(routes), 1)
        self.assertIn("POST", routes[0].methods)
        source = self.source_job()
        path = f"/api/k8s/tasks/{source}/retry"
        self.assertEqual(self.retry(source, None).status_code, 401)
        owner = self.client_for(self.teacher)
        self.assertEqual(owner.post(path).status_code, 400)
        self.assertEqual(owner.post(path, headers={"X-CSRFToken": "invalid"}).status_code, 400)
        self.assertEqual(self.retry(source, self.teacher, origin="https://unlisted.invalid").status_code, 403)
        self.assertEqual(self.retry("missing-retry-source", self.admin).status_code, 404)
        self.enqueue.assert_not_called()

    def test_unrelated_teacher_and_assigned_student_cannot_retry(self):
        source = self.source_job()
        for actor in (self.other_teacher, self.student):
            with self.subTest(actor=actor):
                response = self.retry(source, actor)
                self.assertEqual(response.status_code, 403)
                self.enqueue.assert_not_called()

    def test_owner_retries_admin_job_as_current_actor_with_private_notifications(self):
        source = self.source_job()
        owner_socket = self.socket_for(self.teacher)
        outsiders = [self.socket_for(actor) for actor in (self.other_teacher, self.student)]
        child, queued = self.accepted_child(source, payload={
            "created_by": self.admin, "owner_teacher_id": self.other_teacher,
            "cluster_name": "untrusted-other-cluster", "password": FAKE_PASSWORD,
        })
        owner = self.client_for(self.teacher)
        response = owner.get(f"/api/k8s/tasks/{child}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["created_by"], self.teacher)
        self.assertEqual(data["owner_teacher_id"], self.teacher)
        self.assert_no_private_description(data)
        listing = owner.get("/api/k8s/tasks")
        self.assertEqual(listing.status_code, 200)
        self.assertIn(child, {item["task_id"] for item in listing.get_json()["tasks"]})
        self.assert_no_private_description(listing.get_json())
        self.assertEqual(owner.get(f"/k8s/logs/{child}").status_code, 200)
        for actor in (self.other_teacher, self.student):
            with self.subTest(actor=actor):
                client = self.client_for(actor)
                self.assertEqual(client.get(f"/api/k8s/tasks/{child}").status_code, 403)
                self.assertEqual(client.get(f"/k8s/logs/{child}").status_code, 403)
                result = client.get("/api/k8s/tasks")
                self.assertEqual(result.status_code, 200)
                self.assertNotIn(child, {item["task_id"] for item in result.get_json()["tasks"]})
        with patch.object(self.manager, "deploy_k8s") as deploy:
            self.run_queued(queued)
        deploy.assert_called_once()
        self.assertEqual(deploy.call_args.args[0], self.cluster_name)
        self.assertEqual(deploy.call_args.kwargs["actor_id"], self.teacher)
        self.assertEqual(self.manager.get_task_status(child)["status"], "completed")
        owner_events = [event for event in owner_socket.get_received("/state")
                        if event["name"] == "task_update"
                        and event["args"][0].get("task_id") == child]
        self.assertTrue(owner_events)
        self.assert_no_private_description(owner_events)
        for outsider in outsiders:
            self.assertFalse(any(
                event["name"] == "task_update" and event["args"][0].get("task_id") == child
                for event in outsider.get_received("/state")
            ))

    def test_duplicate_retry_is_conflict_and_enqueues_only_one_direct_child(self):
        source = self.source_job()
        child, _ = self.accepted_child(source)
        for actor in (self.teacher, self.admin):
            with self.subTest(actor=actor):
                response = self.retry(source, actor)
                self.assertEqual(response.status_code, 409)
        self.enqueue.assert_called_once()
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.queued[0][1].task_id, child)

    def assert_worker_refuses_change(self, change):
        source = self.source_job()
        child, queued = self.accepted_child(source)
        if change == "disabled":
            db.update_user(self.teacher, {"is_active": False})
        else:
            with db.session_scope(commit=True) as session:
                cluster = session.query(db.Cluster).filter_by(name=self.cluster_name).one()
                if change == "provider":
                    cluster.pve_server_id = 8
                elif change == "vm":
                    cluster.vms[0].vmid = 902
                else:
                    raise AssertionError("Unknown test change")
        with patch.object(self.manager, "deploy_k8s") as deploy:
            self.run_queued(queued)
        deploy.assert_not_called()
        self.assertIn(self.manager.get_task_status(child)["status"], ("error", "cancelled"))

    def test_retry_worker_rechecks_disabled_requester(self):
        self.assert_worker_refuses_change("disabled")

    def test_retry_worker_rechecks_cluster_provider_fingerprint(self):
        self.assert_worker_refuses_change("provider")

    def test_retry_worker_rechecks_cluster_vm_fingerprint(self):
        self.assert_worker_refuses_change("vm")

    def test_deploy_and_delete_error_or_cancelled_jobs_are_retryable(self):
        for operation in ("deploy", "delete"):
            for status in ("error", "cancelled"):
                with self.subTest(operation=operation, status=status):
                    source = self.source_job(operation=operation, status=status)
                    child, queued = self.accepted_child(source)
                    self.assertEqual(queued[0], operation)
                    target = "deploy_k8s" if operation == "deploy" else "delete_cluster"
                    with patch.object(self.manager, target) as execute:
                        self.run_queued(queued)
                    execute.assert_called_once()
                    self.assertEqual(execute.call_args.kwargs["actor_id"], self.teacher)
                    self.assertEqual(self.manager.get_task_status(child)["status"], "completed")

    def test_running_completed_and_legacy_without_descriptor_are_conflicts(self):
        for status in ("running", "completed"):
            with self.subTest(status=status):
                source = self.source_job(status=status)
                self.assertEqual(self.retry(source, self.teacher).status_code, 409)
                self.enqueue.assert_not_called()
        legacy = "legacy-task-without-server-description"
        self.manager._update_task(
            legacy, status="error", created_by=self.teacher,
            owner_teacher_id=self.teacher, queue="deploy", message="legacy failure",
        )
        self.assertEqual(self.retry(legacy, self.teacher, payload={
            "operation": "deploy", "cluster_name": self.cluster_name,
        }).status_code, 409)
        self.enqueue.assert_not_called()

    def create_source(self):
        self.clear_queue_capture()
        response = self.mutation(self.client_for(self.admin), "POST", "/api/k8s/create", {
            "group_id": self.group_id, "class_id": self.class_id,
            "pve_node": "retry-node", "pve_server_id": 7,
        })
        self.assertEqual(response.status_code, 202)
        self.enqueue.assert_called_once()
        return response.get_json()["task_id"], self.queued[0]

    def test_create_stage_failure_is_conflict_and_never_replays_vm_creation(self):
        source, queued = self.create_source()
        with patch.object(self.manager, "create_cluster", side_effect=RuntimeError("synthetic create failure")) as create:
            self.run_queued(queued)
        create.assert_called_once()
        self.assertEqual(self.manager.get_task_status(source)["status"], "error")
        self.clear_queue_capture()
        with patch.object(self.manager, "create_cluster") as create:
            self.assertEqual(self.retry(source, self.teacher).status_code, 409)
        create.assert_not_called()
        self.enqueue.assert_not_called()

    def test_auto_deploy_failure_retries_deploy_without_recreating_vms(self):
        source, queued = self.create_source()
        with patch.object(self.manager, "create_cluster", return_value=(
            self.cluster_name, db.load_cluster(self.cluster_name),
        )) as create:
            self.run_queued(queued)
        create.assert_called_once()
        deployments = [item for item in self.queued if item[0] == "deploy"]
        self.assertEqual(len(deployments), 1)
        with patch.object(self.manager, "deploy_k8s", side_effect=RuntimeError("synthetic auto-deploy failure")) as deploy:
            self.run_queued(deployments[0])
        deploy.assert_called_once()
        self.assertEqual(self.manager.get_task_status(source)["status"], "error")
        self.clear_queue_capture()
        child, queued = self.accepted_child(source)
        self.assertEqual(queued[0], "deploy")
        with patch.object(self.manager, "create_cluster") as create, patch.object(self.manager, "deploy_k8s") as deploy:
            self.run_queued(queued)
        create.assert_not_called()
        deploy.assert_called_once()
        self.assertEqual(deploy.call_args.kwargs["actor_id"], self.teacher)
        self.assertEqual(self.manager.get_task_status(child)["status"], "completed")

    @contextmanager
    def capture_audit(self):
        root = logging.getLogger()
        old_level = root.level
        capture = CaptureAudit()
        logger = logging.getLogger("security.audit")
        root.setLevel(logging.WARNING)
        logger.addHandler(capture)
        try:
            yield capture.records
        finally:
            logger.removeHandler(capture)
            root.setLevel(old_level)

    def test_retry_success_and_denial_are_audited_without_secrets(self):
        source = self.source_job()
        with self.capture_audit() as records:
            self.assertEqual(self.retry(source, self.other_teacher, payload={
                "password": FAKE_PASSWORD, "ssh_private_key": FAKE_KEY,
            }).status_code, 403)
            self.enqueue.assert_not_called()
            child, _ = self.accepted_child(source, payload={
                "password": FAKE_PASSWORD, "ssh_private_key": FAKE_KEY,
            })
        denied = [record for record in records
                  if record["action"] == "job.retry" and record["outcome"] == "denied"]
        success = [record for record in records
                   if record["action"] == "job.retry" and record["outcome"] == "success"]
        self.assertTrue(denied)
        self.assertTrue(success, "root WARNING must not filter successful retry audit")
        self.assertEqual(denied[-1]["actor"]["id"], self.other_teacher)
        self.assertEqual(success[-1]["actor"]["id"], self.teacher)
        self.assertIn(source, json.dumps(denied[-1]))
        self.assertIn(source, json.dumps(success[-1]))
        self.assertIn(child, json.dumps(success[-1]))
        self.assert_no_private_description(records)

    def test_enqueue_failure_is_safe_500_and_releases_source_reservation(self):
        source = self.source_job()
        owner = self.client_for(self.teacher)
        before = owner.get("/api/k8s/tasks")
        self.assertEqual(before.status_code, 200)
        original_ids = {task["task_id"] for task in before.get_json()["tasks"]}
        with self.capture_audit() as records:
            with patch.object(self.manager.scheduler, "enqueue", side_effect=RuntimeError(
                "scheduler failure password=" + FAKE_PASSWORD + "\n" + FAKE_KEY
            )) as broken_enqueue:
                response = self.retry(source, self.teacher)
            broken_enqueue.assert_called_once()
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        self.assert_no_private_description(response.get_json())
        retry_records = [record for record in records if record["action"] == "job.retry"]
        failures = [record for record in retry_records if record["outcome"] in ("failure", "error")]
        self.assertTrue(failures)
        self.assertFalse(any(record["outcome"] == "success" for record in retry_records))
        self.assertEqual(failures[-1]["actor"]["id"], self.teacher)
        self.assertIn(source, json.dumps(failures[-1]))
        self.assert_no_private_description(records)
        after = owner.get("/api/k8s/tasks")
        self.assertEqual(after.status_code, 200)
        for task in after.get_json()["tasks"]:
            if task["task_id"] not in original_ids:
                self.assertNotIn(task["status"], ("running", "queued", "pending"))
        self.assert_no_private_description(after.get_json())
        # The original capture boundary is restored after the injected failure.
        # A retained reservation would turn this required 202 into a false 409.
        child, queued = self.accepted_child(source)
        self.assertEqual(queued[1].task_id, child)
        self.assertEqual(len(self.queued), 1)


if __name__ == "__main__":
    unittest.main()
