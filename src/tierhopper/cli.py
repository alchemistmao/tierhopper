"""`tierhopper` command-line interface."""

from __future__ import annotations

import json
import subprocess
import sys
import webbrowser
from pathlib import Path

import typer

from tierhopper import credentials
from tierhopper.spec import SpecError, load_spec

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False,
                  help="Run GPU jobs on free cloud GPU tiers, from Claude Code or the terminal.\n\n"
                       "New here? Run: tierhopper setup")
spec_app = typer.Typer(no_args_is_help=True, help="Job spec utilities.")
config_app = typer.Typer(no_args_is_help=True, help="Configure TierHopper services.")
control_app = typer.Typer(no_args_is_help=True, help="Cloud control plane (scheduler + heartbeats) on Modal.")
app.add_typer(spec_app, name="spec")
app.add_typer(config_app, name="config")
app.add_typer(control_app, name="control")

# Keychain entries copied into the Modal Secret, as TIERHOPPER_<PROVIDER>_<FIELD>.
CONTROL_SECRETS = [("supabase", "url"), ("supabase", "secret_key"), ("dashboard", "allowed_email"),
                   ("r2", "account_id"), ("r2", "access_key_id"),
                   ("r2", "secret_access_key"), ("r2", "bucket"), ("kaggle", "api_token"),
                   ("lightning", "user_id"), ("lightning", "api_key"), ("lightning", "teamspace"),
                   ("runpod", "api_key"), ("anthropic", "api_key"), ("callmebot", "phone"), ("callmebot", "apikey"),
                   ("control", "ingest_secret"), ("control", "ingest_url"), ("control", "action_secret"),
                   ("control", "action_url"), ("control", "dashboard_secret"), ("control", "vapid_private"),
                   ("control", "vapid_public")]

REPO_URL = "https://github.com/alchemistmao/tierhopper"
JSON = typer.Option(False, "--json", help="Print the raw data instead of a summary.")


@app.callback(invoke_without_command=True)
def _root(version: bool = typer.Option(False, "--version", help="Show the version and exit.", is_eager=True)) -> None:
    if version:
        from tierhopper import __version__

        typer.echo(f"tierhopper {__version__}")
        raise typer.Exit


NEEDS_STORAGE = {"lightning", "runpod"}  # these download the job package by URL, so they need R2


def _service():
    from tierhopper import notify
    from tierhopper.service import TierHopper
    from tierhopper.store import open_store

    store = open_store()
    return TierHopper(store, notifier=notify.Notifier(store))


def _ok(msg: str) -> None:
    typer.secho(f"✓ {msg}", fg=typer.colors.GREEN)


def _fail(msg: str) -> None:
    typer.secho(f"✗ {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _need(module: str, feature: str) -> None:
    """Full-mode features live in optional packages: say how to get them instead of a traceback."""
    import importlib.util

    if importlib.util.find_spec(module) is None:
        _fail(f"{feature} is a full-mode feature. Install the extras first:\n"
              f"  uv tool install --force \"tierhopper[full] @ {REPO_URL}/archive/refs/heads/main.zip\"")


def _print(data) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


@spec_app.command("check")
def spec_check(path: str = typer.Argument(".", help="tierhopper.yaml or its directory")) -> None:
    """Validate a job spec."""
    try:
        spec, root = load_spec(path)
    except SpecError as e:
        _fail(str(e))
    gpu = f"≥{spec.gpu.min_vram_gb:g} GB" if spec.gpu.min_vram_gb else "any GPU"
    _ok(f"{spec.project}/{spec.name} · {gpu} · root {root}")


@config_app.command("supabase")
def config_supabase() -> None:
    """Full mode: keep state in your own Supabase project (apply supabase/migrations first)."""
    _need("supabase", "Supabase state")
    typer.echo("In your Supabase project: Settings → API (URL) and Settings → API keys (secret key).")
    url = typer.prompt("Supabase project URL (https://<ref>.supabase.co)").strip().rstrip("/")
    if not url.startswith("https://") or ".supabase." not in url:
        _fail("that does not look like a Supabase project URL")
    key = typer.prompt("Supabase secret key", hide_input=True).strip()
    try:
        from tierhopper.registry import sync_registry
        from tierhopper.store.supabase_store import SupabaseStore

        providers = sync_registry(SupabaseStore(url=url, key=key))
    except Exception as e:  # noqa: BLE001
        _fail(f"could not use that Supabase project ({type(e).__name__}); nothing was saved. "
              "Did you apply the SQL files in supabase/migrations?")
    credentials.set_secret("supabase", "url", url)
    credentials.set_secret("supabase", "secret_key", key)
    _ok(f"Supabase connected · {len(providers)} providers in the registry · TierHopper is now in full mode")


@config_app.command("r2")
def config_r2() -> None:
    """Full mode: checkpoints and results in your own Cloudflare R2 bucket named `tierhopper`."""
    import re

    _need("boto3", "R2 storage")

    endpoint = typer.prompt("S3 endpoint URL (https://<account>.r2.cloudflarestorage.com)").strip()
    match = re.match(r"https://([0-9a-f]{32})\.(?:[a-z]+\.)?r2\.cloudflarestorage\.com", endpoint)
    if not match:
        _fail("that does not look like an R2 endpoint URL")
    fields = {
        "account_id": match.group(1),
        "access_key_id": typer.prompt("Access Key ID").strip(),
        "secret_access_key": typer.prompt("Secret Access Key", hide_input=True).strip(),
        "bucket": "tierhopper",
    }
    for field, value in fields.items():
        credentials.set_secret("r2", field, value)
    try:
        from tierhopper.storage_r2 import R2

        R2().check()
    except Exception as e:  # noqa: BLE001
        for field in fields:
            credentials.delete_secret("r2", field)
        _fail(f"R2 check failed ({type(e).__name__}); nothing was saved")
    _ok("R2 connected · bucket tierhopper read/write OK")


@config_app.command("anthropic")
def config_anthropic() -> None:
    """Save a Claude API key in the Keychain (used by the dashboard's natural-language commands)."""
    _need("anthropic", "The natural-language palette")
    typer.echo("Create a key at https://console.anthropic.com/settings/keys")
    key = typer.prompt("Claude API key", hide_input=True).strip()
    credentials.set_secret("anthropic", "api_key", key)
    from tierhopper import command

    out = command.plan_command("go to providers", {"projects": [], "providers": []})
    if out.get("action") != "navigate":
        credentials.delete_secret("anthropic", "api_key")
        _fail(f"the key did not work ({out.get('text', 'unexpected reply')}); nothing was saved")
    _ok("Claude API key saved · test command understood")


@config_app.command("whatsapp")
def config_whatsapp() -> None:
    """Save CallMeBot phone + API key in the Keychain and send a test message."""
    from tierhopper import notify

    phone = typer.prompt("WhatsApp number with country code (e.g. +5511999999999)").strip().replace(" ", "")
    apikey = typer.prompt("CallMeBot API key", hide_input=True).strip()
    credentials.set_secret("callmebot", "phone", phone)
    credentials.set_secret("callmebot", "apikey", apikey)
    if not notify.WhatsApp().send("🟢 TierHopper is connected to your WhatsApp."):
        for field in ("phone", "apikey"):
            credentials.delete_secret("callmebot", field)
        _fail("CallMeBot did not accept the test message; nothing was saved")
    _ok("WhatsApp connected · test message sent")


@app.command()
def card(provider_id: str, send: bool = typer.Option(False, "--send", help="Also send it on WhatsApp.")) -> None:
    """Show (or send) the integration card for a provider."""
    from tierhopper import notify

    provider = _service().store.get_provider(provider_id)
    text = notify.integration_card(provider)
    typer.echo(text)
    if send and not notify.WhatsApp().send(text):
        _fail("WhatsApp send failed")


@app.command()
def report(period: str = typer.Option("week", help="week | month | all")) -> None:
    """Usage report: GPU-hours per provider, credit left, failures and switches, cost and savings."""
    _print(_service().usage_report(period))


@app.command()
def discover(dry_run: bool = typer.Option(False, "--dry-run", help="Research and judge, but save nothing.")) -> None:
    """Search for new free GPU tiers now (uses the Claude API with web search: roughly US$ 0.50 per run)."""
    _need("anthropic", "Discovery")
    from tierhopper import discovery

    service = _service()
    _print(discovery.run_discovery(service.store, notifier=service.notifier, dry_run=dry_run))


@app.command()
def connect(provider_id: str = typer.Argument(..., help="kaggle | modal | lightning | runpod")) -> None:
    """Connect a provider: official login or pasted key → Keychain → validation → GPU smoke test → active."""
    _connect(_service(), provider_id)


def _connect(th, provider_id: str) -> None:
    from tierhopper.models import ProviderStatus
    from tierhopper.store import NotFound

    try:
        provider = th.store.get_provider(provider_id)
    except NotFound:
        known = ", ".join(p.id for p in th.store.list_providers())
        _fail(f"unknown provider {provider_id!r}. Known: {known}")
    if provider.adapter is None:
        _fail(f"{provider.name} has no adapter yet")
    if provider_id in NEEDS_STORAGE and th.blobs is None:
        _fail(f"{provider.name} needs full mode storage (Cloudflare R2). Run `tierhopper config r2` first, "
              "or start with Kaggle or Modal, which work out of the box.")

    if provider_id == "modal" and not th.adapter(provider).validate_credentials().ok:
        typer.echo("Opening Modal login in your browser…")
        subprocess.run([sys.executable, "-m", "modal", "token", "new"], check=False)
    elif provider_id == "lightning":
        from tierhopper.adapters import lightning_adapter as la

        if not th.adapter(provider).validate_credentials().ok:
            typer.echo("Opening Lightning login in your browser…")
            subprocess.run([str(Path(sys.executable).parent / "lightning"), "login"], check=False)
        if not la.import_login_credentials():
            _fail("Lightning login not found; run `lightning login` first")
        credentials.set_secret("lightning", "teamspace", la.discover_teamspace())
    elif provider_id == "runpod":
        if not credentials.has_secret("runpod", "api_key"):
            typer.echo(f"Create an API key at {provider.api_key_url}")
            webbrowser.open(provider.api_key_url or "")
            credentials.set_secret("runpod", "api_key", typer.prompt("RunPod API key", hide_input=True).strip())
        typer.echo("RunPod is paid: the GPU test costs about US$ 0.05 (a few minutes on the cheapest GPU).")
        if not typer.confirm("Run the paid GPU test now?", default=False):
            _fail("cancelled; RunPod stays pending")
    elif provider_id == "kaggle":
        if not credentials.has_secret("kaggle", "api_token"):
            typer.echo("1. Sign in to Kaggle and verify your phone number at https://www.kaggle.com/settings")
            typer.echo("   (Kaggle only gives GPUs to phone-verified accounts).")
            typer.echo(f"2. On {provider.api_key_url}, under “API Tokens”, click “Generate New Token” and copy it.")
            typer.echo("3. Paste it below and press Enter. Nothing shows while you paste: that is normal.")
            _open(provider.api_key_url)
            credentials.set_secret("kaggle", "api_token", _ask_kaggle_token())

    check = th.adapter(provider).validate_credentials()
    if not check.ok:
        if provider_id == "kaggle" and "phone" in check.detail:  # the token is fine; the account is not ready
            _fail("Your Kaggle token works, but the account has no GPU yet. Verify your phone number at "
                  "https://www.kaggle.com/settings and run this command again.")
        if provider_id in ("kaggle", "runpod"):  # forget a key that does not work, so the next try asks again
            credentials.delete_secret(provider_id, "api_token" if provider_id == "kaggle" else "api_key")
            reason = next((ln.strip() for ln in check.detail.splitlines() if ln.strip()), "")[:160]
            _fail(f"{provider.name} did not accept that key ({reason}).\n"
                  "  Nothing was saved. Run this command again and paste a fresh token.")
        _fail(check.detail)
    _ok(check.detail)

    wait = "about 2 minutes" if provider_id == "kaggle" else "the first run can take a few minutes"
    typer.echo(f"Running a real GPU test on {provider.name} ({wait})…")
    ok, detail = th.smoke_test(provider_id, on_update=lambda m: typer.echo(f"  · {m}"))
    if not ok:
        _fail(f"the GPU test failed: {detail}\nYour key is saved; run this command again to retry.")
    provider.status = ProviderStatus.ACTIVE
    th.store.upsert_provider(provider)
    th.refresh_credit(provider)
    _ok(f"{provider.name} is active · {detail}")


@app.command()
def setup(provider_id: str = typer.Option("kaggle", "--provider", help="First provider to connect.")) -> None:
    """First-time setup: connect one free GPU provider and run a real GPU test. Takes about 5 minutes."""
    from tierhopper.models import ProviderStatus

    typer.secho("TierHopper setup", bold=True)
    th = _service()
    active = [p for p in th.store.list_providers() if p.status == ProviderStatus.ACTIVE]
    if active:
        _ok("Already connected: " + ", ".join(p.name for p in active))
    else:
        typer.echo("")
        typer.echo("You need one account on a GPU provider. It is yours: TierHopper never creates accounts,")
        typer.echo("and uses one account per provider, as the providers' terms require.")
        if provider_id == "kaggle":
            typer.echo("Kaggle is the easiest start: free, no card, about 30 GPU-hours every week.")
            typer.echo("Create the account here if you do not have one: https://www.kaggle.com/account/login")
        typer.echo("")
        _connect(th, provider_id)
    typer.echo("")
    typer.echo("Adding TierHopper to Claude Code…")
    if _install():
        typer.echo("")
        typer.secho("All set.", bold=True)
        typer.echo("Open a NEW Claude Code session and ask: “Run a TierHopper test job”.")


def _open(url: str | None) -> None:
    import os

    if url and not os.environ.get("TIERHOPPER_NO_BROWSER"):
        webbrowser.open(url)


def _ask_kaggle_token() -> str:
    for _ in range(3):
        token = typer.prompt("   Kaggle API token", hide_input=True, default="", show_default=False)
        token = token.strip().strip("'\"")
        if token.startswith("{"):
            typer.secho("   That is the old kaggle.json format. Use “Generate New Token” under “API Tokens” "
                        "and paste the token itself (it starts with KGAT_).", fg=typer.colors.YELLOW)
        elif len(token) < 20:
            typer.secho("   Nothing was pasted (or it was cut short). Copy the token and paste it again.",
                        fg=typer.colors.YELLOW)
        else:
            return token
    _fail("no token received. Run `tierhopper setup` again when you have it.")
    raise AssertionError


def _executable() -> str:
    import shutil

    return shutil.which("tierhopper") or str(Path(sys.argv[0]).resolve())


def _claude() -> str | None:
    import shutil

    return shutil.which("claude")


def _claude_config() -> Path:
    return Path.home() / ".claude.json"


def _claude_registered() -> bool:
    try:  # the settings file is the source of truth and reading it is instant
        if "tierhopper" in (json.loads(_claude_config().read_text()).get("mcpServers") or {}):
            return True
    except (OSError, ValueError):
        pass
    claude = _claude()  # registered some other way (e.g. for one project only)
    return bool(claude) and subprocess.run([claude, "mcp", "get", "tierhopper"], capture_output=True,
                                           check=False).returncode == 0


def _register_in_file(exe: str) -> bool:
    """Claude Code without the `claude` command on PATH (desktop app): add the server to its settings file."""
    path = _claude_config()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    backup = path.with_name(".claude.json.before-tierhopper")
    backup.write_text(path.read_text())
    backup.chmod(0o600)
    data.setdefault("mcpServers", {})["tierhopper"] = {"type": "stdio", "command": exe, "args": ["mcp"],
                                                       "env": _server_env()}
    tmp = path.with_name(".claude.json.tierhopper-tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)
    return True


def _server_env() -> dict[str, str]:
    """A custom state folder or key store must reach the server Claude Code starts, or it would look elsewhere."""
    import os

    return {k: os.environ[k] for k in ("TIERHOPPER_HOME", "PYTHON_KEYRING_BACKEND") if os.environ.get(k)}


def _install() -> bool:
    import shutil

    skill_src = Path(__file__).resolve().parent / "skill" / "SKILL.md"
    skill_dst = Path.home() / ".claude" / "skills" / "tierhopper" / "SKILL.md"
    skill_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(skill_src, skill_dst)
    _ok("skill installed (tells Claude when and how to use TierHopper)")

    exe, claude = _executable(), _claude()
    if claude:
        subprocess.run([claude, "mcp", "remove", "tierhopper", "--scope", "user"], capture_output=True, check=False)
        env = [arg for k, v in _server_env().items() for arg in ("-e", f"{k}={v}")]
        out = subprocess.run([claude, "mcp", "add", "tierhopper", "--scope", "user", *env, "--", exe, "mcp"],
                             capture_output=True, text=True, check=False)
        if out.returncode != 0:
            typer.secho("✗ could not register the MCP server: " + (out.stderr.strip() or out.stdout.strip())[:300],
                        fg=typer.colors.RED)
            return False
    elif not _register_in_file(exe):
        typer.secho("Claude Code was not found on this computer.", fg=typer.colors.YELLOW)
        typer.echo("Install it from https://claude.com/claude-code , open it once, then run: tierhopper install")
        return False
    _ok("MCP server registered in Claude Code (all your projects)")
    return True


@app.command()
def install() -> None:
    """Add TierHopper to Claude Code (setup already does this; run it again after reinstalling)."""
    if not _install():
        raise typer.Exit(1)
    typer.echo("Open a NEW Claude Code session and ask: “what can TierHopper do?”")


@app.command()
def init(folder: str = typer.Argument("hello-gpu", help="Folder to create.")) -> None:
    """Create a small example job (finds the GPU and benchmarks it) to try or to start from."""
    import shutil

    here = Path(__file__).resolve()
    src = next((p for p in (here.parent / "examples" / "hello-gpu", here.parents[2] / "examples" / "hello-gpu")
                if p.is_dir()), None)
    dest = Path(folder).expanduser()
    if src is None:
        _fail("the example is missing from this installation; reinstall TierHopper")
    if dest.exists():
        _fail(f"{dest} already exists; pick another name: tierhopper init my-job")
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "results"))
    _ok(f"created {dest}/ (main.py, requirements.txt, tierhopper.yaml)")
    typer.echo(f"Run it:  tierhopper submit {dest} --watch")


@app.command()
def doctor() -> None:
    """Check the installation: mode, providers, credit and Claude Code registration."""
    from tierhopper import __version__, config
    from tierhopper.models import ProviderStatus

    typer.echo(f"TierHopper {__version__} · {config.mode()} mode")
    typer.echo(f"  · files            {config.home()}")
    typer.echo(f"  · keys             {credentials.backend()}")
    try:
        th = _service()
    except Exception as e:  # noqa: BLE001
        _fail(f"could not open the state store: {type(e).__name__}: {e}")
    providers = th.store.list_providers()
    active = [p for p in providers if p.status == ProviderStatus.ACTIVE]
    for p in providers:
        mark = "✓" if p.status == ProviderStatus.ACTIVE else "·"
        snap = th.store.latest_credit(p.id) if p.status == ProviderStatus.ACTIVE else None
        left = f" · {snap.remaining:.1f} {'GPU-hours' if snap.unit == 'gpu_hours' else 'USD'} left" if snap else ""
        state = "connected" if p.status == ProviderStatus.ACTIVE else f"not connected (tierhopper connect {p.id})"
        typer.echo(f"  {mark} {p.name:<16} {state}{left}")
    storage = "on" if th.blobs else "off (full mode only; a job that changes provider starts over)"
    typer.echo(f"  {'✓' if th.blobs else '·'} checkpoints      {storage}")
    registered = _claude_registered()
    claude = "connected" if registered else "not connected (tierhopper install)"
    typer.echo(f"  {'✓' if registered else '·'} Claude Code      {claude}")
    if not active:
        typer.secho("No provider connected yet. Run: tierhopper setup", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    if not registered:
        typer.secho("Claude Code does not know TierHopper yet. Run: tierhopper install", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    _ok("ready")


@app.command()
def watch(job_id: str = typer.Argument(None, help="Follow only this job (default: everything running)."),
          interval: int = typer.Option(60, help="Seconds between checks.")) -> None:
    """Follow running jobs until they finish (local mode: this is what moves a job to the next provider)."""
    import time

    from tierhopper.models import JobStatus

    th = _service()
    live = (JobStatus.RUNNING, JobStatus.QUEUED)
    seen: dict[str, str] = {}
    typer.echo("Following jobs (Ctrl+C stops watching; the jobs keep running in the cloud)…")
    while True:
        th.tick()
        jobs = [j for j in th.store.list_jobs(limit=50)
                if job_id in (None, j.id) and (j.status in live or j.id in seen)]
        for j in jobs:
            if j.status in live:
                where = ", ".join(sorted({a.provider_id for s in th.store.list_shards(j.id)
                                          for a in th.store.list_attempts(s.id) if not a.status.terminal})) or "queue"
                typer.echo(f"  {time.strftime('%H:%M')}  {j.name:<28} {j.status.value:<8} "
                           f"{j.progress * 100:4.0f}%  {where}")
            elif seen.get(j.id) in {s.value for s in live}:
                _job_end(th, j)
            seen[j.id] = j.status.value
        if not any(j.status in live for j in jobs):
            if not seen:
                _ok("nothing is running" if not job_id else f"job is {th.store.get_job(job_id).status.value}")
            return
        time.sleep(max(15, interval))


def _job_end(th, job) -> None:
    if job.status.value == "done":
        _ok(f"{job.name} finished")
        _show_results(th.fetch_results(job.id))
    else:
        typer.secho(f"✗ {job.name}: {job.status.value} · see why: tierhopper logs {job.id}", fg=typer.colors.RED)


def _show_results(got: dict) -> None:
    files = [f for item in got["items"] for f in item["files"] if "/_provider/" not in f.replace("\\", "/")]
    if not files:
        typer.echo(f"  no result files yet (job is {got['status']})")
        return
    typer.echo(f"  results in {got['dest']}")
    for f in files[:12]:
        typer.echo(f"    {Path(f).relative_to(got['dest']) if Path(f).is_relative_to(got['dest']) else f}")
    if len(files) > 12:
        typer.echo(f"    … and {len(files) - 12} more")


@control_app.command("deploy")
def control_deploy() -> None:
    """Create/update the Modal Secret from the Keychain and deploy the control plane."""
    _need("supabase", "The cloud control plane")
    import modal

    from tierhopper import actions, ingest, push
    from tierhopper.control_plane import modal_app

    ingest.ensure_ingest_secret()
    actions.ensure_secret()
    push.ensure_vapid_keys()
    if not credentials.get_secret("control", "dashboard_secret"):
        import secrets as _secrets

        credentials.set_secret("control", "dashboard_secret", _secrets.token_hex(32))

    def env() -> dict[str, str]:
        out = {}
        for provider, field in CONTROL_SECRETS:
            value = credentials.get_secret(provider, field)
            if value:
                out[f"TIERHOPPER_{provider}_{field}".upper()] = value
        return out

    values = env()
    missing = [k for k in ("TIERHOPPER_SUPABASE_SECRET_KEY", "TIERHOPPER_R2_SECRET_ACCESS_KEY") if k not in values]
    if missing:
        _fail(f"missing credentials: {', '.join(missing)}")
    modal.Secret.objects.create(modal_app.SECRET_NAME, values, allow_existing=True)
    modal.Secret.from_name(modal_app.SECRET_NAME).update(values)
    _ok(f"Modal Secret '{modal_app.SECRET_NAME}' updated ({len(values)} values)")

    from tierhopper.adapters.modal_adapter import HF_CACHE_VOLUME

    modal.Volume.objects.create(HF_CACHE_VOLUME, allow_existing=True)
    modal_app.app.deploy(name=modal_app.APP_NAME)
    base = modal.Function.from_name(modal_app.APP_NAME, "web").get_web_url().rstrip("/")
    urls = {"ingest_url": f"{base}/ingest", "action_url": f"{base}/action"}
    for field, value in urls.items():
        credentials.set_secret("control", field, value)
    modal.Secret.from_name(modal_app.SECRET_NAME).update(
        {f"TIERHOPPER_CONTROL_{k.upper()}": v for k, v in urls.items()})
    _ok(f"control plane deployed · scheduler every 5 min · {base}")


@app.command("dashboard-env")
def dashboard_env() -> None:
    """Write dashboard/.env.local (git-ignored) from the Keychain, for local development."""
    from tierhopper import push

    url = credentials.get_secret("supabase", "url")
    if not url:
        _fail("the dashboard needs full mode; run `tierhopper config supabase` first")
    email = credentials.get_secret("dashboard", "allowed_email")
    if not email:
        email = typer.prompt("E-mail allowed to sign in to the dashboard").strip().lower()
        credentials.set_secret("dashboard", "allowed_email", email)
    publishable = credentials.get_secret("supabase", "publishable_key")
    if not publishable:
        publishable = typer.prompt("Supabase publishable (anon) key").strip()
        credentials.set_secret("supabase", "publishable_key", publishable)
    values = {
        "NEXT_PUBLIC_SUPABASE_URL": url,
        "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY": publishable,
        "SUPABASE_SECRET_KEY": credentials.get_secret("supabase", "secret_key") or "",
        "ALLOWED_EMAIL": email,
        "CONTROL_API_URL": (credentials.get_secret("control", "action_url") or "").removesuffix("/action"),
        "CONTROL_API_SECRET": credentials.get_secret("control", "dashboard_secret") or "",
        "NEXT_PUBLIC_VAPID_PUBLIC_KEY": push.ensure_vapid_keys(),
    }
    path = Path.cwd() / "dashboard" / ".env.local"
    if not path.parent.is_dir():
        _fail("run this from the root of a TierHopper checkout (the folder that contains dashboard/)")
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    path.chmod(0o600)
    _ok(f"wrote {path} (git-ignored, mode 600)")


@control_app.command("tick")
def control_tick() -> None:
    """Run one scheduler pass locally (debugging)."""
    _print(_service().tick())


@app.command()
def fetch(job_id: str, dest: str = typer.Option(None, help="Destination folder"),
          partial: bool = typer.Option(False, "--partial", help="Include the last checkpoint if not done"),
          as_json: bool = JSON) -> None:
    """Download a job's results."""
    got = _service().fetch_results(job_id, dest_dir=dest, partial=partial)
    _print(got) if as_json else _show_results(got)


@app.command()
def submit(path: str = typer.Argument(".", help="tierhopper.yaml or its directory"),
           dry_run: bool = typer.Option(False, "--dry-run", help="Only show the plan."),
           follow: bool = typer.Option(False, "--watch", help="Follow the job until it finishes."),
           as_json: bool = JSON) -> None:
    """Submit a job: a folder with your code and a tierhopper.yaml."""
    out = _service().submit_job(path, dry_run=dry_run)
    if as_json:
        return _print(out)
    est = out["estimate"]
    size = f" · about {est['gpu_hours'] * 60:.0f} min of GPU" if est.get("gpu_hours") else ""
    first = out["plan"][0] if out["plan"] else None
    if dry_run:
        typer.echo(f"Plan: {first['provider']} ({first['gpu']}){size}" if first
                   else f"Plan: nothing can run this now. {out.get('next_step', '')}")
        for provider, why in out["not_used"].items():
            typer.echo(f"  not {provider}: {why}")
        return None
    if out.get("needs_approval"):
        typer.secho(f"Needs your OK first: {out['needs_approval']}", fg=typer.colors.YELLOW)
        typer.echo(f"  {out['approve_command']}")
        return None
    where = ", ".join(out["providers"]) or "the queue (no free GPU right now)"
    _ok(f"submitted to {where}{size}")
    typer.echo(f"  job id: {out['job_id']}")
    if follow:
        return watch(job_id=out["job_id"], interval=30)
    typer.echo("  follow it: tierhopper watch")
    return None


@app.command()
def status(job_id: str, as_json: bool = JSON) -> None:
    """Show a job's status (and refresh it from the provider)."""
    out = _service().job_status(job_id)
    if as_json:
        return _print(out)
    typer.secho(f"{out['name']} · {out['status']} · {out['progress'] * 100:.0f}%", bold=True)
    for s in out["stops"]:
        end = f" ({s['end_reason']})" if s["end_reason"] else ""
        typer.echo(f"  {s['provider']:<10} {s['gpu'] or '':<6} {s['status']}{end} · {s['gpu_minutes']} GPU-min")
    if out["status"] == "done":
        typer.echo(f"  get the results: tierhopper fetch {out['job_id']}")
    elif out["status"] == "failed":
        typer.echo(f"  see why: tierhopper logs {out['job_id']}")
    return None


@app.command()
def approve(job_id: str, paid: bool = typer.Option(False, "--paid", help="Also allow a paid provider.")) -> None:
    """Approve a job waiting for your OK."""
    _print(_service().approve(job_id, allow_paid=paid))


@app.command()
def deny(job_id: str) -> None:
    """Deny a pending approval (paid: keep waiting for free credit; otherwise cancel)."""
    _print(_service().deny(job_id))


@app.command()
def pause(job_id: str) -> None:
    """Pause a job (stops running attempts; checkpoints are kept)."""
    _print(_service().pause(job_id))


@app.command()
def cancel(job_id: str) -> None:
    """Cancel a job for good (stops it on the provider)."""
    _print(_service().cancel(job_id))


@app.command()
def resume(job_id: str) -> None:
    """Resume a paused job from its last checkpoint."""
    _print(_service().resume(job_id))


@app.command()
def jobs(status: str = typer.Option(None, help="Filter by status"), limit: int = typer.Option(20)) -> None:
    """List recent jobs with their ids."""
    for j in _service().list_jobs(status=status, limit=limit)["jobs"]:
        typer.echo(f"{j['job_id']}  {j['status']:<18} {j['progress'] * 100:5.1f}%  {j['name']}  ({j['created_at']})")


@app.command()
def logs(job_id: str, lines: int = typer.Option(80, help="Lines per attempt (max 400)"),
         contains: str = typer.Option(None, help="Only lines containing this text")) -> None:
    """Show a job's output lines."""
    for a in _service().job_logs(job_id, lines=lines, contains=contains)["attempts"]:
        typer.secho(f"── {a['provider']} · {a['status']} · {a['source']}", fg=typer.colors.CYAN)
        typer.echo("\n".join(a["lines"]) or "(no output yet)")


@app.command()
def credits(as_json: bool = JSON) -> None:
    """Show free credit left per provider."""
    rows = _service().credits_status()
    if as_json:
        return _print(rows)
    for r in rows:
        if "remaining" in r:
            unit = "GPU-hours" if r["unit"] == "gpu_hours" else "USD"
            resets = f" · renews {r['resets_at'][:10]}" if r.get("resets_at") else ""
            left = f"{r['remaining']:g} {unit} left{resets}"
        else:
            left = f"not connected ({r.get('how_to_connect', '')})" if r["status"] != "active" else "connected"
        typer.echo(f"  {r['name']:<14} {left}")
    return None


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (Claude Code starts this for you)."""
    from tierhopper.mcp_server import main

    main()


# ---- help layout: everyday commands first, advanced ones grouped apart ---------------------------
HELP_PANELS = [
    ("Get started", ["setup", "init", "doctor", "install"]),
    ("Jobs", ["submit", "jobs", "status", "logs", "fetch", "watch", "pause", "resume", "cancel", "approve", "deny"]),
    ("Providers and credit", ["connect", "credits", "report"]),
    ("Full mode (advanced)", ["card", "discover", "dashboard-env", "mcp"]),
]


def _arrange_help() -> None:
    order = {name: (i, j, panel) for i, (panel, names) in enumerate(HELP_PANELS) for j, name in enumerate(names)}

    def name_of(info) -> str:
        return info.name or info.callback.__name__.replace("_", "-")

    for info in app.registered_commands:
        info.rich_help_panel = order.get(name_of(info), (99, 0, "Other"))[2]
    app.registered_commands.sort(key=lambda info: order.get(name_of(info), (99, 0, ""))[:2])
    for group in app.registered_groups:
        group.rich_help_panel = "Full mode (advanced)"


_arrange_help()



def main() -> None:
    """Entry point: one clear line per error; TIERHOPPER_DEBUG=1 shows the full traceback."""
    import os

    from tierhopper.store import NotFound

    try:
        app()
    except KeyboardInterrupt:
        typer.echo("")
        raise SystemExit(130) from None
    except Exception as e:  # noqa: BLE001
        if os.environ.get("TIERHOPPER_DEBUG"):
            raise
        from tierhopper.redact import redact

        if isinstance(e, NotFound):
            text = f"no job or provider with id {e.args[0]!r}. List your jobs with: tierhopper jobs"
        elif isinstance(e, SpecError):
            text = f"{e}\n  A job is a folder with a tierhopper.yaml; create an example with: tierhopper init"
        else:
            text = str(e) if type(e).__name__ in ("SetupNeeded", "AdapterError") else f"{type(e).__name__}: {e}"
        typer.secho(f"✗ {redact(text)[:600]}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
