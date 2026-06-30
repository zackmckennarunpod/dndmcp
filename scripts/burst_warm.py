"""A/B: does a PRE-WARMED pool give a true parallel burst (vs the cold ramp)?

Cold baseline (already measured by burst_timeline): peak 5/12 parallel, ~28s, gradual ramp.
This mints workers_min=6 (6 workers kept hot), warms them, then blasts 12 requests and
reconstructs the timeline. Expectation: peak ~6 from the start, ~5s wall-clock.

ALWAYS tears down — workers_min=6 burns ~$4/hr hot, must not leak.

    python -m scripts.burst_warm
"""

from __future__ import annotations

import asyncio
import sys
import time

import forge

N = 12
WORK_S = 2.5
TARGET_WORKERS = 6
HANDLER = '''
def handler(req):
    import platform, time
    s = time.time()
    time.sleep(req.get("work_s", 2.5))
    e = time.time()
    return {"worker": platform.node(), "srv_start": s, "srv_end": e, "idx": req.get("idx")}
'''


def summarize(records: list[dict], label: str) -> dict:
    good = [r for r in records if r["ok"] and r["srv_start"]]
    if not good:
        print(f"  [{label}] all failed: {[r['err'] for r in records][:1]}")
        return {"ok": 0, "workers": 0, "peak": 0, "wall": 0.0, "start_skew": 0.0}

    first = min(r["srv_start"] for r in good)
    for r in good:
        r["s_rel"] = r["srv_start"] - first
        r["e_rel"] = r["srv_end"] - first
    span = max(r["e_rel"] for r in good)
    ticks = [t * 0.5 for t in range(int(span / 0.5) + 2)]
    peak = max(sum(1 for r in good if r["s_rel"] <= t < r["e_rel"]) for t in ticks)
    workers = sorted({r["worker"] for r in good})
    wall = max(r["done"] for r in records)
    start_skew = max(r["s_rel"] for r in good)
    print(
        f"  [{label}] {len(good)}/{N} ok | {len(workers)} workers | "
        f"PEAK PARALLEL = {peak} | wall {wall:.1f}s | start skew {start_skew:.1f}s"
    )
    return {
        "ok": len(good),
        "workers": len(workers),
        "peak": peak,
        "wall": wall,
        "start_skew": start_skew,
    }


async def blast(tool, label: str) -> tuple[list[dict], dict]:
    t0 = time.time()
    records: list[dict] = []

    async def one(i: int) -> None:
        r = await forge.call(tool, {"idx": i, "work_s": WORK_S})
        o = r.output if (r.ok and isinstance(r.output, dict)) else {}
        records.append({"idx": i, "done": time.time() - t0, "ok": r.ok,
                        "worker": o.get("worker"), "srv_start": o.get("srv_start"),
                        "srv_end": o.get("srv_end"), "err": r.error})

    await asyncio.gather(*(one(i) for i in range(N)))
    summary = summarize(records, label)
    return records, summary


async def main() -> int:
    forge.load_env("prod")
    print(f"minting PRE-WARMED pool: workers=({TARGET_WORKERS},{TARGET_WORKERS}) ...")
    tool = forge.mint("burst-warm", code=HANDLER, gpu="ADA_24",
                      workers=(TARGET_WORKERS, TARGET_WORKERS), idle_timeout=120)
    try:
        print("warm phase (spins up all 6 — pays cold start once):")
        _, warm = await blast(tool, "warm-up")
        if warm["workers"] < TARGET_WORKERS:
            print("warm-up did not touch every worker; running one extra warm pass ...")
            await blast(tool, "warm-up-2")
        print("measured phase (pool now hot — expect peak ~6, ~5s):")
        _, hot = await blast(tool, "PREWARMED")
        print("\n  compare to COLD baseline: peak 5/12, ~28s, gradual ramp")
        print(
            "  verdict: "
            + (
                "prewarm gives true bounded parallelism"
                if hot["peak"] >= TARGET_WORKERS and hot["wall"] <= 10
                else "prewarm improves but still does not behave like an instant burst"
            )
        )
    finally:
        print("\n  tearing down warm pool (must — workers_min=6 is costly) ...")
        res = await forge.undeploy_tools([tool.name])
        print(f"  deleted {res['count']}; remaining: {res['remaining']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
