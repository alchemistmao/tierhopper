<p align="center"><img src="docs/brand/logo-a-frog.png" alt="TierHopper — a frog hopping from a paid GPU to a free one" width="640"></p>

# TierHopper

**Hop till the credits drop.** TierHopper runs your GPU jobs on the free tiers of cloud providers.
When one provider's free credit runs out, the job hops to the next one.

You use it from [Claude Code](https://claude.com/claude-code): ask Claude to "run this on TierHopper"
and it packages the code, picks the fastest free GPU, follows the job and brings the results back.
Your laptop does none of the heavy work.

## What you need

- A Mac or Linux computer with [Claude Code](https://claude.com/claude-code)
- A free [Kaggle](https://www.kaggle.com) account with a verified phone number
  (no card; about 30 GPU-hours every week)

## Install (about 5 minutes)

Open the Terminal app and run these three commands, one at a time.

1. Install [uv](https://docs.astral.sh/uv/), the tool that installs TierHopper (skip if you have it):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close the Terminal window and open a new one, so it finds `uv`.

2. Install TierHopper:

```bash
uv tool install https://github.com/alchemistmao/tierhopper/archive/refs/heads/main.zip
```

3. Set it up:

```bash
tierhopper setup
```

`setup` walks you through it:

- it opens the Kaggle page where you create an API token, and you paste the token (it goes to your
  system keychain, nowhere else);
- it runs a real 2-minute GPU test in your Kaggle account, so you know it works;
- it adds TierHopper to Claude Code.

When it says **All set**, open a **new** Claude Code session and ask:

> Run a TierHopper test job

Claude creates a small example, sends it to a free GPU and tells you which GPU ran it.

`tierhopper doctor` checks the installation at any time.

**If the terminal says `tierhopper: command not found`**, run `uv tool update-shell` and open a new
Terminal window.

## Using it

In Claude Code, point Claude at a folder with your code and say "run this on TierHopper". Claude writes
the job file, shows you the plan, follows the job and brings the results back. Useful things to ask:

- "How is the job going?"
- "Why did it fail?"
- "How much free GPU do I have left?"

The same from the terminal:

```bash
tierhopper init my-first-job
```

```bash
tierhopper submit my-first-job --watch
```

Jobs keep running in the cloud if you close the laptop. Their results arrive the next time you (or
Claude) check on them.

## The job file

A job is a folder with your code and a `tierhopper.yaml`:

```yaml
name: my-run
entrypoint: python run.py --split test
requirements: requirements.txt
gpu:
  min_vram_gb: 16        # per GPU
estimate_hours: 1        # size of the job in hours on a T4; faster GPUs finish sooner
timeout_minutes: 120
outputs: [results/]      # what comes back
```

Print a line like `page 37 of 150` as you go and TierHopper shows progress and time left.
More options (checkpoints, shards, a temporary vLLM server, extra `apt` packages) are in [SPEC.md](SPEC.md).

## Providers

| Provider | Cost | Free allowance | Needs |
|---|---|---|---|
| Kaggle | free | about 30 GPU-hours per week (T4) | account, no card |
| Modal | free | US$ 30 of credit per month (up to H100) | account and a card on file |
| Lightning AI | free | one-time starting credit | account, full mode |
| RunPod | paid | none | credit you buy, full mode, your approval per job |

Connect another one with `tierhopper connect <provider>`. Allowances change; `tierhopper credits`
shows what you really have left. TierHopper always takes the fastest free GPU your remaining credit
can pay for, and never uses a paid provider without your explicit approval.

## Two modes

**Local mode** is what you get after `setup`. Everything stays on your machine
(`~/.tierhopper`), with no extra accounts. A job moves forward whenever Claude checks it or while
`tierhopper watch` is open. Without storage there are no checkpoints, so a job that hops starts over
on the next provider: fine for jobs of a few hours.

**Full mode** adds a scheduler that works while your computer is off, checkpoints that survive a hop,
a web dashboard and phone notifications. It needs your own Supabase project, a Cloudflare R2 bucket and
Modal. Install with
`uv tool install "tierhopper[full] @ https://github.com/alchemistmao/tierhopper/archive/refs/heads/main.zip"`
and see [SPEC.md](SPEC.md).

## Rules it follows

- One account per provider, created by you. TierHopper never creates accounts, never solves CAPTCHAs
  or phone checks and never rotates accounts to stretch a free tier.
- You read and accept each provider's terms yourself.
- Keys live in the system keychain, never in the repository, logs or job packages.
- No money is spent without your approval.

## Commands

| | |
|---|---|
| `tierhopper setup` | connect the first provider, test a GPU, add TierHopper to Claude Code |
| `tierhopper init <folder>` | create a small example job |
| `tierhopper doctor` | check that everything is in place |
| `tierhopper submit <folder> --watch` | run a job and follow it |
| `tierhopper jobs` / `status` / `logs` / `watch` | follow jobs |
| `tierhopper fetch <job_id>` | download results |
| `tierhopper pause` / `resume` / `cancel` | control a job |
| `tierhopper credits` / `report` | free credit left and usage |

## Questions

**Is it really free?** In local mode with Kaggle, yes. Modal asks for a card but only charges past its
monthly free credit, and TierHopper stops before that.

**Where is my code sent?** Only to the provider that runs the job, in your own account.
`.env*`, `kaggle.json` and `.git` are left out of the package automatically.

**What shows up in my Kaggle account?** Each run is a private notebook named `th-…`. You can delete
them whenever you like.

**How big can a job be?** The folder holds code only: on Kaggle it must stay under 900 KB packed. Let the
script download datasets and models when it runs.

**Does it work on Windows?** It has not been tested there yet. Use WSL for now.

**How do I update it?** Run the install command again with `--force`, then `tierhopper install`.

**A job failed. Why?** `tierhopper logs <job_id>`, or ask Claude "why did the job fail?".

**How do I remove it?** `uv tool uninstall tierhopper`, `claude mcp remove tierhopper`, then delete
`~/.tierhopper` and `~/.claude/skills/tierhopper`.

## Development

```bash
uv sync && uv run pytest
```

MIT license. Not affiliated with any of the providers.
