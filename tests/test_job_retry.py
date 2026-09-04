"""Trusted job retries: in-memory resources and captured real queue closures."""

import copy
import dataclasses
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

# This helper suppresses live database configuration discovery before importing
# manager, and guards all database/provider boundaries with in-memory mocks.
try:
    from . import test_job_security as fixture
except ImportError:
    import test_job_security as fixture


manager = fixture.manager
service = fixture.service
Actions = fixture.Actions
AuthorizationDenied = fixture.AuthorizationDenied


class JobRetryTests(unittest.TestCase):
    mock = fixture.JobSecurityTests.mock
    run_queued = fixture.JobSecurityTests.run_queued
    assert_no_providers = fixture.JobSecurityTests.assert_no_providers
    assert_no_secret = fixture.JobSecurityTests.assert_no_secret

    def setUp(self):
        fixture.JobSecurityTests.setUp(self)
        self.stack.enter_context(patch.dict(manager._task_retry_descriptions, {}, clear=True))
        self.stack.enter_context(patch.dict(manager._task_retry_reservations, {}, clear=True))
        self.mock(manager, "_task_retry_pending", new=set())
        self.clusters["k8s_1"]["vms"] = {
            "client-k8s1": {"node": "node-test", "vmid": 101},
            "master1-k8s1": {"node": "node-test", "vmid": 102},
        }
        self.clusters["k8s_1"].update(ssh_private_key=fixture.PRIVATE_KEY,
                                      password="test-password-do-not-expose")

    def fail_source(self, operation="deploy", created_by=2):
        entrypoint = manager.deploy_k8s_async if operation == "deploy" else manager.delete_cluster_async
        sync_name = "deploy_k8s" if operation == "deploy" else "delete_cluster"
        task_id = entrypoint("k8s_1", created_by=created_by)
        with patch.object(manager, sync_name, side_effect=RuntimeError(fixture.PRIVATE_KEY)):
            self.run_queued(len(self.queued) - 1)
        self.assertEqual(manager.get_task_status(task_id)["status"], "error")
        return task_id

    def run_successful_child(self, child_id, operation="deploy"):
        index = next(i for i, (_, task) in enumerate(self.queued) if task.task_id == child_id)
        sync_name = "deploy_k8s" if operation == "deploy" else "delete_cluster"
        with patch.object(manager, sync_name) as actual_operation:
            self.run_queued(index)
        actual_operation.assert_called_once()
        self.assertEqual(actual_operation.call_args.args, ("k8s_1",))
        self.assertEqual(manager.get_task_status(child_id)["status"], "completed")
        return actual_operation.call_args

    def retry_events(self):
        return [event for event in self.events if event["action"] == "job.retry"]

    def test_business_exceptions_ignore_sensitive_constructor_arguments(self):
        for exception_type, expected in (
            (manager.TaskNotFoundError, "任务不存在"),
            (manager.TaskRetryConflict, "任务当前不可重试，请检查任务和资源状态"),
        ):
            for args in ((), (fixture.PRIVATE_KEY,), (fixture.PRIVATE_KEY, "internal provider details")):
                with self.subTest(exception_type=exception_type, argument_count=len(args)):
                    error = exception_type(*args)
                    self.assertEqual(str(error), expected)
                    self.assertEqual(error.args, (expected,))
                    self.assert_no_secret(str(error))

    def test_admin_creator_and_resource_owner_can_retry_deploy_and_delete(self):
        for operation in ("deploy", "delete"):
            for creator, retry_actor in ((2, 1), (2, 2), (1, 2)):
                with self.subTest(operation=operation, creator=creator, retry_actor=retry_actor):
                    source = self.fail_source(operation, creator)
                    child = manager.retry_task(source, actor_id=retry_actor)
                    self.assertNotEqual(child, source)
                    job = manager.get_task_status(child)
                    self.assertEqual(job["created_by"], retry_actor)
                    self.assertEqual(job["owner_teacher_id"], 2)
                    self.assertEqual(job["retry_of"], source)
                    called = self.run_successful_child(child, operation)
                    self.assertEqual(called.kwargs["actor_id"], retry_actor)
        self.assert_no_providers()

    def test_missing_disabled_and_unrelated_actors_cannot_retry(self):
        source = self.fail_source()
        count = len(self.queued)
        for actor_id in (None, 999, 3, 4):
            with self.subTest(actor_id=actor_id):
                with self.assertRaises(AuthorizationDenied):
                    manager.retry_task(source, actor_id=actor_id)
        self.users[2].is_active = False
        with self.assertRaises(AuthorizationDenied):
            manager.retry_task(source, actor_id=2)
        self.assertEqual(len(self.queued), count)
        self.assertNotIn(source, manager._task_retry_reservations)
        self.assertTrue(all(event["outcome"] == "denied" for event in self.retry_events()))

    def test_students_own_trusted_job_is_denied_by_actual_cluster_action(self):
        for operation, action in (("deploy", Actions.CLUSTER_DEPLOY), ("delete", Actions.CLUSTER_DELETE)):
            source = self.fail_source(operation)
            manager._task_store[source]["created_by"] = 4
            manager._task_store[source]["owner_teacher_id"] = None
            self.assertTrue(fixture.is_allowed(self.users[4], Actions.TASK_RETRY,
                                              manager.get_task_status(source)))
            for status in ("error", "running", "completed"):
                with self.subTest(operation=operation, status=status):
                    manager._task_store[source]["status"] = status
                    count = len(self.queued)
                    with patch.object(manager, "authorize_cluster_action", wraps=manager.authorize_cluster_action) as auth:
                        with self.assertRaises(AuthorizationDenied):
                            manager.retry_task(source, actor_id=4)
                    auth.assert_called_once_with(4, action, "k8s_1")
                    self.assertEqual(len(self.queued), count)
        self.assert_no_providers()

    def test_task_permission_precedes_target_and_status_disclosure(self):
        source = self.fail_source()
        manager._task_store[source]["status"] = "running"
        with patch.object(manager, "authorize_cluster_action", wraps=manager.authorize_cluster_action) as auth:
            with self.assertRaises(AuthorizationDenied):
                manager.retry_task(source, actor_id=3)
            auth.assert_not_called()
        manager._task_store[source]["created_by"] = 3
        with patch.object(manager, "authorize_cluster_action", wraps=manager.authorize_cluster_action) as auth:
            with self.assertRaises(AuthorizationDenied):
                manager.retry_task(source, actor_id=3)
            auth.assert_called_once_with(3, Actions.CLUSTER_DEPLOY, "k8s_1")

    def test_missing_source_requires_authorization_before_not_found(self):
        with patch.object(manager, "authorize_task", wraps=manager.authorize_task) as auth:
            with self.assertRaises(manager.TaskNotFoundError) as raised:
                manager.retry_task("absent", actor_id=1)
            self.assertEqual(str(raised.exception), "任务不存在")
            self.assertEqual(auth.call_args.args[1], Actions.TASK_RETRY)
        for actor_id in (2, 3, 4, None):
            with self.subTest(actor_id=actor_id):
                with self.assertRaises(AuthorizationDenied):
                    manager.retry_task("absent", actor_id=actor_id)
        self.assertEqual(self.queued, [])

    def test_only_error_or_cancelled_states_are_retryable(self):
        source = self.fail_source()
        count = len(self.queued)
        for status in ("running", "completed", "cancelling", "unknown", None):
            with self.subTest(status=status):
                manager._task_store[source]["status"] = status
                with self.assertRaises(manager.TaskRetryConflict):
                    manager.retry_task(source, actor_id=2)
                self.assertEqual(len(self.queued), count)
        manager._task_store[source]["status"] = "cancelled"
        child = manager.retry_task(source, actor_id=2)
        self.run_successful_child(child)

    def test_public_task_fields_cannot_supply_or_replace_retry_parameters(self):
        manager._update_task("legacy", status="error", created_by=2, queue="deploy",
                             result={"name": "k8s_1", "operation": "deploy"})
        manager._task_store["legacy"].update(operation="deploy", cluster_name="k8s_1",
                                            retry_description={"operation": "deploy", "name": "k8s_1"})
        with self.assertRaises(manager.TaskRetryConflict):
            manager.retry_task("legacy", actor_id=2)
        self.assertEqual(self.queued, [])
        source = self.fail_source()
        manager._task_store[source].update(queue="delete", operation="delete",
                                          cluster_name="someone-elses-cluster", retry_of="legacy")
        child = manager.retry_task(source, actor_id=2)
        self.assertEqual(self.queued[-1][0], "deploy")
        self.run_successful_child(child, "deploy")

    def test_failed_creation_never_gets_a_retry_descriptor(self):
        source = manager.create_cluster_async(*fixture.CREATE_ARGS, created_by=2, group_id=11, class_id=10)
        with patch.object(manager, "create_cluster", side_effect=RuntimeError("possible leftover resources")) as create:
            self.run_queued()
            self.assertEqual(manager.get_task_status(source)["status"], "error")
            self.assertNotIn(source, manager._task_retry_descriptions)
            with self.assertRaises(manager.TaskRetryConflict):
                manager.retry_task(source, actor_id=2)
            create.assert_called_once()
        self.assertEqual(len(self.queued), 1)

    def test_create_then_deploy_failure_retries_only_deployment(self):
        source = manager.create_cluster_async(*fixture.CREATE_ARGS, created_by=2, group_id=11, class_id=10)
        with patch.object(manager, "create_cluster", return_value=("k8s_1", copy.deepcopy(self.clusters["k8s_1"]))) as create:
            self.run_queued()
            self.assertEqual(manager._task_retry_descriptions[source].operation, "deploy")
            with patch.object(manager, "deploy_k8s", side_effect=RuntimeError(fixture.PRIVATE_KEY)):
                self.run_queued(1)
            child = manager.retry_task(source, actor_id=1)
            self.run_successful_child(child)
            create.assert_called_once()
        self.assertEqual([queue for queue, _ in self.queued], ["create", "deploy", "deploy"])

    def test_creation_string_group_id_matches_persisted_integer_for_deploy_retry(self):
        returned_cluster = copy.deepcopy(self.clusters["k8s_1"])
        returned_cluster["group_id"] = "11"
        source = manager.create_cluster_async(*fixture.CREATE_ARGS, created_by=2,
                                               group_id="11", class_id="10")
        with patch.object(manager, "create_cluster", return_value=("k8s_1", returned_cluster)) as create:
            self.run_queued()
            self.assertEqual(manager._task_retry_descriptions[source].resource_fingerprint[3], 11)
            self.assertEqual(returned_cluster["group_id"], "11")
            self.assertEqual(self.clusters["k8s_1"]["group_id"], 11)
            with patch.object(manager, "deploy_k8s", side_effect=RuntimeError("automatic deploy failed")):
                self.run_queued(1)
            child = manager.retry_task(source, actor_id=2)
            self.run_successful_child(child)
            create.assert_called_once()
            self.assertEqual(create.call_args.kwargs["group_id"], "11")
        self.assertEqual([queue for queue, _ in self.queued], ["create", "deploy", "deploy"])

    def test_fingerprint_strictly_normalizes_numeric_ids_without_mutating_resource(self):
        cluster = copy.deepcopy(self.clusters["k8s_1"])
        expected = manager._retry_resource_fingerprint("k8s_1", cluster)
        for key in ("pve_server_id", "created_by", "group_id"):
            cluster[key] = str(cluster[key])
        for vm in cluster["vms"].values():
            vm["vmid"] = str(vm["vmid"])
        self.assertEqual(manager._retry_resource_fingerprint("k8s_1", cluster), expected)
        self.assertEqual(cluster["pve_server_id"], "7")
        self.assertEqual(cluster["vms"]["client-k8s1"]["vmid"], "101")
        cluster.update(pve_server_id="0", created_by=None, group_id=None)
        self.assertEqual(manager._retry_resource_fingerprint("k8s_1", cluster)[1:4], (0, None, None))
        cluster["pve_server_id"] = None
        self.assertEqual(manager._retry_resource_fingerprint("k8s_1", cluster)[1:4], (None, None, None))

    def test_fingerprint_rejects_invalid_numeric_identity_values(self):
        invalid = (True, False, 1.0, 101.9, -1, "-1", "x", "", "01", "+1", " 1", "1 ", "1.0", "１")
        for key in ("pve_server_id", "created_by", "group_id", "vmid"):
            for value in invalid + (() if key == "pve_server_id" else (0, "0")):
                with self.subTest(key=key, value=repr(value)):
                    cluster = copy.deepcopy(self.clusters["k8s_1"])
                    if key == "vmid":
                        cluster["vms"]["client-k8s1"][key] = value
                    else:
                        cluster[key] = value
                    with self.assertRaises(manager.TaskRetryConflict):
                        manager._retry_resource_fingerprint("k8s_1", cluster)

    def test_batch_children_follow_the_same_creation_boundary(self):
        first, second = manager.batch_create_clusters_async([11, 12], *fixture.CREATE_ARGS,
                                                            created_by=2, class_id=10)
        with patch.object(manager, "create_cluster", return_value=("k8s_1", copy.deepcopy(self.clusters["k8s_1"]))):
            self.run_queued(0)
        with patch.object(manager, "create_cluster", side_effect=RuntimeError("create failed")):
            self.run_queued(1)
        with patch.object(manager, "deploy_k8s", side_effect=RuntimeError("deploy failed")):
            self.run_queued(2)
        with self.assertRaises(manager.TaskRetryConflict):
            manager.retry_task(second, actor_id=2)
        self.run_successful_child(manager.retry_task(first, actor_id=2))

    def test_parent_is_unchanged_and_child_inherits_current_ownership(self):
        source = self.fail_source(created_by=1)
        manager._task_store[source]["result"] = {"unchanged": "original result"}
        before = copy.deepcopy(manager.get_task_status(source))
        child = manager.retry_task(source, actor_id=2)
        self.assertEqual(manager.get_task_status(source), before)
        self.assertEqual(manager.get_task_status(child)["retry_of"], source)
        self.assertEqual(manager.get_task_status(child)["created_by"], 2)
        self.assertEqual(manager.get_task_status(child)["owner_teacher_id"], 2)
        for actor_id in (1, 2):
            row = next(row for row in manager.list_tasks(actor=self.users[actor_id]) if row["task_id"] == child)
            self.assertEqual(row["retry_of"], source)
        self.assertEqual(manager.list_tasks(actor=self.users[3]), [])
        self.run_successful_child(child)
        self.assertEqual(manager.get_task_status(source), before)

    def test_one_direct_retry_per_parent_but_failed_child_can_be_retried(self):
        source = self.fail_source()
        child = manager.retry_task(source, actor_id=2)
        with patch.object(manager, "deploy_k8s", side_effect=RuntimeError("retry failure")):
            self.run_queued(len(self.queued) - 1)
        with self.assertRaises(manager.TaskRetryConflict):
            manager.retry_task(source, actor_id=1)
        grandchild = manager.retry_task(child, actor_id=1)
        self.assertEqual(manager.get_task_status(grandchild)["retry_of"], child)
        self.run_successful_child(grandchild)

    def test_concurrent_requests_enqueue_exactly_one_child(self):
        source = self.fail_source()
        before = len(self.queued)
        barrier = threading.Barrier(2)
        local = threading.local()
        real_authorize = manager._authorize_retry_resource

        def synchronize(actor_id, description):
            if not getattr(local, "seen", False):
                local.seen = True
                barrier.wait(timeout=5)
            return real_authorize(actor_id, description)

        with patch.object(manager, "_authorize_retry_resource", side_effect=synchronize):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(manager.retry_task, source, actor_id=2) for _ in range(2)]
                accepted, conflicts = [], []
                for future in futures:
                    try:
                        accepted.append(future.result(timeout=10))
                    except manager.TaskRetryConflict as exc:
                        conflicts.append(exc)
        self.assertEqual((len(accepted), len(conflicts)), (1, 1))
        self.assertEqual(len(self.queued), before + 1)
        self.assertEqual(manager._task_retry_reservations[source], accepted[0])
        self.assertEqual(manager._task_retry_pending, set())
        self.run_successful_child(accepted[0])

    def test_enqueue_failure_is_safe_releases_reservation_and_allows_next_attempt(self):
        source = self.fail_source()
        original = copy.deepcopy(manager.get_task_status(source))
        ids = set(manager._task_store)
        descriptors = dict(manager._task_retry_descriptions)
        self.events.clear()
        with patch.object(manager.scheduler, "enqueue", side_effect=RuntimeError(fixture.PRIVATE_KEY)):
            with self.assertRaises(RuntimeError) as raised:
                manager.retry_task(source, actor_id=2)
        self.assertNotIsInstance(raised.exception, manager.K8sError)
        self.assertEqual(str(raised.exception), "任务重试失败，请稍后重试")
        self.assertEqual(set(manager._task_store), ids)
        self.assertEqual(manager._task_retry_descriptions, descriptors)
        self.assertEqual(manager.get_task_status(source), original)
        self.assertNotIn(source, manager._task_retry_reservations)
        self.assertEqual(manager._task_retry_pending, set())
        self.assertEqual([event["outcome"] for event in self.retry_events()], ["failure"])
        self.assert_no_secret(self.events)
        self.run_successful_child(manager.retry_task(source, actor_id=2))

    def test_admission_rejects_missing_changed_resource_and_non_running_deploy(self):
        pristine = copy.deepcopy(self.clusters["k8s_1"])
        changes = [lambda c: c.update(pve_server_id=8),
                   lambda c: c.update(created_by=3),
                   lambda c: c.update(group_id=12),
                   lambda c: c.update(name="k8s_2"),
                   lambda c: c["vms"]["client-k8s1"].update(node="other-node"),
                   lambda c: c["vms"]["client-k8s1"].update(vmid=999),
                   lambda c: c["vms"].update(extra={"node": "node-test", "vmid": 103}),
                   lambda c: c["vms"].pop("master1-k8s1"),
                   lambda c: c.update(status="stopped")]
        for change in changes:
            with self.subTest(change=change):
                self.clusters["k8s_1"] = copy.deepcopy(pristine)
                source = self.fail_source()
                count = len(self.queued)
                change(self.clusters["k8s_1"])
                with self.assertRaises(manager.TaskRetryConflict):
                    manager.retry_task(source, actor_id=1)
                self.assertEqual(len(self.queued), count)
                self.assertNotIn(source, manager._task_retry_reservations)
        self.clusters["k8s_1"] = copy.deepcopy(pristine)
        source = self.fail_source()
        del self.clusters["k8s_1"]
        with self.assertRaises(manager.TaskRetryConflict):
            manager.retry_task(source, actor_id=1)

    def test_cluster_ownership_change_denies_teacher_before_fingerprint_conflict(self):
        source = self.fail_source()
        self.clusters["k8s_1"]["created_by"] = 3
        with self.assertRaises(AuthorizationDenied):
            manager.retry_task(source, actor_id=2)

    def test_worker_rechecks_fingerprint_after_enqueue_without_any_operation(self):
        pristine = copy.deepcopy(self.clusters["k8s_1"])
        changes = [lambda c: c.update(pve_server_id=8),
                   lambda c: c.update(created_by=3),
                   lambda c: c.update(group_id=12),
                   lambda c: c["vms"]["client-k8s1"].update(node="other-node"),
                   lambda c: c["vms"]["client-k8s1"].update(vmid=999),
                   lambda c: c["vms"].update(extra={"node": "node-test", "vmid": 103}),
                   lambda c: c["vms"].pop("master1-k8s1"),
                   lambda c: c.update(status="stopped")]
        for change in changes:
            with self.subTest(change=change):
                self.clusters["k8s_1"] = copy.deepcopy(pristine)
                source = self.fail_source()
                child = manager.retry_task(source, actor_id=1)
                change(self.clusters["k8s_1"])
                with patch.object(manager, "deploy_k8s") as deploy:
                    self.run_queued(len(self.queued) - 1)
                    deploy.assert_not_called()
                self.assertEqual(manager.get_task_status(child)["status"], "error")
        self.assert_no_providers()

    def test_worker_rechecks_current_actor_and_child_task_permissions(self):
        for change in ("disabled", "demoted", "child_owner", "parent_owner", "cluster_owner"):
            with self.subTest(change=change):
                self.users[2].is_active = True
                self.users[2].role = "teacher"
                self.clusters["k8s_1"]["created_by"] = 2
                source = self.fail_source()
                child = manager.retry_task(source, actor_id=2)
                if change == "disabled":
                    self.users[2].is_active = False
                elif change == "demoted":
                    self.users[2].role = "student"
                elif change == "cluster_owner":
                    self.clusters["k8s_1"]["created_by"] = 3
                else:
                    target = child if change == "child_owner" else source
                    manager._task_store[target].update(created_by=3, owner_teacher_id=3)
                with patch.object(manager, "deploy_k8s") as deploy:
                    self.run_queued(len(self.queued) - 1)
                    deploy.assert_not_called()
                self.assertEqual(manager.get_task_status(child)["status"], "error")

    def test_delete_worker_also_rechecks_provider_and_actor(self):
        for change in ("provider", "disabled"):
            with self.subTest(change=change):
                self.users[2].is_active = True
                self.clusters["k8s_1"]["pve_server_id"] = 7
                source = self.fail_source("delete")
                child = manager.retry_task(source, actor_id=2)
                if change == "provider":
                    self.clusters["k8s_1"]["pve_server_id"] = 8
                else:
                    self.users[2].is_active = False
                with patch.object(manager, "delete_cluster") as delete:
                    self.run_queued(len(self.queued) - 1)
                    delete.assert_not_called()
                self.assertEqual(manager.get_task_status(child)["status"], "error")

    def test_expired_parent_cleanup_does_not_invalidate_accepted_child(self):
        source = self.fail_source()
        manager._task_store[source]["updated_at"] = manager._time.time() - 1801
        child = manager.retry_task(source, actor_id=2)
        self.assertIsNone(manager.get_task_status(source))
        self.assertNotIn(source, manager._task_retry_descriptions)
        self.assertNotIn(source, manager._task_retry_reservations)
        self.assertIn(child, manager._task_retry_descriptions)
        self.run_successful_child(child)

    def test_child_missing_or_private_descriptor_mismatch_fails_closed(self):
        for change in ("missing", "description"):
            with self.subTest(change=change):
                source = self.fail_source()
                child = manager.retry_task(source, actor_id=2)
                if change == "missing":
                    manager._task_store.pop(child)
                else:
                    description = manager._task_retry_descriptions[child]
                    manager._task_retry_descriptions[child] = dataclasses.replace(description, operation="delete")
                with patch.object(manager, "deploy_k8s") as deploy:
                    self.run_queued(len(self.queued) - 1)
                    deploy.assert_not_called()
                if change == "missing":
                    self.assertIsNone(manager.get_task_status(child))
                else:
                    self.assertEqual(manager.get_task_status(child)["status"], "error")

    def test_audit_success_is_after_enqueue_and_keeps_safe_source_child_actor(self):
        source = self.fail_source()
        self.events.clear()

        def enqueue(queue, task):
            self.assertFalse(any(event["outcome"] == "success" for event in self.retry_events()))
            self.queued.append((queue, task))

        with patch.object(manager.scheduler, "enqueue", side_effect=enqueue):
            child = manager.retry_task(source, actor_id=1)
        successes = [event for event in self.retry_events() if event["outcome"] == "success"]
        self.assertEqual(len(successes), 1)
        event = successes[0]
        self.assertEqual(event["actor"], {"id": 1})
        self.assertEqual(event["resource_id"], source)
        self.assertEqual(event["metadata"]["source_task_id"], source)
        self.assertEqual(event["metadata"]["child_task_id"], child)
        self.assertEqual(event["metadata"]["cluster_name"], "k8s_1")
        self.assert_no_secret(self.events)

    def test_private_description_never_leaks_into_public_jobs_or_callbacks(self):
        source = self.fail_source()
        child = manager.retry_task(source, actor_id=2)
        description = manager._task_retry_descriptions[child]
        self.assertEqual(set(dataclasses.asdict(description)), {"operation", "cluster_name", "resource_fingerprint"})
        self.assertEqual(description.resource_fingerprint,
                         ("k8s_1", 7, 2, 11, (("client-k8s1", "node-test", 101), ("master1-k8s1", "node-test", 102))))
        self.assert_no_secret(dataclasses.asdict(description))
        self.assertNotIn("test-password-do-not-expose", repr(description))
        public = [manager.get_task_status(source), manager.get_task_status(child),
                  manager.list_tasks(actor=self.users[2]), self.callbacks]
        rendered = json.dumps(public, ensure_ascii=False)
        for name in ("resource_fingerprint", "cluster_name", "operation", "_task_retry_descriptions"):
            self.assertNotIn(name, rendered)
        self.assert_no_secret(public)
        self.assertTrue(all(len(callback) == 6 for callback in self.callbacks))

    def test_no_database_callback_audit_or_enqueue_is_under_task_lock(self):
        source = self.fail_source()

        def assert_unlocked():
            self.assertTrue(manager._task_lock.acquire(blocking=False))
            manager._task_lock.release()

        real_get_user = service.get_user.side_effect
        real_get_cluster = service.load_cluster.side_effect
        real_owner_user = manager.get_user.side_effect
        real_audit = self.audit.side_effect
        self.mock(service, "get_user", side_effect=lambda uid: (assert_unlocked(), real_get_user(uid))[1])
        self.mock(service, "load_cluster", side_effect=lambda name: (assert_unlocked(), real_get_cluster(name))[1])
        self.mock(manager, "get_user", side_effect=lambda uid: (assert_unlocked(), real_owner_user(uid))[1])
        self.audit.side_effect = lambda *args, **kwargs: (assert_unlocked(), real_audit(*args, **kwargs))[1]
        self.mock(manager, "_on_task_update", new=lambda *args: (assert_unlocked(), self.callbacks.append(args)))
        self.mock(manager.scheduler, "enqueue", side_effect=lambda queue, task: (assert_unlocked(), self.queued.append((queue, task))))
        self.run_successful_child(manager.retry_task(source, actor_id=2))

    def test_cleanup_removes_private_metadata_and_pending_submission_is_protected(self):
        source = self.fail_source()
        manager._task_store[source]["updated_at"] = manager._time.time() - 1801

        def enqueue(queue, task):
            manager._cleanup_old_tasks()
            self.assertIn(source, manager._task_store)
            self.assertIn(task.task_id, manager._task_store)
            self.queued.append((queue, task))

        with patch.object(manager.scheduler, "enqueue", side_effect=enqueue):
            child = manager.retry_task(source, actor_id=2)
        self.assertNotIn(source, manager._task_store)
        self.assertEqual(manager._task_retry_pending, set())
        manager._task_store[child]["updated_at"] = manager._time.time() - 1801
        manager._task_cancel_events[child] = threading.Event()
        manager._cleanup_old_tasks()
        self.assertNotIn(child, manager._task_store)
        self.assertNotIn(child, manager._task_retry_descriptions)
        self.assertNotIn(child, manager._task_cancel_events)
        self.assertEqual(manager._task_retry_reservations, {})


if __name__ == "__main__":
    unittest.main()
