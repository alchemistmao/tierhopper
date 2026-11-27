"""The function Modal runs for a job. This file has no imports of its own on purpose: it is either pickled by
value (default image) or copied alone into the job's own image (`image:` in tierhopper.yaml), where
TierHopper is not installed and Python may be another version."""

from __future__ import annotations


def _run_job(package: bytes, command: str, runner_config: str, outputs: list[str]) -> dict:
    """Executed inside the Modal container. Must stay self-contained: standard library only."""
    import io
    import os
    import subprocess
    import tarfile
    import time
    from collections import deque
    from pathlib import Path

    work = Path("/job")
    work.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as tar:
        tar.extractall(work, filter="data")
    started = time.time()
    tail: deque[str] = deque(maxlen=200)
    env = {**os.environ, "TH_RUNNER_CONFIG": runner_config, "HF_HOME": "/root/.cache/huggingface"}
    proc = subprocess.Popen(command, shell=True, cwd=work, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        tail.append(line.rstrip("\n"))
    code = proc.wait()
    buf = io.BytesIO()  # provider-native fallback when R2 is not configured
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in outputs:
            p = work / rel
            if p.exists():
                tar.add(p, arcname=rel.rstrip("/"))
    return {"exit_code": code, "seconds": time.time() - started, "log_tail": list(tail),
            "outputs": buf.getvalue()}
