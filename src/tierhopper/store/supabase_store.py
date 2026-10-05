"""Supabase (Postgres) store. Uses the service-role/secret key from the Keychain."""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel

from tierhopper import credentials
from tierhopper.models import Attempt, CreditSnapshot, Event, Job, Project, Provider, Shard
from tierhopper.store.base import NotFound

ACTIVE_ATTEMPT_STATUSES = ["submitted", "queued", "running"]
PROVIDER_STATIC_FIELDS = {"max_concurrent"}

M = TypeVar("M", bound=BaseModel)


def _row(model: BaseModel, exclude: set[str] | None = None) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude=exclude)


class SupabaseStore:
    def __init__(self, url: str | None = None, key: str | None = None) -> None:
        from supabase import create_client

        url = url or credentials.get_secret("supabase", "url")
        key = key or credentials.get_secret("supabase", "secret_key")
        if not (url and key):
            raise RuntimeError("Supabase is not configured; run `tierhopper config supabase`")
        self.db = create_client(url, key)
        self._static: dict[str, Provider] | None = None

    def _t(self, name: str):
        return self.db.table(name)

    @staticmethod
    def _one(resp, model: type[M], key: str) -> M:
        if not resp.data:
            raise NotFound(key)
        return model.model_validate(resp.data[0])

    # providers — static limits not stored in the table come from providers.yaml
    def _with_static(self, row: dict) -> Provider:
        provider = Provider.model_validate(row)
        if self._static is None:
            from tierhopper.registry import load_registry

            try:
                self._static = {p.id: p for p in load_registry()}
            except OSError:
                self._static = {}
        seed = self._static.get(provider.id)
        if seed is not None:
            provider.max_concurrent = seed.max_concurrent
        return provider

    def upsert_provider(self, provider: Provider) -> Provider:
        self._t("providers").upsert(_row(provider, exclude=PROVIDER_STATIC_FIELDS)).execute()
        return provider

    def get_provider(self, provider_id: str) -> Provider:
        resp = self._t("providers").select("*").eq("id", provider_id).execute()
        if not resp.data:
            raise NotFound(provider_id)
        return self._with_static(resp.data[0])

    def list_providers(self, status: str | None = None) -> list[Provider]:
        q = self._t("providers").select("*").order("id")
        if status:
            q = q.eq("status", status)
        return [self._with_static(r) for r in q.execute().data]

    # credits
    def add_credit_snapshot(self, snap: CreditSnapshot) -> CreditSnapshot:
        self._t("credit_snapshots").insert(_row(snap)).execute()
        return snap

    def latest_credit(self, provider_id: str) -> CreditSnapshot | None:
        resp = (self._t("credit_snapshots").select("*").eq("provider_id", provider_id)
                .order("measured_at", desc=True).limit(1).execute())
        return CreditSnapshot.model_validate(resp.data[0]) if resp.data else None

    # projects / jobs
    def get_or_create_project(self, slug: str) -> Project:
        resp = self._t("projects").select("*").eq("slug", slug).execute()
        if resp.data:
            return Project.model_validate(resp.data[0])
        project = Project(slug=slug, name=slug)
        self._t("projects").upsert(_row(project), on_conflict="slug", ignore_duplicates=True).execute()
        return self._one(self._t("projects").select("*").eq("slug", slug).execute(), Project, slug)

    def get_project(self, project_id: str) -> Project:
        return self._one(self._t("projects").select("*").eq("id", project_id).execute(), Project, project_id)

    def create_job(self, job: Job) -> Job:
        self._t("jobs").insert(_row(job)).execute()
        return job

    def update_job(self, job: Job) -> Job:
        self._t("jobs").update(_row(job, exclude={"id"})).eq("id", job.id).execute()
        return job

    def get_job(self, job_id: str) -> Job:
        return self._one(self._t("jobs").select("*").eq("id", job_id).execute(), Job, job_id)

    def list_jobs(self, status: str | None = None, limit: int = 50) -> list[Job]:
        q = self._t("jobs").select("*").order("created_at", desc=True).limit(limit)
        if status:
            q = q.eq("status", status)
        return [Job.model_validate(r) for r in q.execute().data]

    # shards / attempts
    def create_shard(self, shard: Shard) -> Shard:
        self._t("shards").insert(_row(shard)).execute()
        return shard

    def update_shard(self, shard: Shard) -> Shard:
        self._t("shards").update(_row(shard, exclude={"id"})).eq("id", shard.id).execute()
        return shard

    def list_shards(self, job_id: str) -> list[Shard]:
        resp = self._t("shards").select("*").eq("job_id", job_id).order("idx").execute()
        return [Shard.model_validate(r) for r in resp.data]

    def get_shard(self, shard_id: str) -> Shard:
        return self._one(self._t("shards").select("*").eq("id", shard_id).execute(), Shard, shard_id)

    def get_attempt(self, attempt_id: str) -> Attempt:
        return self._one(self._t("attempts").select("*").eq("id", attempt_id).execute(), Attempt, attempt_id)

    def create_attempt(self, attempt: Attempt) -> Attempt:
        self._t("attempts").insert(_row(attempt)).execute()
        return attempt

    def update_attempt(self, attempt: Attempt) -> Attempt:
        self._t("attempts").update(_row(attempt, exclude={"id"})).eq("id", attempt.id).execute()
        return attempt

    def finish_attempt(self, attempt: Attempt) -> bool:
        """Compare-and-set: only one process (Mac or cloud scheduler) wins the end of an attempt."""
        resp = (self._t("attempts").update(_row(attempt, exclude={"id"})).eq("id", attempt.id)
                .in_("status", ACTIVE_ATTEMPT_STATUSES).execute())
        return bool(resp.data)

    def list_attempts(self, shard_id: str) -> list[Attempt]:
        resp = self._t("attempts").select("*").eq("shard_id", shard_id).order("started_at").execute()
        return [Attempt.model_validate(r) for r in resp.data]

    def list_active_attempts(self) -> list[Attempt]:
        resp = self._t("attempts").select("*").in_("status", ACTIVE_ATTEMPT_STATUSES).execute()
        return [Attempt.model_validate(r) for r in resp.data]

    def usage_since(self, provider_id: str, since: datetime) -> tuple[float, float]:
        resp = (self._t("attempts").select("gpu_seconds,market_cost_usd").eq("provider_id", provider_id)
                .gte("started_at", since.isoformat()).execute())
        return (sum(float(r["gpu_seconds"]) for r in resp.data),
                sum(float(r["market_cost_usd"]) for r in resp.data))

    def list_attempts_since(self, since: datetime) -> list[Attempt]:
        resp = (self._t("attempts").select("*").gte("started_at", since.isoformat()).order("started_at")
                .limit(2000).execute())
        return [Attempt.model_validate(r) for r in resp.data]

    # events
    def add_event(self, event: Event) -> Event:
        self._t("events").insert(_row(event)).execute()
        return event

    def list_events(self, job_id: str, limit: int = 100) -> list[Event]:
        resp = (self._t("events").select("*").eq("job_id", job_id).order("ts", desc=True)
                .limit(limit).execute())
        return [Event.model_validate(r) for r in reversed(resp.data)]

    def list_events_since(self, type_: str, since: datetime) -> list[Event]:
        resp = (self._t("events").select("*").eq("type", type_).gte("ts", since.isoformat()).order("ts")
                .limit(2000).execute())
        return [Event.model_validate(r) for r in resp.data]

    # live telemetry (migration 0002)
    def add_metric(self, attempt_id: str, values: dict) -> None:
        self._t("metrics").insert({"attempt_id": attempt_id, **values}).execute()

    def add_log_lines(self, attempt_id: str, lines: list[str]) -> None:
        if lines:
            self._t("log_tail").insert([{"attempt_id": attempt_id, "line": ln[:2000]} for ln in lines]).execute()

    def prune_telemetry(self, before: datetime) -> None:
        for table in ("metrics", "log_tail"):
            self._t(table).delete().lt("ts", before.isoformat()).execute()

    # web push (migration 0003)
    def list_push_subscriptions(self) -> list[dict]:
        return self._t("push_subscriptions").select("endpoint,keys").execute().data

    def delete_push_subscription(self, endpoint: str) -> None:
        self._t("push_subscriptions").delete().eq("endpoint", endpoint).execute()

    # discovery (migration 0004)
    def add_finding(self, finding: dict) -> None:
        self._t("discovery_findings").insert(finding).execute()

    def list_findings(self, limit: int = 50) -> list[dict]:
        return self._t("discovery_findings").select("*").order("run_at", desc=True).limit(limit).execute().data

    def list_log_lines(self, attempt_id: str, limit: int = 200) -> list[str]:
        resp = (self._t("log_tail").select("line").eq("attempt_id", attempt_id).order("id", desc=True)
                .limit(limit).execute())
        return [r["line"] for r in reversed(resp.data)]
