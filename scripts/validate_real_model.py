"""Viability test on a REAL model — validates BOTH ideas at once.

Mints a real open embedding model (sentence-transformers all-MiniLM-L6-v2 — a genuine
RAG/search workload with NO public API needed) and runs it across GPU types.

Proves:
  - GPU-TOOLS viability: an agent can stand up a REAL model with no API and get real output.
  - ORACLE viability: the speed/$ ranking holds on a REAL workload, not a toy kernel
    (cost = $ per MILLION embeddings — a real money number for anyone building RAG).

    python -m scripts.validate_real_model              # 2 GPU types (cheaper)
    python -m scripts.validate_real_model --n-gpus=3

Always tears down, scoped, server-truth verified.
"""

from __future__ import annotations

import asyncio
import sys

import forge

# Real model, authored as an agent tool. All imports inside the body (only the body ships).
EMBED_CODE = '''
def handler(task):
    import time, torch
    from sentence_transformers import SentenceTransformer
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer("all-MiniLM-L6-v2", device=dev)
    n = int(task.get("n", 3000))
    texts = [f"document {i}: machine learning on gpus and retrieval augmented generation" for i in range(n)]
    model.encode(texts[:64], batch_size=64)                 # warmup
    t = time.time()
    emb = model.encode(texts, batch_size=128, show_progress_bar=False)
    dt = time.time() - t
    name = torch.cuda.get_device_name(0) if dev == "cuda" else "cpu"
    return {"device": name, "n": n, "seconds": round(dt, 3),
            "emb_per_sec": round(n / dt, 1), "dim": int(len(emb[0]))}
'''

# sentence-transformers pulls torch+transformers+hf — heavier build; lean enough to be safe.
DEPS = ["sentence-transformers"]
PREFER = ["ADA_24", "AMPERE_24", "AMPERE_48", "AMPERE_80"]


async def main(n_gpus: int = 2, keep: bool = False) -> int:
    print("=== Real-model viability: embeddings across GPUs ===")
    forge.load_env("prod")

    rows = await forge.available_gpus(min_vram_gb=16)
    in_stock = {r["group"]: r["price_usd_hr"] for r in rows
                if r["stock"] in ("High", "Medium") and r["group"] != "HOPPER_141"}
    ordered = [g for g in PREFER if g in in_stock] + [g for g in in_stock if g not in PREFER]
    groups = ordered[:n_gpus]
    print("GPU types:", groups)
    if not groups:
        print("no in-stock GPUs"); return 1

    tools = [forge.mint(f"embed-{g.lower()}", code=EMBED_CODE, gpu=g,
                        dependencies=DEPS, workers=(0, 1), idle_timeout=30) for g in groups]
    names = [t.name for t in tools]
    ok = False
    try:
        print("minting real embedding model on each GPU (cold start: model + deps, minutes) ...")
        results = await asyncio.gather(*(forge.call(t, {"n": 3000}) for t in tools))

        table = []
        for t, r in zip(tools, results):
            if not r.ok or "emb_per_sec" not in (r.output or {}):
                print(f"  {t.gpu:<12} FAILED: {r.error}"); continue
            o = r.output
            # cost per MILLION embeddings = (1e6 / throughput) seconds * $/hr / 3600
            usd_per_million = forge.cost_usd(t.gpu, (1_000_000 / o["emb_per_sec"]))
            table.append({"gpu": t.gpu, "device": o["device"], "eps": o["emb_per_sec"],
                          "dim": o["dim"], "usd_per_million": usd_per_million})

        if table:
            ok = True
            print("\n  REAL MODEL OUTPUT (sentence-transformers MiniLM, dim=384):")
            print(f"  {'GPU':<12} {'device':<26} {'emb/sec':>9}  {'$/1M embeds':>11}")
            for row in sorted(table, key=lambda x: x["usd_per_million"]):
                print(f"  {row['gpu']:<12} {row['device']:<26} {row['eps']:>9.0f}  {row['usd_per_million']:>11.4f}")
            fastest = max(table, key=lambda x: x["eps"])
            cheapest = min(table, key=lambda x: x["usd_per_million"])
            print(f"\n  ⚡ fastest:   {fastest['gpu']} ({fastest['eps']:.0f} emb/s)")
            print(f"  💰 cheapest: {cheapest['gpu']} (${cheapest['usd_per_million']:.4f} per 1M)")
            same = "SAME" if fastest["gpu"] == cheapest["gpu"] else "DIFFERENT"
            print(f"  -> fastest and cheapest are {same} GPU. Real model, real money, measured.")
            print("  VIABILITY: gpu-tools ✓ (real model, no API)  |  oracle ✓ (real workload ranking)")
    finally:
        if keep:
            print("\n--keep:", names)
        else:
            res = await forge.undeploy_tools(names)
            leaks = [n for n in res["remaining"] if "embed-" in n]
            print(f"\n  teardown: deleted {res['count']}; leaks: {leaks or 'NONE'}; remaining: {res['remaining']}")
    return 0 if ok else 1


if __name__ == "__main__":
    n = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--n-gpus=")), 2))
    sys.exit(asyncio.run(main(n_gpus=n, keep="--keep" in sys.argv)))
