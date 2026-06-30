# The GPU Fan-Out Algebra — core primitives, emergent capability

> The realization: swarm, evolver, map-reduce, cross-silicon, grid-search are **not separate
> projects.** They're all the SAME tiny set of primitives, recomposed. The winning project
> isn't a monolith — it's a small, well-designed **algebra over GPU fan-out**, where the
> impressive capabilities *emerge* from composition. We already built & ran every primitive;
> the contribution is naming them orthogonally and showing the emergence.

## Live finding: fan-out is real, "instant burst" is not

Validated Jun 30 with `scripts/burst_timeline.py` and `scripts/burst_warm.py`:

- Cold queue-based endpoint, `workers=(0,6)`, 12 simultaneous 2.5s jobs:
  **peak parallelism 5/12**, wall **~28s**, gradual queue-then-ramp.
- Pre-warmed endpoint, `workers=(6,6)`, same 12 jobs:
  warm-up hit only **4 workers**; measured phase **peak parallelism 4/12**, wall **~13s**,
  start skew **~8.6s**.

So the primitive is **fan-out over Flash's elastic worker pool**, not guaranteed
simultaneous N-way execution. The pitch should be: agents can mint GPU tools, push many
jobs/configs/hardware trials, measure the real ramp/cost/perf, and clean up. Use
"burst" only when a specific workflow has been live-proven to start together.

## The core primitives (4 load-bearing + 1 combinator)
Each is already proven live in this repo — this isn't speculative.

| Primitive | Signature | What it is | Proven in |
|---|---|---|---|
| **mint** | `code → GpuFn` | turn an agent-authored code string into a live GPU function | `forge.mint` (selftest) |
| **map** | `(GpuFn, items) → results` | fan out work over a collection; Flash scales workers elastically, often as a ramp | `forge.fanout` (verify, cross_silicon, burst_timeline) |
| **select** | `(scored, k, key) → top-k` | rank candidates, keep the best by a key | cross_silicon, evolver |
| **reduce** | `(results, how) → summary` | aggregate: vote / mean / merge / cost-rollup | `forge.summarize` |
| **loop** | `(step, until) → state` | iterate a step until a condition (generations, rounds) | evolver |

`generate` (make N candidates) and `score` (attach a fitness) are just specializations of
`map`. So really **three** load-bearing verbs — `mint`, `map`, `select/reduce` — plus `loop`.

## Emergence: every "project" is a one-liner composition
```
evolve        = loop( generate∘mutate → map(score) → select )      # neuroevolution, search
swarm / BoN   = map(generate N) → map(verify) → reduce(vote)       # test-time compute
map-reduce    = map(process) → reduce(merge)                       # process a huge input
cross-silicon = map_over(hardware)(score) → select(by ms or $/op)  # the hardware oracle
grid search   = map(score over configs) → select(best)             # hyperparameter sweep
tournament    = loop( map(pairwise) → select )                     # debate / ranking
monte-carlo   = map(simulate) → reduce(mean)                       # simulation at scale
```
Same 4 primitives, different glue. THAT is the design: power from composition, not surface area.

## Why this is the winning framing (not base-value plumbing)
- "Mint an endpoint" is Flash's base value. **A composable algebra over GPU fan-out** is a real
  design contribution — and engineers (the judges) reward elegant orthogonal primitives.
- It's **demoable as emergence**: "here are 4 primitives; watch evolve, then swarm, then search —
  each ~10 lines, same parts." The *recomposition* is the wow, not any single run.
- It's **theme-proof** (hack day): whatever they announce, the answer is a new composition of
  the same primitives — maximal optionality, zero rebuild.
- Flash is the essential substrate: `map` = cheap ephemeral GPU fan-out with scale-to-zero,
  which only Flash makes trivial. The algebra is useless without it.

## Design rules for the primitives (so they actually compose)
1. **Uniform shapes.** A *candidate* is just data; a *scored candidate* is `(score, candidate)`;
   a *fitness* is a `GpuFn: candidate → number`. Uniformity is what lets anything snap together.
2. **map is the only place workers spin up.** Everything else is pure/local — composition stays cheap.
   From cold, treat scale-up as a measured ramp. If a demo needs tight parallelism, explicitly
   pre-warm and show the latency/cost tradeoff.
3. **Recipes, not features.** `evolve`/`swarm`/`search` are ~10-line recipes built FROM the core,
   not bespoke code. Adding a capability = adding a composition, not a subsystem.
4. **Cost & teardown are cross-cutting**, attached at `map`, so every composition is auto-metered
   and auto-cleaned.

## Proposed shape for `forge`
- **core:** `mint`, `gmap` (alias of fanout), `score = gmap+key`, `select`, `reduce`.
- **recipes/:** `evolve.py`, `swarm.py`, `search.py`, `cross_silicon.py` — each a thin composition.
- The demo: show ONE primitive set producing 3 emergent capabilities live.
