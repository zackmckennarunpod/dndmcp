"""Runtime mint — turn an agent-authored code string into a LIVE Flash GPU endpoint.

This is the moat beat: code -> live GPU endpoint in ~60s as a function call, where
every other platform needs a Docker build + registry push (minutes).

Verified against runpod-flash 1.7.0:
  - `Endpoint(name=..., gpu=GpuGroup.X, workers=(min,max), dependencies=[...])` builds a config.
  - Applying it to a function (`Endpoint(...)(handler)`, i.e. `__call__(func)`) returns an
    awaitable wrapper. Deploy happens lazily on first `await wrapped(payload)`.
  - After deploy, the Endpoint instance carries `.id` (the endpoint id) for teardown.

Two gotchas baked in from prior prep:
  1. Agent code is written to a REAL .py file and imported — Flash captures source via
     `inspect.getsource()`, which fails on `exec()`'d functions (no __file__).
  2. Endpoint names get a stable hash suffix so re-minting the same tool reuses its
     endpoint (Flash reuses on config-hash match) while distinct tools never collide.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Reminder (KNOWLEDGE.md gotcha #1): the agent's handler body ships ALONE to the worker.
# Every import / helper / constant it uses MUST live INSIDE `def handler(...)`.
HANDLER_CONTRACT = "agent code must define a top-level `def handler(payload): ...`"


def _tool_code_dir() -> Path:
    path = Path(os.environ.get("FORGE_STATE_DIR", ".forge")) / "tools"
    path.mkdir(parents=True, exist_ok=True)
    return path


def unique_endpoint_name(friendly: str) -> str:
    """Stable, collision-free endpoint name for a friendly tool name."""
    short_hash = hashlib.sha1(friendly.encode()).hexdigest()[:6]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in friendly).strip("-")
    return f"{safe}-{short_hash}"


def materialize_handler(name: str, code: str) -> Callable[[Any], Any]:
    """Write `code` to a real importable module and return its `handler`."""
    module_name = f"forge_tool_{name.replace('-', '_')}"
    path = _tool_code_dir() / f"{module_name}.py"
    path.write_text(code)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load tool module for {name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "handler"):
        raise ValueError(f"tool {name!r}: {HANDLER_CONTRACT}")
    return module.handler


@dataclass
class MintedTool:
    """A live (or lazily-deployed) GPU tool the agent owns."""

    name: str
    endpoint_name: str
    gpu: str
    dependencies: list[str]
    workers: tuple[int, int]
    endpoint: Any                       # runpod_flash.Endpoint instance
    callable: Callable[[Any], Any]      # awaitable wrapper: await tool.callable(payload)
    idle_timeout: int = 60
    resolved_endpoint_id: str | None = None  # filled after first call (Endpoint.id stays None)
    extra: dict = field(default_factory=dict)

    @property
    def endpoint_id(self) -> str | None:
        # Flash's Endpoint.id is unreliable (stays None); prefer the resolved id.
        return self.resolved_endpoint_id or getattr(self.endpoint, "id", None)


def mint(
    name: str,
    *,
    code: str,
    gpu: str = "ADA_24",
    dependencies: list[str] | None = None,
    system_dependencies: list[str] | None = None,
    workers: tuple[int, int] = (0, 3),
    idle_timeout: int = 60,
    env: dict[str, str] | None = None,
    volume: Any = None,
    cuda_versions: list[str] | None = None,
) -> MintedTool:
    """Materialize agent `code` and bind it to a Flash GPU Endpoint (deploys on first call).

    `gpu` is a GpuGroup name (e.g. "ADA_24", "AMPERE_80"). Deploy is lazy — call the
    returned tool to provision + run, or pre-warm with `warm()`.

    `cuda_versions` (e.g. ["12.8"]) pins workers to hosts supporting that CUDA. REQUIRED to
    avoid the runpod/flash:latest 'cuda>=12.8' container-init crash on older-driver hosts —
    that image needs 12.8, and without pinning, workers land on bad hosts and crash-loop.
    """
    from runpod_flash import CudaVersion, Endpoint, GpuGroup  # lazy

    if gpu not in GpuGroup.__members__:
        raise ValueError(f"unknown gpu {gpu!r}; choose from {list(GpuGroup.__members__)}")

    handler = materialize_handler(name, code)
    endpoint = Endpoint(
        name=unique_endpoint_name(name),
        gpu=GpuGroup[gpu],
        workers=workers,
        idle_timeout=idle_timeout,
        dependencies=dependencies or [],
        system_dependencies=system_dependencies or None,
        env=env or None,
        volume=volume,
    )
    if cuda_versions:
        # The Endpoint ctor has no cuda kwarg; set it on the cached resource config (persists
        # through deploy). Accept enum or string value (CudaVersion('12.8') -> V12_8).
        cfg = endpoint._build_resource_config()  # noqa: SLF001
        cfg.cudaVersions = [v if isinstance(v, CudaVersion) else CudaVersion(str(v)) for v in cuda_versions]
    wrapped = endpoint(handler)  # Endpoint.__call__(func) -> awaitable wrapper
    return MintedTool(
        name=name,
        endpoint_name=endpoint.name,
        gpu=gpu,
        dependencies=dependencies or [],
        workers=workers,
        endpoint=endpoint,
        callable=wrapped,
        idle_timeout=idle_timeout,
    )


async def warm(tool: MintedTool, warmup_payload: Any) -> MintedTool:
    """Force a deploy + one call so the worker is hot before the demo (amortizes cold start).

    Use a payload the handler accepts cheaply. After this, `tool.endpoint_id` is set.
    """
    await tool.callable(warmup_payload)
    return tool
