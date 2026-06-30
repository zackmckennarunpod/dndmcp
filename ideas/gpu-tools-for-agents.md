# Idea: GPU Tools for Agents  (the substrate — candidate)

**One-liner:** an agent mints its own GPU tools at runtime — arbitrary GPU compute that no
API provides — then tears them down. Breaks the agent's "I can only call existing APIs" ceiling.

**Why it helps an agent (ranked by strength):**
1. **Run a model with no API** — niche/just-released open model (HF fine-tune, segmentation,
   protein, embeddings). Agent loads the exact model, uses it, tears down. Unique.
2. **Run arbitrary GPU *code*** — simulation, numerical optimization, custom kernel, image
   pipeline. No API exists for "arbitrary GPU computation." CPU sandboxes (E2B) can't; this can.
3. **Bulk/heavy work** — embed 1M docs, transcribe 100h audio. API = rate limits + cost.
   Mint one tool, fan out. (Caveat: elastic batch, not instant — ramp applies.)
4. **Private compute** — your infra, not a third-party API. Secondary for hackathon.

**Core value (honest):** agents are capped at "what APIs exist." GPU-tools lets an agent
provision custom GPU capability (any open model, any GPU code) on demand, pay nothing idle.

**Honest limitation:** cold start 60s-9min (measured) → "spawns a tool mid-sentence" is rough
unless pre-warmed. Realistic framing: "provisions custom GPU capability when a task needs it."

**Also measured:** Flash bursts are queue-then-ramp, not instant (peak 5/12, ~28s). The fan-out
story is *elastic scale 0→N→0 + scale-to-zero*, not "100 GPUs in an instant."

**Built & proven:** `forge/` (mint/run/cost/teardown/availability/MCP server), selftest 8/8,
UI. The MCP server makes this usable from Claude Desktop / Cursor today.

**Judging fit:** Creativity strong (white space — see research/06). Usefulness needs a NAMED
audience + crisp value prop (its weak pillar as pure substrate). Execution proven. Presentation
= the "agent gives itself a GPU superpower" story.

**Applications of this substrate:** [[cross-silicon-oracle]] (mint same tool across GPU types,
select), swarm/best-of-N, evolver, monte-carlo, map-reduce — all `mint → map → select/reduce`.
