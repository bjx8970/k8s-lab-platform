"""Pure authorization helpers for security-sensitive platform actions."""

from __future__ import annotations

from typing import Any, Iterable


class Actions:
    PROVIDER_READ = "provider.read"
    PROVIDER_WRITE = "provider.write"
    CLUSTER_LIST = "cluster.list"
    CLUSTER_READ = "cluster.read"
    CLUSTER_CREATE = "cluster.create"
    CLUSTER_DELETE = "cluster.delete"
    CLUSTER_DEPLOY = "cluster.deploy"
    CLUSTER_VM_ACTION = "cluster.vm_action"
    WEBSSH_CONNECT = "webssh.connect"
    WEBSSH_TERMINATE_OWN = "webssh.terminate_own"
    WEBSSH_OBSERVE = "webssh.observe"
    TASK_READ = "task.read"
    TASK_CANCEL = "task.cancel"
    TASK_RETRY = "task.retry"
    ADMIN_COMPENSATE = "admin.compensate"


_KNOWN_ACTIONS = frozenset(
    value
    for name, value in vars(Actions).items()
    if name.isupper() and isinstance(value, str)
)

_TEACHER_UNSCOPED_ACTIONS = frozenset(
    {
        Actions.PROVIDER_READ,
        Actions.CLUSTER_LIST,
        Actions.CLUSTER_CREATE,
    }
)
_TEACHER_CLUSTER_OWNER_ACTIONS = frozenset(
    {
        Actions.CLUSTER_READ,
        Actions.CLUSTER_DELETE,
        Actions.CLUSTER_DEPLOY,
        Actions.CLUSTER_VM_ACTION,
        Actions.WEBSSH_CONNECT,
    }
)
_TASK_ACTIONS = frozenset(
    {
        Actions.TASK_READ,
        Actions.TASK_CANCEL,
        Actions.TASK_RETRY,
    }
)
_STUDENT_GROUP_ACTIONS = frozenset(
    {
        Actions.CLUSTER_READ,
        Actions.CLUSTER_VM_ACTION,
        Actions.WEBSSH_CONNECT,
    }
)


class AuthorizationDenied(PermissionError):
    """Raised when a user is not authorized to perform an action."""


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _normalized_id(value: Any) -> tuple[str, Any]:
    if isinstance(value, bool):
        return ("value", value)
    if isinstance(value, int):
        return ("number", value)
    if isinstance(value, str):
        candidate = value.strip()
        if candidate and candidate.lstrip("+-").isdigit():
            return ("number", int(candidate))
    return ("value", value)


def _ids_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    return _normalized_id(left) == _normalized_id(right)


def _matches_any_id(value: Any, candidates: Iterable[Any]) -> bool:
    return any(_ids_equal(value, candidate) for candidate in candidates)


def is_allowed(
    user: Any,
    action: str,
    resource: Any = None,
    *,
    student_group_ids: Iterable[Any] = (),
) -> bool:
    """Return whether ``user`` may perform ``action`` on ``resource``."""

    if user is None or action not in _KNOWN_ACTIONS:
        return False
    if not _field(user, "is_authenticated", False):
        return False
    if not _field(user, "is_active", False):
        return False

    role = _field(user, "role")
    user_id = _field(user, "id")

    if role == "admin":
        return True

    if role == "teacher":
        if action in _TEACHER_UNSCOPED_ACTIONS:
            return True
        if action in _TEACHER_CLUSTER_OWNER_ACTIONS:
            return _ids_equal(_field(resource, "created_by"), user_id)
        if action == Actions.WEBSSH_TERMINATE_OWN:
            return _ids_equal(_field(resource, "owner_user_id"), user_id)
        if action == Actions.WEBSSH_OBSERVE:
            return _ids_equal(_field(resource, "owner_teacher_id"), user_id)
        if action in _TASK_ACTIONS:
            return _ids_equal(
                _field(resource, "created_by"), user_id
            ) or _ids_equal(_field(resource, "owner_teacher_id"), user_id)
        return False

    if role == "student":
        if action == Actions.CLUSTER_LIST:
            return True
        if action in _STUDENT_GROUP_ACTIONS:
            return _matches_any_id(
                _field(resource, "group_id"), student_group_ids
            )
        if action == Actions.WEBSSH_TERMINATE_OWN:
            return _ids_equal(_field(resource, "owner_user_id"), user_id)
        if action in _TASK_ACTIONS:
            return _ids_equal(_field(resource, "created_by"), user_id)
        return False

    return False


def require_allowed(
    user: Any,
    action: str,
    resource: Any = None,
    *,
    student_group_ids: Iterable[Any] = (),
) -> bool:
    """Require authorization, raising a stable exception on denial."""

    if is_allowed(
        user,
        action,
        resource,
        student_group_ids=student_group_ids,
    ):
        return True
    raise AuthorizationDenied("权限不足")
