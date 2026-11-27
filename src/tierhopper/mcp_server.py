"""TierHopper MCP server (stdio). Never returns or accepts secrets."""

from __future__ import annotations

from functools import cache
from typing import Any

from fastmcp import FastMCP

from tierhopper import __version__
from tierhopper.redact import redact

INSTRUCTIONS = """TierHopper runs GPU jobs (training, evals, batch inference) on the free tiers of cloud GPU
providers, in the user's own accounts, so heavy work never runs on their computer.

A job is a folder with code and a `tierhopper.yaml` (name, entrypoint, requirements, gpu.min_vram_gb,
estimate_hours, timeout_minutes, outputs). Flow: submit_job to launch, job_status to follow (in local mode a
job only advances when it is checked), job_logs to see output or errors, fetch_results to bring the
output files back.

Where a job runs is TierHopper's decision, not yours and not something to ask the user: it picks the
provider and the GPU, moves the job when needed, and when a paid provider would be needed it asks the
owner itself, in the TierHopper app. Never ask the user which provider to use or whether to pay, never
relay that question, and never approve a job yourself. Submit, wait, report the result.

Always submit a job that is ready. Free credit, provider availability and cost are not yours to check or
to weigh: never hold a job back, postpone it or ask the user whether to wait because of them. If nothing
can run it right now, TierHopper keeps it in its queue and settles that with its owner.

If a tool answers that no provider is connected, the user has to run `tierhopper setup` in a terminal:
it asks for their own provider key with hidden input. Never ask the user to paste a key in the chat.
Paid providers are never used without the user's explicit approval."""

mcp = FastMCP("tierhopper", instructions=INSTRUCTIONS, version=__version__)


@cache
def _service():
    from tierhopper import notify
    from tierhopper.service import TierHopper
    from tierhopper.store import open_store

    store = open_store()
    return TierHopper(store, notifier=notify.Notifier(store))


def _safe(fn, *args, **kwargs) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - tools report errors instead of crashing the server
        text = str(e)
        if type(e).__name__ in ("ConnectError", "ConnectTimeout") or "<!DOCTYPE" in text:
            return {"error": "The TierHopper database (the user's Supabase project) is not answering. It may have "
                             "been paused for inactivity: the user can restore it at https://supabase.com/dashboard."}
        return {"error": redact(f"{type(e).__name__}: {e}")[:1000]}


@mcp.tool()
def submit_job(spec_path: str, dry_run: bool = False) -> dict[str, Any]:
    """Run a GPU job. TierHopper decides where it runs; you only submit it and wait for the result.

    spec_path: absolute path to a tierhopper.yaml (or the directory containing it).
    dry_run: only check that the job is valid and how long it should take, without launching anything.
    """
    return for_caller(_safe(_service().submit_job, spec_path, dry_run=dry_run))


WAITING_FOR_OWNER = ("TierHopper is waiting for the owner's decision, which they make in the TierHopper app (it "
                     "already notified them). This is not your question to ask or relay, and you must not approve "
                     "it: keep the watch command running and report when the job ends.")


def for_caller(got: dict[str, Any]) -> dict[str, Any]:
    """What the calling program needs from a submit: the job, its state and how to wait for it. Which provider
    or GPU runs it, and any approval to pay, stay between TierHopper and its owner."""
    if "error" in got:
        return got
    estimate = got.get("estimate") or {}
    out: dict[str, Any] = {"shards": got.get("shards"),
                           "estimated_gpu_hours": estimate.get("gpu_hours"),
                           "checkpoints": got.get("checkpoints")}
    if got.get("dry_run"):
        out["dry_run"] = True
        out["valid"] = True  # the yaml is fine; whether and where it starts is TierHopper's business
        if got.get("next_step"):
            out["next_step"] = got["next_step"]
        return out
    out.update(job_id=got["job_id"], status=got["status"])
    if got["status"] == "awaiting_approval":
        out["note"] = WAITING_FOR_OWNER
    elif got["status"] == "queued":
        out["note"] = ("Accepted and waiting in TierHopper's queue for a GPU. Nothing for you or the user to do: "
                       "keep the watch command running and report when the job ends.")
    out["when_it_ends"] = (f"Run `{watch_command(got['job_id'])}` as a background shell command now: it exits "
                           "when the job ends (results already downloaded), which brings you back to report "
                           "to the user without them having to ask.")
    return out


def watch_command(job_id: str) -> str:
    """The shell command that follows one job to its end, for this install (PATH or the project folder)."""
    import shlex
    import shutil
    from pathlib import Path

    if shutil.which("tierhopper"):
        return f"tierhopper watch {job_id}"
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and shutil.which("uv"):
        return f"uv --directory {shlex.quote(str(root))} run tierhopper watch {job_id}"
    import sys

    return f"{shlex.quote(sys.executable)} -m tierhopper.cli watch {job_id}"


@mcp.tool()
def job_status(job_id: str) -> dict[str, Any]:
    """Refresh and return a job's execution: state, progress, each run's timing and messages, results folder.

    If the status is awaiting_approval, the owner decides in the TierHopper app: do not ask them about it
    and do not approve it yourself.
    """
    return status_for_caller(_safe(_service().job_status, job_id))


def status_for_caller(got: dict[str, Any]) -> dict[str, Any]:
    """A job's execution as its caller needs it: state, progress, timing, errors, results. Not where it ran."""
    if "error" in got:
        return got
    runs = [{"part": r.get("shard"), "status": r.get("status"), "end_reason": r.get("end_reason"),
             "started_at": r.get("started_at"), "ended_at": r.get("ended_at"), "progress": r.get("progress"),
             "gpu_minutes": r.get("gpu_minutes"), "message": r.get("message")} for r in got.get("stops") or []]
    out = {"job_id": got.get("job_id"), "name": got.get("name"), "status": got.get("status"),
           "progress": got.get("progress"), "last_checkpoint": got.get("last_checkpoint"),
           "results": got.get("results"), "runs": runs, "recent_events": got.get("recent_events")}
    if got.get("status") == "awaiting_approval":
        out["note"] = WAITING_FOR_OWNER
    return out


@mcp.tool()
def list_jobs(status: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Recent jobs, newest first, with their job_id, status and progress.

    status (optional): queued | awaiting_approval | running | paused | done | failed | cancelled.
    Use this to find a job_id instead of asking the user or reading the database.
    """
    return _safe(_service().list_jobs, status=status, limit=limit)


@mcp.tool()
def job_logs(job_id: str, lines: int = 80, contains: str | None = None) -> dict[str, Any]:
    """Read a job's output: the latest lines of each attempt (the full log once an attempt has ended).

    lines: how many lines per attempt (max 400). contains: only lines containing this text
    (case-insensitive), e.g. "error" or "vllm". Use this to see why a job failed or is slow.
    """
    got = _safe(_service().job_logs, job_id, lines=lines, contains=contains)
    for run in got.get("attempts") or got.get("logs") or []:
        if isinstance(run, dict):
            run.pop("provider", None)  # where it ran is not the caller's business
    return got


@mcp.tool()
def pause_job(job_id: str) -> dict[str, Any]:
    """Pause a job: stops what is running on the provider and keeps the saved progress."""
    return _safe(_service().pause, job_id)


@mcp.tool()
def resume_job(job_id: str) -> dict[str, Any]:
    """Resume a paused job from its last save."""
    return _safe(_service().resume, job_id)


@mcp.tool()
def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a job for good (e.g. it was submitted twice or is clearly wrong). Stops it on the provider."""
    return _safe(_service().cancel, job_id)


@mcp.tool()
def fetch_results(job_id: str, dest_dir: str | None = None, partial: bool = False) -> dict[str, Any]:
    """Download a job's results to dest_dir (default ~/.tierhopper/results/<job_id>).

    partial: if the job is not finished, also download its last checkpoint.
    """
    return _safe(_service().fetch_results, job_id, dest_dir=dest_dir, partial=partial)












def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
