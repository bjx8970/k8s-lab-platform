"""Structured, sanitized security audit logging helpers."""

from __future__ import annotations

import json
import logging
import re
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

REDACTED = "[REDACTED]"

_PEM_BEGIN_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----", re.IGNORECASE
)
_PEM_END_RE = re.compile(
    r"-----END [^-\r\n]*PRIVATE KEY-----", re.IGNORECASE
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>(?<![\w])(?P<keyquote>[\"']?)(?P<key>[\w.-]*(?:password|passwd|secret|token_value|"
    r"authorization|private_key|ssh_key|credential|cookie|session)[\w.-]*)"
    r"(?![\w])(?P=keyquote)\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^,}\]\r\n]+)",
    re.IGNORECASE,
)
_URI_PASSWORD_RE = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^\s/@:]+):(?P<password>[^\s/@]+)@",
    re.IGNORECASE,
)


def _redact_assignment(match: re.Match[str]) -> str:
    value = match.group("value")
    if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
        replacement = value[:1] + REDACTED + value[:1]
    else:
        replacement = REDACTED
    return match.group("prefix") + replacement


class SecretTextSanitizer:
    """Incrementally redact private-key blocks from streaming log text.

    Once a private-key BEGIN marker is seen, every byte is suppressed through
    the matching END marker, including data split across arbitrary chunks.
    A replacement marker is emitted once so a log reader can tell that data
    was intentionally removed.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._in_private_key = False

    @staticmethod
    def _partial_begin_tail(value: str) -> str:
        """Keep only a possible split BEGIN marker, preserving normal output."""

        prefix = "-----BEGIN "
        lowered = value.lower()
        marker_start = lowered.rfind(prefix.lower())
        if marker_start >= 0 and "\r" not in value[marker_start:] and "\n" not in value[marker_start:]:
            return value[marker_start:]
        for size in range(min(len(prefix) - 1, len(value)), 0, -1):
            if lowered.endswith(prefix[:size].lower()):
                return value[-size:]
        return ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buffer += str(text)
        output: list[str] = []
        while self._buffer:
            if self._in_private_key:
                end = _PEM_END_RE.search(self._buffer)
                if end is None:
                    # The body is never emitted.  Retain only a possible END
                    # marker suffix so a marker split across chunks is found.
                    self._buffer = self._buffer[-48:]
                    break
                self._buffer = self._buffer[end.end():]
                self._in_private_key = False
                continue

            begin = _PEM_BEGIN_RE.search(self._buffer)
            if begin is not None:
                output.append(self._buffer[:begin.start()])
                output.append(REDACTED)
                self._buffer = self._buffer[begin.end():]
                self._in_private_key = True
                continue

            tail = self._partial_begin_tail(self._buffer)
            if tail:
                output.append(self._buffer[:-len(tail)])
                self._buffer = tail
            else:
                output.append(self._buffer)
                self._buffer = ""
            break
        return "".join(output)

    def flush(self) -> str:
        """Flush safe text and discard any incomplete/active secret block."""

        if self._in_private_key:
            self._buffer = ""
            return ""
        pending = self._buffer
        self._buffer = ""
        return pending


def sanitize_text(value: str) -> str:
    """Redact secret-bearing text, including one-shot PEM/private-key data."""

    if not isinstance(value, str):
        return value
    stream = SecretTextSanitizer()
    text = stream.feed(value) + stream.flush()
    text = _URI_PASSWORD_RE.sub(
        lambda match: match.group("prefix") + ":" + REDACTED + "@", text
    )
    return _SECRET_ASSIGNMENT_RE.sub(_redact_assignment, text)


def sanitize(value: Any) -> Any:
    """Return a recursively sanitized copy of supported container values."""

    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            lowered_key = str(key).lower()
            if lowered_key == "token_name":
                sanitized[key] = sanitize(item)
            elif any(part in lowered_key for part in SENSITIVE_KEY_PARTS):
                sanitized[key] = REDACTED
            else:
                sanitized[key] = sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item) for item in value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


_AUDIT_LOGGER = logging.getLogger("security.audit")
_AUDIT_LOGGER.setLevel(logging.INFO)
_AUDIT_LOGGER.propagate = False
if not any(getattr(handler, "_k8s_security_audit", False) for handler in _AUDIT_LOGGER.handlers):
    _audit_handler = logging.StreamHandler()
    _audit_handler.setFormatter(logging.Formatter("%(message)s"))
    _audit_handler._k8s_security_audit = True
    _AUDIT_LOGGER.addHandler(_audit_handler)


def safe_error_message(exc: BaseException | None, fallback: str = "操作失败，请联系管理员") -> str:
    """Return a fixed safe message without inspecting or stringifying *exc*."""

    del exc
    return fallback


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

    payload = sanitize({
        "event": "security_audit",
        "action": action,
        "outcome": outcome,
        "actor": _actor_payload(actor),
        "resource_type": resource_type,
        "resource_id": resource_id,
        "reason": reason,
        "metadata": sanitize(metadata),
    })
    message = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    audit_logger = (
        logger if logger is not None else _AUDIT_LOGGER
    )
    if outcome in {"denied", "failure", "error"}:
        audit_logger.warning(message)
    else:
        audit_logger.info(message)
    return payload
