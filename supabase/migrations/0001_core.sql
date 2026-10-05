-- TierHopper core schema (Phase 1).
-- RLS is enabled on every table with no policies: only the service role (MCP server, control plane)
-- can read/write. Dashboard policies for the authenticated owner arrive in Phase 6.

create table public.providers (
  id text primary key,
  name text not null,
  kind text not null check (kind in ('free', 'credit_program', 'paid')),
  status text not null default 'pending'
    check (status in ('pending', 'pending_adapter', 'active', 'disabled')),
  adapter text,
  signup_url text,
  api_key_url text,
  login_command text,
  requirements jsonb not null default '{}'::jsonb,
  credit_model jsonb not null,
  gpu_catalog jsonb not null default '[]'::jsonb,
  max_session_hours numeric,
  spend_cap_usd numeric not null default 0,
  validation_test text,
  source text not null default 'seed',
  confidence numeric not null default 1,
  notes text,
  updated_at timestamptz not null default now()
);

create table public.credit_snapshots (
  id uuid primary key default gen_random_uuid(),
  provider_id text not null references public.providers (id) on delete cascade,
  measured_at timestamptz not null default now(),
  remaining numeric not null,
  unit text not null check (unit in ('usd', 'gpu_hours')),
  source text not null check (source in ('api', 'estimate', 'manual')),
  expires_at timestamptz
);
create index credit_snapshots_provider_measured_idx on public.credit_snapshots (provider_id, measured_at desc);

create table public.projects (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  name text not null,
  created_at timestamptz not null default now()
);

create table public.jobs (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references public.projects (id) on delete cascade,
  name text not null,
  spec jsonb not null,
  spec_hash text not null,
  status text not null default 'queued' check (status in (
    'queued', 'awaiting_approval', 'running', 'paused', 'needs_attention', 'done', 'failed', 'cancelled')),
  progress numeric not null default 0 check (progress between 0 and 1),
  estimate jsonb not null default '{}'::jsonb,
  results_prefix text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);
create index jobs_project_idx on public.jobs (project_id);
create index jobs_status_created_idx on public.jobs (status, created_at desc);
create index jobs_spec_hash_idx on public.jobs (spec_hash);

create table public.shards (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.jobs (id) on delete cascade,
  idx integer not null default 0,
  total integer not null default 1,
  items jsonb not null default '[]'::jsonb,
  status text not null default 'queued',
  progress numeric not null default 0,
  checkpoint_key text,
  checkpoint_at timestamptz,
  unique (job_id, idx)
);

create table public.attempts (
  id uuid primary key default gen_random_uuid(),
  shard_id uuid not null references public.shards (id) on delete cascade,
  provider_id text not null references public.providers (id),
  gpu_type text not null,
  external_ref jsonb not null default '{}'::jsonb,
  status text not null default 'submitted' check (status in (
    'submitted', 'queued', 'running', 'succeeded', 'failed', 'lost', 'cancelled')),
  end_reason text check (end_reason in (
    'completed', 'credit_exhausted', 'session_limit', 'preempted', 'error', 'cancelled')),
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  progress_start numeric not null default 0,
  progress_end numeric,
  gpu_seconds numeric not null default 0,
  cost_usd numeric not null default 0,
  market_cost_usd numeric not null default 0,
  message text
);
create index attempts_shard_idx on public.attempts (shard_id);
create index attempts_provider_started_idx on public.attempts (provider_id, started_at);
create index attempts_active_idx on public.attempts (status)
  where status in ('submitted', 'queued', 'running');

create table public.events (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.jobs (id) on delete cascade,
  shard_id uuid references public.shards (id) on delete cascade,
  attempt_id uuid references public.attempts (id) on delete cascade,
  ts timestamptz not null default now(),
  type text not null,
  payload jsonb not null default '{}'::jsonb
);
create index events_job_ts_idx on public.events (job_id, ts);
create index events_shard_idx on public.events (shard_id);
create index events_attempt_idx on public.events (attempt_id);

alter table public.providers enable row level security;
alter table public.credit_snapshots enable row level security;
alter table public.projects enable row level security;
alter table public.jobs enable row level security;
alter table public.shards enable row level security;
alter table public.attempts enable row level security;
alter table public.events enable row level security;
