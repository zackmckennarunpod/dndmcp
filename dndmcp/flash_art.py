"""Flash art — real SDXL-Turbo image generation via the official runpod-workers image.

Endpoint(image="runpod/sdxl-turbo:latest", ...) — a Runpod-published, pre-built worker
(github.com/runpod-workers/worker-sdxl-turbo, nvidia/cuda:12.1.1 base). Same "pre-built image
over dependencies=[]" lesson flash_llm.py already proved out for vLLM: no pip-install-at-
cold-start conflicts, no CUDA version fighting. Scale-to-zero (workers=(0, N)) so it costs
nothing idle. Auth/host-env setup is shared with flash_llm.py (same account, same Flash
plumbing) rather than duplicated.

Off by default (DND_FLASH_ART!=1) → callers fall back to the ASCII placeholder, so the game
always works. Independent of DND_FLASH_LLM — art and world-gen toggle separately.

enabled() also consults admin_flags — an SSH-side kill switch (scripts/pod_set_flag.sh) that
takes effect with no restart, for backing this out fast if something goes wrong close to a
deadline. DND_FLASH_ART is just the default; admin_flags can override it live either way.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.request

from . import admin_flags, flash_llm

logger = logging.getLogger(__name__)

_ENV_DEFAULT_ENABLED = os.environ.get("DND_FLASH_ART", "0") == "1"


def enabled() -> bool:
    return admin_flags.enabled("flash_art", default=_ENV_DEFAULT_ENABLED)
# runpod/sdxl-turbo has exactly one published tag on Docker Hub — "dev" (there is no
# "latest"). Deploying against ":latest" 400s with "Container image ... was not found on
# the registry" every time — confirmed live, not a hypothetical. See
# https://hub.docker.com/r/runpod/sdxl-turbo/tags
IMAGE = "runpod/sdxl-turbo:dev"
ENDPOINT_NAME = "dnd-art-sdxl-turbo"

_STATE = {"endpoint_id": None}
_LOCK = asyncio.Lock()


async def ensure() -> str:
    """Mint (or reuse) the SDXL-Turbo endpoint and return its resolved endpoint id.

    Same lazy-deploy-then-resolve-id dance as flash_llm.ensure() — Endpoint(...) alone
    doesn't deploy; one throwaway .run() triggers it, then we poll the endpoints list for
    the real id. Locked so concurrent fan-out callers (art prefetch) don't race to construct
    multiple endpoints."""
    if _STATE["endpoint_id"]:
        return _STATE["endpoint_id"]
    async with _LOCK:
        if _STATE["endpoint_id"]:
            return _STATE["endpoint_id"]
        flash_llm._ensure_host_env()  # noqa: SLF001 — shared Flash auth/env setup, not LLM-specific
        from runpod_flash import Endpoint, GpuGroup, PodTemplate
        from runpod_flash.core.api import RunpodGraphQLClient

        # worker-sdxl-turbo's base image is nvidia/cuda:12.1.1 — old/broadly-compatible
        # enough that (unlike vLLM's cuda>=13.0) no min_cuda_version pin is needed to avoid
        # landing on an incompatible host.
        ep = Endpoint(
            name=ENDPOINT_NAME, image=IMAGE, gpu=GpuGroup.ADA_24, workers=(0, 3),
            idle_timeout=300, template=PodTemplate(containerDiskInGb=30),
        )
        try:
            await asyncio.wait_for(ep.run({"input": {}}), timeout=10)
        except Exception:
            pass  # expected — just triggering deploy

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


def _runsync(endpoint_id: str, payload: dict) -> dict:
    body = json.dumps({"input": payload}).encode()
    url = f"https://api.runpod.ai/v2/{endpoint_id}/runsync"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {flash_llm._api_key()}",  # noqa: SLF001
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=150) as resp:  # cold start from zero can take ~90s
        return json.load(resp)


async def generate_image(prompt: str, *, negative_prompt: str = "", width: int = 512,
                         height: int = 512, steps: int = 4, guidance_scale: float = 0.0,
                         seed: int | None = None) -> bytes | None:
    """Generate via Flash (real SDXL-Turbo). Returns raw PNG bytes, or None (-> caller falls
    back to the ASCII placeholder) if disabled/error."""
    if not enabled():
        return None
    try:
        endpoint_id = await ensure()
        payload = {"prompt": prompt, "negative_prompt": negative_prompt, "width": width,
                  "height": height, "num_inference_steps": steps, "guidance_scale": guidance_scale}
        if seed is not None:
            payload["seed"] = seed
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _runsync, endpoint_id, payload)
        if result.get("status") != "COMPLETED":
            logger.warning("flash_art.generate_image: job status %r (not COMPLETED), "
                           "falling back to placeholder", result.get("status"))
            return None
        # worker-sdxl-turbo's actual response shape is `output`: a bare base64 PNG string
        # (optionally data-URL-prefixed) — NOT the {"images": [{"image": ...}]} wrapper an
        # earlier version of this code assumed (confirmed live: that assumption silently
        # 500'd every real generation into the except below, with zero visible signal that
        # art was ever "working" end-to-end until someone went and read the raw response).
        data_url = result["output"]
        b64 = data_url.split(",", 1)[1] if data_url.startswith("data:") else data_url
        return base64.b64decode(b64)
    except Exception:
        logger.warning("flash_art.generate_image: failed, falling back to placeholder", exc_info=True)
        return None


async def worker_status() -> dict:
    """This endpoint's current cached worker status -- see flash_llm._cached_health (shared
    cache/health-check plumbing, keyed by endpoint_id so both modules' checks never collide).
    Used to drive the GUI's warm/cold header badge."""
    return await flash_llm._cached_health(await ensure())  # noqa: SLF001


async def maybe_warm() -> dict:
    """Self-debouncing warm trigger, safe to call from every page load/interaction: only
    actually pays for a real warm() generation (real GPU spend) when nothing's already usable
    or coming up. See flash_llm.maybe_warm for the full state-based rationale -- same
    pattern, same shared cache."""
    if not enabled():
        return {"skipped": "disabled"}
    endpoint_id = await ensure()
    status = await flash_llm._cached_health(endpoint_id)  # noqa: SLF001
    if status["state"] in ("active", "starting"):
        return {"skipped": f"already {status['state']}"}
    return await warm()


async def warm() -> dict:
    """Force a deploy + one generation so the worker is hot before play/recording."""
    import time
    t0 = time.time()
    endpoint_id = await ensure()
    try:
        png = await generate_image("a torch", width=256, height=256, steps=1)
        return {"ok": png is not None, "via": "flash", "bytes": len(png) if png else 0,
                "seconds": round(time.time() - t0, 1)}
    except Exception as exc:
        return {"ok": False, "via": "flash", "error": str(exc), "seconds": round(time.time() - t0, 1)}


async def teardown() -> dict:
    if not _STATE["endpoint_id"]:
        return {"deleted": 0}
    flash_llm._ensure_host_env()  # noqa: SLF001
    from runpod_flash.core.api import RunpodGraphQLClient
    client = RunpodGraphQLClient()
    try:
        res = await client.delete_endpoint(_STATE["endpoint_id"])
    finally:
        await client.close()
    _STATE["endpoint_id"] = None
    return res
