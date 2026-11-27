"""Checks that a job runs inside its own container image with a working GPU."""
import json
import subprocess
import sys
import time
from pathlib import Path

import torch

smi = subprocess.run(["nvidia-smi"], capture_output=True, text=True).stdout
print(smi)
assert torch.cuda.is_available(), "no GPU visible inside the container"
a = torch.randn(4096, 4096, device="cuda")
b = torch.randn(4096, 4096, device="cuda")
torch.cuda.synchronize()
start = time.time()
for step in range(20):
    c = a @ b
    print(f"step {step + 1} of 20")
torch.cuda.synchronize()
result = {"python": sys.version.split()[0], "torch": torch.__version__, "gpu": torch.cuda.get_device_name(0),
          "matmul_4096_x20_seconds": round(time.time() - start, 3), "checksum": float(c.abs().mean())}
print(json.dumps(result))
Path("results").mkdir(exist_ok=True)
Path("results/result.json").write_text(json.dumps(result, indent=2))
