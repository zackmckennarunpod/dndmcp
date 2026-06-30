"""Evolver — parallel evolutionary optimization powered by GPU burst.

The PROJECT, not the plumbing. A generic evolutionary loop where each candidate's
fitness is a GPU computation and the whole population is evaluated as a Flash BURST
(fan-out across workers, scale to zero between generations). "Evolve anything" = swap
the fitness handler.

This demo evolves the weights of a small neural net with NO gradients (neuroevolution /
evolution strategies) to fit a target function — fitness = MSE computed on the GPU. If
the loss drops generation over generation, the engine works and the fitness is pluggable.

Run (background recommended — cold start can be minutes):
    python -m flagship.evolver
"""

from __future__ import annotations

import asyncio
import random
import sys

import forge

# 2 -> 8 -> 1 MLP: W1[2,8]=16, b1[8]=8, W2[8,1]=8, b2[1]=1  => 33 weights
GENOME_LEN = 33

# Fitness handler authored as a string, minted as a GPU tool. Receives a genome (flat
# list of 33 weights), builds the MLP on the GPU, returns MSE on a fixed task. Everything
# is imported INSIDE the body (only the body ships to the worker).
FITNESS_CODE = '''
def handler(genome):
    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # Fixed deterministic task: y = sin(3*x0) * x1 over a 64-point grid.
    xs = [[(i % 7) / 3.0 - 1.0, (i % 5) / 2.0 - 1.0] for i in range(64)]
    X = torch.tensor(xs, dtype=torch.float32, device=dev)
    Y = (torch.sin(3 * X[:, 0]) * X[:, 1]).unsqueeze(1)
    g = torch.tensor(genome, dtype=torch.float32, device=dev)
    i = 0
    W1 = g[i:i+16].view(2, 8); i += 16
    b1 = g[i:i+8];             i += 8
    W2 = g[i:i+8].view(8, 1);  i += 8
    b2 = g[i:i+1]
    pred = torch.tanh(X @ W1 + b1) @ W2 + b2
    mse = torch.mean((pred - Y) ** 2).item()
    return {"mse": mse, "device": torch.cuda.get_device_name(0) if dev == "cuda" else "cpu"}
'''


async def evolve(profile: str = "prod", pop: int = 10, gens: int = 10,
                 elite: int = 3, workers_max: int = 5, keep: bool = False) -> dict:
    print(f"=== Evolver: pop={pop}, generations={gens}, GPU-burst fitness ===")
    forge.load_env(profile)
    tool = forge.mint("evolver-fitness", code=FITNESS_CODE, gpu="ADA_24",
                      dependencies=["torch"], workers=(0, workers_max), idle_timeout=30)
    registry = forge.Registry()
    rng = random.Random(0)

    population = [[rng.gauss(0, 1) for _ in range(GENOME_LEN)] for _ in range(pop)]
    best_mse, best_genome, history = float("inf"), None, []

    try:
        for gen in range(gens):
            # BURST: evaluate the whole population in parallel across Flash GPU workers.
            results = await forge.fanout(tool, population, registry=registry)
            scored = sorted(
                ((r.output["mse"], population[i]) for i, r in enumerate(results)
                 if r.ok and isinstance(r.output, dict)),
                key=lambda t: t[0],
            )
            if not scored:
                print(f"  gen {gen}: all evals failed ({[r.error for r in results][:1]})"); break

            gen_best = scored[0][0]
            if gen_best < best_mse:
                best_mse, best_genome = gen_best, scored[0][1]
            device = next((r.output.get("device") for r in results if r.ok), "?")
            history.append(gen_best)
            bar = "█" * max(1, int(40 * (1 - min(gen_best, 1.0))))
            print(f"  gen {gen:>2}: best MSE={gen_best:.4f}  run-best={best_mse:.4f}  {bar}  ({device})")

            # Elitist reproduction: keep top `elite`, fill the rest by mutating them.
            elites = [g for _, g in scored[:elite]]
            children = []
            while len(children) < pop - len(elites):
                parent = rng.choice(elites)
                children.append([w + rng.gauss(0, 0.3) for w in parent])
            population = elites + children

        baseline = history[0] if history else float("nan")
        improvement = (1 - best_mse / baseline) * 100 if history and baseline else 0
        print(f"\n  EVOLVED: MSE {baseline:.4f} (gen0 random) -> {best_mse:.4f} "
              f"= {improvement:.0f}% better, no gradients, GPU-bursted.")
        print(f"  cost/latency over all {pop*len(history)} evals: {forge.summarize(registry.call_records())}")
        return {"best_mse": best_mse, "baseline": baseline, "history": history}
    finally:
        if keep:
            print("\n--keep set; leaving endpoint live:", tool.name)
        else:
            res = await forge.undeploy_tools([tool.name])
            leaks = [n for n in res["remaining"] if "evolver" in n]
            print(f"\n  teardown: deleted {res['count']}; leaks: {leaks or 'NONE'}; remaining: {res['remaining']}")


if __name__ == "__main__":
    # Modest params to limit cold-start cost: 2 workers (2 torch installs), warm after.
    asyncio.run(evolve(pop=8, gens=8, elite=3, workers_max=2, keep="--keep" in sys.argv))
