"""Credential storage in the macOS Keychain (via `keyring`), with `.env` as a fallback.

Secrets are never logged or returned through MCP. Values are registered with the redactor
as soon as they are read so they cannot leak into logs or notifications.
"""

from __future__ import annotations

import contextlib
import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from tierhopper import redact

SERVICE = "tierhopper"


def _account(provider: str, field: str) -> str:
    return f"{provider}:{field}"


def _env_name(provider: str, field: str) -> str:
    return f"TIERHOPPER_{provider}_{field}".upper().replace("-", "_")


def set_secret(provider: str, field: str, value: str) -> None:
    if not value:
        raise ValueError("empty secret")
    keyring.set_password(SERVICE, _account(provider, field), value)
    if field not in NOT_SECRET:
        redact.register(value)


# Stored alongside secrets for convenience, but not sensitive: never mask these in logs.
NOT_SECRET = {"bucket", "teamspace", "username", "account_id", "user_id", "ingest_url", "action_url", "url",
              "publishable_key", "allowed_email"}


def get_secret(provider: str, field: str) -> str | None:
    """Keychain first; environment (`TIERHOPPER_<PROVIDER>_<FIELD>`, e.g. from a Modal Secret) second."""
    try:
        value = keyring.get_password(SERVICE, _account(provider, field))
    except KeyringError:  # no keyring backend (Linux containers)
        value = None
    if value is None:
        value = os.environ.get(_env_name(provider, field))
    if value and field not in NOT_SECRET:
        redact.register(value)
    return value


def delete_secret(provider: str, field: str) -> None:
    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(SERVICE, _account(provider, field))


def has_secret(provider: str, field: str) -> bool:
    return get_secret(provider, field) is not None
