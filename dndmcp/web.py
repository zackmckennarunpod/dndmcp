"""DNDMCP web GUI — a live map of the world, synced to the game.

Reads the SAME SQLite the MCP server writes, so it auto-syncs: as the player moves (via MCP
tools), the DB updates and this map reflects it on the next poll. Served by the pod brain
alongside the MCP server. Shows the world graph (rooms placed by their path), current
position, character, and the log.
"""

from __future__ import annotations

import asyncio
import datetime
import html
import json
import os
import subprocess
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sse_starlette.sse import EventSourceResponse

from . import worldgen

app = FastAPI(title="DNDMCP map")


def _server_version() -> str:
    """Deployed commit, computed once at process start. A browser tab only fetches fresh
    DATA via polling — it never reloads the page's own JS on its own — so a long-lived tab
    left open across a redeploy silently keeps running stale rendering code against
    whatever new shape /state now returns. Exposing this lets the client detect "the server
    moved on since I loaded" and prompt a refresh instead of failing confusingly (e.g.
    rendering only some of the graph's nodes)."""
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
                              capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except Exception:
        return "unknown"


SERVER_VERSION = _server_version()


def _db() -> sqlite3.Connection:
    state_dir = os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp"))
    c = sqlite3.connect(str(Path(state_dir) / "campaign.db"))
    c.row_factory = sqlite3.Row
    return c


def _client_ip(request: Request) -> str | None:
    """Same X-Forwarded-For-first resolution as server.py's _RequestContextMiddleware — the
    pod sits behind Runpod's proxy, so request.client.host alone would just be the proxy."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>DNDMCP — map</title>
<link rel=preconnect href=https://fonts.googleapis.com>
<link rel=preconnect href=https://fonts.gstatic.com crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel=stylesheet>
<style>
/* The Sundered Weave: a dead civilization's arcane-tech collapse, now ruins and ghosts. Cool
   violet-black instead of the generic dark-dev-tool blue, a ghostly teal for "you are here"
   (you're a ghost too — see the "how this works" panel), and a stone-inscription serif for
   the title only, so it reads like a marker cut into old rock rather than a devtool banner. */
:root{
  --bg:#0a0713; --panel:#150f24; --border:#2b2145; --border-soft:#221a38;
  --text:#e7e1f5; --muted:#8d7fae; --dim:#8a7fa8;
  --warm:#e8b339; --warm-bright:#f5cc66; /* embers/Flash calls — one warm accent in a cool world */
  --ghost:#4fd8c4; --ghost-bright:#8ff0e0; /* current room / "you" */
  --visited:#8072e0; --unvisited:#1c1630; /* explored vs fog-of-war */
  --link:#3c3160;
}
 body{margin:0;background:var(--bg);color:var(--text);font:13px 'IBM Plex Mono',ui-monospace,Menlo,monospace}
 header{padding:12px 18px;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:baseline;
   background:linear-gradient(180deg,#120b21,transparent)}
 h1{font:600 16px 'Cinzel',serif;letter-spacing:1.5px;margin:0;color:var(--ghost-bright);
   text-shadow:0 0 12px rgba(79,216,196,.35)}
 .sub{color:var(--muted);font-size:12px}
 /* 3 columns: map (flexible width) | live stream (own space, not buried below the fold) | the
    existing character/room/recent sidebar. */
 main{display:grid;grid-template-columns:1fr 340px 280px;gap:16px;padding:16px 18px}
 .panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}
 .panel h2{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted);margin:0 0 10px}
 /* Fixed (not %/flex-stretched) heights on purpose: a height that depends on the grid row,
    which depends on the SVG's own intrinsic ratio (see #map), is a circular layout — the
    browser "resolves" it by growing #map without bound every time the ResizeObserver below
    reacts to its own previous resize. Width is still fully responsive (that was the actual
    "map doesn't fit the box" bug); only height is pinned to a plain number now. */
 #map{width:100%;height:560px;overflow:hidden;position:relative;
   background:radial-gradient(ellipse at 50% 40%,#1a1330 0%,var(--panel) 70%)}
 #nodeTooltip{position:absolute;pointer-events:none;background:#1c1433;border:1px solid var(--link);
   border-radius:6px;padding:4px 9px;font-size:12px;color:var(--text);display:none;z-index:10;
   box-shadow:0 4px 16px rgba(0,0,0,.5)}
 /* Same "custom div, not a native tooltip" reasoning as #nodeTooltip (native title/SVG-title
    tooltips are slow/inconsistent across browsers) — position:fixed so it works anywhere on
    the page, not just inside #map's own coordinate space. */
 #itemTooltip{position:fixed;pointer-events:none;background:#1c1433;border:1px solid var(--link);
   border-radius:6px;padding:6px 10px;font-size:12px;color:var(--text);display:none;z-index:50;
   box-shadow:0 4px 16px rgba(0,0,0,.5);max-width:240px}
 .ch b{color:var(--ghost-bright)}.ch span{color:var(--muted)}
 .ch span.item{cursor:help;border-bottom:1px dotted var(--dim)}
 .log div{color:var(--muted);padding:2px 0;border-bottom:1px solid var(--border-soft);font-size:12px}
 .empty{color:var(--dim)}
 /* Known-entity highlighting in free-text log/stream lines (see highlightKnown()) — one
    color per category so "who / where / what" is readable at a glance, not one flat gray
    sentence. Reuses the same palette meaning the graph already established (violet=rooms,
    teal=ghosts/actors) rather than inventing a fourth unrelated color language. */
 .hl-room{color:var(--visited);font-weight:600}
 .hl-actor{color:var(--ghost);font-weight:600}
 .hl-item{color:var(--warm);font-weight:600}
#flashcount{color:var(--warm);font-weight:600;margin-left:auto;transition:transform .15s}
#flashcount.pulse{transform:scale(1.3);color:var(--warm-bright)}
#metricsLink{color:var(--ghost);cursor:pointer;font-weight:600}
#metricsLink:hover{color:var(--ghost-bright)}
#staleBanner{display:none;background:var(--warm);color:#1a1206;font-weight:600;font-size:12.5px;
  padding:7px 18px;text-align:center}
#staleBanner a{color:#1a1206;text-decoration:underline}
#shareBtn{background:var(--link);color:var(--ghost-bright);border:1px solid var(--border);
  border-radius:6px;padding:5px 11px;font:600 12px 'IBM Plex Mono',monospace;cursor:pointer;
  transition:background .15s}
#shareBtn:hover{background:var(--visited)}
#shareBtn.copied{background:var(--ghost);color:var(--bg)}
#exportStoryBtn{display:none;background:var(--link);color:var(--ghost-bright);border:1px solid var(--border);
  border-radius:6px;padding:7px 11px;font:600 12px 'IBM Plex Mono',monospace;cursor:pointer;
  transition:background .15s}
#exportStoryBtn:hover{background:var(--visited)}
#exportStoryBtn:disabled{opacity:.6;cursor:default}
#streamFilterBtn{background:transparent;color:var(--muted);border:1px solid var(--border);
  border-radius:5px;padding:2px 9px;font:600 10.5px 'IBM Plex Mono',monospace;cursor:pointer;
  text-transform:none;letter-spacing:0}
#streamFilterBtn.active{background:var(--warm);color:#1a1206;border-color:var(--warm)}
 /* Was a fixed 120px when this lived as a thin strip below everything else — now it's a full
    column next to the map, sized to match (see #map's height). */
 #stream{display:flex;flex-direction:column-reverse;gap:0;height:560px;overflow-y:auto}
 #stream div{color:var(--muted);padding:2px 0;border-bottom:1px solid var(--border-soft);font-size:12px}
 #stream div.new{animation:flash .8s ease-out}
 #stream .who{color:var(--warm)}
 @keyframes flash{from{background:#4fd8c433}to{background:transparent}}
 #streamDot{width:8px;height:8px;border-radius:50%;background:var(--ghost);display:inline-block;
   margin-right:6px;box-shadow:0 0 6px var(--ghost)}
 details.panel{margin:0 18px 16px;cursor:default}
 details.panel summary{cursor:pointer;font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
   color:var(--muted);list-style:none;display:flex;align-items:center;gap:6px}
 details.panel summary::-webkit-details-marker{display:none}
 details.panel summary::before{content:'▸';transition:transform .15s}
 details.panel[open] summary::before{content:'▾'}
 /* .body used to only ever live inside a <details>, hence the old "details.panel .body"
    ancestor-scoped selectors — now also used inside tab panes (.tabBody), so scoped to the
    class alone. */
 .body{margin-top:10px;color:var(--muted);line-height:1.6;font-size:12.5px}
 .body b{color:var(--ghost-bright)}
 .body a{color:var(--ghost-bright);text-decoration:underline;text-decoration-color:var(--ghost)}
 .body p{margin:0 0 10px}
 /* Tab group (Connect & play / Browse other worlds / How this works) — these are mutually
    exclusive MODES a visitor picks, not things worth having open side-by-side, unlike the
    always-visible "This world" status strip or the collapsible-but-independent sidebar
    cards below. */
 .tabbar{display:flex;gap:2px;border-bottom:1px solid var(--border);margin-bottom:2px}
 .tabBtn{background:none;border:none;color:var(--muted);font-size:11px;text-transform:uppercase;
   letter-spacing:1.5px;padding:8px 12px 9px;cursor:pointer;border-bottom:2px solid transparent;
   font-family:'IBM Plex Mono',monospace;transition:color .15s,border-color .15s}
 .tabBtn:hover{color:var(--text)}
 .tabBtn.active{color:var(--ghost-bright);border-bottom-color:var(--ghost)}
 .tabBody{display:none}
 .tabBody.active{display:block}
 .world-card{display:block;background:var(--bg);border:1px solid var(--border-soft);border-radius:6px;
   padding:9px 11px;margin-bottom:6px;text-decoration:none;cursor:pointer}
 .world-card:hover{border-color:var(--ghost)}
 .world-card .wc-theme{color:var(--ghost-bright);font-weight:600;font-size:12.5px}
 .world-card .wc-meta{color:var(--muted);font-size:11px;float:right}
 .world-card .wc-premise{color:var(--muted);font-size:11.5px;margin-top:3px;
   display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 .body code{background:var(--unvisited);padding:1px 5px;border-radius:4px;font-size:11.5px}
 .codebox{display:flex;align-items:flex-start;gap:8px;background:#0d0819;border:1px solid var(--border);
   border-radius:6px;padding:9px 11px;margin:4px 0}
 .codebox code,.codebox pre{flex:1;background:none;padding:0;font:12px 'IBM Plex Mono',monospace;
   color:var(--ghost-bright);white-space:pre-wrap;word-break:break-all;margin:0}
 .copyCodeBtn{flex-shrink:0;background:var(--link);color:var(--text);border:1px solid var(--border);
   border-radius:5px;padding:4px 10px;font:600 11px 'IBM Plex Mono',monospace;cursor:pointer}
 .copyCodeBtn:hover{background:var(--visited)}
 .copyCodeBtn.copied{background:var(--ghost);color:var(--bg)}
</style></head><body>
<div id=staleBanner>⟳ This tab is running an older version of the page — <a href="#" onclick="location.reload();return false">refresh to update</a></div>
<div id=itemTooltip></div>
<header><h1>⚔ DNDMCP</h1><span class=sub id=where>—</span>
 <span id=flashcount>⚡ 0 Flash calls</span>
 <span id=metricsLink title="Click to see system-wide metrics for this world">📊 Metrics</span>
 <button id=shareBtn title="Copies instructions to paste into your agent (Claude Code/Desktop) running dndmcp">🔗 Share</button></header>
<!-- A bare command means nothing if you don't already know what an MCP server is — so this
     is a real dropdown with the full "what do I actually do" explanation, not a copy-paste
     strip, and it lives right under the header instead of buried below the map. -->
<details class=panel id=connectDetails style="margin:16px 18px 0 18px">
 <summary>🎲 How to connect &amp; play</summary>
 <div class=body style="margin-top:10px">
  <p><b>1. Connect your agent</b> — pick whichever you use:</p>
  <div class=codebox><code id=codeCC>curl -fsSL https://ldghdgi0xxn6jj-8002.proxy.runpod.net/install.sh | bash</code><button class=copyCodeBtn data-target=codeCC>Copy</button></div>
  <p class=sub style="margin:6px 0 14px">↑ <b>Claude Code</b> — one command, installs dndmcp pointed at this exact live shared world.</p>
  <div class=codebox><pre id=codeCD>{
  "mcpServers": {
    "dndmcp": {
      "type": "http",
      "url": "https://ldghdgi0xxn6jj-8000.proxy.runpod.net/mcp"
    }
  }
}</pre><button class=copyCodeBtn data-target=codeCD>Copy</button></div>
  <p class=sub style="margin:6px 0 14px">↑ <b>Claude Desktop</b> — paste into <code>claude_desktop_config.json</code>
  (macOS: <code>~/Library/Application Support/Claude/claude_desktop_config.json</code>), then restart the app.</p>
  <p><b>2. Reconnect</b> — Claude Code: run <code>/mcp</code>; Claude Desktop: restart it — so it picks up the new server.</p>
  <p><b>3. Say "start an adventure."</b> That's it. Your agent becomes the Dungeon Master — talk
  naturally ("go through the door," "attack it," "look around"), you never need game-engine
  syntax. You're joining THIS shared world, live, with everyone else currently playing.</p>
 </div>
</details>
<main style="margin-top:16px">
 <div class=panel><h2>World map (shared, live)</h2><div class=sub id=whereInMap style="margin-bottom:8px">—</div><div id=map><span id=mapEmpty class=empty>no adventure yet — start one in your agent</span><div id=nodeTooltip></div></div></div>
 <div class=panel><h2><span id=streamDot></span><span id=streamTitle>Live world stream</span></h2>
   <div class=sub style="margin-bottom:8px"><span id=streamSub>every player, every session</span>
   <button id=streamFilterBtn style="margin-left:6px">⚡ Flash calls only</button></div>
   <div id=stream><div class=empty>waiting for the world to move...</div></div></div>
 <aside style="display:flex;flex-direction:column;gap:16px">
  <details open class=panel>
   <summary>Character</summary>
   <div class=body style="margin-top:6px">
    <div class=ch id=char>—</div>
    <button id=exportStoryBtn style="margin-top:10px;width:100%">📜 Export story</button>
    <!-- exportStoryBtn is display:none by default (see CSS) — only shown once JS confirms
         ?player= is actually present, since without it the button can't do anything. -->
   </div>
  </details>
  <details open class=panel>
   <summary>Selected room</summary>
   <div class=body style="margin-top:6px"><div class=ch id=roomInfo><span class=empty>click a room on the map</span></div></div>
  </details>
  <details open class=panel>
   <summary>Recent</summary>
   <div class=body style="margin-top:6px"><div class=log id=log></div></div>
  </details>
 </aside>
</main>
<!-- Moved below the map on purpose: this used to sit above it, pushing the map (the actual
     game) below a scroll on a cold homepage visit. This info is worth having, just not
     worth blocking the primary content for. -->
<details class=panel style="margin:16px 18px 16px">
 <summary>This world</summary>
 <div class=ch id=worldInfo style="margin-top:10px">—</div>
 <div class=ch id=questList style="margin-top:10px">—</div>
</details>
<div class=panel style="margin:0 18px 16px">
 <div class=tabbar>
  <button class=tabBtn data-tab=browse>🌍 Browse other worlds</button>
  <button class=tabBtn data-tab=howworks>❔ How this works</button>
 </div>
 <div class="tabBody body" id=tab-browse>
  <input id=worldSearch placeholder="Search by theme or premise..."
    style="width:100%;box-sizing:border-box;background:var(--bg);border:1px solid var(--border);
    border-radius:6px;padding:7px 10px;color:var(--text);font:12.5px 'IBM Plex Mono',monospace;margin-bottom:10px">
  <div id=worldsList><div class=empty>Loading worlds…</div></div>
 </div>
 <div class="tabBody body" id=tab-howworks>
  <p>Under the hood this isn't a D&amp;D-specific engine — it's a generic graph: every room,
  item, and NPC is a <b>node</b>, connections between them (an exit, ownership, a relationship)
  are typed <b>edges</b>, and everything that happens is an append-only <b>event log</b>. We
  call that substrate the <b>Graph Context Engine</b> — the D&amp;D adventure you're watching
  is one skin on it — the same server also runs a Linear-style task graph (nodes = tickets,
  edges = links, same event log) with zero changes to the underlying mechanics.</p>
  <p>That pattern isn't limited to games or tickets — it's the same substrate any long-running
  <b>agent workflow</b> needs: nodes for subtasks or artifacts, edges for dependencies between
  them, the event log for what's already been tried or decided. It's what lets an agent (or a
  human) step away mid-task and pick up real context hours or days later instead of restarting
  from a blank prompt — the same way this world remembers a player's choices between sessions.</p>
  <p>When new content is needed — the next room, an NPC's response, the next step of a task —
  the LLM is never generating in isolation. It's fed the <b>surrounding graph context</b>:
  nearby already-generated nodes a couple hops out, and recent events near this spot, so
  whatever it invents stays consistent with what's already real instead of contradicting it.
  That's the "world stream" below and the map itself: both are live views of that same graph.
  It's a <a href="https://en.wikipedia.org/wiki/Stigmergy" target="_blank" rel="noopener"><b>stigmergic</b></a>
  system in the literal sense — coordination through traces left in the shared graph, not
  direct messages between whoever's generating content — the same mechanism ants use to
  build a colony without a blueprint or a foreman.</p>
  <p>That's also the literal model for other players in this world: you never see or talk to
  them directly — they're <b>ghosts</b>, visible only as a dot moving across this same map in
  real time. The only way you affect each other is through the graph itself: drop something in
  a room and it's really there — the next ghost to pass through can pick it up, same as any
  other trace.</p>
 </div>
</div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const params = new URLSearchParams(location.search);
const playerId = params.get('player');
const campaignId = params.get('campaign') || 'main';
// W/H used to be hardcoded to 700x420, so on any screen wider than that the map's viewBox
// only ever used a small fixed chunk of the actual #map box — the rest sat empty, and
// fitToView's own math (bounded by that same stale W/H) could place nodes outside the box
// entirely, which #map's overflow:hidden then silently clipped. Reading the real box size
// (and re-reading it via ResizeObserver below) keeps the coordinate space honest.
const mapEl = document.getElementById('map');
function mapSize(){
  return {w: mapEl.clientWidth || 700, h: mapEl.clientHeight || 420};
}
let {w: W, h: H} = mapSize();

if (playerId) {
  document.getElementById('exportStoryBtn').style.display = 'block';
}

// Connect / Browse / How-it-works tab group — mutually exclusive modes, not simultaneous
// panels (see the CSS comment above .tabbar). Clicking a tab is always available regardless
// of default state.
function showTab(name){
  document.querySelectorAll('.tabBtn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tabBody').forEach(el => el.classList.toggle('active', el.id === 'tab-' + name));
}
document.querySelectorAll('.tabBtn').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
// Only the bare, cold "main" link (no ?player=, the world everyone lands on by default)
// should open with the connect dropdown already expanded. A link into a SPECIFIC shared
// world means someone was already invited there — the generic "anyone can join" pitch is
// noise, and an already-playing character doesn't need it either.
if (campaignId === 'main' && !playerId) {
  document.getElementById('connectDetails').open = true;
}

// Browse other worlds: campaign ids are opaque hex strings — nobody finds a world they don't
// already have a link to without this. Fetched once (worlds don't churn fast enough to need
// the 1.5s tick's polling cadence); search filters the already-fetched list client-side.
let allWorlds = [];
function timeAgo(ts){
  if(!ts) return 'no activity yet';
  const mins = Math.floor((Date.now()/1000 - ts) / 60);
  if(mins < 1) return 'just now';
  if(mins < 60) return mins + 'm ago';
  if(mins < 1440) return Math.floor(mins/60) + 'h ago';
  return Math.floor(mins/1440) + 'd ago';
}
function renderWorlds(filter){
  const q = (filter||'').toLowerCase();
  const shown = allWorlds.filter(w =>
    !q || (w.theme||'').toLowerCase().includes(q) || (w.premise||'').toLowerCase().includes(q));
  const listEl = document.getElementById('worldsList');
  if(!shown.length){ listEl.innerHTML = '<div class=empty>No worlds match.</div>'; return; }
  listEl.innerHTML = shown.map(w => {
    const isMain = w.id === 'main';
    const href = '/?campaign=' + encodeURIComponent(w.id);
    const label = isMain ? 'main (default shared world)' : w.id;
    return `<a class=world-card href="${href}">`
      + `<span class=wc-meta>${w.players} player${w.players===1?'':'s'} · ${esc(timeAgo(w.last_activity))}</span>`
      + `<span class=wc-theme>${esc(w.theme||'untitled')}</span> <span class=sub>(${esc(label)})</span>`
      + `<div class=wc-premise>${esc(w.premise||'')}</div></a>`;
  }).join('');
}
fetch('/worlds').then(r => r.json()).then(worlds => {
  allWorlds = worlds;
  renderWorlds('');
}).catch(() => {
  document.getElementById('worldsList').innerHTML = '<div class=empty>Could not load worlds.</div>';
});
document.getElementById('worldSearch').addEventListener('input', (e) => renderWorlds(e.target.value));

// Share: copies join instructions, not just a URL — actually PLAYING requires the friend's
// Connect & play panel: generic copy-to-clipboard for the install command / Desktop config,
// same copied/reverts-after-a-beat pattern as the Share button.
document.querySelectorAll('.copyCodeBtn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const targetEl = document.getElementById(btn.dataset.target);
    const text = targetEl.textContent;
    try{ await navigator.clipboard.writeText(text); }
    catch(e){ prompt('Copy this:', text); return; }
    const original = btn.textContent;
    btn.textContent = '✓ Copied';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = original; btn.classList.remove('copied'); }, 1500);
  });
});

// Export story: /export_story runs this player's real event timeline through Flash to
// synthesize a markdown narrative — can take a while (LLM call, possibly a cold start), so
// the button shows real progress instead of looking hung.
document.getElementById('exportStoryBtn').addEventListener('click', async () => {
  const btn = document.getElementById('exportStoryBtn');
  if (!playerId) { alert('Open this page with ?player=<your id> in the URL to export your own story.'); return; }
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '📜 Writing your story… (can take up to a minute)';
  try{
    const r = await fetch('/export_story?campaign='+encodeURIComponent(campaignId)+'&player='+encodeURIComponent(playerId));
    if(!r.ok){ const err = await r.json().catch(()=>({})); throw new Error(err.error || 'export failed'); }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'dndmcp-story.md';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }catch(e){
    alert('Could not export the story: ' + e.message);
  }finally{
    btn.disabled = false;
    btn.textContent = original;
  }
});

// own agent to call start_adventure(campaign_id=...), this GUI is spectator-only. "main"
// needs no id (the default world), so its share text skips campaign_id entirely. The text
// itself tells them WHERE to paste it (their agent, not a browser) — a bare link/id here
// reads ambiguous ("is this a website?"); explicit instructions don't.
document.getElementById('shareBtn').addEventListener('click', async () => {
  const watchUrl = location.origin + location.pathname + '?campaign=' + encodeURIComponent(campaignId);
  const joinLine = campaignId === 'main'
    ? `just start_adventure (no id needed, it's the shared default)`
    : `start_adventure with campaign_id="${campaignId}"`;
  const text = `Paste this into your agent (Claude Code/Desktop) while it's connected to `
    + `the dndmcp MCP server, to join my world: ${joinLine}. Watch it live: ${watchUrl}`;
  try{
    await navigator.clipboard.writeText(text);
    const btn = document.getElementById('shareBtn');
    const original = btn.textContent;
    btn.textContent = '✓ Copied — paste into your agent';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = original; btn.classList.remove('copied'); }, 2200);
  }catch(e){
    prompt('Copy this, then paste it into your agent (connected to dndmcp):', text);
  }
});
function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escRegex(s){ return s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'); }

// Highlight known entity names (rooms/actors/items) wherever they show up in free-text log
// lines, so "Corrin Vale moved south into Dreadful Descent" reads at a glance instead of as
// one flat gray sentence. Accumulates across the whole session (never resets) so older log
// lines mentioning a room/monster that's since left the current snapshot stay highlighted.
const highlightClassOf = {};
let highlightTerms = [];
let highlightRegex = null;
function noteHighlightTerm(name, cls){
  const key = name && String(name).trim();
  if(!key || highlightClassOf[key]) return;
  highlightClassOf[key] = cls;
  highlightTerms.push(key);
}
function rebuildHighlightIndex(state){
  const before = highlightTerms.length;
  (state.rooms||[]).forEach(r => noteHighlightTerm(r.name, 'hl-room'));
  (state.players||[]).forEach(p => noteHighlightTerm(p.name, 'hl-actor'));
  (state.rooms||[]).forEach(r => (r.contents||[]).forEach(c =>
    noteHighlightTerm(c.name, c.type==='monster' ? 'hl-actor' : 'hl-item')));
  if(highlightTerms.length === before) return;  // nothing new -> keep the existing regex
  // Longest-first so "Corrin Vale" matches whole, not just the "Corrin" substring of it.
  const sorted = [...highlightTerms].sort((a,b) => b.length - a.length);
  highlightRegex = new RegExp('(' + sorted.map(escRegex).join('|') + ')', 'g');
}
// Text passed in must ALREADY be HTML-escaped (esc()) -- this only wraps matches in spans,
// it never introduces new unescaped content of its own.
function highlightKnown(escapedText){
  if(!highlightRegex) return escapedText;
  return escapedText.replace(highlightRegex, m => `<span class="${highlightClassOf[m]||'hl-room'}">${m}</span>`);
}

// Real force-directed layout via d3-force — replaces a hand-rolled O(n^2) loop that fully
// re-converged from scratch on every poll. d3-force uses a quadtree (Barnes-Hut) approximation
// for repulsion (O(n log n)) and drives the simulation incrementally via requestAnimationFrame,
// so it only does the small amount of work needed to relax from the last frame, not "recompute
// everything every 1.5s." SVG updates use D3's enter/update/exit joins — only changed elements
// touch the DOM, instead of throwing away and rebuilding the whole SVG as a string each tick.
const svg = d3.select('#map').append('svg').attr('width','100%').attr('height','100%')
  .attr('viewBox',`0 0 ${W} ${H}`);
// Fixed viewBox + a growing world means nodes eventually drift past the edge with no way to
// see them (browser scroll does nothing for SVG content — it just clips). Real pan/zoom via
// d3-zoom instead: scroll wheel to zoom, drag to pan, applied to one wrapper group so the
// force simulation's own coordinates never need to change.
const zoomLayer = svg.append('g');
const linkLayer = zoomLayer.append('g');
const frontierLayer = zoomLayer.append('g');
const nodeLayer = zoomLayer.append('g');
// Auto-fit tracks whether the USER has manually panned/zoomed yet (event.sourceEvent is only
// set for real mouse/wheel gestures, never for our own programmatic .transform() calls) — once
// they have, stop auto-fitting so it doesn't fight their exploration. Until then, the view
// re-fits itself to whatever's actually in the graph, so a growing world never silently drifts
// half off-screen with no indication anything's missing.
let userInteracted = false;
const zoomBehavior = d3.zoom().scaleExtent([0.25, 4])
  // Plain mouse-wheel over the SVG used to always zoom, which d3 implements by calling
  // preventDefault() on the wheel event — that's what trapped page scroll the instant the
  // cursor crossed into the map, making it feel "stuck" and impossible to scroll past.
  // Require Ctrl/Cmd+wheel to zoom (the same convention Google Maps/Figma use) so a plain
  // scroll always scrolls the PAGE; drag-to-pan (mousedown, not wheel) is untouched.
  .filter((event) => event.type !== 'wheel' ? !event.button : (event.ctrlKey || event.metaKey))
  .on('zoom', (event) => {
    zoomLayer.attr('transform', event.transform);
    if(event.sourceEvent) userInteracted = true;
  });
svg.call(zoomBehavior);

function fitToView(nodes){
  if(!nodes.length) return;
  const pad = 40;
  const xs = nodes.map(n=>n.x), ys = nodes.map(n=>n.y);
  const minX = Math.min(...xs)-pad, maxX = Math.max(...xs)+pad;
  const minY = Math.min(...ys)-pad, maxY = Math.max(...ys)+pad;
  const w = Math.max(maxX-minX, 1), h = Math.max(maxY-minY, 1);
  const k = Math.min(4, Math.max(0.25, Math.min(W/w, H/h)));
  const t = d3.zoomIdentity
    .translate(W/2 - (minX+maxX)/2*k, H/2 - (minY+maxY)/2*k)
    .scale(k);
  svg.transition().duration(400).call(zoomBehavior.transform, t);
}

// Click a node -> center the view on it (keeping current zoom level) and show its details
// in the "Selected room" panel. Discovered rooms show real content; undiscovered ones stay
// "???" here too (a click can't reveal what you haven't actually been to).
function centerOn(d){
  const k = d3.zoomTransform(svg.node()).k;
  const t = d3.zoomIdentity.translate(W/2 - d.x*k, H/2 - d.y*k).scale(k);
  svg.transition().duration(500).call(zoomBehavior.transform, t);
}
function showRoomInfo(d){
  const el = document.getElementById('roomInfo');
  if(!d.discovered){ el.innerHTML = '<span class=empty>??? — not discovered yet</span>'; return; }
  const feats = (d.features||[]).map(f => `<div>• ${esc(f)}</div>`).join('');
  const monsters = (d.contents||[]).filter(c=>c.type==='monster')
    .map(c => `<div>⚔ ${esc(c.name)} (HP ${c.hp})</div>`).join('');
  const loot = (d.contents||[]).filter(c=>c.type==='loot')
    .map(c => `<div>✦ ${esc(c.name)}</div>`).join('');
  el.innerHTML = `<b>${esc(d.name)}</b><br><span>${esc(d.description||'')}</span>${feats}${monsters}${loot}`;
}

const simulation = d3.forceSimulation()
  .force('charge', d3.forceManyBody().strength(-220))
  .force('link', d3.forceLink().id(d=>d.id).distance(90))
  .force('center', d3.forceCenter(W/2, H/2))
  .force('collide', d3.forceCollide(24))
  .on('tick', ticked)
  // Fit once the simulation actually settles (alpha decays below its minimum), not on a
  // guessed timeout — a fixed delay went stale because the reheated sim keeps drifting
  // nodes for longer than any one guess, so a node fit-to-view at 400ms could still end up
  // clipped once it kept moving afterward. 'end' fires exactly when motion actually stops.
  .on('end', () => { if(!userInteracted) fitToView(simulation.nodes()); });

// #map's actual box size changes for reasons a plain window 'resize' listener would miss
// entirely — e.g. the sidebar column growing taller (via CSS grid stretch) makes the whole
// row, and therefore #map, taller with no browser resize event at all. ResizeObserver catches
// both that and real window resizes. Re-fit unconditionally (ignoring userInteracted) because
// a manual pan/zoom was framed for the OLD box size and is meaningless once that box changes.
new ResizeObserver(entries => {
  const box = entries[0].contentRect;
  const w = Math.round(box.width), h = Math.round(box.height);
  if(!w || !h || (w === W && h === H)) return;
  W = w; H = h;
  svg.attr('viewBox', `0 0 ${W} ${H}`);
  simulation.force('center', d3.forceCenter(W/2, H/2));
  fitToView(simulation.nodes());
}).observe(mapEl);

let nodesById = {};
let lastSignature = '';

function ticked(){
 linkLayer.selectAll('line').attr('x1',d=>d.source.x).attr('y1',d=>d.source.y)
   .attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
 frontierLayer.selectAll('line').each(function(d){
   const n = nodesById[d.from]; if(!n) return;
   d3.select(this).attr('x1',n.x).attr('y1',n.y)
     .attr('x2',n.x+Math.cos(d.angle)*30).attr('y2',n.y+Math.sin(d.angle)*30);
 });
 nodeLayer.selectAll('g.node').attr('transform', d=>`translate(${d.x},${d.y})`);
}

function renderGraph(rooms, players, you){
 const empty = document.getElementById('mapEmpty');
 if(!rooms.length){ empty.style.display=''; svg.style('display','none'); return; }
 empty.style.display='none'; svg.style('display','');
 const occupants = {}; players.forEach(p=>{(occupants[p.location_id] ||= []).push(p);});
 const byId = {}; rooms.forEach(r=>{byId[r.id]=r;});

 // Structural signature — do the SET of rooms/edges actually differ from last render? If not,
 // we must not touch link bindings at all (see below for why), only refresh node colors/labels.
 const edgeKeys = new Set();
 for(const r of rooms) for(const dir in r.exits) if(byId[r.exits[dir]]) edgeKeys.add([r.id, r.exits[dir]].sort().join('|'));
 const signature = rooms.map(r=>r.id).sort().join(',') + '|' + [...edgeKeys].sort().join(',');
 const structureChanged = signature !== lastSignature;
 lastSignature = signature;

 // Node objects are mutated IN PLACE and never replaced, so their identity stays stable across
 // renders — this is what lets the simulation keep tracking the same objects it's animating.
 if(structureChanged){
   for(const r of rooms){ if(!nodesById[r.id]) nodesById[r.id] = {id: r.id, x: W/2+(Math.random()-.5)*100, y: H/2+(Math.random()-.5)*100}; }
   for(const id of Object.keys(nodesById)) if(!byId[id]) delete nodesById[id];
 }
 for(const r of rooms){
   const n = nodesById[r.id];
   n.name = r.name; n.visited = r.visited; n.discovered = r.discovered;
   n.description = r.description; n.features = r.features; n.contents = r.contents;
   n.mine = you && r.id===you.location_id;
   n.count = (occupants[r.id]||[]).length;
 }
 const nodes = rooms.map(r => nodesById[r.id]);

 // No persistent name labels on the graph — declutters it into a real "fog of war" map.
 // Discovered rooms reveal their name on HOVER (custom tooltip div, not native SVG <title> --
 // that's unreliable across browsers) and full details on CLICK; undiscovered rooms show
 // "???" outright either way, since there's no secret being protected, just "you haven't
 // been here yet." "You are here" is already conveyed by the gold node color + the header's
 // "You are in: ..." line, so the graph node itself doesn't need its own always-visible label.
 const tooltip = document.getElementById('nodeTooltip');
 const nodeSel = nodeLayer.selectAll('g.node').data(nodes, d=>d.id)
   .join(enter => {
     const g = enter.append('g').attr('class','node').style('cursor','pointer');
     // The actual "wow" moment: a room didn't just silently exist on the next poll, it grew
     // into existence right now. r=0 -> full size with a bouncy overshoot, so a freshly
     // Flash-generated room visibly POPS in rather than appearing already-rendered. Fill/
     // stroke color still gets set normally right after (below) — only the radius animates.
     g.append('circle').attr('r',0).attr('stroke-width',2)
       .transition().duration(650).ease(d3.easeBackOut.overshoot(1.8)).attr('r',16);
     g.append('text').attr('class','label').attr('y',30).attr('text-anchor','middle')
       .attr('fill','#8d7fae').attr('font-size',10);
     g.append('text').attr('class','count').attr('y',4).attr('text-anchor','middle')
       .attr('fill','#0a0713').attr('font-size',10).attr('font-weight','bold');
     // Custom hover tooltip, not a native SVG <title> — native SVG title tooltips are
     // unreliable across browsers (inconsistent/missing in Chrome in particular). A plain
     // positioned div driven by mouse events works everywhere.
     g.on('mouseenter', function(event, d){
       tooltip.textContent = d.discovered ? d.name : '???';
       tooltip.style.display = 'block';
     }).on('mousemove', function(event){
       const rect = document.getElementById('map').getBoundingClientRect();
       tooltip.style.left = (event.clientX - rect.left + 14) + 'px';
       tooltip.style.top = (event.clientY - rect.top + 10) + 'px';
     }).on('mouseleave', function(){
       tooltip.style.display = 'none';
     }).on('click', function(event, d){
       centerOn(d);
       showRoomInfo(d);
     });
     return g;
   });
 nodeSel.select('circle')
   .attr('fill', d=> d.mine ? '#4fd8c4' : (d.visited ? '#8072e0' : '#1c1630'))
   .attr('stroke', d=> d.mine ? '#8ff0e0' : '#453a6b')
   // A soft glow on your own current room only — you're a ghost too; this is the one node
   // that's genuinely "alive" right now, everything else is just trace/memory.
   .style('filter', d=> d.mine ? 'drop-shadow(0 0 7px #4fd8c4)' : null);
 nodeSel.select('text.label').text(d=> d.discovered ? '' : '???');
 nodeSel.select('text.count').text(d=> d.count ? d.count : '');

 // IMPORTANT: link objects' source/target start as plain id STRINGS. d3-force only rewrites
 // them in place into resolved node-object references (what makes `d.source.x` work in
 // ticked()) at the moment `.force('link').links(...)` is called on THAT exact array. So the
 // DOM binding and the simulation's link registration must always use the SAME array, every
 // render — skipping the simulation call on "unchanged" polls (an earlier attempt at this)
 // left the DOM bound to a fresh, never-resolved array instead, which is why every line went
 // invisible a second after first rendering. Rebuild+resolve every render; only gate the
 // expensive alpha/restart reheat behind structureChanged, since THAT's what caused the jitter.
 const edgeSet = new Set(); const links = []; const frontier = [];
 for(const r of rooms){
   for(const dir in r.exits){
     const destId = r.exits[dir];
     if(byId[destId]){
       const key = [r.id, destId].sort().join('|');
       if(!edgeSet.has(key)){ edgeSet.add(key); links.push({source: r.id, target: destId}); }
     } else {
       const node = nodesById[r.id];
       if(node._frontierAngle == null) node._frontierAngle = Math.random()*Math.PI*2;
       frontier.push({from: r.id, angle: node._frontierAngle});
     }
   }
 }
 linkLayer.selectAll('line').data(links, d=>[d.source,d.target].sort().join('|'))
   .join('line').attr('stroke','#3c3160').attr('stroke-width',2);
 frontierLayer.selectAll('line').data(frontier, d=>d.from)
   .join('line').attr('stroke','#3c3160').attr('stroke-width',2).attr('stroke-dasharray','3,4');

 simulation.nodes(nodes);
 simulation.force('link').links(links);
 // ticked() only fires while the simulation is actively running (alpha above its minimum).
 // Once it settles, alpha sits near zero and the timer stops — so calling .links() above
 // correctly RESOLVES source/target into node objects (confirmed directly: the bound datum
 // has real x/y), but nothing ever PAINTS those coordinates onto the SVG attributes, since
 // that only happens inside ticked(). Force one manual paint every render so positions are
 // always reflected, independent of whether the simulation's timer happens to be running.
 ticked();
 if(structureChanged){
   simulation.alpha(0.3).restart();
   // Quick immediate fit for instant feedback (still-settling positions, so approximate) --
   // the simulation's 'end' handler above does the accurate final fit once motion actually
   // stops, correcting for however long THIS graph takes to settle.
   if(!userInteracted) fitToView(nodes);
 }
}

async function tick(){
 try{
  const url = '/state?campaign='+encodeURIComponent(campaignId)
    + (playerId ? '&player='+encodeURIComponent(playerId) : '');
  const s = await (await fetch(url)).json();
  // Detect "the server redeployed since this tab loaded" — a long-lived tab only ever
  // fetches fresh DATA here, it never re-fetches its own JS, so stale rendering code can
  // silently misbehave against a data shape it wasn't written for (e.g. only some of the
  // graph's nodes drawing). Surface a refresh prompt instead of failing confusingly.
  if(loadedVersion === null){ loadedVersion = s.server_version; }
  else if(s.server_version && s.server_version !== loadedVersion){
    document.getElementById('staleBanner').style.display = 'block';
  }
  const worldTag = campaignId !== 'main' ? `[world: ${campaignId}] ` : '';
  const whereText = worldTag + (s.current_room ? ('You are in: '+(s.current_room.name||'')) : (playerId ? 'unknown player' : 'Spectating — connect your agent to the MCP server to start your own session'));
  document.getElementById('where').textContent = whereText;
  document.getElementById('whereInMap').textContent = whereText;
  renderGraph(s.rooms||[], s.players||[], s.you||null);
  rebuildHighlightIndex(s);
  const camp = s.campaign;
  document.getElementById('worldInfo').innerHTML = camp
    ? `<b>${esc(camp.theme||'')}</b>${camp.name?` — <span>${esc(camp.name)}</span>`:''}<br>${esc(camp.premise||'')}`
    : '<span class=empty>no world seeded yet</span>';
  const quests = s.quests||[];
  document.getElementById('questList').innerHTML = quests.length
    ? quests.map(q => {
        const qsteps = (q.steps||[]).map(st =>
          `<div>${st.done?'☑':'☐'} ${esc(st.text||'')}</div>`).join('')
          || '<span class=empty>no steps yet</span>';
        return `<div style="margin-bottom:8px"><b>📜 ${esc(q.title)}</b><br>${qsteps}</div>`;
      }).join('')
    : '<span class=empty>no active quests</span>';
  const ch = s.character;
  const invHtml = (ch?.inventory||[]).map(it => {
    const name = typeof it === 'string' ? it : it.name;
    const desc = typeof it === 'string' ? '' : (it.description||'');
    return desc ? `<span class=item data-desc="${esc(desc)}">${esc(name)}</span>` : `<span>${esc(name)}</span>`;
  }).join(', ');
  document.getElementById('char').innerHTML = ch? `<b>${esc(ch.name)}</b> <span>lvl ${ch.level} ${esc(ch.klass)}</span><br>HP ${ch.hp}/${ch.max_hp} · AC ${ch.ac}<br>${invHtml||'<span class=empty>empty-handed</span>'}`:'—';
  // esc() FIRST, always -- this was previously raw l.text with no escaping at all, a stored-
  // XSS gap (log_event's free-form agent-authored text would render as live HTML for every
  // viewer). highlightKnown only wraps matches in spans; it never introduces new raw content.
  document.getElementById('log').innerHTML=(s.log||[]).map(l=>`<div>${highlightKnown(esc(l.text))}</div>`).join('')||'<div class=empty>—</div>';
  const fc = document.getElementById('flashcount');
  const n = s.flash_calls||0;
  if(n !== lastFlashCalls){
    fc.textContent = `⚡ ${n} Flash call${n===1?'':'s'}`;
    if(n > lastFlashCalls){ fc.classList.add('pulse'); setTimeout(()=>fc.classList.remove('pulse'), 300); }
    lastFlashCalls = n;
  }
 }catch(e){}
}
let lastFlashCalls = -1;
let loadedVersion = null;
setInterval(tick,1500);tick();

// Live world stream — every domain event, from every player's session, pushed here as it
// happens. Deliberately unfiltered by playerId: this is the out-of-world view of the same
// stigmergic mechanic that shows up in-game as "Traces of those who came before."
//
// Reconnectable so the Flash-only toggle can swap filters live: default mode only ever shows
// events from "now" forward (matches the SSE endpoint's default), but Flash-only mode passes
// backfill=1 so clicking it actually shows the world's FULL Flash-call history, not just
// whatever happens to arrive after you click.
const streamEl = document.getElementById('stream');
const streamDot = document.getElementById('streamDot');
const streamTitle = document.getElementById('streamTitle');
const streamSub = document.getElementById('streamSub');
const filterBtn = document.getElementById('streamFilterBtn');
let es = null;
let flashOnly = false;
let streamCaughtUp = true;

function connectStream(){
  if (es) es.close();
  streamEl.innerHTML = '<div class=empty>waiting for the world to move...</div>';
  let url = '/stream/events?campaign='+encodeURIComponent(campaignId);
  if (flashOnly) url += '&flash_only=1&backfill=1';
  es = new EventSource(url);
  es.addEventListener('world-event', (e) => {
    const ev = JSON.parse(e.data);
    const empty = streamEl.querySelector('.empty');
    if (empty) empty.remove();
    const div = document.createElement('div');
    const who = ev.player_id ? `<span class=who>${esc(ev.player_id.slice(0,6))}</span> ` : '';
    div.innerHTML = `${who}${highlightKnown(esc(ev.text))}`;
    streamEl.prepend(div);
    while (streamEl.children.length > 50) streamEl.lastChild.remove();
    // backfilled rows arrive all at once on connect — only flash the ones that show up
    // AFTER that initial catch-up, same "something just happened" cue as the default mode.
    if (!flashOnly || streamCaughtUp) {
      div.classList.add('new');
      setTimeout(() => div.classList.remove('new'), 900);
    }
  });
  es.onerror = () => { streamDot.style.background = '#ef4444'; };
  es.onopen = () => {
    streamDot.style.background = '#22c55e';
    streamCaughtUp = false;
    setTimeout(() => { streamCaughtUp = true; }, 800);
  };
}

filterBtn.addEventListener('click', () => {
  flashOnly = !flashOnly;
  filterBtn.classList.toggle('active', flashOnly);
  filterBtn.textContent = flashOnly ? '✕ Showing Flash calls only' : '⚡ Flash calls only';
  streamTitle.textContent = flashOnly ? 'Flash calls' : 'Live world stream';
  streamSub.textContent = flashOnly
    ? 'every GPU generation call this world has made'
    : 'every player, every session';
  connectStream();
});

// Clicking the header counter opens the FULL Flash-call history on its own page (new tab) —
// every call this world has ever made, uncapped, not a live-filtered view of the panel below
// (that's what the "⚡ Flash calls only" toggle on the stream panel is for instead).
document.getElementById('flashcount').style.cursor = 'pointer';
document.getElementById('flashcount').title = 'Click to see every Flash call this world has made';
document.getElementById('flashcount').addEventListener('click', () => {
  window.open('/flash-calls?campaign='+encodeURIComponent(campaignId), '_blank');
});

document.getElementById('metricsLink').addEventListener('click', () => {
  window.open('/metrics?campaign='+encodeURIComponent(campaignId), '_blank');
});

// Item description tooltip: delegated from the never-replaced #char panel div (its innerHTML
// is rewritten every tick(), so listeners bound directly to .item spans would be lost on the
// next redraw) — same custom-div-over-mouse-events approach as #nodeTooltip, for the same
// reason (native title tooltips are slow/inconsistent across browsers).
const itemTooltip = document.getElementById('itemTooltip');
document.getElementById('char').addEventListener('mouseover', (e) => {
  const el = e.target.closest('.item');
  if (!el) return;
  itemTooltip.textContent = el.dataset.desc || '';
  itemTooltip.style.display = 'block';
});
document.getElementById('char').addEventListener('mousemove', (e) => {
  if (itemTooltip.style.display !== 'block') return;
  itemTooltip.style.left = (e.clientX + 14) + 'px';
  itemTooltip.style.top = (e.clientY + 10) + 'px';
});
document.getElementById('char').addEventListener('mouseout', (e) => {
  if (e.target.closest('.item')) itemTooltip.style.display = 'none';
});

connectStream();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/worlds")
def worlds_list() -> JSONResponse:
    """Every world that exists, with enough context to actually recognize one — theme,
    premise, player count, last activity. Campaign ids are opaque hex strings; nobody finds
    a world they don't already have a direct link to without this."""
    c = _db()
    try:
        rows = c.execute(
            "SELECT id, name, theme, premise, created_at FROM campaigns ORDER BY created_at DESC"
        ).fetchall()
        counts = dict(c.execute(
            "SELECT campaign_id, COUNT(*) FROM character GROUP BY campaign_id"
        ).fetchall())
        activity = dict(c.execute(
            "SELECT campaign_id, MAX(ts) FROM log GROUP BY campaign_id"
        ).fetchall())
    except sqlite3.OperationalError:
        return JSONResponse([])
    finally:
        c.close()
    worlds = [{"id": r["id"], "name": r["name"], "theme": r["theme"], "premise": r["premise"],
              "players": counts.get(r["id"], 0), "last_activity": activity.get(r["id"]),
              "created_at": r["created_at"]} for r in rows]
    return JSONResponse(worlds)


@app.get("/install.sh")
def install_script() -> Response:
    """Serves scripts/install_claude_code.sh straight from this pod's own checkout.

    The dndmcp GitHub repo is PRIVATE (verified) — raw.githubusercontent.com 404s for anyone
    without repo access, which silently breaks the curl-install one-liner for literally
    everyone except the repo owner. The pod already has this exact file on disk; serving it
    here means the same one-liner works for any judge/player, no GitHub auth needed, and it
    can never drift from what's actually deployed (same checkout redeploy_pod.sh updates)."""
    script_path = Path(__file__).parent.parent / "scripts" / "install_claude_code.sh"
    try:
        content = script_path.read_text()
    except FileNotFoundError:
        content = "echo 'install script not found on this pod' >&2\nexit 1\n"
    return Response(content=content, media_type="text/x-shellscript")


_EMPTY_STATE = {"rooms": [], "players": [], "character": None, "you": None, "current_room": None, "log": [], "quests": [], "flash_calls": 0, "campaign": None, "server_version": SERVER_VERSION}


@app.get("/state")
def state(request: Request) -> JSONResponse:
    player_id = request.query_params.get("player")
    # Multi-world: each world's map is independent now — "main" is the well-known default
    # (what every pre-multi-world link/bookmark still means), anything else is a specific
    # world someone created/shared (see server.py start_adventure's campaign_id).
    campaign_id = request.query_params.get("campaign") or "main"
    try:
        c = _db()
    except Exception:
        return JSONResponse(_EMPTY_STATE)
    try:
        # exits live in the generic `edges` table now (from_type/to_type='room'), not a JSON
        # column on the room row — same pattern as the Context DB's own edges table. Room ids
        # are unique across worlds by construction (non-main worlds prefix with their own
        # campaign_id — see server.py), so scoping via "from_id belongs to this campaign" is
        # enough without a campaign_id column on edges itself.
        exits_by_room: dict[str, dict[str, str]] = {}
        for e in c.execute(
            "SELECT from_id, edge_type, to_id FROM edges WHERE from_type='room' AND to_type='room'"
            " AND from_id IN (SELECT id FROM rooms WHERE campaign_id=?)", (campaign_id,)
        ).fetchall():
            exits_by_room.setdefault(e["from_id"], {})[e["edge_type"]] = e["to_id"]

        # Per-VIEWING-PLAYER discovery, not the global `visited` flag — a room another player
        # generated/visited isn't "known" to you until you've actually been there yourself
        # (character--discovered-->room edge, same mechanism server.py's _adjacent_rooms uses
        # to gate spoilers). Spectating with no ?player= has no "you" to gate against, so fall
        # back to the global flag (anyone-has-visited) rather than blanking the whole map.
        discovered_ids: set[str] | None = None
        if player_id:
            discovered_ids = {row["to_id"] for row in c.execute(
                "SELECT to_id FROM edges WHERE from_type='character' AND from_id=?"
                " AND to_type='room' AND edge_type='discovered'", (player_id,)
            ).fetchall()}

        rooms = []
        for r in c.execute("SELECT * FROM rooms WHERE campaign_id=?", (campaign_id,)).fetchall():
            discovered = (r["id"] in discovered_ids) if discovered_ids is not None else bool(r["visited"])
            rooms.append({"id": r["id"], "name": r["name"], "description": r["description"],
                          "features": json.loads(r["features"] or "[]"),
                          "contents": json.loads(r["contents"] or "[]"),
                          "visited": bool(r["visited"]), "discovered": discovered,
                          "exits": exits_by_room.get(r["id"], {})})  # {direction: dest_room_id}
        players = [{"player_id": p["player_id"], "name": p["name"], "location_id": p["location_id"]}
                   for p in c.execute(
                       "SELECT player_id, name, location_id FROM character WHERE campaign_id=?",
                       (campaign_id,)).fetchall()]
        # The world's founding hook — theme + premise, seeded once at create_campaign and
        # never touched again. Currently only ever shown once, in start_adventure's own reply
        # text, then lost to scrollback — surfacing it here so anyone watching (a spectator,
        # a returning player) can see what this world's premise actually was.
        camp_row = c.execute(
            "SELECT name, theme, premise, created_at FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        campaign = dict(camp_row) if camp_row else None
        quests = [dict(r) for r in c.execute(
            "SELECT id, title, description, state, steps, given_by, created_at FROM quest"
            " WHERE campaign_id=? AND state='active' ORDER BY created_at", (campaign_id,)
        ).fetchall()]
        for q in quests:
            q["steps"] = json.loads(q["steps"] or "[]")
        char = None
        cur = None
        if player_id:
            row = c.execute("SELECT * FROM character WHERE player_id=?", (player_id,)).fetchone()
            if row:
                char = dict(row)
                char["inventory"] = json.loads(char["inventory"] or "[]")
                cur = c.execute("SELECT * FROM rooms WHERE id=?", (char["location_id"],)).fetchone()
        log = [dict(r) for r in c.execute(
            "SELECT text FROM log WHERE campaign_id=? ORDER BY seq DESC LIMIT 8", (campaign_id,)
        ).fetchall()][::-1]
        # room.generated is the highest-volume Flash use, but entity.spawned (NPC persona
        # generation) and npc.talked (NPC dialogue) also call Flash — count all three so the
        # counter doesn't undercount just because personas are generated far more sparsely
        # (deterministic density gate — see server.py::_maybe_spawn_entity_persona).
        flash_calls = c.execute(
            "SELECT COUNT(*) FROM log WHERE campaign_id=?"
            " AND kind IN ('room.generated','entity.spawned','npc.talked','item.picked_up','story.exported')"
            " AND text LIKE '%(flash)%'",
            (campaign_id,),
        ).fetchone()[0]
        return JSONResponse({"rooms": rooms, "players": players, "character": char,
                             "you": char, "current_room": (dict(cur) if cur else None), "log": log,
                             "quests": quests,
                             "flash_calls": flash_calls, "campaign": campaign,
                             "server_version": SERVER_VERSION})
    except sqlite3.OperationalError:
        # schema not initialized yet — no one has called start_adventure on this pod yet
        return JSONResponse(_EMPTY_STATE)
    finally:
        c.close()


@app.get("/export_story")
async def export_story(request: Request):
    """One player's real event timeline, synthesized into a markdown story via Flash
    (worldgen.generate_story) — falls back to a plain chronological listing of the same
    timeline if Flash is off/errors, same reliability-first pattern as everything else
    (this always produces SOMETHING downloadable, just less polished without a model)."""
    campaign_id = request.query_params.get("campaign") or "main"
    player_id = request.query_params.get("player")
    if not player_id:
        return JSONResponse({"error": "?player=<id> is required to export a story"}, status_code=400)
    c = _db()
    try:
        char = c.execute("SELECT * FROM character WHERE player_id=?", (player_id,)).fetchone()
        if not char:
            return JSONResponse({"error": "unknown player"}, status_code=404)
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        # This player's own actions, PLUS world-level events with no player_id (room.generated
        # etc.) that happened in this campaign — not other players' actions, this is THEIR
        # story, not the whole world's. Chronological via seq (monotonic insert order).
        events = c.execute(
            "SELECT ts, kind, text FROM log WHERE campaign_id=? AND (player_id=? OR player_id IS NULL)"
            " ORDER BY seq ASC", (campaign_id, player_id),
        ).fetchall()
    finally:
        c.close()

    theme = camp["theme"] if camp else "adventure"
    premise = camp["premise"] if camp else ""
    timeline_lines = [f"- {e['text']}" for e in events] or ["- (nothing has happened yet)"]
    timeline_text = "\n".join(timeline_lines)

    markdown = await worldgen.generate_story(char["name"], char["klass"], theme, premise, timeline_text)
    via = "flash"
    if not markdown:
        via = "procedural"
        markdown = (f"# {char['name']}'s Story\n\n*A {char['klass']} in a {theme} world.*\n\n"
                   f"{premise}\n\n## Timeline\n\n{timeline_text}\n")

    # Same "every Flash call is a domain event" pattern as room.generated/npc.talked/etc —
    # otherwise the (real) GPU work of writing this story is invisible to the Flash-call
    # counter and the live/history stream (see /state, /stream/events flash_only).
    c = _db()
    try:
        c.execute(
            "INSERT INTO log (ts,kind,text,player_id,subject_type,subject_id,campaign_id,ip)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), "story.exported", f"{char['name']} exported their story ({via}).",
             player_id, "character", player_id, campaign_id, _client_ip(request)),
        )
        c.commit()
    finally:
        c.close()

    safe_name = "".join(ch for ch in char["name"] if ch.isalnum() or ch in " -_").strip() or "story"
    return Response(content=markdown, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name}.md"',
                             "X-Story-Via": via})


@app.get("/flash-calls", response_class=HTMLResponse)
def flash_calls_page(request: Request) -> str:
    """Every Flash call this world has EVER made, in full, on its own page — not a capped/
    truncated panel. What #flashcount in the header links to (opens in a new tab): the whole
    history at once, not a live-filtered view of the existing stream."""
    campaign_id = request.query_params.get("campaign") or "main"
    c = _db()
    try:
        rows = c.execute(
            "SELECT ts, kind, text, player_id, subject_type, subject_id FROM log"
            " WHERE campaign_id=? AND text LIKE '%(flash)%' ORDER BY seq DESC",
            (campaign_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        c.close()

    def row_html(r: sqlite3.Row) -> str:
        ts = datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S") if r["ts"] else "?"
        who = f'<span class=who>{html.escape(r["player_id"][:6])}</span> ' if r["player_id"] else ""
        subj = (f'<span class=subj>{html.escape(r["subject_type"])}:{html.escape(r["subject_id"])}</span>'
               if r["subject_type"] else "")
        return (f'<div class=row><span class=ts>{ts}</span><span class=kind>{html.escape(r["kind"])}</span>'
               f'{who}<span class=text>{html.escape(r["text"])}</span>{subj}</div>')

    body = "".join(row_html(r) for r in rows) or '<div class=empty>No Flash calls yet in this world.</div>'
    return f"""<!doctype html><html><head><meta charset=utf-8><title>Flash calls — {html.escape(campaign_id)}</title>
<style>
:root{{--bg:#0a0713;--panel:#150f24;--border:#2b2145;--border-soft:#221a38;--text:#e7e1f5;
  --muted:#8d7fae;--warm:#e8b339;--warm-bright:#f5cc66;--ghost:#4fd8c4;--ghost-bright:#8ff0e0}}
body{{margin:0;background:var(--bg);color:var(--text);font:13px 'IBM Plex Mono',ui-monospace,Menlo,monospace}}
header{{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:baseline}}
h1{{font-size:16px;margin:0;color:var(--warm-bright)}}
.count{{color:var(--muted)}}
main{{padding:10px 20px}}
.row{{display:flex;gap:12px;padding:7px 0;border-bottom:1px solid var(--border-soft);font-size:12.5px;align-items:baseline}}
.ts{{color:var(--muted);flex-shrink:0;width:150px}}
.kind{{color:var(--warm);flex-shrink:0;width:130px}}
.who{{color:var(--ghost)}}
.subj{{color:var(--muted);margin-left:auto}}
.text{{color:var(--text)}}
.empty{{color:var(--muted);padding:20px 0}}
</style></head><body>
<header><h1>⚡ Flash calls</h1><span class=count>{len(rows)} total in this world</span></header>
<main>{body}</main>
</body></html>"""


@app.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: Request) -> str:
    """System-wide counters for this world: event volume over time, unique players/IPs seen,
    breakdown by event kind, and a per-player table (last seen, last IP, event count). All
    derived from the existing `log`/`character` tables via aggregate queries — no new table,
    same "just use what's there more fully" approach as the rest of EVENT_STREAM_SPEC.md.
    Hackathon-demo surface, not an ops dashboard: counters + plain tables, no charting lib."""
    campaign_id = request.query_params.get("campaign") or "main"
    c = _db()
    try:
        total_events = c.execute(
            "SELECT COUNT(*) FROM log WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
        unique_players = c.execute(
            "SELECT COUNT(DISTINCT player_id) FROM log WHERE campaign_id=? AND player_id IS NOT NULL",
            (campaign_id,)).fetchone()[0]
        unique_ips = c.execute(
            "SELECT COUNT(DISTINCT ip) FROM log WHERE campaign_id=? AND ip IS NOT NULL",
            (campaign_id,)).fetchone()[0]
        # Same three-kind Flash definition /state's header counter and /flash-calls use.
        flash_calls = c.execute(
            "SELECT COUNT(*) FROM log WHERE campaign_id=?"
            " AND kind IN ('room.generated','entity.spawned','npc.talked','item.picked_up','story.exported')"
            " AND text LIKE '%(flash)%'",
            (campaign_id,)).fetchone()[0]
        by_kind = c.execute(
            "SELECT kind, COUNT(*) AS n FROM log WHERE campaign_id=? GROUP BY kind ORDER BY n DESC LIMIT 20",
            (campaign_id,)).fetchall()
        hourly = c.execute(
            "SELECT strftime('%Y-%m-%d %H:00', ts, 'unixepoch') AS bucket, COUNT(*) AS n"
            " FROM log WHERE campaign_id=? AND ts >= ? GROUP BY bucket ORDER BY bucket ASC",
            (campaign_id, time.time() - 86400)).fetchall()
        players = c.execute(
            "SELECT ch.player_id AS player_id, ch.name AS name, ch.klass AS klass,"
            " (SELECT COUNT(*) FROM log WHERE player_id=ch.player_id AND campaign_id=ch.campaign_id) AS events,"
            " (SELECT MAX(ts) FROM log WHERE player_id=ch.player_id AND campaign_id=ch.campaign_id) AS last_seen,"
            " (SELECT ip FROM log WHERE player_id=ch.player_id AND campaign_id=ch.campaign_id"
            "  AND ip IS NOT NULL ORDER BY seq DESC LIMIT 1) AS last_ip"
            " FROM character ch WHERE ch.campaign_id=? ORDER BY last_seen DESC",
            (campaign_id,)).fetchall()
    except sqlite3.OperationalError:
        total_events = unique_players = unique_ips = flash_calls = 0
        by_kind = hourly = players = []
    finally:
        c.close()

    def ts_fmt(ts: float | None) -> str:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "—"

    kind_rows = "".join(
        f'<div class=row><span class=kind>{html.escape(r["kind"])}</span>'
        f'<span class=bar><span style="width:{min(100, r["n"] * 100 // max(by_kind[0]["n"], 1))}%"></span></span>'
        f'<span class=n>{r["n"]}</span></div>'
        for r in by_kind
    ) or '<div class=empty>No events yet.</div>'

    hour_rows = "".join(
        f'<div class=row><span class=ts>{html.escape(r["bucket"])}</span><span class=n>{r["n"]}</span></div>'
        for r in hourly
    ) or '<div class=empty>No events in the last 24h.</div>'

    player_rows = "".join(
        f'<div class=row><span class=who>{html.escape(p["player_id"][:8])}</span>'
        f'<span class=pname>{html.escape(p["name"] or "?")} <span class=muted>({html.escape(p["klass"] or "?")})</span></span>'
        f'<span class=n>{p["events"]} events</span>'
        f'<span class=ip>{html.escape(p["last_ip"] or "—")}</span>'
        f'<span class=ts>{ts_fmt(p["last_seen"])}</span></div>'
        for p in players
    ) or '<div class=empty>No players yet.</div>'

    return f"""<!doctype html><html><head><meta charset=utf-8><title>Metrics — {html.escape(campaign_id)}</title>
<style>
:root{{--bg:#0a0713;--panel:#150f24;--border:#2b2145;--border-soft:#221a38;--text:#e7e1f5;
  --muted:#8d7fae;--warm:#e8b339;--warm-bright:#f5cc66;--ghost:#4fd8c4;--ghost-bright:#8ff0e0}}
body{{margin:0;background:var(--bg);color:var(--text);font:13px 'IBM Plex Mono',ui-monospace,Menlo,monospace}}
header{{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:baseline}}
h1{{font-size:16px;margin:0;color:var(--warm-bright)}}
.count{{color:var(--muted)}}
main{{padding:14px 20px;max-width:820px}}
.cards{{display:flex;gap:14px;margin-bottom:22px;flex-wrap:wrap}}
.card{{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:12px 18px;min-width:130px}}
.card .num{{font-size:22px;color:var(--ghost-bright);font-weight:600}}
.card .label{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
section{{margin-bottom:26px}}
h2{{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin:0 0 8px}}
.row{{display:flex;gap:12px;padding:6px 0;border-bottom:1px solid var(--border-soft);font-size:12.5px;align-items:center}}
.kind{{color:var(--warm);flex-shrink:0;width:150px}}
.bar{{flex:1;background:var(--border-soft);height:8px;border-radius:4px;overflow:hidden}}
.bar span{{display:block;height:100%;background:var(--warm);border-radius:4px}}
.n{{color:var(--text);flex-shrink:0;width:70px;text-align:right}}
.ts{{color:var(--muted);flex-shrink:0;width:150px}}
.who{{color:var(--ghost);flex-shrink:0;width:80px}}
.pname{{flex:1}}
.muted{{color:var(--muted)}}
.ip{{color:var(--muted);flex-shrink:0;width:130px}}
.empty{{color:var(--muted);padding:10px 0}}
</style></head><body>
<header><h1>📊 Metrics</h1><span class=count>{html.escape(campaign_id)}</span></header>
<main>
<div class=cards>
 <div class=card><div class=num>{total_events}</div><div class=label>Events</div></div>
 <div class=card><div class=num>{unique_players}</div><div class=label>Players</div></div>
 <div class=card><div class=num>{unique_ips}</div><div class=label>Unique IPs</div></div>
 <div class=card><div class=num>{flash_calls}</div><div class=label>Flash calls</div></div>
</div>
<section><h2>Events by kind</h2>{kind_rows}</section>
<section><h2>Activity, last 24h (hourly)</h2>{hour_rows}</section>
<section><h2>Players</h2>{player_rows}</section>
</main>
</body></html>"""


@app.get("/stream/events")
async def stream_events(request: Request):
    """The world's live pulse, pushed to every connected tab as events happen.

    Default (no query params beyond ?campaign=): EVERY player's actions in ONE world,
    unfiltered by who's watching — the out-of-world view of the same stigmergic mechanic
    that surfaces in-world as 'Traces of those who came before' (server.py's _render_scene).
    This is the demo centerpiece; don't filter it by player by default. Scoped to ?campaign=
    (default "main") since multi-world landed — otherwise another world's ghosts would leak
    into yours, which breaks the premise just as badly as per-player filtering would.

    Optional filters (EVENT_STREAM_SPEC.md #4 — a separate capability layered on top, same
    feed mechanism): ?player_id=  (one player's own events), ?subject_type=&subject_id=
    (one room/npc's history), ?kind_prefix=  (e.g. "flash." for system/GPU events vs
    everything else). Combine freely; each is AND'd in.

    ?flash_only=1: text LIKE '%(flash)%' — the same condition /state's flash_calls counter
    uses, but as a stream filter (Flash calls span several `kind`s — room.generated,
    entity.spawned, npc.talked, item.picked_up — so kind_prefix alone can't isolate them).

    ?backfill=1: start from seq 0 instead of "now" — without this, a fresh connection only
    ever sees events from the moment it opened, same as the default stream. Backfill is what
    makes "click the Flash counter, see every call this world has ever made" possible."""
    campaign_id = request.query_params.get("campaign") or "main"
    player_id = request.query_params.get("player_id")
    subject_type = request.query_params.get("subject_type")
    subject_id = request.query_params.get("subject_id")
    kind_prefix = request.query_params.get("kind_prefix")
    flash_only = request.query_params.get("flash_only") == "1"
    backfill = request.query_params.get("backfill") == "1"

    where = ["campaign_id = ?"]
    params: list[object] = [campaign_id]
    if player_id:
        where.append("player_id = ?")
        params.append(player_id)
    if subject_type and subject_id:
        where.append("subject_type = ? AND subject_id = ?")
        params.extend([subject_type, subject_id])
    if kind_prefix:
        where.append("kind LIKE ?")
        params.append(f"{kind_prefix}%")
    if flash_only:
        where.append("text LIKE '%(flash)%'")
    extra_where = (" AND " + " AND ".join(where)) if where else ""

    async def gen():
        last_seq = 0
        if not backfill:
            try:
                c = _db()
                last_seq = c.execute("SELECT COALESCE(MAX(seq), 0) FROM log").fetchone()[0]
                c.close()
            except Exception:
                pass
        while True:
            if await request.is_disconnected():
                break
            try:
                c = _db()
                rows = c.execute(
                    "SELECT seq, ts, kind, text, player_id, subject_type, subject_id"
                    f" FROM log WHERE seq > ?{extra_where} ORDER BY seq ASC",
                    (last_seq, *params),
                ).fetchall()
                c.close()
            except Exception:
                rows = []
            for r in rows:
                last_seq = r["seq"]
                yield {"event": "world-event", "data": json.dumps(dict(r))}
            await asyncio.sleep(1)
    return EventSourceResponse(gen())


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("GUI_PORT", "8001")),
                log_level="warning")


if __name__ == "__main__":
    main()
