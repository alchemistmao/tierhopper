"""TierHopper job runner. Shipped inside every job package as `_tierhopper_runner.py`.

Standard library only: it runs on any provider image. Configuration comes from the
TH_RUNNER_CONFIG environment variable (JSON). It:
  1. restores the latest checkpoint (presigned GET) into the checkpoint dir, if any;
  2. runs the job entrypoint, streaming its output;
  3. sends heartbeats (progress, GPU metrics) to the ingest endpoint;
  4. uploads the checkpoint dir periodically and on exit (presigned PUT);
  5. stops the job gracefully before the provider's session deadline;
  6. uploads the declared outputs and the log, then reports how the attempt ended.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

EXIT_STOPPED = 75  # stopped early (deadline or preemption) after saving a checkpoint

cfg = json.loads(os.environ.get("TH_RUNNER_CONFIG", "{}"))
urls = cfg.get("urls", {})
work = Path.cwd()
ckpt_dir = work / cfg["checkpoint_dir"] if cfg.get("checkpoint_dir") else None
progress_file = work / cfg["progress_file"] if cfg.get("progress_file") else None
log_path = Path(tempfile.gettempdir()) / "tierhopper-job.log"
tail: deque[str] = deque(maxlen=50)
fresh: deque[str] = deque(maxlen=120)  # lines not yet sent with a heartbeat
stop_reason: str | None = None
child: subprocess.Popen | None = None


def say(msg: str) -> None:
    print(f"[tierhopper] {msg}", flush=True)


def http(method: str, url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 120) -> bytes | None:
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 3:
                say(f"{method} failed: HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                say(f"{method} failed: {type(e).__name__}")
        time.sleep(2 ** attempt)
    return None


def tar_bytes(paths: list[tuple[Path, str]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, arcname in paths:
            if path.exists():
                tar.add(path, arcname=arcname)
    return buf.getvalue()


def dir_signature(path: Path) -> tuple:
    if not path.is_dir():
        return ()
    return tuple(sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in path.rglob("*") if p.is_file()))


def read_progress() -> float | None:
    try:
        return max(0.0, min(1.0, float(progress_file.read_text().strip()))) if progress_file else None
    except (OSError, ValueError):
        return None


def gpu_metrics() -> dict:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,name",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
        util, used, total, name = (x.strip() for x in out.splitlines()[0].split(",", 3))
        return {"gpu_util": float(util), "vram_used_mb": float(used), "vram_total_mb": float(total),
                "gpu_name": name}
    except Exception:  # noqa: BLE001
        return {}


def heartbeat(kind: str, **extra) -> None:
    if not cfg.get("ingest_url"):
        return
    body = {"attempt_id": cfg.get("attempt_id"), "kind": kind, "progress": read_progress(), **gpu_metrics(), **extra}
    http("POST", cfg["ingest_url"], json.dumps(body).encode(),
         {"Content-Type": "application/json", "Authorization": f"Bearer {cfg.get('ingest_token', '')}"}, timeout=30)


def restore_checkpoint() -> bool:
    if not (ckpt_dir and urls.get("ckpt_get")):
        return False
    data = http("GET", urls["ckpt_get"])
    if not data:
        return False
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(ckpt_dir, filter="data")
    say(f"restored checkpoint ({len(data) // 1024} KB)")
    return True


last_ckpt_sig: tuple = ()


def save_checkpoint(force: bool = False) -> None:
    global last_ckpt_sig
    if not (ckpt_dir and urls.get("ckpt_put") and ckpt_dir.is_dir()):
        return
    sig = dir_signature(ckpt_dir)
    if not sig or (sig == last_ckpt_sig and not force):
        return
    data = tar_bytes([(p, str(p.relative_to(ckpt_dir))) for p in ckpt_dir.iterdir()])
    if http("PUT", urls["ckpt_put"], data) is not None:
        last_ckpt_sig = sig
        heartbeat("checkpoint", checkpoint_bytes=len(data))
        say(f"checkpoint saved ({len(data) // 1024} KB)")


def stop_child(reason: str) -> None:
    global stop_reason
    if stop_reason or child is None or child.poll() is not None:
        return
    stop_reason = reason
    say(f"stopping job: {reason}")
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=int(cfg.get("grace_seconds", 90)))
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)


def start_server(env: dict) -> subprocess.Popen | None:
    """Ephemeral OpenAI-compatible endpoint (vLLM) for the job's lifetime; None if not requested."""
    serve = cfg.get("serve")
    if not serve:
        return None
    port = int(serve.get("port", 8000))
    command = serve.get("command")
    if not command:
        try:
            import vllm  # noqa: F401
        except ImportError:
            say("installing vLLM…")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "vllm"], check=True)
        args = " ".join(serve.get("args", []))
        command = (f"{sys.executable} -m vllm.entrypoints.openai.api_server --model {serve['model']} "
                   f"--port {port} --host 127.0.0.1 {args}")
    say(f"starting model server: {serve.get('model', 'custom')}")
    server_log = open(Path(tempfile.gettempdir()) / "tierhopper-server.log", "w")  # noqa: SIM115
    server = subprocess.Popen(command, shell=True, env=env, stdout=server_log, stderr=subprocess.STDOUT,
                              start_new_session=True)
    deadline = time.time() + int(serve.get("startup_timeout_s", 1800))
    while time.time() < deadline:
        if server.poll() is not None:
            raise RuntimeError("model server exited during startup")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                if resp.status == 200:
                    break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    else:
        raise RuntimeError("model server did not become healthy in time")
    env["OPENAI_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
    env["OPENAI_API_KEY"] = "EMPTY"
    env["TH_SERVED_MODEL"] = serve.get("model", "")
    say("model server ready")
    return server


def stop_server(server: subprocess.Popen | None) -> None:
    if server is None or server.poll() is not None:
        return
    os.killpg(server.pid, signal.SIGTERM)
    try:
        server.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(server.pid, signal.SIGKILL)
    say("model server stopped")


def pump(stream, log) -> None:
    for line in stream:
        sys.stdout.write(line)
        sys.stdout.flush()
        log.write(line)
        tail.append(line.rstrip("\n"))
        fresh.append(line.rstrip("\n"))


def main() -> int:
    global child
    resumed = restore_checkpoint()
    env = {**os.environ, **cfg.get("env", {}), "TH_RESUMED": "1" if resumed else "0"}
    if ckpt_dir:
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        env["TH_CHECKPOINT_DIR"] = str(ckpt_dir)
    if progress_file:
        env["TH_PROGRESS_FILE"] = str(progress_file)
    items = cfg.get("items") or []
    if items:  # this shard's share of `shard.over`
        items_file = work / ".tierhopper_items.json"
        items_file.write_text(json.dumps(items))
        env["TH_SHARD_ITEMS_FILE"] = str(items_file)
    env.pop("TH_RUNNER_CONFIG", None)

    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=stop_child, args=("preempted",)).start())
    heartbeat("started", resumed=resumed)
    started = time.time()
    try:
        server = start_server(env)
    except Exception as e:  # noqa: BLE001
        say(f"model server failed: {e}")
        heartbeat("exit", exit_code=1, reason="error", seconds=time.time() - started, tail=[f"model server: {e}"])
        return 1
    with log_path.open("w") as log:
        child = subprocess.Popen(cfg["entrypoint"], shell=True, cwd=work, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
        reader = threading.Thread(target=pump, args=(child.stdout, log), daemon=True)
        reader.start()
        deadline = cfg.get("deadline_ts")
        every_ckpt = int(cfg.get("checkpoint_every_s", 600))
        every_beat = int(cfg.get("heartbeat_every_s", 60))
        next_ckpt, next_beat = time.time() + every_ckpt, time.time() + every_beat
        while child.poll() is None:
            time.sleep(1)
            now = time.time()
            if deadline and now >= deadline:
                stop_child("session_limit")
            if now >= next_beat:
                lines = list(fresh)
                fresh.clear()
                heartbeat("progress", lines=lines)
                next_beat = now + every_beat
            if now >= next_ckpt:
                save_checkpoint()
                next_ckpt = now + every_ckpt
        reader.join(timeout=10)
    stop_server(server)
    code = child.returncode
    seconds = time.time() - started

    save_checkpoint(force=True)
    reason = stop_reason or ("completed" if code == 0 else "error")
    outputs = [(work / rel, rel.rstrip("/")) for rel in cfg.get("outputs", [])]
    if urls.get("results_put") and reason == "completed":
        data = tar_bytes(outputs)
        http("PUT", urls["results_put"], data)
        say(f"results uploaded ({len(data) // 1024} KB)")
    if urls.get("log_put"):
        http("PUT", urls["log_put"], log_path.read_bytes())
    heartbeat("exit", exit_code=code, reason=reason, seconds=seconds, tail=list(tail)[-20:])
    say(f"job ended: {reason} (exit {code}, {seconds:.0f}s)")
    return EXIT_STOPPED if reason in ("session_limit", "preempted") else code


if __name__ == "__main__":
    sys.exit(main())
