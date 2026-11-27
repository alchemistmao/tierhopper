-- Each place that starts jobs (this computer, the cloud scheduler) records its last login check here:
-- {"this computer": {"ok": true, "detail": "...", "at": "..."}, "cloud": {...}}.
-- A provider is shown as active only when every place that checked can use it.
alter table public.providers add column if not exists health jsonb not null default '{}'::jsonb;
