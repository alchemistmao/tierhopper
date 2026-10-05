"""Domain records shared by the store, scheduler, adapters and MCP server."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class ProviderStatus(StrEnum):
    PENDING = "pending"
    PENDING_ADAPTER = "pending_adapter"
    ACTIVE = "active"
    DISABLED = "disabled"


class ProviderKind(StrEnum):
    FREE = "free"
    CREDIT_PROGRAM = "credit_program"
    PAID = "paid"


class JobStatus(StrEnum):
    QUEUED = "queued"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    PAUSED = "paused"
    NEEDS_ATTENTION = "needs_attention"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LOST = "lost"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {AttemptStatus.SUCCEEDED, AttemptStatus.FAILED, AttemptStatus.LOST,
                        AttemptStatus.CANCELLED}


class EndReason(StrEnum):
    COMPLETED = "completed"
    CREDIT_EXHAUSTED = "credit_exhausted"
    SESSION_LIMIT = "session_limit"
    PREEMPTED = "preempted"
    ERROR = "error"
    CANCELLED = "cancelled"


class CreditUnit(StrEnum):
    USD = "usd"
    GPU_HOURS = "gpu_hours"


class CreditSource(StrEnum):
    API = "api"
    ESTIMATE = "estimate"
    MANUAL = "manual"


class GpuOffer(BaseModel):
    type: str
    vram_gb: float
    count: int = 1
    usd_per_hour: float = 0.0  # market (on-demand) price, used for savings and credit estimates


class CreditModel(BaseModel):
    type: str  # monthly_usd | weekly_hours | one_time_usd | pay_as_you_go
    amount: float = 0.0
    resets: str | None = None  # monthly | weekly | None
    reset_weekday: int | None = None  # 0=Mon..6=Sun (weekly)
    notes: str | None = None


class Requirements(BaseModel):
    email: bool = True
    phone: bool = False
    card: bool = False
    notes: str | None = None


class Provider(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    status: ProviderStatus = ProviderStatus.PENDING
    adapter: str | None = None
    signup_url: str | None = None
    api_key_url: str | None = None
    login_command: str | None = None
    requirements: Requirements = Field(default_factory=Requirements)
    credit_model: CreditModel
    gpu_catalog: list[GpuOffer] = Field(default_factory=list)
    max_session_hours: float | None = None
    max_concurrent: int | None = None  # simultaneous attempts; static, lives in providers.yaml
    spend_cap_usd: float = 0.0
    validation_test: str | None = None
    source: str = "seed"
    confidence: float = 1.0
    notes: str | None = None
    updated_at: datetime = Field(default_factory=now)


class CreditSnapshot(BaseModel):
    id: str = Field(default_factory=new_id)
    provider_id: str
    measured_at: datetime = Field(default_factory=now)
    remaining: float
    unit: CreditUnit
    source: CreditSource
    expires_at: datetime | None = None


class Project(BaseModel):
    id: str = Field(default_factory=new_id)
    slug: str
    name: str
    created_at: datetime = Field(default_factory=now)


class Job(BaseModel):
    id: str = Field(default_factory=new_id)
    project_id: str
    name: str
    spec: dict[str, Any]
    spec_hash: str
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    estimate: dict[str, Any] = Field(default_factory=dict)
    results_prefix: str | None = None
    created_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Shard(BaseModel):
    id: str = Field(default_factory=new_id)
    job_id: str
    idx: int = 0
    total: int = 1
    items: list[str] = Field(default_factory=list)
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    checkpoint_key: str | None = None
    checkpoint_at: datetime | None = None


class Attempt(BaseModel):
    id: str = Field(default_factory=new_id)
    shard_id: str
    provider_id: str
    gpu_type: str
    external_ref: dict[str, Any] = Field(default_factory=dict)
    status: AttemptStatus = AttemptStatus.SUBMITTED
    end_reason: EndReason | None = None
    started_at: datetime = Field(default_factory=now)
    ended_at: datetime | None = None
    progress_start: float = 0.0
    progress_end: float | None = None
    gpu_seconds: float = 0.0
    cost_usd: float = 0.0
    market_cost_usd: float = 0.0
    message: str | None = None


class Event(BaseModel):
    id: str = Field(default_factory=new_id)
    job_id: str
    shard_id: str | None = None
    attempt_id: str | None = None
    ts: datetime = Field(default_factory=now)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
