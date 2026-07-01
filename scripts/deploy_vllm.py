"""Deploy the OFFICIAL Runpod vLLM worker image via Flash's own client-mode SDK calls.

Per docs.runpod.io/flash/custom-docker-images: client-mode image endpoints are called via
the SDK's own `ep.run({"input": {...}})` (QB) or `ep.post(path, data)` (LB) — NOT a hand-built
HTTP URL. A prior version of this script hit a raw guessed `/openai/v1/chat/completions` URL
directly, which produced a `FunctionRequest`/`execution_type` error — that was the wrong
calling convention, not proof the image was misdeployed. Use `.run()` + `{"input": {...}}`
exactly as documented, and read `job.output[0]['choices'][0]['tokens'][0]`.

    python -m scripts.deploy_vllm            # test + teardown
    python -m scripts.deploy_vllm --keep     # leave warm for wiring/demo
"""

from __future__ import annotations

import asyncio
import sys
import time

import forge

IMAGE = "runpod/worker-vllm:stable-cuda12.1.0"
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"


async def main(keep: bool = False) -> int:
    forge.load_env("prod")
    from runpod_flash import Endpoint, GpuGroup, NetworkVolume, PodTemplate

    print(f"deploying vLLM worker: {IMAGE}  model={MODEL}")
    ep = Endpoint(name="dnd-vllm", image=IMAGE, gpu=GpuGroup.ADA_24, workers=(1, 1),
                  idle_timeout=300,
                  volume=NetworkVolume(name="dnd-models", size=30),
                  template=PodTemplate(containerDiskInGb=50),
                  env={"MODEL_NAME": MODEL, "MAX_MODEL_LEN": "8192",
                       "GPU_MEMORY_UTILIZATION": "0.90"})

    payload = {"input": {
        "prompt": 'Generate a dungeon room. JSON: {"name":"...","look":"..."}',
        "max_tokens": 120, "temperature": 0.8,
    }}

    t0 = time.time()
    ok, text = False, None
    print("submitting QB job (waits for model load; up to ~6min) ...")
    try:
        job = await ep.run(payload)
        await job.wait(timeout=360)
        out = job.output
        print(f"\n  RAW OUTPUT ({type(out).__name__}):\n  {out!r}")
        # worker-vllm QB output shape: [{"choices": [{"tokens": ["..."]}]}]
        try:
            text = out[0]["choices"][0]["tokens"][0] if isinstance(out, list) else str(out)
            ok = True
            print(f"\n  ✅ vLLM RESPONDED in {time.time()-t0:.0f}s:\n  {text[:300]}")
        except Exception:
            pass
    except Exception as exc:
        print(f"  t+{int(time.time()-t0)}s FAILED: {type(exc).__name__}: {str(exc)[:300]}")

    if not keep:
        print("\n  teardown ...")
        res = await forge.undeploy("dnd-vllm")
        print(f"  {res}")
    else:
        print(f"\n  --keep: dnd-vllm warm. Set FLASH endpoint env to this id to wire it.")
    print("\nRESULT:", "vLLM WORKS ✅" if ok else "still not responding")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(keep="--keep" in sys.argv)))
