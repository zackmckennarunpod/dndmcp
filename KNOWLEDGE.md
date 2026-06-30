# Flash Hackathon — Master Knowledge Corpus
**Event:** Tue Jun 30, 2026 · **Prize:** $10k · **Goal:** walk in with the boring 60% solved, compose day-of.

> **Single source of truth, in priority order:**
> 1. **`research/_skills-repo/flash/SKILL.md`** — the official `flash` skill, verified against **v1.17.0** + 5 passing evals. Trust this over everything.
> 2. This file (KNOWLEDGE.md) — synthesized, kept aligned to v1.17.0.
> 3. `research/*.md` — deep-dive digests. ⚠ `01-python-sdk.md` and `02-ts-sdk.md` were read from a **stale v1.4.2 checkout** (`/work/flash`, Feb 27) and describe the OLD `@remote`/`LiveServerless` API. Use them only for internals/mechanics, NOT for current API shape.

---

## ⚠ VERSION TRUTH (read first — VERIFIED by installing the package)
- **Current Flash = v1.7.0** (PyPI latest; `pip install runpod-flash` gives this). The ecosystem skill said "1.17.0" — that was a misread; available versions are 1.0.0 … 1.7.0.
- Local `/work/flash` is **v1.4.2 (4 months stale)** — do NOT code against it.
- **The API changed.** Old (v1.4.2): `@remote(resource_config=LiveServerless(...))`. **Current (1.7.0): `Endpoint(name=..., gpu=..., workers=..., dependencies=[...])`** (apply to a function via `Endpoint(...)(handler)`; `remote` is deprecated → `Endpoint`).
- **CLI in 1.7.0 = `flash run`** (dev server), `flash deploy`, `flash init`, `flash undeploy [--all|list|--interactive|--cleanup-stale]`, `flash env`, `flash app`, `flash build`, `flash login`. (Docs/skill mention `flash dev` — a newer alias; the installed 1.7.0 CLI uses `flash run`. Always `flash --help` on the day.)
- Verified `Endpoint(...)` params: `name, id, gpu, cpu, workers=(min,max), idle_timeout=60, dependencies, system_dependencies, accelerate_downloads=True, volume, datacenter=EU_RO_1, env, gpu_count=1, execution_timeout_ms=0, flashboot=True, image, scaler_type, scaler_value=4, template`. (No `min_cuda_version`/`max_concurrency`/BLACKWELL in 1.7.0 — those are doc-ahead.)
- `runsync(input, timeout=60.0)` and `run(input, webhook=) -> EndpointJob`; `EndpointJob` exposes `output/error/status/done/id/wait/cancel` — **no delay/exec timing** (cost = wall-clock × rate).
- Python 3.10–3.13. Install: `pip install runpod-flash` (or `uv tool install`). Don't `pip install` the local repo.

---

## ✅ FULLY VERIFIED on prod (Jun 27) — every core capability runs
`python -m scripts.verify_ideas` — 8/8 checks pass, zero leaked resources (independently confirmed: only `runpod-coder-v1` + your 6 volumes remain):
1. availability returns live stock (41 GPUs, 12 in stock)
2. mint → call (dep-free) → correct result
3. fan-out / burst (6 payloads, all ok)
4+5. real pip dep install (torch) + GPU/CUDA used — ran on RTX 4090, kernel timed 0.0155ms
6. flagship mechanism — torch installed on 3 workers in parallel, timed 3 configs, picked winner (n=1024 @ 0.007ms)
7. MCP server stdio handshake — all 7 tools exposed to a client
8. teardown — server-truth + scoped, zero forge leaks

### ⚠ OPERATIONAL FINDING — cold start is HIGHLY variable: 60s–550s
Same dep-free tool cold-started in **63s on Jun 26 but 547s (~9 min) on Jun 27** (provisioning queue/contention). torch tool ~551s. ⇒ **NEVER mint cold on stage.** Pre-warm the demo pool ahead of time (`forge.warm` / `flash run --auto-provision`, `workers=(1,_)`), and keep ONE narrated live-mint where a 60s+ wait is the *point* (the Docker-vs-runtime moat beat) — but have a warm fallback in case it spikes to minutes.

## ✅ LIVE-VALIDATED on prod (Jun 26, runpod-flash 1.7.0)
Ran the FORGE spine end-to-end against the real account. Confirmed working:
- **mint → call → result**: `Endpoint(name,gpu,workers,dependencies)(handler)` deploys on first call; returns handler output. Real cold start: **Delay ~57–67s, Exec ~46ms** (dep-free tool) — provision-dominated, matches doctrine. Warm would be ~1s.
- **Cost readout**: wall-clock × rate works (~$0.013/dep-free call). Flash exposes NO delay/exec ms to decorator calls — wall-clock is the signal.
- **Availability (the gap-filler) WORKS**: query `gpuTypes { lowestPrice(input: $lp){ stockStatus uninterruptablePrice } }` with `$lp: GpuLowestPriceInput!` and `input={gpuCount:1}` (REQUIRED, non-null) returns live stock — e.g. RTX 4090=High, A40=High, A100 SXM 80GB=High, A100 PCIe=Low. (A null/missing input errors per-row — that's the bug to avoid.)

### ⚠ TEARDOWN — the part that bites (validated the hard way; never lose endpoints again)
- **`Endpoint.id` stays `None`** after a decorator call — you can't read the deployed id off the object.
- **Deployed name gets a `-fb` suffix** (flashboot): `my-tool` → `my-tool-<hash>-fb`. `flash undeploy my-tool` (bare) silently no-ops.
- **`flash undeploy list` reads a LOCAL cache** (`.runpod/resources.pkl`) that LAGS the server — right after deploy it can show 0 while the endpoint is live → silent leak. **Resolve teardown from SERVER truth**: `query { myself { endpoints { id name } } }`, match your name, delete by id.
- **NEVER `flash undeploy --all` on a shared account** — it deletes EVERY endpoint, including pre-existing ones you didn't mint (the account has `runpod-coder-v1`). Delete scoped, by name.
- **Out-of-band GraphQL delete leaves the local pickle stale** → next same-named mint tries to UPDATE a dead endpoint ("Endpoint not found"). After deleting, `forge.clear_local_cache()` (rm `.runpod/resources.pkl`).
- **`.runpod/resources.pkl` stores your API key in PLAINTEXT** — gitignored. Don't commit it.
- `forge.undeploy(name)` / `forge.undeploy_tools([...])` do all of the above safely.

## The mental model
- **You write Python locally. `flash dev` runs your decorated functions on REMOTE Runpod GPU/CPU workers** with hot-reload + live worker logs streamed to your terminal. No Docker build loop.
- **`flash deploy`** builds an artifact and ships a stable endpoint (slow: build+upload+provision). Only after code works under `flash dev`.
- `flash init` scaffolds a project and writes `AGENTS.md` (+`CLAUDE.md` symlink) so agents get rules.
- Apps contain **environments** (dev/staging/prod). Endpoints live under an environment.

## The 3 Endpoint modes
| Params | Mode | Call style |
|---|---|---|
| `name=` only | **Decorator (your code)** | `await fn(data)` |
| `name=` + `@api.post/get` routes | **Load-balanced (your code)** | HTTP routes share one worker pool |
| `image=` set | **Client (pre-built Docker image)** | `await ep.post(...)` / `await ep.run(...)` |
| `id=` set | **Client (existing endpoint, no provision)** | `await ep.runsync(...)` |

```python
from runpod_flash import Endpoint, GpuGroup

# Mode 1 — queue-based decorator (one function = one endpoint + its own workers)
@Endpoint(name="my-worker", gpu=GpuGroup.AMPERE_80, workers=5, dependencies=["torch"])
async def compute(data):
    import torch                      # MUST import inside the body (cloudpickle ships body only)
    return {"sum": torch.tensor(data, device="cuda").sum().item()}
result = await compute([1,2,3])

# Mode 2 — load-balanced routes (multiple routes, shared pool)
api = Endpoint(name="my-api", gpu=GpuGroup.ADA_24, workers=(1,5), dependencies=["torch"])
@api.post("/predict")
async def predict(data: list[float]): import torch; return {"r": torch.tensor(data).sum().item()}
@api.get("/health")
async def health(): return {"status":"ok"}

# Mode 3 — client (deploy an image, or connect by id)
server = Endpoint(name="vllm", image="org/img:latest", gpu=GpuGroup.AMPERE_80, workers=1, env={"HF_TOKEN":"x"})
out = await server.post("/v1/completions", {"prompt":"hi"})
ep  = Endpoint(id="abc123"); job = await ep.runsync({"prompt":"hi"}, timeout=120)
```

## Endpoint constructor (the params that matter)
`name`(req) · `id` · `gpu=GpuGroup|GpuType|list` · `cpu=CpuInstanceType` (mutually exclusive w/ gpu) · `workers=5`→`(0,5)` or `(min,max)` · `max_concurrency=1` (raise for I/O-bound LB) · `idle_timeout=60` (seconds!) · `dependencies=[pip]` · `system_dependencies=[apt]` · `image=` · `env={}` · `volume=NetworkVolume(name,size)` · `datacenter=DataCenter.X|list|str` · `gpu_count=1` · `template=PodTemplate(containerDiskInGb=64,...)` · `flashboot=True` · `accelerate_downloads=True` · `min_cuda_version=CudaVersion.V12_8` · `scaler_type` · `scaler_value=4` · `execution_timeout_ms=0`.

## GPU tiers (GpuGroup — picks cheapest available in tier)
`ANY` · `AMPERE_16`(A4000/4000Ada,16G) · `AMPERE_24`(A5000/L4/3090,24G) · `AMPERE_48`(A40/A6000,48G) · `AMPERE_80`(A100,80G) · `ADA_24`(4090,24G) · `ADA_32_PRO`(5090,32G) · `ADA_48_PRO`(6000Ada/L40/L40S,48G) · `ADA_80_PRO`(H100,80G+) · `HOPPER_141`(H200,141G) · `BLACKWELL_96`(RTX PRO 6000,96G) · `BLACKWELL_180`(B200,180G). Pin exact model with `GpuType.NVIDIA_*`.

## CPU types (CpuInstanceType)
`CPU3G_*`(general) / `CPU3C_*` / `CPU5C_*`(compute, latest). e.g. `CPU5C_4_8` = 4 vCPU/8GB/60GB.

## Fan-out (no .map())
`results = await asyncio.gather(compute(a), compute(b), compute(c))`. Cap with `asyncio.Semaphore(workersMax)`. CPU→GPU pipeline: `await infer(await preprocess(data))`.

## Cost/latency telemetry
Client-mode `EndpointJob`: `job.id/output/error/done`, `await job.wait(timeout=)`, `await job.cancel()`. Cold-start/exec timing comes from the job/run metadata (Delay Time + Execution Time → $). ⚠ The exact field path for the cost readout was **not verified end-to-end** in prior prep — selftest this first (`research/03`).

---

## COLD START — the master constraint (shapes the demo)
- **flashboot=True (default) = fast cold starts via snapshot restore.** New in current Flash; softens but does not erase first-build cost.
- First call still pays: worker provision + **dep install** (torch dominates) → can be ~60s. Warm ≈ 1–2s.
- **Minimize:** lean deps (let Flash auto-exclude torch where possible), `workers=(1,N)` to keep one warm, pre-stage big weights on a **NetworkVolume** (not in `dependencies`), `accelerate_downloads=True`, stable endpoint names.
- **Demo doctrine:** pre-warm everything that must feel instant (`workers=(1,_)`, `flash dev --auto-provision`). Take exactly ONE *narrated* 60s live-mint as the moat beat: "deploying a brand-new GPU tool mid-conversation — on Modal/Replicate that's a Docker build+push; here it's a runtime call, already live." Surface idle-pool $/hr on screen = the cost-awareness rubric line.
- `runsync` times out at **60s** — cold starts exceed it. Use `ep.runsync(data, timeout=120)` or `ep.run()` + `job.wait()` for first calls.

---

## KNOWN BUGS / FOOTGUNS (full list in `research/00-known-issues-and-positioning.md`)
1. **Only the function BODY ships.** Imports, helpers, module constants the fn uses MUST be inside the decorated body, or `NameError` under `flash dev` (deploy masks it). #1 cause of breakage.
2. **torch unavailable under `flash dev` (live serverless)** — Linear AE-3186. Test torch primitives via `flash deploy`, not dev.
3. **Multi-endpoint build is all-or-nothing** — one endpoint's dep with no prebuilt wheel fails the WHOLE build. Keep deps lean; vet wheels.
4. **No model-cache support** with Flash yet — use NetworkVolume for weights.
5. **`flash deploy` can fail during worker init** (container won't start) — test the deploy path early; have a known-good base image.
6. **dev→deploy leaves orphan endpoints.** Clean up: `flash undeploy --all` / `--cleanup-stale` / `--interactive`.
7. **`flash deploy` doesn't print the endpoint URL** — our control plane should surface it.
8. **10MB payload limit** — pass URLs, not big objects.
9. **No live GPU stock/availability in the SDK** — agents reverse-engineer GraphQL, hit 403 Cloudflare from automation IPs. ← biggest gap; see below.
10. **`max_concurrency` default 1** — raise for I/O-bound LB routes. **Auto GPU-switching needs workers>=5** + a GPU list.

## The gap that = a winning idea
SDK has **no `GpuType.availability()` / `DataCenter.available_gpus()`**. The Flash team explicitly wants it. A clean **availability-aware deploy helper / MCP tool** (authed GraphQL, picks a DC with stock) fills a real gap and is judge-resonant. **Pre-build into the kit regardless of theme.**

---

## DEV-HOST GOTCHA (gold from prior prep, `research/03`)
Flash defaults ALL hosts to **prod** (`api.runpod.io`). A **dev** API key 401/404s unless you override all three:
`RUNPOD_API_BASE_URL`, `RUNPOD_REST_*`, `RUNPOD_ENDPOINT_*` → `api.runpod.dev`. Bake this into the kit's env loader. (Hackathon will likely use a real/prod key — confirm which on the day.)

## Deployment — NO DOCKER, it's all Flash (the whole point)
- ⚠ **Do not build a Docker control-plane image** — that's the anti-pattern Flash exists to kill. Everything deploys *through* Flash. `research/05` (Docker/pod playbook) is **deprecated** for this project; keep only its platform footgun notes.
- **GPU work** = `@Endpoint` decorators, deployed via `flash deploy`. No container we manage.
- **Control plane / dashboard / MCP**: runs **locally** on the laptop during the demo (MCP-stdio needs no ingress). If it must "be on Runpod," deploy it as a **Flash CPU load-balanced endpoint** (Mode 2) — Flash builds it, still no Docker.
- **Weights**: stage on a `NetworkVolume(name, size)` attached to the GPU `@Endpoint` (`volume=`), not baked into a container, not in `dependencies`.
- The only thing that "survives" is the Flash endpoint itself (stable after `flash deploy`) + the network volume. Iterate with `flash dev` (hot-reload, no rebuild).

## TS SDK (`@runpod/flash`, `research/02`)
Richer CLI + typed GraphQL codegen (the pattern the user's CLAUDE.md references). GPU image default `zackmckennarunpod/flash-ts:latest`. Code-over-wire hot reload. **Decision (this kit): Python primary** — prior prep + primitives are Python and the proven mint loop is Python; TS as optional control-plane upgrade.

## Ecosystem assets
- `flash` skill: `npx skills add runpod/skills` (saved copy: `research/_skills-repo/flash/`).
- Docs: docs.runpod.io/flash/apps/overview. Examples: github.com/runpod/flash-examples. Source: github.com/runpod/flash.
- `runpod/runpod-mcp` (`@runpod/mcp-server`): 35 pod/endpoint/volume/template/job tools incl. `list-gpu-types`, `list-data-centers` — but **no Flash-specific tools**. We also have `mcp__runpod__*` locally.
- Positioning: Dean's Journey Map + JTBD (Figma/PDF) — "why Flash over traditional Serverless." Mirror its language in the pitch.

## Decision tree — announcement → grab blocks (from `notes/prep-kit.md`)
| If they announce… | Grab |
|---|---|
| Agents / tools / MCP | runtime-deploy helper + MCP spine + primitives |
| Media / data processing | pipeline pattern + transcription/vision/embedding primitives |
| Real-time / interactive | warm pool + burst UI (audience beat) |
| Cost / efficiency / scale | batch-eval pattern + cost scoreboard |
| "Build a product" (open) | compose primitives into one vertical app |
Always usable: the **60s-runtime-deploy-vs-Docker-build** narration + the **GPU-availability** helper.
</content>
