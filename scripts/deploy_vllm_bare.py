"""Deploy the official vLLM worker image via bare runpod_flash — NO forge wrapper at all.

python -m scripts.deploy_vllm_bare
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

# --- auth + host, set directly (no forge) ---
os.environ["RUNPOD_API_BASE_URL"] = "https://api.runpod.io"
os.environ["RUNPOD_REST_API_URL"] = "https://rest.runpod.io/v1"
os.environ["RUNPOD_ENDPOINT_BASE_URL"] = "https://api.runpod.ai/v2"
if not os.environ.get("RUNPOD_API_KEY"):
    key = subprocess.run(
        ["security", "find-generic-password", "-s", "runpod-api-key-prod", "-w"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    os.environ["RUNPOD_API_KEY"] = key

from runpod_flash import Endpoint, GpuGroup, PodTemplate  # noqa: E402

# NOT runpod/worker-vllm:stable-cuda12.1.0 — that Docker Hub repo is abandoned (last
# published July 2024, bundles vLLM 0.4.2, predates Qwen2.5 entirely: model weights load
# but generation silently fails). runpod/worker-v1-vllm is the actively maintained repo
# (v2.22.5 published today); v2.22.4 is the exact tag already proven on runpod-coder-v1.
IMAGE = "runpod/worker-v1-vllm:v2.22.4"
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"


async def main() -> int:
    print(f"deploying vLLM worker (bare SDK, no volume): {IMAGE}  model={MODEL}")
    ep = Endpoint(
        name="dnd-vllm4", image=IMAGE, gpu=GpuGroup.ADA_24, workers=(1, 1),
        idle_timeout=300,
        template=PodTemplate(containerDiskInGb=50),
        env={"MODEL_NAME": MODEL, "MAX_MODEL_LEN": "8192", "GPU_MEMORY_UTILIZATION": "0.90"},
    )

    payload = {"input": {
        "messages": [
            {"role": "user", "content": 'Generate a dungeon room. JSON: {"name":"...","look":"..."}'},
        ],
        "sampling_params": {"max_tokens": 120, "temperature": 0.8},
    }}

    t0 = time.time()
    try:
        job = await ep.run(payload)
        print(f"  job submitted: {job.id}")
        await job.wait(timeout=300)
        print(f"\n  RAW OUTPUT ({type(job.output).__name__}):\n  {job.output!r}")
        return 0
    except Exception as exc:
        print(f"  t+{int(time.time()-t0)}s FAILED: {type(exc).__name__}: {str(exc)[:400]}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
