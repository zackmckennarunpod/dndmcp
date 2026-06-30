"""Call + fan-out — the execution layer with telemetry.

Flash has no `.map()`; fan-out is `asyncio.gather` with a concurrency cap. We wrap
every call to capture wall-clock latency (for the cost dashboard) and to degrade
gracefully on partial failure rather than blowing up a whole batch.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .cost import cost_usd
from .minting import MintedTool


@dataclass
class CallResult:
    ok: bool
    output: Any = None
    error: str | None = None
    seconds: float = 0.0
    cost_usd: float = 0.0
    worker_id: str | None = None

    def as_meta(self) -> dict:
        return {
            "ok": self.ok,
            "seconds": round(self.seconds, 3),
            "cost_usd": round(self.cost_usd, 6),
            "worker_id": self.worker_id,
            "error": self.error,
        }


async def call(tool: MintedTool, payload: Any, *, registry=None) -> CallResult:
    """Invoke a minted tool once, timing it and (optionally) recording to a registry.

    NOTE: a decorator-minted Flash tool returns the handler's value directly (no job
    object), so timing is wall-clock — correct for cost, and the only signal Flash 1.7.0
    exposes for decorator calls. First call pays cold start (can exceed 60s).
    """
    started = time.perf_counter()
    try:
        output = await tool.callable(payload)
        elapsed = time.perf_counter() - started
        result = CallResult(
            ok=True,
            output=output,
            seconds=elapsed,
            cost_usd=cost_usd(tool.gpu, elapsed),
            worker_id=getattr(tool.endpoint, "id", None),
        )
    except Exception as exc:  # never let one call kill a batch
        elapsed = time.perf_counter() - started
        result = CallResult(ok=False, error=f"{type(exc).__name__}: {exc}", seconds=elapsed)

    # NOTE: Flash's Endpoint object never exposes the deployed id (.id stays None). We do
    # NOT resolve it here — the local `flash undeploy list` lags reality and would miss a
    # just-created endpoint. Teardown resolves the id from SERVER truth instead.
    if registry is not None:
        registry.record_call(
            tool=tool.name, seconds=result.seconds, ok=result.ok,
            worker_id=result.worker_id, error=result.error,
        )
    return result


async def fanout(
    tool: MintedTool,
    payloads: list[Any],
    *,
    concurrency: int | None = None,
    registry=None,
) -> list[CallResult]:
    """Run `payloads` through `tool` concurrently, capped at `concurrency`.

    Cap defaults to the tool's max workers — no point launching more in-flight calls
    than there are workers to serve them. Partial failures come back as failed
    CallResults, not exceptions.
    """
    cap = concurrency or max(tool.workers[1], 1)
    limiter = asyncio.Semaphore(cap)

    async def one(payload: Any) -> CallResult:
        async with limiter:
            return await call(tool, payload, registry=registry)

    return await asyncio.gather(*(one(p) for p in payloads))
