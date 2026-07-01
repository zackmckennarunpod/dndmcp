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
(`DNDMCP_STATE_DIR`), so the map reflects your moves automatically.

## 1. The GUI map (running now)
The world map is served at **http://localhost:8001** — open it. It auto-refreshes (1.5s) and
shows the world graph, your position (`[@]`), visited rooms (`[#]`), character, and recent log.

Start/restart the GUI manually with:
```bash
cd ~/Developer/work/flash-hackathon
DNDMCP_STATE_DIR=~/.dndmcp_dev GUI_PORT=8001 .venv/bin/python -m dndmcp.web
```

## 2. Attach the MCP server to Claude Desktop (stdio)
Add the contents of `dndmcp/claude_desktop_config.snippet.json` to your Claude Desktop config:
- macOS path: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Merge the `mcpServers.dndmcp` block in (keep any existing servers).
- **Restart Claude Desktop.** It launches the stdio server per session, sharing `~/.dndmcp_dev`.

Then in Claude Desktop: the `dndmcp` tools appear, and the DM persona (server instructions)
makes the agent act as your Dungeon Master. Say "start an adventure" and play — watch the map
at :8001 update as you move.

## 3. Attach from a terminal harness (Claude Code etc.)
Same stdio command:
```
PYTHONPATH=~/Developer/work/flash-hackathon DNDMCP_STATE_DIR=~/.dndmcp_dev \
  ~/Developer/work/flash-hackathon/.venv/bin/python -m dndmcp.server
```

## 4. Dev loop
- Edit `dndmcp/*.py` → restart the harness's MCP connection (or Claude Desktop) to reload.
  stdio MCP servers are long-running processes, not re-read per call — a code edit does
  nothing until the client reconnects (Claude Code: `/mcp`; Claude Desktop: restart the app).
- The GUI picks up DB changes live; no restart needed for play.
- The world is shared/multiplayer: `start_adventure` joins the existing campaign if one is
  already running (does NOT wipe it) — a new player just gets their own character in it.
  To actually reset: `rm ~/.dndmcp_dev/campaign.db`.

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
