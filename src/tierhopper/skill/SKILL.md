---
name: tierhopper
description: Run heavy GPU work (training, evals, batch inference, benchmarks) on free cloud GPU tiers through the TierHopper MCP server instead of the local machine. Use when a task needs a GPU, more than ~8 GB RAM, or more than a few minutes of heavy compute.
---

# TierHopper

TierHopper runs a job on free cloud GPU credit and moves it by itself when a place runs out. Use its MCP tools; do not run heavy jobs on the local machine.

## When to use
- Anything that needs CUDA, or a model that does not fit in ~6 GB of RAM.
- Experiments that take more than a few minutes of heavy compute.
- Not for quick CPU scripts, unit tests or anything touching local-only files that cannot be shipped.

## First time / test job
If the user wants to try TierHopper, run `tierhopper init <folder>` in the shell (it creates a tiny example
that reports which GPU it got), then follow the flow below with that folder.

## How to package a job
1. Put the code in one directory and keep it small: code only (the packed folder must stay under
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
   There is no CUDA compiler (`nvcc`) by default, so vLLM runs with its built-in sampler
   (`VLLM_USE_FLASHINFER_SAMPLER=0` is set for you). Only if the job itself compiles GPU kernels at run
   time (custom CUDA extensions, JIT kernels) add `cuda_compiler: true`; it makes the machine start slower.
7. Secrets never go in the package; `.env*`, key files and `.git` are excluded automatically.

## Flow
1. `submit_job(spec_path=<absolute path>)` → returns `job_id`. Just submit: there is nothing to confirm with
   the user first (`dry_run=true` only checks that the yaml is valid).
2. Right after submitting, run the command given in `when_it_ends` (`tierhopper watch <job_id>`) as a
   background shell command. It exits when the job ends, with the results already downloaded, and that
   brings you back to tell the user — do not wait for them to ask whether it finished. Keep working on
   other things meanwhile; do not poll in a loop.
   `job_status(job_id)` shows where it is at any moment; when `status` is `done`, `fetch_results(job_id, dest_dir=...)` brings the results back.
   `job_logs(job_id, contains="error")` shows what the job printed — use it to see why a job failed or is slow.
   `list_jobs()` finds a job_id (e.g. after a new session). Never query the TierHopper database directly.
   `pause_job`, `resume_job` and `cancel_job` control a job. Submit once: if a reply was cut off, call
   `list_jobs` before submitting again, so the same job is not launched twice.

## Which GPU runs it
TierHopper picks the GPU and the place. Give a realistic `estimate_hours` and the `min_vram_gb` the job
really needs; do not pin `gpu.types` unless the user asks for a specific GPU.

## Rules
- Always submit a job that is ready. Never check free credit, provider availability or cost, and never hold
  a job back, postpone it or ask the user whether to wait because of them. TierHopper queues the job and
  settles where and when it runs with its owner. Your part is: submit, wait, report the result.
- Where a job runs is TierHopper's decision, never yours. Do not ask the user which provider or GPU to use,
  do not suggest one, and do not ask whether to pay for a paid one. When a paid provider would be needed,
  TierHopper itself asks its owner in the TierHopper app, with an approve button. Never relay that question
  and never run `tierhopper approve`. If a job is `awaiting_approval`, say once, in one line, that it is
  waiting for their OK in the TierHopper app, and keep waiting.
- If `submit_job` answers that TierHopper is not set up yet, give the user the command it names. Nothing else
  about providers is yours to handle.
- Local mode (no cloud scheduler): a job only moves forward when you call `job_status`, so check on it.
