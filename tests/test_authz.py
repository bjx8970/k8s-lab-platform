import copy
import json
import logging
import unittest
from types import SimpleNamespace

from modules.audit import sanitize, security_audit
from modules.authz import (
    Actions,
    AuthorizationDenied,
    is_allowed,
    require_allowed,
)


ALL_ACTIONS = [
    Actions.PROVIDER_READ,
    Actions.PROVIDER_WRITE,
    Actions.CLUSTER_LIST,
    Actions.CLUSTER_READ,
    Actions.CLUSTER_CREATE,
    Actions.CLUSTER_DELETE,
    Actions.CLUSTER_DEPLOY,
    Actions.CLUSTER_VM_ACTION,
    Actions.WEBSSH_CONNECT,
    Actions.WEBSSH_TERMINATE_OWN,
    Actions.WEBSSH_OBSERVE,
    Actions.TASK_READ,
    Actions.TASK_CANCEL,
    Actions.TASK_RETRY,
    Actions.ADMIN_COMPENSATE,
]


def user(user_id=1, role="admin", authenticated=True, active=True):
    return SimpleNamespace(
        id=user_id,
        role=role,
        is_authenticated=authenticated,
        is_active=active,
    )


class AuthorizationTests(unittest.TestCase):
    def test_unknown_action_and_unknown_role_are_denied(self):
        self.assertFalse(is_allowed(user(), "unknown.action"))
        for action in ALL_ACTIONS:
            with self.subTest(action=action):
                self.assertFalse(is_allowed(user(role="unknown"), action))

    def test_missing_unauthenticated_and_inactive_users_are_denied(self):
        for action in ALL_ACTIONS:
            with self.subTest(state="missing", action=action):
                self.assertFalse(is_allowed(None, action))
            with self.subTest(state="unauthenticated", action=action):
                self.assertFalse(
                    is_allowed(user(authenticated=False), action)
                )
            with self.subTest(state="inactive", action=action):
                self.assertFalse(is_allowed(user(active=False), action))

    def test_admin_is_allowed_every_known_action(self):
        admin = {
            "id": "1",
            "role": "admin",
            "is_authenticated": True,
            "is_active": True,
        }
        for action in ALL_ACTIONS:
            with self.subTest(action=action):
                self.assertTrue(is_allowed(admin, action))

    def test_teacher_unscoped_permissions_and_explicit_denials(self):
        teacher = user(user_id=7, role="teacher")
        for action in (
            Actions.PROVIDER_READ,
            Actions.CLUSTER_LIST,
            Actions.CLUSTER_CREATE,
        ):
            with self.subTest(action=action):
                self.assertTrue(is_allowed(teacher, action))
        for action in (Actions.PROVIDER_WRITE, Actions.ADMIN_COMPENSATE):
            with self.subTest(action=action):
                self.assertFalse(is_allowed(teacher, action))

    def test_teacher_cluster_actions_require_created_by(self):
        teacher = user(user_id=7, role="teacher")
        actions = (
            Actions.CLUSTER_READ,
            Actions.CLUSTER_DELETE,
            Actions.CLUSTER_DEPLOY,
            Actions.CLUSTER_VM_ACTION,
            Actions.WEBSSH_CONNECT,
        )
        for action in actions:
            with self.subTest(action=action, owner=True):
                self.assertTrue(
                    is_allowed(teacher, action, {"created_by": "7"})
                )
            with self.subTest(action=action, owner=False):
                self.assertFalse(
                    is_allowed(teacher, action, {"created_by": 8})
                )
            with self.subTest(action=action, owner=None):
                self.assertFalse(
                    is_allowed(teacher, action, {"created_by": None})
                )

    def test_teacher_session_boundaries(self):
        teacher = user(user_id=7, role="teacher")
        self.assertTrue(
            is_allowed(
                teacher,
                Actions.WEBSSH_TERMINATE_OWN,
                {"owner_user_id": "7"},
            )
        )
        self.assertFalse(
            is_allowed(
                teacher,
                Actions.WEBSSH_TERMINATE_OWN,
                {"owner_user_id": 8},
            )
        )
        self.assertTrue(
            is_allowed(
                teacher,
                Actions.WEBSSH_OBSERVE,
                {"owner_teacher_id": "7"},
            )
        )
        self.assertFalse(
            is_allowed(
                teacher,
                Actions.WEBSSH_OBSERVE,
                {"owner_teacher_id": 8},
            )
        )

    def test_teacher_task_creator_or_owner_teacher_boundaries(self):
        teacher = user(user_id=7, role="teacher")
        for action in (
            Actions.TASK_READ,
            Actions.TASK_CANCEL,
            Actions.TASK_RETRY,
        ):
            with self.subTest(action=action, relation="creator"):
                self.assertTrue(
                    is_allowed(teacher, action, {"created_by": "7"})
                )
            with self.subTest(action=action, relation="owner_teacher"):
                self.assertTrue(
                    is_allowed(
                        teacher,
                        action,
                        {"created_by": 8, "owner_teacher_id": "7"},
                    )
                )
            with self.subTest(action=action, relation="unrelated"):
                self.assertFalse(
                    is_allowed(
                        teacher,
                        action,
                        {"created_by": 8, "owner_teacher_id": 9},
                    )
                )

    def test_student_group_permissions(self):
        student = user(user_id=12, role="student")
        self.assertTrue(is_allowed(student, Actions.CLUSTER_LIST))
        for action in (
            Actions.CLUSTER_READ,
            Actions.CLUSTER_VM_ACTION,
            Actions.WEBSSH_CONNECT,
        ):
            with self.subTest(action=action, member=True):
                self.assertTrue(
                    is_allowed(
                        student,
                        action,
                        {"group_id": "5"},
                        student_group_ids=(4, 5),
                    )
                )
            with self.subTest(action=action, member=False):
                self.assertFalse(
                    is_allowed(
                        student,
                        action,
                        {"group_id": 6},
                        student_group_ids=(4, "5"),
                    )
                )
            with self.subTest(action=action, none_never_matches=True):
                self.assertFalse(
                    is_allowed(
                        student,
                        action,
                        {"group_id": None},
                        student_group_ids=(None,),
                    )
                )

    def test_student_own_session_and_task_boundaries(self):
        student = user(user_id=12, role="student")
        self.assertTrue(
            is_allowed(
                student,
                Actions.WEBSSH_TERMINATE_OWN,
                {"owner_user_id": "12"},
            )
        )
        self.assertFalse(
            is_allowed(
                student,
                Actions.WEBSSH_TERMINATE_OWN,
                {"owner_user_id": 13},
            )
        )
        for action in (
            Actions.TASK_READ,
            Actions.TASK_CANCEL,
            Actions.TASK_RETRY,
        ):
            with self.subTest(action=action, own=True):
                self.assertTrue(
                    is_allowed(student, action, {"created_by": "12"})
                )
            with self.subTest(action=action, own=False):
                self.assertFalse(
                    is_allowed(student, action, {"created_by": 13})
                )

    def test_student_other_actions_are_denied(self):
        student = user(user_id=12, role="student")
        denied = set(ALL_ACTIONS) - {
            Actions.CLUSTER_LIST,
            Actions.CLUSTER_READ,
            Actions.CLUSTER_VM_ACTION,
            Actions.WEBSSH_CONNECT,
            Actions.WEBSSH_TERMINATE_OWN,
            Actions.TASK_READ,
            Actions.TASK_CANCEL,
            Actions.TASK_RETRY,
        }
        for action in denied:
            with self.subTest(action=action):
                self.assertFalse(is_allowed(student, action, {}))
        self.assertFalse(
            is_allowed(student, Actions.WEBSSH_OBSERVE, {"owner_teacher_id": 12})
        )

    def test_object_resources_and_numeric_string_ids_are_supported(self):
        teacher = user(user_id="007", role="teacher")
        resource = SimpleNamespace(created_by=7)
        self.assertTrue(is_allowed(teacher, Actions.CLUSTER_READ, resource))
        self.assertFalse(
            is_allowed(
                user(user_id=None, role="teacher"),
                Actions.CLUSTER_READ,
                SimpleNamespace(created_by=None),
            )
        )

    def test_require_allowed_returns_true_or_raises_stable_exception(self):
        self.assertTrue(require_allowed(user(), Actions.PROVIDER_WRITE))
        with self.assertRaisesRegex(AuthorizationDenied, "^权限不足$"):
            require_allowed(user(role="student"), Actions.PROVIDER_WRITE)


class _CapturingLogger:
    def __init__(self):
        self.records = []

    def info(self, message):
        self.records.append((logging.INFO, message))

    def warning(self, message):
        self.records.append((logging.WARNING, message))


class AuditTests(unittest.TestCase):
    def test_sanitize_deeply_redacts_without_mutating_input(self):
        original = {
            "username": "alice",
            "PasswordHash": "hash",
            "nested": [
                {"api_token_value": "token", "safe": "visible"},
                ({"SSH_KEY_DATA": "key"}, {"CookieJar": "cookie"}),
            ],
            "authorizationHeader": "Bearer raw",
            "session_id": "session",
        }
        before = copy.deepcopy(original)

        result = sanitize(original)

        self.assertEqual(original, before)
        self.assertIsNot(result, original)
        self.assertEqual(result["PasswordHash"], "[REDACTED]")
        self.assertEqual(result["nested"][0]["api_token_value"], "[REDACTED]")
        self.assertEqual(result["nested"][0]["safe"], "visible")
        self.assertEqual(result["nested"][1][0]["SSH_KEY_DATA"], "[REDACTED]")
        self.assertEqual(result["nested"][1][1]["CookieJar"], "[REDACTED]")
        self.assertEqual(result["authorizationHeader"], "[REDACTED]")
        self.assertEqual(result["session_id"], "[REDACTED]")

    def test_security_audit_emits_parseable_json_and_minimal_actor(self):
        logger = _CapturingLogger()
        actor = {
            "id": 3,
            "username": "教师甲",
            "role": "teacher",
            "password": "must-not-leak",
            "email": "not-required@example.test",
        }
        metadata = {"safe": "保留", "private_key": "must-not-leak"}

        payload = security_audit(
            "cluster.delete",
            "success",
            actor=actor,
            resource_type="cluster",
            resource_id=9,
            reason="完成",
            metadata=metadata,
            logger=logger,
        )

        self.assertEqual(logger.records[0][0], logging.INFO)
        decoded = json.loads(logger.records[0][1])
        self.assertEqual(decoded, payload)
        self.assertEqual(set(payload["actor"]), {"id", "username", "role"})
        self.assertNotIn("password", logger.records[0][1])
        self.assertNotIn("email", logger.records[0][1])
        self.assertEqual(payload["metadata"]["private_key"], "[REDACTED]")
        self.assertIn("教师甲", logger.records[0][1])
        self.assertNotIn("\n", logger.records[0][1])

    def test_denied_failure_and_error_use_warning_other_outcomes_use_info(self):
        logger = _CapturingLogger()
        for outcome in ("denied", "failure", "error"):
            security_audit("test", outcome, logger=logger)
        for outcome in ("success", "allowed"):
            security_audit("test", outcome, logger=logger)

        self.assertEqual(
            [level for level, _ in logger.records],
            [
                logging.WARNING,
                logging.WARNING,
                logging.WARNING,
                logging.INFO,
                logging.INFO,
            ],
        )

    def test_actor_object_is_reduced_to_three_fields(self):
        logger = _CapturingLogger()
        actor = SimpleNamespace(
            id="4",
            username="bob",
            role="student",
            cookie="must-not-leak",
        )
        payload = security_audit("task.read", "denied", actor=actor, logger=logger)
        self.assertEqual(
            payload["actor"],
            {"id": "4", "username": "bob", "role": "student"},
        )
        self.assertNotIn("must-not-leak", logger.records[0][1])


if __name__ == "__main__":
    unittest.main()
