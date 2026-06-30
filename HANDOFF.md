# DNDMCP — Handoff / Resume Point

**Date:** 2026-06-30 (hackathon day; video due Wed 12pm PST). Account: **CLEAN** (only
`runpod-coder-v1` remains; zero forge leaks). Read this first, then BUILD.md / REQUIREMENTS.md /
WORLD_SCHEMA.md / MCP_SURFACE.md / JUDGING.md / STRATEGY.md / ideas/.

## What this is
**DNDMCP** — an MCP server that turns any agent harness into a Dungeon Master for a solo RPG in
**The Sundered Weave** (original setting: runaway-AI-magic collapsed civilization; the world's
"magic" is AI — thematic wink for a Flash/AI hackathon). The MCP **proxies GPU generation to
Runpod Flash** ("an MCP that proxies requests to GPUs"). All through tools; terminal-rendered
(text/ASCII), GUI map optional.

## ✅ WORKS NOW (playable, zero-GPU fallback)
- Game engine + DM persona — proven in a real Sonnet 5 session. Tools: start_adventure, look,
  move, roll_dice, attack, character_sheet, get_state (+ be_the_dm prompt).
- Persistent world graph (SQLite, `dndmcp/state.py`): campaign, character, rooms(+features+exits), log.
- **SRD compendium** (`dndmcp/compendium.py`): 334 real monsters + 15 conditions vendored
  (`dndmcp/srd/`), wired into combat → rules-accurate (real AC/HP/attacks/traits).
- **Liveness**: room features, ambient events, real monsters → fixed "not alive" feedback (procedural).
- **World-builder** (`dndmcp/worldgen.py`): structured directional room gen via Flash LLM, with
  procedural fallback. Setting bible (`dndmcp/setting.py`) injected into all gen.
- Container brain (`Dockerfile`, `dndmcp/app.py`): MCP(HTTP)+GUI together; builds + runs locally.
- GUI map (`dndmcp/web.py`): live world map synced to DB. Local dev setup (`dndmcp/SETUP.md`,
  `claude_desktop_config.snippet.json`).
- **forge kit** (`forge/`): the Flash GPU-proxy layer — mint/call/fanout/cost/teardown/diagnostics.
  Live-validated days ago (selftest, cross-silicon, evolver).

## 🟢 FLASH LLM — THE RECIPE (resolved the saga; resume here)
After a long debugging marathon, the working pattern for an LLM on Flash is the **canonical
class pattern from our OWN repos** (`tetra-rp/tetra-examples/2_ml_inference/llm_inference/vllm_inference.py`
and `coding-model` → runpod-coder-v1). NOT a cloudpickle function handler, NOT the
worker-v1-vllm image-without-a-staged-model. **The script `scripts/deploy_vllm_class.py` has it:**

```python
@Endpoint(name="dnd-llm", gpu=GpuGroup.AMPERE_24, workers=(1,1),
          dependencies=["vllm==0.7.3", "transformers==4.48.2"])   # PINNED — see why below
class DnDLLM:
    def __init__(self):
        import os; from vllm import LLM, SamplingParams
        os.environ["VLLM_USE_V1"]="0"; os.environ["VLLM_WORKER_MULTIPROC_METHOD"]="spawn"
        self.llm = LLM(model="Qwen/Qwen2.5-1.5B-Instruct", enforce_eager=True,
                       gpu_memory_utilization=0.6, max_model_len=2048)
    def chat(self, messages, ...): ...   # build im_start prompt, self.llm.generate(...)
```

**Errors cleared, in order (each was a real root cause):**
1. Silent 500s (worker-v1-vllm IMAGE) — unreadable. The CLASS executor RETURNS exceptions → debuggable. Switch to the class.
2. `libcudart.so.13: cannot open shared object` — latest `vllm` is built for CUDA 13; flash:latest container is CUDA 12 → **pin `vllm==0.7.3`** (CUDA-12, supports Qwen2.5).
3. `Could not import ProcessorMixin` — vllm 0.7.3 ↔ transformers mismatch → **pin `transformers==4.48.2`**.
4. (state at context-clear) Redeploying with BOTH pinned; was still in the big cold-start
   (vllm+transformers install + model load, the longest yet), NO error yet — promising.

**Stability env vars are mandatory** (from the tetra example): `VLLM_USE_V1=0`,
`VLLM_WORKER_MULTIPROC_METHOD=spawn`, `enforce_eager=True`, `gpu_memory_utilization=0.6`.

**RESUME STEP:** `RUNPOD_API_KEY=$(security find-generic-password -s runpod-api-key-prod -w) \
.venv/bin/python -m scripts.deploy_vllm_class --keep` → if it returns the JSON room, **loop closed**:
then point `dndmcp/worldgen.py` / `flash_llm.py` at this endpoint (the class is callable as
`await DnDLLM().chat(messages)`). If another dep error, keep pinning (next likely: a tokenizers/
torch pin) — the class executor will show the exact error in the job output.

**Debugging tip (hard-won):** worker logs are NOT reachable from automation (job-logs API =
empty/lifecycle only; pod-logs hapi.runpod.net = Cloudflare 403 even w/ JWT; Datadog = scheduler
only). The CLASS pattern returning exceptions as job output is the ONLY agent-visible error channel.
5 Flash gotchas recorded to Context DB (`find_learnings('flash')`).

## 🔴 (historical) BLOCKED — the critical Flash anchor (LLM world-gen)
**Goal:** a small LLM running on Flash generates structured world content (rooms/NPCs/lore) as you
explore → written to DB. Code is DONE (`dndmcp/flash_llm.py`, `worldgen.py` wired, stub fallback).
**Blocker:** the Flash worker (`runpod/flash:latest`, used by forge.mint decorator mode) requires
**CUDA≥12.8**; workers landing on older-driver hosts crash at container-init
(`nvidia-container-cli: unsatisfied condition: cuda>=12.8`) → crash-loop → job stuck inQueue.
Intermittent (host-dependent) — that's why earlier raw-torch tests worked (good hosts).

### Resolution paths (pick one next session)
1. **Pin CUDA on the resource config (smallest change):** Endpoint ctor has no cuda kwarg, but
   `endpoint._build_resource_config()` returns a `LiveServerless` with a settable `cudaVersions`
   field (default `[]`). Set `cudaVersions=[CudaVersion.V12_8]` before deploy, or patch
   `forge.minting.mint` to accept + apply it. Risk: shrinks host pool → possible allocation hangs.
2. **Client-mode vLLM image (RECOMMENDED for LLM serving):** `Endpoint(image="<vllm-image>", ...)`
   with an image built on a CUDA the hosts support (error says "use an earlier cuda container").
   Solves BOTH the cuda mismatch AND avoids cloudpickle-handler model-load fragility. Standard way
   to serve LLMs on Runpod serverless. Wire `flash_llm`/`inference` to call its /v1/chat/completions.
3. **Retry/datacenter:** flaky, not recommended alone.

NOTE: the defensive-handler diagnostic (return errors) does NOT help here — the crash is at
CONTAINER INIT, before the handler runs. The fix is host/image cuda, not handler code.

## 🛠 Debugging Flash (hard-won — see Context DB learnings)
- **Container-init/worker-crash errors are in POD logs, NOT job logs.** `/v2/{endpointId}/logs`
  is empty for pre-handler crashes. Use `GET https://hapi.runpod.net/v1/pod/{podId}/logs` (console
  Clerk JWT; pod id from getEndpointFull pods[].id). BUILD THIS INTO forge.diagnostics next.
- Allocation hangs: job inQueue + 0 workers >2min = kill (use /v2/{id}/health). ADA_24 most reliable.
- Teardown: server-truth + scoped (forge.undeploy); never `flash undeploy --all` (shared account).
- We have **Datadog** (via mcp__context__exec ddLogs) — use it for Flash worker observability.
- 4 Flash gotchas recorded to Context DB (find_learnings query 'flash').

## Requirements & specs (consolidated)
- **REQUIREMENTS.md** — full requirement table (R1-R12), Flash anchor priority, multiplayer tiers, vision.
- **WORLD_SCHEMA.md** — the graph schema for "playing DM" (nodes/edges/state/read+write tools).
- **MCP_SURFACE.md** — all tools + skills + resources, MVP-flagged.
- **BUILD.md** — locked build plan + art/GPU strategy. **JUDGING.md** — 4 pillars + how we win.
- **STRATEGY.md** — thesis. **ideas/** — dndmcp, gpu-tools, cross-silicon-oracle, models-reference.

## Locked decisions
- Project: DNDMCP solo RPG, install-from-any-harness, terminal-rendered, all through MCP tools.
- Flash anchor priority: **world-builder LLM (primary)** > images (deprioritized) > ask_npc (deferred, built+stubbed).
- SQLite + tool-mediated writes (NOT Dolt). Postgres/Dolt/Restate = scale/production path, pitch don't build.
- Setting: The Sundered Weave (original; AI-magic-collapse; on-theme).
- Model: Qwen2.5-1.5B generic baseline; D&D-tuned `chendren/dnd-unified-1.5b` is the swap candidate
  (set DND_LLM_MODEL). Image gen: `0xJustin/Dungeons-and-Diffusion` (later).

## NEXT (priority order, ~remaining time to Wed 12pm PST)
1. **UNBLOCK FLASH** (critical): resolution path 2 (vLLM image) or 1 (pin cudaVersions). Verify with
   `scripts/verify_flash_worldgen.py` (8-check plan: deploy/structured/multi-skill/scale/DB/latency/fallback/teardown).
2. Wire `forge.diagnostics` to fetch POD logs (hapi.runpod.net) so Flash debugging is one call.
3. Once Flash world-gen verified live → record video (the deliverable). README/install (task #4).
4. Optional if time: ask_npc live (same endpoint), images, multiplayer messaging.

## How to resume
- Venv `.venv` (pip install -e . done). Auth: keychain `runpod-api-key-prod`. `import forge; forge.load_env('prod')`.
- Play locally: GUI `DNDMCP_STATE_DIR=~/.dndmcp_dev .venv/bin/python -m dndmcp.web` (:8001) +
  Claude Desktop stdio config (dndmcp/SETUP.md). Reset: `start_adventure` or rm campaign.db.
- Flash work: ALWAYS pre-check account clean (`forge.server_endpoints()`), tear down after, watch cost.
- Tasks tracked in TaskList (#2 Flash anchor = the blocker; #4 README; #5 video; #6 container amd64 push needs `docker login`).
