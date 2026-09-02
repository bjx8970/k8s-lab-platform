"""Flask-independent, fail-closed authorization at the service boundary."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from modules.audit import security_audit
from modules.authz import Actions, AuthorizationDenied, require_allowed
from modules.db import get_class, get_group, get_student_group_ids, get_user, load_cluster


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _positive_id(value: Any) -> int:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        try:
            return int(value)
        except ValueError:
            pass
    raise ValueError("资源编号必须是正整数")


def _same_id(left: Any, right: Any) -> bool:
    try:
        return _positive_id(left) == _positive_id(right)
    except ValueError:
        return False


def _actor_id(actor: Any) -> Any:
    return actor if isinstance(actor, (int, str)) else _field(actor, "id")


def _audit(action: str, outcome: str, *, actor_id: Any = None,
           resource_type: str | None = None, resource_id: Any = None,
           reason: str | None = None) -> None:
    try:
        actor_id = _positive_id(actor_id)
    except ValueError:
        actor_id = None
    security_audit(
        action + ".authorize", outcome,
        actor={"id": actor_id} if actor_id is not None else None,
        resource_type=resource_type, resource_id=resource_id, reason=reason,
    )


def _fresh_actor(actor_id: Any):
    try:
        actor_id = _positive_id(actor_id)
    except ValueError:
        raise AuthorizationDenied("权限不足") from None
    actor = get_user(actor_id)
    if actor is None or not _same_id(_field(actor, "id"), actor_id):
        raise AuthorizationDenied("权限不足")
    # Every supported role has CLUSTER_LIST.  This rejects disabled,
    # unauthenticated and unknown-role actors before loading secret resources.
    require_allowed(actor, Actions.CLUSTER_LIST)
    return actor


def _group_ids(group_ids: Any) -> list[int]:
    if isinstance(group_ids, (str, bytes)) or not isinstance(group_ids, Sequence):
        raise ValueError("分组编号必须是列表")
    return [_positive_id(group_id) for group_id in group_ids]


def validate_cluster_creation(actor: Any, group_ids=(), class_id=None) -> None:
    """Validate every supplied group/class before the caller enqueues work."""
    actor_id = _actor_id(actor)
    try:
        if isinstance(actor, (int, str)):
            actor = _fresh_actor(actor_id)
        require_allowed(actor, Actions.CLUSTER_CREATE)
        if not _same_id(actor_id, actor_id):
            raise AuthorizationDenied("权限不足")

        groups = _group_ids(group_ids)
        class_id = _positive_id(class_id) if class_id is not None else None
        classes = {}

        def checked_class(value):
            value = _positive_id(value)
            if value not in classes:
                classes[value] = get_class(value)
            school_class = classes[value]
            if school_class is None or not _same_id(_field(school_class, "id"), value):
                raise ValueError("课程不存在")
            return school_class

        school_class = checked_class(class_id) if class_id is not None else None
        associations = []
        for group_id in groups:
            group = get_group(group_id)
            if group is None or not _same_id(_field(group, "id"), group_id):
                raise ValueError("分组不存在")
            actual_class_id = _positive_id(_field(group, "class_id"))
            if class_id is not None and actual_class_id != class_id:
                raise ValueError("分组与课程不匹配")
            associations.append((group, checked_class(actual_class_id)))

        if _field(actor, "role") == "teacher":
            if school_class is not None and not _same_id(_field(school_class, "created_by"), actor_id):
                raise AuthorizationDenied("权限不足")
            for group, actual_class in associations:
                if not _same_id(_field(group, "created_by"), actor_id):
                    raise AuthorizationDenied("权限不足")
                if not _same_id(_field(actual_class, "created_by"), actor_id):
                    raise AuthorizationDenied("权限不足")
    except AuthorizationDenied:
        _audit(Actions.CLUSTER_CREATE, "denied", actor_id=actor_id,
               reason="权限不足")
        raise
    except ValueError:
        _audit(Actions.CLUSTER_CREATE, "denied", actor_id=actor_id,
               reason="资源关联校验失败")
        raise
    except Exception:
        _audit(Actions.CLUSTER_CREATE, "failure", actor_id=actor_id,
               reason="授权检查失败")
        raise
    _audit(Actions.CLUSTER_CREATE, "success", actor_id=actor_id)


def authorize_cluster_action(actor_id: Any, action: str, cluster_name: str):
    """Reload actor/resource and authorize the exact cluster being operated on."""
    try:
        actor = _fresh_actor(actor_id)
        cluster = load_cluster(cluster_name)
        if cluster is None:
            raise ValueError("集群不存在")
        groups = get_student_group_ids(_field(actor, "id")) if _field(actor, "role") == "student" else ()
        require_allowed(actor, action, cluster, student_group_ids=groups)
    except (AuthorizationDenied, ValueError):
        _audit(action, "denied", actor_id=actor_id, resource_type="cluster",
               resource_id=cluster_name, reason="权限不足或资源不存在")
        raise
    except Exception:
        _audit(action, "failure", actor_id=actor_id, resource_type="cluster",
               resource_id=cluster_name, reason="授权检查失败")
        raise
    _audit(action, "success", actor_id=actor_id,
           resource_type="cluster", resource_id=cluster_name)
    return actor, cluster


def reload_actor(actor_id: Any):
    """Reload and reject missing, inactive or unsupported actors."""
    try:
        return _fresh_actor(actor_id)
    except AuthorizationDenied:
        _audit("actor.reload", "denied", actor_id=actor_id, reason="权限不足")
        raise
    except Exception:
        _audit("actor.reload", "failure", actor_id=actor_id, reason="授权检查失败")
        raise


def authorize_task(actor: Any, action: str, task: Any):
    actor_id = _actor_id(actor)
    try:
        fresh_actor = _fresh_actor(actor_id)
        require_allowed(fresh_actor, action, task)
    except AuthorizationDenied:
        _audit(action, "denied", actor_id=actor_id, resource_type="task",
               resource_id=_field(task, "task_id"), reason="权限不足")
        raise
    except Exception:
        _audit(action, "failure", actor_id=actor_id, resource_type="task",
               resource_id=_field(task, "task_id"), reason="授权检查失败")
        raise
    _audit(action, "success", actor_id=actor_id, resource_type="task",
           resource_id=_field(task, "task_id"))
    return fresh_actor
