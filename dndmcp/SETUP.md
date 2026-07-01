# DNDMCP — install & setup

## Play now — join the shared world (Claude Code)

One command. Installs `dndmcp` as an MCP server pointed at the live, pod-hosted shared world
— you're joining the SAME persistent campaign every other player is in.

```bash
curl -fsSL https://raw.githubusercontent.com/zackmckennarunpod/dndmcp/main/scripts/install_claude_code.sh | bash
```

(Or clone the repo and run `scripts/install_claude_code.sh` directly.) Then restart Claude
Code (or run `/mcp`) and say **"start an adventure."** Watch the shared world map live at
`https://ldghdgi0xxn6jj-8002.proxy.runpod.net`.

You're a ghost passing through: you never see or talk to other players directly, but the
rooms remember — kills, looted items, and notes left by everyone who came through before you
show up as "Traces of those who came before" when you enter a room.

### Claude Desktop

Add this to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS), then restart the app:
```json
{
  "mcpServers": {
    "dndmcp": {
      "type": "http",
      "url": "https://ldghdgi0xxn6jj-8000.proxy.runpod.net/mcp"
    }
  }
}
```

## Local dev setup

Play in your agent harness; watch the world map sync live in the browser. Both share one DB
(`DNDMCP_STATE_DIR`), so the map reflects your moves automatically. No Runpod account needed
for any of this — Flash generation is opt-in (see "Optional env vars" below) and everything
falls back to deterministic procedural content when it's off, which is the default.

**Working from a `bd worktree` to test a fix?** Skip everything below and run
`scripts/dev_worktree.sh` from inside your worktree instead — it auto-picks free ports and an
isolated state dir per worktree, so multiple agents can each run their own server with zero
setup and zero collisions. See `dndmcp/CLAUDE.md` → "Test locally before you push" for details.

Examples below use `<repo>` for wherever you cloned this — `cd` there first and either
substitute the real path or just leave `<repo>` as a relative `.` if you're already in it.

## 0. Install
```bash
cd <repo>
python3 -m venv .venv
.venv/bin/pip install -r dndmcp/requirements.txt
```

## 1. The GUI map
The world map is served at **http://localhost:8001** — open it once it's running. It
auto-refreshes (1.5s) and shows the world graph, your position (`[@]`), visited rooms
(`[#]`), character, and recent log.

Start it with:
```bash
cd <repo>
DNDMCP_STATE_DIR=~/.dndmcp_dev GUI_PORT=8001 .venv/bin/python -m dndmcp.web
```

## 2. Attach the MCP server to Claude Desktop (stdio)
Copy `dndmcp/claude_desktop_config.snippet.json`, replacing every `<ABS_PATH_TO_YOUR_CLONE>`
with the absolute path to `<repo>` (must be absolute — Claude Desktop doesn't resolve `~` or
relative paths), then merge the `mcpServers.dndmcp` block into your Claude Desktop config:
- macOS path: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Keep any existing servers already in that file.
- **Restart Claude Desktop.** It launches the stdio server per session, sharing `~/.dndmcp_dev`.

Then in Claude Desktop: the `dndmcp` tools appear, and the DM persona (server instructions)
makes the agent act as your Dungeon Master. Say "start an adventure" and play — watch the map
at :8001 update as you move.

## 3. Attach from a terminal harness (Claude Code etc.)
Same stdio command, with `<repo>` replaced by its absolute path:
```
PYTHONPATH=<repo> DNDMCP_STATE_DIR=~/.dndmcp_dev \
  <repo>/.venv/bin/python -m dndmcp.server
```

## 4. Dev loop
- Edit `dndmcp/*.py` → restart the harness's MCP connection (or Claude Desktop) to reload.
  stdio MCP servers are long-running processes, not re-read per call — a code edit does
  nothing until the client reconnects (Claude Code: `/mcp`; Claude Desktop: restart the app).
- The GUI picks up DB changes live; no restart needed for play.
- The world is shared/multiplayer: `start_adventure` joins the existing campaign if one is
  already running (does NOT wipe it) — a new player just gets their own character in it.
  To actually reset: `rm ~/.dndmcp_dev/campaign.db`.

## Optional env vars
None of these are required — everything works locally with just the install step above.
| Var | Default | What |
|---|---|---|
| `DND_FLASH_LLM` | unset (off) | `1` to generate rooms/NPCs/items via Flash instead of the built-in procedural pool. Needs `RUNPOD_API_KEY`. |
| `DND_FLASH_ART` | unset (off) | `1` to generate ANSI-rendered room art via Flash. Needs `RUNPOD_API_KEY` + Pillow (`pip install Pillow`). |
| `DND_LLM_MODEL` | see `flash_llm.py` | Override the model Flash calls for room/NPC generation. |
| `FLASH_NPC` / `FLASH_NPC_ENDPOINT_ID` | unset | Route `ask_npc` dialogue through a specific Flash endpoint instead of the shared pool. |
| `RUNPOD_API_KEY` | unset | Required by any `DND_FLASH_*` flag above. On macOS, falls back to Keychain entry `runpod-api-key-prod` if unset — that fallback is a convenience for the original author's machine, not something to rely on elsewhere; just export the var. |
| `DNDMCP_STATE_DIR` | `~/.dndmcp` | Where the SQLite world DB lives. |
| `GUI_PORT` | `8001` | Port `dndmcp.web` binds to. |
| `PORT` | `8000` | Port the MCP server binds to (HTTP transports only). |
| `DNDMCP_TRANSPORT` | `stdio` | `http` to run the MCP server over streamable-HTTP instead of stdio (what the pod/container uses). |

## Pod / container (the "brain" for hosted play) — live

The container runs MCP (HTTP, :8000) + GUI (:8002) together via `python -m dndmcp.app`, on a
Runpod CPU pod with a network volume mounted at `/data` (persists across restarts — the whole
point of a shared world). Currently live at pod `ldghdgi0xxn6jj` (EU-RO-1).

Iterating on the live pod (see `scripts/`):
```bash
scripts/redeploy_pod.sh          # git pull + restart the app
scripts/pod_status.sh            # is it up? what commit?
scripts/pod_logs.sh [n]          # tail the app log
scripts/reset_world.sh --yes     # DESTRUCTIVE: wipes the shared world, fresh start
```
All auto-resolve the pod's current SSH endpoint via the Runpod API (the direct-TCP port
changes across restarts, so nothing is hardcoded).

## Deploy your own instance (self-contained)

You don't need access to the live pod above to run dndmcp — the image is public
(`zackmckennarunpod/dndmcp` on Docker Hub) and requires no secrets baked in, just your own
Runpod API key. This gets you a completely independent world, not a copy of anyone else's.

**Prerequisites:** a Runpod account + API key (Runpod console → Settings → API Keys).

```bash
RUNPOD_API_KEY=... scripts/deploy_own_pod.sh [name] [datacenter]   # creates volume + pod
scripts/install_claude_code.sh <pod-id>                            # connect Claude Code to it
scripts/destroy_own_pod.sh <pod-id> --yes                          # tear it down when done
```

`deploy_own_pod.sh` creates a small network volume and a CPU pod (`cpu3c-2-4`, default
datacenter `EU-RO-1`) running the published image, and prints the MCP/GUI URLs once it's up
(give it ~30-60s to pull the image and boot). This costs real money while the pod is
running — check current Runpod CPU pricing — so tear it down with `destroy_own_pod.sh` when
you're done; it also deletes the network volume by default (`--keep-volume` to keep it).
`destroy_own_pod.sh` refuses to ever target the live shared pod (`ldghdgi0xxn6jj`), by design.

Want Flash-generated rooms/NPCs instead of the procedural fallback? Also set
`DND_FLASH_LLM=1` and pass your key through as a pod env var — see `redeploy_pod.sh` for the
exact pattern (`RUNPOD_API_KEY` has to travel with the pod itself, since Flash calls happen
from inside the container).
