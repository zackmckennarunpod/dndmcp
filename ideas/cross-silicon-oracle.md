# Idea: Cross-Silicon GPU Oracle  (candidate, not locked)

**One-liner:** take your actual code, run it on several DIFFERENT real GPUs at once, get
back a ranked table — fastest + cheapest-per-op — then run the real job on the winner.

**Problem it solves:** ~40 GPU options on RunPod; people guess or assume "bigger = better."
The guess is often wrong on cost. This turns the choice into a measured answer.

**Why only Flash:** needs 3-4 different real GPUs spun up in ~a minute, run, thrown away.
Elsewhere = hours of Docker/provisioning per GPU. Flash makes it a function call.

**Proof (live, prod):** same LayerNorm kernel, simultaneous:
| GPU | speed | $/1k ops |
|---|---|---|
| RTX A6000 | 0.296 ms | $0.00005 |
| L4 | 0.565 ms | $0.00007 |
Punchline: the "cheaper" L4 is ~1.9× slower → costs MORE per op. Guessing = wrong.
(`scripts/cross_silicon.py`; a 3-GPU run was also validated.)

**Audience:** anyone spending money on GPU inference/training; the agent/dev choosing hardware.

**Judging fit:** Creativity (nobody can do this) · Usefulness (real money question, surprising
answer) · Execution (proven) · Presentation (a table with a surprising winner).

**Open questions / risks:**
- Toy kernel → need a *realistic* workload (real model inference) for the table to feel real.
- $/op uses our estimated rate table, not live billing — directionally right, label it.
- Cold-start variance (60s-9min) — for a video, pre-warm and edit; for live, would bite.

**Relationship:** this is really ONE application of the GPU-tools substrate (see
[[gpu-tools-for-agents]]) — mint the same tool across different `gpu=` types, then `select`.
