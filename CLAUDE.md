# flash-hackathon — project guide for Claude

FORGE: agent-native GPU through Runpod Flash. Read `KNOWLEDGE.md` (verified Flash 1.7.0 API
+ gotchas), `STRATEGY.md`, `JUDGING.md`, `ideas/`. Kit in `forge/`, scripts in `scripts/`, UI in `ui/`.

Venv: `.venv` (runpod-flash 1.7.0 + mcp + fastapi). Auth: keychain `runpod-api-key-prod`.
`import forge; forge.load_env("prod")` wires hosts + key.

## Fetching serverless LOGS + build/worker status locally  ← how to debug a hung endpoint

When a job hangs `inQueue` with no workers, the health api can't say WHY. Use these:

**1. Worker/job status (fast, no aiKey needed)** — `/v2/{id}/health` with the account key:
```python
# returns workers{idle,initializing,ready,running,...} + jobs{inQueue,inProgress,completed,...}
# inQueue>0 with ALL workers 0 (not even initializing) = platform not allocating = capacity/alloc hang.
```

**2. Worker LOGS + build errors (the "why")** — `forge.diagnostics`:
```python
import forge
forge.load_env("prod")
d = await forge.diagnose("<endpoint_id>")   # config + builds(state/error) + pods + recent logs
lg = await forge.logs("<endpoint_id>")      # full worker logs (auto-resolves the endpoint's aiKey)
```
CLI: `python -m forge.diagnostics <endpoint_id>` (status+logs) or `... --logs`.

**How it works (the mechanism the console uses):**
- LOGS api: `GET https://api.runpod.ai/v2/{endpoint_id}/logs?page=0&pageSize=500&from=1970-01-01T00:00:00.000Z&to=<nowISO>`
  with header `Authorization: Bearer <aiKey>`. The **aiKey is PER-ENDPOINT** (not your account key).
- Resolve the aiKey with the `getEndpointFull` GraphQL query (authed with the account key):
  `myself { endpoint(id:$id) { aiKey builds{state error} pods{desiredStatus machine{gpuDisplayName}} } }`.
  `builds[].state/error` catches the all-or-nothing wheel/build failure; `pods[]` shows real workers.
- `forge.diagnostics` does both and never hardcodes a secret (resolves aiKey dynamically).

## Live monitor UI
`MONITOR_LOG=<task.out> FORGE_PROFILE=prod .venv/bin/python -m ui.monitor` → http://localhost:8001.
Shows server-truth endpoints, FORGE-vs-not, per-endpoint worker/job health, derived phase, run log.

## SAFETY / hard-won rules (see KNOWLEDGE.md for the full list)
- Teardown is **server-truth + scoped by name** (`forge.undeploy`, `forge.undeploy_tools`). NEVER
  `flash undeploy --all` — the account has `runpod-coder-v1` + network volumes we must not touch.
- `Endpoint.id` stays None; `flash undeploy list` / `.runpod/resources.pkl` LAG the server — verify
  with `await forge.server_endpoints()`. Clear stale cache with `forge.clear_local_cache()`.
- Cold start is 60s–9min AND can HANG indefinitely (`inQueue`, 0 workers) when the serverless pool
  can't allocate — observed on AMPERE_24/48. **ADA_24 (RTX 4090) has been the reliable GPU.**
- **"In stock" (availability query = gpuTypes stock) ≠ serverless can allocate a worker.** Different pools.
- Network volumes are the user's pre-existing (coding models etc.) — never delete without explicit IDs.
- Don't commit `.runpod/` (plaintext API key) — gitignored.
