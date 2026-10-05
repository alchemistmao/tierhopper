"""RunPod adapter (paid, last resort, only after explicit approval).

An on-demand pod runs a public PyTorch image. Its start command downloads the job package from R2
through a presigned URL and runs the TierHopper runner, then idles: RunPod never restarts the job by
itself, and the scheduler terminates the pod as soon as the runner reports its exit (heartbeat).
The monthly spend cap is enforced before any paid attempt is even proposed.
"""

from __future__ import annotations

import json
import shlex
import urllib.error
import urllib.request
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
from tierhopper.adapters.lightning_adapter import SYSTEM_TOOLS
from tierhopper.models import AttemptStatus, CreditSource, CreditUnit, Provider
from tierhopper.redact import redact

REST = "https://rest.runpod.io/v1"
GRAPHQL = "https://api.runpod.io/graphql"
IMAGE = "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
GPU_IDS = {"RTX-4090": "NVIDIA GeForce RTX 4090", "A40": "NVIDIA A40", "A100-80GB": "NVIDIA A100 80GB PCIe",
           "L4": "NVIDIA L4", "L40S": "NVIDIA L40S", "H100": "NVIDIA H100 80GB HBM3"}


def _key() -> str:
    key = credentials.get_secret("runpod", "api_key")
    if not key:
        raise AdapterError("RunPod API key missing; run `tierhopper connect runpod`")
    return key


def _call(method: str, url: str, body: dict | None = None) -> Any:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json",
                                          "User-Agent": "tierhopper/0.1 (+https://github.com/alchemistmao/tierhopper)",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise AdapterError(redact(f"RunPod {method} failed: HTTP {e.code} {e.read()[:300]!r}")) from e
    return json.loads(raw) if raw else None


class RunPodAdapter:
    id = "runpod"
    terminates_on_exit = False  # the pod idles after the runner exits; the scheduler terminates it

    def validate_credentials(self) -> ValidationResult:
        try:
            balance = self._balance()
        except Exception as e:  # noqa: BLE001
            return ValidationResult(False, redact(f"RunPod auth failed: {e}"))
        return ValidationResult(True, f"RunPod key valid · balance ${balance:.2f}")

    def _balance(self) -> float:
        data = _call("POST", GRAPHQL, {"query": "query { myself { clientBalance } }"})
        return float(data["data"]["myself"]["clientBalance"])

    def credit_remaining(self, provider: Provider, used_usd: float = 0.0) -> CreditReading:
        cap_left = max(provider.spend_cap_usd - used_usd, 0.0)
        try:
            remaining, source = min(self._balance(), cap_left), CreditSource.API
        except Exception:  # noqa: BLE001
            remaining, source = cap_left, CreditSource.ESTIMATE
        return CreditReading(remaining, CreditUnit.USD, source, total=provider.spend_cap_usd)

    def submit(self, plan: AttemptPlan) -> dict[str, Any]:
        if not plan.package_url:
            raise AdapterError("RunPod needs R2 (the job downloads its package by URL)")
        gpu_id = GPU_IDS.get(plan.gpu.type)
        if gpu_id is None:
            raise AdapterError(f"unsupported RunPod GPU: {plan.gpu.type}")
        fetch = "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/pkg.tgz')"
        steps = [SYSTEM_TOOLS, f"python -c {shlex.quote(fetch)} \"$TH_PACKAGE_URL\"",
                 "mkdir -p /job && tar xzf /tmp/pkg.tgz -C /job && cd /job"]
        if plan.spec.requirements:
            steps.append(f"pip install -q -r {shlex.quote(plan.spec.requirements)}")
        steps.append(f"python {RUNNER_FILE}")
        script = " && ".join(steps) + "; echo '[tierhopper] runner finished'; sleep infinity"
        body = {
            "name": f"th-{plan.attempt_id[:8]}",
            "imageName": IMAGE,
            "computeType": "GPU",
            "gpuTypeIds": [gpu_id],
            "gpuCount": plan.spec.gpu.count,
            "containerDiskInGb": 40,
            "cloudType": "SECURE",
            "interruptible": False,
            "env": {"TH_PACKAGE_URL": plan.package_url, "TH_RUNNER_CONFIG": json.dumps(plan.runner_config)},
            "dockerEntrypoint": ["bash", "-c"],
            "dockerStartCmd": [script],
        }
        pod = _call("POST", f"{REST}/pods", body)
        return {"pod_id": pod["id"], "cost_per_hr": pod.get("costPerHr")}

    def poll(self, ref: dict[str, Any]) -> AttemptState:
        try:
            pod = _call("GET", f"{REST}/pods/{ref['pod_id']}")
        except AdapterError as e:
            if "404" in str(e):
                return AttemptState(AttemptStatus.LOST, message="pod no longer exists")
            raise
        status = (pod or {}).get("desiredStatus", "RUNNING")
        if status == "TERMINATED":
            return AttemptState(AttemptStatus.LOST, message=pod.get("lastStatusChange"))
        if status == "EXITED":
            return AttemptState(AttemptStatus.FAILED, message=pod.get("lastStatusChange"))
        return AttemptState(AttemptStatus.RUNNING)

    def fetch_outputs(self, ref: dict[str, Any], dest: Path, result: dict | None = None) -> list[Path]:
        return []  # outputs always travel through R2

    def cancel(self, ref: dict[str, Any]) -> None:
        try:
            _call("DELETE", f"{REST}/pods/{ref['pod_id']}")
        except AdapterError as e:
            if "404" not in str(e):
                raise

    def max_session(self, provider: Provider) -> timedelta | None:
        return None
