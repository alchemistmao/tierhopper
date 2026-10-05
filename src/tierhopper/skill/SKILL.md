---
name: tierhopper
description: Run heavy GPU work (training, evals, batch inference, benchmarks) on free cloud GPU tiers through the TierHopper MCP server instead of the local machine. Use when a task needs a GPU, more than ~8 GB RAM, or more than a few minutes of heavy compute.
---

# TierHopper

TierHopper runs a job on free GPU credit (Modal, Kaggle, …), hopping to the next provider when one
runs out. Use its MCP tools; do not run heavy jobs on the local machine.

## When to use
- Anything that needs CUDA, or a model that does not fit in ~6 GB of RAM.
- Experiments that take more than a few minutes of heavy compute.
- Not for quick CPU scripts, unit tests or anything touching local-only files that cannot be shipped.

## First time / test job
If the user wants to try TierHopper, run `tierhopper init <folder>` in the shell (it creates a tiny example
that reports which GPU it got), then follow the flow below with that folder.

## How to package a job
1. Put the code in one directory and keep it small: code only (on Kaggle the packed folder must stay under
   900 KB). The script downloads data and models itself (Hugging Face, URLs).
2. Add `tierhopper.yaml` next to the code:

```yaml
name: my-run            # lowercase, digits, . _ -
project: my-project
entrypoint: python run.py --split test
requirements: requirements.txt
gpu:
  min_vram_gb: 16       # per GPU
estimate_hours: 1       # size of the job in hours ON A T4; faster GPUs finish proportionally sooner
timeout_minutes: 120
outputs: [results/]     # folders/files brought back
```

3. The script must write everything it wants back under the `outputs` paths.
4. Long jobs: add `checkpoint: {dir: checkpoints/, every_minutes: 10}` and make the script resume from
   `$TH_CHECKPOINT_DIR` when `$TH_RESUMED` is `1`. Write checkpoint files atomically (write a temp file,
   then rename) — the job can be stopped at any moment. Write progress (0..1) to `$TH_PROGRESS_FILE`.
5. Progress: print one line per unit of work, like `page 37 of 150` (or `página 37 de 150`, `37/150 documents`,
   a tqdm bar). TierHopper reads it and shows "37 of 150 pages" with a time-left estimate from the recent pace.
6. System tools: `git`, `curl` and `wget` are available on every provider; list anything else under `apt:`.
7. Secrets never go in the package; `.env*`, `kaggle.json` and `.git` are excluded automatically.

## Flow
1. `submit_job(spec_path=<absolute path>, dry_run=true)` → show the plan and estimate to the user.
2. `submit_job(spec_path=...)` → returns `job_id`.
3. `job_status(job_id)` to follow it; when `status` is `done`, `fetch_results(job_id, dest_dir=...)` brings the results back.
   `job_logs(job_id, contains="error")` shows what the job printed — use it to see why a job failed or is slow.
   `list_jobs()` finds a job_id (e.g. after a new session). Never query the TierHopper database directly.
   `pause_job`, `resume_job` and `cancel_job` control a job. Submit once: if a reply was cut off, call
   `job_status` or `usage_report` before submitting again, so the same job is not launched twice.
4. `credits_status()` to see free credit left; `list_providers()` to see what can be connected.

## Which GPU runs it
TierHopper always takes the fastest free GPU the remaining credit can pay for (e.g. an H100 or A100 on
Modal before a T4 on Kaggle), and falls back to slower ones only when the fast one has no capacity or
the credit would not cover the job. Give a realistic `estimate_hours` so it can check the credit.
Do not pin `gpu.types` unless the user asks for a specific GPU.

## Rules
- Money: paid providers are never used without the user's explicit approval.
- If no provider is available, tell the user which command connects one (`tierhopper setup` the first time,
  `tierhopper connect <id>` for more).
- Local mode (no cloud scheduler): a job only moves forward when you call `job_status`, so check on it.
