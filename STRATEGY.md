# Flash Hackathon — Killer Use Cases, Meta Spin & the $10k Target

> **UPDATE (Jun 29) — flagship locked & PROVEN: the Cross-Silicon Optimizer.**
> Kernel autotuning *as an algorithm* is done-to-death (AutoKernel, KernelAgent, KernelBench 2026).
> The new, only-Flash-possible angle = **run the same code across many DIFFERENT real GPUs on demand
> and rank by speed AND $/op.** Single-box autotuners can't (no hardware breadth); benchmark sites
> aren't your code/live/agent-driven. Also fixes the team's documented "agents can't pick hardware" gap.
> **Live-proven** (`scripts/cross_silicon.py`): same LayerNorm kernel on RTX A6000 (0.296ms, $0.00005/1k)
> vs L4 (0.565ms, $0.00007/1k) — the cheaper-tier L4 is ~1.9x slower so it costs MORE per op. You'd
> guess wrong without measuring. The kernel is just the payload; swap in any model/inference config.
> Replaces the plain LoRA-sweep flagship (commodity). See `research/06-landscape-2026.md`.


> Reframe (locked): **No Docker — it's all Flash.** GPU work = `@Endpoint`. Control plane/dashboard runs locally during the demo or deploys as a Flash **CPU load-balanced endpoint** (Mode 2). The "working image" we want is a warm Flash project + local skill, not a container.

## The one insight everything hangs on
Flash's moat is **not running models**. It's that **an agent can mint brand-new GPU compute on demand, mid-task, in ~60s, as a function call** — where every other platform needs a Docker build + registry push (minutes). So the highest-leverage thing to build is not "an app that calls SDXL." It's **the agent-native layer that turns Flash into an agent's hands-on GPU.**

This is also exactly the gap the Flash team itself has identified:
- Usability findings: *agents are poor at Flash* (hardware/region selection, no live stock).
- Real ask surfaced internally: *"good instructions for Flash for an agent setting up inferences?"*
- The documented gap: no `DataCenter.available_gpus()` → agents reverse-engineer GraphQL, hit 403.
- DX papercuts: orphan endpoints, no URL output, cold starts.

**Meta spin: don't build *on* Flash — build the missing agent layer *for* Flash.** Judges = the Flash team. A project that makes their own product agent-native, and turns three of their documented pain points into features, lands harder than any vertical app.

---

## Killer use cases (answering "what can an agent do with immediate GPU access?")

### Tier 1 — hard/impossible WITHOUT a GPU (the "wow, it couldn't do that before")
- **Write → compile → autotune a Triton/CUDA kernel on a real GPU**, read the benchmark, iterate. An agent fundamentally cannot do this without GPU hardware. Self-improving kernel loop = jaw-drop.
- **LoRA / hyperparameter sweep**: fan out N training configs across N GPU workers, score, pick the winner — minutes, not a day. Parallel experimentation an agent can't fake on CPU.
- **Profile & optimize**: `torch.compile`, flash-attention, nsight — measure real speedups on real silicon.
- **Reproduce a paper's GPU result** / verify a claim empirically.

### Tier 2 — beyond inference: prepare, learn, build
- **Mint → train → serve loop**: agent gathers data, fine-tunes a small model, redeploys it as a *new* live endpoint — all in one conversation.
- **Eval-as-fan-out**: spin a model, run an eval suite across a worker pool, score with cost readout.
- **Elastic data processing**: embeddings/video/OCR over a large corpus, fan out across workers, scale 0→N→0.

### Tier 3 — the substrate (inference primitives, warm and ready)
Whisper · SDXL · embeddings · small-LLM (vLLM) · vision/caption · OCR · bg-remove/upscale. These are the *adaptability reserve* — compose into whatever theme is announced. Not the headline; the safety net.

---

## RECOMMENDED TARGET — "FORGE": GPU-on-tap for agents
**One sentence:** an MCP server + live dashboard that gives any agent (Claude, Cursor, Codex) hands-on GPU through Flash — discover stock, mint a new GPU tool at runtime, run it with live cost, and tear down — with one flagship "couldn't-do-this-without-a-GPU" beat.

**The five capabilities (each maps to a team pain point or rubric line):**
1. **`gpu.available()`** — live GPU/DC stock via authed GraphQL. *Fills the documented SDK gap; no more 403 reverse-engineering.*
2. **`gpu.mint(spec)`** — NL/code spec → live Flash `@Endpoint` in ~60s. *The moat beat, narrated against Docker build+push.*
3. **`gpu.run(...)` + fan-out** — call it, submit many jobs/configs/hardware trials, and show **live dashboard** data: worker count, p50/p99, **$/call**, and cold-ramp vs warm-pool behavior. *Hits the cost-awareness rubric line.*
4. **Flagship beat (the differentiator)** — pick ONE Tier-1 thing the agent does on the GPU that it couldn't otherwise: **autotune a Triton kernel** *or* **a LoRA sweep**. This is what separates us from every "I called SDXL" demo.
5. **`gpu.cleanup()`** — `flash undeploy --all`. *Solves the orphan-endpoint sprawl complaint, on stage.*

**Why it deserves $10k**
- Makes Flash's unique capability (sub-minute runtime GPU deploy) *legible and useful to the fastest-growing consumer of compute — AI agents.*
- Turns 3 documented team pain points (agent usability, availability gap, DX papercuts) into shipped features.
- The Tier-1 beat proves GPU work *beyond inference* — depth, not a wrapper.

**Why it wins on adaptability (survives any announced theme)**
It's a *substrate*, not a vertical. Media theme → mint media tools. Data theme → mint processors + fan-out. Agent/MCP theme → it already IS the agent tooling. Cost theme → the dashboard is the centerpiece. We change the flagship beat, not the spine.

**Demo arc (≈4 min)**
1. Agent: "I need to transcribe + summarize this." → `gpu.available()` picks a DC with stock → `gpu.mint()` → narrate the 60s: *"on Modal we'd still be waiting on a Docker build; it's already live."* → result. (moat)
2. Fan it out — dashboard lights up, **$ ticker** runs; show the honest latency/cost tradeoff: cold endpoints ramp, warm pools cost $X/hr, tail scales to zero. (cost rubric)
3. Flagship: "now optimize this kernel" → agent writes a Triton kernel, deploys, benchmarks on a real GPU, reports the speedup. *(couldn't-do-without-GPU)*
4. `gpu.cleanup()` → endpoints gone. (DX papercut, solved live)

**Risk & mitigation (keep it achievable)**
- Core spine (discover/mint/run/cost/cleanup) is **already proven** in `gpu-toolbelt` (mint→call→teardown ran on dev) — port to the v1.17.0 `Endpoint` API. Low risk.
- The Tier-1 flagship is the only risky part → build the **LoRA sweep** as the safe default (lean deps, no exotic toolchain) and treat the **Triton kernel** beat as the stretch wow. Have both; demo whichever is solid by Monday night.
- Watch the known footguns: torch broken under `flash dev` (use `flash deploy` for GPU primitives), all-or-nothing multi-endpoint builds (lean deps), `runsync` 60s timeout (use `run()`+`wait()`).

---

## Alternatives (if you want a different flavor)
- **A. "Availability Copilot"** — laser-focus on just the GPU-stock gap: an `available_gpus()` SDK helper + MCP tool + a placement optimizer (cheapest DC with stock for a given model). Smaller, very on-target for the team, but less of a wow demo.
- **B. "Self-improving GPU agent"** — go all-in on the Tier-1 kernel/training loop as the headline (agent measurably improves code on real hardware). Highest wow, highest risk.
- **C. "Vertical product"** — compose primitives into one polished app (e.g. a media pipeline). Safest to demo, lowest meta-resonance with Flash-team judges.

**Pick:** FORGE (recommended) = the substrate with a Tier-1 flagship — best blend of moat demo + rubric fit + adaptability + achievability.

## What the kit becomes (once a target is locked)
Same `kit/` spine as planned, now Flash-native (no Docker): `env.py` (host override + auth), `availability.py` (the gap-filler), `deploy.py` (runtime mint + prints URL), `fanout.py`, `cost.py` (dashboard data), `teardown.py`, `mcp_server.py` (stdio), `http_server.py` (dashboard) — control plane optionally deployed as a Flash CPU LB endpoint. Plus all primitives warm, and a `flagship/` (Triton + LoRA beats).
