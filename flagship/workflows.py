"""Validate the workflows — TWO different capabilities, same primitives, same GPU tool.

Proves the emergence claim concretely: mint ONE GPU compute primitive, then build two
distinct capabilities purely by composing map / select / reduce client-side.

  workflow A  (map -> REDUCE)  = Monte-Carlo estimator of pi   -> known answer 3.14159
  workflow B  (map -> SELECT)  = best-of-N global optimizer     -> known answer 0 (Rastrigin)

Both use known-correct answers so the result is unambiguous. Both are GPU-essential and
run in ms after warmup. One mint = one cold start shared by both.

    python -m flagship.workflows
"""

from __future__ import annotations

import asyncio
import math
import statistics
import sys

import forge

# ONE versatile GPU primitive, minted once. Dispatches on task["op"]. Everything imported
# inside the body (only the body ships to the worker).
GPU_PRIMITIVE = '''
def handler(task):
    import torch, math
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    name = torch.cuda.get_device_name(0) if dev == "cuda" else "cpu"
    op, seed = task["op"], int(task.get("seed", 0))
    gen = torch.Generator(device=dev).manual_seed(seed)
    if op == "mc_pi":
        n = int(task.get("n", 4_000_000))
        pts = torch.rand(n, 2, generator=gen, device=dev)
        inside = ((pts ** 2).sum(1) <= 1.0).float().mean().item()
        return {"op": op, "estimate": 4.0 * inside, "device": name}
    if op == "rastrigin":
        d = 10
        x = ((torch.rand(d, generator=gen, device=dev) * 10.24) - 5.12).requires_grad_(True)
        opt = torch.optim.Adam([x], lr=0.05)
        for _ in range(500):
            opt.zero_grad()
            val = 10 * d + ((x ** 2) - 10 * torch.cos(2 * math.pi * x)).sum()
            val.backward(); opt.step()
        return {"op": op, "best": float(val.item()), "seed": seed, "device": name}
    return {"error": f"unknown op {op}"}
'''


async def main(keep: bool = False) -> int:
    print("=== Validating workflows: one GPU primitive, two compositions ===")
    forge.load_env("prod")
    tool = forge.mint("wf-primitive", code=GPU_PRIMITIVE, gpu="ADA_24",
                      dependencies=["torch"], workers=(0, 3), idle_timeout=40)
    registry = forge.Registry()
    ok = True

    try:
        # ---- Workflow A: map -> REDUCE (Monte-Carlo estimator) -------------------
        print("\n[A] map -> reduce : Monte-Carlo estimate of pi (16 parallel seeds)")
        seeds = [{"op": "mc_pi", "seed": s} for s in range(16)]
        res = await forge.fanout(tool, seeds, registry=registry)
        ests = [r.output["estimate"] for r in res if r.ok and "estimate" in (r.output or {})]
        one = ests[0]
        combined = statistics.fmean(ests)        # the REDUCE step
        dev = next((r.output["device"] for r in res if r.ok), "?")
        print(f"    single seed:   {one:.5f}  (err {abs(one-math.pi):.5f})")
        print(f"    reduce of {len(ests):>2}:  {combined:.5f}  (err {abs(combined-math.pi):.5f})  on {dev}")
        a_ok = abs(combined - math.pi) < abs(one - math.pi) and abs(combined - math.pi) < 0.01
        print(f"    -> reduce shrinks error toward pi: {'PASS' if a_ok else 'FAIL'}")
        ok = ok and a_ok

        # ---- Workflow B: map -> SELECT (best-of-N global optimizer) --------------
        print("\n[B] map -> select : best-of-N optimization of Rastrigin (16 random starts)")
        starts = [{"op": "rastrigin", "seed": s} for s in range(16)]
        res = await forge.fanout(tool, starts, registry=registry)
        vals = sorted(r.output["best"] for r in res if r.ok and "best" in (r.output or {}))
        typical = statistics.median(vals)
        best_of_n = vals[0]                       # the SELECT step
        print(f"    typical single run (median): {typical:.3f}")
        print(f"    best-of-{len(vals)} (select min):     {best_of_n:.3f}   (global min = 0)")
        b_ok = best_of_n < typical and best_of_n < typical * 0.6
        print(f"    -> best-of-N beats a single attempt: {'PASS' if b_ok else 'FAIL'}")
        ok = ok and b_ok

        print(f"\n  cost over all evals: {forge.summarize(registry.call_records())}")
        print(f"\n  EMERGENCE: same mint + map, different reducer/selector -> two capabilities. "
              f"{'BOTH PASS ✅' if ok else 'see failures above'}")
    finally:
        if keep:
            print("\n--keep set; leaving endpoint live:", tool.name)
        else:
            r = await forge.undeploy_tools([tool.name])
            leaks = [n for n in r["remaining"] if "wf-" in n]
            print(f"\n  teardown: deleted {r['count']}; leaks: {leaks or 'NONE'}; remaining: {r['remaining']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(keep="--keep" in sys.argv)))
