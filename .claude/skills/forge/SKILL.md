---
name: forge
description: FORGE hackathon kit — give an agent hands-on GPU through Runpod Flash. Use to mint GPU tools at runtime, fan out / cost-meter calls, query GPU stock, and tear down. Loads the verified Flash v1.7.0 API + gotchas. Invoke on hack day (Jun 30) or any Flash task in this repo.
user-invocable: true
---

# FORGE — GPU-on-tap for agents

This repo is a pre-built kit for the Runpod Flash hackathon (Jun 30, $10k). The goal:
walk in with the boring 60% solved and *compose* on the announced theme. The headline
build is **FORGE**: an agent-native control layer over Flash — discover GPU stock, mint a
GPU tool at runtime (~60s, the moat), run/fan-out with live cost, tear down.

**Read first:** `KNOWLEDGE.md` (day-of corpus) and `STRATEGY.md` (the pitch + demo arc).
Deep dives in `research/`. The authoritative external ref is `research/_skills-repo/flash/SKILL.md`.

## Ground truth (verified against installed runpod-flash 1.7.0)
- API is **`Endpoint(...)`**, NOT the old `@remote`/`LiveServerless` (that was v1.4.2; the
  local `/work/flash` checkout is stale — ignore it for API shape).
- CLI in 1.7.0: **`flash run`** (dev server), `flash deploy`, `flash init`, `flash undeploy --all`,
  `flash env`, `flash app`. (Docs/skill may say `flash dev` — that's a newer alias; this account's
  installed CLI uses `flash run`. Verify with `flash --help`.)
- `pip install runpod-flash` → **1.7.0**. GpuGroups: ANY, ADA_24/32_PRO/48_PRO/80_PRO,
  AMPERE_16/24/48/80, HOPPER_141 (no BLACKWELL yet).

## Setup (once)
```bash
cd <repo>
python3 -m venv .venv && .venv/bin/pip install -e .   # installs runpod-flash + mcp
# Auth: prod key from keychain 'runpod-api-key-prod' or env. Confirm WHICH account/key on the day.
```

## Use the kit
```python
import forge
forge.load_env("prod")            # wires all 3 Flash hosts + resolves the API key (the env-recipe gotcha)

# 1) discover live GPU stock (fills the SDK gap — no raw GraphQL, no 403)
opts = await forge.available_gpus(min_vram_gb=24)     # rows incl. .group for Endpoint(gpu=...)

# 2) mint a GPU tool at runtime from an agent-authored code string
tool = forge.mint("bg-remove", gpu="ADA_24", dependencies=["rembg","pillow"], code='''
def handler(payload):
    import base64, io
    from rembg import remove
    from PIL import Image
    img = Image.open(io.BytesIO(base64.b64decode(payload["image_b64"])))
    out = io.BytesIO(); remove(img).save(out, "PNG")
    return {"image_b64": base64.b64encode(out.getvalue()).decode()}
''')

# 3) call / fan-out (first call pays cold start; can exceed 60s)
r = await forge.call(tool, {"image_b64": "..."})      # r.output, r.seconds, r.cost_usd
results = await forge.fanout(tool, [p1, p2, p3])      # gather + worker-capped

# 4) tear down (kills orphan-endpoint sprawl)
forge.undeploy(tool.endpoint_name)   # or forge.undeploy_all()
```

**As an MCP server (Claude Desktop / Cursor):** `python -m forge.server` (stdio). Tools:
`gpu_available, gpu_mint, gpu_call, gpu_fanout, fleet_cost, fleet_list, fleet_cleanup`.

**Flagship beat (the differentiator):** `python -m flagship.lora_sweep` — fan a LoRA
hyperparameter sweep across GPU workers, pick the winner by loss. "Beyond inference."

## Status: LIVE-VALIDATED on prod (Jun 26, 1.7.0)
mint→call→cost→availability→teardown all confirmed working end-to-end. Re-run to reconfirm
on the day (key/account may differ):
```bash
.venv/bin/python -m forge.selftest          # mint→call→assert→cost→safe teardown on ONE real endpoint
```
Real numbers seen: cold start Delay ~60s / Exec ~46ms; availability returns live stock
(4090=High, A40=High, A100=High/Low). Cost ~$0.013 per dep-free call.

## TEARDOWN SAFETY — read before deleting anything
- Teardown is **server-truth + scoped by name**: `forge.undeploy(tool_name)` queries
  `myself.endpoints`, deletes only matches (tolerates the `-fb` suffix), clears the local cache.
- **NEVER `flash undeploy --all`** on a shared account — it kills endpoints you didn't mint
  (the account has `runpod-coder-v1`). FORGE never calls it.
- `Endpoint.id` is always None; the local `flash undeploy list` lags the server — don't trust
  either for "is it gone?". Verify with `await forge.server_endpoints()`.

## Non-negotiable gotchas (these WILL bite)
1. **Only the handler BODY ships to the worker.** Put every import/helper/constant
   INSIDE `def handler(payload)`. Module-level names → `NameError` under `flash run`.
2. **torch is broken under `flash run` (live serverless), works under `flash deploy`.**
   Test torch primitives (the LoRA flagship) via `flash deploy`, not the dev server.
3. **Multi-endpoint build is all-or-nothing** — one dep with no prebuilt wheel fails the whole build. Keep deps lean.
4. **`runsync` times out at 60s** — cold starts exceed it; use `forge.call` (run+wait) for first calls.
5. **10MB payload limit** — pass URLs/volume paths, not big blobs.
6. **No model-cache** with Flash — stage weights on a NetworkVolume (`volume=`), EU-RO-1.
7. **Cost timing is wall-clock** (Flash exposes no delay/exec ms to decorator calls) — fine for $ math.
8. **Teardown footguns (validated):** `Endpoint.id` is None; deployed name has a `-fb` suffix;
   `flash undeploy list` lags the server; out-of-band deletes leave `.runpod/resources.pkl` stale
   (→ `forge.clear_local_cache()`); that pickle holds your API key in plaintext (gitignored).

## Demo arc (≈4 min) — see STRATEGY.md
discover stock → mint (narrate the 60s vs Docker build) → burst + $ ticker → LoRA flagship → `fleet_cleanup`.
Swap the flagship/primitive to fit the announced theme; the spine never changes.
