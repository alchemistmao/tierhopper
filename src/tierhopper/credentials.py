"""Credential storage in the system keychain (macOS Keychain, Windows Credential Manager, Secret Service).

Where there is no keychain (headless Linux, containers) secrets go to a private file,
`~/.tierhopper/credentials.json` (mode 600, outside any repository). Environment variables
`TIERHOPPER_<PROVIDER>_<FIELD>` are read last.

Secrets are never logged or returned through MCP. Values are registered with the redactor
as soon as they are read so they cannot leak into logs or notifications.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from tierhopper import redact

SERVICE = "tierhopper"


def _account(provider: str, field: str) -> str:
    return f"{provider}:{field}"


def _env_name(provider: str, field: str) -> str:
    return f"TIERHOPPER_{provider}_{field}".upper().replace("-", "_")


def _file() -> Path:
    return Path(os.environ.get("TIERHOPPER_HOME") or Path.home() / ".tierhopper").expanduser() / "credentials.json"


def _file_read() -> dict[str, str]:
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}


def _file_write(data: dict[str, str]) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.chmod(0o600)
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _no_keychain() -> bool:
    try:
        name = type(keyring.get_keyring()).__module__
    except Exception:  # noqa: BLE001
        return True
    return name.endswith((".fail", ".null"))


def backend() -> str:
    """Where secrets are kept on this machine, for `tierhopper doctor`."""
    return f"private file {_file()}" if _no_keychain() else "system keychain"


def set_secret(provider: str, field: str, value: str) -> None:
    if not value:
        raise ValueError("empty secret")
    try:
        if _no_keychain():
            raise KeyringError("no keychain")
        keyring.set_password(SERVICE, _account(provider, field), value)
    except KeyringError:  # no keychain on this machine
        _file_write(_file_read() | {_account(provider, field): value})
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
        value = _file_read().get(_account(provider, field))
    if value is None:
        value = os.environ.get(_env_name(provider, field))
    if value and field not in NOT_SECRET:
        redact.register(value)
    return value


def delete_secret(provider: str, field: str) -> None:
    with contextlib.suppress(PasswordDeleteError, KeyringError):
        keyring.delete_password(SERVICE, _account(provider, field))
    data = _file_read()
    if data.pop(_account(provider, field), None) is not None:
        _file_write(data)


def has_secret(provider: str, field: str) -> bool:
    return get_secret(provider, field) is not None
