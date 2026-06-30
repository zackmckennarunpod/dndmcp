"""Live capability verification — prove the FORGE idea actually works, then clean up.

Runs the core capabilities our hackathon idea depends on against REAL Flash, reports a
pass/fail checklist, and GUARANTEES teardown (server-truth, scoped — never touches
endpoints we didn't mint). Safe to run on the shared prod account.

    python -m scripts.verify_ideas            # prod
    python -m scripts.verify_ideas --keep     # skip teardown (debug)

Each check is independent and degrades gracefully so one failure doesn't block the rest.
"""

from __future__ import annotations

import asyncio
import sys
import time

import forge

RESULTS: list[tuple[str, bool, str]] = []
MINTED: list[str] = []  # tool names to guarantee teardown


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")


# --- agent-authored handlers (strings, exactly like the real product) -------------

DEPFREE_CODE = '''
def handler(numbers):
    import platform
    return {"doubled_sum": sum(n * 2 for n in numbers), "host": platform.node()}
'''

# Validates: real pip dep install + GPU/CUDA actually used + the kernel-autotune
# MECHANISM (time a GPU op, return ms). cfg = {"n": int} lets us sweep sizes.
TORCH_KERNEL_CODE = '''
def handler(cfg):
    import torch
    if not torch.cuda.is_available():
        return {"error": "no cuda", "device": "cpu"}
    n = int(cfg.get("n", 2048))
    x = torch.randn(n, n, device="cuda")
    norm = torch.nn.functional.layer_norm
    for _ in range(3):            # warmup
        y = norm(x, (n,))
    torch.cuda.synchronize()
    start = torch.cuda.Event(enable_timing=True); end = torch.cuda.Event(enable_timing=True)
    start.record()
    for _ in range(10):
        y = norm(x, (n,))
    end.record(); torch.cuda.synchronize()
    return {"device": torch.cuda.get_device_name(0), "n": n,
            "ms_per_iter": start.elapsed_time(end) / 10.0,
            "checksum": float(y.float().mean())}
'''


async def check_availability() -> None:
    rows = await forge.available_gpus(min_vram_gb=16)
    in_stock = [r for r in rows if r["stock"] in ("High", "Medium")]
    top = ", ".join(f"{r['displayName']}={r['stock']}" for r in rows[:3])
    record("1. availability returns live stock", bool(rows) and bool(in_stock),
           f"{len(rows)} gpus, {len(in_stock)} in stock; top: {top}")


async def check_mint_call(registry) -> forge.MintedTool:
    tool = forge.mint("verify-depfree", code=DEPFREE_CODE, gpu="ADA_24",
                      workers=(0, 3), idle_timeout=10)
    MINTED.append(tool.name)
    r = await forge.call(tool, [1, 2, 3, 4, 5], registry=registry)
    record("2. mint -> call (dep-free)", r.ok and r.output.get("doubled_sum") == 30,
           f"output={r.output}, {r.seconds:.0f}s, ${r.cost_usd:.4f}")
    return tool


async def check_fanout(tool, registry) -> None:
    payloads = [[i, i + 1, i + 2] for i in range(6)]
    results = await forge.fanout(tool, payloads, registry=registry)
    ok = [r for r in results if r.ok]
    rollup = forge.summarize([{"gpu": tool.gpu, "seconds": r.seconds, "ok": r.ok} for r in results])
    record("3. fan-out / burst (6 payloads)", len(ok) == len(payloads),
           f"{len(ok)}/{len(payloads)} ok, p50={rollup['p50_s']}s, ${rollup['total_usd']:.4f}")


async def check_torch_gpu(registry) -> forge.MintedTool | None:
    tool = forge.mint("verify-torch", code=TORCH_KERNEL_CODE, gpu="ADA_24",
                      dependencies=["torch"], workers=(0, 3), idle_timeout=20)
    MINTED.append(tool.name)
    r = await forge.call(tool, {"n": 2048}, registry=registry)
    used_gpu = r.ok and isinstance(r.output, dict) and "ms_per_iter" in r.output
    record("4+5. dep install (torch) + GPU/CUDA used", used_gpu,
           f"{r.output if r.ok else r.error} ({r.seconds:.0f}s)")
    return tool if used_gpu else None


async def check_flagship_mechanism(tool, registry) -> None:
    # The autotune loop in miniature: fan several configs across workers, pick the fastest.
    configs = [{"n": 1024}, {"n": 2048}, {"n": 4096}]
    results = await forge.fanout(tool, configs, registry=registry)
    timed = [r.output for r in results if r.ok and "ms_per_iter" in (r.output or {})]
    winner = min(timed, key=lambda o: o["ms_per_iter"]) if timed else None
    record("6. flagship mechanism (parallel autotune + pick winner)", winner is not None,
           f"{len(timed)}/{len(configs)} timed; fastest: n={winner['n']} @ "
           f"{winner['ms_per_iter']:.3f}ms on {winner['device']}" if winner else "no timings")


async def main(keep: bool = False) -> int:
    print("=== FORGE live capability verification (prod) ===")
    forge.load_env("prod")
    registry = forge.Registry()

    try:
        await check_availability()
        depfree_tool = await check_mint_call(registry)
        await check_fanout(depfree_tool, registry)
        torch_tool = await check_torch_gpu(registry)
        if torch_tool:
            await check_flagship_mechanism(torch_tool, registry)
        else:
            record("6. flagship mechanism", False, "skipped — torch tool failed to come up")
    finally:
        if keep:
            print("\n--keep set; leaving endpoints live. MINTED:", MINTED)
        else:
            print("\n=== teardown (server-truth, scoped) ===")
            result = await forge.undeploy_tools(MINTED)
            print(f"  deleted {result['count']}: {[d['name'] for d in result['deleted']]}")
            mine_left = [n for n in result["remaining"] if "verify-" in n]
            record("8. teardown leaves zero forge leaks", not mine_left,
                   f"server remaining: {result['remaining']}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n=== CHECKLIST: {passed}/{len(RESULTS)} passed ===")
    for name, ok, _ in RESULTS:
        print(f"  {'✅' if ok else '❌'} {name}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(keep="--keep" in sys.argv)))
