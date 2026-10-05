# TierHopper — Tasks

Legend: `[x]` done · `[~]` in progress · `[ ]` todo · **[you]** needs the owner · **[approval]** needs OK

## Phase 0 — Foundations
- [x] git init on main, .gitignore, pyproject (uv), ruff, pytest
- [x] providers.yaml seed (Modal, Kaggle, Lightning AI, RunPod)
- [x] credentials.py (Keychain), redact.py
- [x] spec.py + `tierhopper spec check`
- [x] In-memory store + tests green

## Phase 1 — MCP skeleton + Modal and Kaggle end to end
- [x] **[approval]** Supabase project `tierhopper` in sa-east-1
- [x] **[approval]** Migration 0001_core applied
- [x] Adapter interface
- [x] Modal adapter
- [x] Kaggle adapter
- [x] Supabase store
- [x] `tierhopper connect modal|kaggle` (login → Keychain → validate → GPU smoke test → active)
- [x] FastMCP server: submit_job, credits_status, job_status, list_providers
- [x] Simple routing (expiring credit first + hard filters)
- [x] **[you]** Modal account (+ card)
- [x] **[you]** `tierhopper config supabase` (secret key → Keychain)
- [x] `tierhopper connect modal` — active, T4 smoke test passed
- [x] **[you]** Kaggle account + phone verification + `tierhopper connect kaggle` — active, T4 smoke test passed
- [x] Register MCP server in Claude Code (user scope)
- [x] E2E: hello-gpu via MCP tools on Modal (chosen: credit expires first) — done in 18 s
- [x] E2E: hello-gpu on Kaggle (Modal disabled) — done on 2x T4
- [x] Thin Claude Code skill (skill/SKILL.md)

## Phase 2 — Checkpoints, results and hops
- [x] **[you]** Cloudflare account + R2 bucket `tierhopper` + scoped API token
- [x] Presigned URLs + runner (heartbeat, progress, checkpoint sync, results/log upload, deadline stop)
- [x] **[approval]** Control plane on Modal (scheduler every 5 min + ingest endpoint) + Modal Secret
- [ ] **[approval]** Migration: metrics, log_tail (deferred to Phase 6 — dashboard is the only consumer)
- [x] Hops on interruption: live test stopped at 60% (session limit), cloud scheduler resumed from step 24, done
- [x] fetch_results (incl. partial) + MCP tool, job_status with checkpoints
- [ ] Install skill in Claude Code

## Phase 3 — Smart scheduling
- [x] Learned ranking (failure rate, queue time, real speed per GPU) + SPEC §5.2 score
- [x] History-based estimates; approval above 12 GPU-hours (`tierhopper approve|deny`)
- [x] Paid provider only after approval, asked only when free providers can't take the job
- [x] Automatic sharding (`shard.over` / `shard.count`, `max_parallel`) + per-provider concurrency limits
- [x] pause / resume
- [x] Lightning AI adapter (image job + R2 package) + `tierhopper connect lightning`
- [x] **[you]** Lightning account + `tierhopper connect lightning` — active, GPU test passed
- [x] Redeploy control plane with Phase 3 code

## Phase 4 — WhatsApp and onboarding
- [x] CallMeBot notifier (approval needed, done, stopped, weekly summary) — all text redacted
- [x] Signed action links (approve / deny / pause / resume / status / report); GET = confirmation page, POST acts
- [x] Control plane: one web app (/ingest, /action) + Monday 09:00 weekly summary cron
- [x] Integration cards (`tierhopper card <id> [--send]`), MCP connect_provider + usage_report
- [x] **[you]** CallMeBot activation + `tierhopper config whatsapp` — test message received
- [x] Redeploy control plane (ingest URL moved to /ingest)

## Phase 5 — Models and paid fallback
- [x] Ephemeral model endpoint (`serve: {engine: vllm, model: ...}`): runner starts vLLM, waits for health, exposes OPENAI_BASE_URL, stops it at the end
- [x] Weight cache: Modal Volume `tierhopper-hf-cache` at ~/.cache/huggingface (created by `control deploy`)
- [x] RunPod adapter: pod idles after the runner, scheduler terminates it; wall-time billing; US$ 20/month cap checked before proposing
- [x] **[you]** RunPod account + US$ 10 credit + connect — active (A40 test passed; cost ~US$ 0.15 due to a since-fixed pod-termination bug)
- [ ] Live test: small VLM batch through vLLM on Modal (after the redeploy approval)

## Phase 6 — Dashboard core
- [x] Next.js 16 app (dashboard/): Home (overall bar, project rows, plain status, free credit, savings), Project detail (Summary, Journey, Stops, Pause/Resume, Get results so far, Technical details), Hop map, Providers (unlockable cards)
- [x] 3 themes (Mission default, CRT, Light), sounds toggle, ⌘K palette (navigation/actions), keyboard j/k/enter, mobile layout, PWA manifest
- [x] Auth: magic link only to the allowed e-mail; data read server-side with the secret key; actions go through the control plane API
- [x] Verified locally with real data (desktop + 375 px), production build passes
- [x] Redeploy control plane (web app /ingest /action /api)
- [x] Railway: dashboard deployed (project tierhopper)
- [x] Migration 0002 applied: owner RLS, Realtime, metrics, log_tail

## Brand
- [x] Logo: The Frog (icon, header with hover hop animation, login illustration, README)

## Next (need the owner's go-ahead)
- [x] Phase 7 code: natural-language ⌘K (Claude proposes, you confirm), rerun, job replay, Web Push (bell)
- [x] **[you]** Claude API key saved (live ⌘K test passed against the cloud)
- [x] Migration 0003 applied, control plane and dashboard redeployed
- [ ] **[you]** Try in the dashboard: ⌘K sentence, replay, bell (push)
- [x] Phase 8 code: weekly discovery (Claude + web search, rules enforced in code), Reports tab, "Found by discovery" cards
- [x] Migration 0004 applied; control plane and dashboard redeployed
- [x] First live discovery run: 2 offers reported, both rejected (API not confirmed), nothing added
- [x] Control plane redeployed with the discovery fix and the six fixes from the first real job
- [ ] Live test: small VLM batch through vLLM
- [ ] Private GitHub repo + first push
