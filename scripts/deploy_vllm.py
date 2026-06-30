"""Deploy the OFFICIAL Runpod vLLM worker via Flash + call its OpenAI HTTP route.

The worker (runpod/worker-v1-vllm:cuda12.1) starts clean. It's reached via the OpenAI-
compatible HTTP route POST https://api.runpod.ai/v2/{id}/openai/v1/chat/completions
(Bearer = account API key) — NOT Flash's ep.post. First call waits for model load, so we
poll with retries until vLLM is serving.

    python -m scripts.deploy_vllm            # test + teardown
    python -m scripts.deploy_vllm --keep     # leave warm for wiring/demo
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request

import forge

# Proven-working image version (copied from runpod-coder-v1's live config; the old
# stable-cuda12.1.0 tag started but vLLM 500'd). Small model via HF id (vLLM downloads ~3GB once).
IMAGE = "runpod/worker-v1-vllm:v2.22.4"
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"


def openai_chat(endpoint_id: str, api_key: str, payload: dict, timeout: int = 45) -> dict:
    url = f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1/chat/completions"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


async def main(keep: bool = False) -> int:
    api_key = forge.load_env("prod")
    from runpod_flash import Endpoint, GpuGroup, NetworkVolume, PodTemplate

    print(f"deploying vLLM worker: {IMAGE}  model={MODEL}")
    # KEY FIX (config diff vs working runpod-coder-v1): attach a network volume so
    # /runpod-volume exists (the worker's BASE_PATH / HF cache); model downloads into it.
    # Also bump container disk to match the working endpoint (50GB).
    ep = Endpoint(name="dnd-vllm", image=IMAGE, gpu=GpuGroup.ADA_24, workers=(1, 1),
                  idle_timeout=300,
                  volume=NetworkVolume(name="dnd-models", size=30),
                  template=PodTemplate(containerDiskInGb=50),
                  env={"MODEL_NAME": MODEL, "MAX_MODEL_LEN": "8192",
                       "DTYPE": "bfloat16", "GPU_MEMORY_UTILIZATION": "0.90"})
    # trigger deploy (don't block long), then resolve the endpoint id
    try:
        await asyncio.wait_for(ep.run({"messages": [{"role": "user", "content": "hi"}]}), timeout=8)
    except Exception:
        pass
    eid = None
    for _ in range(10):
        eps = [e for e in await forge.server_endpoints() if "dnd-vllm" in e["name"]]
        if eps:
            eid = eps[0]["id"]; break
        await asyncio.sleep(3)
    if not eid:
        print("could not resolve endpoint id"); return 1
    print(f"endpoint id: {eid}")

    payload = {"model": MODEL, "max_tokens": 120, "temperature": 0.8, "messages": [
        {"role": "system", "content": "Reply with STRICT JSON only."},
        {"role": "user", "content": 'Generate a dungeon room. JSON: {"name":"...","look":"..."}'}]}

    t0 = time.time()
    ok, text = False, None
    print("polling OpenAI route (waits for model load; up to ~6min) ...")
    for attempt in range(24):
        try:
            resp = openai_chat(eid, api_key, payload, timeout=45)
            text = resp["choices"][0]["message"]["content"]
            ok = True
            print(f"\n  ✅ vLLM RESPONDED in {time.time()-t0:.0f}s:\n  {text[:300]}")
            break
        except Exception as exc:
            msg = str(exc)[:90]
            print(f"  t+{int(time.time()-t0)}s attempt {attempt+1}: not ready ({type(exc).__name__}: {msg})")
            await asyncio.sleep(15)

    if not keep:
        print("\n  teardown ...")
        res = await forge.undeploy("dnd-vllm")
        print(f"  {res}")
    else:
        print(f"\n  --keep: dnd-vllm warm (id={eid}). Set FLASH endpoint env to this id to wire it.")
    print("\nRESULT:", "vLLM WORKS ✅" if ok else "still not responding")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(keep="--keep" in sys.argv)))
