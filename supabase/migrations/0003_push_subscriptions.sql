-- TierHopper 0003 (applied 2026-10-01).
-- Browser push subscriptions of the dashboard owner. Server-side only: RLS on, no policies.

create table public.push_subscriptions (
  id bigint generated always as identity primary key,
  endpoint text not null unique,
  keys jsonb not null,
  created_at timestamptz not null default now()
);
alter table public.push_subscriptions enable row level security;
