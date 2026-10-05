"""Application service: the operations behind the MCP tools, the CLI and the control plane."""

from __future__ import annotations

import contextlib
import glob
import json
import os
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from tierhopper import config, credentials, ingest
from tierhopper.adapters import IMPLEMENTED, AdapterError, ProviderAdapter, get_adapter
from tierhopper.adapters.base import AttemptPlan, AttemptState, build_package, extract_tar
from tierhopper.models import (
    Attempt,
    AttemptStatus,
    CreditSnapshot,
    CreditSource,
    CreditUnit,
    EndReason,
    Event,
    Job,
    JobStatus,
    Provider,
    ProviderKind,
    ProviderStatus,
    Shard,
    now,
)
from tierhopper.progress import parse_counter
from tierhopper.redact import redact
from tierhopper.registry import sync_registry
from tierhopper.routing import Candidate, plan_route, speed_of
from tierhopper.spec import JobSpec, load_spec
from tierhopper.stats import ProviderStats, compute_stats, estimate_t4_hours
from tierhopper.storage_r2 import Keys
from tierhopper.store.base import NotFound, Store

SMOKE_SPEC = Path(__file__).resolve().parent / "smoke"
MODAL_OVERHEAD = 1.10  # CPU/memory billed on top of GPU time
SESSION_MARGIN = timedelta(minutes=15)  # stop before a provider's session limit to save a checkpoint
MAX_HOPS = 10
MAX_ERRORS = 2
APPROVAL_GPU_HOURS = 12.0  # ask before a job estimated above this many GPU-hours (env override)
CREDIT_WORDS = ("quota", "credit", "billing", "payment method", "insufficient funds", "spend limit")

# How an attempt ended -> (end reason, counts as a failure, exclude this provider for the shard)
HOP_RULES = {
    EndReason.SESSION_LIMIT: (False, False),
    EndReason.PREEMPTED: (False, False),
    EndReason.CREDIT_EXHAUSTED: (False, True),
    EndReason.ERROR: (True, True),
}


def match_gpu(name: str, vram_total_mb: Any, catalog: list) -> str | None:
    """Catalog type for what nvidia-smi reports (e.g. "NVIDIA A100-SXM4-40GB" -> "A100-40GB")."""
    text = name.upper().replace(" ", "")
    if not text:
        return None
    gb = float(vram_total_mb) / 1024 if isinstance(vram_total_mb, int | float) else None
    best = None
    for offer in sorted(catalog, key=lambda g: -len(g.type)):
        family = offer.type.upper().split("-")[0]
        if family in text and (gb is None or abs(offer.vram_gb - gb) <= offer.vram_gb * 0.15):
            best = offer.type
            break
    return best


def shard_items(spec: JobSpec, root: Path) -> list[list[str]]:
    """Split the work: `shard.over` (glob, relative to the job root) or `shard.count` parts."""
    if spec.shard is None:
        return [[]]
    if spec.shard.count:
        return [[] for _ in range(spec.shard.count)]
    files = sorted(os.path.relpath(p, root) for p in glob.glob(str(root / spec.shard.over), recursive=True)
                   if os.path.isfile(p))
    if not files:
        raise ValueError(f"shard.over matched no files: {spec.shard.over}")
    parts = min(len(files), max(spec.shard.max_parallel * 2, 1))
    return [files[i::parts] for i in range(parts)]


class Blobs(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def exists(self, key: str) -> bool: ...
    def presign_get(self, key: str, ttl: timedelta) -> str: ...
    def presign_put(self, key: str, ttl: timedelta) -> str: ...


def default_blobs() -> Blobs | None:
    from tierhopper import storage_r2

    return storage_r2.R2() if storage_r2.configured() else None


class TierHopper:
    def __init__(self, store: Store, adapter_factory: Callable[[str], ProviderAdapter] = get_adapter,
                 results_dir: Path | None = None, blobs: Blobs | None | str = "auto",
                 packages_dir: Path | None = None, sync: bool = True, notifier: Any = None) -> None:
        results_dir = results_dir or config.results_dir()
        packages_dir = packages_dir or config.packages_dir()
        self.store = store
        self.notifier = notifier
        self._stats: tuple[float, dict[str, ProviderStats]] | None = None
        self.adapter_factory = adapter_factory
        self.results_dir = results_dir
        self.packages_dir = packages_dir
        self.blobs: Blobs | None = default_blobs() if blobs == "auto" else blobs  # type: ignore[assignment]
        self._adapters: dict[str, ProviderAdapter] = {}
        if sync:
            sync_registry(store)

    def adapter(self, provider: Provider) -> ProviderAdapter:
        if provider.adapter not in self._adapters:
            self._adapters[provider.adapter] = self.adapter_factory(provider.adapter)
        return self._adapters[provider.adapter]

    # ---- credits -------------------------------------------------------------------------------
    def month_usage_usd(self, provider_id: str) -> float:
        start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return self.store.usage_since(provider_id, start)[1]

    def month_spent_usd(self, provider_id: str) -> float:
        start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return sum(a.cost_usd for a in self.store.list_attempts_since(start) if a.provider_id == provider_id)

    def refresh_credit(self, provider: Provider) -> CreditSnapshot:
        if provider.kind == ProviderKind.PAID:
            used = self.month_spent_usd(provider.id)
        elif provider.credit_model.type == "one_time_usd":  # never resets: count everything ever used
            used = self.store.usage_since(provider.id, datetime(2020, 1, 1, tzinfo=UTC))[1]
        else:
            used = self.month_usage_usd(provider.id) * (MODAL_OVERHEAD if provider.id == "modal" else 1)
        reading = self.adapter(provider).credit_remaining(provider, used_usd=used)
        snap = CreditSnapshot(provider_id=provider.id, remaining=reading.remaining, unit=reading.unit,
                              source=reading.source, expires_at=reading.resets_at)
        return self.store.add_credit_snapshot(snap)

    def refresh_stale_credits(self, max_age: timedelta = timedelta(minutes=10)) -> int:
        """Keep the credit numbers current while jobs run (the dashboard reads the latest snapshot)."""
        refreshed = 0
        for p in self.store.list_providers():
            if p.status != ProviderStatus.ACTIVE or p.adapter not in IMPLEMENTED:
                continue
            last = self.store.latest_credit(p.id)
            if last is not None and now() - last.measured_at < max_age:
                continue
            with contextlib.suppress(Exception):  # a provider API hiccup must not stop the scheduler
                self.refresh_credit(p)
                refreshed += 1
        return refreshed

    def credits_status(self, refresh: bool = True) -> list[dict[str, Any]]:
        rows = []
        for p in self.store.list_providers():
            row: dict[str, Any] = {"provider": p.id, "name": p.name, "kind": p.kind.value,
                                   "status": p.status.value}
            if p.status == ProviderStatus.ACTIVE and p.adapter in IMPLEMENTED:
                try:
                    snap = self.refresh_credit(p) if refresh else self.store.latest_credit(p.id)
                except Exception as e:  # noqa: BLE001 - report, never crash the status call
                    row["error"] = redact(str(e))[:300]
                    snap = self.store.latest_credit(p.id)
                if snap:
                    row.update(remaining=round(snap.remaining, 2), unit=snap.unit.value,
                               source=snap.source.value, total=p.credit_model.amount,
                               resets_at=snap.expires_at.isoformat() if snap.expires_at else None)
            else:
                row["how_to_connect"] = f"tierhopper connect {p.id}"
            if p.spend_cap_usd:
                row["spend_cap_usd"] = p.spend_cap_usd
                row["spent_this_month_usd"] = round(self.month_spent_usd(p.id), 2)
            rows.append(row)
        return rows

    def _routing_credits(self, providers: list[Provider], refresh: bool) -> dict[str, CreditSnapshot | None]:
        credits: dict[str, CreditSnapshot | None] = {}
        for p in providers:
            if p.status != ProviderStatus.ACTIVE or p.adapter not in IMPLEMENTED:
                credits[p.id] = None
                continue
            try:
                credits[p.id] = self.refresh_credit(p) if refresh else self.store.latest_credit(p.id)
            except Exception:  # noqa: BLE001 - fall back to the last known value
                credits[p.id] = self.store.latest_credit(p.id)
        return credits

    # ---- submit --------------------------------------------------------------------------------
    def submit_job(self, spec_path: str, dry_run: bool = False) -> dict[str, Any]:
        spec, root = load_spec(spec_path)
        providers = self.store.list_providers()
        stats = self.stats()
        t4_hours, source = self._t4_hours(spec, stats)
        candidates, rejected = plan_route(spec, providers, self._routing_credits(providers, refresh=True),
                                          stats=stats, busy=self._busy(), t4_hours=t4_hours)
        plan = [{"provider": c.provider.id, "gpu": c.gpu.type, "score": c.score} for c in candidates]
        why_not = {r.provider_id: r.reason for r in rejected}
        items = shard_items(spec, root)
        estimate = self._estimate(candidates[0] if candidates else None, stats, t4_hours, source)
        reason = self._approval_reason(estimate)
        base = {"plan": plan, "not_used": why_not, "estimate": estimate, "shards": len(items),
                "checkpoints": self.blobs is not None}
        if dry_run:
            return {"dry_run": True, "needs_approval": reason, **base}

        package = build_package(root)
        project = self.store.get_or_create_project(spec.project)
        job = Job(project_id=project.id, name=spec.name, spec=spec.model_dump() | {"_root": str(root)},
                  spec_hash=spec.spec_hash(), estimate=estimate)
        self._save_package(job, package)
        self.store.create_job(job)
        for idx, chunk in enumerate(items):
            self.store.create_shard(Shard(job_id=job.id, idx=idx, total=len(items), items=chunk))
        self._event(job, "submitted", plan=plan, shards=len(items))
        if reason:
            self._request_approval(job, reason)
        else:
            self._fill_parallel(job)
        job = self.store.get_job(job.id)
        running = [a for s in self.store.list_shards(job.id) for a in self.store.list_attempts(s.id)]
        return {"job_id": job.id, "status": job.status.value, "needs_approval": reason,
                "approve_command": f"tierhopper approve {job.id}" if reason else None,
                "providers": sorted({a.provider_id for a in running}), **base}

    def _t4_hours(self, spec: JobSpec, stats: dict) -> tuple[float | None, str]:
        """The job's size in T4-equivalent GPU-hours: history first, then the spec's `estimate_hours`."""
        hours = estimate_t4_hours(self.store, spec.spec_hash(), stats)
        if hours is not None:
            return hours, "history"
        return (spec.estimate_hours, "spec") if spec.estimate_hours else (None, "unknown")

    @staticmethod
    def _estimate(best: Candidate | None, stats: dict, t4_hours: float | None, source: str) -> dict[str, Any]:
        if t4_hours is None or best is None:
            return {"gpu_hours": None, "market_usd": None, "source": "unknown", "t4_hours": t4_hours,
                    "gpu": best.gpu.type if best else None}
        speed = speed_of(best.gpu, stats.get(best.provider.id))
        hours = t4_hours / speed
        return {"gpu_hours": round(hours, 3), "market_usd": round(hours * best.gpu.usd_per_hour, 2),
                "source": source, "gpu": best.gpu.type, "t4_hours": round(t4_hours, 3),
                "speed_vs_t4": round(speed, 2)}

    @staticmethod
    def _approval_reason(estimate: dict[str, Any]) -> str | None:
        limit = float(os.environ.get("TIERHOPPER_APPROVAL_GPU_HOURS", APPROVAL_GPU_HOURS))
        hours = estimate.get("t4_hours")  # job size in T4-equivalent hours: independent of the GPU chosen
        if hours is not None and hours > limit:
            return f"estimated {hours:g} GPU-hours of work (limit {limit:g})"
        return None

    def _request_approval(self, job: Job, reason: str, paid: dict[str, Any] | None = None) -> None:
        job.status = JobStatus.AWAITING_APPROVAL
        job.estimate["approval"] = {"reason": reason, "requested_at": now().isoformat(), "paid": paid}
        self.store.update_job(job)
        self._event(job, "approval_requested", reason=reason, paid=paid)
        if self.notifier:
            self.notifier.approval_needed(job, reason, paid)

    def approve(self, job_id: str, allow_paid: bool = False, channel: str = "cli") -> dict[str, Any]:
        """Approve a job waiting for approval (over the GPU-hours limit and/or a paid provider)."""
        job = self.store.get_job(job_id)
        if job.status != JobStatus.AWAITING_APPROVAL:
            return {"job_id": job.id, "status": job.status.value, "note": "nothing to approve"}
        request = job.estimate.get("approval") or {}
        job.estimate["approval"] = {**request, "approved_at": now().isoformat(), "channel": channel}
        if request.get("paid") or allow_paid:
            job.spec["_allow_paid"] = True
        job.status = JobStatus.QUEUED
        self.store.update_job(job)
        self._event(job, "approved", channel=channel, paid=bool(job.spec.get("_allow_paid")))
        self._fill_parallel(job)
        job = self.store.get_job(job_id)
        return {"job_id": job.id, "status": job.status.value}

    def deny(self, job_id: str, channel: str = "cli") -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job.status == JobStatus.AWAITING_APPROVAL:
            request = job.estimate.get("approval") or {}
            if request.get("paid"):  # "wait for free credit": back in line, free providers only
                job.status = JobStatus.QUEUED
                job.estimate["approval"] = {**request, "denied_at": now().isoformat()}
            else:
                job.status, job.finished_at = JobStatus.CANCELLED, now()
            self.store.update_job(job)
            self._event(job, "denied", channel=channel)
        return {"job_id": job.id, "status": job.status.value}

    def pause(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            return {"job_id": job.id, "status": job.status.value}
        for shard in self.store.list_shards(job.id):
            for a in self.store.list_attempts(shard.id):
                if not a.status.terminal:
                    self.adapter(self.store.get_provider(a.provider_id)).cancel(a.external_ref)
                    a.status, a.end_reason, a.ended_at = AttemptStatus.CANCELLED, EndReason.CANCELLED, now()
                    self.store.update_attempt(a)
            if shard.status == JobStatus.RUNNING:
                shard.status = JobStatus.QUEUED
                self.store.update_shard(shard)
        job.status = JobStatus.PAUSED
        self.store.update_job(job)
        self._event(job, "paused")
        return {"job_id": job.id, "status": job.status.value}

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Stop a job for good: running attempts are stopped on the provider; saved progress is kept in R2."""
        job = self.store.get_job(job_id)
        if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            return {"job_id": job.id, "status": job.status.value}
        self.pause(job_id)
        job = self.store.get_job(job_id)
        job.status, job.finished_at = JobStatus.CANCELLED, now()
        self.store.update_job(job)
        self._event(job, "cancelled")
        return {"job_id": job.id, "status": job.status.value}

    def resume(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job.status == JobStatus.PAUSED:
            job.status = JobStatus.QUEUED
            self.store.update_job(job)
            self._event(job, "resumed")
            self._fill_parallel(job)
        return {"job_id": job.id, "status": self.store.get_job(job_id).status.value}

    # ---- packages ------------------------------------------------------------------------------
    def _save_package(self, job: Job, package: bytes) -> None:
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        (self.packages_dir / f"{job.id}.tar.gz").write_bytes(package)
        if self.blobs:
            key = Keys(job.id).package
            self.blobs.put(key, package)
            job.spec["_package"] = key

    def _load_package(self, job: Job) -> bytes:
        local = self.packages_dir / f"{job.id}.tar.gz"
        if local.is_file():
            return local.read_bytes()
        if self.blobs and job.spec.get("_package"):
            data = self.blobs.get(job.spec["_package"])
            if data:
                return data
        root = Path(job.spec.get("_root", ""))
        if root.is_dir():
            return build_package(root)
        raise AdapterError("job package not found (no local copy and no R2 copy)")

    @staticmethod
    def _spec(job: Job) -> JobSpec:
        return JobSpec.model_validate({k: v for k, v in job.spec.items() if not k.startswith("_")})

    # ---- dispatch ------------------------------------------------------------------------------
    def stats(self) -> dict[str, ProviderStats]:
        if self._stats is None or time.monotonic() - self._stats[0] > 300:
            self._stats = (time.monotonic(), compute_stats(self.store))
        return self._stats[1]

    def _busy(self) -> dict[str, int]:
        busy: dict[str, int] = {}
        for a in self.store.list_active_attempts():
            busy[a.provider_id] = busy.get(a.provider_id, 0) + 1
        return busy

    def _fill_parallel(self, job: Job) -> int:
        """Dispatch queued shards of a job up to its max_parallel."""
        spec = self._spec(job)
        limit = spec.shard.max_parallel if spec.shard else 1
        shards = self.store.list_shards(job.id)
        running = sum(1 for s in shards if s.status == JobStatus.RUNNING)
        started = 0
        for shard in shards:
            if running >= limit:
                break
            if shard.status != JobStatus.QUEUED:
                continue
            job = self.store.get_job(job.id)
            if job.status in (JobStatus.AWAITING_APPROVAL, JobStatus.PAUSED, JobStatus.CANCELLED):
                break
            if self._dispatch(job, shard, self._excluded(shard)) is None:
                break  # nothing available right now; the scheduler retries on the next tick
            running += 1
            started += 1
        return started

    def _dispatch(self, job: Job, shard: Shard, exclude: set[str] | None = None) -> Attempt | None:
        exclude = set(exclude or ())
        # Re-read: a pause/cancel or another scheduler may have acted since the caller loaded the job.
        current = self.store.get_job(job.id)
        if current.status in (JobStatus.PAUSED, JobStatus.CANCELLED, JobStatus.DONE, JobStatus.FAILED,
                              JobStatus.AWAITING_APPROVAL):
            job.status = current.status
            return None
        if any(not a.status.terminal for a in self.store.list_attempts(shard.id)):
            return None  # this part is already running somewhere
        spec = self._spec(job)
        providers = self.store.list_providers()
        credits = {p.id: self.store.latest_credit(p.id) for p in providers}
        allow_paid = bool(job.spec.get("_allow_paid"))
        left = (job.estimate.get("t4_hours") or 0) * (1 - shard.progress) or None  # what is still to do
        candidates, _ = plan_route(spec, providers, credits, exclude=exclude, allow_paid=allow_paid,
                                   stats=self.stats(), busy=self._busy(), t4_hours=left)
        package = None
        for cand in candidates:
            attempt = Attempt(shard_id=shard.id, provider_id=cand.provider.id, gpu_type=cand.gpu.type,
                              progress_start=shard.progress)
            try:
                package = package or self._load_package(job)
                plan = AttemptPlan(attempt_id=attempt.id, job_id=job.id, spec=spec, package=package, gpu=cand.gpu,
                                   runner_config=self._runner_config(job, shard, attempt, cand.provider, spec),
                                   package_url=self._package_url(job),
                                   gpu_fallbacks=[g.type for g in cand.alternatives])
                attempt.external_ref = self.adapter(cand.provider).submit(plan)
            except AdapterError as e:
                exclude.add(cand.provider.id)
                self._event(job, "submit_failed", provider=cand.provider.id, error=str(e)[:500])
                continue
            self.store.create_attempt(attempt)
            shard.status = JobStatus.RUNNING
            self.store.update_shard(shard)
            job.status = JobStatus.RUNNING
            job.started_at = job.started_at or now()
            self.store.update_job(job)
            self._event(job, "attempt_started", attempt=attempt, provider=cand.provider.id, gpu=cand.gpu.type,
                        resume=shard.checkpoint_key is not None)
            return attempt
        if not allow_paid and self._ask_for_paid(job, shard, spec, providers, credits, exclude):
            return None
        shard.status = JobStatus.QUEUED
        self.store.update_shard(shard)
        others_running = any(s.status == JobStatus.RUNNING for s in self.store.list_shards(job.id))
        if job.status not in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.AWAITING_APPROVAL) \
                and not others_running:
            job.status = JobStatus.QUEUED
            self.store.update_job(job)
        self._event(job, "waiting", tried=sorted(exclude))
        return None

    def _ask_for_paid(self, job: Job, shard: Shard, spec: JobSpec, providers: list[Provider],
                      credits: dict, exclude: set[str]) -> bool:
        """No free provider can take the shard: ask to continue on a paid one (never automatic)."""
        if job.status == JobStatus.AWAITING_APPROVAL or (job.estimate.get("approval") or {}).get("denied_at"):
            return False
        paid, _ = plan_route(spec, providers, credits, exclude=exclude, allow_paid=True, busy=self._busy())
        paid = [c for c in paid if c.provider.kind == ProviderKind.PAID]
        if not paid:
            return False
        best = paid[0]
        t4_left = (job.estimate.get("t4_hours") or 1.0) * (1 - shard.progress)
        hours = t4_left / speed_of(best.gpu, self.stats().get(best.provider.id))
        cost = round(hours * best.gpu.usd_per_hour, 2)
        spent = self.month_spent_usd(best.provider.id)
        if spent + cost > best.provider.spend_cap_usd:
            self._event(job, "spend_cap", provider=best.provider.id, spent=spent, cap=best.provider.spend_cap_usd)
            return False
        shard.status = JobStatus.QUEUED
        self.store.update_shard(shard)
        self._request_approval(job, f"free credit ran out; continue on {best.provider.name} for about ${cost:.2f}?",
                               paid={"provider": best.provider.id, "gpu": best.gpu.type, "usd": cost})
        return True

    def _package_url(self, job: Job) -> str | None:
        if not (self.blobs and job.spec.get("_package")):
            return None
        return self.blobs.presign_get(job.spec["_package"], timedelta(hours=26))

    def _runner_config(self, job: Job, shard: Shard, attempt: Attempt, provider: Provider,
                       spec: JobSpec) -> dict[str, Any]:
        budget = timedelta(minutes=spec.timeout_minutes)
        if provider.max_session_hours:
            budget = min(budget, timedelta(hours=provider.max_session_hours))
        deadline = now() + budget - min(SESSION_MARGIN, budget / 4)
        cfg: dict[str, Any] = {
            "attempt_id": attempt.id,
            "entrypoint": spec.entrypoint,
            "outputs": spec.outputs,
            "progress_file": spec.progress_file,
            "checkpoint_dir": spec.checkpoint.dir if spec.checkpoint else None,
            "checkpoint_every_s": (spec.checkpoint.every_minutes * 60) if spec.checkpoint else 600,
            "deadline_ts": deadline.timestamp(),
            "env": {"TH_JOB_ID": job.id, "TH_SHARD_INDEX": str(shard.idx), "TH_SHARD_COUNT": str(shard.total)},
            "items": shard.items,
            "serve": spec.serve.model_dump() if spec.serve else None,
            "urls": {},
        }
        if self.blobs:
            keys, ttl = Keys(job.id), budget + timedelta(hours=2)
            urls = {"results_put": self.blobs.presign_put(keys.results(shard.idx), ttl),
                    "log_put": self.blobs.presign_put(Keys.log(attempt.id), ttl)}
            if spec.checkpoint:
                urls["ckpt_get"] = self.blobs.presign_get(keys.checkpoint(shard.idx), ttl)
                urls["ckpt_put"] = self.blobs.presign_put(keys.checkpoint(shard.idx), ttl)
            cfg["urls"] = urls
        ingest_url, secret = credentials.get_secret("control", "ingest_url"), ingest.ingest_secret()
        if ingest_url and secret:
            cfg["ingest_url"] = ingest_url
            cfg["ingest_token"] = ingest.token_for(attempt.id, secret)
        return cfg

    # ---- heartbeats (control-plane ingest endpoint) --------------------------------------------
    def handle_heartbeat(self, body: dict[str, Any], token: str) -> dict[str, Any]:
        secret = ingest.ingest_secret()
        attempt_id = str(body.get("attempt_id") or "")
        if not secret or not ingest.verify(attempt_id, token, secret):
            return {"ok": False, "error": "unauthorized"}
        try:
            attempt = self.store.get_attempt(attempt_id)
        except NotFound:
            return {"ok": False, "error": "unknown attempt"}
        if attempt.status.terminal:
            return {"ok": True, "ignored": True}
        shard = self.store.get_shard(attempt.shard_id)
        job = self.store.get_job(shard.job_id)
        kind = body.get("kind")
        progress = body.get("progress")
        counter = parse_counter([str(x) for x in (body.get("lines") or body.get("tail") or [])])
        previous = (job.estimate.get("counters") or {}).get(str(shard.idx))
        if counter and counter["unit"] == "items" and (
                job.spec.get("progress_file") or (previous and previous["unit"] != "items")):
            counter = None  # a download/install bar, not the job's work: the job reports its own progress
        if counter:  # "37 of 150 pages", read from the job's own output
            counters = dict(job.estimate.get("counters") or {})
            counters[str(shard.idx)] = counter
            job.estimate["counters"] = counters
            if not isinstance(progress, int | float):
                progress = counter["done"] / counter["total"]
        if isinstance(progress, int | float):
            body = {**body, "progress": progress}
        self._record_telemetry(attempt.id, body)
        if isinstance(progress, int | float):
            attempt.progress_end = float(progress)
            shard.progress = max(shard.progress, float(progress))
        actual = match_gpu(str(body.get("gpu_name") or ""), body.get("vram_total_mb"),
                           self.store.get_provider(attempt.provider_id).gpu_catalog)
        if actual and actual != attempt.gpu_type and attempt.gpu_type not in ("T4x2", "L4x2"):
            attempt.gpu_type = actual  # the provider gave a different GPU from our preference list
        if kind == "started" and attempt.status != AttemptStatus.RUNNING:
            attempt.status = AttemptStatus.RUNNING
            self._event(job, "attempt_running", attempt=attempt, resumed=bool(body.get("resumed")))
        elif kind == "checkpoint":
            shard.checkpoint_key = Keys(job.id).checkpoint(shard.idx)
            shard.checkpoint_at = now()
            self._event(job, "checkpoint", attempt=attempt, bytes=body.get("checkpoint_bytes"),
                        progress=shard.progress)
        elif kind == "exit":
            reason = str(body.get("reason") or "")
            if reason in EndReason._value2member_map_:
                attempt.end_reason = EndReason(reason)
            if isinstance(body.get("seconds"), int | float):
                attempt.gpu_seconds = float(body["seconds"])
            tail = body.get("tail") or []
            attempt.message = redact("\n".join(str(x) for x in tail))[-2000:] or attempt.message
            self._event(job, "runner_exit", attempt=attempt, reason=reason, exit_code=body.get("exit_code"))
        self.store.update_attempt(attempt)
        self.store.update_shard(shard)
        shards = self.store.list_shards(job.id)
        job.progress = round(sum(s.progress for s in shards) / max(len(shards), 1), 4)
        self.store.update_job(job)
        return {"ok": True}

    def _record_telemetry(self, attempt_id: str, body: dict[str, Any]) -> None:
        """Best effort: telemetry must never block job state updates, and metrics must not block logs."""
        with contextlib.suppress(Exception):
            values: dict[str, Any] = {}
            for key in ("gpu_util", "progress"):
                if isinstance(body.get(key), int | float):
                    values[key] = float(body[key])
            for key in ("vram_used_mb", "vram_total_mb"):  # integer columns
                if isinstance(body.get(key), int | float):
                    values[key] = int(body[key])
            if values:
                self.store.add_metric(attempt_id, values)
        with contextlib.suppress(Exception):
            lines = [redact(str(x)) for x in (body.get("lines") or body.get("tail") or [])][-50:]
            self.store.add_log_lines(attempt_id, lines)

    # ---- polling and hops ----------------------------------------------------------------------
    def tick(self) -> dict[str, int]:
        """One scheduler pass: poll running attempts, hop where needed, dispatch waiting shards."""
        polled = dispatched = errors = 0
        with contextlib.suppress(Exception):  # housekeeping must never block scheduling
            self.store.prune_telemetry(now() - timedelta(days=14))
        self.refresh_stale_credits()
        active = {a.shard_id: a for a in self.store.list_active_attempts()}
        for job in self.store.list_jobs(limit=200):
            if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.PAUSED):
                continue
            if job.status == JobStatus.AWAITING_APPROVAL:
                continue
            try:
                for shard in self.store.list_shards(job.id):
                    attempt = active.get(shard.id)
                    if attempt is not None:
                        self._poll_attempt(job, shard, attempt)
                        polled += 1
                        job = self.store.get_job(job.id)
                if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                    dispatched += self._fill_parallel(job)
            except Exception as e:  # noqa: BLE001 - one broken job must not stall the others
                errors += 1
                with contextlib.suppress(Exception):
                    self._event(job, "tick_error", error=redact(f"{type(e).__name__}: {e}")[:300])
        return {"polled": polled, "dispatched": dispatched, "errors": errors}

    def refresh_job(self, job_id: str) -> Job:
        job = self.store.get_job(job_id)
        for shard in self.store.list_shards(job.id):
            for attempt in self.store.list_attempts(shard.id):
                if not attempt.status.terminal:
                    self._poll_attempt(job, shard, attempt)
                    job = self.store.get_job(job_id)
        return self.store.get_job(job_id)

    def _excluded(self, shard: Shard) -> set[str]:
        return {a.provider_id for a in self.store.list_attempts(shard.id)
                if a.end_reason and HOP_RULES.get(a.end_reason, (False, False))[1]}

    @staticmethod
    def _classify(state_status: AttemptStatus, message: str | None) -> EndReason:
        if state_status == AttemptStatus.SUCCEEDED:
            return EndReason.COMPLETED
        if state_status == AttemptStatus.CANCELLED:
            return EndReason.CANCELLED
        if state_status == AttemptStatus.LOST:
            return EndReason.PREEMPTED
        text = (message or "").lower()
        if any(w in text for w in CREDIT_WORDS):
            return EndReason.CREDIT_EXHAUSTED
        return EndReason.ERROR

    def _poll_attempt(self, job: Job, shard: Shard, attempt: Attempt, hop: bool = True) -> None:
        provider = self.store.get_provider(attempt.provider_id)
        adapter = self.adapter(provider)
        # Heartbeats are written by the control plane: pick up what the runner reported since we last looked.
        with contextlib.suppress(NotFound):
            fresh = self.store.get_attempt(attempt.id)
            attempt.end_reason = attempt.end_reason or fresh.end_reason
            attempt.message = fresh.message or attempt.message
            attempt.progress_end = fresh.progress_end if fresh.progress_end is not None else attempt.progress_end
            attempt.gpu_seconds = fresh.gpu_seconds or attempt.gpu_seconds
            if fresh.status.terminal:
                attempt.status, attempt.ended_at = fresh.status, fresh.ended_at
                return
        state = adapter.poll(attempt.external_ref)
        if not state.status.terminal and not getattr(adapter, "terminates_on_exit", True):
            # Pods idle after the runner exits: stop them as soon as the runner reported its end,
            # or when they overrun the attempt budget (never pay for an idle GPU).
            overdue = (now() - attempt.started_at) > timedelta(minutes=self._spec(job).timeout_minutes + 30)
            if attempt.end_reason or overdue:
                adapter.cancel(attempt.external_ref)
                ok = attempt.end_reason == EndReason.COMPLETED
                state = AttemptState(AttemptStatus.SUCCEEDED if ok else AttemptStatus.FAILED,
                                     message="stopped after exceeding its time budget" if overdue and not ok else None)
        if state.status == attempt.status:
            return
        if not state.status.terminal:
            attempt.status = state.status
            self.store.update_attempt(attempt)
            self._event(job, "attempt_" + state.status.value, attempt=attempt)
            return

        # Terminal. A reason reported by the runner (heartbeat) wins over the provider's view.
        reason = attempt.end_reason or self._classify(state.status, state.message)
        if reason == EndReason.COMPLETED and state.status != AttemptStatus.SUCCEEDED:
            reason = self._classify(state.status, state.message)
        attempt.status = state.status
        attempt.end_reason = reason
        attempt.message = state.message or attempt.message
        attempt.ended_at = now()
        if state.gpu_seconds is not None:
            attempt.gpu_seconds = state.gpu_seconds
        elif not attempt.gpu_seconds:
            attempt.gpu_seconds = (attempt.ended_at - attempt.started_at).total_seconds()
        price = next((g.usd_per_hour for g in provider.gpu_catalog if g.type == attempt.gpu_type), 0.0)
        attempt.market_cost_usd = round(attempt.gpu_seconds / 3600 * price, 4)
        if state.billed_usd is not None:  # the provider's own number beats our estimate (startup time is billed too)
            attempt.market_cost_usd = round(max(state.billed_usd, attempt.market_cost_usd), 4)
        if provider.kind == ProviderKind.PAID:
            wall = (attempt.ended_at - attempt.started_at).total_seconds()
            billed_hours = max(wall, attempt.gpu_seconds) / 3600  # pods bill wall time
            rate = float(attempt.external_ref.get("cost_per_hr") or price)
            attempt.cost_usd = round(billed_hours * rate, 4)
        else:
            attempt.cost_usd = 0.0
        if not self.store.finish_attempt(attempt):
            return  # another process (Mac or cloud scheduler) already handled this ending

        if reason == EndReason.COMPLETED:
            self._complete_shard(job, shard, attempt, provider, state.result)
            return
        self._event(job, "attempt_ended", attempt=attempt, reason=reason.value,
                    message=(attempt.message or "")[-500:])
        if reason == EndReason.CANCELLED or not hop:
            job.status = JobStatus.CANCELLED if reason == EndReason.CANCELLED else JobStatus.FAILED
            job.finished_at = now()
            self.store.update_job(job)
            return
        if reason == EndReason.CREDIT_EXHAUSTED:
            last = self.store.latest_credit(provider.id)
            unit = last.unit if last else (CreditUnit.GPU_HOURS if provider.credit_model.type == "weekly_hours"
                                           else CreditUnit.USD)
            self.store.add_credit_snapshot(CreditSnapshot(provider_id=provider.id, remaining=0, unit=unit,
                                                          source=CreditSource.ESTIMATE,
                                                          expires_at=last.expires_at if last else None))
        attempts = self.store.list_attempts(shard.id)
        errors = sum(1 for a in attempts if a.end_reason == EndReason.ERROR)
        if errors >= MAX_ERRORS or len(attempts) >= MAX_HOPS:
            job.status, job.finished_at = JobStatus.FAILED, now()
            self.store.update_job(job)
            self._event(job, "failed", reason=f"{errors} errors, {len(attempts)} attempts")
            if self.notifier:
                self.notifier.job_failed(job, attempt.message or "")
            return
        self._event(job, "hop", from_provider=provider.id, reason=reason.value,
                    resume_from=shard.checkpoint_key is not None)
        shard.status = JobStatus.QUEUED
        self.store.update_shard(shard)
        self._dispatch(job, shard, self._excluded(shard))

    def _complete_shard(self, job: Job, shard: Shard, attempt: Attempt, provider: Provider,
                        result: dict | None) -> None:
        dest = self.results_dir / job.id
        try:
            files = self._download_results(job, shard, dest)
            if files is None:  # no R2 copy: provider-native outputs
                files = self.adapter(provider).fetch_outputs(attempt.external_ref, dest, result=result)
        except Exception as e:  # noqa: BLE001
            files = []
            self._event(job, "fetch_failed", attempt=attempt, error=redact(str(e))[:300])
        shard.status, shard.progress = JobStatus.DONE, 1.0
        self.store.update_shard(shard)
        shards = self.store.list_shards(job.id)
        job.progress = round(sum(s.progress for s in shards) / len(shards), 4)
        job.results_prefix = str(dest)
        if all(s.status == JobStatus.DONE for s in shards):
            job.status, job.finished_at = JobStatus.DONE, now()
            self._event(job, "done", attempt=attempt, files=len(files), results=str(dest))
            if self.notifier:
                self.notifier.job_done(job)
        self.store.update_job(job)

    def _download_results(self, job: Job, shard: Shard, dest: Path) -> list[Path] | None:
        if not self.blobs:
            return None
        data = self.blobs.get(Keys(job.id).results(shard.idx))
        if data is None:
            return None
        return extract_tar(data, dest if shard.total == 1 else dest / f"shard-{shard.idx}")

    # ---- results / status ----------------------------------------------------------------------
    def fetch_results(self, job_id: str, dest_dir: str | None = None, partial: bool = False) -> dict[str, Any]:
        job = self.refresh_job(job_id)
        dest = Path(dest_dir).expanduser() if dest_dir else self.results_dir / job.id
        manifest: list[dict[str, Any]] = []
        for shard in self.store.list_shards(job.id):
            files = self._download_results(job, shard, dest) if shard.status == JobStatus.DONE else None
            if files is None and shard.status == JobStatus.DONE and job.results_prefix:
                src = Path(job.results_prefix)
                files = [p for p in src.rglob("*") if p.is_file()] if src.is_dir() else []
                if dest_dir and files and src.resolve() != dest.resolve():  # local mode: copy where asked
                    target = dest if shard.total == 1 else dest / f"shard-{shard.idx}"
                    shutil.copytree(src, target, dirs_exist_ok=True)
                    files = [target / f.relative_to(src) for f in files]
            if files:
                manifest.append({"shard": shard.idx, "kind": "results", "files": [str(f) for f in files]})
            elif partial and self.blobs and shard.checkpoint_key:
                data = self.blobs.get(shard.checkpoint_key)
                if data:
                    got = extract_tar(data, dest / "partial" / f"shard-{shard.idx}")
                    manifest.append({"shard": shard.idx, "kind": "checkpoint", "files": [str(f) for f in got]})
        return {"job_id": job.id, "status": job.status.value, "dest": str(dest), "items": manifest}

    def list_jobs(self, status: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Recent jobs, newest first: enough to find a job id without touching the database."""
        rows = []
        for j in self.store.list_jobs(status=status, limit=max(1, min(int(limit), 100))):
            if j.name.startswith("smoke-"):
                continue
            counter = (j.estimate.get("counters") or {}).get("0")
            rows.append({"job_id": j.id, "name": j.name, "status": j.status.value, "progress": round(j.progress, 4),
                         "count": f"{counter['done']} of {counter['total']} {counter['unit']}" if counter else None,
                         "created_at": j.created_at.isoformat(timespec="minutes"),
                         "finished_at": j.finished_at.isoformat(timespec="minutes") if j.finished_at else None})
        return {"jobs": rows}

    @staticmethod
    def _provider_log(job: Job) -> list[str]:
        """Output the provider kept for a finished job (local mode has no log storage of its own)."""
        folder = Path(job.results_prefix) / "_provider" if job.results_prefix else None
        if not folder or not folder.is_dir():
            return []
        lines: list[str] = []
        for path in sorted(folder.glob("*.log")):
            raw = path.read_text(errors="replace")
            try:  # Kaggle: a JSON list of {stream_name, time, data}
                lines += [ln for row in json.loads(raw) for ln in str(row.get("data", "")).splitlines()]
            except (ValueError, AttributeError, TypeError):
                lines += raw.splitlines()
        return lines

    def job_logs(self, job_id: str, lines: int = 80, contains: str | None = None) -> dict[str, Any]:
        """Latest output lines of each attempt of a job (full log from R2 once an attempt has ended)."""
        lines = max(1, min(int(lines), 400))
        job = self.store.get_job(job_id)
        out = []
        for shard in self.store.list_shards(job.id):
            for a in self.store.list_attempts(shard.id):
                text: list[str] = []
                source = "live"
                if a.status.terminal and self.blobs:
                    data = self.blobs.get(Keys.log(a.id))
                    if data:
                        text, source = data.decode(errors="replace").splitlines(), "full log"
                if not text:
                    text = self.store.list_log_lines(a.id, limit=400)
                if not text and a.status.terminal:
                    text = self._provider_log(job)
                    source = "provider log" if text else source
                if not text and a.message:
                    text, source = a.message.splitlines(), "last message"
                if contains:
                    needle = contains.lower()
                    text = [ln for ln in text if needle in ln.lower()]
                out.append({"shard": shard.idx, "provider": a.provider_id, "status": a.status.value,
                            "end_reason": a.end_reason.value if a.end_reason else None, "source": source,
                            "lines": [redact(ln)[:2000] for ln in text[-lines:]]})
        return {"job_id": job.id, "name": job.name, "status": job.status.value, "attempts": out}

    def job_status(self, job_id: str, refresh: bool = True) -> dict[str, Any]:
        job = self.refresh_job(job_id) if refresh else self.store.get_job(job_id)
        stops = []
        for shard in self.store.list_shards(job.id):
            for a in self.store.list_attempts(shard.id):
                stops.append({"shard": shard.idx, "provider": a.provider_id, "gpu": a.gpu_type,
                              "status": a.status.value, "end_reason": a.end_reason.value if a.end_reason else None,
                              "started_at": a.started_at.isoformat(),
                              "ended_at": a.ended_at.isoformat() if a.ended_at else None,
                              "progress": a.progress_end, "gpu_minutes": round(a.gpu_seconds / 60, 1),
                              "message": a.message[-800:] if a.message else None,
                              "link": a.external_ref.get("url")})
        shards = self.store.list_shards(job.id)
        events = [{"ts": e.ts.isoformat(), "type": e.type} for e in self.store.list_events(job.id, limit=15)]
        return {"job_id": job.id, "name": job.name, "status": job.status.value, "progress": job.progress,
                "last_checkpoint": max((s.checkpoint_at for s in shards if s.checkpoint_at), default=None),
                "results": job.results_prefix, "stops": stops, "recent_events": events}

    # ---- reports -------------------------------------------------------------------------------
    def usage_report(self, period: str = "week") -> dict[str, Any]:
        """GPU-hours per provider, credit left, failed/migrated jobs, real cost and savings."""
        days = {"week": 7, "month": 30, "all": 3650}.get(period, 7)
        since = now() - timedelta(days=days)
        attempts = self.store.list_attempts_since(since)
        providers = {p.id: p for p in self.store.list_providers()}
        per: dict[str, dict[str, Any]] = {}
        for a in attempts:
            row = per.setdefault(a.provider_id, {"gpu_hours": 0.0, "market_usd": 0.0, "spent_usd": 0.0,
                                                 "saved_usd": 0.0, "attempts": 0, "errors": 0, "hops_from": 0})
            row["saved_usd"] += 0.0 if a.cost_usd > 0 else a.market_cost_usd
            row["gpu_hours"] += a.gpu_seconds / 3600
            row["market_usd"] += a.market_cost_usd
            row["spent_usd"] += a.cost_usd
            row["attempts"] += 1
            row["errors"] += a.end_reason == EndReason.ERROR
            row["hops_from"] += a.end_reason in (EndReason.SESSION_LIMIT, EndReason.PREEMPTED,
                                                 EndReason.CREDIT_EXHAUSTED, EndReason.ERROR)
        rows = []
        for pid, p in providers.items():
            r = per.get(pid, {"gpu_hours": 0.0, "market_usd": 0.0, "spent_usd": 0.0, "saved_usd": 0.0,
                              "attempts": 0, "errors": 0, "hops_from": 0})
            snap = self.store.latest_credit(pid) if p.status == ProviderStatus.ACTIVE else None
            left = None
            if snap:
                left = f"{snap.remaining:.1f} h" if snap.unit == CreditUnit.GPU_HOURS else f"${snap.remaining:.2f}"
            rows.append({"provider": pid, "name": p.name, "status": p.status.value, "credit_left": left,
                         **{k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()}})
        jobs = [j for j in self.store.list_jobs(limit=500) if j.created_at >= since]
        totals = {
            "gpu_hours": round(sum(r["gpu_hours"] for r in rows), 3),
            "spent_usd": round(sum(r["spent_usd"] for r in rows), 2),
            "saved_usd": round(sum(r["saved_usd"] for r in rows), 2),  # market value of free GPU time only
            "jobs_done": sum(j.status == JobStatus.DONE for j in jobs),
            "jobs_failed": sum(j.status == JobStatus.FAILED for j in jobs),
            "jobs_waiting": sum(j.status in (JobStatus.QUEUED, JobStatus.AWAITING_APPROVAL) for j in jobs),
            "hops": sum(r["hops_from"] for r in rows),
        }
        return {"period": period, "since": since.isoformat(), "totals": totals,
                "providers": sorted(rows, key=lambda r: -r["gpu_hours"])}

    def status_text(self, job_id: str | None = None) -> str:
        jobs = [self.store.get_job(job_id)] if job_id else self.store.list_jobs(limit=10)
        lines = []
        for j in jobs:
            label = {"running": "Running", "done": "Done", "awaiting_approval": "Needs your OK",
                     "queued": "Waiting in line", "paused": "Paused", "failed": "Stopped",
                     "cancelled": "Cancelled", "needs_attention": "Needs attention"}.get(j.status.value, j.status.value)
            lines.append(f"{j.name}: {label} · {j.progress * 100:.0f}%")
        return "\n".join(lines) or "No jobs yet."

    def results_link(self, job_id: str) -> dict[str, Any]:
        """Presigned download of finished results, or of the latest checkpoint while running."""
        if not self.blobs:
            return {"error": "R2 is not configured"}
        job = self.store.get_job(job_id)
        links = []
        for shard in self.store.list_shards(job.id):
            key = Keys(job.id).results(shard.idx)
            kind = "results"
            if not self.blobs.exists(key):
                key, kind = shard.checkpoint_key, "checkpoint"
            if key and self.blobs.exists(key):
                links.append({"shard": shard.idx, "kind": kind,
                              "url": self.blobs.presign_get(key, timedelta(minutes=15))})
        return {"job_id": job.id, "links": links}

    def rerun(self, job_id: str, min_vram_gb: float | None = None, cheapest: bool = False) -> dict[str, Any]:
        """Run an existing job again as a new job (package reused from R2), optionally changing GPU needs."""
        source = self.store.get_job(job_id)
        spec = self._spec(source)
        if min_vram_gb:
            spec.gpu.min_vram_gb = float(min_vram_gb)
        if cheapest:
            spec.gpu.types = []  # routing already picks the cheapest GPU that fits on each provider
        package = self._load_package(source)
        job = Job(project_id=source.project_id, name=spec.name,
                  spec=spec.model_dump() | {"_root": source.spec.get("_root", "")}, spec_hash=spec.spec_hash(),
                  estimate={k: v for k, v in source.estimate.items() if k != "approval"})
        self._save_package(job, package)
        self.store.create_job(job)
        for shard in self.store.list_shards(source.id):
            self.store.create_shard(Shard(job_id=job.id, idx=shard.idx, total=shard.total, items=shard.items))
        self._event(job, "submitted", rerun_of=source.id)
        reason = self._approval_reason(job.estimate)
        if reason:
            self._request_approval(job, reason)
        else:
            self._fill_parallel(job)
        return {"job_id": job.id, "status": self.store.get_job(job.id).status.value}

    def command_snapshot(self) -> dict[str, Any]:
        """What the natural-language palette is allowed to know: names, states and credit. No secrets."""
        providers = {p.id: p for p in self.store.list_providers()}
        projects = []
        for j in self.store.list_jobs(limit=25):
            if j.name.startswith("smoke-"):
                continue
            projects.append({"id": j.id, "name": j.name, "status": j.status.value,
                             "progress": round(j.progress, 2),
                             "created": j.created_at.isoformat(timespec="minutes"),
                             "finished": j.finished_at.isoformat(timespec="minutes") if j.finished_at else None,
                             "needs_ok": (j.estimate.get("approval") or {}).get("reason")
                             if j.status == JobStatus.AWAITING_APPROVAL else None})
        credit = []
        for p in providers.values():
            if p.status != ProviderStatus.ACTIVE:
                continue
            snap = self.store.latest_credit(p.id)
            credit.append({"provider": p.name, "paid": p.kind == ProviderKind.PAID,
                           "left": round(snap.remaining, 2) if snap else None,
                           "unit": snap.unit.value if snap else None})
        report = self.usage_report("month")["totals"]
        return {"now": now().isoformat(timespec="minutes"), "projects": projects, "providers": credit,
                "this_month": {"gpu_hours": report["gpu_hours"], "saved_usd": report["saved_usd"],
                               "spent_usd": report["spent_usd"]}}

    def dashboard_action(self, job_id: str, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        handlers = {"approve": lambda: self.approve(job_id, channel="dashboard"),
                    "deny": lambda: self.deny(job_id, channel="dashboard"),
                    "pause": lambda: self.pause(job_id), "resume": lambda: self.resume(job_id),
                    "rerun": lambda: self.rerun(job_id, args.get("min_vram_gb"), bool(args.get("cheapest"))),
                    "results": lambda: self.results_link(job_id)}
        if action not in handlers:
            return {"error": f"unknown action {action}"}
        return handlers[action]()

    # ---- WhatsApp action links -----------------------------------------------------------------
    def handle_action(self, token: str, method: str) -> tuple[int, str]:
        from tierhopper import actions
        from tierhopper.notify import weekly_text

        key = actions.secret()
        payload = actions.read_token(token, key) if key else None
        if payload is None:
            return 403, actions.page("Link expired", "Ask for a new one from TierHopper.")
        action, job_id = payload["a"], payload.get("j")
        job = self.store.get_job(job_id) if job_id else None
        title = f"{action.title()} · {job.name}" if job else action.title()
        if action in ("status", "report"):
            text = self.status_text(job_id) if action == "status" else weekly_text(self.usage_report("week"))
            return 200, actions.page(title, "TierHopper", actions.pre(text))
        if method != "POST":
            what = (job.estimate.get("approval") or {}).get("reason", "") if job else ""
            return 200, actions.page(title, what or "Tap to confirm.", actions.confirm_form(token, action))
        handlers = {"approve": lambda: self.approve(job_id, channel="whatsapp"),
                    "deny": lambda: self.deny(job_id, channel="whatsapp"),
                    "pause": lambda: self.pause(job_id), "resume": lambda: self.resume(job_id)}
        result = handlers[action]()
        return 200, actions.page(title, f"Done — status is now {result['status']}.")

    # ---- connect -------------------------------------------------------------------------------
    def smoke_test(self, provider_id: str, timeout_s: int = 1200, poll_s: int = 15,
                   on_update: Callable[[str], None] = lambda _: None) -> tuple[bool, str]:
        """Run the built-in GPU smoke job on one provider, regardless of its status."""
        provider = self.store.get_provider(provider_id)
        spec, root = load_spec(SMOKE_SPEC)
        offers = sorted((g for g in provider.gpu_catalog if g.vram_gb >= spec.gpu.min_vram_gb),
                        key=lambda g: g.usd_per_hour)
        if not offers:
            return False, "provider has no suitable GPU in the registry"
        project = self.store.get_or_create_project("tierhopper-smoke")
        job = Job(project_id=project.id, name=f"smoke-{provider_id}",
                  spec=spec.model_dump() | {"_root": str(root)}, spec_hash=spec.spec_hash())
        package = build_package(root)
        self._save_package(job, package)
        self.store.create_job(job)
        shard = self.store.create_shard(Shard(job_id=job.id))
        attempt = Attempt(shard_id=shard.id, provider_id=provider_id, gpu_type=offers[0].type)
        plan = AttemptPlan(attempt_id=attempt.id, job_id=job.id, spec=spec, package=package, gpu=offers[0],
                           runner_config=self._runner_config(job, shard, attempt, provider, spec),
                           package_url=self._package_url(job))
        try:
            attempt.external_ref = self.adapter(provider).submit(plan)
        except AdapterError as e:
            job.status = JobStatus.FAILED
            self.store.update_job(job)
            return False, str(e)
        self.store.create_attempt(attempt)
        job.status, job.started_at = JobStatus.RUNNING, now()
        self.store.update_job(job)
        on_update(f"submitted to {provider.name} ({offers[0].type}); waiting for the GPU…")
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            self._poll_attempt(job, shard, attempt, hop=False)
            if attempt.status != last:
                on_update(f"status: {attempt.status.value}")
                last = attempt.status
            if attempt.status.terminal:
                break
            time.sleep(poll_s)
        if not attempt.status.terminal:  # timed out: never leave a (possibly paid) machine running
            with contextlib.suppress(Exception):
                self.adapter(provider).cancel(attempt.external_ref)
            attempt.status, attempt.end_reason, attempt.ended_at = AttemptStatus.CANCELLED, EndReason.CANCELLED, now()
            self.store.update_attempt(attempt)
            job.status = JobStatus.FAILED
            self.store.update_job(job)
            return False, "smoke test timed out; the machine was stopped"
        if attempt.status != AttemptStatus.SUCCEEDED:
            return False, attempt.message or f"smoke test ended as {attempt.status.value}"
        result = self.results_dir / job.id / "results" / "result.json"
        if not result.is_file():
            return False, "smoke job finished but result.json is missing"
        info = json.loads(result.read_text())
        if not info.get("cuda"):
            return False, "job ran without a GPU"
        return True, f"{info.get('gpu')} · {info.get('vram_gb')} GB · {info.get('tflops')} TFLOPS fp16"

    # ---- helpers -------------------------------------------------------------------------------
    def _event(self, job: Job, type_: str, attempt: Attempt | None = None, **payload: Any) -> None:
        clean = {k: redact(v) if isinstance(v, str) else v for k, v in payload.items()}
        self.store.add_event(Event(job_id=job.id, shard_id=attempt.shard_id if attempt else None,
                                   attempt_id=attempt.id if attempt else None, type=type_, payload=clean))
