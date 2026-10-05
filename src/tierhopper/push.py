"""Web Push to the installed dashboard (PWA). VAPID keys live in the Keychain / Modal Secret."""

from __future__ import annotations

import base64
import json
from typing import Any

from tierhopper import credentials
from tierhopper.redact import redact


def contact() -> str:
    """VAPID contact (required by push services): the dashboard owner's e-mail."""
    return "mailto:" + (credentials.get_secret("dashboard", "allowed_email") or "owner@example.com")


def ensure_vapid_keys() -> str:
    """Create the VAPID key pair once; returns the public key (base64url) for the browser."""
    public = credentials.get_secret("control", "vapid_public")
    if public and credentials.get_secret("control", "vapid_private"):
        return public
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
    public = base64.urlsafe_b64encode(public_raw).decode().rstrip("=")
    credentials.set_secret("control", "vapid_private", base64.urlsafe_b64encode(private_raw).decode().rstrip("="))
    credentials.set_secret("control", "vapid_public", public)
    return public


def configured() -> bool:
    return bool(credentials.get_secret("control", "vapid_private"))


def send_all(store: Any, title: str, body: str, url: str = "/") -> int:
    """Send to every subscription; drops the ones the browser has revoked. Returns how many were sent."""
    private = credentials.get_secret("control", "vapid_private")
    if not private:
        return 0
    from pywebpush import WebPushException, webpush

    payload = json.dumps({"title": redact(title)[:120], "body": redact(body)[:300], "url": url})
    sent = 0
    for sub in store.list_push_subscriptions():
        try:
            webpush(subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]}, data=payload,
                    vapid_private_key=private, vapid_claims={"sub": contact()}, ttl=3600, timeout=15)
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):  # subscription is gone
                store.delete_push_subscription(sub["endpoint"])
        except Exception:  # noqa: BLE001 - push must never break the scheduler
            continue
    return sent
