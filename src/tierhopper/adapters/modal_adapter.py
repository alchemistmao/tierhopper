"""Modal adapter.

Each attempt deploys a small app (`th-<attempt8>`) whose image comes from the job spec, spawns a
single call and stores the `FunctionCall` id, so any later process (MCP server, scheduler) can poll
it. The job code travels as a tarball argument; outputs come back as a tarball in the return value
(R2 replaces this in Phase 2). The app is stopped once the call finishes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from calendar import monthrange
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tierhopper.adapters.base import (
    AdapterError,
    AttemptPlan,
    AttemptState,
    CreditReading,
    ValidationResult,
    extract_tar,
)
from tierhopper.models import AttemptStatus, CreditSource, CreditUnit, Provider
from tierhopper.redact import redact


def _run_job(package: bytes, command: str, runner_config: str, outputs: list[str]) -> dict:
    """Executed inside the Modal container. Must stay self-contained (serialized by value)."""
    import io
    import os
    import subprocess
    import tarfile
    import time
    from collections import deque
    from pathlib import Path

    work = Path("/job")
    work.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as tar:
        tar.extractall(work, filter="data")
    started = time.time()
    tail: deque[str] = deque(maxlen=200)
    env = {**os.environ, "TH_RUNNER_CONFIG": runner_config, "HF_HOME": "/root/.cache/huggingface"}
    proc = subprocess.Popen(command, shell=True, cwd=work, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        tail.append(line.rstrip("\n"))
    code = proc.wait()
    buf = io.BytesIO()  # provider-native fallback when R2 is not configured
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in outputs:
            p = work / rel
            if p.exists():
                tar.add(p, arcname=rel.rstrip("/"))
    return {"exit_code": code, "seconds": time.time() - started, "log_tail": list(tail),
            "outputs": buf.getvalue()}


HF_CACHE_VOLUME = "tierhopper-hf-cache"


class ModalAdapter:
    id = "modal"

    # ---- credentials -------------------------------------------------------------------------
    def validate_credentials(self) -> ValidationResult:
        proc = subprocess.run([sys.executable, "-m", "modal", "token", "info"], capture_output=True, text=True)
        if proc.returncode != 0:
            return ValidationResult(False, redact(proc.stderr.strip() or "no Modal token; run `modal token new`"))
        return ValidationResult(True, "Modal token is valid")

    # ---- credit ------------------------------------------------------------------------------
    def credit_remaining(self, provider: Provider, used_usd: float = 0.0) -> CreditReading:
        """Starter has no billing API: remaining = monthly credit - usage we recorded this month."""
        total = provider.credit_model.amount
        now = datetime.now(UTC)
        days = monthrange(now.year, now.month)[1]
        resets = datetime(now.year, now.month, days, tzinfo=UTC) + timedelta(days=1)
        return CreditReading(remaining=max(total - used_usd, 0.0), unit=CreditUnit.USD,
                             source=CreditSource.ESTIMATE, total=total, resets_at=resets)

    # ---- execution ---------------------------------------------------------------------------
    def submit(self, plan: AttemptPlan) -> dict[str, Any]:
        import modal

        spec = plan.spec
        app_name = f"th-{plan.attempt_id[:8]}"
        # Serialized functions need the same Python minor version locally and remotely.
        local_python = f"{sys.version_info.major}.{sys.version_info.minor}"
        image = modal.Image.debian_slim(python_version=local_python).apt_install(
            "git", "curl", "wget", *spec.apt)
        requirements = plan.requirements
        if requirements:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
                f.write(requirements)
            image = image.pip_install_from_requirements(f.name)
        if spec.serve:
            image = image.uv_pip_install("vllm")  # baked into the image so restarts do not reinstall it
        # Modal takes a preference list: the fastest GPU first, then slower ones if it has no capacity.
        wanted = [plan.gpu.type, *plan.gpu_fallbacks]
        gpu = wanted if spec.gpu.count == 1 else [f"{g}:{spec.gpu.count}" for g in wanted]
        gpu = gpu[0] if len(gpu) == 1 else gpu
        volumes = {}
        try:  # shared Hugging Face cache, created by `tierhopper control deploy`
            cache = modal.Volume.from_name(HF_CACHE_VOLUME)
            cache.hydrate()
            volumes["/root/.cache/huggingface"] = cache
        except Exception:  # noqa: BLE001 - no cache volume yet: download weights each time
            pass

        # `_run_job` lives in this module, which does not exist in the container: ship it by value.
        from modal._vendor import cloudpickle  # the pickler Modal itself uses for serialized=True

        cloudpickle.register_pickle_by_value(sys.modules[__name__])
        app = modal.App(app_name)
        app.function(image=image, gpu=gpu, timeout=spec.timeout_minutes * 60, serialized=True, volumes=volumes,
                     name="run_job", restrict_modal_access=True, include_source=False)(_run_job)
        try:
            app.deploy(name=app_name)
            fn = modal.Function.from_name(app_name, "run_job")
            call = fn.spawn(plan.package, plan.command, json.dumps(plan.runner_config), spec.outputs)
        except Exception as e:  # noqa: BLE001 - surface any SDK error as an adapter error
            self._stop_app({"app_name": app_name})
            raise AdapterError(redact(f"Modal submit failed: {e}")) from e
        return {"app_name": app_name, "call_id": call.object_id, "gpu": gpu}

    def poll(self, ref: dict[str, Any]) -> AttemptState:
        import modal

        call = modal.FunctionCall.from_id(ref["call_id"])
        try:
            result = call.get(timeout=0)
        except TimeoutError:
            return AttemptState(AttemptStatus.RUNNING)
        except Exception as e:  # noqa: BLE001
            self._stop_app(ref)
            return AttemptState(AttemptStatus.FAILED, message=redact(str(e))[:500])
        self._stop_app(ref)
        ok = result["exit_code"] == 0
        tail = "\n".join(result["log_tail"][-20:])
        return AttemptState(AttemptStatus.SUCCEEDED if ok else AttemptStatus.FAILED,
                            message=redact(tail)[-2000:], gpu_seconds=result["seconds"], result=result)

    def fetch_outputs(self, ref: dict[str, Any], dest: Path, result: dict | None = None) -> list[Path]:
        if result is None:
            import modal

            result = modal.FunctionCall.from_id(ref["call_id"]).get(timeout=0)
        return extract_tar(result["outputs"], dest)

    def cancel(self, ref: dict[str, Any]) -> None:
        import modal

        modal.FunctionCall.from_id(ref["call_id"]).cancel()
        self._stop_app(ref)

    def max_session(self, provider: Provider) -> timedelta | None:
        return timedelta(hours=provider.max_session_hours) if provider.max_session_hours else None

    @staticmethod
    def _stop_app(ref: dict[str, Any]) -> None:
        subprocess.run([sys.executable, "-m", "modal", "app", "stop", "--yes", ref["app_name"]],
                       capture_output=True, text=True, check=False)
