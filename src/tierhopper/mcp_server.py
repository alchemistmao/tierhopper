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
estimate_hours, timeout_minutes, outputs). Flow: submit_job(dry_run=true) to show the plan, submit_job to
launch, job_status to follow (in local mode a job only advances when it is checked), job_logs to see
output or errors, fetch_results to bring the output files back.

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
        return {"error": redact(f"{type(e).__name__}: {e}")[:1000]}


@mcp.tool()
def submit_job(spec_path: str, dry_run: bool = False) -> dict[str, Any]:
    """Run a GPU job on the best free provider.

    spec_path: absolute path to a tierhopper.yaml (or the directory containing it).
    dry_run: only return the routing plan and estimate, without launching anything.
    """
    return _safe(_service().submit_job, spec_path, dry_run=dry_run)


@mcp.tool()
def job_status(job_id: str) -> dict[str, Any]:
    """Refresh and return a job's status: provider stops, GPU minutes, messages and results folder."""
    return _safe(_service().job_status, job_id)


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
    return _safe(_service().job_logs, job_id, lines=lines, contains=contains)


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


@mcp.tool()
def credits_status() -> dict[str, Any]:
    """Free credit left per provider (from the provider API when available, else a local estimate)."""
    return _safe(lambda: {"providers": _service().credits_status()})


@mcp.tool()
def list_providers(status: str | None = None) -> dict[str, Any]:
    """Providers in the registry with status, requirements and how to connect them."""
    def run():
        rows = []
        for p in _service().store.list_providers(status):
            rows.append({"id": p.id, "name": p.name, "kind": p.kind.value, "status": p.status.value,
                         "signup_url": p.signup_url, "requirements": p.requirements.model_dump(),
                         "connect_command": f"tierhopper connect {p.id}"})
        return {"providers": rows}
    return _safe(run)


@mcp.tool()
def usage_report(period: str = "week") -> dict[str, Any]:
    """GPU-hours per provider, credit left, failed and migrated jobs, real cost and savings.

    period: week | month | all
    """
    return _safe(_service().usage_report, period)


@mcp.tool()
def connect_provider(provider_id: str) -> dict[str, Any]:
    """How to connect a provider: what it gives and the command the user runs in their own terminal.

    Account creation, terms, phone and card verification are always done by the user. Secrets are
    never passed through this tool; `tierhopper connect` asks for them with hidden input.
    """
    def run():
        from tierhopper import notify

        p = _service().store.get_provider(provider_id)
        return {"provider": p.id, "status": p.status.value, "card": notify.integration_card(p),
                "command": f"tierhopper connect {p.id}",
                "note": "The user runs this in a terminal; it asks for the key with hidden input."}
    return _safe(run)


def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
