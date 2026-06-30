"""Live end-to-end selftest — RUN THIS FIRST on hack day.

Proves the whole spine against real Flash: wire env -> mint a dependency-free tool
-> deploy+call -> assert result -> read cost/latency -> tear down. Dependency-free
on purpose so it's a ~60s cold start, not a 3-minute pip install.

Usage:
    # prod (default) — picks up key from env or keychain 'runpod-api-key-prod'
    python -m forge.selftest
    # dev control plane
    python -m forge.selftest --profile dev
    # keep the endpoint alive afterwards (skip teardown)
    python -m forge.selftest --keep
"""

from __future__ import annotations

import asyncio
import sys

from . import availability, cost, env, minting, run, teardown

PROBE_CODE = '''
def handler(numbers):
    import platform, os
    return {
        "doubled_sum": sum(n * 2 for n in numbers),
        "ran_on_host": platform.node(),
        "python": platform.python_version(),
        "remote_cwd": os.getcwd(),
    }
'''

PROBE_NAME = "forge-selftest-probe"


async def main(profile: str = "prod", keep: bool = False) -> int:
    print(f"[1/6] wiring env (profile={profile}) ...")
    env.load_env(profile)
    print(f"      active profile: {env.active_profile()}")

    print("[2/6] querying GPU availability (the SDK-gap filler) ...")
    try:
        options = await availability.available_gpus(min_vram_gb=16)
        top = options[:3]
        for o in top:
            print(f"      {o['displayName']:<28} {o['memoryInGb']}GB  stock={o['stock']}  group={o['group']}")
        if top and top[0]["stock"] == "unknown":
            print("      NOTE: stock='unknown' -> rich query fell back. Check RICH_GPU_QUERY vs live schema.")
    except Exception as exc:
        print(f"      availability query failed (non-fatal): {type(exc).__name__}: {exc}")

    print("[3/6] minting a dependency-free GPU tool ...")
    tool = minting.mint(PROBE_NAME, code=PROBE_CODE, gpu="ADA_24", workers=(0, 1), idle_timeout=5)
    print(f"      endpoint name: {tool.endpoint_name}")

    ok = False
    try:
        print("[4/6] calling it (first call pays cold start, can exceed 60s) ...")
        result = await run.call(tool, [1, 2, 3, 4, 5])
        if not result.ok:
            print(f"      CALL FAILED: {result.error}")
            return 1
        out = result.output
        print(f"      output: {out}")
        assert out.get("doubled_sum") == 30, f"unexpected: {out}"
        ok = True

        print("[5/6] cost / latency readout ...")
        print(f"      latency: {result.seconds:.2f}s  cost: ${result.cost_usd:.6f}  "
              f"endpoint_id: {tool.endpoint_id}")
        print(f"      rollup: {cost.summarize([{'gpu': tool.gpu, 'seconds': result.seconds, 'ok': True}])}")
    finally:
        if keep:
            print("[6/6] --keep set; leaving endpoint live.")
        else:
            print("[6/6] tearing down (server-truth, scoped to this tool) ...")
            result = await teardown.undeploy(PROBE_NAME)
            print(f"      undeploy -> {result}")
            # Verify against SERVER truth that our endpoint is gone (and report what's left).
            remaining = await teardown.server_endpoints()
            ours = [e for e in remaining if "forge-selftest-probe" in e["name"]]
            if ours:
                print(f"      ⚠ LEAK — still present: {ours}; deleting by id")
                for e in ours:
                    await teardown.delete_endpoint(e["id"])
                remaining = await teardown.server_endpoints()
            print(f"      server now has {len(remaining)} endpoint(s): "
                  f"{[e['name'] for e in remaining]} (none should be ours)")

    print("\nSELFTEST", "PASSED ✅" if ok else "FAILED ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    profile_arg = "dev" if "--profile" in args and args[args.index("--profile") + 1] == "dev" else "prod"
    sys.exit(asyncio.run(main(profile=profile_arg, keep="--keep" in args)))
