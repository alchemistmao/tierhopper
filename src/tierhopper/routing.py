"""Provider selection: hard filters, then FASTEST FREE GPU FIRST.

On each provider we take the fastest GPU that fits the job and that the remaining free credit can pay
for. Providers are then ordered by that GPU's speed; ties are broken by the score below. Free
providers always rank before paid ones (paid needs explicit approval).

score = 0.45 * urgency       credit value that expires unused, per hour left (use-it-or-lose-it)
      + 0.25 * speed         learned throughput of the offered GPU (static table until we have history)
      - 0.15 * failure_rate  errors over finished attempts, last 30 days
      - 0.15 * queue_time    median wait until the job was running, last 30 days
Each term is normalised to 0..1 across the candidates. Free providers always rank before paid ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tierhopper.adapters import IMPLEMENTED
from tierhopper.models import CreditSnapshot, CreditUnit, GpuOffer, Provider, ProviderKind, ProviderStatus, now
from tierhopper.spec import JobSpec
from tierhopper.stats import ProviderStats, static_speed

WEIGHTS = {"urgency": 0.45, "speed": 0.25, "failure": 0.15, "queue": 0.15}


@dataclass
class Candidate:
    provider: Provider
    gpu: GpuOffer
    credit: CreditSnapshot | None
    alternatives: list[GpuOffer] = field(default_factory=list)  # slower GPUs that also fit, fastest first
    score: float = 0.0
    terms: dict[str, float] = field(default_factory=dict)


@dataclass
class Rejection:
    provider_id: str
    reason: str


def gpu_fits(offer: GpuOffer, spec: JobSpec) -> bool:
    if spec.gpu.types and offer.type not in spec.gpu.types:
        return False
    # `min_vram_gb` is per GPU: a model that needs 24 GB does not fit on 2x16 GB without sharding.
    return offer.vram_gb >= spec.gpu.min_vram_gb


# Adapters that run a job in the container image named by its `image:` field. Kaggle never will (it has no
# such option); Lightning and RunPod take registry images and come next.
RUNS_IMAGES = {"modal"}


def plan_route(
    spec: JobSpec,
    providers: list[Provider],
    credits: dict[str, CreditSnapshot | None],
    exclude: set[str] | None = None,
    allow_paid: bool = False,
    stats: dict[str, ProviderStats] | None = None,
    busy: dict[str, int] | None = None,
    at: datetime | None = None,
    t4_hours: float | None = None,
    committed: dict[str, float] | None = None,
) -> tuple[list[Candidate], list[Rejection]]:
    """Return eligible candidates, best first, and why the others were rejected.

    committed: what the jobs already running on each provider are expected to use (same unit as its credit).
    """
    exclude, stats, busy, committed = exclude or set(), stats or {}, busy or {}, committed or {}
    ok: list[Candidate] = []
    rejected: list[Rejection] = []
    for p in providers:
        if p.id in exclude:
            rejected.append(Rejection(p.id, "already tried for this job"))
            continue
        if p.status == ProviderStatus.UNAVAILABLE:
            rejected.append(Rejection(p.id, "not answering (failed its health check)"))
            continue
        if p.status != ProviderStatus.ACTIVE:
            rejected.append(Rejection(p.id, f"not connected ({p.status})"))
            continue
        if p.adapter not in IMPLEMENTED:
            rejected.append(Rejection(p.id, "no adapter yet"))
            continue
        if spec.image and p.adapter not in RUNS_IMAGES:
            rejected.append(Rejection(p.id, "cannot run the job's own container image yet"))
            continue
        if p.kind == ProviderKind.PAID and not allow_paid:
            rejected.append(Rejection(p.id, "paid provider needs your approval"))
            continue
        if p.max_concurrent and busy.get(p.id, 0) >= p.max_concurrent:
            rejected.append(Rejection(p.id, f"busy ({busy[p.id]} running, limit {p.max_concurrent})"))
            continue
        fits = [g for g in p.gpu_catalog if gpu_fits(g, spec)]
        if not fits:
            rejected.append(Rejection(p.id, f"no GPU with {spec.gpu.min_vram_gb:g} GB+"))
            continue
        credit = credits.get(p.id)
        if credit is not None and p.kind != ProviderKind.PAID:
            free = credit.remaining - committed.get(p.id, 0.0)  # what is really left for a new job
            if credit.remaining <= 0:
                rejected.append(Rejection(p.id, "free credit used up"))
                continue
            if credit.unit == CreditUnit.USD:
                # Dollar credit sits in front of a card: a job that does not fit would be billed for real.
                # So the job goes there only when its whole estimated cost fits what is left, with a reserve.
                cheapest_need = min(job_cost(g, spec, stats.get(p.id), t4_hours) for g in fits)
                if cheapest_need > free - USD_RESERVE:
                    rejected.append(Rejection(p.id, f"free credit left (${max(free, 0):.2f}) does not cover "
                                                    f"this job (about ${cheapest_need:.2f})"))
                    continue
            credit = credit.model_copy(update={"remaining": free - (USD_RESERVE if credit.unit == CreditUnit.USD
                                                                    else 0.0)})
        best = pick_gpu(p, fits, credit, spec, stats.get(p.id), t4_hours)
        slower = sorted((g for g in fits if speed_of(g, stats.get(p.id)) < speed_of(best, stats.get(p.id))),
                        key=lambda g: -speed_of(g, stats.get(p.id)))
        ok.append(Candidate(p, best, credit, alternatives=[] if p.kind == ProviderKind.PAID else slower))
    _score(ok, stats, at or now())
    ok.sort(key=lambda c: (c.provider.kind == ProviderKind.PAID, -speed_of(c.gpu, stats.get(c.provider.id)),
                           -c.score))
    return ok, rejected


USD_RESERVE = 1.0  # never plan to spend the last dollar of a free credit: provider billing has extras
COST_MARGIN = 1.2  # CPU, memory and startup are billed on top of the GPU price


def job_cost(gpu: GpuOffer, spec: JobSpec, stats: ProviderStats | None, t4_hours: float | None) -> float:
    """Estimated dollars a job uses on this GPU. Without an estimate, its whole time limit (the worst case)."""
    hours = t4_hours / speed_of(gpu, stats) if t4_hours else spec.timeout_minutes / 60
    return hours * gpu.usd_per_hour * COST_MARGIN


def speed_of(gpu: GpuOffer, stats: ProviderStats | None) -> float:
    """Relative throughput (T4 = 1), learned from history when we have it."""
    return stats.speed_of(gpu.type) if stats and stats.speed else static_speed(gpu.type)


def pick_gpu(provider: Provider, fits: list[GpuOffer], credit: CreditSnapshot | None, spec: JobSpec,
             stats: ProviderStats | None, t4_hours: float | None) -> GpuOffer:
    """Fastest GPU the remaining free credit can pay for.

    If no GPU can finish the job on the credit left, take the one that does the most work per dollar:
    the job gets as far as possible before it hops. Paid providers keep the cheapest GPU that fits
    (real money is never spent on speed by default). Without an estimate we budget for the job's
    whole time limit, which is the worst case.
    """
    cheapest = min(fits, key=lambda g: g.usd_per_hour)
    if provider.kind == ProviderKind.PAID:
        return cheapest
    by_speed = sorted(fits, key=lambda g: (-speed_of(g, stats), g.usd_per_hour))
    if credit is None:
        return by_speed[0]
    for gpu in by_speed:
        hours = t4_hours / speed_of(gpu, stats) if t4_hours else spec.timeout_minutes / 60
        need = hours if credit.unit == CreditUnit.GPU_HOURS else hours * gpu.usd_per_hour * COST_MARGIN
        if need <= credit.remaining:
            return gpu
    return max(fits, key=lambda g: speed_of(g, stats) / max(g.usd_per_hour, 0.01))


def _score(cands: list[Candidate], stats: dict[str, ProviderStats], at: datetime) -> None:
    if not cands:
        return
    raw = {}
    for c in cands:
        s = stats.get(c.provider.id, ProviderStats(c.provider.id))
        raw[id(c)] = {
            "urgency": _urgency(c, at),
            "speed": s.speed_of(c.gpu.type) if s.speed else static_speed(c.gpu.type),
            "failure": s.failure_rate,
            "queue": s.queue_p50 or 0.0,
        }
    top = {k: max(r[k] for r in raw.values()) or 1.0 for k in WEIGHTS}
    for c in cands:
        t = {k: raw[id(c)][k] / top[k] for k in WEIGHTS}
        c.terms = {k: round(v, 3) for k, v in t.items()}
        c.score = round(WEIGHTS["urgency"] * t["urgency"] + WEIGHTS["speed"] * t["speed"]
                        - WEIGHTS["failure"] * t["failure"] - WEIGHTS["queue"] * t["queue"], 4)


def _urgency(c: Candidate, at: datetime) -> float:
    """USD-equivalent of remaining free credit divided by hours until it expires."""
    credit = c.credit
    if c.provider.kind == ProviderKind.PAID or credit is None or credit.expires_at is None:
        return 0.0
    value = credit.remaining * (c.gpu.usd_per_hour if credit.unit == CreditUnit.GPU_HOURS else 1.0)
    hours_left = max((credit.expires_at - at).total_seconds() / 3600, 1.0)
    return value / hours_left
