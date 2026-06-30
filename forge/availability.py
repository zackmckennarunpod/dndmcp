"""GPU availability — the SDK gap, filled.

Documented team pain point (Flash usability study): the SDK has NO way to ask
"which GPUs / datacenters actually have stock right now?" Agents (Codex, Copilot,
Cursor) had to reverse-engineer the GraphQL API and hit 403 Cloudflare blocks. The
SDK's own `get_gpu_types()` returns only static metadata (id/displayName/VRAM/
secureCloud) — no live stock.

This module adds `available_gpus()` / `pick()` on top of the *authenticated* Flash
GraphQL client (so no 403 — we reuse the SDK's auth, not raw scraping), returning
live stock + price so an agent can choose hardware before deploying.

⚠ VERIFY-LIVE: the exact `gpuTypes.lowestPrice.stockStatus` schema fields below are
the publicly-known Runpod GraphQL shape; confirm against a live key with `selftest`
and adjust QUERY if the schema differs. The function degrades gracefully (falls back
to the static SDK list with stock="unknown") if the rich query errors.
"""

from __future__ import annotations

from typing import Any

# Map a concrete GPU (by VRAM) back to the Flash GpuGroup you'd pass to Endpoint(gpu=...).
GPU_GROUP_BY_VRAM: list[tuple[int, str]] = [
    (16, "AMPERE_16"),
    (24, "AMPERE_24"),   # also ADA_24 (4090) — prefer per task
    (48, "AMPERE_48"),
    (80, "AMPERE_80"),
    (141, "HOPPER_141"),
]

# Rich query: static metadata + live lowestPrice (carries stockStatus + price).
# VERIFIED on prod 2026-06-26: the input type is NON-NULL `GpuLowestPriceInput!` and
# MUST include gpuCount, else every row errors. Pass dataCenterId to scope to a DC.
RICH_GPU_QUERY = """
query forgeGpuAvailability($lp: GpuLowestPriceInput!) {
  gpuTypes {
    id
    displayName
    memoryInGb
    secureCloud
    lowestPrice(input: $lp) {
      stockStatus
      minimumBidPrice
      uninterruptablePrice
    }
  }
}
"""


def vram_to_group(memory_gb: int) -> str:
    for threshold, group in GPU_GROUP_BY_VRAM:
        if memory_gb <= threshold:
            return group
    return "HOPPER_141"


async def available_gpus(
    *,
    min_vram_gb: int = 0,
    data_center_id: str | None = None,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Return live GPU availability, richest-data-first.

    Each row: {id, displayName, memoryInGb, group, stock, price_usd_hr, secure}.
    `stock` is the API's stockStatus ("High"/"Medium"/"Low"/None) or "unknown" on
    fallback. Sorted by stock desc then price asc.
    """
    from runpod_flash.core.api import RunpodGraphQLClient

    client = RunpodGraphQLClient(api_key=api_key)
    rows: list[dict[str, Any]] = []
    try:
        # gpuCount is REQUIRED by the schema; dataCenterId optionally scopes to one DC.
        lp_input: dict[str, Any] = {"gpuCount": 1}
        if data_center_id:
            lp_input["dataCenterId"] = data_center_id
        try:
            result = await client._execute_graphql(  # noqa: SLF001 — reuse SDK auth/retry
                RICH_GPU_QUERY, {"lp": lp_input}
            )
            gpu_types = result.get("gpuTypes", []) or []
            for g in gpu_types:
                low = g.get("lowestPrice") or {}
                rows.append({
                    "id": g.get("id"),
                    "displayName": g.get("displayName"),
                    "memoryInGb": g.get("memoryInGb") or 0,
                    "group": vram_to_group(g.get("memoryInGb") or 0),
                    "stock": low.get("stockStatus") or "unknown",
                    "price_usd_hr": low.get("uninterruptablePrice"),
                    "secure": g.get("secureCloud"),
                })
        except Exception:
            # Degrade to the static SDK list — still useful (VRAM + group), no live stock.
            for g in await client.get_gpu_types():
                rows.append({
                    "id": g.get("id"),
                    "displayName": g.get("displayName"),
                    "memoryInGb": g.get("memoryInGb") or 0,
                    "group": vram_to_group(g.get("memoryInGb") or 0),
                    "stock": "unknown",
                    "price_usd_hr": None,
                    "secure": g.get("secureCloud"),
                })
    finally:
        await client.close()

    if min_vram_gb:
        rows = [r for r in rows if r["memoryInGb"] >= min_vram_gb]

    stock_rank = {"High": 0, "Medium": 1, "Low": 2, "unknown": 3, None: 4}
    rows.sort(key=lambda r: (stock_rank.get(r["stock"], 5), r["price_usd_hr"] or 1e9))
    return rows


async def pick(*, min_vram_gb: int, api_key: str | None = None) -> dict[str, Any] | None:
    """Pick the in-stock GPU with enough VRAM at the best price. Returns a row (incl.
    the Flash `group` you pass to Endpoint(gpu=...)), or None if nothing qualifies."""
    options = await available_gpus(min_vram_gb=min_vram_gb, api_key=api_key)
    in_stock = [o for o in options if o["stock"] in ("High", "Medium")]
    return (in_stock or options or [None])[0]
