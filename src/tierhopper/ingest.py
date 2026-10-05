"""Per-attempt ingest tokens: jobs authenticate heartbeats without holding any real credential."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from tierhopper import credentials


def ingest_secret() -> str | None:
    return credentials.get_secret("control", "ingest_secret")


def ensure_ingest_secret() -> str:
    secret = ingest_secret()
    if not secret:
        secret = secrets.token_hex(32)
        credentials.set_secret("control", "ingest_secret", secret)
    return secret


def token_for(attempt_id: str, secret: str) -> str:
    return hmac.new(secret.encode(), attempt_id.encode(), hashlib.sha256).hexdigest()


def verify(attempt_id: str, token: str, secret: str) -> bool:
    return bool(attempt_id and token) and hmac.compare_digest(token_for(attempt_id, secret), token)
