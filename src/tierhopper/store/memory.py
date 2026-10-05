"""In-memory store used by tests and dry runs."""

from __future__ import annotations

from datetime import datetime

from tierhopper.models import (
    Attempt,
    CreditSnapshot,
    Event,
    Job,
    Project,
    Provider,
    Shard,
)
from tierhopper.store.base import NotFound


class MemoryStore:
    def __init__(self) -> None:
        self.providers: dict[str, Provider] = {}
        self.credits: list[CreditSnapshot] = []
        self.projects: dict[str, Project] = {}
        self.jobs: dict[str, Job] = {}
        self.shards: dict[str, Shard] = {}
        self.attempts: dict[str, Attempt] = {}
        self.events: list[Event] = []
        self.metrics: list[dict] = []
        self.log_lines: list[dict] = []
        self.push_subscriptions: list[dict] = []
        self.findings: list[dict] = []

    @staticmethod
    def _get(table: dict, key: str):
        try:
            return table[key]
        except KeyError:
            raise NotFound(key) from None

    def upsert_provider(self, provider: Provider) -> Provider:
        self.providers[provider.id] = provider.model_copy()
        return provider

    def get_provider(self, provider_id: str) -> Provider:
        return self._get(self.providers, provider_id).model_copy()

    def list_providers(self, status: str | None = None) -> list[Provider]:
        return [p.model_copy() for p in self.providers.values() if status is None or p.status == status]

    def add_credit_snapshot(self, snap: CreditSnapshot) -> CreditSnapshot:
        self.credits.append(snap)
        return snap

    def latest_credit(self, provider_id: str) -> CreditSnapshot | None:
        snaps = [s for s in self.credits if s.provider_id == provider_id]
        return max(snaps, key=lambda s: s.measured_at) if snaps else None

    def get_or_create_project(self, slug: str) -> Project:
        for p in self.projects.values():
            if p.slug == slug:
                return p
        project = Project(slug=slug, name=slug)
        self.projects[project.id] = project
        return project

    def get_project(self, project_id: str) -> Project:
        return self._get(self.projects, project_id)

    def create_job(self, job: Job) -> Job:
        self.jobs[job.id] = job.model_copy()
        return job

    update_job = create_job

    def get_job(self, job_id: str) -> Job:
        return self._get(self.jobs, job_id).model_copy()

    def list_jobs(self, status: str | None = None, limit: int = 50) -> list[Job]:
        jobs = [j for j in self.jobs.values() if status is None or j.status == status]
        return [j.model_copy() for j in sorted(jobs, key=lambda j: j.created_at, reverse=True)[:limit]]

    def create_shard(self, shard: Shard) -> Shard:
        self.shards[shard.id] = shard.model_copy()
        return shard

    update_shard = create_shard

    def list_shards(self, job_id: str) -> list[Shard]:
        return sorted((s.model_copy() for s in self.shards.values() if s.job_id == job_id), key=lambda s: s.idx)

    def get_shard(self, shard_id: str) -> Shard:
        return self._get(self.shards, shard_id).model_copy()

    def get_attempt(self, attempt_id: str) -> Attempt:
        return self._get(self.attempts, attempt_id).model_copy()

    def create_attempt(self, attempt: Attempt) -> Attempt:
        self.attempts[attempt.id] = attempt.model_copy()
        return attempt

    update_attempt = create_attempt

    def finish_attempt(self, attempt: Attempt) -> bool:
        current = self.attempts.get(attempt.id)
        if current is not None and current.status.terminal:
            return False
        self.attempts[attempt.id] = attempt.model_copy()
        return True

    def list_attempts(self, shard_id: str) -> list[Attempt]:
        return sorted((a.model_copy() for a in self.attempts.values() if a.shard_id == shard_id),
                      key=lambda a: a.started_at)

    def list_active_attempts(self) -> list[Attempt]:
        return [a.model_copy() for a in self.attempts.values() if not a.status.terminal]

    def usage_since(self, provider_id: str, since: datetime) -> tuple[float, float]:
        """(gpu_seconds, market_cost_usd) of attempts on a provider started since `since`."""
        rows = [a for a in self.attempts.values() if a.provider_id == provider_id and a.started_at >= since]
        return sum(a.gpu_seconds for a in rows), sum(a.market_cost_usd for a in rows)

    def list_attempts_since(self, since: datetime) -> list[Attempt]:
        return sorted((a.model_copy() for a in self.attempts.values() if a.started_at >= since),
                      key=lambda a: a.started_at)

    def add_event(self, event: Event) -> Event:
        self.events.append(event)
        return event

    def list_events(self, job_id: str, limit: int = 100) -> list[Event]:
        return [e for e in self.events if e.job_id == job_id][-limit:]

    def list_events_since(self, type_: str, since: datetime) -> list[Event]:
        return [e for e in self.events if e.type == type_ and e.ts >= since]

    def add_metric(self, attempt_id: str, values: dict) -> None:
        self.metrics.append({"attempt_id": attempt_id, **values})

    def add_log_lines(self, attempt_id: str, lines: list[str]) -> None:
        self.log_lines.extend({"attempt_id": attempt_id, "line": line} for line in lines)

    def prune_telemetry(self, before: datetime) -> None:
        return None

    def list_push_subscriptions(self) -> list[dict]:
        return list(self.push_subscriptions)

    def delete_push_subscription(self, endpoint: str) -> None:
        self.push_subscriptions = [s for s in self.push_subscriptions if s["endpoint"] != endpoint]

    def add_finding(self, finding: dict) -> None:
        self.findings.append(finding)

    def list_findings(self, limit: int = 50) -> list[dict]:
        return self.findings[-limit:]

    def list_log_lines(self, attempt_id: str, limit: int = 200) -> list[str]:
        return [row["line"] for row in self.log_lines if row["attempt_id"] == attempt_id][-limit:]
