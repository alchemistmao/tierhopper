"""Redaction of secrets from any text that leaves the process (logs, events, notifications)."""

from __future__ import annotations

import re
import threading

MASK = "[REDACTED]"

_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)(\s*[:=]\s*|\s+)(['\"]?)[^\s'\",]{8,}"),
    re.compile(r"\b(ak|as|sk|pk|rk|rpa|KGAT)[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    re.compile(r"(?i)(X-Amz-Signature|X-Amz-Credential|Signature)=[^&\s]+"),  # presigned URLs
]

_known: set[str] = set()
_lock = threading.Lock()


def register(secret: str) -> None:
    """Remember a concrete secret value so it is masked wherever it appears."""
    if secret and len(secret) >= 6:
        with _lock:
            _known.add(secret)


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        known = sorted(_known, key=len, reverse=True)
    for value in known:
        text = text.replace(value, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(_mask_match, text)
    return text


def _mask_match(m: re.Match) -> str:
    if m.re is _PATTERNS[0]:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}"
    if m.re is _PATTERNS[3]:
        return f"{m.group(1)}={MASK}"
    return MASK
