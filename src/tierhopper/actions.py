"""Signed action links for WhatsApp commands (status, pause, approve, deny, report).

A link is `<action_url>?t=<token>`; the token carries {action, job, exp} and an HMAC. Opening the link
(GET) only renders a confirmation page; the action runs on the page's POST button, so link previews
can never approve anything. Tokens expire after 30 minutes (status/report: 24 h).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time

from tierhopper import credentials

ACTIONS = {"status", "pause", "resume", "approve", "deny", "report"}
TTL = {"status": 86400, "report": 86400}
DEFAULT_TTL = 1800


def secret() -> str | None:
    return credentials.get_secret("control", "action_secret")


def ensure_secret() -> str:
    value = secret()
    if not value:
        value = secrets.token_hex(32)
        credentials.set_secret("control", "action_secret", value)
    return value


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(action: str, job_id: str | None, key: str, now: float | None = None) -> str:
    if action not in ACTIONS:
        raise ValueError(action)
    payload = {"a": action, "j": job_id, "e": int((now or time.time()) + TTL.get(action, DEFAULT_TTL))}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()[:18])
    return f"{body}.{sig}"


def read_token(token: str, key: str, now: float | None = None) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()[:18])
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("a") not in ACTIONS or payload.get("e", 0) < (now or time.time()):
        return None
    return payload


def link(action: str, job_id: str | None = None) -> str | None:
    """Full link, or None when the control plane action endpoint is not deployed yet."""
    base, key = credentials.get_secret("control", "action_url"), secret()
    if not (base and key):
        return None
    return f"{base}?t={make_token(action, job_id, key)}"


LABELS = {"approve": "Approve", "deny": "Decline", "pause": "Pause", "resume": "Resume",
          "status": "Refresh", "report": "Refresh"}

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TierHopper</title>
<style>
:root{{--bg:#07090d;--panel:#0d1117;--line:#1c2330;--text:#e6edf3;--muted:#8b95a5;
--brand:#5ee0ff}}
@media (prefers-color-scheme:light){{:root{{--bg:#f7f7f5;--panel:#fff;--line:#e7e5e4;
--text:#1c1917;--muted:#57534e;--brand:#0e7490}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif;
padding:24px 16px}}
main{{max-width:520px;margin:0 auto;background:var(--panel);border:1px solid var(--line);
border-radius:12px;padding:22px}}
h1{{font-size:18px;margin:0 0 6px}} p{{color:var(--muted);margin:0 0 16px}}
pre{{white-space:pre-wrap;font:13px/1.5 ui-monospace,monospace;background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:12px;overflow:auto}}
button{{width:100%;padding:14px;border:0;border-radius:10px;background:var(--brand);color:#031318;font-weight:600;
font-size:16px;cursor:pointer}}
</style></head><body><main><h1>{title}</h1><p>{subtitle}</p>{body}</main></body></html>"""


def page(title: str, subtitle: str, body: str = "") -> str:
    return PAGE.format(title=html.escape(title), subtitle=html.escape(subtitle), body=body)


def confirm_form(token: str, action: str) -> str:
    return (f'<form method="post"><input type="hidden" name="t" value="{html.escape(token)}">'
            f'<button type="submit">{html.escape(LABELS.get(action, action.title()))}</button></form>')


def pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"
