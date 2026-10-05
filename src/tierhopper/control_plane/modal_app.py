"""Modal app `tierhopper-control`.

- `scheduler_tick`: every 5 minutes, polls running attempts, hops, dispatches waiting jobs.
- `web` (one HTTPS app):
    POST /ingest  runner heartbeats, authenticated by per-attempt HMAC tokens;
    GET  /action  WhatsApp action links: confirmation page only;
    POST /action  performs the confirmed action.
- `weekly_summary`: Monday 09:00 (São Paulo) usage summary on WhatsApp.
- `weekly_discovery`: Monday 08:00 (São Paulo) search for new free GPU tiers (Claude API + web search).

All credentials come from the Modal Secret `tierhopper` as TIERHOPPER_* environment variables.
Deploy with `tierhopper control deploy`.
"""

from pathlib import Path

import modal

PACKAGE = Path(__file__).resolve().parents[1]
APP_NAME = "tierhopper-control"
SECRET_NAME = "tierhopper"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install("pydantic>=2.8", "pyyaml>=6.0", "keyring>=25.0", "supabase>=2.0", "boto3>=1.34",
                    "kaggle>=2.0", "modal", "fastapi[standard]", "python-multipart", "lightning-sdk",
                    "anthropic>=1.0", "pywebpush>=2.0")
    .env({"TIERHOPPER_REGISTRY": "/root/providers.yaml"})
    .add_local_file(PACKAGE / "providers.yaml", "/root/providers.yaml")
    .add_local_python_source("tierhopper")
)
app = modal.App(APP_NAME, image=image, secrets=[modal.Secret.from_name(SECRET_NAME)])


def _service():
    from tierhopper import notify
    from tierhopper.service import TierHopper
    from tierhopper.store import open_store

    store = open_store()
    return TierHopper(store, notifier=notify.Notifier(store))


@app.function(schedule=modal.Period(minutes=5), timeout=600, max_containers=1)
def scheduler_tick() -> dict:
    result = _service().tick()
    print(f"tick: {result}")
    return result


@app.function(timeout=120, max_containers=4)
@modal.asgi_app(label="tierhopper-web")
def web():
    from fastapi import FastAPI, Form, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @api.post("/ingest")
    async def ingest(request: Request) -> dict:
        body = await request.json()
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return _service().handle_heartbeat(body, token)

    @api.get("/action", response_class=HTMLResponse)
    def action_get(t: str = "") -> HTMLResponse:
        code, html = _service().handle_action(t, "GET")
        return HTMLResponse(html, status_code=code, headers={"Cache-Control": "no-store"})

    @api.post("/action", response_class=HTMLResponse)
    def action_post(t: str = Form("")) -> HTMLResponse:
        code, html = _service().handle_action(t, "POST")
        return HTMLResponse(html, status_code=code, headers={"Cache-Control": "no-store"})

    def authorized(request: Request) -> bool:
        import hmac

        from tierhopper import credentials

        expected = credentials.get_secret("control", "dashboard_secret") or ""
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return bool(expected) and hmac.compare_digest(given, expected)

    @api.post("/api/jobs/{job_id}/{action}")
    async def dashboard_action(job_id: str, action: str, request: Request):
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            args = await request.json()
        except Exception:  # noqa: BLE001 - body is optional
            args = {}
        return _service().dashboard_action(job_id, action, args if isinstance(args, dict) else {})

    @api.post("/api/command")
    async def command(request: Request):
        """Natural-language palette: returns a proposed action or an answer. Never changes state."""
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from tierhopper import command as nl

        if not nl.configured():
            return {"kind": "answer",
                    "text": "Natural language is off: add a Claude API key with `tierhopper config anthropic`."}
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            return {"kind": "answer", "text": "Type what you want to do."}
        return nl.plan_command(text, _service().command_snapshot())

    return api


@app.function(schedule=modal.Cron("0 12 * * 1"), timeout=300)  # Monday 09:00 America/Sao_Paulo
def weekly_summary() -> None:
    from tierhopper import notify

    if notify.configured():
        notify.WhatsApp().weekly(_service().usage_report("week"))


@app.function(schedule=modal.Cron("0 11 * * 1"), timeout=1200)  # Monday 08:00 America/Sao_Paulo
def weekly_discovery() -> dict:
    from tierhopper import command, discovery

    if not command.configured():
        return {"skipped": "no Claude API key"}
    service = _service()
    result = discovery.run_discovery(service.store, notifier=service.notifier)
    print(f"discovery: {result['reported']} reported, added {result['added_to_registry']}")
    return result
