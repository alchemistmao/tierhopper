# TierHopper — Build Plan

Each phase ends with something you can test yourself. Steps marked **[approval]** wait for your OK
(money, cloud resources, schema, deploy, `git push`). Steps marked **[you]** are manual actions.

## Phase 0 — Foundations (local only)
- `git init` on `main`, `.gitignore`, gitleaks pre-commit, `pyproject.toml` (uv), ruff, pytest.
- `providers.yaml` seeded with Modal, Kaggle, Lightning AI, RunPod (URLs, requirements, login commands, status `pending`).
- `credentials.py` (Keychain via `keyring`), `redact.py`, `spec.py` (parse + validate `tierhopper.yaml`).
- In-memory store for tests.
- **Test:** `pytest` green; `tierhopper spec check examples/hello-gpu/tierhopper.yaml` passes.

## Phase 1 — MCP skeleton + Modal and Kaggle, end to end
- Supabase project in `sa-east-1` **[approval]** + first migration (providers, credit_snapshots, projects, jobs, shards, attempts, events) **[approval]**.
- Adapter interface; Modal adapter (image from spec, GPU function); Kaggle adapter (`kernels push/status/output`).
- `tierhopper connect modal|kaggle` (official login / pasted key → Keychain → validation → 30-s GPU test → `active`).
- FastMCP server with `submit_job`, `credits_status` and a minimal `job_status` (needed to see the job finish).
- Simple routing (expiring credit first, hard filters only).
- **[you]** Modal and Kaggle accounts, Kaggle phone verification.
- **Test:** from Claude Code, `submit_job examples/hello-gpu` runs on Modal and, with Modal disabled, on Kaggle; `credits_status` shows both.

## Phase 2 — Checkpoints, results and hops
- R2 bucket **[you + approval]**, presigned URLs, `runner` package (heartbeat, progress, checkpoint sync, log shipping).
- Control plane on Modal **[approval]**: scheduler tick (cron), ingest endpoint; Modal Secrets **[approval]**.
- Migration: metrics, log_tail **[approval]**.
- Hops on interruption (resume from checkpoint on next provider), preemptive hop before Kaggle's session limit.
- `fetch_results` (including partial), full `job_status`, thin Claude Code skill.
- **Test:** a 20-min counter job killed mid-run on Modal resumes on Kaggle from the checkpoint and finishes; results downloaded.

## Phase 3 — Smart scheduling
- Learned ranking (`provider_stats`), history-based estimates, approval thresholds, `approvals` table **[approval]**.
- Automatic sharding with `max_parallel`.
- Lightning AI adapter + connect **[you: account]**.
- **Test:** an 8-shard job spreads over Modal, Kaggle and Lightning in parallel; an over-threshold job waits for approval.

## Phase 4 — WhatsApp and onboarding
- CallMeBot setup **[you]**, notifications, signed action links + confirmation page (action endpoint).
- Integration cards, `list_providers`, `connect_provider`.
- **Test:** you approve/pause a job from WhatsApp; a card for a `pending` provider arrives and `connect` activates it.

## Phase 5 — Models and paid fallback
- `serve: vllm` ephemeral endpoint in the runner; weight cache (Modal Volume, HF cache).
- RunPod adapter with US$ 20/month hard cap, approval-only **[approval before any spend]**.
- **Test:** a small VLM batch runs through vLLM and tears down; RunPod appears only after an approval.

## Phase 6 — Dashboard core
- Next.js app, Supabase Auth (single user), 3 themes, Home (overall bar + project rows), Project detail (Summary, Journey, Stops, buttons), Realtime.
- Railway service **[approval]** and deploy **[approval]**.
- **Test:** open the dashboard on the Mac and on the phone, watch a job progress live and hop.

## Phase 7 — Dashboard advanced
- Technical details (live logs, GPU metrics, Job replay), Hop map tab, Providers tab with unlockable cards, savings meter + streaks, ⌘K with natural language, sounds, PWA + Web Push.
- **Test:** replay a finished job event by event; run a job from ⌘K in plain English; get a push on "Needs your OK".

## Phase 8 — Discovery and reports
- Weekly discovery on Modal (curated sources + web search, Claude API validation, confidence score).
- `usage_report`, Reports tab, weekly WhatsApp summary.
- **Test:** a discovery run produces scored findings and at most a few cards; Monday summary arrives.

## Phase 9 — Going public (later, your call)
- README, CONTRIBUTING for `providers.yaml`, license, secret audit, then make the repo public **[approval]**.
