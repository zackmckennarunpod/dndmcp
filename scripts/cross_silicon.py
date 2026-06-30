"""Cross-silicon optimizer — the Flash-native NEW capability.

Take ONE kernel/workload, run it on several DIFFERENT real GPU types in parallel, and
rank by measured speed AND $/op. No single-box autotuner can do this — it needs instant
access to heterogeneous real hardware, which is exactly Flash's edge. Also directly
answers the team's "agents can't pick hardware" gap: hardware choice becomes a measured
experiment, not a guess.

    python -m scripts.cross_silicon            # prod; auto-picks in-stock GPU groups

Guarantees teardown (server-truth, scoped). Safe on the shared account.
"""

from __future__ import annotations

import asyncio
import sys

import forge

# Same kernel everywhere — only the GPU changes. (LayerNorm: memory-bound, reliable wins.)
KERNEL_CODE = '''
def handler(cfg):
    import torch
    if not torch.cuda.is_available():
        return {"error": "no cuda"}
    n = int(cfg.get("n", 4096)); iters = int(cfg.get("iters", 50))
    x = torch.randn(n, n, device="cuda")
    norm = torch.nn.functional.layer_norm
    for _ in range(5):
        y = norm(x, (n,))
    torch.cuda.synchronize()
    s = torch.cuda.Event(enable_timing=True); e = torch.cuda.Event(enable_timing=True)
    s.record()
    for _ in range(iters):
        y = norm(x, (n,))
    e.record(); torch.cuda.synchronize()
    ms = s.elapsed_time(e) / iters
    gb = (x.numel() * x.element_size() * 2) / 1e9      # read+write
    return {"device": torch.cuda.get_device_name(0), "ms_per_iter": ms,
            "GBps": gb / (ms / 1000.0), "checksum": float(y.float().mean())}
'''

PAYLOAD = {"n": 4096, "iters": 50}


async def main(keep: bool = False) -> int:
    print("=== Cross-silicon optimizer (prod) ===")
    forge.load_env("prod")

    # config: how many distinct GPU types, and which to avoid (scarce = slow to provision)
    n_gpus = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--n-gpus=")), 3))
    # HOPPER_141 (H200) is scarce -> minutes to provision; skip unless explicitly allowed.
    avoid = set() if "--allow-scarce" in sys.argv else {"HOPPER_141"}
    # Prefer common, fast-provisioning groups first for a reliable demo.
    PREFER = ["ADA_24", "AMPERE_24", "AMPERE_48", "AMPERE_80", "ADA_48_PRO", "ADA_80_PRO"]

    # 1) Ask Flash what's in stock, pick N DISTINCT in-stock GPU groups (preferred order).
    rows = await forge.available_gpus(min_vram_gb=16)
    in_stock = {r["group"]: r["price_usd_hr"] for r in rows
                if r["stock"] in ("High", "Medium") and r["group"] not in avoid}
    ordered = [g for g in PREFER if g in in_stock] + [g for g in in_stock if g not in PREFER]
    groups = [(g, in_stock[g]) for g in ordered[:n_gpus]]
    print("picked in-stock GPU groups:", [g for g, _ in groups])
    if not groups:
        print("no in-stock GPUs; aborting"); return 1

    # 2) Mint the SAME kernel once per GPU group (one endpoint each).
    tools = []
    for group, _ in groups:
        t = forge.mint(f"xsil-{group.lower()}", code=KERNEL_CODE, gpu=group,
                       dependencies=["torch"], workers=(0, 1), idle_timeout=20)
        tools.append(t)
    names = [t.name for t in tools]

    try:
        # 3) Run the same payload on every GPU type, in parallel.
        print("running same kernel across all GPU types in parallel (cold starts vary 1-9min) ...")
        results = await asyncio.gather(*(forge.call(t, PAYLOAD) for t in tools))

        # 4) Rank by speed and by $/op (cost of one iter = rate x exec-seconds).
        table = []
        for t, r in zip(tools, results):
            if not r.ok or "ms_per_iter" not in (r.output or {}):
                print(f"  {t.gpu:<12} FAILED: {r.error}"); continue
            o = r.output
            usd_per_1k = forge.cost_usd(t.gpu, (o["ms_per_iter"] / 1000.0) * 1000)  # 1000 iters
            table.append({"gpu": t.gpu, "device": o["device"], "ms": o["ms_per_iter"],
                          "GBps": o["GBps"], "usd_per_1k_iters": usd_per_1k})

        if table:
            print("\n  GPU          device                         ms/iter   GB/s    $/1k-iters")
            for row in sorted(table, key=lambda x: x["ms"]):
                print(f"  {row['gpu']:<12} {row['device']:<28} {row['ms']:>7.4f}  {row['GBps']:>6.0f}  {row['usd_per_1k_iters']:>9.5f}")
            fastest = min(table, key=lambda x: x["ms"])
            cheapest = min(table, key=lambda x: x["usd_per_1k_iters"])
            print(f"\n  ⚡ fastest:        {fastest['gpu']} ({fastest['ms']:.4f} ms)")
            print(f"  💰 best $/op:      {cheapest['gpu']} (${cheapest['usd_per_1k_iters']:.5f}/1k)")
            print("  → THIS is the new thing: same code, measured live across real heterogeneous silicon.")
    finally:
        if keep:
            print("\n--keep set; leaving endpoints live:", names)
        else:
            print("\n=== teardown ===")
            res = await forge.undeploy_tools(names)
            mine = [n for n in res["remaining"] if "xsil-" in n]
            print(f"  deleted {res['count']}; forge leaks: {mine or 'NONE'}; remaining: {res['remaining']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(keep="--keep" in sys.argv)))
