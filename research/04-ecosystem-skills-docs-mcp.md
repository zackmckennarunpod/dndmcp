# Flash Ecosystem Digest — Skills, Docs, MCP, Context DB

Compiled 2026-06-26 for the Runpod Flash hackathon. Sources: `runpod/skills` (flash SKILL, PR #22 merged), `docs.runpod.io/flash`, `runpod/runpod-mcp`, internal Context DB.

---

## TL;DR — the one reference to trust day-of

**The `flash` SKILL.md** (`runpod/skills` @ `flash/SKILL.md`, synced to runpod-flash **v1.17.0** via merged PR #22) is the single most authoritative, agent-ready reference. Every command/flag in it was verified against v1.17.0 source AND the live CLI `--help`, and re-validated against 5 eval scenarios at 100% pass. It is denser and more current than the public docs. Use it as the primary; use `docs.runpod.io/flash` for prose explanations and `flash --help` to confirm at runtime.

Full local copy: `/Users/zackmckenna/Developer/work/flash-hackathon/research/_skills-repo/flash/SKILL.md`

---

## 1. The official init → dev → deploy flow

```bash
# install (Python 3.10–3.13)
uv tool install runpod-flash      # or: pip install runpod-flash
# or, in a venv:  uv venv && source .venv/bin/activate && uv pip install runpod-flash

flash login                       # browser OAuth (saves token); --no-open for headless
# or: export RUNPOD_API_KEY=your_key

flash init my-project             # scaffold (writes AGENTS.md + CLAUDE.md symlink); `flash init .` for cwd
flash dev                         # LOCAL server :8888, functions run on REMOTE GPU/CPU, hot-reload + live worker logs
flash deploy                      # build + upload + provision a stable endpoint (slow; only once code works)
```

- **`flash dev` is canonical** (was `flash run` pre-v1.17; `run` is now a hidden alias). This is the #1 contradiction vs older material and vs the Context DB (see §5).
- Iterate entirely under `flash dev` — no build/upload wait. Add `--auto-provision` to skip first-call cold start. `--port/--host/--reload`.
- `flash deploy` flags: `--env staging`, `--app my-app --env prod`, `--preview` (local Docker), `--no-deps`, `--python-version 3.11`.
- **Two-level structure**: an **app** contains **environments** (dev/staging/prod). `flash app create|get|list|delete`, `flash env create|get|list|delete`.
- `flash build` is build-only/debug (artifact **1500 MB** limit, torch auto-excluded). `flash undeploy <name> | list | --all | --cleanup-stale`.
- `flash update [--version X]` updates the CLI.

### Calling endpoints under `flash dev` (autonomous agent loop)
```bash
flash dev > /tmp/flash-dev.log 2>&1 &                                # background, never block
until grep -q "flash dev  localhost:" /tmp/flash-dev.log; do sleep 2; done
URL=$(grep -o "localhost:[0-9]*" /tmp/flash-dev.log | head -1)        # port auto-bumps if 8888 taken
curl -s "$URL/main/predict" -d '{"data": {...}}'                     # routes namespaced by file: main.py/predict → /main/predict
```
A handler `def predict(data: dict)` expects `{"data": {...}}` (top-level field), else 422.

---

## 2. The Endpoint SDK (three modes)

| Params | Mode |
|--------|------|
| `name=` only | Decorator — your code becomes the worker (Queue-Based) |
| `image=` set | Client — deploys a pre-built Docker image, call via HTTP/job |
| `id=` set | Client — connect to existing endpoint, no provisioning |

**Mode 1 — Queue-based decorator** (one function = one endpoint w/ own workers):
```python
from runpod_flash import Endpoint, GpuGroup

@Endpoint(name="my-worker", gpu=GpuGroup.AMPERE_80, workers=5, dependencies=["torch"])
async def compute(data):
    import torch                  # MUST import inside the body (cloudpickle)
    return {"sum": torch.tensor(data, device="cuda").sum().item()}

result = await compute([1, 2, 3])
```

**Mode 2 — Load-balanced routes** (many HTTP routes share one worker pool):
```python
api = Endpoint(name="my-api", gpu=GpuGroup.ADA_24, workers=(1, 5), dependencies=["torch"])

@api.post("/predict")
async def predict(data: list[float]): ...
@api.get("/health")
async def health(): return {"status": "ok"}
```

**Mode 3 — External image / existing endpoint (client)**:
```python
server = Endpoint(name="my-server", image="my-org/my-image:latest", gpu=GpuGroup.AMPERE_80,
                  workers=1, env={"HF_TOKEN": "xxx"}, template=PodTemplate(containerDiskInGb=100))
result = await server.post("/v1/completions", {"prompt": "hello"})   # LB style
job = await server.run({"prompt": "hello"}); await job.wait(); print(job.output)  # QB style

ep = Endpoint(id="abc123")
job = await ep.runsync({"prompt": "hello"})    # runsync wraps as {"input": {...}}
```

### Full constructor (defaults noted)
```python
Endpoint(
    name=..., id=None,
    gpu=GpuGroup.ANY,                # GpuGroup tier | GpuType model | list of either
    cpu=CpuInstanceType.CPU5C_4_8,   # mutually exclusive with gpu
    workers=5,                       # = (0,5); or workers=(1,5). Default (0,1)
    max_concurrency=1,               # concurrent reqs/worker; raise for I/O-bound LB
    idle_timeout=60,                 # SECONDS before scale-down
    dependencies=["torch"],          # pip packages on worker
    system_dependencies=["ffmpeg"],  # apt-get packages
    image="org/image:tag",           # client mode
    env={"KEY": "val"},
    volume=NetworkVolume(name="v", size=100),   # size GB, default 100
    datacenter=DataCenter.US_CA_2,   # DataCenter | list | str. Default None (unset)
    gpu_count=1,                     # GPUs/worker; >1 for multi-GPU models
    template=PodTemplate(containerDiskInGb=64, dockerArgs="", ports="", startScript=""),
    flashboot=True,                  # fast cold starts via snapshot restore
    accelerate_downloads=True,
    min_cuda_version=CudaVersion.V12_8,
    scaler_type=ServerlessScalerType.QUEUE_DELAY,  # QB default QUEUE_DELAY, LB default REQUEST_COUNT
    scaler_value=4,
    execution_timeout_ms=0,          # 0 = unlimited
)
```
`DataCenter`, `CudaVersion`, `ServerlessScalerType`, `GpuGroup`, `GpuType`, `CpuInstanceType` all import from `runpod_flash`.

`EndpointJob` (client mode): `await job.wait(timeout=120)`, `job.id/output/error/done`, `await job.cancel()`.

### GPU pools (GpuGroup)
`ANY` · `AMPERE_16` (16) · `AMPERE_24` (24) · `AMPERE_48` (48) · `AMPERE_80` A100 (80) · `ADA_24` RTX 4090 (24) · `ADA_32_PRO` RTX 5090 (32) · `ADA_48_PRO` RTX 6000 Ada/L40/L40S (48) · `ADA_80_PRO` H100 (80+) · `HOPPER_141` H200 (141) · `BLACKWELL_96` RTX PRO 6000 Blackwell (96) · `BLACKWELL_180` B200 (180).
`GpuGroup` = cheapest available in a VRAM tier; `GpuType` = pinned exact model (e.g. `GpuType.NVIDIA_GEFORCE_RTX_4090`, ~22 models). Pass a **list** for supply-based fallback/auto-switch.

### CPU types (CpuInstanceType)
General `CPU3G_{1_4..8_32}`; Compute `CPU3C_*`; 5th-gen Compute `CPU5C_{1_2..8_16}` (e.g. `CPU5C_4_8` = 4 vCPU/8 GB/60 GB disk).

### Gotchas (from the skill — load-bearing)
1. **Only the function body ships to the worker.** Put imports AND module-level constants/helpers the function uses *inside* the body. `flash deploy` imports the whole module (globals work); `flash dev` ships only the body → a module-level name raises `NameError`. A deployed-working handler can break under dev — fix by moving everything inside. (Develop under `dev` to catch it early.)
2. Forgetting `await` (all decorated fns + client methods need it).
3. Missing deps → list in `dependencies=[]`.
4. `gpu`/`cpu` mutually exclusive.
5. `idle_timeout` is **seconds** (default 60).
6. **10 MB payload limit** — pass URLs, not large objects.
7. `image=`/`id=` ⇒ client; else decorator.
8. **Auto GPU switching needs `workers>=5`** + a list of GPU types.
9. **`runsync` timeout is 60s** — cold starts exceed it; use `runsync(data, timeout=120)` or `run()`+`job.wait()`.

### Skill also ships 5 evals (agent test scenarios)
`flash/evals/*.eval.md`: client-external-image, connect-existing-endpoint, cpu-gpu-pipeline, dev-loop-iteration, lb-multi-route-api, qb-gpu-function (+ fixture `evals/fixtures/dev-loop/main.py`). PR #22 ran all 5 through fresh agents → 100% assertion pass.

**Install the skill:** `npx skills add runpod/skills` (works with Claude Code, Cursor, Copilot, Windsurf, Cline, 17+ agents). Setup check: `runpodctl doctor`.

---

## 3. Docs — docs.runpod.io/flash (full page map)

Public docs are clean and prose-first; append `.md` to any URL for raw markdown (used below). Cross-checks the skill; the skill is more current/dense.

**Apps:** overview · build-app · initialize-project · customize-app · deploy-apps · local-testing · apps-and-environments · requests
**CLI:** overview · app · init · build · dev · deploy · undeploy · login · env · update
**Configuration:** best-practices · cpu-types · gpu-types · parameters · storage
**Core:** overview · quickstart · create-endpoints · custom-docker-images · execution-model · pricing · troubleshooting · windows-wsl2

Key doc URLs:
- https://docs.runpod.io/flash/overview · /quickstart · /execution-model · /pricing
- https://docs.runpod.io/flash/configuration/{parameters,gpu-types,cpu-types,storage,best-practices}
- https://docs.runpod.io/flash/cli/{dev,deploy,init,...}

### Quickstart (docs version — note plain `python script.py`, no flash dev needed to invoke)
```bash
uv venv && source .venv/bin/activate && uv pip install runpod-flash
flash login                      # or export RUNPOD_API_KEY=...
```
```python
# gpu_demo.py
import asyncio
from runpod_flash import Endpoint, GpuGroup

@Endpoint(name="flash-quickstart", gpu=GpuGroup.ANY, workers=3,
          idle_timeout=300, dependencies=["numpy", "torch"])
def gpu_matrix_multiply(size):
    import numpy as np, torch
    device_name = torch.cuda.get_device_name(0)
    C = np.dot(np.random.rand(size, size), np.random.rand(size, size))
    return {"matrix_size": size, "result_mean": float(np.mean(C)), "gpu": device_name}

async def main():
    result = await gpu_matrix_multiply(1000)
    print(result)

if __name__ == "__main__":
    asyncio.run(main())
```
`python gpu_demo.py` — first run 30–60s (provision), warm runs 2–3s. Cleanup: `flash undeploy flash-quickstart` / `--all`.
> Note: you can invoke `@Endpoint` functions directly from a local Python script (it provisions on first call); `flash dev` is for the hot-reload server loop. Both are valid.

### Execution model (docs)
@Endpoint ships the function body (serialized); control flow/I/O stays local. Invoke → look up/create endpoint → serialize fn+args → submit job → remote worker runs → result returns as a Python object. Worker states: Initializing, Idle, Running, Throttled, Outdated, Unhealthy. `workers=(0,n)` scales to zero (guarantees cold start after idle); `(1,n)` keeps one warm. **Cold start 10–60s** (deps pre-installed in the build, no runtime pip); **warm ~1s** within `idle_timeout`.

### GPU types / datacenter guidance (docs)
Production → pin a specific `GpuType` for predictable cost/perf; dev → `ANY` for fastest iteration. A **list** of GPUs creates a fallback chain tried in order. `datacenter` defaults to None (unset = any). There is no explicit per-DC availability table in Flash docs; use the MCP `list-data-centers` / `list-gpu-types` tools (§4) for live availability.

### Pricing (docs)
Pay-per-second, no idle charge beyond `idle_timeout`. Billed across 3 phases: start (container init + model load), execution, idle-timeout wait. Optimize: right-size GPU/CPU, shorten `idle_timeout` (trades cold starts), use CPU workers for preprocessing, cap `workers`. Exact rates → /serverless/pricing.

---

## 4. runpod/runpod-mcp — `@runpod/mcp-server`

Official Runpod MCP server (npm `@runpod/mcp-server`, also Smithery `@runpod/runpod-mcp-ts`). Node 18+.

**Setup (Claude Code):**
```bash
claude mcp add runpod -s user -e RUNPOD_API_KEY=YOUR_API_KEY -- npx -y @runpod/mcp-server@latest
# or run directly:  RUNPOD_API_KEY=KEY npx -y @runpod/mcp-server@latest
```
Two modes: local `stdio` (caller sets `RUNPOD_API_KEY`) and hosted Streamable HTTP (per-request `Authorization: Bearer`; server holds no credential).

**Flash connection:** the MCP server exposes **no Flash-specific tools**. Its only Flash tie is the hosted **OAuth "Sign in with Runpod"** flow, which reuses the **flash auth backend** (`createFlashAuthRequest` mutation → console handoff `/integrations/mcp/login` → `flashAuthRequestStatus` poll → mints a real Runpod API key named `runpod-mcp`). No PKCE; relies on redirect_uri allowlist + single-use short-lived flash approval. Env: `RUNPOD_GRAPHQL_URL` (default api.runpod.io/graphql), `CONSOLE_BASE_URL` (console.runpod.io), `RUNPOD_REST_API_URL`/`RUNPOD_SERVERLESS_API_URL`, `RUNPOD_API_KEY_NAME`.

**Full tool list (35) — from `src/tools.ts`, identical to the local `mcp__runpod__*` tools:**
- **Pods:** list-pods, get-pod, create-pod, update-pod, start-pod, stop-pod, delete-pod
- **Endpoints (Serverless):** list-endpoints, get-endpoint, create-endpoint, update-endpoint, delete-endpoint, endpoint-health, purge-endpoint-queue
- **Jobs:** run-endpoint, runsync-endpoint, get-job-status, stream-job, cancel-job, retry-job
- **Templates:** list-templates, get-template, create-template, update-template, delete-template
- **Network volumes:** list-network-volumes, get-network-volume, create-network-volume, update-network-volume, delete-network-volume
- **Container registry auth:** list-container-registry-auths, get-container-registry-auth, create-container-registry-auth, delete-container-registry-auth
- **Infra/lookup:** list-data-centers, list-gpu-types

These operate on raw Runpod Serverless/Pod resources (the layer Flash provisions onto). Useful day-of for: checking GPU availability (`list-gpu-types`), datacenters (`list-data-centers`), and inspecting/cleaning up the endpoints Flash creates (`list-endpoints`, `delete-endpoint`, `endpoint-health`).

---

## 5. Internal Context DB findings

### Requirements — `req:FL-*` (Flash SDK/CLI) — all P0/P1 **verified**
Full surface is specced and verified in the Context DB. Highlights:
- **CLI:** FL-CLI-001 init scaffold (pyproject + example worker), FL-CLI-002 login (browser OAuth or `RUNPOD_API_KEY`), FL-CLI-003 build, FL-CLI-004 deploy (build+upload+provision).
- **Dev:** FL-DEV-001 dev server + auto-reload, FL-DEV-002 *(stale)* "QB routes execute locally in-process, LB routes dispatch remotely", FL-DEV-003 preview in Docker, **FL-DEV-004 auto-provision = still `open`**.
- **Execution:** FL-EXE-001 QB decorator, FL-EXE-002 LB FastAPI pool, FL-EXE-003 client/image mode, FL-EXE-004 class-based endpoints (singleton + method dispatch), FL-EXE-005 CPU-only.
- **Config:** FL-CFG-001..007 (name+GPU+scaling, gpu/cpu exclusive, deps, network volumes, FlashBoot default, multi-GPU supply switching, **CFG-007 config drift detection auto-updates endpoints**).
- **Workers:** FL-WRK-001 cloudpickle+base64 payloads, WRK-002 uv/pip on-the-fly, WRK-003 dual entrypoints (`runpod.serverless.start` QB / FastAPI+uvicorn LB), WRK-004 path-traversal-safe unpack, WRK-006 **four Docker images: GPU-QB, CPU-QB, GPU-LB, CPU-LB**.
- **Cross-endpoint:** FL-XEP-001 service registry resolves cross-endpoint calls at runtime, XEP-002 API-key propagation, XEP-003 manifest reconciliation + TTL cache, **XEP-004 graceful local fallback = `open`**.
- **Env:** FL-ENV-001..004 multi-env per app, CRUD, `--env` targeting, undeploy.

> **CONTRADICTION to flag:** Context DB requirements predate v1.17.0. They say `flash run` (FL-DEV-001) and a **500 MB** build limit (FL-CLI-003, FL-DEP-004). The current truth (skill/CLI v1.17.0): command is **`flash dev`** and the limit is **1500 MB**. Also FL-DEV-002's "QB local / LB remote" split is superseded — under modern `flash dev`, decorated functions execute on **remote** workers with live log streaming. **Trust the skill over these requirement titles.**

### Host-side FlashBoot — `req:HD-FLASH-*` (all `done`)
The platform mechanics behind fast cold starts (separate from the SDK): HD-FLASH-001 Docker image preloading, **002 FlashBoot = Redis pubsub-triggered container unpause**, 003 Priority FlashBoot worker lifecycle, 004 Flash volume preloading, 005 PFB GPU resource tracking.

### Learnings / gotchas
- **#1780956465455 (issue, warm, prod-confirmed):** `pfb-done` skips pause when container rename fails → worker never frozen, keeps pulling jobs, ai-api re-sends pfb-done in an infinite loop. Root cause of "repeated PFB done / renaming something that doesn't exist." (Host-side; not hackathon-blocking but explains FlashBoot weirdness.)
- **#1779122711043 (pattern):** Build pipeline state lives in `silver.developer_platform.fct_git_builds` (states lowercase: pending/building/uploading/completed/failed/cancelled/testing/test_failed); `dim_flash_build` tracks artifacts, not pipeline state.
- **#1778478639331 (pattern):** `FlashApp` table uses `@@unique([userId, name])` only (no org-scoped unique constraint).
- No learnings under "tetra" (Tetra is the prior/underlying name; nothing tagged).

### Docs in Context DB
- `doc:1778298033338` — "Architecture deepening review across rphttp, Flash, RunPod, and main-ui" (2026-05-08): notes the Flash domain lifecycle in `RunPod` (Environment state, artifact upload/finalize, build→deploy promotion, endpoint/network-volume attachment, artifact dedupe) and `flash-examples` structure (unified discovery/registry, worker shape, `workersMin/workersMax/idleTimeout`, GPU/CPU config).
- `doc:runpod-rest-api-v2-endpoint-spec` — REST API v2 endpoint surface (rphttp2) if hitting the API directly.

---

## 6. Day-of cheat sheet

1. **Primary ref:** the `flash` SKILL.md (install it: `npx skills add runpod/skills`). Trust it over docs and Context DB for commands/flags.
2. **Flow:** `flash login` → `flash init x` → `flash dev` (background, drive over HTTP, read the live worker log) → `flash deploy --env prod`.
3. **#1 bug source:** put ALL imports + helpers INSIDE the decorated body (only the body ships).
4. **Cold starts:** `flash dev --auto-provision`; keep `workers=(1,n)`; `runsync(..., timeout=120)`.
5. **GPU/DC availability:** use MCP `list-gpu-types` / `list-data-centers`; pin `GpuType` for prod, `GpuGroup.ANY` for speed, list for fallback.
6. **Limits:** 1500 MB build, 10 MB payload, idle_timeout in seconds.

### Local asset paths
- Skill (full): `/Users/zackmckenna/Developer/work/flash-hackathon/research/_skills-repo/flash/SKILL.md`
- Evals: `/Users/zackmckenna/Developer/work/flash-hackathon/research/_skills-repo/flash/evals/`
- Flash source: https://github.com/runpod/flash · Examples: https://github.com/runpod/flash-examples
