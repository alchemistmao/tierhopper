-- TierHopper 0004 (applied 2026-10-01).
-- What the weekly discovery run found, including offers it rejected and why.

create table public.discovery_findings (
  id bigint generated always as identity primary key,
  run_at timestamptz not null default now(),
  name text not null,
  url text not null,
  summary text,
  confidence numeric not null default 0,
  official_api boolean not null default false,
  headless boolean not null default false,
  tos_ok boolean,
  status text not null check (status in ('qualified', 'needs_review', 'rejected')),
  reason text,
  provider_id text references public.providers (id) on delete set null
);
create index discovery_findings_run_idx on public.discovery_findings (run_at desc);
create index discovery_findings_provider_idx on public.discovery_findings (provider_id);
alter table public.discovery_findings enable row level security;
create policy owner_read on public.discovery_findings for select to authenticated using ((select public.is_owner()));
