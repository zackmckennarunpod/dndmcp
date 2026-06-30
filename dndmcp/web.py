"""DNDMCP web GUI — a live map of the world, synced to the game.

Reads the SAME SQLite the MCP server writes, so it auto-syncs: as the player moves (via MCP
tools), the DB updates and this map reflects it on the next poll. Served by the pod brain
alongside the MCP server. Shows the world graph (rooms placed by their path), current
position, character, and the log.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="DNDMCP map")

_DELTA = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}


def _db() -> sqlite3.Connection:
    state_dir = os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp"))
    c = sqlite3.connect(str(Path(state_dir) / "campaign.db"))
    c.row_factory = sqlite3.Row
    return c


def _coord(room_id: str) -> tuple[int, int]:
    """Place a room by walking the direction-path encoded in its id (r0:east:north…)."""
    x = y = 0
    for part in room_id.split(":")[1:]:
        dx, dy = _DELTA.get(part, (0, 0))
        x += dx; y += dy
    return x, y


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>DNDMCP — map</title>
<style>
 body{margin:0;background:#0b0e14;color:#e6edf3;font:13px ui-monospace,Menlo,monospace}
 header{padding:12px 18px;border-bottom:1px solid #222c3a;display:flex;gap:12px;align-items:baseline}
 h1{font-size:15px;margin:0}.sub{color:#7d8794;font-size:12px}
 main{display:grid;grid-template-columns:1fr 280px;gap:16px;padding:16px 18px}
 .panel{background:#141a24;border:1px solid #222c3a;border-radius:10px;padding:14px}
 .panel h2{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#7d8794;margin:0 0 10px}
 #map{font:14px/1.1 ui-monospace,Menlo,monospace;white-space:pre;overflow:auto}
 .ch b{color:#e6edf3}.ch span{color:#7d8794}
 .log div{color:#9fb1c1;padding:2px 0;border-bottom:1px solid #1a2230;font-size:12px}
 .empty{color:#7d8794}
</style></head><body>
<header><h1>⚔ DNDMCP</h1><span class=sub id=where>—</span></header>
<main>
 <div class=panel><h2>World map (synced to your session)</h2><div id=map class=empty>no adventure yet — start one in your agent</div></div>
 <aside style="display:flex;flex-direction:column;gap:16px">
  <div class=panel><h2>Character</h2><div class=ch id=char>—</div></div>
  <div class=panel><h2>Recent</h2><div class=log id=log></div></div>
 </aside>
</main>
<script>
function gridFromRooms(rooms, cur){
 if(!rooms.length) return 'no adventure yet';
 const xs=rooms.map(r=>r.coord[0]), ys=rooms.map(r=>r.coord[1]);
 const minx=Math.min(...xs),maxx=Math.max(...xs),miny=Math.min(...ys),maxy=Math.max(...ys);
 const W=maxx-minx, H=maxy-miny;
 // build cells
 const cell={}; rooms.forEach(r=>{cell[(r.coord[0])+','+(r.coord[1])]=r;});
 let out='';
 for(let y=miny;y<=maxy;y++){
   let row1='',row2='';
   for(let x=minx;x<=maxx;x++){
     const r=cell[x+','+y];
     if(r){
       const here=r.id===cur;
       row1+= here?'[@]':(r.visited?'[#]':'[ ]');
       row2+= r.exits.includes('east')?' — ':'   ';
     } else { row1+='   '; row2+='   '; }
   }
   out+=row1+'\\n';
   // vertical connectors
   let rowv='';
   for(let x=minx;x<=maxx;x++){
     const r=cell[x+','+y];
     rowv+= (r&&r.exits.includes('south'))?' | ':'   ';
   }
   out+=rowv+'\\n';
 }
 return out;
}
async function tick(){
 try{
  const s=await (await fetch('/state')).json();
  document.getElementById('where').textContent = s.current_room? ('You are in: '+(s.current_room.name||'')) : 'no active adventure';
  const m=document.getElementById('map');
  m.className=''; m.textContent=gridFromRooms(s.rooms||[], s.current_room&&s.current_room.id);
  const ch=s.character;
  document.getElementById('char').innerHTML = ch? `<b>${ch.name}</b> <span>lvl ${ch.level} ${ch.klass}</span><br>HP ${ch.hp}/${ch.max_hp} · AC ${ch.ac}<br><span>${(ch.inventory||[]).join(', ')}</span>`:'—';
  document.getElementById('log').innerHTML=(s.log||[]).map(l=>`<div>${l.text}</div>`).join('')||'<div class=empty>—</div>';
 }catch(e){}
}
setInterval(tick,1500);tick();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/state")
def state() -> JSONResponse:
    try:
        c = _db()
    except Exception:
        return JSONResponse({"rooms": [], "character": None, "current_room": None, "log": []})
    try:
        camp = c.execute("SELECT * FROM campaign WHERE id=1").fetchone()
        cur_id = camp["current_room"] if camp else None
        rooms = []
        for r in c.execute("SELECT * FROM rooms").fetchall():
            x, y = _coord(r["id"])
            rooms.append({"id": r["id"], "name": r["name"], "coord": [x, y],
                          "visited": bool(r["visited"]),
                          "exits": list(json.loads(r["exits"] or "{}").keys())})
        ch = c.execute("SELECT * FROM character WHERE id=1").fetchone()
        char = None
        if ch:
            char = dict(ch)
            char["inventory"] = json.loads(char["inventory"] or "[]")
        cur = c.execute("SELECT * FROM rooms WHERE id=?", (cur_id,)).fetchone() if cur_id else None
        log = [dict(r) for r in c.execute("SELECT text FROM log ORDER BY seq DESC LIMIT 8").fetchall()][::-1]
        return JSONResponse({"rooms": rooms, "character": char,
                             "current_room": (dict(cur) if cur else None), "log": log})
    finally:
        c.close()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("GUI_PORT", "8001")),
                log_level="warning")


if __name__ == "__main__":
    main()
