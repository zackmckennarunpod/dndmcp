"""Serverless diagnostics — logs + build status + worker/pod state for an endpoint.

The missing piece: when a job hangs `inQueue` with no workers, the HEALTH api can't say
WHY. `getEndpointFull` exposes `builds` (state + error — catches the all-or-nothing wheel
failure) and `pods` (real worker/machine state), and `/v2/{id}/logs` streams the worker log.

Auth: the LOGS api uses the endpoint's per-endpoint `aiKey` (not your account key). We
resolve that aiKey dynamically via `getEndpointFull` (authed with the account key), so no
secret is ever hardcoded.

    python -m forge.diagnostics <endpoint_id>          # status + builds + last log lines
    python -m forge.diagnostics <endpoint_id> --logs   # full logs only
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.request

from . import env

# Trimmed getEndpointFull — only the diagnostic fields (full query has ~80 fields).
ENDPOINT_FULL_QUERY = """
query getEndpointFull($id: String!) {
  myself {
    endpoint(id: $id) {
      id name aiKey gpuIds type idleTimeout locations
      workersMin workersMax networkVolumeId flashBootType
      builds { id state error startedAt completedAt imageName }
      pods {
        id desiredStatus costPerHr uptimeSeconds lastStartedAt slsVersion
        machine { gpuDisplayName gpuTypeId dataCenterId }
      }
    }
  }
}
"""


async def endpoint_full(endpoint_id: str, *, api_key: str | None = None) -> dict:
    """Full endpoint record incl. aiKey, builds (state/error), pods (worker state)."""
    from runpod_flash.core.api import RunpodGraphQLClient

    client = RunpodGraphQLClient(api_key=api_key)
    try:
        r = await client._execute_graphql(ENDPOINT_FULL_QUERY, {"id": endpoint_id})  # noqa: SLF001
        return ((r.get("myself") or {}).get("endpoint")) or {}
    finally:
        await client.close()


def fetch_logs(endpoint_id: str, ai_key: str, *, page: int = 0, page_size: int = 500) -> dict:
    """Raw worker logs for an endpoint via /v2/{id}/logs (auth = the endpoint's aiKey)."""
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    url = (f"https://api.runpod.ai/v2/{endpoint_id}/logs"
           f"?page={page}&pageSize={page_size}&from=1970-01-01T00:00:00.000Z&to={now}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ai_key}",
                                               "accept": "*/*"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


async def logs(endpoint_id: str, *, api_key: str | None = None, ai_key: str | None = None,
               page_size: int = 500) -> dict:
    """Logs for an endpoint, auto-resolving its aiKey if not provided."""
    if ai_key is None:
        full = await endpoint_full(endpoint_id, api_key=api_key)
        ai_key = full.get("aiKey")
        if not ai_key:
            return {"error": f"could not resolve aiKey for {endpoint_id}"}
    return fetch_logs(endpoint_id, ai_key, page_size=page_size)


async def diagnose(endpoint_id: str, *, api_key: str | None = None) -> dict:
    """One-shot: config + build state + pods + recent logs. The 'why is it hung' call."""
    full = await endpoint_full(endpoint_id, api_key=api_key)
    if not full:
        return {"error": f"endpoint {endpoint_id} not found"}
    log_data = {}
    if full.get("aiKey"):
        try:
            log_data = fetch_logs(endpoint_id, full["aiKey"], page_size=200)
        except Exception as exc:
            log_data = {"error": f"log fetch failed: {exc}"}
    return {
        "name": full.get("name"), "gpu": full.get("gpuIds"), "type": full.get("type"),
        "workers": f"{full.get('workersMin')}/{full.get('workersMax')}",
        "flashBoot": full.get("flashBootType"), "volume": full.get("networkVolumeId"),
        "builds": full.get("builds") or [], "pods": full.get("pods") or [],
        "logs": log_data,
    }


def _print(d: dict) -> None:
    if "error" in d:
        print("ERROR:", d["error"]); return
    print(f"endpoint: {d['name']}  gpu={d['gpu']} type={d['type']} workers={d['workers']} "
          f"flashBoot={d['flashBoot']} volume={d['volume']}")
    builds = d["builds"]
    print(f"\nbuilds ({len(builds)}):")
    for b in builds[:5]:
        print(f"  state={b.get('state')}  error={b.get('error')}  image={b.get('imageName')}")
    pods = d["pods"]
    print(f"\npods/workers ({len(pods)}):")
    for p in pods[:6]:
        m = p.get("machine") or {}
        print(f"  status={p.get('desiredStatus')}  gpu={m.get('gpuDisplayName')} "
              f"${p.get('costPerHr')}/hr  up={p.get('uptimeSeconds')}s  dc={m.get('dataCenterId')}")
    log = d["logs"]
    lines = log.get("logs") if isinstance(log, dict) else None
    print(f"\nlogs:")
    if isinstance(lines, list):
        for ln in lines[-25:]:
            msg = ln.get("message", ln) if isinstance(ln, dict) else ln
            print(f"  {msg}")
    else:
        print(f"  {log}")


async def _main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m forge.diagnostics <endpoint_id> [--logs]"); return 1
    env.load_env(os.environ.get("FORGE_PROFILE", "prod"))
    eid = sys.argv[1]
    if "--logs" in sys.argv:
        _print({"name": eid, "gpu": "", "type": "", "workers": "", "flashBoot": "",
                "volume": "", "builds": [], "pods": [], "logs": await logs(eid)})
    else:
        _print(await diagnose(eid))
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(_main()))
