# Flash Hackathon Kit — FORGE

GPU-on-tap for agents. A pre-built, warm kit for the Runpod Flash hackathon (Jun 30, $10k)
so hack day is **composition, not bootstrap**.

## Layout
| Path | What |
|---|---|
| `KNOWLEDGE.md` | Day-of corpus — verified Flash 1.7.0 API, cold-start doctrine, known bugs, env recipe |
| `STRATEGY.md` | The $10k thesis: killer use cases, meta spin, FORGE target, demo arc |
| `research/` | Deep-dive digests (SDK, TS, ecosystem, prior prep, known issues) + saved official `flash` skill |
| `forge/` | The kit (Python pkg): `env, minting, run, cost, availability, teardown, registry, server, selftest` |
| `flagship/lora_sweep.py` | The Tier-1 "beyond inference" beat — LoRA sweep fanned across workers |
| `.claude/skills/forge/` | Local skill — load it (or `/forge`) to drive the kit with the verified API + gotchas |
| `notes/` | Original strategy (prep-kit, deployment) |
| `dndmcp/` | Separate project: a multiplayer D&D MCP server built on FORGE — see `dndmcp/SETUP.md` |

## Quickstart
```bash
python3 -m venv .venv && .venv/bin/pip install -e .
# Auth: keychain `runpod-api-key-prod` or set RUNPOD_API_KEY in .env (see .env.example)
.venv/bin/python -m forge.selftest      # VALIDATE the spine live: mint→call→cost→teardown
```

```python
import forge; forge.load_env("prod")
tool = forge.mint("hello", gpu="ADA_24", code="def handler(x):\n    import platform\n    return {'host': platform.node(), 'x2': x*2}")
print((await forge.call(tool, 21)).output)
forge.undeploy(tool.endpoint_name)
```

MCP server for agents: `python -m forge.server` (stdio) → `gpu_available, gpu_mint, gpu_call, gpu_fanout, fleet_cost, fleet_cleanup`.

## Also in this repo: DNDMCP

`dndmcp/` is a separate, self-contained project built on top of FORGE — a multiplayer
tabletop-RPG MCP server (FastMCP + FastAPI), with optional Flash-generated rooms/NPCs/art
and a live world map GUI. It's not part of the FORGE kit itself; start at `dndmcp/SETUP.md`
if that's what you're here for. Runs fully locally with zero Runpod account required
(Flash generation is opt-in, off by default); `dndmcp/CLAUDE.md` covers operating the live
hosted pod.

## Status — LIVE-VALIDATED on prod (Jun 26, runpod-flash 1.7.0)
- ✅ Full spine proven end-to-end against the real account: **mint → call → cost → availability → safe teardown**, with zero leaked resources.
- ✅ Availability gap-filler returns **real live stock** (4090=High, A40=High, A100=High/Low). Cold start ~60s, cost ~$0.013/dep-free call.
- ✅ Teardown is **server-truth + scoped by name** — never `flash undeploy --all` (which would kill other endpoints on the account).
- See `KNOWLEDGE.md` → "LIVE-VALIDATED" + the `forge` skill for the full gotcha list (torch-under-dev, all-or-nothing builds, 60s runsync, 10MB payloads, `-fb` suffix, stale local cache).
