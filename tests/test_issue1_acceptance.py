"""Issue #1 regressions against real application, service and DB modules.

Only disposable SQLite data and fictitious secrets are used. Providers and the
queue boundary are mocked; no worker is allowed to contact infrastructure.
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
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError, StatementError
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash


ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = os.path.normcase(os.path.abspath(ROOT / ".db_config.json"))
_real_exists = os.path.exists


def _exists_without_live_db_config(path):
    try:
        if os.path.normcase(os.path.abspath(os.fspath(path))) == _CONFIG_PATH:
            return False
    except TypeError:
        pass
    return _real_exists(path)


# Suppress only the live configuration file on the first DB-module import.
# All ordinary path operations retain their real behavior.
with patch("os.path.exists", side_effect=_exists_without_live_db_config):
    db = importlib.import_module("modules.db")


class CaptureAudit(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(json.loads(record.getMessage()))


class Issue1AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ssh = importlib.import_module("modules.ssh_terminal")
        status_cache = importlib.import_module("modules.status_cache")
        previous_app = sys.modules.pop("app", None)
        manager = importlib.import_module("modules.k8s_manager")
        old_callback = manager._on_task_update
        try:
            with (
                patch.object(db, "is_db_configured", return_value=False),
                patch.object(ssh, "db_get_config", return_value=None),
                patch.object(status_cache, "start_monitor"),
                patch.object(ssh.SSHManager, "_start_cleanup_thread"),
            ):
                cls.web = importlib.import_module("app")
        finally:
            manager._on_task_update = old_callback
            if previous_app is not None:
                sys.modules["app"] = previous_app
            else:
                sys.modules.pop("app", None)
        cls.manager = manager
        # Restore the real configuration predicate captured during import.
        cls.web.is_db_configured = db.is_db_configured
        cls.web.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=True,
            SESSION_COOKIE_SECURE=False,
            SECRET_KEY="issue1-isolated-session-key",
        )

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="issue1-acceptance-")
        )
        engine = create_engine("sqlite:///" + str(Path(directory) / "test.sqlite"))
        self.stack.callback(engine.dispose)
        self.stack.enter_context(patch.object(db, "engine", engine))
        self.stack.enter_context(
            patch.object(db, "SessionLocal", sessionmaker(bind=engine))
        )
        self.stack.enter_context(patch.dict(os.environ, {
            "K8S_LAB_CREDENTIAL_KEY": Fernet.generate_key().decode("ascii"),
        }))
        db.Base.metadata.create_all(engine)
        self.stack.enter_context(patch.object(self.manager, "_task_store", {}))
        self.stack.enter_context(patch.object(self.manager, "_task_cancel_events", {}))
        self.stack.enter_context(patch.object(
            self.manager, "_on_task_update", self.web._emit_task_update
        ))
        self.stack.enter_context(patch.object(self.web, "_allowed_origins", []))
        self.stack.enter_context(patch.object(self.web, "_setup_complete", True))
        self.stack.enter_context(patch.object(self.web, "_online_users", {}))
        self.stack.enter_context(patch.object(self.web, "_webssh_sid_users", {}))
        self.stack.enter_context(patch.object(self.web, "_webssh_connect_times", {}))
        self.provider_guards = [
            self.stack.enter_context(patch.object(self.manager, "PVEClient")),
            self.stack.enter_context(patch.object(self.manager, "OpenWrtClient")),
        ]
        password_hash = generate_password_hash("fictitious-login-password")
        self.admin, self.teacher, self.other_teacher = [
            db.create_user({
                "username": name, "role": role, "password_hash": password_hash,
            })
            for name, role in (("admin-test", "admin"), ("teacher-2", "teacher"),
                               ("teacher-3", "teacher"))
        ]
        self.own_class = db.create_class({"name": "own", "created_by": self.teacher})
        self.second_own_class = db.create_class({
            "name": "other-own", "created_by": self.teacher,
        })
        self.foreign_class = db.create_class({
            "name": "foreign", "created_by": self.other_teacher,
        })
        self.own_group = db.create_group({
            "name": "own", "class_id": self.own_class, "created_by": self.teacher,
        })
        self.foreign_group = db.create_group({
            "name": "foreign", "class_id": self.foreign_class,
            "created_by": self.other_teacher,
        })
        self.cluster_name = "k8s_acceptance_owned"
        db.save_cluster(self.cluster_name, {
            "status": "running", "created_by": self.teacher,
            "group_id": self.own_group, "class_id": self.own_class,
            "vms": {},
        })

    def client_for(self, user_id):
        client = self.web.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        return client

    def mutation(self, client, method, path, payload=None):
        response = client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)
        return client.open(
            path, method=method, json=payload,
            headers={"X-CSRFToken": response.get_json()["csrf_token"]},
        )

    def assert_no_provider_calls(self):
        for provider in self.provider_guards:
            provider.assert_not_called()

    def test_creation_rejects_foreign_and_inconsistent_associations_before_enqueue(self):
        cases = [
            (self.teacher, "/api/k8s/create",
             {"group_id": self.foreign_group, "class_id": self.foreign_class}, 403),
            (self.teacher, "/api/k8s/batch-create",
             {"group_ids": [self.own_group, self.foreign_group]}, 403),
            (self.teacher, "/api/k8s/create",
             {"group_id": self.own_group, "class_id": self.second_own_class}, 400),
            (self.teacher, "/api/k8s/batch-create",
             {"group_ids": [self.own_group], "class_id": self.second_own_class}, 400),
            (self.admin, "/api/k8s/create", {"group_id": 999999}, 400),
            (self.admin, "/api/k8s/batch-create", {"group_ids": [999999]}, 400),
            (self.admin, "/api/k8s/create", {"class_id": 999999}, 400),
        ]
        for actor, path, associations, expected in cases:
            with self.subTest(path=path, actor=actor, associations=associations):
                with patch.object(self.manager.scheduler, "enqueue") as enqueue:
                    response = self.mutation(self.client_for(actor), "POST", path, {
                        "pve_node": "test-node", **associations,
                    })
                self.assertEqual(response.status_code, expected)
                enqueue.assert_not_called()
                self.assert_no_provider_calls()

    def test_own_group_create_and_batch_are_accepted(self):
        for path, associations in (
            ("/api/k8s/create", {"group_id": self.own_group}),
            ("/api/k8s/batch-create", {"group_ids": [self.own_group]}),
        ):
            with self.subTest(path=path):
                with patch.object(self.manager.scheduler, "enqueue") as enqueue:
                    response = self.mutation(self.client_for(self.teacher), "POST", path, {
                        "pve_node": "test-node", "class_id": self.own_class,
                        **associations,
                    })
                self.assertEqual(response.status_code, 202)
                enqueue.assert_called_once()
                self.assert_no_provider_calls()

    @staticmethod
    def provider_payload(name):
        return {
            "name": name, "host": "pve.invalid", "user": "test@pam",
            "token_name": "test-token-name", "token_value": "FAKE_PROVIDER_TOKEN",
            "ow_host": "openwrt.invalid", "ow_username": "test-user",
            "ow_password": "FAKE_OPENWRT_PASSWORD",
        }

    def test_provider_read_edit_roundtrip_keeps_encrypted_credentials(self):
        original = self.provider_payload("roundtrip-provider")
        db.create_pve_server(original)
        client = self.client_for(self.admin)
        response = client.get("/api/pve/servers")
        self.assertEqual(response.status_code, 200)
        returned = next(item for item in response.get_json()
                        if item["name"] == original["name"])
        self.assertEqual(returned["token_name"], original["token_name"])
        self.assertNotIn(original["token_value"], response.get_data(as_text=True))
        self.assertNotIn(original["ow_password"], response.get_data(as_text=True))
        returned["name"] = "renamed-provider"
        response = self.mutation(
            client, "PUT", f"/api/pve/servers/{returned['id']}", returned
        )
        self.assertEqual(response.status_code, 200)
        from modules.credential_store import decrypt_secret, ENCRYPTED_PREFIX
        with db.session_scope() as session:
            row = session.get(db.PVEServer, returned["id"])
            self.assertEqual(row.name, "renamed-provider")
            self.assertEqual(row.token_name, original["token_name"])
            for field in ("token_value", "ow_password"):
                value = getattr(row, field)
                self.assertTrue(value.startswith(ENCRYPTED_PREFIX))
                self.assertEqual(decrypt_secret(value), original[field])

    def test_new_provider_placeholder_is_bad_request_without_insert(self):
        payload = self.provider_payload("must-not-exist")
        payload.update(token_value="[REDACTED]", ow_password="[REDACTED]")
        with db.session_scope() as session:
            before = session.query(db.PVEServer).count()
        response = self.mutation(
            self.client_for(self.admin), "POST", "/api/pve/servers", payload
        )
        self.assertEqual(response.status_code, 400)
        with db.session_scope() as session:
            self.assertEqual(session.query(db.PVEServer).count(), before)

    def enqueue_deploy(self, actor):
        with patch.object(self.manager.scheduler, "enqueue") as enqueue:
            response = self.mutation(
                self.client_for(actor), "POST",
                f"/api/k8s/clusters/{self.cluster_name}/deploy",
            )
        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once()
        return response.get_json()["task_id"], enqueue.call_args.args[1]

    def test_admin_created_job_is_visible_to_resource_owner_only(self):
        task_id, _ = self.enqueue_deploy(self.admin)
        owner = self.client_for(self.teacher)
        other = self.client_for(self.other_teacher)
        for client, visible in ((owner, True), (other, False)):
            response = client.get("/api/k8s/tasks")
            self.assertEqual(response.status_code, 200)
            ids = {item["task_id"] for item in response.get_json()["tasks"]}
            self.assertEqual(task_id in ids, visible)
            response = client.get(f"/api/k8s/tasks/{task_id}")
            self.assertEqual(response.status_code, 200 if visible else 403)
            with patch.object(self.web, "render_template", return_value="LOG PAGE"):
                response = client.get(f"/k8s/logs/{task_id}")
            self.assertEqual(response.status_code, 200 if visible else 403)
        with patch.object(self.web, "cancel_task") as cancel:
            response = self.mutation(other, "POST", f"/api/k8s/tasks/{task_id}/cancel")
        self.assertEqual(response.status_code, 403)
        cancel.assert_not_called()
        response = self.mutation(owner, "POST", f"/api/k8s/tasks/{task_id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assert_no_provider_calls()

    def test_worker_reloads_disabled_actor_before_deploy(self):
        task_id, queued = self.enqueue_deploy(self.teacher)
        db.update_user(self.teacher, {"is_active": False})
        with patch.object(self.manager, "deploy_k8s") as deploy:
            queued.fn(*queued.args, **queued.kwargs)
        deploy.assert_not_called()
        self.assertNotEqual(self.manager.get_task_status(task_id)["status"], "completed")
        self.assert_no_provider_calls()

    def socket_for(self, actor, namespace):
        client = self.web.socketio.test_client(
            self.web.app, namespace=namespace, flask_test_client=self.client_for(actor)
        )
        self.assertTrue(client.is_connected(namespace))
        self.stack.callback(
            lambda: client.disconnect(namespace=namespace)
            if client.is_connected(namespace) else None
        )
        client.get_received(namespace)
        return client

    def test_sqlalchemy_errors_and_free_text_do_not_escape_through_jobs(self):
        private_marker = "FICTITIOUS_OPENSSH_KEY_BODY"
        password_marker = "FICTITIOUS_SQL_PASSWORD"
        private_key = ("-----BEGIN OPENSSH PRIVATE KEY-----\n" + private_marker
                       + "\n-----END OPENSSH PRIVATE KEY-----")
        params = {"ssh_private_key": private_key, "password": password_marker}
        errors = (
            StatementError("save failed", "INSERT INTO clusters VALUES (:password)",
                           params, RuntimeError("synthetic database failure")),
            DBAPIError("INSERT INTO clusters VALUES (:password)", params,
                       RuntimeError("synthetic database failure")),
        )
        socket = self.socket_for(self.teacher, "/state")
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                task_id, queued = self.enqueue_deploy(self.teacher)
                socket.get_received("/state")
                with patch.object(self.manager, "deploy_k8s", side_effect=error) as deploy:
                    queued.fn(*queued.args, **queued.kwargs)
                deploy.assert_called_once()
                # Existing provider/log callbacks use string messages. Exercise
                # both log ingestion and update ingestion, not only exception formatting.
                self.manager._append_log(task_id, str(error))
                self.manager._update_task(
                    task_id, status="error", message=str(error), error=str(error)
                )
                response = self.client_for(self.teacher).get(f"/api/k8s/tasks/{task_id}")
                self.assertEqual(response.status_code, 200)
                events = [event for event in socket.get_received("/state")
                          if event["name"] == "task_update"]
                self.assertTrue(events, "真实任务更新必须到达获权的 SocketIO 客户端")
                output = response.get_data(as_text=True) + json.dumps(events, ensure_ascii=False)
                for marker in (private_marker, password_marker, "BEGIN OPENSSH PRIVATE KEY"):
                    self.assertNotIn(marker, output)
                self.assert_no_provider_calls()

    @contextmanager
    def capture_audit(self):
        logger = logging.getLogger("security.audit")
        root = logging.getLogger()
        original_level = root.level
        capture = CaptureAudit()
        root.setLevel(logging.WARNING)
        logger.addHandler(capture)
        try:
            yield capture.records
        finally:
            logger.removeHandler(capture)
            root.setLevel(original_level)

    def test_success_and_socket_denial_reach_real_audit_handler(self):
        command_marker = "FICTITIOUS_TERMINAL_INPUT_MUST_NOT_BE_AUDITED"
        with self.capture_audit() as records:
            provider = MagicMock()
            provider.clone_template.return_value = 1001
            with patch.object(self.web, "get_pve_client", return_value=provider):
                response = self.mutation(
                    self.client_for(self.admin), "POST", "/api/pve/clone",
                    {"node": "test-node", "vmid": 9000, "newid": 1001,
                     "name": "test-clone", "config": {"password": command_marker}},
                )
            self.assertEqual(response.status_code, 201)
            successes = [record for record in records
                         if record["action"] == "http.write" and record["outcome"] == "success"]
            self.assertTrue(successes, "root WARNING 不能过滤成功安全审计")
            self.assertEqual(successes[-1]["actor"]["id"], self.admin)
            self.assertEqual(successes[-1]["resource_type"], "http")
            self.assertEqual(successes[-1]["resource_id"], "/api/pve/clone")
            self.assertNotIn(command_marker, json.dumps(successes, ensure_ascii=False))
            socket = self.socket_for(self.other_teacher, "/webssh")
            records.clear()
            # This is a real unauthorized resource request; no SSH session or
            # provider should be created and no arbitrary input should be logged.
            with patch.object(self.web.ssh_manager, "create_session") as create_session:
                socket.emit("session_create", {
                    "cluster": self.cluster_name, "data": command_marker,
                    "password": command_marker,
                }, namespace="/webssh")
            create_session.assert_not_called()
            denied = [record for record in records if record["outcome"] == "denied"]
            self.assertTrue(denied)
            self.assertEqual(denied[-1]["actor"]["id"], self.other_teacher)
            self.assertTrue(denied[-1]["action"])
            self.assertEqual(denied[-1]["resource_type"], "webssh")
            self.assertEqual(denied[-1]["resource_id"], self.cluster_name)
            self.assertNotIn(command_marker, json.dumps(records, ensure_ascii=False))

    def seed_legacy_credentials(self):
        with db.session_scope(commit=True) as session:
            session.add(db.PVEServer(
                name="legacy", host="legacy.invalid", user="test@pam",
                token_name="legacy-name", token_value="FAKE_LEGACY_TOKEN",
                ow_password="FAKE_LEGACY_OW_PASSWORD",
            ))
            session.add(db.Config(key="pve", value=json.dumps({
                "token_value": "FAKE_CONFIG_TOKEN",
            })))
            session.add(db.Config(key="openwrt", value=json.dumps({
                "password": "FAKE_CONFIG_PASSWORD",
            })))
            session.add(db.Cluster(
                name="legacy-cluster", ssh_private_key="FAKE_LEGACY_PRIVATE_KEY",
            ))

    @staticmethod
    def raw_legacy_credentials():
        with db.session_scope() as session:
            server = session.query(db.PVEServer).filter_by(name="legacy").one()
            cluster = session.query(db.Cluster).filter_by(name="legacy-cluster").one()
            config = {row.key: json.loads(row.value) for row in session.query(db.Config).all()}
            return {
                "token": server.token_value, "ow_password": server.ow_password,
                "key": cluster.ssh_private_key,
                "config_token": config["pve"]["token_value"],
                "config_password": config["openwrt"]["password"],
            }

    def test_explicit_sqlite_credential_migration_is_idempotent(self):
        from modules.credential_store import decrypt_secret, ENCRYPTED_PREFIX
        self.seed_legacy_credentials()
        before = self.raw_legacy_credentials()
        db.migrate_plaintext_credentials()
        after = self.raw_legacy_credentials()
        for name, ciphertext in after.items():
            self.assertTrue(ciphertext.startswith(ENCRYPTED_PREFIX))
            self.assertEqual(decrypt_secret(ciphertext), before[name])
        db.migrate_plaintext_credentials()
        self.assertEqual(self.raw_legacy_credentials(), after)

    def test_failed_sqlite_migration_rolls_back_every_credential(self):
        from modules.credential_store import CredentialError
        self.seed_legacy_credentials()
        before = self.raw_legacy_credentials()
        encrypt = db.encrypt_secret
        count = 0

        def fail_after_progress(value):
            nonlocal count
            count += 1
            if count == 3:
                raise CredentialError("synthetic encryption failure")
            return encrypt(value)

        with patch.object(db, "encrypt_secret", side_effect=fail_after_progress):
            with self.assertRaises(CredentialError):
                db.migrate_plaintext_credentials()
        self.assertGreaterEqual(count, 3)
        self.assertEqual(self.raw_legacy_credentials(), before)


if __name__ == "__main__":
    unittest.main()
