"""Server-side encryption helpers for credentials.

The application deliberately has no fallback credential key.  A missing or
invalid key must stop the operation instead of returning a value that might be
plaintext from an older database.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


ENCRYPTED_PREFIX = "enc:v1:"
CREDENTIAL_ERROR_MESSAGE = "凭据处理失败，请检查 K8S_LAB_CREDENTIAL_KEY 或执行迁移"


class CredentialError(ValueError):
    """Safe, user-facing credential failure without secret-bearing details."""


def _credential_error() -> CredentialError:
    return CredentialError(CREDENTIAL_ERROR_MESSAGE)


def _fernet() -> Fernet:
    raw_key = os.environ.get("K8S_LAB_CREDENTIAL_KEY", "")
    if not raw_key:
        raise _credential_error()
    try:
        return Fernet(raw_key.encode("ascii"))
    except Exception:
        # Do not expose the parser error: it can contain configuration data.
        raise _credential_error() from None


def encrypt_secret(value: str | None) -> str | None:
    """Encrypt a credential with the configured Fernet key.

    Empty values remain empty because an optional credential is not a secret
    that needs a ciphertext.  The version prefix also makes this operation
    safe for migration paths that may see an already encrypted value.
    """

    if value is None or value == "":
        return value
    if not isinstance(value, str):
        raise _credential_error()
    if value.startswith(ENCRYPTED_PREFIX):
        # Authenticate before retaining an existing ciphertext.  This catches
        # a missing/wrong key or damaged value without double encryption.
        decrypt_secret(value)
        return value
    try:
        token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    except CredentialError:
        raise
    except Exception:
        raise _credential_error() from None
    return ENCRYPTED_PREFIX + token


def decrypt_secret(value: str | None) -> str | None:
    """Decrypt a stored credential, failing closed for legacy plaintext."""

    if value is None or value == "":
        return value
    if not isinstance(value, str) or not value.startswith(ENCRYPTED_PREFIX):
        raise _credential_error()
    try:
        token = value[len(ENCRYPTED_PREFIX):].encode("ascii")
        return _fernet().decrypt(token).decode("utf-8")
    except CredentialError:
        raise
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError):
        raise _credential_error() from None
    except Exception:
        raise _credential_error() from None
