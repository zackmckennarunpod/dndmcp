"""Live state monitor — watch what's deployed on Flash + the active run log, in your browser.

Polls SERVER TRUTH (myself.endpoints) so it shows what's really live (not a stale local
cache), highlights FORGE-minted endpoints (what's burning $), and tails the active run log.

Run:
    MONITOR_LOG=/path/to/task.output FORGE_PROFILE=prod .venv/bin/python -m ui.monitor
    # then open http://localhost:8001
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import forge

app = FastAPI(title="FORGE monitor")
START = time.time()
FORGE_KEYS = ("embed-", "xsil", "wf-", "verify", "burst", "evolver", "selftest", "forge")


def _health(endpoint_id: str) -> dict:
    """Real live status from the serverless health API: worker states + job queue."""
    url = f"https://api.runpod.ai/v2/{endpoint_id}/health"
    req = urllib.request.Request(url, headers={"Authorization": os.environ.get("RUNPOD_API_KEY", "")})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.load(resp)
    except Exception:
        return {}


def _phase(workers: dict, jobs: dict) -> str:
    """Human-readable phase derived from the correctly-mapped health fields."""
    if workers.get("running", 0) or jobs.get("inProgress", 0):
        return "RUNNING"
    if workers.get("initializing", 0):
        return "cold-starting (worker initializing — installing deps / loading model)"
    if jobs.get("inQueue", 0) and not any(workers.get(k, 0) for k in
                                          ("idle", "initializing", "ready", "running")):
        return "queued — provisioning worker (cold start)"
    if workers.get("idle", 0) or workers.get("ready", 0):
        return "warm / idle"
    if jobs.get("inQueue", 0):
        return "queued"
    return "scaled to zero"

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>FORGE monitor</title>
<style>
 body{margin:0;font:13px ui-monospace,Menlo,monospace;background:#0b0e14;color:#e6edf3}
 header{padding:14px 20px;border-bottom:1px solid #222c3a;display:flex;gap:14px;align-items:baseline}
 h1{font-size:16px;margin:0}.sub{color:#7d8794;font-size:12px}
 main{padding:18px 20px;display:grid;gap:16px;max-width:900px}
 .panel{background:#141a24;border:1px solid #222c3a;border-radius:10px;padding:14px}
 .panel h2{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#7d8794;margin:0 0 10px}
 .ep{padding:9px 11px;border:1px solid #222c3a;border-radius:7px;margin-bottom:7px}
 .ep.mine{border-color:#f5a623;background:#1c1605}
 .ep.safe{opacity:.5}
 .ep .top{display:flex;justify-content:space-between;align-items:center}
 .tag{font-size:10px;padding:2px 7px;border-radius:5px}
 .tag.burn{background:#f5a623;color:#08111f;font-weight:700}
 .tag.ok{background:#22303f;color:#7d8794}
 .phase{font-size:12px;margin-top:6px}
 .phase.run{color:#3fb950}.phase.cold{color:#f5a623}.phase.zero{color:#7d8794}
 .states{display:flex;gap:10px;margin-top:6px;flex-wrap:wrap}
 .st{font-size:11px;color:#7d8794}.st b{color:#e6edf3}
 .st.hot b{color:#3fb950}.st.warn b{color:#f5a623}
 .big{font-size:22px;font-weight:700}
 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
 .live{background:#3fb950}.idle{background:#444}
 pre{white-space:pre-wrap;font-size:11px;color:#9fb1c1;max-height:300px;overflow:auto;margin:0}
</style></head><body>
<header><h1>FORGE monitor</h1><span class=sub id=clock></span></header>
<main>
 <div class=panel>
   <h2>Status</h2>
   <div class=big><span class="dot idle" id=dot></span><span id=state>—</span></div>
   <div class=sub id=summary></div>
 </div>
 <div class=panel><h2>Live Flash endpoints (server truth)</h2><div id=eps></div></div>
 <div class=panel><h2>Active run log</h2><pre id=log>—</pre></div>
</main>
<script>
async function tick(){
 try{
  const s=await (await fetch('/state')).json();
  document.getElementById('clock').textContent='monitoring '+s.uptime+'s';
  const mine=s.endpoints.filter(e=>e.mine);
  document.getElementById('dot').className='dot '+(mine.length?'live':'idle');
  document.getElementById('state').textContent=mine.length?(mine.length+' FORGE endpoint(s) LIVE'):'idle — nothing of ours running';
  document.getElementById('summary').textContent=s.endpoints.length+' total endpoints on account';
  document.getElementById('eps').innerHTML=s.endpoints.map(e=>{
    const w=e.workers||{}, j=e.jobs||{};
    const pcls=/RUNNING/.test(e.phase)?'run':/cold|queued|initial/.test(e.phase)?'cold':'zero';
    const states=e.mine?`<div class=states>
      <span class="st ${w.initializing?'warn':''}">init <b>${w.initializing||0}</b></span>
      <span class="st ${w.running?'hot':''}">running <b>${w.running||0}</b></span>
      <span class=st>ready <b>${w.ready||0}</b></span>
      <span class=st>idle <b>${w.idle||0}</b></span>
      <span class="st ${j.inQueue?'warn':''}">queued <b>${j.inQueue||0}</b></span>
      <span class=st>inProgress <b>${j.inProgress||0}</b></span>
      <span class=st>done <b>${j.completed||0}</b></span></div>`:'';
    return `<div class="ep ${e.mine?'mine':'safe'}">
      <div class=top><span>${e.name}</span>
      <span class="tag ${e.mine?'burn':'ok'}">${e.mine?'FORGE · cfg '+e.wmin+'/'+e.wmax:'not ours'}</span></div>
      ${e.mine?`<div class="phase ${pcls}">${e.phase}</div>`:''}${states}</div>`;
  }).join('')||'<div class=sub>none</div>';
  document.getElementById('log').textContent=s.log||'(no log)';
 }catch(e){document.getElementById('state').textContent='monitor error';}
}
setInterval(tick,2000);tick();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


@app.get("/state")
async def state() -> JSONResponse:
    forge.load_env(os.environ.get("FORGE_PROFILE", "prod"))
    try:
        eps = await forge.server_endpoints()
    except Exception as exc:
        return JSONResponse({"endpoints": [], "log": f"endpoint query error: {exc}",
                             "uptime": int(time.time() - START)})
    from . import diagnostics

    rows = []
    for e in eps:
        mine = any(k in e["name"] for k in FORGE_KEYS)
        row = {"name": e["name"], "wmin": e.get("workersMin"), "wmax": e.get("workersMax"),
               "mine": mine, "workers": {}, "jobs": {}, "phase": "",
               "build": "", "build_err": "", "log_tail": []}
        if mine:  # for OUR endpoints: health + build state + worker logs (the full picture)
            h = _health(e["id"])
            row["workers"] = h.get("workers", {})
            row["jobs"] = h.get("jobs", {})
            row["phase"] = _phase(row["workers"], row["jobs"])
            try:
                full = await diagnostics.endpoint_full(e["id"])
                builds = full.get("builds") or []
                if builds:
                    row["build"] = builds[0].get("state") or ""
                    row["build_err"] = (builds[0].get("error") or "")[:300]
                ai_key = full.get("aiKey")
                if ai_key:
                    log = diagnostics.fetch_logs(e["id"], ai_key, page_size=80)
                    lines = log.get("logs") if isinstance(log, dict) else None
                    if isinstance(lines, list):
                        row["log_tail"] = [
                            f"[{l.get('level','')}] {l.get('message','')}"[:200]
                            if isinstance(l, dict) else str(l)[:200]
                            for l in lines[-12:]
                        ]
            except Exception as exc:
                row["log_tail"] = [f"(log fetch error: {type(exc).__name__})"]
        rows.append(row)

    log = ""
    log_path = os.environ.get("MONITOR_LOG")
    if log_path and Path(log_path).is_file():
        lines = Path(log_path).read_text().splitlines()
        # KEEP the SLS worker-log signal (cold-start progress) — that's the useful part.
        SIGNAL = ("Installing", "Status:", "Delay Time", "Execution Time", "Started Job",
                  "===", "GPU", "minting", "teardown", "VIABILITY", "fastest", "cheapest",
                  "emb/sec", "$/", "FAILED", "ERROR", "->", "picked")
        NOISE = ("DeprecationWarning", "warnings.warn", "Retrying GraphQL")
        keep = [l for l in lines if l.strip() and not any(n in l for n in NOISE)
                and (any(s in l for s in SIGNAL) or not l.lstrip().startswith(("INFO", "2026")))]
        log = "\n".join(keep[-30:])
    return JSONResponse({"endpoints": rows, "log": log, "uptime": int(time.time() - START)})


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8001)), log_level="warning")


if __name__ == "__main__":
    main()
