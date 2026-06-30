"""FORGE demo dashboard — make GPU fan-out + cost visible.

A local web UI that runs a workflow on Flash and shows, live: fan-out (per-task chips
lighting up as workers process them), a running $ ticker, latency, streaming results, and
the final headline. The fan-out-and-cost visual is the demo wow, independent of workload.

Run:
    .venv/bin/python -m ui.server                 # http://localhost:8000
    FORGE_PROFILE=prod .venv/bin/python -m ui.server

Develop without spending: every /run accepts mock=1 to simulate fan-out (no GPU).
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from contextlib import suppress

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path

import forge

app = FastAPI(title="FORGE demo")
HTML = (Path(__file__).parent / "index.html").read_text()

# Shared live state the dashboard polls. One run at a time (it's a demo).
STATE: dict = {
    "running": False, "workflow": "", "phase": "idle",
    "tasks_total": 0, "tasks_done": 0, "inflight": 0, "max_workers": 0,
    "cost_usd": 0.0, "elapsed_s": 0.0,
    "results": [], "headline": "", "log": [], "tool_ready": False,
}
_TOOL = {"obj": None}  # the warm minted GPU primitive (real mode)

# Same versatile GPU primitive as flagship/workflows.py: mc_pi + rastrigin.
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
        return {"estimate": 4.0 * inside, "device": name}
    if op == "rastrigin":
        d = 10
        x = ((torch.rand(d, generator=gen, device=dev) * 10.24) - 5.12).requires_grad_(True)
        opt = torch.optim.Adam([x], lr=0.05)
        for _ in range(500):
            opt.zero_grad()
            val = 10 * d + ((x ** 2) - 10 * torch.cos(2 * math.pi * x)).sum()
            val.backward(); opt.step()
        return {"best": float(val.item()), "device": name}
    return {"error": "unknown op"}
'''

WORKFLOWS = {
    "montecarlo": {"op": "mc_pi", "n": 16, "label": "Monte-Carlo π  (map → reduce)"},
    "bestofn":    {"op": "rastrigin", "n": 16, "label": "Best-of-N optimize  (map → select)"},
    "swarm":      {"op": "rastrigin", "n": 24, "label": "Swarm / best-of-N  (map → select)"},
}


def _log(msg: str) -> None:
    STATE["log"] = (STATE["log"] + [msg])[-12:]


def _reset(workflow: str, n: int, max_workers: int) -> None:
    STATE.update(running=True, workflow=workflow, phase="starting", tasks_total=n,
                 tasks_done=0, inflight=0, max_workers=max_workers, cost_usd=0.0,
                 elapsed_s=0.0, results=[], headline="", log=[])


async def _run_one_mock(i: int) -> dict:
    STATE["inflight"] += 1
    await asyncio.sleep(random.uniform(0.4, 1.4))  # pretend GPU work
    STATE["inflight"] -= 1
    STATE["tasks_done"] += 1
    STATE["cost_usd"] += random.uniform(0.002, 0.006)
    val = random.uniform(3.10, 3.18) if STATE["workflow"] == "montecarlo" else random.uniform(20, 90)
    STATE["results"] = STATE["results"] + [{"i": i, "value": round(val, 4)}]
    return {"value": val}


async def _run_one_real(tool, task: dict, i: int) -> dict:
    STATE["inflight"] += 1
    try:
        r = await forge.call(tool, task)
    finally:
        STATE["inflight"] -= 1
    STATE["tasks_done"] += 1
    STATE["cost_usd"] += r.cost_usd
    out = r.output if r.ok else {"error": r.error}
    value = out.get("estimate", out.get("best")) if isinstance(out, dict) else None
    if value is not None:
        STATE["results"] = STATE["results"] + [{"i": i, "value": round(value, 4)}]
    return out


async def _runner(workflow: str, mock: bool) -> None:
    import math
    spec = WORKFLOWS[workflow]
    n = spec["n"]
    started = time.perf_counter()
    tool = None
    try:
        if not mock:
            forge.load_env(os.environ.get("FORGE_PROFILE", "prod"))
            tool = _TOOL["obj"]
            if tool is None:
                STATE["phase"] = "minting GPU primitive (cold start can be minutes)"
                _log("minting wf-ui-primitive ...")
                tool = forge.mint("wf-ui-primitive", code=GPU_PRIMITIVE, gpu="ADA_24",
                                  dependencies=["torch"], workers=(0, 6), idle_timeout=120)
                _TOOL["obj"] = tool
        STATE["phase"] = "fan-out"
        _log(f"fanning out {n} GPU tasks ...")
        cap = STATE["max_workers"]
        limiter = asyncio.Semaphore(cap)

        async def one(i: int):
            async with limiter:
                if mock:
                    return await _run_one_mock(i)
                return await _run_one_real(tool, {"op": spec["op"], "seed": i, "n": 4_000_000}, i)

        outs = await asyncio.gather(*(one(i) for i in range(n)))

        # reduce / select -> headline
        STATE["phase"] = "aggregating"
        if workflow == "montecarlo":
            ests = [o["value"] if mock else o.get("estimate") for o in outs]
            ests = [e for e in ests if e is not None]
            combined = sum(ests) / len(ests)
            STATE["headline"] = f"π ≈ {combined:.5f}   (err {abs(combined - math.pi):.5f}, reduced over {len(ests)})"
        else:
            vals = [o["value"] if mock else o.get("best") for o in outs]
            vals = sorted(v for v in vals if v is not None)
            STATE["headline"] = f"best-of-{len(vals)} = {vals[0]:.3f}   (global min 0; single typ. ~{vals[len(vals)//2]:.0f})"
        _log("done.")
    except Exception as exc:  # surface, don't crash the server
        STATE["headline"] = f"ERROR: {type(exc).__name__}: {exc}"
        _log(STATE["headline"])
    finally:
        STATE["elapsed_s"] = round(time.perf_counter() - started, 1)
        STATE["phase"] = "done"
        STATE["running"] = False


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML


@app.get("/status")
async def status() -> JSONResponse:
    return JSONResponse(STATE)


@app.post("/run")
async def run(workflow: str = "montecarlo", mock: int = 0, workers: int = 6) -> JSONResponse:
    if STATE["running"]:
        return JSONResponse({"error": "already running"}, status_code=409)
    if workflow not in WORKFLOWS:
        return JSONResponse({"error": "unknown workflow"}, status_code=400)
    _reset(workflow, WORKFLOWS[workflow]["n"], workers)
    asyncio.create_task(_runner(workflow, bool(mock)))
    return JSONResponse({"started": workflow, "mock": bool(mock)})


@app.post("/teardown")
async def teardown() -> JSONResponse:
    tool = _TOOL["obj"]
    if tool is None:
        return JSONResponse({"deleted": 0, "note": "no warm tool"})
    forge.load_env(os.environ.get("FORGE_PROFILE", "prod"))
    res = await forge.undeploy_tools([tool.name])
    _TOOL["obj"] = None
    STATE["tool_ready"] = False
    return JSONResponse(res)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), log_level="warning")


if __name__ == "__main__":
    main()
