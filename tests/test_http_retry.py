"""Retry HTTP contracts; the service alone controls the target and actor policy."""

import unittest
from contextlib import ExitStack
from unittest.mock import patch

# Import the safe fixture before any application/service module can load DB config.
from tests.test_security_contract import app_module as m
from modules.authz import AuthorizationDenied
from modules.k8s_manager import TaskNotFoundError, TaskRetryConflict


class HttpRetryTests(unittest.TestCase):
    path = "/api/k8s/tasks/source-task/retry"

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("modules.db.session_scope", side_effect=AssertionError("real DB access forbidden")))
        self.stack.enter_context(patch.object(m, "_allowed_origins", []))
        self.retry = self.stack.enter_context(patch.object(m, "retry_task", return_value="replacement-task"))
        self.audit = self.stack.enter_context(patch.object(m, "security_audit"))

    def client(self, user_id=None):
        client = m.app.test_client()
        if user_id is not None:
            with client.session_transaction() as session:
                session["_user_id"] = str(user_id)
                session["_fresh"] = True
        return client

    def csrf(self, client):
        response = client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)
        token = response.get_json()["csrf_token"]
        self.assertTrue(token)
        self.assertNotEqual(token, "[REDACTED]")
        return token

    def post(self, client, **kwargs):
        return client.post(self.path, headers={"X-CSRFToken": self.csrf(client)}, **kwargs)

    def test_anonymous_user_is_401_even_with_valid_csrf(self):
        response = self.post(self.client())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "未登录，请先登录"})
        self.retry.assert_not_called()

    def test_authenticated_user_without_or_with_invalid_csrf_is_400(self):
        client = self.client(2)
        for headers in ({}, {"X-CSRFToken": "invalid"}):
            with self.subTest(headers=headers):
                response = client.post(self.path, headers=headers)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json(), {"error": "CSRF 校验失败"})
        self.retry.assert_not_called()

    def test_valid_retry_returns_202_and_passes_only_authenticated_actor(self):
        response = self.post(self.client(2))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"task_id": "replacement-task", "retry_of": "source-task"})
        self.retry.assert_called_once_with("source-task", actor_id=2)
        writes = [call for call in self.audit.call_args_list if call.args[:2] == ("http.write", "success")]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].kwargs["metadata"], {"method": "POST", "path": self.path, "status": 202})

    def test_malicious_body_and_query_cannot_change_service_arguments(self):
        client = self.client(2)
        response = client.post(self.path + "?actor_id=1&created_by=1&task_type=delete",
                               headers={"X-CSRFToken": self.csrf(client)}, json={
                                   "created_by": 1, "actor_id": 1, "owner_teacher_id": 3,
                                   "task_id": "different-task", "target": "other-cluster",
                                   "task_type": "delete", "parameters": {"pve_server_id": 999},
                               })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"task_id": "replacement-task", "retry_of": "source-task"})
        self.retry.assert_called_once_with("source-task", actor_id=2)
        self.retry.reset_mock()
        response = self.post(client, data="{not-json", content_type="application/json")
        self.assertEqual(response.status_code, 202)
        self.retry.assert_called_once_with("source-task", actor_id=2)

    def test_service_authorization_denial_is_403(self):
        self.retry.side_effect = AuthorizationDenied("internal owner details")
        response = self.post(self.client(2))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"error": "权限不足"})
        self.retry.assert_called_once_with("source-task", actor_id=2)

    def test_missing_task_is_404_with_fixed_message(self):
        self.retry.side_effect = TaskNotFoundError()
        response = self.post(self.client(2))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {"error": "任务不存在"})

    def test_retry_conflict_is_409_with_fixed_message(self):
        self.retry.side_effect = TaskRetryConflict()
        response = self.post(self.client(2))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json(), {"error": "当前任务状态不允许重试"})

    def test_sensitive_provider_exception_is_fixed_500(self):
        secret = "FAKE-RETRY-PROVIDER-SECRET"
        self.retry.side_effect = RuntimeError(
            "provider password=" + secret + " token_value=" + secret
            + " -----BEGIN OPENSSH PRIVATE KEY-----\n" + secret
            + "\n-----END OPENSSH PRIVATE KEY-----"
        )
        response = self.post(self.client(2))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "集群操作失败，请稍后重试"})
        self.assertNotIn(secret, response.get_data(as_text=True))
        self.assertNotIn(secret, str(self.audit.call_args_list))

    def test_disallowed_origin_rejected_before_retry_service(self):
        client = self.client(2)
        response = client.post(self.path, headers={"X-CSRFToken": self.csrf(client), "Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"error": "不允许的请求来源"})
        self.retry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
