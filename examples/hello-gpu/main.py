"""Prints GPU info, runs a matmul benchmark and writes results/result.json."""

import json
import os
import subprocess
import time
from pathlib import Path

import torch


def main() -> None:
    out = Path("results")
    out.mkdir(exist_ok=True)
    info = {"cuda": torch.cuda.is_available()}
    if info["cuda"]:
        props = torch.cuda.get_device_properties(0)
        info.update(gpu=props.name, vram_gb=round(props.total_memory / 2**30, 1),
                    device_count=torch.cuda.device_count())
        a = torch.randn(4096, 4096, device="cuda", dtype=torch.float16)
        b = torch.randn(4096, 4096, device="cuda", dtype=torch.float16)
        torch.cuda.synchronize()
        start = time.perf_counter()
        for _ in range(50):
            a @ b
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - start
        info["tflops"] = round(50 * 2 * 4096**3 / elapsed / 1e12, 1)
    try:
        smi = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
        info["nvidia_smi"] = smi.stdout.strip()
    except FileNotFoundError:
        info["nvidia_smi"] = None
    info["shard"] = os.environ.get("TH_SHARD_INDEX", "0")
    (out / "result.json").write_text(json.dumps(info, indent=2))
    print("TIERHOPPER_RESULT " + json.dumps(info))


if __name__ == "__main__":
    main()
