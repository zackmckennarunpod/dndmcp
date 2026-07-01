"""DNDMCP web GUI — a live map of the world, synced to the game.

Reads the SAME SQLite the MCP server writes, so it auto-syncs: as the player moves (via MCP
tools), the DB updates and this map reflects it on the next poll. Served by the pod brain
alongside the MCP server. Shows the world graph (rooms placed by their path), current
position, character, and the log.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sqlite3
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
  --text:#e7e1f5; --muted:#8d7fae; --dim:#5f5480;
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
 main{display:grid;grid-template-columns:1fr 280px;gap:16px;padding:16px 18px}
 .panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}
 .panel h2{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted);margin:0 0 10px}
 #map{width:100%;height:420px;overflow:hidden;position:relative;
   background:radial-gradient(ellipse at 50% 40%,#1a1330 0%,var(--panel) 70%)}
 #nodeTooltip{position:absolute;pointer-events:none;background:#1c1433;border:1px solid var(--link);
   border-radius:6px;padding:4px 9px;font-size:12px;color:var(--text);display:none;z-index:10;
   box-shadow:0 4px 16px rgba(0,0,0,.5)}
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
#staleBanner{display:none;background:var(--warm);color:#1a1206;font-weight:600;font-size:12.5px;
  padding:7px 18px;text-align:center}
#staleBanner a{color:#1a1206;text-decoration:underline}
#shareBtn{background:var(--link);color:var(--ghost-bright);border:1px solid var(--border);
  border-radius:6px;padding:5px 11px;font:600 12px 'IBM Plex Mono',monospace;cursor:pointer;
  transition:background .15s}
#shareBtn:hover{background:var(--visited)}
#shareBtn.copied{background:var(--ghost);color:var(--bg)}
#streamFilterBtn{background:transparent;color:var(--muted);border:1px solid var(--border);
  border-radius:5px;padding:2px 9px;font:600 10.5px 'IBM Plex Mono',monospace;cursor:pointer;
  text-transform:none;letter-spacing:0}
#streamFilterBtn.active{background:var(--warm);color:#1a1206;border-color:var(--warm)}
 #stream{display:flex;flex-direction:column-reverse;gap:0;height:120px;overflow-y:auto}
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
 details.panel .body{margin-top:10px;color:var(--muted);line-height:1.6;font-size:12.5px}
 details.panel .body b{color:var(--ghost-bright)}
 details.panel .body a{color:var(--ghost-bright);text-decoration:underline;text-decoration-color:var(--ghost)}
 details.panel .body p{margin:0 0 10px}
 details.panel .body code{background:var(--unvisited);padding:1px 5px;border-radius:4px;font-size:11.5px}
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
<header><h1>⚔ DNDMCP</h1><span class=sub id=where>—</span>
 <span id=flashcount>⚡ 0 Flash calls</span>
 <button id=shareBtn title="Copies instructions to paste into your agent (Claude Code/Desktop) running dndmcp">🔗 Share</button></header>
<details open class=panel style="margin:16px 18px 16px">
 <summary>🎲 Connect &amp; play — anyone can join, no account needed</summary>
 <div class=body>
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
<details class=panel style="margin:0 18px 0">
 <summary>How this works — the Graph Context Engine underneath</summary>
 <div class=body>
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
</details>
<main>
 <div class=panel><h2>World map (shared, live)</h2><div class=sub id=whereInMap style="margin-bottom:8px">—</div><div id=map><span id=mapEmpty class=empty>no adventure yet — start one in your agent</span><div id=nodeTooltip></div></div></div>
 <aside style="display:flex;flex-direction:column;gap:16px">
  <div class=panel><h2>This world</h2><div class=ch id=worldInfo>—</div></div>
  <div class=panel><h2>Character</h2><div class=ch id=char>—</div>
   <button id=exportStoryBtn style="margin-top:10px;width:100%">📜 Export story</button></div>
  <div class=panel><h2>Selected room</h2><div class=ch id=roomInfo><span class=empty>click a room on the map</span></div></div>
  <div class=panel><h2>Recent</h2><div class=log id=log></div></div>
 </aside>
</main>
<div style="padding:0 18px 18px" id=streamSection>
 <div class=panel><h2><span id=streamDot></span><span id=streamTitle>Live world stream — every player, every session</span>
   <button id=streamFilterBtn style="margin-left:10px">⚡ Flash calls only</button></h2>
   <div id=stream><div class=empty>waiting for the world to move...</div></div></div>
</div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const params = new URLSearchParams(location.search);
const playerId = params.get('player');
const campaignId = params.get('campaign') || 'main';
const W=700, H=420;

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
const svg = d3.select('#map').append('svg').attr('width','100%').attr('height',H)
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
const zoomBehavior = d3.zoom().scaleExtent([0.25, 4]).on('zoom', (event) => {
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
  const whereText = worldTag + (s.current_room ? ('You are in: '+(s.current_room.name||'')) : (playerId ? 'unknown player' : 'spectating — no ?player= in link'));
  document.getElementById('where').textContent = whereText;
  document.getElementById('whereInMap').textContent = whereText;
  renderGraph(s.rooms||[], s.players||[], s.you||null);
  rebuildHighlightIndex(s);
  const camp = s.campaign;
  document.getElementById('worldInfo').innerHTML = camp
    ? `<b>${esc(camp.theme||'')}</b>${camp.name?` — <span>${esc(camp.name)}</span>`:''}<br>${esc(camp.premise||'')}`
    : '<span class=empty>no world seeded yet</span>';
  const ch = s.character;
  const invHtml = (ch?.inventory||[]).map(it => {
    const name = typeof it === 'string' ? it : it.name;
    const desc = typeof it === 'string' ? '' : (it.description||'');
    return desc ? `<span class=item title="${esc(desc)}">${esc(name)}</span>` : `<span>${esc(name)}</span>`;
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
  streamTitle.textContent = flashOnly
    ? "Flash calls — every GPU generation call this world has made"
    : 'Live world stream — every player, every session';
  connectStream();
});

// Clicking the header counter jumps straight to the (now Flash-filtered) stream panel —
// "see all the flash calls that were made" in one click, not a separate view to hunt for.
document.getElementById('flashcount').style.cursor = 'pointer';
document.getElementById('flashcount').addEventListener('click', () => {
  if (!flashOnly) filterBtn.click();
  document.getElementById('streamSection').scrollIntoView({behavior:'smooth', block:'start'});
});

connectStream();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


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


_EMPTY_STATE = {"rooms": [], "players": [], "character": None, "you": None, "current_room": None, "log": [], "flash_calls": 0, "campaign": None, "server_version": SERVER_VERSION}


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
            " AND kind IN ('room.generated','entity.spawned','npc.talked','item.picked_up')"
            " AND text LIKE '%(flash)%'",
            (campaign_id,),
        ).fetchone()[0]
        return JSONResponse({"rooms": rooms, "players": players, "character": char,
                             "you": char, "current_room": (dict(cur) if cur else None), "log": log,
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

    safe_name = "".join(ch for ch in char["name"] if ch.isalnum() or ch in " -_").strip() or "story"
    return Response(content=markdown, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name}.md"',
                             "X-Story-Via": via})


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
