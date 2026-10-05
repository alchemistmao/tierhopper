"""Counts to 40 (5 s per step), saving its position so it can resume anywhere."""

import json
import os
import socket
import time
from pathlib import Path

STEPS = 40
ckpt = Path(os.environ.get("TH_CHECKPOINT_DIR", "checkpoints")) / "state.json"
state = json.loads(ckpt.read_text()) if ckpt.exists() else {"step": 0, "hosts": []}
state["hosts"].append({"host": socket.gethostname(), "from_step": state["step"]})
print(f"starting at step {state['step']} (resumed={os.environ.get('TH_RESUMED')})", flush=True)

for step in range(state["step"], STEPS):
    time.sleep(5)
    state["step"] = step + 1
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    tmp = ckpt.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(ckpt)  # atomic: the runner may stop the job at any moment
    Path(os.environ.get("TH_PROGRESS_FILE", ".progress")).write_text(str(state["step"] / STEPS))

Path("results").mkdir(exist_ok=True)
Path("results/summary.json").write_text(json.dumps(state, indent=2))
print("done", json.dumps(state), flush=True)
