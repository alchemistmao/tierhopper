"""Runs the real runner script locally against a tiny HTTP server standing in for R2 + ingest."""

import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "src" / "tierhopper" / "runner_script.py"


class Fake(BaseHTTPRequestHandler):
    objects: dict[str, bytes] = {}
    beats: list[dict] = []

    def log_message(self, *a):
        pass

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_PUT(self):
        self.objects[self.path] = self._body()
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        data = self.objects.get(self.path)
        self.send_response(200 if data is not None else 404)
        self.end_headers()
        if data is not None:
            self.wfile.write(data)

    def do_POST(self):
        self.beats.append(json.loads(self._body()))
        self.send_response(200)
        self.end_headers()


def run(tmp_path, script, extra_cfg=None):
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    (tmp_path / "job.py").write_text(script)
    cfg = {
        "attempt_id": "a1", "entrypoint": f"{sys.executable} job.py", "outputs": ["results/"],
        "checkpoint_dir": "ckpt", "progress_file": ".progress", "ingest_url": f"{base}/ingest",
        "ingest_token": "t", "heartbeat_every_s": 1, "checkpoint_every_s": 1, "grace_seconds": 5,
        "urls": {"ckpt_get": f"{base}/ckpt", "ckpt_put": f"{base}/ckpt", "results_put": f"{base}/results",
                 "log_put": f"{base}/log"},
        **(extra_cfg or {}),
    }
    env = {**os.environ, "TH_RUNNER_CONFIG": json.dumps(cfg)}
    proc = subprocess.run([sys.executable, str(RUNNER)], cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=60)
    srv.shutdown()
    return proc


JOB = """
import os, pathlib, time
ck = pathlib.Path(os.environ["TH_CHECKPOINT_DIR"]) / "step.txt"
start = int(ck.read_text()) if ck.exists() else 0
for step in range(start, 12):
    tmp = ck.with_suffix(".tmp")
    tmp.write_text(str(step + 1))
    tmp.replace(ck)  # atomic: a kill mid-write never leaves a truncated checkpoint
    pathlib.Path(os.environ["TH_PROGRESS_FILE"]).write_text(str((step + 1) / 12))
    time.sleep(0.5)
pathlib.Path("results").mkdir(exist_ok=True)
pathlib.Path("results/out.txt").write_text(f"resumed_from={start}")
"""


def test_completes_and_uploads(tmp_path):
    Fake.objects.clear()
    Fake.beats.clear()
    proc = run(tmp_path, JOB)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with tarfile.open(fileobj=io.BytesIO(Fake.objects["/results"]), mode="r:gz") as tar:
        assert tar.extractfile("results/out.txt").read() == b"resumed_from=0"
    exit_beat = Fake.beats[-1]
    assert exit_beat["kind"] == "exit" and exit_beat["reason"] == "completed"
    assert exit_beat["progress"] == 1.0
    assert "/ckpt" in Fake.objects and "/log" in Fake.objects


def test_deadline_stops_then_resume_continues(tmp_path):
    Fake.objects.clear()
    Fake.beats.clear()
    import time

    proc = run(tmp_path, JOB, {"deadline_ts": time.time() + 1})
    assert proc.returncode == 75, proc.stdout
    assert Fake.beats[-1]["reason"] == "session_limit"
    assert "/results" not in Fake.objects
    saved = Fake.objects["/ckpt"]

    second = tmp_path / "second"
    second.mkdir()
    Fake.objects["/ckpt"] = saved
    Fake.beats.clear()
    proc = run(second, JOB)
    assert proc.returncode == 0, proc.stdout
    with tarfile.open(fileobj=io.BytesIO(Fake.objects["/results"]), mode="r:gz") as tar:
        resumed_from = int(tar.extractfile("results/out.txt").read().split(b"=")[1])
    assert resumed_from > 0
    assert Fake.beats[0]["resumed"] is True


SERVER = """
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""

SERVE_JOB = """
import os, pathlib, urllib.request
base = os.environ["OPENAI_BASE_URL"]
pathlib.Path("results").mkdir(exist_ok=True)
health = urllib.request.urlopen(base.replace("/v1", "/health")).read().decode()
pathlib.Path("results/out.txt").write_text(base + " " + health)
"""


def test_ephemeral_server_is_started_and_stopped(tmp_path):
    import socket

    Fake.objects.clear()
    Fake.beats.clear()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (tmp_path / "server.py").write_text(SERVER)
    serve = {"model": "fake", "port": port, "command": f"{sys.executable} server.py {port}", "startup_timeout_s": 30}
    proc = run(tmp_path, SERVE_JOB, {"serve": serve, "checkpoint_dir": None})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "model server ready" in proc.stdout and "model server stopped" in proc.stdout
    with tarfile.open(fileobj=io.BytesIO(Fake.objects["/results"]), mode="r:gz") as tar:
        assert tar.extractfile("results/out.txt").read().decode() == f"http://127.0.0.1:{port}/v1 ok"
    with socket.socket() as s:  # the server is gone
        assert s.connect_ex(("127.0.0.1", port)) != 0
