-- Providers that fail the daily health check are marked 'unavailable' and skipped by routing
-- until they pass again (or the user reconnects them).
alter table public.providers drop constraint if exists providers_status_check;
alter table public.providers add constraint providers_status_check
  check (status in ('pending', 'pending_adapter', 'active', 'unavailable', 'disabled'));
