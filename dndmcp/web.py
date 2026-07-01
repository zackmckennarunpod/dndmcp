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
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

app = FastAPI(title="DNDMCP map")


def _db() -> sqlite3.Connection:
    state_dir = os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp"))
    c = sqlite3.connect(str(Path(state_dir) / "campaign.db"))
    c.row_factory = sqlite3.Row
    return c


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>DNDMCP — map</title>
<style>
 body{margin:0;background:#0b0e14;color:#e6edf3;font:13px ui-monospace,Menlo,monospace}
 header{padding:12px 18px;border-bottom:1px solid #222c3a;display:flex;gap:12px;align-items:baseline}
 h1{font-size:15px;margin:0}.sub{color:#7d8794;font-size:12px}
 main{display:grid;grid-template-columns:1fr 280px;gap:16px;padding:16px 18px}
 .panel{background:#141a24;border:1px solid #222c3a;border-radius:10px;padding:14px}
 .panel h2{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#7d8794;margin:0 0 10px}
 #map{width:100%;height:420px;overflow:hidden;position:relative}
 #nodeTooltip{position:absolute;pointer-events:none;background:#1c2532;border:1px solid #334155;
   border-radius:6px;padding:4px 9px;font-size:12px;color:#e6edf3;display:none;z-index:10;
   box-shadow:0 4px 12px rgba(0,0,0,.4)}
 .ch b{color:#e6edf3}.ch span{color:#7d8794}
 .ch span.item{cursor:help;border-bottom:1px dotted #475569}
 .log div{color:#9fb1c1;padding:2px 0;border-bottom:1px solid #1a2230;font-size:12px}
 .empty{color:#7d8794}
#flashcount{color:#f5a524;font-weight:bold;margin-left:auto;transition:transform .15s}
#flashcount.pulse{transform:scale(1.3);color:#fcd34d}
 #stream{display:flex;flex-direction:column-reverse;gap:0;height:120px;overflow-y:auto}
 #stream div{color:#9fb1c1;padding:2px 0;border-bottom:1px solid #1a2230;font-size:12px}
 #stream div.new{animation:flash .8s ease-out}
 #stream .who{color:#f5a524}
 @keyframes flash{from{background:#f5a52433}to{background:transparent}}
 #streamDot{width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block;margin-right:6px}
 details.panel{margin:0 18px 16px;cursor:default}
 details.panel summary{cursor:pointer;font-size:11px;text-transform:uppercase;letter-spacing:1px;
   color:#7d8794;list-style:none;display:flex;align-items:center;gap:6px}
 details.panel summary::-webkit-details-marker{display:none}
 details.panel summary::before{content:'▸';transition:transform .15s}
 details.panel[open] summary::before{content:'▾'}
 details.panel .body{margin-top:10px;color:#9fb1c1;line-height:1.6;font-size:12.5px}
 details.panel .body b{color:#e6edf3}
 details.panel .body p{margin:0 0 10px}
</style></head><body>
<header><h1>⚔ DNDMCP</h1><span class=sub id=where>—</span><span id=flashcount>⚡ 0 Flash calls</span></header>
<details class=panel style="margin:16px 18px 0">
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
  It's a <b>stigmergic</b> system in the literal sense — coordination through traces left in
  the shared graph, not direct messages between whoever's generating content — the same
  mechanism ants use to build a colony without a blueprint or a foreman.</p>
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
  <div class=panel><h2>Character</h2><div class=ch id=char>—</div></div>
  <div class=panel><h2>Selected room</h2><div class=ch id=roomInfo><span class=empty>click a room on the map</span></div></div>
  <div class=panel><h2>Recent</h2><div class=log id=log></div></div>
 </aside>
</main>
<div style="padding:0 18px 18px">
 <div class=panel><h2><span id=streamDot></span>Live world stream — every player, every session</h2>
   <div id=stream><div class=empty>waiting for the world to move...</div></div></div>
</div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const params = new URLSearchParams(location.search);
const playerId = params.get('player');
const campaignId = params.get('campaign') || 'main';
const W=700, H=420;
function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

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
     g.append('circle').attr('r',16).attr('stroke-width',2);
     g.append('text').attr('class','label').attr('y',30).attr('text-anchor','middle')
       .attr('fill','#7d8794').attr('font-size',10);
     g.append('text').attr('class','count').attr('y',4).attr('text-anchor','middle')
       .attr('fill','#0b0e14').attr('font-size',10).attr('font-weight','bold');
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
   .attr('fill', d=> d.mine ? '#f5a524' : (d.visited ? '#3b82f6' : '#1f2937'))
   .attr('stroke', d=> d.mine ? '#fcd34d' : '#475569');
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
   .join('line').attr('stroke','#334155').attr('stroke-width',2);
 frontierLayer.selectAll('line').data(frontier, d=>d.from)
   .join('line').attr('stroke','#334155').attr('stroke-width',2).attr('stroke-dasharray','3,4');

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
  const worldTag = campaignId !== 'main' ? `[world: ${campaignId}] ` : '';
  const whereText = worldTag + (s.current_room ? ('You are in: '+(s.current_room.name||'')) : (playerId ? 'unknown player' : 'spectating — no ?player= in link'));
  document.getElementById('where').textContent = whereText;
  document.getElementById('whereInMap').textContent = whereText;
  renderGraph(s.rooms||[], s.players||[], s.you||null);
  const ch = s.character;
  const invHtml = (ch?.inventory||[]).map(it => {
    const name = typeof it === 'string' ? it : it.name;
    const desc = typeof it === 'string' ? '' : (it.description||'');
    return desc ? `<span class=item title="${esc(desc)}">${esc(name)}</span>` : `<span>${esc(name)}</span>`;
  }).join(', ');
  document.getElementById('char').innerHTML = ch? `<b>${esc(ch.name)}</b> <span>lvl ${ch.level} ${esc(ch.klass)}</span><br>HP ${ch.hp}/${ch.max_hp} · AC ${ch.ac}<br>${invHtml||'<span class=empty>empty-handed</span>'}`:'—';
  document.getElementById('log').innerHTML=(s.log||[]).map(l=>`<div>${l.text}</div>`).join('')||'<div class=empty>—</div>';
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
setInterval(tick,1500);tick();

// Live world stream — every domain event, from every player's session, pushed here as it
// happens. Deliberately unfiltered by playerId: this is the out-of-world view of the same
// stigmergic mechanic that shows up in-game as "Traces of those who came before."
const streamEl = document.getElementById('stream');
const streamDot = document.getElementById('streamDot');
const es = new EventSource('/stream/events?campaign='+encodeURIComponent(campaignId));
es.addEventListener('world-event', (e) => {
  const ev = JSON.parse(e.data);
  const empty = streamEl.querySelector('.empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.className = 'new';
  const who = ev.player_id ? `<span class=who>${esc(ev.player_id.slice(0,6))}</span> ` : '';
  div.innerHTML = `${who}${esc(ev.text)}`;
  streamEl.prepend(div);
  while (streamEl.children.length > 50) streamEl.lastChild.remove();
  setTimeout(() => div.classList.remove('new'), 900);
});
es.onerror = () => { streamDot.style.background = '#ef4444'; };
es.onopen = () => { streamDot.style.background = '#22c55e'; };
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


_EMPTY_STATE = {"rooms": [], "players": [], "character": None, "you": None, "current_room": None, "log": [], "flash_calls": 0}


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
            " AND kind IN ('room.generated','entity.spawned','npc.talked') AND text LIKE '%(flash)%'",
            (campaign_id,),
        ).fetchone()[0]
        return JSONResponse({"rooms": rooms, "players": players, "character": char,
                             "you": char, "current_room": (dict(cur) if cur else None), "log": log,
                             "flash_calls": flash_calls})
    except sqlite3.OperationalError:
        # schema not initialized yet — no one has called start_adventure on this pod yet
        return JSONResponse(_EMPTY_STATE)
    finally:
        c.close()


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
    everything else). Combine freely; each is AND'd in."""
    campaign_id = request.query_params.get("campaign") or "main"
    player_id = request.query_params.get("player_id")
    subject_type = request.query_params.get("subject_type")
    subject_id = request.query_params.get("subject_id")
    kind_prefix = request.query_params.get("kind_prefix")

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
    extra_where = (" AND " + " AND ".join(where)) if where else ""

    async def gen():
        last_seq = 0
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
