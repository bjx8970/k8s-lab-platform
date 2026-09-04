"""Job security regressions using in-memory resources and captured queue work.

No app startup, credentials, database connections or real provider calls are
required.  Database configuration discovery is suppressed even on first import.
"""

import copy
import json
import os
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch


_real_exists = os.path.exists


def _without_live_config(path):
    if os.path.basename(os.fspath(path)) == ".db_config.json":
        return False
    return _real_exists(path)


with patch("os.path.exists", side_effect=_without_live_config):
    from modules import audit, db
    from modules import k8s_manager as manager
    from modules import security_service as service

from modules.authz import Actions, AuthorizationDenied, is_allowed


PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "TEST_PRIVATE_BODY_ABC123\n"
    "TEST_PRIVATE_BODY_DEF456\n"
    "-----END OPENSSH PRIVATE KEY-----"
)
CREATE_ARGS = (1, 1, 2, 2048, 2, 2048, "node-test")


def memory_user(user_id, role):
    return SimpleNamespace(
        id=user_id, role=role, username=f"user-{user_id}",
        is_authenticated=True, is_active=True,
    )


class JobSecurityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.users = {1: memory_user(1, "admin"), 2: memory_user(2, "teacher"),
                      3: memory_user(3, "teacher"), 4: memory_user(4, "student")}
        self.classes = {10: {"id": 10, "created_by": 2},
                        20: {"id": 20, "created_by": 3}}
        self.groups = {11: {"id": 11, "created_by": 2, "class_id": 10},
                       12: {"id": 12, "created_by": 2, "class_id": 10},
                       21: {"id": 21, "created_by": 3, "class_id": 20}}
        self.clusters = {"k8s_1": {"created_by": 2, "group_id": 11,
                                   "class_id": 10, "status": "running",
                                   "pve_server_id": 7, "vms": {}}}
        self.memberships = {4: [11]}
        self.queued = []
        self.callbacks = []
        self.events = []
        self.mock(service, "get_user", side_effect=lambda uid: copy.deepcopy(self.users.get(uid)))
        self.mock(manager, "get_user", side_effect=lambda uid: copy.deepcopy(self.users.get(uid)))
        self.mock(service, "get_class", side_effect=lambda cid: copy.deepcopy(self.classes.get(cid)))
        self.mock(service, "get_group", side_effect=lambda gid: copy.deepcopy(self.groups.get(gid)))
        self.mock(manager, "get_group", side_effect=lambda gid: copy.deepcopy(self.groups.get(gid)))
        self.mock(service, "load_cluster", side_effect=lambda name: copy.deepcopy(self.clusters.get(name)))
        self.mock(manager, "load_cluster", side_effect=lambda name: copy.deepcopy(self.clusters.get(name)))
        self.mock(service, "get_student_group_ids", side_effect=lambda uid: list(self.memberships.get(uid, [])))
        self.enqueue = self.mock(manager.scheduler, "enqueue", side_effect=lambda queue, task: self.queued.append((queue, task)))
        self.mock(manager, "_on_task_update", new=lambda *args: self.callbacks.append(args))
        self.stack.enter_context(patch.dict(manager._task_store, {}, clear=True))
        self.stack.enter_context(patch.dict(manager._task_cancel_events, {}, clear=True))
        self.audit = Mock(side_effect=lambda action, outcome, **kwargs: self.events.append({"action": action, "outcome": outcome, **kwargs}))
        self.mock(audit, "security_audit", new=self.audit)
        self.mock(service, "security_audit", new=self.audit)
        self.providers = [self.mock(manager, name, side_effect=AssertionError("测试禁止真实基础设施调用"))
                          for name in ("_pve_client", "_openwrt_client", "get_config", "get_pve_server", "session_scope", "save_cluster", "delete_cluster_db")]
        self.mock(db, "get_session", side_effect=AssertionError("测试禁止真实数据库访问"))
        self.mock(manager._SSHClient, "connect", side_effect=AssertionError("测试禁止真实 SSH 连接"))

    def mock(self, target, name, **kwargs):
        return self.stack.enter_context(patch.object(target, name, **kwargs))

    def run_queued(self, index=0):
        task = self.queued[index][1]
        task.fn(*task.args, **task.kwargs)

    def assert_no_providers(self):
        for provider in self.providers:
            provider.assert_not_called()

    def assert_no_secret(self, value):
        rendered = json.dumps(value, ensure_ascii=False, default=str)
        self.assertNotIn("TEST_PRIVATE_BODY_ABC123", rendered)
        self.assertNotIn("TEST_PRIVATE_BODY_DEF456", rendered)

    def test_creation_accepts_own_associations_and_admin_foreign_associations(self):
        for actor, groups, class_id in [(2, [11, 12], 10), (2, ["11"], "10"),
                                        (2, [11], None), (2, [], None), (2, [], 10),
                                        (1, [21], 20), (1, [21], None)]:
            with self.subTest(actor=actor, groups=groups, class_id=class_id):
                self.assertIsNone(service.validate_cluster_creation(self.users[actor], groups, class_id))
        self.assert_no_providers()

    def test_teacher_cannot_use_foreign_group_or_class(self):
        with self.assertRaises(AuthorizationDenied):
            service.validate_cluster_creation(self.users[2], [21])
        with self.assertRaises(AuthorizationDenied):
            service.validate_cluster_creation(self.users[2], [], 20)
        self.groups[11]["created_by"] = 3
        with self.assertRaises(AuthorizationDenied):
            service.validate_cluster_creation(self.users[2], [11], 10)
        self.groups[11]["created_by"] = 2
        self.classes[10]["created_by"] = 3
        with self.assertRaises(AuthorizationDenied):
            service.validate_cluster_creation(self.users[2], [11])
        self.assert_no_providers()

    def test_existence_and_association_checks_apply_to_admin(self):
        for groups, class_id in [([999], None), ([], 999), ([11], 20)]:
            with self.subTest(groups=groups, class_id=class_id):
                with self.assertRaises(ValueError):
                    service.validate_cluster_creation(self.users[1], groups, class_id)
        del self.classes[10]
        with self.assertRaises(ValueError):
            service.validate_cluster_creation(self.users[1], [11])

    def test_invalid_ids_and_non_sequence_group_input_are_value_errors(self):
        invalid_ids = (True, False, 1.0, 11.9, "", " ", "-1", -1, 0,
                       "0", "01", "+11", "11.0", " 11", "11 ", {}, [])
        for value in invalid_ids:
            with self.subTest(value=repr(value), field="group"):
                with self.assertRaises(ValueError):
                    service.validate_cluster_creation(self.users[1], [value])
            with self.subTest(value=repr(value), field="class"):
                with self.assertRaises(ValueError):
                    service.validate_cluster_creation(self.users[1], [], value)
        for value in (None, 11, True, 1.5, "11", b"11", {11}, {"id": 11}):
            with self.subTest(group_ids=repr(value)):
                with self.assertRaises(ValueError):
                    service.validate_cluster_creation(self.users[1], value)
        self.assertFalse(service._same_id(True, 1))
        self.assertFalse(service._same_id(11.9, 11))
        self.assertTrue(service._same_id("11", 11))

    def test_invalid_batch_enqueues_nothing_including_prior_valid_groups(self):
        for groups, class_id, error in [([11, 21], None, AuthorizationDenied),
                                        ([11, 999], None, ValueError),
                                        ([11, 21], 10, ValueError),
                                        ([11, True], 10, ValueError),
                                        (11, None, ValueError)]:
            with self.subTest(groups=groups, class_id=class_id):
                with self.assertRaises(error):
                    manager.batch_create_clusters_async(groups, *CREATE_ARGS,
                                                        created_by=2, class_id=class_id)
                self.assertEqual(self.queued, [])
                self.assertEqual(manager._task_store, {})
        self.assert_no_providers()

    def test_legal_batch_records_creator_and_owner_before_queueing(self):
        task_ids = manager.batch_create_clusters_async([11, 12], *CREATE_ARGS,
                                                       created_by=2, class_id=10)
        self.assertEqual(len(task_ids), 2)
        self.assertEqual(len(self.queued), 2)
        for task_id in task_ids:
            task = manager.get_task_status(task_id)
            self.assertEqual((task["created_by"], task["owner_teacher_id"]), (2, 2))
        self.assert_no_providers()

    def test_direct_service_calls_without_actor_are_denied(self):
        calls = [lambda: manager.create_cluster(*CREATE_ARGS),
                 lambda: manager.create_cluster_async(*CREATE_ARGS),
                 lambda: manager.delete_cluster("k8s_1"),
                 lambda: manager.delete_cluster_async("k8s_1"),
                 lambda: manager.deploy_k8s("k8s_1"),
                 lambda: manager.deploy_k8s_async("k8s_1"),
                 lambda: manager.force_delete_cluster("k8s_1"),
                 lambda: manager.batch_create_clusters([11], *CREATE_ARGS),
                 lambda: manager.batch_create_clusters_async([11], *CREATE_ARGS),
                 lambda: manager.cancel_task("missing")]
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(AuthorizationDenied):
                    call()
        self.assertEqual(self.queued, [])
        self.assert_no_providers()

    def test_create_worker_rechecks_disabled_actor_and_changed_associations(self):
        create = self.mock(manager, "create_cluster")
        for change in ("disabled", "group_owner", "class_owner", "group_class"):
            with self.subTest(change=change):
                self.users[2].is_active = True
                self.groups[11].update(created_by=2, class_id=10)
                self.classes[10]["created_by"] = 2
                task_id = manager.create_cluster_async(*CREATE_ARGS, created_by=2,
                                                       group_id=11, class_id=10)
                if change == "disabled":
                    self.users[2].is_active = False
                elif change == "group_owner":
                    self.groups[11]["created_by"] = 3
                elif change == "class_owner":
                    self.classes[10]["created_by"] = 3
                else:
                    self.groups[11]["class_id"] = 20
                self.run_queued(len(self.queued) - 1)
                self.assertEqual(manager.get_task_status(task_id)["status"], "error")
                create.assert_not_called()
                self.assert_no_providers()

    def test_delete_and_deploy_workers_recheck_live_actor_and_cluster_owner(self):
        for kind in ("delete", "deploy"):
            name = "delete_cluster" if kind == "delete" else "deploy_k8s"
            enqueue = manager.delete_cluster_async if kind == "delete" else manager.deploy_k8s_async
            with patch.object(manager, name) as operation:
                for change in ("disabled", "owner"):
                    with self.subTest(kind=kind, change=change):
                        self.users[2].is_active = True
                        self.clusters["k8s_1"]["created_by"] = 2
                        task_id = enqueue("k8s_1", created_by=2)
                        if change == "disabled":
                            self.users[2].is_active = False
                        else:
                            self.clusters["k8s_1"]["created_by"] = 3
                        self.run_queued(len(self.queued) - 1)
                        self.assertEqual(manager.get_task_status(task_id)["status"], "error")
                        operation.assert_not_called()
        self.assert_no_providers()

    def test_cascaded_deployment_reauthorizes_after_create_queue(self):
        create = self.mock(manager, "create_cluster", return_value=("k8s_1", self.clusters["k8s_1"]))
        deploy = self.mock(manager, "deploy_k8s")
        for change in ("disabled", "owner"):
            with self.subTest(change=change):
                self.users[2].is_active = True
                self.clusters["k8s_1"]["created_by"] = 2
                task_id = manager.create_cluster_async(*CREATE_ARGS, created_by=2,
                                                       group_id=11, class_id=10)
                self.run_queued(len(self.queued) - 1)
                self.assertEqual(self.queued[-1][0], "deploy")
                if change == "disabled":
                    self.users[2].is_active = False
                else:
                    self.clusters["k8s_1"]["created_by"] = 3
                self.run_queued(len(self.queued) - 1)
                self.assertEqual(manager.get_task_status(task_id)["status"], "error")
                deploy.assert_not_called()
        self.assertEqual(create.call_count, 2)
        self.assert_no_providers()

    def test_authorized_workers_pass_actor_and_complete(self):
        for enqueue, operation_name in [(manager.delete_cluster_async, "delete_cluster"),
                                         (manager.deploy_k8s_async, "deploy_k8s")]:
            with patch.object(manager, operation_name) as operation:
                task_id = enqueue("k8s_1", created_by=2)
                self.run_queued(len(self.queued) - 1)
                self.assertEqual(operation.call_args.kwargs["actor_id"], 2)
                self.assertEqual(manager.get_task_status(task_id)["status"], "completed")
        with patch.object(manager, "create_cluster", return_value=("k8s_1", self.clusters["k8s_1"])) as create:
            with patch.object(manager, "deploy_k8s") as deploy:
                task_id = manager.create_cluster_async(*CREATE_ARGS, created_by=2)
                self.run_queued(len(self.queued) - 1)
                self.run_queued(len(self.queued) - 1)
                self.assertEqual(create.call_args.kwargs["created_by"], 2)
                self.assertEqual(deploy.call_args.kwargs["actor_id"], 2)
                self.assertEqual(manager.get_task_status(task_id)["status"], "completed")

    def test_admin_task_is_visible_and_cancellable_to_resource_teacher_only(self):
        for enqueue in (manager.deploy_k8s_async, manager.delete_cluster_async):
            task_id = enqueue("k8s_1", created_by=1)
            task = manager.get_task_status(task_id)
            self.assertEqual((task["created_by"], task["owner_teacher_id"]), (1, 2))
            self.assertTrue(is_allowed(self.users[2], Actions.TASK_READ, task))
            self.assertFalse(is_allowed(self.users[3], Actions.TASK_READ, task))
            for actor_id in (1, 2):
                self.assertIn(task_id, [row["task_id"] for row in manager.list_tasks(actor=self.users[actor_id])])
            self.assertEqual(manager.list_tasks(actor=self.users[3]), [])
            with self.assertRaises(AuthorizationDenied):
                manager.cancel_task(task_id, actor_id=3)
            self.assertTrue(manager.cancel_task(task_id, actor_id=2))

    def test_admin_creating_for_teacher_group_records_owner(self):
        task_id = manager.create_cluster_async(*CREATE_ARGS, created_by=1,
                                               group_id=21, class_id=20)
        self.assertEqual(manager.get_task_status(task_id)["owner_teacher_id"], 3)

    def test_disabled_resource_owner_is_preserved_without_granting_access(self):
        self.users[2].is_active = False
        task_id = manager.deploy_k8s_async("k8s_1", created_by=1)
        self.assertEqual(manager.get_task_status(task_id)["owner_teacher_id"], 2)
        self.assertEqual(manager.list_tasks(actor=self.users[2]), [])
        with self.assertRaises(AuthorizationDenied):
            manager.cancel_task(task_id, actor_id=2)

    def test_synchronous_batch_checks_all_groups_before_any_operation(self):
        with patch.object(manager, "create_cluster") as create:
            with self.assertRaises(AuthorizationDenied):
                manager.batch_create_clusters([11, 21], *CREATE_ARGS, created_by=2)
            create.assert_not_called()
        self.assert_no_providers()

    def test_direct_synchronous_failures_have_safe_actor_audits(self):
        cases = [(lambda: manager.create_cluster(*CREATE_ARGS, created_by=2), "session_scope", Actions.CLUSTER_CREATE),
                 (lambda: manager.delete_cluster("k8s_1", actor_id=2), "_pve_client", Actions.CLUSTER_DELETE),
                 (lambda: manager.deploy_k8s("k8s_1", actor_id=2), "get_pve_server", Actions.CLUSTER_DEPLOY)]
        for operation, boundary, action in cases:
            with self.subTest(action=action):
                self.events.clear()
                with patch.object(manager, boundary, side_effect=RuntimeError(PRIVATE_KEY)):
                    with self.assertRaises(RuntimeError):
                        operation()
                writes = [event for event in self.events if event["action"] == action]
                self.assertEqual([event["outcome"] for event in writes], ["failure"])
                self.assertEqual(writes[0]["actor"], {"id": 2})
                self.assert_no_secret(self.events)

    def test_list_and_cancel_reload_actor_reject_disabled_and_missing(self):
        task_id = manager.deploy_k8s_async("k8s_1", created_by=2)
        old_actor = copy.deepcopy(self.users[2])
        self.users[2].is_active = False
        self.assertEqual(manager.list_tasks(actor=old_actor), [])
        with self.assertRaises(AuthorizationDenied):
            manager.cancel_task(task_id, actor_id=2)
        with self.assertRaises(AuthorizationDenied):
            manager.cancel_task(task_id)
        with self.assertRaises(AuthorizationDenied):
            manager.cancel_task("missing", actor_id=999)
        self.assertFalse(manager.cancel_task("missing", actor_id=1))

    def test_authorized_cancel_sets_event_and_denied_cancel_does_not(self):
        task_id = manager.deploy_k8s_async("k8s_1", created_by=2)
        event = threading.Event()
        manager._task_cancel_events[task_id] = event
        with self.assertRaises(AuthorizationDenied):
            manager.cancel_task(task_id, actor_id=3)
        self.assertFalse(event.is_set())
        self.assertEqual(manager.get_task_status(task_id)["status"], "running")
        self.assertTrue(manager.cancel_task(task_id, actor_id=2))
        self.assertTrue(event.is_set())
        self.assertEqual(manager.get_task_status(task_id)["status"], "cancelling")

    def test_student_cluster_authorization_uses_fresh_membership(self):
        self.assertEqual(service.authorize_cluster_action(4, Actions.WEBSSH_CONNECT, "k8s_1")[0].id, 4)
        self.memberships[4] = []
        with self.assertRaises(AuthorizationDenied):
            service.authorize_cluster_action(4, Actions.WEBSSH_CONNECT, "k8s_1")
        self.users[4].is_active = False
        with self.assertRaises(AuthorizationDenied):
            service.authorize_cluster_action(4, Actions.CLUSTER_READ, "k8s_1")
        with self.assertRaises(ValueError):
            service.authorize_cluster_action(1, Actions.CLUSTER_READ, "missing")

    def test_task_write_log_result_and_six_argument_callback_are_sanitized(self):
        callback_snapshots = []

        def callback(*args):
            self.assertEqual(len(args), 6)
            self.assertTrue(manager._task_lock.acquire(blocking=False))
            manager._task_lock.release()
            callback_snapshots.append((args, copy.deepcopy(manager.get_task_status(args[0]))))

        with patch.object(manager, "_on_task_update", callback):
            manager._update_task("task-a", message=PRIVATE_KEY, error=PRIVATE_KEY,
                                 result={"ssh_private_key": PRIVATE_KEY, "message": PRIVATE_KEY},
                                 created_by=1, owner_teacher_id=2, queue="deploy")
        manager._append_log("task-a", "exception [parameters: " + PRIVATE_KEY + "]")
        self.assert_no_secret(manager.get_task_status("task-a"))
        self.assert_no_secret(callback_snapshots)
        self.assertEqual(callback_snapshots[0][0][4:], (1, "deploy"))
        self.assertEqual(callback_snapshots[0][1]["owner_teacher_id"], 2)

    def test_raw_database_exceptions_never_enter_job_state_or_callback(self):
        error = RuntimeError("database failed [parameters: " + PRIVATE_KEY + "]")
        cases = [(lambda: manager.create_cluster_async(*CREATE_ARGS, created_by=2), "create_cluster"),
                 (lambda: manager.delete_cluster_async("k8s_1", created_by=2), "delete_cluster"),
                 (lambda: manager.deploy_k8s_async("k8s_1", created_by=2), "deploy_k8s")]
        for enqueue, operation in cases:
            with self.subTest(operation=operation):
                with patch.object(manager, operation, side_effect=error):
                    task_id = enqueue()
                    self.run_queued(len(self.queued) - 1)
                task = manager.get_task_status(task_id)
                self.assertEqual(task["status"], "error")
                self.assertEqual(task["error"], "操作失败，请联系管理员")
                self.assert_no_secret(task)
        self.assert_no_secret(self.callbacks)
        self.assert_no_secret(self.events)

    def test_cascaded_deploy_exception_is_safe(self):
        with patch.object(manager, "create_cluster", return_value=("k8s_1", self.clusters["k8s_1"])):
            task_id = manager.create_cluster_async(*CREATE_ARGS, created_by=2)
            self.run_queued(len(self.queued) - 1)
        with patch.object(manager, "deploy_k8s", side_effect=RuntimeError(PRIVATE_KEY)):
            self.run_queued(len(self.queued) - 1)
        self.assertEqual(manager.get_task_status(task_id)["status"], "error")
        self.assert_no_secret(manager.get_task_status(task_id))
        self.assert_no_secret(self.callbacks)

    def test_streaming_private_key_is_suppressed_across_chunks_and_lines(self):
        for payload in ("before\n" + PRIVATE_KEY + "\nafter\npassword=hidden-pass\n",
                        "before\n-----BEGIN OPENSSH PRIVATE KEY-----\nTEST_PRIVATE_BODY_ABC123\n",
                        "before\n" + PRIVATE_KEY + "\nafter-final"):
            for chunk_size in (1, 7, 43):
                with self.subTest(chunk_size=chunk_size, truncated="after" not in payload):
                    raw = payload.encode()
                    chunks = [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)]
                    channel = Mock()
                    channel.recv_ready.side_effect = lambda: bool(chunks)
                    channel.recv.side_effect = lambda size: chunks.pop(0)
                    channel.exit_status_ready.side_effect = lambda: not chunks
                    channel.recv_exit_status.return_value = 0
                    ssh = manager._SSHClient("mock.invalid", 22, "teacher", "unused")
                    ssh._ssh = Mock()
                    ssh._ssh.get_transport.return_value.open_session.return_value = channel
                    logs = []
                    with patch.object(manager._time, "sleep"):
                        ssh.exec_streaming("echo mocked-output", log_callback=logs.append)
                    self.assert_no_secret(logs)
                    rendered = "\n".join(logs)
                    self.assertNotIn("hidden-pass", rendered)
                    self.assertIn("before", rendered)
                    if "after" in payload:
                        self.assertIn("after", rendered)
                    channel.close.assert_called_once()

    def test_compensation_audits_success_after_database_write_and_failure_on_error(self):
        for actor_id in (None, 2, 4):
            with self.assertRaises(AuthorizationDenied):
                manager.force_delete_cluster("k8s_1", actor_id=actor_id)
        manager.delete_cluster_db.assert_not_called()
        self.events.clear()

        def delete_ok(name):
            self.assertEqual(name, "k8s_1")
            self.assertFalse(any(event["action"] == Actions.ADMIN_COMPENSATE and event["outcome"] == "success" for event in self.events))

        with patch.object(manager, "delete_cluster_db", side_effect=delete_ok):
            manager.force_delete_cluster("k8s_1", actor_id=1)
        self.assertEqual(self.events[-1]["outcome"], "success")
        self.assertEqual(self.events[-1]["actor"], {"id": 1})
        self.events.clear()
        with patch.object(manager, "delete_cluster_db", side_effect=RuntimeError(PRIVATE_KEY)):
            with self.assertRaises(RuntimeError):
                manager.force_delete_cluster("k8s_1", actor_id=1)
        writes = [event for event in self.events if event["action"] == Actions.ADMIN_COMPENSATE]
        self.assertEqual([event["outcome"] for event in writes], ["failure"])
        self.assert_no_secret(self.events)

    def test_authorization_audits_cover_initial_policy_reload_and_task_denials(self):
        with self.assertRaises(AuthorizationDenied):
            service.validate_cluster_creation(self.users[4])
        self.assertEqual(self.events[-1]["actor"], {"id": 4})
        self.assertEqual(self.events[-1]["action"], "cluster.create.authorize")
        with self.assertRaises(AuthorizationDenied):
            service.authorize_task(3, Actions.TASK_CANCEL, {"task_id": "test", "created_by": 2})
        self.assertEqual(self.events[-1]["actor"], {"id": 3})
        self.assertEqual(self.events[-1]["reason"], "权限不足")
        self.users[2].is_active = False
        with self.assertRaises(AuthorizationDenied):
            service.reload_actor(2)
        self.assertEqual(self.events[-1]["actor"], {"id": 2})
        self.assertEqual(self.events[-1]["outcome"], "denied")
        self.events.clear()
        service.validate_cluster_creation(self.users[1], [11], 10)
        service.authorize_cluster_action(1, Actions.CLUSTER_DELETE, "k8s_1")
        self.assertTrue(all(event["action"].endswith(".authorize") for event in self.events))
        self.assert_no_providers()


if __name__ == "__main__":
    unittest.main()
