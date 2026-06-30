"""Instrument the 'burst' — is it actually parallel, or queue-then-ramp?

Blasts N requests at a queue-based Flash endpoint simultaneously and reconstructs the
real timeline: which worker served each request, when it started/ended (server-side),
and how concurrency ramped. Dep-free handler (fixed sleep, no torch) so cold start is
~60s and the experiment is cheap; the sleep makes overlap visible.

    python -m scripts.burst_timeline
"""

from __future__ import annotations

import asyncio
import sys
import time

import forge

N = 12
WORK_S = 2.5

HANDLER = '''
def handler(req):
    import platform, time, os
    s = time.time()
    time.sleep(req.get("work_s", 2.5))   # fixed work so concurrency is visible
    e = time.time()
    return {"worker": platform.node(), "pid": os.getpid(),
            "srv_start": s, "srv_end": e, "idx": req.get("idx")}
'''


async def main(keep: bool = False) -> int:
    forge.load_env("prod")
    tool = forge.mint("burst-timeline", code=HANDLER, gpu="ADA_24",
                      workers=(0, 6), idle_timeout=20)
    print(f"minted {tool.endpoint_name}; blasting {N} requests simultaneously ...")

    t0 = time.time()
    records: list[dict] = []

    async def one(i: int) -> None:
        submit = time.time() - t0
        r = await forge.call(tool, {"idx": i, "work_s": WORK_S})
        done = time.time() - t0
        o = r.output if (r.ok and isinstance(r.output, dict)) else {}
        records.append({"idx": i, "submit": submit, "done": done, "ok": r.ok,
                        "worker": o.get("worker"), "srv_start": o.get("srv_start"),
                        "srv_end": o.get("srv_end"), "err": r.error})

    try:
        # NO concurrency cap — send all N at once to truly test the burst.
        await asyncio.gather(*(one(i) for i in range(N)))
        good = [r for r in records if r["ok"] and r["srv_start"]]
        if not good:
            print("all failed:", [r["err"] for r in records][:2]); return 1

        first = min(r["srv_start"] for r in good)
        for r in good:
            r["s_rel"] = r["srv_start"] - first
            r["e_rel"] = r["srv_end"] - first
        good.sort(key=lambda r: r["s_rel"])
        workers = sorted({r["worker"] for r in good})
        wid = {w: f"W{i}" for i, w in enumerate(workers)}

        # Gantt (server-side execution windows), scale ~4 chars/sec
        print(f"\n  {len(good)} ok across {len(workers)} distinct worker(s)")
        print("  idx wk   submit  done   server-exec timeline (each █≈0.25s, t=first exec start)")
        scale = 4
        for r in good:
            pad = " " * int(r["s_rel"] * scale)
            bar = "█" * max(1, int((r["e_rel"] - r["s_rel"]) * scale))
            print(f"  {r['idx']:>3} {wid[r['worker']]:<3} {r['submit']:>6.1f} {r['done']:>6.1f}  {pad}{bar}")

        # concurrency ramp: how many executing at each 0.5s tick
        span = max(r["e_rel"] for r in good)
        ticks = [t * 0.5 for t in range(int(span / 0.5) + 2)]
        ramp = [(t, sum(1 for r in good if r["s_rel"] <= t < r["e_rel"])) for t in ticks]
        peak = max(c for _, c in ramp)
        print(f"\n  concurrency ramp (workers executing in parallel over time):")
        print("   " + "  ".join(f"{c}" for _, c in ramp))
        print(f"\n  VERDICT: peak parallelism = {peak} of {N} requests; "
              f"{len(workers)} worker(s) spun up. "
              f"{'-> queue-then-ramp, NOT instant burst' if peak < N else '-> true parallel burst'}")
    finally:
        if keep:
            print("\n--keep:", tool.name)
        else:
            res = await forge.undeploy_tools([tool.name])
            print(f"\n  teardown: deleted {res['count']}; remaining: {res['remaining']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(keep="--keep" in sys.argv)))
