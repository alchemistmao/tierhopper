"""Provider statistics learned from history: failure rate, queue time and real speed per GPU."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from tierhopper.models import Attempt, EndReason, now
from tierhopper.store.base import Store

WINDOW = timedelta(days=30)

# Relative single-GPU fp16 throughput, used until we have real measurements.
STATIC_SPEED = {
    "T4": 1.0, "T4x2": 1.0, "L4": 1.5, "L4x2": 1.5, "A10": 2.0, "A10G": 2.0, "A40": 3.0, "RTX-4090": 3.5,
    "L40S": 4.0, "A100-40GB": 5.0, "A100": 5.0, "A100-80GB": 5.5, "H100": 10.0, "H200": 11.0,
}


def static_speed(gpu: str) -> float:
    return STATIC_SPEED.get(gpu, 1.0)


@dataclass
class ProviderStats:
    provider_id: str
    attempts: int = 0
    failures: int = 0
    queue_seconds: list[float] = field(default_factory=list)
    speed: dict[str, float] = field(default_factory=dict)  # gpu type -> learned relative speed

    @property
    def failure_rate(self) -> float:
        # Laplace smoothing so one early failure does not bench a provider forever.
        return (self.failures + 0.5) / (self.attempts + 2)

    @property
    def queue_p50(self) -> float | None:
        return median(self.queue_seconds) if self.queue_seconds else None

    def speed_of(self, gpu: str) -> float:
        return self.speed.get(gpu, static_speed(gpu))


def compute_stats(store: Store, since: datetime | None = None) -> dict[str, ProviderStats]:
    since = since or now() - WINDOW
    attempts = store.list_attempts_since(since)
    running_at = {e.attempt_id: e.ts for e in store.list_events_since("attempt_running", since) if e.attempt_id}
    stats: dict[str, ProviderStats] = defaultdict(lambda: ProviderStats(""))
    for a in attempts:
        s = stats[a.provider_id]
        s.provider_id = a.provider_id
        if a.end_reason is None:
            continue
        s.attempts += 1
        if a.end_reason == EndReason.ERROR:
            s.failures += 1
        if a.id in running_at:
            s.queue_seconds.append(max((running_at[a.id] - a.started_at).total_seconds(), 0.0))
    _learn_speed(store, attempts, stats)
    return dict(stats)


def _learn_speed(store: Store, attempts: list[Attempt], stats: dict[str, ProviderStats]) -> None:
    """Seconds per unit of progress for the same job spec on different GPUs -> relative speed."""
    rate: dict[str, dict[tuple[str, str], list[float]]] = defaultdict(lambda: defaultdict(list))
    spec_of: dict[str, str] = {}
    for a in attempts:
        done = (a.progress_end or 0) - a.progress_start
        if a.end_reason is None or a.end_reason == EndReason.ERROR or done <= 0.05 or a.gpu_seconds <= 0:
            continue
        if a.shard_id not in spec_of:
            try:
                spec_of[a.shard_id] = store.get_job(store.get_shard(a.shard_id).job_id).spec_hash
            except KeyError:
                continue
        rate[spec_of[a.shard_id]][(a.provider_id, a.gpu_type)].append(a.gpu_seconds / done)
    ratios: dict[tuple[str, str], list[float]] = defaultdict(list)
    for per_gpu in rate.values():
        if len(per_gpu) < 2:
            continue
        # Normalise against a T4-equivalent baseline derived from the static table.
        baseline = median(median(v) * static_speed(g) for (_, g), v in per_gpu.items())
        for (provider, gpu), values in per_gpu.items():
            ratios[(provider, gpu)].append(baseline / median(values))
    for (provider, gpu), values in ratios.items():
        stats[provider].provider_id = provider
        stats[provider].speed[gpu] = median(values)


def estimate_t4_hours(store: Store, spec_hash: str, stats: dict[str, ProviderStats]) -> float | None:
    """T4-equivalent GPU-hours for a spec, from finished jobs with the same spec hash."""
    hours = estimate_hours(store, spec_hash, "T4", stats)
    return hours * static_speed("T4") if hours is not None else None


def estimate_hours(store: Store, spec_hash: str, target_gpu: str, stats: dict[str, ProviderStats]) -> float | None:
    """GPU-hours for a spec on `target_gpu`, from finished jobs with the same spec hash."""
    totals = []
    for job in store.list_jobs(status="done", limit=200):
        if job.spec_hash != spec_hash:
            continue
        t4_seconds = 0.0
        for shard in store.list_shards(job.id):
            for a in store.list_attempts(shard.id):
                speed = stats.get(a.provider_id, ProviderStats(a.provider_id)).speed_of(a.gpu_type)
                t4_seconds += a.gpu_seconds * speed
        if t4_seconds > 0:
            totals.append(t4_seconds)
    if not totals:
        return None
    return round(median(totals) / static_speed(target_gpu) / 3600, 3)
