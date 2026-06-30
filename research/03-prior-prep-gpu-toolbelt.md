# Prior Prep Digest — `gpu-toolbelt` (most reusable asset)

Source: `/Users/zackmckenna/Developer/work/gpu-toolbelt` — my own Flash Hack Day prep from 2026-06-22.
This is the single most reusable thing I have for the Jun 30 hackathon. It is a working
**"agent mints/calls/meters/manages its own Flash GPU endpoints at runtime"** harness, exposed as
an MCP server. Below is everything salvageable, with verbatim code.

> Bottom line: the **deploy→call loop is PROVEN on dev** (real endpoint id created). The full
> `core.py` engine + MCP server are **written but the `runsync` metadata path was never verified
> end-to-end** (STATE step 3 is still `[ ]`). One known fix (unique endpoint names) is already baked in.
> Nothing is staged on a network volume. No warm demo tool pre-baked yet.

---

## 1. What works today

**No running control plane / server right now.** It's a stdio MCP server you launch on demand;
nothing is hosted. `.runpod/resources.pkl` is empty (`({}, {})`) — no live endpoints, no local
registry state persisted (`.toolbelt/` never committed / cleaned).

**Proven:** the core mint→call loop ran green on DEV — string → file → import → `inspect.getsource`
OK → `get_or_deploy_resource` deployed → real dev endpoint id returned. The historical proof is
`probe_loop.py`. Activity log confirms a real endpoint `u41hnv6yaptuxd` was created (then the run hit
the unique-name gotcha and a 401/404 from wrong hosts — both since solved).

**Two host gotchas found + solved** (this is the friction that's worth its weight in gold on day-of):

### THE env recipe — Flash defaults every host to PROD; a dev key needs all 3 overridden
```bash
cd /Users/zackmckenna/Developer/work/gpu-toolbelt
RUNPOD_API_KEY=$(security find-generic-password -s runpod-api-key-dev -w) \
RUNPOD_API_BASE_URL=https://api.runpod.dev \
RUNPOD_REST_API_URL=https://rest.runpod.dev/v1 \
RUNPOD_ENDPOINT_BASE_URL=https://api.runpod.dev/v2 \
  /Users/zackmckenna/Developer/work/flash/.venv/bin/python probe_loop.py
```
- `RUNPOD_API_BASE_URL` → GraphQL control plane (deploy/save_endpoint). Default `api.runpod.io` → **401**.
- `RUNPOD_ENDPOINT_BASE_URL` → endpoint invocation (run/runsync). Default `api.runpod.ai/v2` → **404**.
- `RUNPOD_REST_API_URL` → REST control.
- Borrows `../flash/.venv` (runpod-flash 1.4.2 + deps, Python 3.11). No separate install was ever made.

### `probe_loop.py` — the proven reusable spine (verbatim, copy this first)
```python
"""Proof of the core loop: an 'agent' authors a tool AS A STRING, we deploy it to
Flash, call it, then tear it down. This is the reusable spine for gpu_tool_create/call."""
import asyncio, importlib.util, sys, time
from pathlib import Path
from runpod_flash import GpuGroup, LiveServerless, ResourceManager, remote

# The "agent" emits a tool as a STRING (this is what an LLM would produce).
# Dependency-free on purpose: proves the deploy/exec/return roundtrip without a slow pip cold start.
AGENT_AUTHORED_CODE = '''
def handler(numbers):
    import platform, os
    return {
        "doubled_sum": sum(n * 2 for n in numbers),
        "ran_on_host": platform.node(),
        "python": platform.python_version(),
        "remote_cwd": os.getcwd(),
    }
'''
TOOL_NAME = "agent-loop-probe"

def materialize_tool(name: str, code: str):
    """Write agent code to a real .py file and import it.
    Required because Flash extracts source via inspect.getsource(), which fails on
    exec()'d functions (no __file__). A real file on disk makes getsource() work."""
    tool_dir = Path("/tmp/agent_tools"); tool_dir.mkdir(parents=True, exist_ok=True)
    path = tool_dir / f"{name.replace('-', '_')}.py"
    path.write_text(code)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.handler

async def main(keep: bool = False):
    handler = materialize_tool(TOOL_NAME, AGENT_AUTHORED_CODE)
    config = LiveServerless(
        name=TOOL_NAME,
        gpus=[GpuGroup.ADA_24],   # 4090 — cheap; swap per task
        workersMax=1,
        idleTimeout=5,            # scale to zero fast after the call
    )
    gpu_tool = remote(config)(handler)   # @remote programmatically = the deploy
    try:
        result = await gpu_tool([1, 2, 3, 4, 5])  # expect doubled_sum = 30
        assert result.get("doubled_sum") == 30, f"unexpected result: {result}"
    finally:
        if not keep:                       # teardown runs even on failure
            mgr = ResourceManager()
            for resource_id, res in mgr.find_resources_by_name(TOOL_NAME):
                await mgr.undeploy_resource(resource_id, resource_name=getattr(res, "name", None))

if __name__ == "__main__":
    asyncio.run(main(keep="--keep" in sys.argv))
```

**What `core.py` actually does** — the full engine, decoupled from MCP. `class Toolbelt`:
- `create(name, gpu, deps, code, workers_min, workers_max, idle_timeout)` — materialize code → import
  → `ResourceManager.get_or_deploy_resource(config)`. Returns `{name, endpoint_id, gpu, status}`.
- `call(name, payload)` — drives the **low-level metadata path** to recover JobOutput fields the
  `remote()` wrapper discards: `LiveServerlessStub(resource).prepare_request(...)` →
  `resource.runsync(wire)` → `JobOutput`. Falls back to the proven `remote()` wrapper (output only)
  if that path errors at runtime. Returns `{output, meta}` where meta = delay_ms/exec_ms/worker_id/cost_usd.
- `list_tools()`, `cost()`, `health()`, `gc(policy)`, `rightsize(name)` — management plane.
- Persistence: `.toolbelt/registry.json` (tool code + call records) + Flash's `.runpod/resources.pkl`.

**What `server.py` does** — thin `FastMCP("gpu-toolbelt")` stdio adapter exposing 7 tools:
`gpu_tool_create / gpu_tool_call / gpu_tool_list` (data plane) + `fleet_cost / fleet_health /
fleet_gc / fleet_rightsize` (management plane). `mcp.run()` = stdio, no ingress/auth/pod needed.

⚠️ **The one unverified link:** STATE step 3 (`[ ]`) — the `runsync`/`prepare_request` metadata path
in `call()` was never confirmed against a live JobOutput. It's written with a fallback, but the
"real cost/latency numbers" money-shot depends on that path working. **Verify first on day-of.**

---

## 2. Primitives — state of each GPU function

**Nothing is coded as a real handler yet.** The only handler that exists is the dependency-free
`doubled_sum` toy used to prove the loop (in both `probe_loop.py` and `core.py` `_SELFTEST_CODE`).

The catalog (`ideas/examples-tool-catalog.md`) is a **shopping list, not built code** — deps + notes
per candidate tool. Legend: 🎬 demo-gold (fast+visual), 📦 needs weights on a network volume, 💰 cost story.

| Candidate | Deps | State |
|---|---|---|
| Background removal 🎬 | `rembg` | not built; demo opener pick |
| Image upscale 🎬 | `realesrgan` | not built |
| Transcription 📦 | `faster-whisper` | not built |
| Caption/VQA 📦 | `transformers` (BLIP/LLaVA) | not built |
| Image gen 🎬📦 | `diffusers` (SDXL/Flux) | not built |
| Embed + FAISS 💰 | `sentence-transformers`, `faiss-gpu` | not built (this is "gpu_search") |
| Bulk summarize on local LLM 💰📦 | `vllm` | not built; the cost-story pick |
| LoRA fine-tune 🎬📦 | `peft`,`transformers`,`trl` | not built; mic-drop finale if stable |

**Demo shortlist to pre-bake warm:** (1) bg-removal + upscale 🎬, (2) bulk summarize 💰, (3) LoRA 🎬 (cut if unstable).

---

## 3. Flash API patterns (`notes/flash-api.md`, verified vs source 2026-06-22)

**Decorator** — `src/runpod_flash/client.py:98`:
`remote(resource_config, dependencies=None, system_dependencies=None, accelerate_downloads=True, local=False, method=None, path=None, **extra)`
```python
from runpod_flash import remote, LiveServerless
gpu_config = LiveServerless(name="flash-quickstart")

@remote(resource_config=gpu_config, dependencies=["torch", "numpy"])
def gpu_compute(data):
    import torch
    return {"result": torch.tensor(data, device="cuda").sum().item()}

result = await gpu_compute([1,2,3])   # ALWAYS returns an awaitable, sync or async body
```
- `resource_config` REQUIRED. `dependencies` = pip specs installed at cold start.
  `system_dependencies` = apt packages. Works programmatically: `remote(cfg, deps)(fn)`.

**Deploy = runtime auto-deploy (the key fact):**
- First call resolves/deploys via `ResourceManager.get_or_deploy_resource(config)`
  (`resource_manager.py:206`). Config-hash drift: reuse if unchanged, update if changed, deploy if new.
- **Deploy WITHOUT calling:** `await ResourceManager().get_or_deploy_resource(config)` → resource with `.id`.
- Source extracted via AST (`stubs/live_serverless.py:26`) + `cloudpickle`d → `FunctionRequest`. **Body runs on the remote worker.**
- First call ~1 min (cold, dep-install dominated); subsequent ~1s.

**Calling / fan-out:**
- `await fn(args)`. **No `.map()`/`.batch()`** — fan out with `asyncio.gather(...)`.
- Modes: `/runsync` (wait, return) and `/run` (async, poll) — `serverless.py:893`.
- Result: arbitrary Python object, cloudpickled+base64, unpickled client-side. Stdout captured.

**GPU enums** (`core/resources/gpu.py:36`) — `GpuGroup`:
`ANY, ADA_24`(4090), `ADA_32_PRO`(5090), `ADA_48_PRO`(L40/L40S/6000Ada), `ADA_80_PRO`(H100),
`AMPERE_16`(A4000/4500), `AMPERE_24`(A5000/L4/3090), `AMPERE_48`(A40/A6000), `AMPERE_80`(A100 80GB), `HOPPER_141`(H200).

**CPU enums** (`core/resources/cpu.py:5`) — `CpuInstanceType`: `cpu3g-1-4, cpu3g-2-8, cpu3g-4-16, cpu3g-8-32, cpu3c-*, cpu5c-*`…

**ServerlessResource config** (`serverless.py:93`):
`name`(req), `gpus`/`instanceIds`, `gpuCount`=1, `workersMin`/`workersMax`=0/1, `idleTimeout`=60s,
`executionTimeoutMs`=0, `env`, `networkVolumeId`, `template`(PodTemplate), `type`=`QB`(queue)|`LB`(http).
`LiveServerless` subclass locks to the optimized Flash image.

**JobOutput** (`serverless.py:1037`): `id, workerId, status, delayTime(ms), executionTime(ms), output, error`.
**No cost field** — derive cost = executionTime × GPU hourly rate (the billing edge).

**Fleet plane** (`resource_manager.py:324+`): `list_all_resources()`, `find_resources_by_name()`,
`find_resources_by_provider_id()`, `await undeploy_resource(resource_id, ...)`. Persists to `.runpod/resources.pkl`.
⚠️ `find_resources_by_name` only sees LOCALLY-tracked resources — a crashed-run leak won't appear → need GraphQL teardown.

**Auth / config:** `.env-example` = `RUNPOD_API_KEY=`, `FLASH_HOST=localhost`, `FLASH_PORT=8888`.
Precedence: env `RUNPOD_API_KEY` → `~/.config/runpod/credentials.toml` (`flash login`) → error.
Dev GQL `api.runpod.dev` (introspection ON), prod `api.runpod.io` (OFF).

**Constraints:** cold start ~1 min → pre-bake. **EU-RO-1 datacenter only.** **500MB deploy limit** → lean
deps, big weights on a network volume. `cloudpickle` needs all fn deps importable in the remote env.

**Stacked `@remote`** (idea 05): Flash's `dependency_resolver` (`resolve_dependencies`,
`build_augmented_source`, `generate_stub_code`, `strip_remote_imports` in `stubs/`) stitches one
remote fn calling another. **UNVERIFIED for string→file→import fns** — make-or-break, spike early.

---

## 4. Network volume / weight staging

**NOTHING staged.** No network volume created, no volume IDs, no weights pre-positioned anywhere in
the repo. `.runpod/resources.pkl` is empty `({}, {})`. Network volumes are only referenced as a
*plan* ("big weights on a network volume, EU-RO-1 only, 500MB deploy limit"). Any 📦 tool
(whisper/SDXL/vLLM/LoRA) **needs a volume created + weights staged from scratch** before the event.
This is a real gap — staging weights has wall-clock cost and must be done ahead of the demo.

---

## 5. Reusable infra (copy verbatim)

### Server framework — FastMCP stdio adapter (`toolbelt/server.py`, verbatim)
```python
"""GPU Toolbelt — MCP adapter (stdio). stdio => no ingress/auth/pod needed for the demo."""
from mcp.server.fastmcp import FastMCP
from .core import Toolbelt

mcp = FastMCP("gpu-toolbelt")
tb = Toolbelt()

@mcp.tool()
async def gpu_tool_create(name: str, gpu: str, dependencies: list[str], code: str,
                          workers_min: int = 0, workers_max: int = 3, idle_timeout: int = 60) -> dict:
    """Mint a brand-new GPU tool at runtime: deploy `code` (must define `def handler(payload)`)
    as a live, auto-scaling Flash endpoint. `gpu` is a GpuGroup name (e.g. ADA_24, AMPERE_80)."""
    return await tb.create(name, gpu, dependencies, code, workers_min, workers_max, idle_timeout)

@mcp.tool()
async def gpu_tool_call(name: str, payload: object) -> dict:
    """Invoke a minted tool. Returns {output, meta} — meta carries delay_ms/exec_ms/cost_usd."""
    return await tb.call(name, payload)

@mcp.tool()
def gpu_tool_list() -> list[dict]:
    """List the GPU tools this agent has minted (persists across sessions)."""
    return tb.list_tools()

@mcp.tool()
def fleet_cost() -> dict:
    """Per-tool + total $ spent, and ongoing idle burn for any pinned-warm tool."""
    return tb.cost()

@mcp.tool()
def fleet_health(name: str | None = None) -> dict:
    """Error rate + p50/p95 execution latency from recent calls."""
    return tb.health(name)

@mcp.tool()
async def fleet_gc(policy: str = "idle", idle_ttl_s: int = 900) -> dict:
    """Tear down endpoints. policy: 'idle' | 'all' | a tool name."""
    return await tb.gc(policy, idle_ttl_s)

@mcp.tool()
def fleet_rightsize(name: str) -> dict:
    """Recommend a cheaper GPU for a tool based on observed execution time."""
    return tb.rightsize(name)

if __name__ == "__main__":
    mcp.run()  # stdio
```

### Runtime-deploy helper + unique-name fix (from `core.py`, verbatim)
```python
def _unique_endpoint_name(friendly: str) -> str:
    """Stable per friendly name: re-creating the same tool reuses its endpoint
    (Flash config-hash drift reuses), while distinct tools never collide."""
    short_hash = hashlib.sha1(friendly.encode()).hexdigest()[:6]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in friendly).strip("-")
    return f"{safe}-{short_hash}"

def materialize_tool(name: str, code: str):
    """Write agent-authored code to a real .py file and import it, so the handler has a
    real __file__ and inspect.getsource() (Flash's source capture) succeeds.
    `code` must define `def handler(payload): ...`."""
    TOOL_CODE_DIR.mkdir(parents=True, exist_ok=True)
    module_name = f"toolbelt_tool_{name.replace('-', '_')}"
    path = TOOL_CODE_DIR / f"{module_name}.py"
    path.write_text(code)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "handler"):
        raise ValueError(f"tool '{name}' code must define a top-level `def handler(payload)`")
    return module.handler
```

### Cost/telemetry readout — the "cost awareness" rubric beat (from `core.py`, verbatim)
```python
# APPROXIMATE Runpod serverless *flex* $/hr per GPU group. VERIFY vs current pricing before stage.
# Override with $TOOLBELT_STATE_DIR/rates.json.
_DEFAULT_GPU_RATES_USD_PER_HR = {
    "ADA_24": 0.69, "ADA_32_PRO": 0.90, "ADA_48_PRO": 0.79, "ADA_80_PRO": 2.99,
    "AMPERE_16": 0.34, "AMPERE_24": 0.43, "AMPERE_48": 0.59, "AMPERE_80": 1.19,
    "HOPPER_141": 3.99, "ANY": 0.69,
}

def _cost_usd(gpu: str, ms: int, worker_count: int = 1) -> float:
    rate = _gpu_rates().get(gpu, _DEFAULT_GPU_RATES_USD_PER_HR["ANY"])
    hours = (ms / 1000.0) / 3600.0
    return rate * hours * worker_count

def cost(self) -> dict:
    """Per-tool + total $ spent (from call records), plus ongoing idle burn for any
    tool pinned warm (workersMin>0) — the number fleet_gc drops."""
    now = time.time()
    per_tool, total_spent, total_idle_burn = [], 0.0, 0.0
    for rec in self._tools.values():
        spent = sum(c.cost_usd for c in rec.calls)
        idle_burn = _cost_usd(rec.gpu, int((now - rec.created_at) * 1000), rec.workers_min)
        total_spent += spent; total_idle_burn += idle_burn
        per_tool.append({"name": rec.name, "gpu": rec.gpu, "calls": len(rec.calls),
                         "spent_usd": round(spent, 4), "idle_burn_usd": round(idle_burn, 4),
                         "pinned_warm": rec.workers_min > 0})
    return {"per_tool": per_tool, "total_spent_usd": round(total_spent, 4),
            "total_idle_burn_usd": round(total_idle_burn, 4)}
```

### Metadata-recovery call path + wrapper fallback (the unverified-but-written core, from `core.py`)
```python
async def call(self, name: str, payload: Any) -> dict:
    rec = self._tools.get(name)
    config, handler, resource = await self._ensure_live(name)
    output, meta, call_rec = None, None, None
    try:
        stub = LiveServerlessStub(resource)
        request = await stub.prepare_request(handler, rec.dependencies, None, True, payload)
        wire = request.model_dump(exclude_none=True)
        job = await resource.runsync(wire)
        if job.error: raise RuntimeError(job.error)
        output = stub.handle_response(FunctionResponse(**job.output))
        cost = _cost_usd(rec.gpu, job.executionTime)
        meta = {"delay_ms": job.delayTime, "exec_ms": job.executionTime,
                "worker_id": job.workerId, "cost_usd": round(cost, 6)}
        call_rec = CallRecord(ts=time.time(), ok=True, delay_ms=job.delayTime,
                              exec_ms=job.executionTime, worker_id=job.workerId, cost_usd=cost)
    except Exception as metadata_path_failed:   # degrade, never crash a call
        from runpod_flash import remote
        output = await remote(config)(handler)(payload)
        call_rec = CallRecord(ts=time.time(), ok=True)
        meta = {"note": f"metadata unavailable: {metadata_path_failed}"}
    rec.calls.append(call_rec); self._save_registry()
    return {"output": output, "meta": meta}
```

### Orphan teardown (GC for crashed-run leaks, bypasses local pkl)
```python
from runpod_flash.core.api.runpod import RunpodGraphQLClient
async with RunpodGraphQLClient() as c:
    await c.delete_endpoint(endpoint_id)   # {'success': True}
```

### Claude Desktop wiring + Bright Data co-host composition (from `notes/bright-data.md`)
```json
{
  "mcpServers": {
    "Bright Data": { "command": "npx", "args": ["@brightdata/mcp"], "env": { "API_TOKEN": "<token>" } },
    "gpu-toolbelt": {
      "command": "/Users/zackmckenna/Developer/work/flash/.venv/bin/python",
      "args": ["-m", "toolbelt.server"],
      "env": { "RUNPOD_API_KEY": "<dev-key>", "RUNPOD_API_BASE_URL": "https://api.runpod.dev",
               "RUNPOD_REST_API_URL": "https://rest.runpod.dev/v1",
               "RUNPOD_ENDPOINT_BASE_URL": "https://api.runpod.dev/v2" }
    }
  }
}
```
No fan-out helper exists yet — fan-out is just `asyncio.gather(*[tb.call(name, p) for p in batch])`.
No dashboard exists — `fleet_cost()`/`fleet_health()` return JSON; the "live cost graph" is aspirational.

---

## 6. Ideas & decisions

**Locked direction:** GPU toolbelt MCP — an agent mints/calls/meters/manages its own Flash endpoints
at runtime. **Differentiator (the whole pitch):** every code-sandbox MCP today is CPU + throwaway;
this is **GPU + persistent live endpoints + cost-managed.** "The agent doesn't run code; it deploys itself."

**Landscape (why it's novel):** "agent writes its own tools" is TAKEN (Strands, OpenSage, TTE/STA).
"MCP runs arbitrary Python" is TAKEN (PRIMS, pydantic/mcp-run-python, Code Sandbox MCP) — all CPU +
ephemeral. The unfilled gap = **GPU-backed execution that PERSISTS as a live endpoint.** Empty because
pre-Flash, GPU deploy meant Docker build + registry push; Flash collapses it to seconds. That's the moat.

**Idea files:**
- **01 (frontrunner):** mint→call→keep, demo = Claude Desktop, arc = bg-remove+4x product photos.
- **05 (build next):** stacked pipelines — tools that call tools (multi-stage GPU DAG). Source-backed
  via Flash's dependency_resolver but UNVERIFIED for string-authored fns. Top "build-upon" pick.
- **Backlog:** 02 run_on_gpu (fallback spine), 03 @tool→fleet framework, 04 multimodal pipeline (safe
  fallback, zero deploy-risk), 06 FinOps budget guardrails 💰, 07 cost-arbitrage routing 💰,
  08 self-eval harness, 09 serverless GPU cron. Standalone hedges H1–H6 (live burst, non-AI compute,
  stream monitor, voice pipeline, distributed training, test-time compute) — pick one only if the
  day-of prompt makes the toolbelt awkward.

**Decision-map (prompt → reach-for):** media→transcribe/caption/embed fanned; agents/automation→the
toolbelt IS the answer + fleet_cost; cost/efficiency→fleet_cost→fleet_gc mic-drop; training→LoRA finale.

**Cold-start doctrine (the central tactical decision):** cold start ~52s punishes "instant from
nothing," nearly free for "pre-warm then burst." → **two paths:** warm pre-baked tools (`workersMin=1`,
~1s) for every instant beat; exactly ONE narrated live-mint we *want* to take 60s ("Modal/Replicate
would still be on the Docker build — mine's already live"). Turns dead air into proof of the moat.

**Deployment decision:** demo = MCP **stdio** → Claude Desktop calls the server locally → **no ingress,
no tunnel, no auth, no CPU pod needed.** Hosted CPU-pod variant (Runpod proxy + single bearer token +
SQLite telemetry) is a STRETCH, not a prereq. Cement to image before demo — nothing on a pod survives redeploy.

**Bright Data (co-host):** no token in keychain — **grab a free one (5k credits/mo, no card) before the
event** and store as `bright-data-token`. Default = Path A (MCP composition, zero glue: their
`search_engine`/`scrape_batch` → our `gpu_tool_create`/`call`). Path B = call their REST from inside a
minted tool's handler (pass token via LiveServerless `env=`, which is excluded from the config hash).

---

## 7. What's broken / half-done / TODO (per STATE.md)

- 🟡 **Metadata `runsync` path UNVERIFIED** (STATE step 3, still `[ ]`) — the cost/latency numbers
  depend on it. Written with a `remote()` fallback. **Run the selftest first on day-of to confirm
  which path executes (watch the `meta` field).**
- ❌ **No warm demo tool pre-baked** (step 5) — needed for any "instant call" beat.
- ❌ **MCP server not wired into Claude Desktop / demo arc not dry-run** (step 6).
- ❌ **No network volume / weights staged** — every 📦 tool needs this built from scratch.
- ✅ Done: env recipe solved, unique-name fix baked in, leaked endpoint `u41hnv6yaptuxd` torn down,
  engine + MCP server written, decision-map + deployment architecture + tool catalog curated.
- ⚠️ **Untested assumptions to re-verify against current Flash version (it was 1.4.2 on 2026-06-22):**
  `LiveServerlessStub.prepare_request` signature, `resource.runsync` return shape, `FunctionResponse`
  import path, GPU `$/hr` rates (hardcoded approximations — verify before quoting on stage),
  stacked-`@remote` resolver working for string-authored fns.

### Selftest to run first (proves green loop + captures a real JobOutput)
```bash
RUNPOD_API_KEY=$(security find-generic-password -s runpod-api-key-dev -w) \
RUNPOD_API_BASE_URL=https://api.runpod.dev \
RUNPOD_REST_API_URL=https://rest.runpod.dev/v1 \
RUNPOD_ENDPOINT_BASE_URL=https://api.runpod.dev/v2 \
  /Users/zackmckenna/Developer/work/flash/.venv/bin/python -m toolbelt.core selftest
# deploys + tears down ONE dev endpoint; asserts doubled_sum==30; prints meta/cost/health
```

---

## Salvage map (fold into the fresh kit)

| Asset | Path | Action |
|---|---|---|
| Proven mint→call→teardown spine | `probe_loop.py` | **Copy as-is** |
| Engine (mint/call/meter/gc/rightsize) | `toolbelt/core.py` | **Copy**, re-verify runsync path |
| MCP stdio adapter (7 tools) | `toolbelt/server.py` | **Copy as-is** |
| Flash API map | `notes/flash-api.md` | **Copy** facts; re-pin to current version |
| Env recipe (3 host overrides) | `STATE.md` | **Copy** — top friction-saver |
| Cost model + rates | `core.py` `_DEFAULT_GPU_RATES` | Copy; verify $/hr |
| Cold-start doctrine + decision map | `notes/decision-map.md` | Copy strategy |
| Deployment (stdio-first) decision | `notes/deployment.md` | Copy |
| Bright Data interface | `notes/bright-data.md` | Copy; get token first |
| Tool catalog (deps shopping list) | `ideas/examples-tool-catalog.md` | Copy |
| Idea pitches + landscape | `ideas/*.md`, `notes/landscape.md` | Reference |
