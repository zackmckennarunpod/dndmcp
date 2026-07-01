"""Flash LLM — real vLLM inference via the proven worker-v1-vllm recipe.

Verified live 2026-06-30: Endpoint(image="runpod/worker-v1-vllm:v2.22.4", ...) + the
OpenAI-compatible chat-completions route. This replaced an earlier, never-actually-working
dependencies=[] transformers approach (three-layer pip version conflicts) — the pre-built
image sidesteps all of that. Scale-to-zero (workers=(0, N)) so it costs nothing idle.

Off by default (DND_FLASH_LLM!=1) → callers fall back to procedural generation, so the
game always works. Flip on to make world-gen run live through Flash.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.request

MODEL = os.environ.get("DND_LLM_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
ENABLED = os.environ.get("DND_FLASH_LLM", "0") == "1"
IMAGE = "runpod/worker-v1-vllm:v2.22.4"
ENDPOINT_NAME = "dnd-llm-vllm"

_STATE = {"endpoint_id": None}
_LOCK = asyncio.Lock()


def _api_key() -> str:
    if os.environ.get("RUNPOD_API_KEY"):
        return os.environ["RUNPOD_API_KEY"]
    key = subprocess.run(
        ["security", "find-generic-password", "-s", "runpod-api-key-prod", "-w"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    os.environ["RUNPOD_API_KEY"] = key
    return key


def _ensure_host_env() -> None:
    os.environ.setdefault("RUNPOD_API_BASE_URL", "https://api.runpod.io")
    os.environ.setdefault("RUNPOD_ENDPOINT_BASE_URL", "https://api.runpod.ai/v2")
    _api_key()


async def ensure() -> str:
    """Mint (or reuse) the vLLM endpoint and return its resolved endpoint id.

    Constructing Endpoint(...) alone does NOT deploy it — Flash deploys lazily on the first
    actual call. So we trigger deploy with one throwaway .run() (its result/error is ignored;
    the QB job.input format hits an unrelated bug, but the call still triggers deployment),
    THEN resolve the real id from the endpoints list. Locked so concurrent fan-out callers
    don't race to construct multiple endpoints."""
    if _STATE["endpoint_id"]:
        return _STATE["endpoint_id"]
    async with _LOCK:
        if _STATE["endpoint_id"]:
            return _STATE["endpoint_id"]
        _ensure_host_env()
        from runpod_flash import CudaVersion, Endpoint, GpuGroup, PodTemplate
        from runpod_flash.core.api import RunpodGraphQLClient

        # worker-v1-vllm:v2.22.4's container declares cuda>=13.0 — without pinning this,
        # Flash can schedule the pod onto an older-driver host, which fails at container-init
        # ("nvidia-container-cli: unsatisfied condition: cuda>=13.0") before the app ever runs.
        ep = Endpoint(
            name=ENDPOINT_NAME, image=IMAGE, gpu=GpuGroup.ADA_24, workers=(0, 3),
            idle_timeout=300, template=PodTemplate(containerDiskInGb=50),
            min_cuda_version=CudaVersion.V13_0,
            env={"MODEL_NAME": MODEL, "MAX_MODEL_LEN": "8192", "GPU_MEMORY_UTILIZATION": "0.90",
                 # verified 2026-07-01 on a disposable test endpoint: only changes behavior for
                 # requests that include tools[]; plain chat (world-gen's calls) is unaffected.
                 "ENABLE_AUTO_TOOL_CHOICE": "true", "TOOL_CALL_PARSER": "hermes"},
        )
        try:
            await asyncio.wait_for(ep.run({"input": {}}), timeout=10)
        except Exception:
            pass  # expected — just triggering deploy; the QB job.input path 400s harmlessly

        client = RunpodGraphQLClient()
        try:
            for _ in range(10):
                r = await client._execute_graphql("query { myself { endpoints { id name } } }", {})  # noqa: SLF001
                for e in r["myself"]["endpoints"]:
                    if e["name"] == ENDPOINT_NAME:
                        _STATE["endpoint_id"] = e["id"]
                        return e["id"]
                await asyncio.sleep(2)
            raise RuntimeError(f"{ENDPOINT_NAME!r} not found after deploy")
        finally:
            await client.close()


def _chat_sync(endpoint_id: str, messages: list[dict], max_tokens: int, temperature: float) -> str:
    body = json.dumps({
        "model": MODEL, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }).encode()
    url = f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1/chat/completions"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=150) as resp:  # cold start from zero can take ~90s
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


async def generate(messages: list[dict], *, max_tokens: int = 250,
                   temperature: float = 0.9) -> str | None:
    """Generate via Flash (real vLLM). Returns None (→ caller falls back) if disabled/error."""
    if not ENABLED:
        return None
    try:
        endpoint_id = await ensure()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _chat_sync, endpoint_id, messages, max_tokens, temperature)
    except Exception:
        return None


async def warm() -> dict:
    """Force a deploy + one generation so the worker is hot before play/recording."""
    import time
    t0 = time.time()
    endpoint_id = await ensure()
    try:
        text = await asyncio.get_running_loop().run_in_executor(
            None, _chat_sync, endpoint_id, [{"role": "user", "content": "Say 'ready'."}], 8, 0.5)
        return {"ok": True, "via": "flash", "sample": text, "seconds": round(time.time() - t0, 1)}
    except Exception as exc:
        return {"ok": False, "via": "flash", "error": str(exc), "seconds": round(time.time() - t0, 1)}


async def teardown() -> dict:
    if not _STATE["endpoint_id"]:
        return {"deleted": 0}
    _ensure_host_env()
    from runpod_flash.core.api import RunpodGraphQLClient
    client = RunpodGraphQLClient()
    try:
        res = await client.delete_endpoint(_STATE["endpoint_id"])
    finally:
        await client.close()
    _STATE["endpoint_id"] = None
    return res
