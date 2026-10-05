-- TierHopper 0002 (applied 2026-09-30).
-- Lets the dashboard owner read state directly and receive Supabase Realtime updates,
-- replacing the 5-second polling; adds live GPU metrics and a short log tail.

-- Read-only access for the single dashboard owner (writes stay service-role only).
create or replace function public.is_owner() returns boolean
language sql stable security invoker set search_path = '' as $$
  select coalesce((auth.jwt() ->> 'email') = 'you@example.com'  -- replace with the dashboard owner's e-mail before applying, false)
$$;

create policy owner_read on public.providers for select to authenticated using ((select public.is_owner()));
create policy owner_read on public.credit_snapshots for select to authenticated using ((select public.is_owner()));
create policy owner_read on public.projects for select to authenticated using ((select public.is_owner()));
create policy owner_read on public.jobs for select to authenticated using ((select public.is_owner()));
create policy owner_read on public.shards for select to authenticated using ((select public.is_owner()));
create policy owner_read on public.attempts for select to authenticated using ((select public.is_owner()));
create policy owner_read on public.events for select to authenticated using ((select public.is_owner()));

-- Live GPU metrics from runner heartbeats (pruned after 14 days).
create table public.metrics (
  id bigint generated always as identity primary key,
  attempt_id uuid not null references public.attempts (id) on delete cascade,
  ts timestamptz not null default now(),
  gpu_util numeric,
  vram_used_mb integer,
  vram_total_mb integer,
  progress numeric
);
create index metrics_attempt_ts_idx on public.metrics (attempt_id, ts);
alter table public.metrics enable row level security;
create policy owner_read on public.metrics for select to authenticated using ((select public.is_owner()));

-- Last lines of each attempt's output (full logs live in R2), pruned after 14 days.
create table public.log_tail (
  id bigint generated always as identity primary key,
  attempt_id uuid not null references public.attempts (id) on delete cascade,
  ts timestamptz not null default now(),
  line text not null
);
create index log_tail_attempt_ts_idx on public.log_tail (attempt_id, ts);
alter table public.log_tail enable row level security;
create policy owner_read on public.log_tail for select to authenticated using ((select public.is_owner()));

-- Realtime for the screens that update live.
alter publication supabase_realtime add table public.jobs, public.attempts, public.events, public.metrics;
