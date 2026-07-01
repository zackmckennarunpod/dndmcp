# DNDMCP — local dev setup

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

## Pod / container (the "brain" for hosted play) — later
Container runs MCP (HTTP, :8000) + GUI (:8001) together via `python -m dndmcp.app`.
Build amd64 + push (needs `docker login`):
```
docker buildx build --platform linux/amd64 -t zackmckennarunpod/dndmcp:latest --push .
```
Pod connects via `https://{podId}-8000.proxy.runpod.net/mcp`; map at `…-8001.proxy.runpod.net`.
