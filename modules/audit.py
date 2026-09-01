"""Structured, sanitized security audit logging helpers."""

from __future__ import annotations

import json
import logging
from typing import Any


SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "private_key",
    "ssh_key",
    "credential",
    "authorization",
    "cookie",
    "session",
)


def sanitize(value: Any) -> Any:
    """Return a recursively sanitized copy of supported container values."""

    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            lowered_key = str(key).lower()
            if any(part in lowered_key for part in SENSITIVE_KEY_PARTS):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item) for item in value)
    return value


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _actor_payload(actor: Any) -> Any:
    if actor is None:
        return None
    return sanitize(
        {
            "id": _field(actor, "id"),
            "username": _field(actor, "username"),
            "role": _field(actor, "role"),
        }
    )


def security_audit(
    action: str,
    outcome: str,
    *,
    actor: Any = None,
    resource_type: Any = None,
    resource_id: Any = None,
    reason: Any = None,
    metadata: Any = None,
    logger: Any = None,
) -> dict[str, Any]:
    """Emit one sanitized JSON security-audit record and return its payload."""

    payload = {
        "event": "security_audit",
        "action": action,
        "outcome": outcome,
        "actor": _actor_payload(actor),
        "resource_type": resource_type,
        "resource_id": resource_id,
        "reason": reason,
        "metadata": sanitize(metadata),
    }
    message = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    audit_logger = (
        logger if logger is not None else logging.getLogger("security.audit")
    )
    if outcome in {"denied", "failure", "error"}:
        audit_logger.warning(message)
    else:
        audit_logger.info(message)
    return payload
