"""Teardown — SAFE, server-truth-based. Only ever deletes endpoints FORGE minted.

Hard-won lessons from live validation on a shared prod account:
  - `flash undeploy list` reads a LOCAL cache (.runpod/resources.pkl) that lags reality:
    right after a deploy it can show 0, and a just-created endpoint leaks. So we resolve
    the truth from the SERVER (`myself.endpoints`), not the local list.
  - `flash undeploy --all` deletes EVERY endpoint on the account — including ones you
    didn't mint (e.g. a pre-existing `runpod-coder-v1`). NEVER use it on a shared account.
    We delete by explicit name match only.
  - A raw GraphQL delete leaves the local pickle stale, so the next same-named mint tries
    to UPDATE a dead endpoint ("Endpoint not found"). After deleting, clear the local cache.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

MYSELF_ENDPOINTS_QUERY = "query { myself { endpoints { id name workersMin workersMax } } }"


async def server_endpoints(*, api_key: str | None = None) -> list[dict[str, Any]]:
    """Ground truth: every serverless endpoint on the account (from the server, not local cache)."""
    from runpod_flash.core.api import RunpodGraphQLClient

    client = RunpodGraphQLClient(api_key=api_key)
    try:
        result = await client._execute_graphql(MYSELF_ENDPOINTS_QUERY, {})  # noqa: SLF001
        return (result.get("myself") or {}).get("endpoints") or []
    finally:
        await client.close()


async def delete_endpoint(endpoint_id: str, *, api_key: str | None = None) -> dict[str, Any]:
    """Delete a single endpoint by id."""
    from runpod_flash.core.api import RunpodGraphQLClient

    client = RunpodGraphQLClient(api_key=api_key)
    try:
        return await client.delete_endpoint(endpoint_id)
    finally:
        await client.close()


def _matches(endpoint_name: str, tool_name: str) -> bool:
    """True if a server endpoint name belongs to `tool_name` (tolerating the `-fb` suffix)."""
    return endpoint_name in {tool_name, f"{tool_name}-fb"} or endpoint_name.startswith(tool_name)


def clear_local_cache() -> None:
    """Drop Flash's local deployment cache so a re-mint creates fresh (avoids stale-update).
    Only affects THIS project dir's `.runpod/resources.pkl`."""
    cache = Path(".runpod") / "resources.pkl"
    if cache.exists():
        cache.unlink()


async def undeploy(tool_name: str, *, api_key: str | None = None) -> dict[str, Any]:
    """Delete the endpoint(s) matching `tool_name` from the SERVER, then clear local cache.

    Scoped by name — will never touch an endpoint you didn't mint. Returns what it deleted.
    """
    deleted = []
    for ep in await server_endpoints(api_key=api_key):
        if _matches(ep["name"], tool_name):
            res = await delete_endpoint(ep["id"], api_key=api_key)
            deleted.append({"id": ep["id"], "name": ep["name"], "result": res})
    clear_local_cache()
    return {"tool": tool_name, "deleted": deleted, "count": len(deleted)}


async def undeploy_tools(tool_names: list[str], *, api_key: str | None = None) -> dict[str, Any]:
    """Delete every endpoint matching any name in `tool_names` (the scoped fleet cleanup)."""
    server = await server_endpoints(api_key=api_key)
    deleted = []
    for ep in server:
        if any(_matches(ep["name"], name) for name in tool_names):
            res = await delete_endpoint(ep["id"], api_key=api_key)
            deleted.append({"id": ep["id"], "name": ep["name"], "result": res})
    clear_local_cache()
    return {"deleted": deleted, "count": len(deleted),
            "remaining": [e["name"] for e in await server_endpoints(api_key=api_key)]}
