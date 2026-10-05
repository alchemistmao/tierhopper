"""Local store: one SQLite file (default ~/.tierhopper/state.db). No cloud account needed.

Each record is kept as a JSON document next to the few columns we filter on. Safe for the MCP
server and the CLI running at the same time (WAL mode, short transactions).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from tierhopper.models import Attempt, CreditSnapshot, Event, Job, Project, Provider, Shard, now
from tierhopper.store.base import NotFound

M = TypeVar("M", bound=BaseModel)
ACTIVE = ("submitted", "queued", "running")

SCHEMA = """
create table if not exists providers (id text primary key, status text, doc text not null);
create table if not exists credits (id text primary key, provider_id text, measured_at text, doc text not null);
create table if not exists projects (id text primary key, slug text unique, doc text not null);
create table if not exists jobs (id text primary key, status text, created_at text, doc text not null);
create table if not exists shards (id text primary key, job_id text, idx integer, doc text not null);
create table if not exists attempts (id text primary key, shard_id text, provider_id text, status text,
                                     started_at text, doc text not null);
create table if not exists events (id text primary key, job_id text, type text, ts text, doc text not null);
create table if not exists metrics (n integer primary key autoincrement, attempt_id text, ts text, doc text not null);
create table if not exists log_lines (n integer primary key autoincrement, attempt_id text, ts text, line text);
create table if not exists findings (n integer primary key autoincrement, run_at text, doc text not null);
create index if not exists credits_provider on credits (provider_id, measured_at);
create index if not exists shards_job on shards (job_id, idx);
create index if not exists attempts_shard on attempts (shard_id, started_at);
create index if not exists attempts_status on attempts (status);
create index if not exists events_job on events (job_id, ts);
create index if not exists log_attempt on log_lines (attempt_id, n);
"""


def _iso(value: datetime) -> str:
    return value.isoformat()


class SQLiteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma busy_timeout=30000")
        self.db.executescript(SCHEMA)

    # ---- helpers -------------------------------------------------------------------------------
    def _run(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    @staticmethod
    def _doc(model: BaseModel) -> str:
        return model.model_dump_json()

    @staticmethod
    def _load(model: type[M], rows: list[tuple]) -> list[M]:
        return [model.model_validate_json(r[0]) for r in rows]

    def _one(self, model: type[M], sql: str, key: str) -> M:
        rows = self._run(sql, (key,))
        if not rows:
            raise NotFound(key)
        return model.model_validate_json(rows[0][0])

    # ---- providers -----------------------------------------------------------------------------
    def upsert_provider(self, provider: Provider) -> Provider:
        self._run("insert into providers (id, status, doc) values (?, ?, ?) on conflict(id) do update set "
                  "status = excluded.status, doc = excluded.doc",
                  (provider.id, provider.status.value, self._doc(provider)))
        return provider

    def get_provider(self, provider_id: str) -> Provider:
        return self._one(Provider, "select doc from providers where id = ?", provider_id)

    def list_providers(self, status: str | None = None) -> list[Provider]:
        if status:
            return self._load(Provider, self._run("select doc from providers where status = ? order by id", (status,)))
        return self._load(Provider, self._run("select doc from providers order by id"))

    # ---- credits -------------------------------------------------------------------------------
    def add_credit_snapshot(self, snap: CreditSnapshot) -> CreditSnapshot:
        self._run("insert into credits (id, provider_id, measured_at, doc) values (?, ?, ?, ?)",
                  (snap.id, snap.provider_id, _iso(snap.measured_at), self._doc(snap)))
        return snap

    def latest_credit(self, provider_id: str) -> CreditSnapshot | None:
        rows = self._run("select doc from credits where provider_id = ? order by measured_at desc limit 1",
                         (provider_id,))
        return CreditSnapshot.model_validate_json(rows[0][0]) if rows else None

    # ---- projects / jobs -----------------------------------------------------------------------
    def get_or_create_project(self, slug: str) -> Project:
        rows = self._run("select doc from projects where slug = ?", (slug,))
        if rows:
            return Project.model_validate_json(rows[0][0])
        project = Project(slug=slug, name=slug)
        self._run("insert or ignore into projects (id, slug, doc) values (?, ?, ?)",
                  (project.id, slug, self._doc(project)))
        return self._one(Project, "select doc from projects where slug = ?", slug)

    def get_project(self, project_id: str) -> Project:
        return self._one(Project, "select doc from projects where id = ?", project_id)

    def create_job(self, job: Job) -> Job:
        self._run("insert into jobs (id, status, created_at, doc) values (?, ?, ?, ?)",
                  (job.id, job.status.value, _iso(job.created_at), self._doc(job)))
        return job

    def update_job(self, job: Job) -> Job:
        self._run("update jobs set status = ?, doc = ? where id = ?", (job.status.value, self._doc(job), job.id))
        return job

    def get_job(self, job_id: str) -> Job:
        return self._one(Job, "select doc from jobs where id = ?", job_id)

    def list_jobs(self, status: str | None = None, limit: int = 50) -> list[Job]:
        if status:
            rows = self._run("select doc from jobs where status = ? order by created_at desc limit ?", (status, limit))
        else:
            rows = self._run("select doc from jobs order by created_at desc limit ?", (limit,))
        return self._load(Job, rows)

    # ---- shards / attempts ---------------------------------------------------------------------
    def create_shard(self, shard: Shard) -> Shard:
        self._run("insert into shards (id, job_id, idx, doc) values (?, ?, ?, ?)",
                  (shard.id, shard.job_id, shard.idx, self._doc(shard)))
        return shard

    def update_shard(self, shard: Shard) -> Shard:
        self._run("update shards set doc = ? where id = ?", (self._doc(shard), shard.id))
        return shard

    def list_shards(self, job_id: str) -> list[Shard]:
        return self._load(Shard, self._run("select doc from shards where job_id = ? order by idx", (job_id,)))

    def get_shard(self, shard_id: str) -> Shard:
        return self._one(Shard, "select doc from shards where id = ?", shard_id)

    def get_attempt(self, attempt_id: str) -> Attempt:
        return self._one(Attempt, "select doc from attempts where id = ?", attempt_id)

    def create_attempt(self, attempt: Attempt) -> Attempt:
        self._run("insert into attempts (id, shard_id, provider_id, status, started_at, doc) values (?, ?, ?, ?, ?, ?)",
                  (attempt.id, attempt.shard_id, attempt.provider_id, attempt.status.value,
                   _iso(attempt.started_at), self._doc(attempt)))
        return attempt

    def update_attempt(self, attempt: Attempt) -> Attempt:
        self._run("update attempts set status = ?, doc = ? where id = ?",
                  (attempt.status.value, self._doc(attempt), attempt.id))
        return attempt

    def finish_attempt(self, attempt: Attempt) -> bool:
        """Compare-and-set: only one process wins the end of an attempt."""
        with self._lock:
            cur = self.db.execute(
                f"update attempts set status = ?, doc = ? where id = ? and status in ({','.join('?' * len(ACTIVE))})",
                (attempt.status.value, self._doc(attempt), attempt.id, *ACTIVE))
            return cur.rowcount > 0

    def list_attempts(self, shard_id: str) -> list[Attempt]:
        return self._load(Attempt, self._run("select doc from attempts where shard_id = ? order by started_at",
                                             (shard_id,)))

    def list_active_attempts(self) -> list[Attempt]:
        marks = ",".join("?" * len(ACTIVE))
        return self._load(Attempt, self._run(f"select doc from attempts where status in ({marks})", ACTIVE))

    def usage_since(self, provider_id: str, since: datetime) -> tuple[float, float]:
        rows = self._load(Attempt, self._run("select doc from attempts where provider_id = ? and started_at >= ?",
                                             (provider_id, _iso(since))))
        return sum(a.gpu_seconds for a in rows), sum(a.market_cost_usd for a in rows)

    def list_attempts_since(self, since: datetime) -> list[Attempt]:
        return self._load(Attempt, self._run("select doc from attempts where started_at >= ? order by started_at",
                                             (_iso(since),)))

    # ---- events --------------------------------------------------------------------------------
    def add_event(self, event: Event) -> Event:
        self._run("insert into events (id, job_id, type, ts, doc) values (?, ?, ?, ?, ?)",
                  (event.id, event.job_id, event.type, _iso(event.ts), self._doc(event)))
        return event

    def list_events(self, job_id: str, limit: int = 100) -> list[Event]:
        rows = self._run("select doc from (select doc, ts from events where job_id = ? order by ts desc limit ?) "
                         "order by ts", (job_id, limit))
        return self._load(Event, rows)

    def list_events_since(self, type_: str, since: datetime) -> list[Event]:
        return self._load(Event, self._run("select doc from events where type = ? and ts >= ? order by ts",
                                           (type_, _iso(since))))

    # ---- telemetry -----------------------------------------------------------------------------
    def add_metric(self, attempt_id: str, values: dict) -> None:
        self._run("insert into metrics (attempt_id, ts, doc) values (?, ?, ?)",
                  (attempt_id, _iso(now()), json.dumps(values)))

    def add_log_lines(self, attempt_id: str, lines: list[str]) -> None:
        stamp = _iso(now())
        with self._lock:
            self.db.executemany("insert into log_lines (attempt_id, ts, line) values (?, ?, ?)",
                                [(attempt_id, stamp, line[:2000]) for line in lines])

    def list_log_lines(self, attempt_id: str, limit: int = 200) -> list[str]:
        rows = self._run("select line from (select line, n from log_lines where attempt_id = ? "
                         "order by n desc limit ?) order by n", (attempt_id, limit))
        return [r[0] for r in rows]

    def prune_telemetry(self, before: datetime) -> None:
        self._run("delete from metrics where ts < ?", (_iso(before),))
        self._run("delete from log_lines where ts < ?", (_iso(before),))

    # ---- discovery / push (cloud features; kept so the interface is complete) -------------------
    def add_finding(self, finding: dict) -> None:
        self._run("insert into findings (run_at, doc) values (?, ?)", (_iso(now()), json.dumps(finding)))

    def list_findings(self, limit: int = 50) -> list[dict]:
        return [json.loads(r[0]) for r in self._run("select doc from findings order by n desc limit ?", (limit,))]

    def list_push_subscriptions(self) -> list[dict]:
        return []

    def delete_push_subscription(self, endpoint: str) -> None:
        return None
