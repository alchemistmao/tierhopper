"""WhatsApp notifications through CallMeBot (outbound only).

Commands travel as signed action links (see actions.py) that open a confirmation page on the control
plane. Messages never include secrets, logs or presigned URLs; every text goes through the redactor.
"""

from __future__ import annotations

import contextlib
import urllib.parse
import urllib.request
from typing import Any

from tierhopper import actions, credentials
from tierhopper.models import Job, Provider
from tierhopper.redact import redact

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
MAX_LEN = 1200


def configured() -> bool:
    return bool(credentials.get_secret("callmebot", "phone") and credentials.get_secret("callmebot", "apikey"))


class WhatsApp:
    def __init__(self, sender=None) -> None:
        self._sender = sender or self._callmebot

    @staticmethod
    def _callmebot(text: str) -> None:
        phone = credentials.get_secret("callmebot", "phone")
        apikey = credentials.get_secret("callmebot", "apikey")
        if not (phone and apikey):
            raise RuntimeError("CallMeBot not configured; run `tierhopper config whatsapp`")
        query = urllib.parse.urlencode({"phone": phone, "text": text, "apikey": apikey})
        with urllib.request.urlopen(f"{CALLMEBOT_URL}?{query}", timeout=30) as resp:
            body = resp.read().decode(errors="replace")
        if resp.status != 200 or "ERROR" in body.upper():
            raise RuntimeError(f"CallMeBot rejected the message (HTTP {resp.status})")

    def send(self, text: str) -> bool:
        try:
            self._sender(redact(text)[:MAX_LEN])
            return True
        except Exception:  # noqa: BLE001 - notifications must never break the scheduler
            return False

    # ---- message templates ---------------------------------------------------------------------
    def approval_needed(self, job: Job, reason: str, paid: dict[str, Any] | None) -> bool:
        approve = actions.link("approve", job.id)
        deny = actions.link("deny", job.id)
        head = "🟠 TierHopper · Needs your OK"
        what = f"{job.name}: {reason}"
        lines = [head, what]
        if approve:
            lines += ["", f"✅ Approve: {approve}", f"✋ {'Wait for free credit' if paid else 'Cancel'}: {deny}"]
        else:
            lines += ["", f"Approve on your Mac: tierhopper approve {job.id}"]
        return self.send("\n".join(lines))

    def job_done(self, job: Job) -> bool:
        return self.send(f"🟢 TierHopper · Done\n{job.name} finished. Results: tierhopper fetch {job.id}")

    def job_failed(self, job: Job, message: str) -> bool:
        last = message.strip().splitlines()[-1][:200] if message.strip() else "see job_status"
        pause_status = actions.link("status", job.id)
        extra = f"\nDetails: {pause_status}" if pause_status else ""
        return self.send(f"🔴 TierHopper · Stopped\n{job.name} stopped after repeated errors.\n"
                         f"Last line: {last}{extra}")

    def card(self, provider: Provider) -> bool:
        return self.send(integration_card(provider))

    def weekly(self, report: dict[str, Any]) -> bool:
        return self.send(weekly_text(report))


def integration_card(p: Provider) -> str:
    """The onboarding card: what you get, sign-up link, 3-5 steps and the final command."""
    steps = [f"Create your account: {p.signup_url}"]
    manual = []
    if p.requirements.phone:
        manual.append("verify your phone")
    if p.requirements.card:
        manual.append("add a card (no charge unless you approve)" if p.kind != "paid" else "add a card")
    steps.append("Accept the terms" + (f" and {' and '.join(manual)}" if manual else "") + " yourself")
    if p.login_command:
        steps.append(f"On your computer, the connect command opens `{p.login_command}` in the browser")
    elif p.api_key_url:
        steps.append(f"Create an API key: {p.api_key_url}")
    steps.append(f"Run: tierhopper connect {p.id}")
    gift = _what_you_get(p)
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    return f"🟢 TierHopper · New GPU source: {p.name}\nWhat you get: {gift}\n{numbered}"


def _what_you_get(p: Provider) -> str:
    gpus = ", ".join(sorted({g.type for g in p.gpu_catalog})) or "GPUs"
    cm = p.credit_model
    if cm.type == "monthly_usd":
        credit = f"${cm.amount:g}/month free"
    elif cm.type == "weekly_hours":
        credit = f"~{cm.amount:g} GPU-hours/week free"
    elif cm.type == "one_time_usd":
        credit = f"${cm.amount:g} free credit"
    else:
        credit = "pay as you go (only with your OK)"
    return f"{credit} on {gpus}"


def weekly_text(report: dict[str, Any]) -> str:
    t = report["totals"]
    lines = [f"📊 TierHopper · Week of {report['since'][:10]}",
             f"GPU time: {t['gpu_hours']:.1f} h · Saved: ${t['saved_usd']:.2f} · Spent: ${t['spent_usd']:.2f}",
             f"Jobs: {t['jobs_done']} done, {t['jobs_failed']} stopped, {t['hops']} provider switches"]
    for p in report["providers"]:
        if p["gpu_hours"] or p.get("credit_left") is not None:
            left = f" · left {p['credit_left']}" if p.get("credit_left") is not None else ""
            lines.append(f"• {p['name']}: {p['gpu_hours']:.1f} h{left}")
    return "\n".join(lines)


class Notifier:
    """Fans notifications out to every configured channel (WhatsApp, Web Push)."""

    def __init__(self, store: Any = None, whatsapp: WhatsApp | None = None, push_sender=None) -> None:
        from tierhopper import push

        self.store = store
        self.whatsapp = whatsapp if whatsapp is not None else (WhatsApp() if configured() else None)
        self._push = push_sender or (push.send_all if push.configured() else None)

    def _push_all(self, title: str, body: str, url: str) -> None:
        if self._push and self.store is not None:
            with contextlib.suppress(Exception):  # e.g. the subscriptions table is not there yet
                self._push(self.store, title, body, url)

    def approval_needed(self, job: Job, reason: str, paid: dict[str, Any] | None) -> None:
        if self.whatsapp:
            self.whatsapp.approval_needed(job, reason, paid)
        self._push_all("Needs your OK", f"{job.name}: {reason}", f"/jobs/{job.id}")

    def job_done(self, job: Job) -> None:
        if self.whatsapp:
            self.whatsapp.job_done(job)
        self._push_all("Done", f"{job.name} finished. Results are ready.", f"/jobs/{job.id}")

    def job_failed(self, job: Job, message: str) -> None:
        if self.whatsapp:
            self.whatsapp.job_failed(job, message)
        self._push_all("Stopped", f"{job.name} stopped after repeated errors.", f"/jobs/{job.id}")
