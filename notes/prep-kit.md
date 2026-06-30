# Prep kit — be ready, not committed

**Premise:** the hosts will almost certainly announce a specific angle/theme on hack day
(Tue Jun 30). So the goal *right now* is **optionality**, not picking the winning idea: walk in
with the boring 60% solved and a shelf of warm building blocks, then spend the day *composing*
into whatever they announce. Optimize for "reusable across any theme," never "polish on one idea."

A hackathon is lost by spending the day on infra/bootstrap. Everything here exists to make sure we don't.

---

## The kit — 3 layers, ordered by how many themes they pay off across

### 1. The spine (pays off in EVERY theme — build first)
A working Flash project skeleton clonable into anything:
- Auth wired, `@remote` deploy loop proven end-to-end on real hardware.
- **Fan-out helper** — there's no `.map()`; a clean `asyncio.gather` wrapper with a concurrency cap
  + partial-failure handling.
- **Cost/latency readout** from `JobOutput` (`Delay Time` + `Execution Time` → $). Serves the
  cost-awareness rubric line *no matter what we build*.
- **Drop-in demo UI**: live worker-count graph + cost ticker. Every winning Flash demo needs the
  burst-and-cost visual — build it once, skin it later.

### 2. Pre-warmed primitive tools (compose into anything)
A handful of GPU functions covering the common workload shapes, deps lean (exclude torch), big
weights pre-staged on a network volume, ready to `workersMin=1`:
- transcription (Whisper), image gen (SDXL), embeddings, vision/caption, OCR, a small LLM,
  bg-remove/upscale.

Whatever they announce — "media tool," "process this data," "make an agent" — we assemble from
primitives that already exist and warm in seconds. This is the adaptability reserve.

### 3. Reusable Flash patterns (hard mechanics, pre-solved)
So we never burn day-of time relearning the SDK:
- **Cold-start recipe** (see below) as copy-paste config.
- **Runtime-deploy helper** — idea 01's ~15-line "code → live endpoint" loop. On the shelf in case
  the theme is agent/tool-shaped (the moat play).
- **MCP server spine** (3 tools + registry) — greenfield per `flash-api.md`; pre-building = insurance.
- **Network-volume weight-staging script** — staging takes wall-clock and EU-RO-1 is the only DC;
  do it before the day, not during.

---

## Cold start is the master constraint (it shapes the demo, not just the build)

Anatomy (from README execution log, lines 124–139): the ~1 min first call is
`Delay Time: 51842 ms` (worker provision + **`pip install` of deps at worker init**) vs
`Execution Time: 1533 ms`. **Cold start is dominated by dependency install, not compute.**

**Sorting principle:** cold start *punishes* demos whose wow IS the spin-up moment
(a tool appearing, a model materializing on demand), and is *nearly free* for demos that
**pre-warm a pool, then burst/serve/batch** (a one-time upfront cost amortized over volume).

Robustness ranking: `08 batch-eval > burst-payload demos > 01 mint+call > 06 audience burst > 05 model-zoo`.
(05's "instant on-demand" pitch is undercut by cold start — can't have both zero-idle AND instant.)

### The judo: weaponize the 60s instead of hiding it
On Modal/Replicate/SageMaker, deploying a new GPU capability = Docker build + registry push =
*minutes*. Flash does it in ~60s as a **runtime call**. So a live-mint beat becomes:
> "My agent is deploying a brand-new GPU tool right now, mid-conversation. On any container
> platform we'd still be waiting on the Docker build. It's already live."
The 60s stops being dead air and becomes proof of the moat. Only available to runtime-deploy ideas (01).

### Demo architecture that falls out of it — two paths
1. **Warm path (the bulk):** everything that must feel instant runs on **pre-warmed** endpoints
   (`workersMin=1`, deployed via `--auto-provision` before we walk on stage). All "call the tool, ~1s" beats.
2. **Cold path (exactly one beat):** a single *narrated* live-mint we *want* to take 60s — that's where
   the competitor-comparison judo runs. Make it a lean-dep tool (pre-installed torch, no weight download)
   so it's 60s, not 3 min.

Pre-warming costs idle GPU $ — **make that the cost-awareness story**, don't hide it:
on screen, "warm pool costs $X/hr idle; the long tail scales to zero."

---

## Decision tree — announcement → grab these blocks

| If they announce…            | Grab                                                        |
|------------------------------|-------------------------------------------------------------|
| Agents / tools / MCP         | runtime-deploy helper + MCP spine + primitives              |
| Media / data processing      | pipeline pattern + transcription/vision/embedding primitives|
| Real-time / interactive      | warm pool + burst UI (audience-participation beat)          |
| Cost / efficiency / scale    | batch-eval pattern + the cost scoreboard                    |
| "Build a product" (open)     | compose primitives into one vertical app                    |

Plus reusable narration regardless of theme: the **60s-runtime-deploy-vs-Docker-build** talking points.

---

## What NOT to over-invest in now
- Don't choreograph one idea's full demo — the theme may not fit it. Keep `ideas/` files as
  lightweight skeletons to grab.
- Don't pick the headline idea today. The investment goes into spine + primitives + patterns,
  which survive any pivot.

## Build priority
1. **Spine** (`kit/`) — no announced angle makes it useless; most likely to eat day-of hours if unsolved.
2. **2–3 primitives + weight staging** — pre-warming/staging has real wall-clock; can't be last-minute.
3. **Patterns** (runtime-deploy helper, MCP spine) — insurance for the likely agent-tooling angle.
