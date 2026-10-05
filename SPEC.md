# TierHopper — Specification

> Status: draft for approval · 2026-09-30
> Runs GPU jobs on free cloud tiers in rotation: burn one provider's free credit, then hop to the next.
> Used from Claude Code through an MCP server + a thin skill, with a web dashboard for humans.

---

## 1. Decisions (questionnaire summary)

| # | Topic | Decision |
|---|-------|----------|
| 1 | Routing | **Fastest free GPU first** (changed 2026-10-02); expiring credit breaks ties |
| 2 | WhatsApp channel | CallMeBot (outbound only) |
| 3 | WhatsApp interaction | Simple commands (status / pause / approve / report) via signed action links |
| 4 | Estimates | Estimate from history; ask for approval only above a threshold |
| 5 | Storage | Cloudflare R2 |
| 6 | Job format | Declarative `tierhopper.yaml` compiled per provider |
| 7 | Discovery sources | Curated sources + web search |
| 8 | Offer validation | Confidence score + automatic official-API and ToS check before a card is sent |
| 9 | Credit reading | Provider API when available, local estimate otherwise |
| 10 | Reports | HTML dashboard + short weekly WhatsApp summary |
| 11 | Interruptions | Resume from last checkpoint on the next available provider |
| 12 | Parallelism | Automatic sharding across providers |
| 13 | DocBench Lab | Generic only (no preset) |
| 14 | Open models | Ephemeral vLLM endpoint inside the job |
| 15 | Weight cache | Provider persistent volume + Hugging Face cache |
| 16 | Paid provider (RunPod) | Only after explicit WhatsApp/dashboard approval, only when free tiers are exhausted |
| 17 | Scheduler host | Scheduled function on Modal |
| 18 | State | Supabase, region `sa-east-1` |
| 19 | Ranking | Learned from history (queue time, failure rate, real speed per GPU) |
| 20 | Publishing | Private now, public later (`github.com/alchemistmao/tierhopper`) |
| 21 | Dashboard platform | Next.js + Supabase, deployed on Railway |
| 22 | Aesthetics | 3 user-selectable themes: **Mission Control** (default), Retro CRT, Minimal Light |
| 23 | Main view | Simple project list with plain-language status; hop map in a secondary tab |
| 24 | Commands | ⌘K command palette with natural language (Claude API) |
| 25 | Real time | Streaming logs + live GPU metrics via Supabase Realtime |
| 26 | Economics | Savings meter with streaks and milestones |
| 27 | Debugging | Job replay timeline (inside Technical details) |
| 28 | Provider onboarding (UI) | Unlockable provider cards with checklist and live test |
| 29 | Mobile | Installable PWA with push notifications |
| 30 | Micro-interactions | Subtle animations + optional Web Audio sounds |
| 31 | Public page | None — stats stay private |
| 32 | Layout | Fixed, carefully designed layout |

---

## 2. Architecture

```
            Claude Code                         Browser / phone (PWA)
                │  MCP (stdio)                          │ HTTPS + Realtime
                ▼                                       ▼
   ┌──────────────────────────┐              ┌───────────────────────┐
   │ tierhopper MCP server    │              │ Dashboard (Next.js)   │
   │ + CLI (Mac, local)       │              │ Railway               │
   └────────────┬─────────────┘              └──────────┬────────────┘
                │ supabase-py (service key)             │ supabase-js (user JWT + RLS)
                ▼                                       ▼
          ┌───────────────────────────────────────────────────┐
          │ Supabase sa-east-1: Postgres + Realtime + Auth    │
          └───────────────────────────────────────────────────┘
                ▲                           ▲
                │ service key (Modal Secret)│
   ┌────────────┴──────────────────────────────────────────┐
   │ Modal control plane (CPU only)                        │
   │  • scheduler tick (cron, every 5 min)                 │
   │  • discovery (cron, weekly)                           │
   │  • ingest endpoint (heartbeats, logs, metrics)        │
   │  • action endpoint (signed WhatsApp links)            │
   └───────┬──────────────────────┬────────────────────────┘
           │ adapters             │ CallMeBot / Web Push
           ▼                      ▼
   Modal GPU · Kaggle · Lightning · RunPod        WhatsApp / phone
           │
           ▼  presigned URLs only
     Cloudflare R2 (checkpoints, results, full logs)
```

### 2.1 Components

| Component | Language / host | Responsibility |
|-----------|-----------------|----------------|
| `tierhopper` core library | Python 3.12 | Spec parsing, routing, adapters, state access, R2 presigning, credentials |
| MCP server | Python, FastMCP, stdio, Mac | Exposes the 7 tools to Claude Code |
| CLI | Python, Typer, Mac | `tierhopper connect|approve|status|pause|report|mcp` |
| Control plane | Python on Modal (CPU) | Scheduler tick, discovery, ingest + action endpoints |
| Runner | Python, injected into every job | Heartbeats, progress, checkpoint sync, vLLM lifecycle, metrics |
| Dashboard | Next.js (App Router) + TypeScript on Railway | Human UI, ⌘K, PWA, push |
| Skill | Markdown in `skill/` | Teaches Claude Code when/how to send a job to TierHopper |

### 2.2 Dispatch model

- `submit_job` writes the job to Supabase and triggers an immediate dispatch attempt from the Mac (fast path).
- The Modal scheduler tick (every 5 min) is the source of truth: it reconciles attempts, detects
  interruptions, migrates, dispatches queued work, snapshots credits and sends notifications.
  It runs even with the Mac off.
- Every state change is appended to `events`; the dashboard subscribes to it via Realtime.

---

## 3. Provider adapters

Only providers with an official API/SDK for headless execution. Google Colab is excluded.

| Provider | Kind | Execution | Credit reading | Notes |
|----------|------|-----------|----------------|-------|
| Modal | free monthly credit (card required for GPU) | `modal` SDK: per-attempt deployed app, `spawn` + `FunctionCall.from_id` polling | Local estimate (no billing API on Starter) | Also hosts the control plane; weights cached in a Modal Volume |
| Kaggle | free weekly GPU quota | `kaggle kernels push` with generated `kernel-metadata.json` + script (job embedded); poll `kernels status`; outputs via `kernels output` | Quota API (`quota_view`: used, total, refresh time) | Phone verification (manual) needed for GPU + internet; session hard limit → checkpoint + preemptive hop |
| Lightning AI | free monthly credit | `lightning_sdk` Jobs/Studios API | SDK if exposed, else estimate | Login via `lightning login` |
| RunPod | **paid**, last resort | REST/GraphQL API, on-demand pod, auto-terminate | API balance | Only with explicit approval; hard cap US$ 20/month |

All quota numbers, limits and URLs live in `providers.yaml` / the `providers` table and are
**re-verified at `connect` time and in the weekly discovery run**, never hard-coded.

### 3.1 Adapter interface

```python
class ProviderAdapter(Protocol):
    id: str
    def validate_credentials(self) -> ValidationResult: ...
    def gpu_catalog(self) -> list[GpuOffer]: ...            # type, vram_gb, usd_per_hour (market)
    def credit_remaining(self) -> CreditReading: ...       # value, unit, source=api|estimate, expires_at
    def submit(self, attempt: AttemptPlan) -> ExternalRef: ...
    def poll(self, ref: ExternalRef) -> AttemptState: ...   # queued|running|succeeded|failed|lost
    def cancel(self, ref: ExternalRef) -> None: ...
    def max_session(self) -> timedelta | None: ...
```

---

## 4. Job specification (`tierhopper.yaml`)

```yaml
name: layout-eval-v3
project: docbench-lab
entrypoint: python run_eval.py --split test
workdir: .                      # packed (respecting .gitignore + .tierhopperignore), size-limited
python: "3.11"
requirements: requirements.txt
gpu:
  min_vram_gb: 24
  types: [L4, A10G, A100, T4x2]  # optional allow-list
  count: 1
estimate_hours: 3               # optional hint; history wins when available
checkpoint:
  dir: checkpoints/             # runner syncs this dir to R2
  every_minutes: 10
progress_file: .progress        # script writes 0..1 (optional; SDK call also works)
shard:                          # optional
  over: data/docs/*.pdf         # or `count: 8`
  max_parallel: 4
serve:                          # optional ephemeral model endpoint
  engine: vllm
  model: Qwen/Qwen2.5-VL-7B-Instruct
  args: ["--max-model-len", "16384"]
outputs: [results/]
allow_paid: false               # even true still requires approval
```

Inside the job the runner exposes: `TH_SHARD_INDEX`, `TH_SHARD_COUNT`, `TH_SHARD_ITEMS` (file list),
`TH_CHECKPOINT_DIR`, `TH_RESUMED=1|0`, `OPENAI_BASE_URL` (when `serve` is set), `HF_HOME` (cache).
Optional SDK: `from tierhopper_runner import progress, checkpoint`.

---

## 5. Scheduling

### 5.1 Hard filters
Provider `active` · GPU meets `min_vram_gb`/`types` · spend cap not reached · enough credit for at
least one checkpoint interval · paid providers only with an approved `approvals` row.

### 5.2 Fastest free GPU first, then the score
On each provider TierHopper takes the fastest GPU that fits the job and that the remaining free credit
can pay for (job size = T4-equivalent hours, from history or `estimate_hours`). Providers are ordered by
that GPU's speed. If no GPU can finish the job on the credit left, it takes the one with the most work
per dollar. Modal receives the slower GPUs as a fallback list, used only when the fast one has no
capacity; the GPU that actually ran is recorded from the runner's first heartbeat. Paid providers keep
the cheapest GPU that fits and are never chosen without approval. The score below only breaks ties.

### 5.2.1 Tie-break score

```
score = 0.45 * urgency       # share of this provider's credit that expires unused if not spent now
      + 0.25 * speed         # learned throughput factor for this GPU type (history, EWMA)
      - 0.15 * failure_rate  # learned, last 30 days
      - 0.15 * queue_time    # learned p50 wait, normalised
```
Weights live in config. With no history, `speed` falls back to a static GPU table.

### 5.3 Hops
An attempt ends with an `end_reason`: `completed`, `credit_exhausted`, `session_limit`, `preempted`,
`error`, `cancelled`. For everything except `completed`/`cancelled`, the shard resumes from its last
R2 checkpoint on the next best provider. Same-error retries are capped at 2 per shard; after that
the job becomes `needs_attention`. Before a known session limit (e.g. Kaggle), the scheduler forces a
checkpoint and hops preemptively.

### 5.4 Estimates and approvals
Estimate = history for the same `spec_hash`/project scaled by GPU speed, else `estimate_hours`.
Approval required when: any paid provider · estimated free GPU-hours > `approval.max_free_gpu_hours`
(default 12) · real money > US$ 0. Approvals expire after 30 min.

### 5.5 Sharding
`shard.over` / `shard.count` → N shards, each an independent unit that can land on a different provider.
The job is `done` when all shards are done; results are merged under `results/<job_id>/`.

---

## 6. MCP tools

| Tool | Input | Output |
|------|-------|--------|
| `submit_job` | `spec_path` or inline `spec`, `dry_run?` | `job_id`, estimate, chosen plan, `needs_approval` + approval link |
| `job_status` | `job_id` | status, progress, current provider, stops, ETA, last events |
| `fetch_results` | `job_id`, `dest_dir`, `partial?` | downloaded files (via presigned GET), manifest |
| `credits_status` | — | per provider: remaining, unit, source (api/estimate), resets/expires at, spend vs cap |
| `usage_report` | `period` (week/month/all) | GPU-hours per provider, credit used, failed/migrated jobs, real cost, savings |
| `connect_provider` | `provider_id` | starts the connect flow (returns instructions; secrets are never passed through MCP) |
| `list_providers` | `status?` | registry entries with status, requirements, card link |

The MCP server never returns secrets and never accepts them as arguments.

---

## 7. Data model (Supabase Postgres, `sa-east-1`)

All tables have RLS enabled. The dashboard reads with the authenticated user's JWT (single owner);
the MCP server and control plane use the service-role key.

```sql
providers(
  id text pk, name text, kind text check (kind in ('free','credit_program','paid')),
  status text check (status in ('pending','pending_adapter','active','disabled')),
  adapter text, signup_url text, api_key_url text, login_command text,
  requirements jsonb,          -- {email:bool, phone:bool, card:bool, notes}
  credit_model jsonb,          -- {type: monthly_usd|weekly_hours|one_time_usd, amount, resets_rule, expires_at}
  gpu_catalog jsonb, max_session_hours numeric, spend_cap_usd numeric default 0,
  validation_test text, source text, confidence numeric, tos_checked_at timestamptz,
  created_at timestamptz, updated_at timestamptz)

credit_snapshots(id bigserial pk, provider_id fk, measured_at, remaining numeric,
  unit text check (unit in ('usd','gpu_hours')), source text check (source in ('api','estimate','manual')),
  expires_at timestamptz)

projects(id uuid pk, slug text unique, name text, created_at)

jobs(id uuid pk, project_id fk, name text, spec jsonb, spec_hash text,
  status text check (status in ('queued','awaiting_approval','running','paused',
                                 'needs_attention','done','failed','cancelled')),
  progress numeric, estimate jsonb, results_prefix text,
  created_at, started_at, finished_at)

shards(id uuid pk, job_id fk, idx int, total int, items jsonb, status text, progress numeric,
  checkpoint_key text, checkpoint_at timestamptz)

attempts(id uuid pk, shard_id fk, provider_id fk, gpu_type text, external_ref jsonb,
  status text, end_reason text, started_at, ended_at, progress_start numeric, progress_end numeric,
  gpu_seconds numeric, cost_usd numeric, market_cost_usd numeric)

events(id bigserial pk, job_id fk, shard_id fk null, attempt_id fk null, ts timestamptz,
  type text, payload jsonb)                       -- hop, checkpoint, error, approval, progress…

metrics(attempt_id fk, ts, gpu_util numeric, vram_used_mb int, vram_total_mb int, cost_usd_cum numeric)
log_tail(attempt_id fk, seq int, ts, stream text, line text)   -- last 2k lines; full logs in R2

approvals(id uuid pk, job_id fk, kind text, amount_usd numeric, reason text,
  token_hash text, status text check (status in ('pending','approved','denied','expired')),
  expires_at, decided_at, channel text)

discovery_findings(id uuid pk, run_at, name text, url text, summary text, confidence numeric,
  official_api bool, headless bool, tos_ok bool, status text, provider_id fk null)

market_prices(gpu_type text pk, usd_per_hour numeric, source text, updated_at)
push_subscriptions(id uuid pk, endpoint text, keys jsonb, created_at)
user_prefs(user_id uuid pk, theme text, sounds bool, reduce_motion bool)
```

Views: `provider_stats` (queue p50, failure rate, speed factor per GPU), `job_journey`
(per-job ordered stops), `savings_summary`.
Retention: `metrics` and `log_tail` pruned after 14 days (pg_cron) to stay inside the free tier.
Every schema change is a migration in `supabase/migrations/` and **requires your approval**.

---

## 8. Storage (Cloudflare R2)

Layout: `ckpt/<job>/<shard>/latest.tar.zst`, `results/<job>/…`, `logs/<attempt>.log.zst`.
Jobs never receive R2 keys. The control plane issues **presigned URLs** scoped to the job's prefix,
valid for at most 24 h and refreshed through the ingest endpoint heartbeat.
Pre-work: the Cloudflare account and R2 activation are done by you (R2 activation may ask for a
payment method — manual step, no charge on the free tier).

---

## 9. Onboarding flow

1. A provider exists in the registry with status `pending` (seeded or discovered).
2. **Integration card** via WhatsApp (and as an unlockable card in the dashboard):

```
🟢 TierHopper · New GPU source: Lightning AI
What you get: ~monthly free credits on T4/L4 (verify on page)
Sign up: https://lightning.ai/sign-up
1. Create the account with your usual e-mail
2. Accept the terms and verify the phone yourself (if asked)
3. Open Settings → Keys
4. On your Mac, run the command below
Then: tierhopper connect lightning
```
3. `tierhopper connect <provider>`:
   opens the signup/API-key page in the browser → runs the official login when it exists
   (`modal token new`, `lightning login`) or asks you to paste the key with hidden input →
   stores it in macOS Keychain (`service=tierhopper`, `account=<provider>`) → `validate_credentials()`
   → **runs a 30-second GPU test job** (`nvidia-smi` + a small matmul) → only then sets status `active`.
4. Account creation, accepting terms, phone verification and adding a card are always done by you.

---

## 10. Notifications and WhatsApp commands

- Outbound: CallMeBot (`phone` + `apikey` in Keychain / Modal Secret). Messages never include
  secrets, tokens, full logs or presigned URLs.
- Commands (CallMeBot cannot receive messages): each message carries short **signed action links**
  to the Modal action endpoint: `status`, `pause`, `approve`, `report`.
  - Links are HMAC-signed, single use, expire in 30 min.
  - `GET` only renders a confirmation page; the action happens on a `POST` button — so WhatsApp link
    previews can never approve anything by accident.
- Weekly summary (Monday 09:00 America/Sao_Paulo): GPU-hours, credits left, savings, failed/migrated jobs.
- The PWA receives the same events as Web Push.

---

## 11. Discovery (weekly, Modal cron)

1. Collect from curated sources (GitHub awesome lists of free GPU/cloud credits, r/MachineLearning,
   r/LocalLLaMA, Hacker News, pricing pages of known providers) + web search.
2. Claude API (direct tool use, no frameworks) extracts candidate offers.
3. Validation: official API/SDK for headless jobs? ToS allow programmatic use? Credit real and current?
   → confidence score 0–1.
4. `confidence ≥ 0.7` and no ToS red flag → registry entry `pending` (or `pending_adapter` when no
   adapter exists yet) and a card. Lower → stored as a finding only, visible in the dashboard.

---

## 12. Security rules (non-negotiable)

1. No automated account creation, no CAPTCHA bypass, no phone-verification automation, one account
   per provider, created by you, respecting each provider's terms.
2. Credentials only in macOS Keychain (via `keyring`) or in `.env` outside git; in the cloud, only as
   Modal Secrets / Railway variables. Provider CLIs keep their own official files
   (`~/.modal.toml`, `~/.kaggle/kaggle.json`) outside the repo.
3. A redaction filter runs on every log line, event payload and notification (key/token patterns +
   known secret values).
4. Jobs receive only short-lived, prefix-scoped presigned URLs and per-attempt ingest tokens.
5. `.gitignore` covers `.env*`, `*.json` credential files, `checkpoints/`, `results/`; a pre-commit
   secret scan (gitleaks) runs before every commit.
6. Your approval is required before: any money spend, creating cloud resources (Supabase project,
   Modal apps/secrets, R2 bucket, Railway service), schema changes, deploys and `git push`.
7. RunPod spend is capped at US$ 20/month in code and on the RunPod account (manual setting).

---

## 13. UX/UI

### 13.1 Principles
- **Plain language first.** Main screens never show jargon (no "attempt", "shard", "preempted").
  Technical detail lives behind *Technical details*.
- **One glance answers three questions:** Is everything moving? Does anything need me? How much did I save?
- **Fixed, deliberate layout.** No widget juggling; the same thing is always in the same place.
- Keyboard-first (⌘K, `j/k` to move, `enter` to open, `p` pause, `a` approve), fully usable by touch.

### 13.2 Screens

**1. Home — "Projects" tab (default)**
- Top: **overall progress bar** across all active projects, with time left, money spent and money saved
  (savings meter with streak chip, e.g. "12 days at $0").
- Below: **one row per project** (a submitted job): name, progress bar, plain status, one-line
  explanation, time left.
  | Status | Color | Meaning shown to the user |
  |--------|-------|---------------------------|
  | Running | blue | "Running on Modal · 1h 10m left" |
  | Done | green | "Results ready" |
  | Needs your OK | orange | "Free credit ran out — continue on RunPod for about $1.80?" with inline *Approve* / *Wait* |
  | Waiting in line | gray | "Starts when Kaggle hours reset on Saturday" |
  | Paused | gray outline | "Paused by you" |
  | Stopped | red | "Stopped after 3 tries — see why" |
- Right column (below on mobile): **Free credit left** per provider (bar, reset date) and **Savings**.

**2. Project detail** (click a row)
1. **Summary**: time running, time left, providers used, money saved.
2. **Journey**: one horizontal bar split by provider (one color per provider, width ∝ time);
   the remaining part is dashed; labels for start, *now* and expected finish.
3. **Stops**: one row per provider — % of the work done there, time spent and, in plain language,
   why it moved on ("free credit ran out", "session time limit", "machine was taken back").
4. Buttons: **Pause**, **Get results so far**, **Technical details**.

**3. Technical details** (drawer)
Live logs (streaming), GPU metrics (utilisation, VRAM, cost so far), checkpoints list, and
**Job replay**: a scrubbable timeline of every event (hops, checkpoints, failures, cost) with
play/pause and speed control, like a video.

**4. "Hop map" tab** — providers as nodes, live jobs animating along the edges on each hop.

**5. "Providers" tab** — unlockable cards: locked cards show what you get and a progress checklist
(Account created → Key saved → 30-s GPU test passed); *Connect* shows the CLI command and a live
animated test once running. Discovery findings appear as "Suggested" cards.

**6. "Reports" tab** — GPU-hours per provider, credit used, failed/migrated jobs, real cost, savings over time.

**7. Settings** — theme, sounds, reduced motion, approval threshold, spend caps, notification channels.

### 13.3 Visual identity
- **Themes** (user-selectable, stored in `user_prefs`):
  - **Mission Control** (default): near-black (#07090d) with a faint 24-px grid, hairline borders,
    Geist + Geist Mono, restrained neon accents (cyan #5ee0ff for brand, status colors as above).
  - **Retro CRT**: phosphor green on black, VT323 for headings, subtle scanlines + glow
    (disabled automatically with reduced motion).
  - **Minimal Light**: off-white, soft grays, Geist, same status hues darkened for contrast.
- All themes meet WCAG AA contrast; status is never conveyed by color alone (text label + icon).
- Numbers use tabular monospace; time in "1h 10m", money in "$1.80", dates relative ("Sat").

### 13.4 Interactions
- ⌘K palette: navigation, actions and natural language ("run layout-eval on the cheapest 24 GB GPU").
  The Claude API turns text into a structured action preview; anything that spends money or creates
  resources shows a confirmation step.
- Framer Motion for row/progress transitions and hop animations; honours `prefers-reduced-motion`.
- Web Audio: soft tick on hop, chime on done, low tone on "needs your OK". Off switch in the top bar
  and in Settings; muted by default on first visit until the user enables it.
- Realtime: rows and bars update live from Supabase Realtime; a "live" pulse shows the connection.
- PWA: installable, offline shell, Web Push for "Needs your OK" and "Done".

### 13.5 Dashboard auth
Supabase Auth with a single allowed account (magic link to your e-mail); all data behind RLS.
No public pages.

---

## 14. Repository layout

```
tierhopper/
├── SPEC.md  PLAN.md  README.md  providers.yaml  pyproject.toml
├── src/tierhopper/
│   ├── spec.py  routing.py  estimate.py  approvals.py  credentials.py  redact.py
│   ├── store/ (supabase.py, memory.py)
│   ├── adapters/ (base.py, modal.py, kaggle.py, lightning.py, runpod.py)
│   ├── mcp_server.py  cli.py  storage_r2.py  notify/ (callmebot.py, webpush.py)
│   └── control_plane/ (modal_app.py, scheduler.py, discovery.py, ingest.py, actions.py)
├── runner/tierhopper_runner/
├── skill/SKILL.md
├── supabase/migrations/
├── dashboard/ (Next.js)
├── docs/mockups/
└── tests/
```
