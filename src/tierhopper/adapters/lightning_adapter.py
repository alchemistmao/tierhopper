"""Lightning AI adapter (lightning_sdk Jobs, image mode).

The job runs a public PyTorch image; its command downloads the job package from R2 through a
presigned URL, installs requirements and starts the TierHopper runner. Requires R2.
Credentials: `lightning login` (official, ~/.lightning/credentials.json) on the Mac; the control plane
gets LIGHTNING_USER_ID / LIGHTNING_API_KEY copied into the Keychain/Modal Secret at connect time.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
from datetime import timedelta
from pathlib import Path
from typing import Any

from tierhopper import credentials
from tierhopper.adapters.base import (
    RUNNER_FILE,
    AdapterError,
    AttemptPlan,
    AttemptState,
    CreditReading,
    ValidationResult,
)
from tierhopper.models import AttemptStatus, CreditSource, CreditUnit, Provider
from tierhopper.redact import redact

IMAGE = "pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime"
# Slim images ship without git/curl; jobs commonly need them to fetch code or weights.
SYSTEM_TOOLS = ("(command -v git >/dev/null && command -v curl >/dev/null) || "
                "(apt-get update -qq && apt-get install -y -qq git curl wget >/dev/null)")
MACHINES = {"T4": "T4", "L4": "L4", "L40S": "L40S", "A100-40GB": "A100_40GB", "A100-80GB": "A100_80GB",
            "H100": "H100"}
_STATUS = {
    "NotCreated": AttemptStatus.SUBMITTED,
    "Pending": AttemptStatus.QUEUED,
    "Running": AttemptStatus.RUNNING,
    "Stopping": AttemptStatus.RUNNING,
    "Completed": AttemptStatus.SUCCEEDED,
    "Failed": AttemptStatus.FAILED,
    "Stopped": AttemptStatus.CANCELLED,
}


def _auth() -> None:
    """Prefer the official login file; fall back to Keychain/env values (control plane)."""
    creds_file = Path.home() / ".lightning" / "credentials.json"
    if creds_file.is_file() and not os.environ.get("LIGHTNING_API_KEY"):
        return
    user_id = credentials.get_secret("lightning", "user_id")
    api_key = credentials.get_secret("lightning", "api_key")
    if user_id and api_key:
        os.environ["LIGHTNING_USER_ID"] = user_id
        os.environ["LIGHTNING_API_KEY"] = api_key


def _teamspace() -> str:
    ts = credentials.get_secret("lightning", "teamspace")
    if not ts:
        raise AdapterError("Lightning teamspace unknown; run `tierhopper connect lightning`")
    return ts


def discover_teamspace() -> str:
    """owner/teamspace of the logged-in user: the first teamspace the account belongs to."""
    _auth()
    from lightning_sdk.utils.resolve import _get_authed_user, _get_teamspace_names_for_authed_user

    user = _get_authed_user()
    names = _get_teamspace_names_for_authed_user()
    if not names:
        raise AdapterError("no Lightning teamspace found for this account")
    first = names[0]
    return first if "/" in first else f"{user.name}/{first}"


def import_login_credentials() -> bool:
    """Copy user_id/api_key from the official login file into the Keychain (for the control plane)."""
    creds_file = Path.home() / ".lightning" / "credentials.json"
    if not creds_file.is_file():
        return False
    data = json.loads(creds_file.read_text())
    if not (data.get("user_id") and data.get("api_key")):
        return False
    credentials.set_secret("lightning", "user_id", data["user_id"])
    credentials.set_secret("lightning", "api_key", data["api_key"])
    return True


class LightningAdapter:
    id = "lightning"

    def validate_credentials(self) -> ValidationResult:
        try:
            _auth()
            from lightning_sdk.utils.resolve import _get_authed_user

            user = _get_authed_user()
        except Exception as e:  # noqa: BLE001
            return ValidationResult(False, redact(f"Lightning auth failed: {e}"))
        return ValidationResult(True, f"Lightning login valid for {user.name}")

    def credit_remaining(self, provider: Provider, used_usd: float = 0.0) -> CreditReading:
        total = provider.credit_model.amount
        return CreditReading(max(total - used_usd, 0.0), CreditUnit.USD, CreditSource.ESTIMATE, total=total)

    def submit(self, plan: AttemptPlan) -> dict[str, Any]:
        if not plan.package_url:
            raise AdapterError("Lightning needs R2 (the job downloads its package by URL)")
        machine = MACHINES.get(plan.gpu.type)
        if machine is None:
            raise AdapterError(f"unsupported Lightning GPU: {plan.gpu.type}")
        _auth()
        from lightning_sdk import Job, Machine

        name = f"th-{plan.attempt_id[:8]}"
        fetch = ("import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/pkg.tgz')")
        steps = [
            SYSTEM_TOOLS,
            f"python -c {shlex.quote(fetch)} \"$TH_PACKAGE_URL\"",
            "mkdir -p /job && tar xzf /tmp/pkg.tgz -C /job && cd /job",
        ]
        if plan.spec.requirements:
            steps.append(f"pip install -q -r {shlex.quote(plan.spec.requirements)}")
        steps.append(f"python {RUNNER_FILE}")
        env = {"TH_PACKAGE_URL": plan.package_url, "TH_RUNNER_CONFIG": json.dumps(plan.runner_config)}
        try:
            job = Job.run(name=name, machine=getattr(Machine, machine), image=IMAGE, command=" && ".join(steps),
                          teamspace=_teamspace(), env=env, tags=["tierhopper"])
        except Exception as e:  # noqa: BLE001
            raise AdapterError(redact(f"Lightning submit failed: {e}")) from e
        return {"job": name, "teamspace": _teamspace(), "url": getattr(job, "link", None)}

    def _job(self, ref: dict[str, Any]):
        _auth()
        from lightning_sdk import Job

        return Job(ref["job"], teamspace=ref["teamspace"])

    def poll(self, ref: dict[str, Any]) -> AttemptState:
        try:
            job = self._job(ref)
        except ValueError:
            return AttemptState(AttemptStatus.LOST, message="job no longer exists on Lightning")
        raw = getattr(job.status, "value", str(job.status))
        status = _STATUS.get(raw, AttemptStatus.RUNNING)
        seconds = None
        if status.terminal and job.started_at and job.stopped_at:
            seconds = (job.stopped_at - job.started_at).total_seconds()
        message = None
        if status == AttemptStatus.FAILED:
            try:
                logs = job.logs(tail=40)
                text = logs if isinstance(logs, str) else "\n".join(str(line) for line in logs)
                message = redact(text)[-2000:]
            except Exception:  # noqa: BLE001
                message = "failed (logs unavailable)"
        billed = None
        if status.terminal:
            with contextlib.suppress(Exception):
                billed = float(job.total_cost) if job.total_cost is not None else None
        return AttemptState(status, message=message, gpu_seconds=seconds, billed_usd=billed)

    def fetch_outputs(self, ref: dict[str, Any], dest: Path, result: dict | None = None) -> list[Path]:
        return []  # outputs always travel through R2 for Lightning

    def cancel(self, ref: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self._job(ref).stop()

    def max_session(self, provider: Provider) -> timedelta | None:
        return timedelta(hours=provider.max_session_hours) if provider.max_session_hours else None
