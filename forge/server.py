"""FORGE MCP server (stdio) — the agent interface.

Exposes FORGE's five verbs as MCP tools so any MCP client (Claude Desktop, Cursor,
Codex) gets hands-on GPU through Flash. stdio transport => no ingress, no auth, no
pod to expose; the process runs locally and calls Flash over the network.

Run:
    python -m forge.server          # prod key from env/keychain
    FORGE_PROFILE=dev python -m forge.server

Claude Desktop config (claude_desktop_config.json):
    "forge": {
      "command": "/abs/path/.venv/bin/python",
      "args": ["-m", "forge.server"],
      "env": {"RUNPOD_API_KEY": "...", "FORGE_PROFILE": "prod"}
    }
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import availability, cost, env, run as run_mod, teardown
from .minting import MintedTool, mint as mint_fn
from .registry import Registry

env.load_env(os.environ.get("FORGE_PROFILE", "prod"))

mcp = FastMCP("forge")
registry = Registry()
_live: dict[str, MintedTool] = {}  # name -> minted tool (this process)


@mcp.tool()
async def gpu_available(min_vram_gb: int = 0) -> list[dict]:
    """Live GPU stock + price across Runpod (fills the SDK's availability gap).
    Returns rows with the Flash `group` to pass to gpu_mint(gpu=...). Call this BEFORE
    minting so you pick hardware that's actually in stock."""
    return await availability.available_gpus(min_vram_gb=min_vram_gb)


@mcp.tool()
async def gpu_mint(
    name: str,
    code: str,
    gpu: str = "ADA_24",
    dependencies: list[str] | None = None,
    workers_min: int = 0,
    workers_max: int = 3,
    idle_timeout: int = 60,
) -> dict:
    """Mint a brand-new GPU tool at runtime: deploy `code` as a live, auto-scaling Flash
    endpoint in ~60s. `code` MUST define `def handler(payload)` with ALL imports/helpers
    INSIDE the function body (only the body ships to the worker). `gpu` is a GpuGroup
    name (e.g. ADA_24, AMPERE_80). Deploy is lazy — first gpu_call provisions it."""
    tool = mint_fn(
        name, code=code, gpu=gpu, dependencies=dependencies or [],
        workers=(workers_min, workers_max), idle_timeout=idle_timeout,
    )
    _live[name] = tool
    registry.upsert_tool(
        name=name, gpu=gpu, workers_min=workers_min, workers_max=workers_max,
        deps=dependencies or [],
    )
    return {"name": name, "endpoint_name": tool.endpoint_name, "gpu": gpu, "status": "minted"}


@mcp.tool()
async def gpu_call(name: str, payload: Any) -> dict:
    """Invoke a minted tool once. Returns {output, meta} where meta has latency + cost.
    First call to a tool pays cold start (can exceed 60s)."""
    tool = _live.get(name)
    if tool is None:
        return {"error": f"no live tool {name!r} in this session; gpu_mint it first"}
    result = await run_mod.call(tool, payload, registry=registry)
    return {"output": result.output, "meta": result.as_meta()}


@mcp.tool()
async def gpu_fanout(name: str, payloads: list, concurrency: int | None = None) -> dict:
    """Run many payloads through a minted tool concurrently (burst). Returns per-call
    results + a cost/latency rollup for the batch."""
    tool = _live.get(name)
    if tool is None:
        return {"error": f"no live tool {name!r} in this session; gpu_mint it first"}
    results = await run_mod.fanout(tool, payloads, concurrency=concurrency, registry=registry)
    rollup = cost.summarize([{"gpu": tool.gpu, "seconds": r.seconds, "ok": r.ok} for r in results])
    return {
        "results": [{"output": r.output, **r.as_meta()} for r in results],
        "rollup": rollup,
    }


@mcp.tool()
def fleet_cost() -> dict:
    """Total $ spent, $/call, p50/p99 latency, success rate across all minted tools —
    plus ongoing idle burn for any pinned-warm pool. The cost-awareness dashboard data."""
    records = registry.call_records()
    rollup = cost.summarize(records)
    idle = sum(
        cost.idle_burn_usd_per_hr(t["gpu"], t["workers_min"]) for t in registry.tools()
    )
    return {**rollup, "idle_burn_usd_per_hr": round(idle, 4)}


@mcp.tool()
def fleet_list() -> list[dict]:
    """List every GPU tool minted on this account (persists across sessions)."""
    return registry.tools()


@mcp.tool()
async def fleet_cleanup(scope: str = "all") -> dict:
    """Tear down FORGE-minted endpoints to stop the burn. scope: 'all' (every tool THIS
    agent minted — never other endpoints on the account) or a single tool name. Solves
    the orphan-endpoint sprawl — the on-stage mic-drop. Safe on a shared account."""
    if scope == "all":
        names = [t["name"] for t in registry.tools()]
        out = await teardown.undeploy_tools(names) if names else {"deleted": [], "count": 0}
        for name in names:
            registry.forget_tool(name)
        _live.clear()
        return {"scope": "all", "result": out}
    out = await teardown.undeploy(scope)
    registry.forget_tool(scope)
    _live.pop(scope, None)
    return {"scope": scope, "result": out}


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
