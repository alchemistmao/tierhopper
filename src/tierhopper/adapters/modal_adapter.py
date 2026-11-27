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

from tierhopper.adapters import modal_entry
from tierhopper.adapters.base import (
    AdapterError,
    AttemptPlan,
    AttemptState,
    CreditReading,
    ValidationResult,
    extract_tar,
)
from tierhopper.adapters.modal_entry import _run_job
from tierhopper.models import AttemptStatus, CreditSource, CreditUnit, Provider
from tierhopper.redact import redact


def _transient(e: Exception) -> bool:
    """Errors talking to Modal (auth, network, rate limits), as opposed to the job itself failing."""
    import re

    import modal.exception as mx

    kinds = tuple(k for k in (getattr(mx, n, None) for n in ("AuthError", "ConnectionError", "TimeoutError"))
                  if isinstance(k, type))
    if isinstance(e, kinds + (OSError,)) or type(e).__module__.split(".")[0] in ("grpclib", "h2"):
        return True
    return bool(re.search(r"status = '(401|403|429|5\d\d)'|UNAVAILABLE|DEADLINE_EXCEEDED|connection", str(e), re.I))


ENTRY_MODULE = "th_modal_entry"


def _entry_as_module():
    """`_run_job` loaded as a top-level module, the name under which the container imports the copied file."""
    import importlib.util

    if ENTRY_MODULE not in sys.modules:
        found = importlib.util.spec_from_file_location(ENTRY_MODULE, modal_entry.__file__)
        module = importlib.util.module_from_spec(found)
        sys.modules[ENTRY_MODULE] = module
        found.loader.exec_module(module)
    return sys.modules[ENTRY_MODULE]._run_job


def credit_left(summary: dict[str, Any]) -> float:
    """Free credit left according to a Modal billing summary (`modal billing summary --json`).
    metered_cost is everything used; adjustments are negative numbers: the free credit and free allowances."""
    adjustments = {k: float(v) for k, v in (summary.get("adjustments") or {}).items()}
    credit = -adjustments.pop("credits")
    used = float(summary["metered_cost"]) + sum(v for v in adjustments.values() if v < 0)
    return round(credit - used, 4)


def official_credit_left() -> float:
    proc = subprocess.run([sys.executable, "-m", "modal", "billing", "summary", "--json"], capture_output=True,
                          text=True, timeout=45, check=True)
    return credit_left(json.loads(proc.stdout[proc.stdout.index("{"):]))


HF_CACHE_VOLUME = "tierhopper-hf-cache"
CUDA_DEVEL_IMAGE = "nvidia/cuda:12.8.1-devel-ubuntu22.04"


class ModalAdapter:
    id = "modal"
    # Modal normally hands over a GPU in seconds: longer than this means we are in its queue.
    start_deadline = timedelta(minutes=5)

    # ---- credentials -------------------------------------------------------------------------
    def validate_credentials(self) -> ValidationResult:
        import os

        if os.environ.get("MODAL_TASK_ID"):  # inside Modal (the cloud scheduler): no token file, ask the API
            try:
                import modal

                modal.Volume.objects.create(HF_CACHE_VOLUME, allow_existing=True)
            except Exception as e:  # noqa: BLE001
                return ValidationResult(False, redact(f"Modal API refused the scheduler: {e}"))
            return ValidationResult(True, "Modal API reachable from the scheduler")
        proc = subprocess.run([sys.executable, "-m", "modal", "token", "info"], capture_output=True, text=True)
        if proc.returncode != 0:
            return ValidationResult(False, redact(proc.stderr.strip() or "no Modal token; run `modal token new`"))
        return ValidationResult(True, "Modal token is valid")

    # ---- credit ------------------------------------------------------------------------------
    def credit_remaining(self, provider: Provider, used_usd: float = 0.0) -> CreditReading:
        """Modal's own billing number for this month: free credit minus what was really metered. It can go
        below zero: that is money billed on the card. Only if Modal does not answer, our own estimate."""
        total = provider.credit_model.amount
        now = datetime.now(UTC)
        days = monthrange(now.year, now.month)[1]
        resets = datetime(now.year, now.month, days, tzinfo=UTC) + timedelta(days=1)
        try:
            return CreditReading(remaining=official_credit_left(), unit=CreditUnit.USD, source=CreditSource.API,
                                 total=total, resets_at=resets)
        except Exception:  # noqa: BLE001 - no answer (e.g. from inside the cloud scheduler)
            return CreditReading(remaining=max(total - used_usd, 0.0), unit=CreditUnit.USD,
                                 source=CreditSource.ESTIMATE, total=total, resets_at=resets)

    # ---- execution ---------------------------------------------------------------------------
    def submit(self, plan: AttemptPlan) -> dict[str, Any]:
        import modal

        spec = plan.spec
        app_name = f"th-{plan.attempt_id[:8]}"
        # Serialized functions need the same Python minor version locally and remotely.
        local_python = f"{sys.version_info.major}.{sys.version_info.minor}"
        entry, own_image = _run_job, bool(spec.image)
        if own_image:
            # A ready image from a public registry, with its own Python and packages. Modal builds any extra
            # layer on its side (no Docker on the user's machine). The entry travels as a file, not a pickle,
            # so the image's Python does not have to match the local one.
            entry = _entry_as_module()
            image = modal.Image.from_registry(spec.image)
            if spec.apt:
                image = image.apt_install(*spec.apt)
        else:
            if spec.cuda_compiler:  # only when the job asks: the CUDA devel image is several GB larger
                base = modal.Image.from_registry(CUDA_DEVEL_IMAGE, add_python=local_python)
            else:
                base = modal.Image.debian_slim(python_version=local_python)
            image = base.apt_install("git", "curl", "wget", *spec.apt)
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

        if own_image:
            image = image.add_local_file(modal_entry.__file__, f"/root/{ENTRY_MODULE}.py")  # last layer
        else:
            # `_run_job` lives in a module that does not exist in the container: ship it by value.
            from modal._vendor import cloudpickle  # the pickler Modal itself uses for serialized=True

            cloudpickle.register_pickle_by_value(modal_entry)
        app = modal.App(app_name)
        app.function(image=image, gpu=gpu, timeout=spec.timeout_minutes * 60, serialized=not own_image,
                     volumes=volumes, name="run_job", restrict_modal_access=True, include_source=False)(entry)
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
            if _transient(e):  # we could not ask Modal; the job itself may be fine: ask again next pass
                return AttemptState(AttemptStatus.RUNNING, message=redact(f"Modal did not answer: {e}")[:300])
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
