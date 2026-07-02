"""DNDMCP web GUI — a live map of the world, synced to the game.

Reads the SAME SQLite the MCP server writes, so it auto-syncs: as the player moves (via MCP
tools), the DB updates and this map reflects it on the next poll. Served by the pod brain
alongside the MCP server. Shows the world graph (rooms placed by their path), current
position, character, and the log.
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import html
import json
import logging
import os
import re
import subprocess
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from sse_starlette.sse import EventSourceResponse

from . import chat_sessions, dm_loop, pairing, server, worldgen
from .state import MAIN_CAMPAIGN_ID

app = FastAPI(title="DNDMCP map")
logger = logging.getLogger(__name__)

# Kill switch for the whole browser-DM chat pane (e0b.3) — default ON. Flip to "0" to pull it
# without a redeploy of the surrounding map/GUI: POST /chat 503s and GET /chat/enabled tells
# the page's own JS to hide the pane and fall back to BYO-agent-only, same idea as any other
# feature flag, just an env var since this app has no flag service of its own.
def _browser_dm_enabled() -> bool:
    return os.environ.get("DND_BROWSER_DM", "1") != "0"


MAX_CHAT_MESSAGE_LEN = 500  # see POST /chat — message-length cap.

# Abuse-guard responses (e0b.4), written in the DM's own voice — the chat UI renders these as
# a dim system line (not an error alert), see PAGE's addChatMessage('system', ...) branch.
RATE_LIMIT_MESSAGE = ('The DM raises a hand. "Slow down a moment — even a Dungeon Master '
                     'needs to breathe between turns." (try again in a few seconds)')
SESSION_CAP_MESSAGE = ('The DM closes the book gently. "This character\'s tale has reached '
                      'its telling for now — thank you for playing."')


def _log_dm_event(ip: str | None, campaign_id: str | None, kind: str, text: str) -> None:
    """Insert a domain-event log row the same raw-sqlite way /export_story does — world.log()
    (state.py's World, one connection per THREAD) isn't reachable from here for the identical
    reason /export_story already works around it: this module opens its own ad hoc connection
    straight against campaign.db (see _db()) rather than sharing World's thread-local one.
    Used for kind='dm.throttled' (see chat_sessions.check_ip_rate_limit /
    session_cap_exceeded, which decide WHEN to call this — only the first rejection of a
    throttle window, never every rejected request)."""
    try:
        c = _db()
        try:
            c.execute(
                "INSERT INTO log (ts,kind,text,player_id,subject_type,subject_id,campaign_id,ip)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), kind, text, None, None, None, campaign_id, ip),
            )
            c.commit()
        finally:
            c.close()
    except Exception:
        logger.exception("failed to log %s event", kind)


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
    # timeout=5: the game process writes constantly; without a busy timeout a read that
    # lands mid-write surfaces as "database is locked" and blanks the map for that poll.
    # WAL (set persistently by state.World's connections) makes these reads non-blocking
    # in the common case; the timeout covers the rest.
    c = sqlite3.connect(str(Path(state_dir) / "campaign.db"), timeout=5)
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
<link rel=icon href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚔</text></svg>">
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
    column next to the map, sized to match (see #map's height). Plain column (not reversed):
    newest event lands at the TOP via prepend() below, so the feed reads top-down in the
    order things actually happened, with the newest visible without any scrolling. */
 #stream{display:flex;flex-direction:column;gap:0;height:560px;overflow-y:auto}
 /* Roomier rows (was padding:2px, which crammed wrapped two-line events against their
    neighbors' border lines) — 6px breathing room + a line-height that keeps a wrapped
    entity-highlighted line readable. */
 #stream div{color:var(--muted);padding:6px 0;border-bottom:1px solid var(--border-soft);
   font-size:12px;line-height:1.5}
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
 /* Middle panel's own tab group (Live world stream / Play here) — deliberately NOT the same
    .tabbar/.tabBtn/.tabBody classes as the bottom Browse/How-this-works group: that group's
    showTab() queries ALL .tabBtn/.tabBody elements on the page and forcibly toggles 'active'
    off anything whose data-tab doesn't match, which would silently kill this panel's own tab
    state every time someone clicks Browse or How-it-works. Same visual language, independent
    wiring (see showMidTab() below). */
 .midTabbar{display:flex;gap:2px;border-bottom:1px solid var(--border);margin-bottom:10px}
 .midTabBtn{background:none;border:none;color:var(--muted);font-size:11px;text-transform:uppercase;
   letter-spacing:1.5px;padding:0 10px 8px;cursor:pointer;border-bottom:2px solid transparent;
   font-family:'IBM Plex Mono',monospace;transition:color .15s,border-color .15s}
 .midTabBtn:hover{color:var(--text)}
 .midTabBtn.active{color:var(--ghost-bright);border-bottom-color:var(--ghost)}
 .midTabBody{display:none}
 .midTabBody.active{display:block}
 /* #midTab-chat needs display:flex (column, so the input row pins to the bottom and the
    message list is the only part that scrolls) instead of plain display:block — an ID
    selector always outranks the two-class .midTabBody.active rule above regardless of
    stylesheet order, so this wins without needing !important. */
 #midTab-chat.active{display:flex;flex-direction:column}
 #chatLog{flex:1;overflow-y:auto;margin-bottom:8px;display:flex;flex-direction:column;gap:1px}
 .chatMsg{padding:4px 2px;font-size:12.5px;line-height:1.55;word-wrap:break-word}
 .chatMsg.player{text-align:right;color:var(--ghost-bright)}
 .chatMsg.dm{text-align:left;color:var(--text)}
 .chatMsg.error{text-align:left;color:var(--warm)}
 /* Throttle/cap responses (e0b.4) are expected, routine traffic-shaping, not a failure —
    dim + italic like a breadcrumb, deliberately NOT the same alarmed .error color above. */
 .chatMsg.system{text-align:left;color:var(--dim);font-style:italic}
 .chatBreadcrumb{color:var(--muted);font-size:11px;padding:2px 2px;opacity:.85;font-style:italic}
 #chatForm{display:flex;gap:6px;flex-shrink:0}
 #chatInput{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:6px;
   padding:8px 10px;color:var(--text);font:12.5px 'IBM Plex Mono',monospace}
 #chatInput:disabled{opacity:.6}
 #chatInput:focus{outline:none;border-color:var(--ghost)}
 #chatSendBtn{background:var(--link);color:var(--ghost-bright);border:1px solid var(--border);
   border-radius:6px;padding:8px 16px;font:600 12px 'IBM Plex Mono',monospace;cursor:pointer;flex-shrink:0}
 #chatSendBtn:hover{background:var(--visited)}
 #chatSendBtn:disabled{opacity:.6;cursor:default}
 .choiceBtn{background:var(--link);color:var(--ghost-bright);border:1px solid var(--border);
   border-radius:6px;padding:5px 12px;font:600 11.5px 'IBM Plex Mono',monospace;cursor:pointer}
 .choiceBtn:hover{background:var(--visited)}
 .choiceBtn:disabled{opacity:.6;cursor:default}
 .choiceInput{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:6px;
   padding:5px 8px;color:var(--text);font:11.5px 'IBM Plex Mono',monospace}
 /* Onboarding wizard (e0b.12) — a modal stepper over the map, REPLACING the old inline
    choice card above the chat input AND the always-open connect <details> panel (its
    install-command content now lives in step 2B below). Compact by design: the whole flow
    must fit a 1440x900 viewport with no scrolling, so padding/type stay tight throughout. */
 #playBtn{background:var(--warm);color:#1a1206;border:none;border-radius:6px;padding:6px 14px;
   font:700 12.5px 'IBM Plex Mono',monospace;cursor:pointer;transition:transform .1s}
 #playBtn:hover{transform:scale(1.05)}
 .wizardOverlay{position:fixed;inset:0;background:rgba(6,4,14,.72);z-index:100;
   display:none;align-items:center;justify-content:center;padding:24px;box-sizing:border-box}
 /* click-outside-to-dismiss (see the overlay's own click listener) relies on the click target
    being THIS element and not the modal card floating inside it, hence modal has its own
    background/position rather than being transparent over the overlay. */
 #wizardModal{position:relative;width:100%;max-width:600px;max-height:84vh;overflow-y:auto;
   padding:20px 26px 22px}
 #wizardModal h2{font:600 15px 'IBM Plex Mono',monospace;color:var(--ghost-bright);margin:0 0 12px}
 #wizardCloseBtn{position:absolute;top:10px;right:12px;background:none;border:none;
   color:var(--muted);font-size:17px;line-height:1;cursor:pointer;padding:4px}
 #wizardCloseBtn:hover{color:var(--text)}
 .wizStep{display:none}
 .wizBackBtn{background:none;border:none;color:var(--muted);font:600 11px 'IBM Plex Mono',monospace;
   letter-spacing:.03em;cursor:pointer;padding:0;margin-bottom:12px}
 .wizBackBtn:hover{color:var(--text)}
 .wizBigBtn{display:block;width:100%;box-sizing:border-box;text-align:left;background:var(--bg);
   border:1px solid var(--border-soft);border-radius:8px;padding:11px 14px;margin-bottom:9px;
   color:var(--ghost-bright);font:600 13px 'IBM Plex Mono',monospace;cursor:pointer}
 .wizBigBtn:hover{border-color:var(--ghost)}
 .wizBigBtn .wizSub{display:block;color:var(--muted);font-weight:400;font-size:11px;margin-top:3px}
 .wizRecommended{display:inline-block;margin-left:8px;padding:1px 7px;border-radius:10px;
   background:var(--warm);color:#1a1206;font:700 9.5px 'IBM Plex Mono',monospace;
   letter-spacing:.03em;vertical-align:middle}
 /* Deliberately a much quieter style than .wizBigBtn — the browser-play option is the
    fallback for "no agent set up", not a coequal choice, so it shouldn't visually compete
    with the recommended agent path above it (user request: deprioritize it). */
 .wizMinorBtn{display:block;width:100%;box-sizing:border-box;text-align:left;background:none;
   border:1px solid transparent;border-radius:8px;padding:7px 14px;margin-top:2px;
   color:var(--muted);font:600 11.5px 'IBM Plex Mono',monospace;cursor:pointer}
 .wizMinorBtn:hover{color:var(--text);border-color:var(--border-soft)}
 .wizMinorBtn .wizSub{display:block;color:var(--muted);font-weight:400;font-size:10.5px;margin-top:2px}
 .wizFriendRow{background:var(--bg);border:1px solid var(--border-soft);border-radius:8px;
   padding:11px 14px;color:var(--ghost-bright);font:600 13px 'IBM Plex Mono',monospace}
 .wizFriendRow .wizJoinInputRow{display:flex;gap:6px;margin-top:8px}
 .wizPairCode{font:700 22px 'IBM Plex Mono',monospace;color:var(--warm-bright);letter-spacing:1.5px;
   background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 12px;
   display:inline-block;margin:6px 0 4px}
 #wizJoinFeed{max-height:64px;overflow-y:auto;font-size:11.5px;color:var(--muted);margin-top:4px}
 #wizJoinFeed div{padding:2px 0}
 /* RESPONSIVE (the real target: half-screen — agent terminal on one side, this UI on the
    other). The 3-column grid only fits ~1280px+; below that the map got crushed. Two tiers:
    - <=1280px (half a 1440p/1080p monitor, tablets): 2 columns — map keeps the left and
      spans both rows; stream/chat + the character sidebar stack in the right column.
    - <=760px (phones, very narrow splits): single column, map first at a viewport-relative
      height so the panels below are reachable without scrolling past a 560px wall.
    #map/#stream keep FIXED heights per tier (see the circular-layout comment below) — only
    the tier changes them, never the grid row. The map's own ResizeObserver re-fits the
    viewBox whenever these kick in. */
 @media (max-width: 1280px){
  main{grid-template-columns:minmax(0,1fr) minmax(280px,340px)}
  main > .panel:first-child{grid-row:1 / span 2}
  main > aside{grid-column:2}
 }
 @media (max-width: 760px){
  main{grid-template-columns:minmax(0,1fr)}
  main > .panel:first-child{grid-row:auto}
  main > aside{grid-column:auto}
  #map{height:46vh}
  #stream{height:38vh}
  #chatLog{height:38vh}
  header{flex-wrap:wrap;row-gap:4px}
  #flashcount{margin-left:0}
 }
</style></head><body>
<div id=staleBanner>⟳ This tab is running an older version of the page — <a href="#" onclick="location.reload();return false">refresh to update</a></div>
<div id=itemTooltip></div>
<header><h1>⚔ DNDMCP</h1><span class=sub id=where>—</span>
 <button id=playBtn type=button title="Start here — the wizard walks you through every way to play">▶ Play</button>
 <span id=flashcount>⚡ 0 Flash calls</span>
 <span id=metricsLink title="Click to see system-wide metrics for this world">📊 Metrics</span>
 <button id=shareBtn title="Copies instructions to paste into your agent (Claude Code/Desktop) running dndmcp">🔗 Share</button></header>
<!-- Onboarding wizard (e0b.12) — a modal stepper that owns ALL onboarding now: replaces the
     old always-open connect <details> panel (content absorbed into step 2B below) AND the
     inline choice card that used to sit above the chat input (see midTab-chat, which no
     longer renders one). Hidden by default; opened by #playBtn or auto-opened once for a
     cold visitor with no character yet (see the wizard JS below for the exact probe). The
     map/stream stay visible and live behind it the whole time -- this is a modal OVER the
     page, not a separate view. -->
<div id=wizardOverlay class=wizardOverlay>
 <div id=wizardModal class=panel role=dialog aria-modal=true aria-label="Onboarding wizard">
  <button id=wizardCloseBtn type=button title="Close (Esc)">✕</button>
  <div id=wizStep1 class=wizStep>
   <h2>How do you want to play?</h2>
   <div class=sub style="margin:0 0 10px">We recommend playing through your own agent — it
    talks to the real MCP server with full tool access, and a stronger model makes a much
    better Dungeon Master. Don't have an agent set up, or not sure how? Play right here in
    the browser instead — zero setup, just simpler storytelling from a smaller built-in model.</div>
   <button id=wizAgentBtn class=wizBigBtn type=button title="Recommended — your agent's model (e.g. Claude) is far stronger than the built-in narrator, so you get a much better Dungeon Master">🖥 Through your own agent <span class=wizRecommended>★ Recommended</span>
    <span class=wizSub>Claude Code / Claude Desktop — the full MCP server, a stronger model runs your game</span></button>
   <button id=wizBrowseBtn class=wizMinorBtn type=button style="display:none" title="No agent set up? This works instantly with zero setup — but the built-in narrator runs on a small model, so the storytelling is simpler">💬 Right here in the browser instead
    <span class=wizSub>no agent, or don't know how to set one up? play instantly in this tab</span></button>
  </div>
  <div id=wizStep2a class=wizStep>
   <button class=wizBackBtn type=button data-wizback>← back</button>
   <h2>Which world?</h2>
   <button id=wizSharedBtn class=wizBigBtn type=button>⚔ This shared world</button>
   <button id=wizCreateBtn class=wizBigBtn type=button>🌱 Create my own world
    <span class=wizSub>yours to shape — share the link with friends</span></button>
   <div id=wizFriendRow class=wizFriendRow>🔗 A friend's world
    <div class=wizJoinInputRow>
     <input id=wizFriendInput class=choiceInput placeholder="paste a world id">
     <button id=wizFriendGoBtn class=choiceBtn type=button>Go</button>
    </div>
   </div>
  </div>
  <div id=wizStep2c class=wizStep>
   <button id=wizStep2cBackBtn class=wizBackBtn type=button>← back</button>
   <h2>Build your world</h2>
   <div class=body>
    <p class=sub style="margin:0 0 8px">Describe a theme or premise — or leave it blank and
     we'll surprise you.</p>
    <textarea id=wizCreateThemeInput class=choiceInput style="width:100%;min-height:60px;resize:vertical;box-sizing:border-box"
     placeholder="e.g. a fishing village that made a pact with something in the tide..."></textarea>
    <div class=wizJoinInputRow style="margin-top:8px">
     <input id=wizCreateNameInput class=choiceInput placeholder="character name (optional)">
     <input id=wizCreateClassInput class=choiceInput placeholder="class (optional)">
    </div>
    <button id=wizCreateGoBtn class=choiceBtn type=button style="margin-top:10px;width:100%">🌱 Create this world</button>
    <div id=wizCreateStatus class=sub style="margin-top:8px;display:none">Building your world —
     this can take up to a minute (the world, your character, and the first room are all
     generated fresh) — you'll land in the live chat once it's ready.</div>
   </div>
  </div>
  <div id=wizStep2b class=wizStep>
   <button class=wizBackBtn type=button data-wizback>← back</button>
   <h2>Connect your agent</h2>
   <div class=body>
    <p class=sub style="margin:0 0 4px"><b>Claude Code — any OS:</b> one command, done:</p>
    <div class=codebox><code id=codeCCWin>claude mcp add --transport http dndmcp -s user "https://ldghdgi0xxn6jj-8000.proxy.runpod.net/mcp"</code><button class=copyCodeBtn data-target=codeCCWin>Copy</button></div>
    <p class=sub style="margin:10px 0 4px">or the shell one-liner (macOS/Linux/WSL):</p>
    <div class=codebox><code id=codeCC>curl -fsSL https://ldghdgi0xxn6jj-8002.proxy.runpod.net/install.sh | bash</code><button class=copyCodeBtn data-target=codeCC>Copy</button></div>
    <p class=sub style="margin:10px 0 4px"><b>Claude Desktop:</b> paste into <code>claude_desktop_config.json</code>, restart the app.</p>
    <div class=codebox><pre id=codeCD>{
  "mcpServers": {
    "dndmcp": {
      "type": "http",
      "url": "https://ldghdgi0xxn6jj-8000.proxy.runpod.net/mcp"
    }
  }
}</pre><button class=copyCodeBtn data-target=codeCD>Copy</button></div>
    <p style="margin:10px 0">Reconnect (Claude Code: <code>/mcp</code>; Desktop: restart), then say
     <b>"start an adventure."</b> That's it — your agent becomes the Dungeon Master.</p>
    <div id=wizPairSection>
     <button id=wizMintBtn class=choiceBtn type=button>Get my pairing code</button>
     <div id=wizPairCodeBox style="display:none">
      <div id=wizPairCode class=wizPairCode>—</div>
      <div class=sub>tell your agent: "start an adventure, pairing code <code id=wizPairCodeInline>—</code>"</div>
      <div id=wizPairStatus class=sub style="margin-top:4px">watching for your agent to connect...</div>
      <a id=wizPairMapLink class=choiceBtn style="display:none;text-decoration:none;margin-top:6px" href="#">🗺 See my character on the map</a>
      <button id=wizPairAdoptBtn class=choiceBtn style="display:none;margin-top:6px" title="Bind this character to this browser too — game state carries over; your agent's chat history stays in your agent. One narrator at a time.">💬 Also play them here in the browser</button>
     </div>
    </div>
    <div class=sub style="margin-top:12px">📡 or watch for new adventurers:</div>
    <div id=wizJoinFeed><div class=empty>watching for new adventurers...</div></div>
   </div>
  </div>
 </div>
</div>
<main style="margin-top:16px">
 <div class=panel><h2 id=mapTitle>World map (shared, live)</h2><div class=sub id=mapExplainer style="display:none;margin-bottom:2px">one persistent world everyone shares — other players' ghosts have already passed through it</div><div class=sub id=whereInMap style="margin-bottom:2px">—</div><div class=sub style="margin-bottom:4px;opacity:.85">
  <span style="color:var(--ghost)">●</span> your room &nbsp;
  <span style="color:var(--visited)">●</span> places you've been &nbsp;
  <span style="color:var(--dim)">●</span>&thinsp;??? not yet discovered &nbsp;
  <b style="color:var(--text)">2</b> = players in that room now</div>
<div class=sub style="margin-bottom:8px;opacity:.6">scroll + ⌘/Ctrl to zoom · drag to pan · click a room for details</div><div id=map><span id=mapEmpty class=empty>no adventure yet — start one in your agent</span><div id=nodeTooltip></div></div></div>
 <div class=panel>
  <div class=midTabbar>
   <button class=midTabBtn data-miditab=stream>Live world stream</button>
   <!-- Hidden until GET /chat/enabled confirms the kill switch (DND_BROWSER_DM) is on — see
        the script below. Absent that check, a visitor could click into a pane whose backend
        is off and only find out on their first send. -->
   <button class=midTabBtn id=chatTabBtn data-miditab=chat style="display:none">🎲 Play here</button>
  </div>
  <div id=midTab-stream class="midTabBody active">
   <!-- No repeated title here — the tab button directly above IS the title. One compact
        status row: connection dot + scope + filter. streamTitle stays as a (visually
        hidden-ish) span only because the Flash-only toggle rewrites it. -->
   <div class=sub style="margin-bottom:8px;display:flex;align-items:center;gap:6px">
    <span id=streamDot></span><span id=streamTitle style="display:none">Live world stream</span>
    <span id=streamSub>every player, every session</span>
    <button id=streamFilterBtn style="margin-left:auto">⚡ Flash calls only</button></div>
   <div id=stream><div class=empty>waiting for the world to move...</div></div>
  </div>
  <div id=midTab-chat class=midTabBody>
   <div class=sub style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
    <!-- Persistent "which world am I in" label (e0b.10) — updated every tick() from /state's
         campaign data, visible whether or not a character exists yet. Fixes a live prod
         confusion: a player on a friend's world's page had no on-screen confirmation of
         which world the chat pane itself was scoped to. -->
    <span id=chatWorldLabel>Playing in: —</span>
    <a href="#" id=chatResetBtn title="Start over with a brand-new character — your current one stays in the world"
      style="color:var(--muted);text-decoration:underline dotted;white-space:nowrap;margin-left:8px">↺ new character</a>
    <a href="#" id=chatHandoffBtn title="Reveal this character's player_id so your own agent (Claude Code/Desktop) can take over playing them"
      style="color:var(--muted);text-decoration:underline dotted;white-space:nowrap;margin-left:8px">🖥 use my agent</a>
   </div>
   <div id=chatLog><div class=empty>say "start an adventure" to begin</div></div>
   <form id=chatForm>
    <input id=chatInput type=text autocomplete=off maxlength=500
      placeholder='say &quot;start an adventure&quot; to begin'>
    <button id=chatSendBtn type=submit>Send</button>
   </form>
  </div>
 </div>
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
  <p>You're reaching all of this through <b>MCP</b> (Model Context Protocol) — the same open
  standard your own agent uses for every tool call. The server exposes mechanics as typed
  tools that return facts, never prose: a dice roll, an HP total, what's actually in a room.
  The agent narrates from those facts; it never touches state directly, so nothing you read
  was invented to sound good — it's downstream of something real. Even the Dungeon Master
  persona ships through MCP's own <b>instructions</b> field: connecting doesn't just grant
  tools, it assigns your agent a role for the session. And because MCP tools don't care which
  transport carries them, this exact server runs solo over stdio and shared over HTTP on this
  live pod with zero duplicated logic — a private local game and the world you're watching
  right now are the same code path.</p>
  <p>That MCP-native shape carries into how this gets built, too: it's a long-running process,
  not re-read per call, so a code change means reconnecting the session, not restarting a
  server — and whoever's developing it plays through the same tool calls you do, no separate
  admin path. Even the pod this world runs on got provisioned that way: an agent using
  MCP-exposed infrastructure tooling to look up the right API shapes and deploy it, rather than
  a web console. The tooling you're using to read this is also, one layer down, how the thing
  you're reading about got made.</p>
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

// Map title copy (e0b.12): main gets the "this is the real shared world" explainer; any other
// world keeps the plain title -- its own [world: <id>] tag already shows up in #whereInMap
// every tick() (see the worldTag logic below), so it doesn't need a second static label here.
if (campaignId === 'main') {
  document.getElementById('mapTitle').textContent = 'World map — the main shared world';
  document.getElementById('mapExplainer').style.display = '';
}

// Per-world chat state (e0b.10, trimmed for e0b.12 -- the world-selection DECISION itself now
// lives entirely in the onboarding wizard below, not an inline card above the chat input).
// Declared here (top of script, ahead of tick()'s own first synchronous call a bit further
// down) rather than down near the rest of the chat-pane wiring — those `const`s aren't
// initialized yet the FIRST time tick() runs (tick() is invoked once immediately, before the
// script has finished executing top to bottom), so anything tick() touches on a cold call has
// to already exist by here.
let chatStarted = false;      // true once the player has taken ANY action this page-load --
                               // a wizard choice that lands in chat, or typing into the input
                               // directly. Gates the wizard's own one-time auto-open (see
                               // maybeAutoOpenWizard below) the same way it used to gate the
                               // old inline choice card.
let newWorldPending = false;  // "Create my own world" was clicked in the wizard -- sent as
                               // new_world:true on exactly the NEXT /chat POST only, then
                               // cleared regardless of the response (see chatForm's submit
                               // handler).
let lastCampaignTheme = null; // updated every tick() from /state's campaign.theme -- lets the
                               // wizard's step 2A label a non-main world by name instead of
                               // just its opaque id.

// --- Onboarding wizard (e0b.12) --------------------------------------------------------------
// A modal stepper over the map: replaces the old inline choice card (immediately above) AND
// the always-open connect <details> panel (its install-command content now lives in step 2B's
// static HTML). Opened by the header's #playBtn, or once automatically for a cold visitor (see
// maybeAutoOpenWizard). ESC / click-outside / the ✕ button all dismiss it back to spectating,
// same "the game stays visible behind it" idea the task describes.
let wizardOpen = false;
const WIZ_STEPS = ['wizStep1', 'wizStep2a', 'wizStep2b', 'wizStep2c'];
const WIZARD_DISMISSED_KEY = 'dndmcp_wizard_dismissed';

function showWizStep(id){
  WIZ_STEPS.forEach(s => { document.getElementById(s).style.display = (s === id) ? 'block' : 'none'; });
}
function openWizard(step){
  document.getElementById('wizardOverlay').style.display = 'flex';
  wizardOpen = true;
  showWizStep(step || 'wizStep1');
}
function closeWizard(){
  document.getElementById('wizardOverlay').style.display = 'none';
  wizardOpen = false;
  // ANY dismissal path (✕ / Esc / click-outside / a real in-wizard choice) counts as "seen
  // it" -- a cold visitor who explicitly closed it once should never be nagged again on this
  // browser, per the task's own "no dismissal remembered... remember in localStorage" note.
  try{ localStorage.setItem(WIZARD_DISMISSED_KEY, '1'); }catch(e){}
}
document.getElementById('playBtn').addEventListener('click', () => openWizard('wizStep1'));
document.getElementById('wizardCloseBtn').addEventListener('click', () => closeWizard());
document.getElementById('wizardOverlay').addEventListener('click', (e) => {
  if (e.target.id === 'wizardOverlay') closeWizard();  // click on the backdrop, not the modal card
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && wizardOpen) closeWizard();
});
// wizStep2c is excluded here (own listener below) -- it's TWO hops from wizStep1
// (wizStep1 -> wizStep2a -> wizStep2c), so the generic "back always means wizStep1" behavior
// every OTHER step relies on would wrongly skip past "Which world?" for this one.
document.querySelectorAll('.wizBackBtn:not(#wizStep2cBackBtn)').forEach(
  b => b.addEventListener('click', () => showWizStep('wizStep1')));
document.getElementById('wizStep2cBackBtn').addEventListener('click', () => showWizStep('wizStep2a'));

document.getElementById('wizAgentBtn').addEventListener('click', () => showWizStep('wizStep2b'));
document.getElementById('wizBrowseBtn').addEventListener('click', () => {
  const label = campaignId === 'main' ? '⚔ This shared world'
    : `⚔ This world: ${esc(lastCampaignTheme || campaignId)}`;
  document.getElementById('wizSharedBtn').textContent = label;
  showWizStep('wizStep2a');
});
document.getElementById('wizSharedBtn').addEventListener('click', () => {
  closeWizard();
  chatStarted = true;
  showMidTab('chat');
  const input = document.getElementById('chatInput');
  if (input) input.focus();
});
document.getElementById('wizCreateBtn').addEventListener('click', () => showWizStep('wizStep2c'));
document.getElementById('wizCreateGoBtn').addEventListener('click', () => {
  const theme = (document.getElementById('wizCreateThemeInput').value || '').trim();
  const name = (document.getElementById('wizCreateNameInput').value || '').trim();
  const klass = (document.getElementById('wizCreateClassInput').value || '').trim();
  let msg = theme || 'surprise me — invent an evocative theme and premise';
  if (name || klass) {
    msg += ` My character: ${name || 'invent a name'}${klass ? ', a ' + klass : ''}.`;
  }
  // Reuses the EXACT same /chat + new_world:true contract the free-text flow already used
  // (see chatForm's submit handler below) — requestSubmit() fires that handler's real
  // NDJSON/redirect-on-world-event logic verbatim, so this form is just a structured front
  // end for it, not a second implementation to keep in sync.
  newWorldPending = true;
  closeWizard();
  chatStarted = true;
  showMidTab('chat');
  // addChatMessage/chatForm/chatInput are hoisted/const-declared further down in the script
  // but already initialized by the time ANY click handler can fire (the whole script has
  // finished its top-level run before a user can click anything) — same safe pattern
  // wizSharedBtn's own handler above already relies on.
  addChatMessage('system', 'Building your world — this can take up to a minute (the world, '
    + 'your character, and the first room are all generated fresh)...');
  const input = document.getElementById('chatInput');
  input.value = msg;
  chatForm.requestSubmit();
});
function goToFriendWorld(){
  const id = (document.getElementById('wizFriendInput').value || '').trim();
  if (id) location.href = '/?campaign=' + encodeURIComponent(id);
}
document.getElementById('wizFriendGoBtn').addEventListener('click', goToFriendWorld);
document.getElementById('wizFriendInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') goToFriendWorld();
});

// Auto-open once per browser (not per pageview -- see WIZARD_DISMISSED_KEY) for a cold
// visitor: no character yet in THIS world, and this browser has never dismissed the wizard
// before. Checked exactly once, from the very first /state poll (see tick() below) -- a
// character created LATER in the same pageview must not retroactively pop the wizard back
// open, so this never re-evaluates after that first check.
let wizardAutoOpenChecked = false;
function maybeAutoOpenWizard(s){
  if (wizardAutoOpenChecked) return;
  wizardAutoOpenChecked = true;
  if (chatStarted) return;
  try{ if (localStorage.getItem(WIZARD_DISMISSED_KEY)) return; }catch(e){}
  if (!s.you) openWizard('wizStep1');
}

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

// Middle panel's OWN tab group (Live world stream / Play here) — deliberately separate
// function/classes from showTab() above (see the .midTabBtn CSS comment for why reusing
// .tabBtn/.tabBody would silently break this one every time the bottom tab group is clicked).
function showMidTab(name){
  document.querySelectorAll('.midTabBtn').forEach(b => b.classList.toggle('active', b.dataset.miditab === name));
  document.querySelectorAll('.midTabBody').forEach(el => el.classList.toggle('active', el.id === 'midTab-' + name));
}
document.querySelectorAll('.midTabBtn').forEach(b => b.addEventListener('click', () => showMidTab(b.dataset.miditab)));

// Kill switch check (DND_BROWSER_DM): only reveal the "Play here" tab (and the wizard's
// browser-play option) once the server confirms browser play is actually on. Fails closed
// (both stay hidden) on any fetch error. The wizard now owns the landing experience (e0b.12):
// no more auto-selecting the chat tab here for a cold visit — that would fight the wizard's
// own auto-open (see maybeAutoOpenWizard), which is what decides "type here to play" vs
// spectating vs BYO-agent now.
fetch('/chat/enabled').then(r => r.json()).then(d => {
  if (!d.enabled) return;
  // The tab is ALWAYS available once browser play is on — a visitor may want to play here
  // regardless of how they arrived. ?player= means this tab is the companion map for someone
  // already playing through their own MCP agent, so the chat carries a note that playing
  // here is a separate browser character, not a handle on their agent's session.
  document.getElementById('chatTabBtn').style.display = '';
  document.getElementById('wizBrowseBtn').style.display = '';
  if (playerId) {
    addChatMessage('system', "heads up: you're watching an agent-driven session — playing " +
      'here starts a separate browser character, not control of that one.');
  }
}).catch(() => {});

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
let _roomInfoOpenId = null;  // guards the async regen callback below against a stale write
                              // if the player clicks a DIFFERENT room before it resolves
function showRoomInfo(d){
  _roomInfoOpenId = d.id;
  const el = document.getElementById('roomInfo');
  if(!d.discovered){ el.innerHTML = '<span class=empty>??? — not discovered yet</span>'; return; }
  const img = d.image_ref
    ? `<img src="/art/${encodeURIComponent(d.image_ref)}.png" style="width:100%;border-radius:6px;image-rendering:pixelated">`
    : '';
  const feats = (d.features||[]).map(f => `<div>• ${esc(f)}</div>`).join('');
  const monsters = (d.contents||[]).filter(c=>c.type==='monster')
    .map(c => `<div>⚔ ${esc(c.name)} (HP ${c.hp})</div>`).join('');
  const loot = (d.contents||[]).filter(c=>c.type==='loot')
    .map(c => `<div>✦ ${esc(c.name)}</div>`).join('');
  el.innerHTML = `${img}<b>${esc(d.name)}</b><br><span>${esc(d.description||'')}</span>${feats}${monsters}${loot}`;
  // On-demand backstop: art otherwise only ever generates once, at room creation — if that
  // one speculative attempt transiently fails (observed live: a brief GPU-allocation hiccup
  // can silently drop a room's art forever), nothing else ever revisits it. Safe to fire on
  // every open: /art/regen no-ops server-side if the room already has art.
  if(!d.image_ref){
    fetch('/art/regen', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({room_id: d.id, campaign: campaignId})})
      .then(r => r.json()).then(res => {
        if(res.image_ref && _roomInfoOpenId === d.id){
          d.image_ref = res.image_ref;
          showRoomInfo(d);
        }
      }).catch(()=>{});
  }
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
   n.image_ref = r.image_ref;
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
  // Name WHO you are, not just where — a cookie-resumed character otherwise appears
  // silently in the panels ("who is this??" — observed user confusion): the resume is a
  // feature, but only if the page SAYS a resume happened.
  const whereText = worldTag + (s.current_room
    ? ('Playing as ' + ((s.you && s.you.name) || 'your character') + ' — in ' + (s.current_room.name||''))
    : (playerId ? 'unknown player' : 'Spectating — hit ▶ Play to join, or connect your own agent'));
  document.getElementById('where').textContent = whereText;
  document.getElementById('whereInMap').textContent = whereText;
  // First poll that finds a resumed character while the chat pane still shows its cold
  // "say start an adventure" hint: replace it with an explicit welcome-back so all three
  // panels (header, character card, chat) tell the SAME story about who you are. Direct
  // getElementById on purpose — tick() runs once synchronously BEFORE the chat section's
  // consts (chatLog etc.) initialize, and touching a TDZ const here would silently kill
  // this whole first render inside tick's catch.
  if (s.you && s.you.name && !chatWelcomedBack) {
    const logEl = document.getElementById('chatLog');
    if (logEl && logEl.querySelector('.empty')) {
      chatWelcomedBack = true;
      logEl.innerHTML = '';
      const wb = document.createElement('div');
      wb.className = 'chatMsg system';
      wb.textContent = 'welcome back, ' + s.you.name + ' — your character here resumed ' +
        'automatically. say "look around" to pick up where you left off, or hit ↺ new ' +
        'character to start fresh.';
      logEl.appendChild(wb);
    }
  }
  renderGraph(s.rooms||[], s.players||[], s.you||null);
  rebuildHighlightIndex(s);
  const camp = s.campaign;
  lastCampaignTheme = camp && camp.theme;
  document.getElementById('worldInfo').innerHTML = camp
    ? `<b>${esc(camp.theme||'')}</b>${camp.name?` — <span>${esc(camp.name)}</span>`:''}<br>${esc(camp.premise||'')}`
    : '<span class=empty>no world seeded yet</span>';
  // Onboarding wizard (e0b.12): auto-open once for a cold visitor, checked from this first
  // real /state payload (needs s.you to know whether this browser already has a character
  // here) -- see maybeAutoOpenWizard's own docstring for the exact one-shot probe.
  maybeAutoOpenWizard(s);
  const worldLabelTheme = (camp && camp.theme) || (campaignId === 'main' ? 'the shared world' : 'this world');
  document.getElementById('chatWorldLabel').textContent = campaignId === 'main'
    ? `Playing in: ${worldLabelTheme} (main)`
    : `Playing in: ${worldLabelTheme} (${campaignId})`;
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
let chatWelcomedBack = false;  // one-shot: the resumed-character welcome line (see tick())
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
  // Default mode backfills the trailing ~20 events so a fresh tab opens onto the world's
  // recent life instead of an empty box until someone acts; Flash-only keeps its full
  // from-the-beginning backfill (that view IS the complete history).
  if (flashOnly) url += '&flash_only=1&backfill=1';
  else url += '&backfill=recent';
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
    // backfilled rows arrive all at once on connect (both modes backfill now) — only flash
    // the ones that show up AFTER that initial catch-up as "something just happened".
    if (streamCaughtUp) {
      div.classList.add('new');
      setTimeout(() => div.classList.remove('new'), 900);
    }
    // Wizard step 2B's soft join-feed fallback (e0b.12): piggyback this SAME connection
    // rather than opening a second EventSource -- every adventure.started event (any world
    // session's start_adventure — see server.py) is already flowing through here regardless
    // of whether the wizard happens to be open right now. Backfill (this stream's default
    // "recent ~20" mode) means a visitor who opens the wizard right after someone joined
    // still sees it, not just joins from this exact moment forward.
    if (ev.kind === 'adventure.started') addJoinFeedEvent(ev);
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
// every call ANY world has ever made, uncapped, not a live-filtered view of the panel below
// (that's what the "⚡ Flash calls only" toggle on the stream panel is for instead). No
// ?campaign= here on purpose — the header number itself is now server-wide (see /state's
// flash_calls query), so the page it opens must match that same total, not just this world's.
document.getElementById('flashcount').style.cursor = 'pointer';
document.getElementById('flashcount').title = 'Click to see every Flash call this server has ever made, across all worlds';
document.getElementById('flashcount').addEventListener('click', () => {
  window.open('/flash-calls', '_blank');
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

// Wizard step 2B soft fallback: last few adventure.started events, fed by connectStream()'s
// own 'world-event' listener above (see its comment for why this doesn't open a second
// EventSource). Shown regardless of whether the wizard is currently open -- cheap to keep
// current, and it's what makes "watching for new adventurers..." already have something in
// it the moment someone opens the panel, not just from that instant forward.
function addJoinFeedEvent(ev){
  const el = document.getElementById('wizJoinFeed');
  if (!el) return;
  const empty = el.querySelector('.empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.textContent = `⚡ ${ev.text}`;
  el.prepend(div);
  while (el.children.length > 5) el.lastChild.remove();
}

// Wizard step 2B verification panel (e0b.12): mint a pairing code (POST /pair/mint), show it
// large, then poll GET /pair/status every 3s until claimed or the ~10min TTL lapses (matches
// pairing.py's own TTL — polling past it is pointless, the code is already gone server-side).
// See pairing.py's module docstring for the full mechanism this is the front end of.
let pairPollTimer = null;
let pairPollDeadline = 0;

function stopPairPoll(){
  if (pairPollTimer) { clearInterval(pairPollTimer); pairPollTimer = null; }
}

function pairExpired(){
  stopPairPoll();
  document.getElementById('wizPairStatus').textContent = 'that code expired — get a new one to try again.';
  const btn = document.getElementById('wizMintBtn');
  btn.textContent = 'Get a new pairing code';
  btn.disabled = false;
  btn.style.display = '';
}

function startPairPoll(code){
  stopPairPoll();
  pairPollDeadline = Date.now() + 10 * 60 * 1000;
  pairPollTimer = setInterval(async () => {
    if (Date.now() > pairPollDeadline) { pairExpired(); return; }
    try{
      const r = await fetch('/pair/status?code=' + encodeURIComponent(code));
      if (r.status === 404) { pairExpired(); return; }
      if (!r.ok) return;  // transient error — just try again on the next tick
      const d = await r.json();
      if (d.claimed) {
        stopPairPoll();
        document.getElementById('wizPairStatus').innerHTML =
          `✓ That's you — <b>${esc(d.name || 'your character')}</b>!`;
        const linkBtn = document.getElementById('wizPairMapLink');
        linkBtn.href = d.map_link;
        linkBtn.style.display = '';
        // Adoption (agent -> browser transfer): bind the paired character to this browser's
        // chat session too, so "Play here" can continue the SAME character later. Offered,
        // never automatic — some players want the agent to stay the only narrator.
        const adoptBtn = document.getElementById('wizPairAdoptBtn');
        adoptBtn.style.display = '';
        adoptBtn.onclick = async () => {
          try{
            const ar = await fetch('/pair/adopt?code=' + encodeURIComponent(code), {method: 'POST', credentials: 'same-origin'});
            const ad = await ar.json().catch(() => ({}));
            if (!ar.ok) { document.getElementById('wizPairStatus').textContent = ad.error || ('error ' + ar.status); return; }
            if (ad.campaign_id && ad.campaign_id !== campaignId) {
              location.href = '/?campaign=' + encodeURIComponent(ad.campaign_id);
              return;
            }
            closeWizard();
            showMidTab('chat');
            chatWelcomedBack = false;  // let tick() greet the adopted character
            const input = document.getElementById('chatInput');
            if (input) input.focus();
          }catch(e){ /* leave the wizard open; user can retry */ }
        };
      }
    }catch(e){ /* network hiccup — next 3s tick tries again */ }
  }, 3000);
}

document.getElementById('wizMintBtn').addEventListener('click', async () => {
  const btn = document.getElementById('wizMintBtn');
  btn.disabled = true;
  try{
    const r = await fetch('/pair/mint', {method: 'POST'});
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      document.getElementById('wizPairStatus').textContent =
        err.error || 'could not mint a code — try again in a moment.';
      btn.disabled = false;
      return;
    }
    const d = await r.json();
    document.getElementById('wizPairCode').textContent = d.code;
    document.getElementById('wizPairCodeInline').textContent = d.code;
    document.getElementById('wizPairCodeBox').style.display = '';
    document.getElementById('wizPairStatus').textContent = 'watching for your agent to connect...';
    document.getElementById('wizPairMapLink').style.display = 'none';
    btn.style.display = 'none';
    startPairPoll(d.code);
  }catch(e){
    document.getElementById('wizPairStatus').textContent = 'connection trouble — try again?';
    btn.disabled = false;
  }
});

// Play here (e0b.3): a full turn of the browser-DM loop over POST /chat, streamed back as
// NDJSON (EventSource can't POST, so this is a plain fetch() + ReadableStream read instead of
// SSE — see /chat's own docstring). session_id/player_id live entirely server-side behind the
// HttpOnly dm_session cookie the browser sends automatically on same-origin fetches; this JS
// never sees either one.
const chatLog = document.getElementById('chatLog');
const chatForm = document.getElementById('chatForm');
const chatInput = document.getElementById('chatInput');
const chatSendBtn = document.getElementById('chatSendBtn');
let chatTurnInFlight = false;

function chatScrollToBottom(){ chatLog.scrollTop = chatLog.scrollHeight; }
function clearChatEmptyState(){
  const empty = chatLog.querySelector('.empty');
  if (empty) empty.remove();
}
// textContent, not innerHTML -- both the player's own typed text AND the DM's model-generated
// narration are untrusted strings; this is the same "escape before it touches the DOM"
// discipline as esc() elsewhere on this page, just via the DOM API instead of a string helper.
function addChatMessage(role, text){
  clearChatEmptyState();
  const div = document.createElement('div');
  div.className = 'chatMsg ' + role;
  div.textContent = (role === 'player' ? 'You: ' : '') + text;
  chatLog.appendChild(div);
  chatScrollToBottom();
}
function addChatBreadcrumb(name, summary){
  clearChatEmptyState();
  const div = document.createElement('div');
  div.className = 'chatBreadcrumb';
  div.textContent = `⚙ ${name}${summary ? ' — 🎲 ' + summary : ''}`;
  chatLog.appendChild(div);
  chatScrollToBottom();
}

// "New character": sever this browser's identity server-side (POST /chat/reset rotates the
// HttpOnly cookie + deletes the durable mapping) and reset the pane. The old character stays
// in the world as a ghost — the confirm() says so explicitly, since "reset" could otherwise
// read as "delete my character."
// Browser -> agent handoff: reveal this browser's OWN player_id (cookie-authenticated,
// owner-only — see /chat/handoff) with copy-paste instructions for their agent. The mirror
// of the wizard's adopt button.
document.getElementById('chatHandoffBtn').addEventListener('click', async (e) => {
  e.preventDefault();
  try{
    const r = await fetch('/chat/handoff?campaign=' + encodeURIComponent(campaignId), {credentials: 'same-origin'});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { addChatMessage('system', d.error === 'no character in this world' ? 'no character to hand off yet — start an adventure first.' : (d.error || ('error ' + r.status))); return; }
    addChatMessage('system', d.instructions);
  }catch(err){
    addChatMessage('error', 'connection trouble — try again?');
  }
});

document.getElementById('chatResetBtn').addEventListener('click', async (e) => {
  e.preventDefault();
  if (chatTurnInFlight) { addChatMessage('system', 'wait for the current turn to finish first.'); return; }
  if (!confirm('Start over with a brand-new character? Your current character stays in the world as a ghost.')) return;
  try{
    // campaign (e0b.10): reset is per-world now -- resetting on THIS page must not touch this
    // same browser's character in any OTHER world (see /chat/reset's own docstring).
    const r = await fetch('/chat/reset', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({campaign: campaignId}),
    });
    if(!r.ok){ const err = await r.json().catch(() => ({})); addChatMessage('error', err.error || `error ${r.status}`); return; }
    chatStarted = true;  // never bring the choice card back after a deliberate reset
    chatLog.innerHTML = '';
    addChatMessage('system', 'fresh start — say "start an adventure" to begin anew.');
    chatInput.focus();
  }catch(err){
    addChatMessage('error', 'connection trouble — try again?');
  }
});

chatForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (chatTurnInFlight) return;
  const text = chatInput.value.trim();
  if (!text) return;
  chatStarted = true;  // real interaction happened -- the choice card never reappears now
  // "Create my world" (e0b.10): sent on exactly this ONE upcoming turn, then cleared
  // regardless of outcome -- captured into a local BEFORE the flag is reset so a slow/failed
  // request can't leave it dangling into a later, unrelated turn.
  const sendNewWorld = newWorldPending;
  newWorldPending = false;
  addChatMessage('player', text);
  chatInput.value = '';
  chatTurnInFlight = true;
  chatInput.disabled = true;
  chatSendBtn.disabled = true;
  // Set only when THIS turn's start_adventure lands in a brand-new world (see the {"type":
  // "world",...} handling below) -- triggers the redirect once the stream finishes.
  let redirectToCampaign = null;
  try{
    const r = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',  // send/receive the dm_session cookie -- same-origin default
                                    // for same-origin fetches in most browsers, but explicit
                                    // here since this call MUST carry it to reuse the session.
      // campaign (e0b.10): the chat operates in the world of the PAGE it's on, not a
      // hardcoded "main" -- server validates it (400 if it's neither "main" nor an existing
      // world). new_world: only ever honored server-side while this session has no character
      // yet in this world.
      body: JSON.stringify({message: text, campaign: campaignId, new_world: sendNewWorld}),
    });
    if(!r.ok){
      const err = await r.json().catch(() => ({}));
      // 429 (per-IP rate limit or per-session lifetime cap, e0b.4) is routine traffic-shaping,
      // not a failure -- render it as a dim system line, same as a tool breadcrumb, not the
      // alarmed error-red used for actual failures (409 in-flight, 413 too long, 503 disabled).
      if(r.status === 429) addChatMessage('system', err.error || 'please slow down a moment.');
      else addChatMessage('error', err.error || `error ${r.status}`);
      return;
    }
    // NDJSON: one JSON object per newline-terminated line. Buffer partial lines across
    // chunks -- a chunk boundary is a byte-stream artifact, not guaranteed to land on a line
    // break. NOTE: PAGE is a plain (non-raw) Python triple-quoted string, so a literal
    // backslash-n here would be eaten by PYTHON before this ever became JS source -- every
    // newline below is written as \\n for exactly that reason (see also the SAME gotcha
    // already worked around in escRegex()'s \\\\ a few hundred lines up).
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      buf += decoder.decode(value, {stream: true});
      let nl;
      while((nl = buf.indexOf('\\n')) >= 0){
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if(!line) continue;
        let ev;
        try{ ev = JSON.parse(line); } catch(parseErr){ continue; }
        if(ev.type === 'tool') addChatBreadcrumb(ev.name, ev.summary);
        else if(ev.type === 'text') addChatMessage('dm', ev.text);
        // New-world flow (e0b.10): this turn's start_adventure just minted a brand-new world
        // (session.campaign_id no longer matches the page we're on) -- tell the player, then
        // redirect once the whole turn (including its final narration) has actually arrived.
        else if(ev.type === 'world'){
          addChatMessage('system', 'your world is ready — taking you there...');
          redirectToCampaign = ev.campaign_id;
        }
        // {"type":"done"} carries no content -- it's only the client's cue the turn is over,
        // which the surrounding try/finally already handles by re-enabling input below.
      }
    }
  }catch(err){
    addChatMessage('error', 'connection trouble — try again?');
  }finally{
    chatTurnInFlight = false;
    chatInput.disabled = false;
    chatSendBtn.disabled = false;
    chatInput.focus();
  }
  // A turn can mint a brand-new character (start_adventure) or move an existing one -- either
  // way, the very next /state poll (tick() already runs every 1.5s) now resolves "you" from
  // the SAME dm_session cookie this fetch just used, so the map/character panel light up with
  // no extra round trip needed here.
  if (redirectToCampaign) {
    location.href = '/?campaign=' + encodeURIComponent(redirectToCampaign);
  }
});
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


# Room ids (which double as art cache refs — see server.py's _prefetch_room_art, ref=dest_id)
# only ever contain lowercase letters, digits, ':', '.', '-' by this codebase's own convention
# (e.g. "r0", "r0:north", "<campaign_id>:r0:north"). `ref` below comes straight off the URL
# path, so it's validated against exactly that charset before ever touching the filesystem —
# this is what rules out "/" and ".." path-traversal, not a blocklist of those specifically.
_ART_REF_RE = re.compile(r"^[a-z0-9:.\-]+$")


@app.get("/art/{ref}.png")
def art_image(ref: str) -> Response:
    """Serves a GPU-generated room image cached by dndmcp/art.py's prefetch() at
    $DNDMCP_STATE_DIR/art/{ref}.png. 404s if disabled/not-yet-generated/never requested (same
    "art always optional" contract as the rest of the art layer) or if `ref` fails the
    charset check above."""
    if "/" in ref or ".." in ref or not _ART_REF_RE.match(ref):
        return Response(status_code=404)
    state_dir = os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp"))
    path = Path(state_dir) / "art" / f"{ref}.png"
    if not path.is_file():
        return Response(status_code=404)
    # A ref's image never changes once generated (only scripts/regen_art.sh's rare, manual
    # admin action overwrites one) — cache hard, both browser- and Cloudflare-side (this URL
    # is proxied through Cloudflare, which was defaulting to cf-cache-status: BYPASS with no
    # Cache-Control at all, so every single view — including repeat views of the same room —
    # was re-reading the file off the network volume and re-sending the full PNG).
    return Response(content=path.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.post("/art/regen")
async def art_regen(request: Request) -> JSONResponse:
    """On-demand backstop for a room's art: art otherwise only ever generates once, at room
    creation (server.py's _generate_and_link/start_adventure), with no retry anywhere else if
    that one speculative attempt transiently fails (observed live: a brief GPU-allocation
    hiccup silently dropped a room's art forever, with nothing else ever revisiting it).
    Called by the room panel's own JS whenever it opens a room with no image_ref yet. Safe to
    call repeatedly/on every open: art.prefetch() no-ops (returns True immediately) if a
    cached image already exists, so this can't double-generate or double-spend a room that
    already succeeded."""
    try:
        body = await request.json()
    except Exception:
        body = None
    room_id = body.get("room_id") if isinstance(body, dict) else None
    campaign_id = body.get("campaign") if isinstance(body, dict) else None
    if not room_id or not campaign_id:
        return JSONResponse({"error": "room_id and campaign are required"}, status_code=400)
    room = server.world.room(room_id)
    if not room:
        return JSONResponse({"error": "unknown room"}, status_code=404)
    if room.image_ref:
        return JSONResponse({"image_ref": room.image_ref})
    await server._prefetch_room_art(room_id, room.name, room.description, campaign_id)  # noqa: SLF001
    updated = server.world.room(room_id)
    return JSONResponse({"image_ref": updated.image_ref if updated else None})


@app.get("/chat/enabled")
def chat_enabled() -> JSONResponse:
    """Polled once by the page's own JS before it shows the "Play here" tab at all — the kill
    switch (DND_BROWSER_DM=0) needs a way to hide the pane client-side too, not just 503 the
    endpoint once someone's already typing into it."""
    return JSONResponse({"enabled": _browser_dm_enabled()})


# --- Onboarding wizard: pairing endpoints (e0b.12) -------------------------------------------
# See pairing.py's module docstring for the full mechanism (mint/claim/status already shipped
# there + server.py's claim_pairing MCP tool). This is just the two thin web endpoints the
# wizard's step 2B talks to.

# Cheap per-IP mint guard: the code space is only 64*64=4096 combos (pairing.py's word lists) —
# unlike /chat, minting has no LLM/GPU cost of its own to naturally throttle it, so a scripted
# hammer on this one endpoint alone could drain/collide the space. Same sliding-window shape as
# chat_sessions.check_ip_rate_limit, kept local here since minting shares nothing else with a
# chat turn (no session, no world, no lock).
_MINT_LIMIT_PER_IP_PER_MINUTE = 5
_MINT_WINDOW_SECONDS = 60.0
_mint_hits: dict[str, collections.deque] = {}


def _check_mint_rate_limit(ip: str | None) -> bool:
    if ip is None:
        return True  # no IP to key on — never block blind
    now = time.monotonic()
    hits = _mint_hits.setdefault(ip, collections.deque())
    while hits and now - hits[0] > _MINT_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= _MINT_LIMIT_PER_IP_PER_MINUTE:
        return False
    hits.append(now)
    return True


@app.post("/pair/mint")
def pair_mint(request: Request) -> JSONResponse:
    """Wizard step 2B, "Get my pairing code": mint a fresh onboarding code
    (pairing.mint() — two-word, ~10min TTL, single-use). Rate-guarded per IP only; see
    _check_mint_rate_limit above for why that's enough here."""
    ip = _client_ip(request)
    if not _check_mint_rate_limit(ip):
        return JSONResponse(
            {"error": "too many codes requested — wait a moment and try again"}, status_code=429)
    return JSONResponse({"code": pairing.mint()})


@app.get("/pair/status")
def pair_status(request: Request) -> JSONResponse:
    """Wizard poll target (every 3s while step 2B's code box is showing). 404 when the code is
    unknown or its ~10min TTL expired — the wizard treats that as "give up, offer a fresh
    code." Otherwise {"claimed": bool, "name": str|None, "map_link": str|None}.

    map_link is built HERE, server-side, ONLY once claimed: "/?player=<id>&campaign=<id>" —
    this endpoint is the ONE sanctioned channel a browser is allowed to learn its own full
    player link through (see pairing.py's module docstring for why: the code that unlocks it
    was minted by this same browser and is single-use, so nothing else can ride along). The
    raw player_id is deliberately never surfaced as its own field — only baked into the link."""
    code = request.query_params.get("code") or ""
    result = pairing.status(code)
    if result is None:
        return JSONResponse({"error": "unknown or expired code"}, status_code=404)
    map_link = None
    if result["claimed"]:
        map_link = f"/?player={quote(result['player_id'])}&campaign={quote(result['campaign_id'])}"
    return JSONResponse({"claimed": result["claimed"], "name": result["name"], "map_link": map_link})


@app.post("/pair/adopt")
def pair_adopt(request: Request) -> JSONResponse:
    """Agent -> browser character transfer: bind a PAIRED character to THIS browser's chat
    session, so the Play-here pane on that world's page resumes it (the exact resume path a
    redeploy already exercises — game state carries over fully; conversation history stays
    in the agent's own context, and the browser DM re-orients via look/character_sheet).
    Authorization is the pairing code itself: single-use to CLAIM, but readable within its
    TTL by the browser that minted it — the same capability boundary /pair/status's map_link
    already stands on. The dm_session cookie is created here if the browser has none yet."""
    code = request.query_params.get("code") or ""
    result = pairing.status(code)
    if result is None:
        return JSONResponse({"error": "unknown or expired code"}, status_code=404)
    if not result["claimed"]:
        return JSONResponse({"error": "code not claimed yet — connect your agent first"},
                            status_code=409)
    session_id = request.cookies.get(chat_sessions.COOKIE_NAME) or chat_sessions.new_session_id()
    server.world.save_web_session_world(
        session_id, result["campaign_id"], player_id=result["player_id"])
    # Drop any in-memory session for this (browser, world) so the next chat turn resumes the
    # adopted character from the durable row instead of continuing an older one.
    chat_sessions.drop(session_id, result["campaign_id"])
    resp = JSONResponse({"ok": True, "name": result["name"],
                         "campaign_id": result["campaign_id"]})
    resp.set_cookie(chat_sessions.COOKIE_NAME, session_id, httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/chat/handoff")
def chat_handoff(request: Request) -> JSONResponse:
    """Browser -> agent character transfer: reveal THIS browser's own player_id for one world
    so its owner can hand it to their own agent ("resume playing player_id X" — every MCP
    tool already takes player_id; that's how agent play works). Cookie-authenticated: only
    the session that owns the character can see it, which makes this the mirror image of
    /pair/status's map_link — an owner learning their own bearer token, never anyone else's."""
    campaign_id = request.query_params.get("campaign") or "main"
    session_id = request.cookies.get(chat_sessions.COOKIE_NAME)
    if not session_id:
        return JSONResponse({"error": "no session"}, status_code=404)
    ws = server.world.get_web_session_world(session_id, campaign_id)
    if not ws or not ws.player_id or not server.world.character(ws.player_id):
        return JSONResponse({"error": "no character in this world"}, status_code=404)
    ch = server.world.character(ws.player_id)
    return JSONResponse({
        "player_id": ws.player_id, "name": ch.name, "campaign_id": campaign_id,
        "instructions": (f'Connect your agent to this MCP server (▶ Play → "Through your own '
                         f'agent"), then tell it: resume playing player_id {ws.player_id}'
                         + (f' in world {campaign_id}' if campaign_id != 'main' else '')
                         + f" — you are {ch.name}. Heads up: don't drive {ch.name} from here "
                         f"and your agent at the same time; one narrator per body.")})


@app.post("/chat")
async def chat(request: Request):
    """One player turn of the browser-DM loop, streamed back as NDJSON (one JSON object per
    line — EventSource can't POST, so this is a plain streamed fetch() response instead of
    SSE, see the module's other SSE use at /stream/events for contrast). Each line is exactly
    the event shape dm_loop.handle_message yields ({"type":"tool",...} / {"type":"text",...} /
    {"type":"world",...} — the last one only on a turn that just created a brand-new world,
    see below), plus a final {"type":"done"} the client uses to know the turn is over and
    re-enable input.

    Session handshake: session_id comes from the dm_session HttpOnly cookie; minted here (and
    set on the response) the first time a browser has none. player_id itself never appears
    anywhere in this response or gets accepted as a request field — see chat_sessions.py's
    module docstring for the full boundary.

    PER-WORLD (e0b.10): the chat now operates in whatever world the PAGE it's on names — the
    page's own JS sends its campaignId as the "campaign" body field every turn (defaults to
    "main" for any older/bare client that omits it). Anything other than "main" must already
    exist (campaign_exists) or this 400s — a typo'd/garbage campaign id would otherwise
    silently mint a brand-new, permanently-empty DMSession pointed at a world nothing will
    ever generate content in. One browser (one dm_session cookie) can hold an independent
    DMSession — and an independent real character — in as many worlds as it visits; see
    chat_sessions.py's module docstring for the full (session_id, campaign_id) key shape.

    "new_world" (e0b.10): the choice card's "Create my world" button sends `new_world: true`
    on its next turn. Honored ONLY while this (session, world) has no character yet
    (session.player_id is None) — it flips session.pending_new_world, which dm_loop's
    start_adventure tool wrapper reads to swap in campaign_id="new" for its own call. See
    dm_loop.DMSession's docstring and _tool_start_adventure.

    Guards, in order: message length cap (413); kill switch (503, checked first, above);
    unknown campaign (400); per-IP sliding-window rate limit (429,
    chat_sessions.check_ip_rate_limit — e0b.4); the per-(session, world) lifetime message cap
    (429, chat_sessions.session_cap_exceeded, backed by state.py's
    web_session_world.message_count so it survives a redeploy — e0b.4, widened per-world by
    e0b.10); one turn in flight per (session, world) (409, via chat_sessions.lock_for —
    checked-then-acquired with no `await` in between, safe under asyncio's single-threaded
    scheduling); and the process-wide chat_sessions.turn_semaphore protecting the single warm
    LLM worker from a burst of simultaneous browser turns.
    """
    if not _browser_dm_enabled():
        return JSONResponse({"error": "browser play is currently disabled"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = None
    message = (body.get("message") if isinstance(body, dict) else None) or ""
    message = message.strip() if isinstance(message, str) else ""
    if len(message) > MAX_CHAT_MESSAGE_LEN:
        return JSONResponse(
            {"error": f"message too long (max {MAX_CHAT_MESSAGE_LEN} chars)"}, status_code=413)
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    # Per-world (e0b.10): the page's own campaignId, not a hardcoded "main" — see this route's
    # own docstring. Anything other than "main" must already exist; a bare/older client that
    # sends no "campaign" field at all still gets the old main-only behavior for free.
    campaign_id = (body.get("campaign") if isinstance(body, dict) else None) or "main"
    if not isinstance(campaign_id, str):
        campaign_id = "main"
    if campaign_id != "main" and not server.world.campaign_exists(campaign_id):
        return JSONResponse({"error": f'no world with id "{campaign_id}" exists'}, status_code=400)
    new_world_requested = bool(body.get("new_world")) if isinstance(body, dict) else False

    session_id = request.cookies.get(chat_sessions.COOKIE_NAME)
    minted_cookie = session_id is None
    if minted_cookie:
        session_id = chat_sessions.new_session_id()
    # get_or_create is where a returning browser's OWN character IN THIS WORLD gets resumed
    # (durable web_session_world mapping, e0b.4/e0b.10) if the in-memory store lost it to a
    # redeploy — see chat_sessions.py's module docstring and _resume_from_durable_store.
    session = chat_sessions.get_or_create(session_id, campaign_id)

    # "Create my world" (e0b.10): only ever honored while THIS session has no character yet
    # IN THIS WORLD — an established player can't retroactively hijack their own turn into
    # abandoning a live character via some stray client replay. See DMSession.pending_new_world
    # and dm_loop._tool_start_adventure for what happens with this flag next.
    if new_world_requested and session.player_id is None:
        session.pending_new_world = True

    ip = _client_ip(request)
    allowed, first_throttle = chat_sessions.check_ip_rate_limit(ip)
    if not allowed:
        if first_throttle:
            _log_dm_event(ip, session.campaign_id, "dm.throttled",
                          f"per-IP rate limit ({chat_sessions.MAX_MESSAGES_PER_IP_PER_MINUTE}/min) hit.")
        return JSONResponse({"error": RATE_LIMIT_MESSAGE}, status_code=429)

    # Lifetime cap check: peek the durable count WITHOUT incrementing it yet (a rejected
    # request never counts as a used turn) — see World.touch_web_session_world for the actual
    # increment, which only happens once the turn is allowed to run, below. Scoped to THIS
    # world (session_id, campaign_id) — see chat_sessions.py's module docstring for why the
    # cap is now counted per world rather than per browser overall.
    existing = server.world.get_web_session_world(session_id, campaign_id)
    message_count = existing.message_count if existing else 0
    exceeded, first_cap_hit = chat_sessions.session_cap_exceeded((session_id, campaign_id), message_count)
    if exceeded:
        if first_cap_hit:
            _log_dm_event(ip, session.campaign_id, "dm.throttled",
                          f"per-session lifetime cap ({chat_sessions.MAX_SESSION_MESSAGES}) hit.")
        return JSONResponse({"error": SESSION_CAP_MESSAGE}, status_code=429)

    lock = chat_sessions.lock_for(session_id, campaign_id)
    if lock.locked():
        return JSONResponse(
            {"error": "a turn is already in progress for this session"}, status_code=409)
    # Acquire NOW (not inside the generator below) — no `await` happens between the .locked()
    # check above and this line, so under asyncio's cooperative single-threaded scheduling
    # nothing else can run in between and race the check. Released in the generator's finally.
    await lock.acquire()

    async def turn_stream():
        # Observability: capture the COMPLETE turn — every streamed event verbatim, any
        # exception, wall-clock — into state.record_dm_turn. A player reporting "I typed X
        # and nothing happened" is undiagnosable from the world log alone (a turn where the
        # model called no tools writes no world events at all); this table is the flight
        # recorder that answers what the model actually did.
        turn_started = time.time()
        turn_events: list[dict] = []
        turn_error: str | None = None
        try:
            async with chat_sessions.turn_semaphore:
                async for event in dm_loop.handle_message(session, message):
                    turn_events.append(event)
                    yield json.dumps(event) + "\n"
        except Exception as exc:
            turn_error = repr(exc)
            logger.exception("POST /chat: dm_loop.handle_message failed")
            yield json.dumps({
                "type": "text",
                "text": "The DM pauses, momentarily lost in thought... (something went wrong — try again?)",
            }) + "\n"
        finally:
            try:
                server.world.record_dm_turn(
                    session_id=session_id, player_id=session.player_id,
                    campaign_id=session.campaign_id, user_message=message,
                    events=turn_events, error=turn_error,
                    duration_ms=int((time.time() - turn_started) * 1000))
            except Exception:
                logger.exception("POST /chat: failed to record dm_turn")
            # Durable bookkeeping for THIS turn: bump the lifetime-cap counter (regardless of
            # success/failure above — a failed turn still consumed a slot), and persist the
            # session_id -> player_id mapping once start_adventure has minted one (or refresh
            # it if it already existed) — see state.py's web_session_world table / e0b.4/e0b.10.
            #
            # IMPORTANT ordering note (e0b.10): both calls use session.campaign_id — the world
            # the turn actually ENDED in — NOT the campaign_id this request came in with. On a
            # normal turn those are the same value. On a turn where "Create my world" just
            # minted a brand-new world, dm_loop's start_adventure tool wrapper already updated
            # session.campaign_id to the real new id (see _tool_start_adventure) BEFORE this
            # finally block ever runs — dm_loop.handle_message's async generator is fully
            # drained (the `async for` above only exits once it does) before we reach here, so
            # there is no race: session.campaign_id is guaranteed settled by this point. That's
            # what makes it correct to write the durable row under the NEW campaign_id — the
            # page's client-side redirect (triggered by the {"type":"world",...} event) lands
            # on /?campaign=<new-id>, and that page's very next /chat POST resolves
            # get_web_session_world(session_id, new_id) straight to this same row.
            try:
                server.world.touch_web_session_world(session_id, session.campaign_id)
                if session.player_id:
                    server.world.save_web_session_world(
                        session_id, session.campaign_id, player_id=session.player_id)
            except Exception:
                logger.exception("POST /chat: failed to persist web_session_world bookkeeping")
            yield json.dumps({"type": "done"}) + "\n"
            lock.release()

    resp = StreamingResponse(
        turn_stream(), media_type="application/x-ndjson",
        # Defeat any intermediary/proxy response buffering — same class of concern
        # /stream/events already has to deal with for its SSE stream, just spelled out
        # explicitly here since this is a plain streamed response, not EventSourceResponse.
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
    if minted_cookie:
        # 30 days — durable identity (e0b.4, state.py's web_session_world table) now lets a
        # returning browser resume its OWN character (in each world it visited) across a
        # redeploy, so the cookie itself is worth keeping around far longer than the in-memory
        # session store ever survived. One cookie/session_id anchors EVERY world this browser
        # ever visits (e0b.10) — it is never rotated by a per-world reset, see POST /chat/reset.
        resp.set_cookie(chat_sessions.COOKIE_NAME, session_id, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/chat/reset")
async def chat_reset(request: Request):
    """The "new character" flow, now PER WORLD (e0b.10): sever this browser's identity in
    exactly the ONE world named by the "campaign" body field (in-memory session + durable
    web_session_world row for that (session_id, campaign_id) pair only), so the next /chat
    message on THAT page starts the normal opening flow fresh. The old character is NOT
    deleted — it stays in the world as an abandoned ghost, consistent with every other way a
    character gets left behind.

    The dm_session cookie is deliberately NEVER rotated here anymore. Before multi-world, one
    cookie meant one character, so rotating it on reset both discarded the old identity AND
    handed back a clean slate in the same motion. Now one cookie anchors a browser across
    EVERY world it's visited (chat_sessions.py's module docstring) — rotating it would sever
    ALL of them, so resetting your character on a friend's world would silently also abandon
    your character back in main. Scoping the drop to (session_id, campaign_id) is what makes
    "reset THIS world only" possible; the per-(session, world) lifetime message cap resets
    itself for free the moment the web_session_world row for this world is deleted (the next
    turn just creates a fresh row starting at message_count=1). The per-IP sliding window
    still applies unchanged, so this isn't a rate-limit bypass, only a budget-per-world reset."""
    if not _browser_dm_enabled():
        return JSONResponse({"error": "browser play is currently disabled"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = None
    campaign_id = (body.get("campaign") if isinstance(body, dict) else None) or "main"
    if not isinstance(campaign_id, str):
        campaign_id = "main"
    session_id = request.cookies.get(chat_sessions.COOKIE_NAME)
    if session_id:
        if chat_sessions.lock_for(session_id, campaign_id).locked():
            return JSONResponse(
                {"error": "a turn is still in progress — wait for it to finish first"},
                status_code=409)
        chat_sessions.drop(session_id, campaign_id)
        try:
            server.world.delete_web_session_world(session_id, campaign_id)
        except Exception:
            logger.exception("POST /chat/reset: failed to delete web_session_world row")
    return JSONResponse({"ok": True})


_EMPTY_STATE = {"rooms": [], "players": [], "character": None, "you": None, "current_room": None, "log": [], "quests": [], "flash_calls": 0, "campaign": None, "server_version": SERVER_VERSION}


@app.get("/state")
def state(request: Request) -> JSONResponse:
    player_id = request.query_params.get("player")
    # Multi-world: each world's map is independent now — "main" is the well-known default
    # (what every pre-multi-world link/bookmark still means), anything else is a specific
    # world someone created/shared (see server.py start_adventure's campaign_id).
    campaign_id = request.query_params.get("campaign") or "main"
    # Browser-chat path (e0b.3): no ?player= in the URL at all for that flow — the credential
    # lives only in the dm_session cookie -> chat_sessions store, never in a URL a viewer could
    # see over someone's shoulder or find in browser history. Falls back to the cookie ONLY
    # when ?player= is absent, so an explicit ?player= (the BYO-agent share-link flow) keeps
    # working exactly as before and is never silently overridden by a stale cookie.
    # CAMPAIGN-SCOPED on purpose (fresh-player test finding #1): the cookie identifies a
    # character in ONE world — browsing a DIFFERENT world's page used to still resolve "you"
    # from the cookie, so the header/Character panel showed your main-world character and
    # room inside someone else's world, which reads as completely broken. A cookie session
    # only counts as "you" on the page of the world it actually belongs to.
    if not player_id:
        session_id = request.cookies.get(chat_sessions.COOKIE_NAME)
        if session_id:
            # (e0b.10) chat_sessions.get_if_resumable is keyed by (session_id, campaign_id) —
            # this already only ever returns a session that lives at THIS page's campaign_id,
            # never a same-cookie session for some other world — AND (unlike a bare in-memory
            # lookup) will resume an already-real character from the durable web_session_world
            # row if the in-memory object isn't reachable under this exact key yet. That
            # resume path is what makes the new-world redirect flow actually work end to end:
            # the turn that just minted a brand-new world wrote its durable row under the NEW
            # campaign_id (POST /chat's finally-bookkeeping), but the in-memory DMSession
            # object is still only reachable under the OLD page's key — without the resume
            # fallback here, the redirected page's first /state poll would show no character
            # at all until the player's first /chat message there. See
            # chat_sessions.get_if_resumable's own docstring for why this stays safe to call
            # from a passive GET (it can only ever surface a character that TRULY exists).
            # The session.campaign_id == campaign_id check stays anyway as a narrow safety net
            # for one remaining transient window: mid-turn, right after start_adventure just
            # minted a brand-new world but BEFORE the client has redirected, a /state poll on
            # the OLD page (still keyed to the OLD campaign_id in _sessions) must not show the
            # new world's character there a beat early.
            session = chat_sessions.get_if_resumable(session_id, campaign_id)
            if session and session.player_id and session.campaign_id == campaign_id:
                player_id = session.player_id
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
                          "image_ref": r["image_ref"],
                          "exits": exits_by_room.get(r["id"], {})})  # {direction: dest_room_id}
        # player_id IS the game's bearer credential -- server.py trusts it directly as the
        # only auth for move/attack/drop_item/delete_world/etc, no separate token or session.
        # This /state response is public and unauthenticated (anyone with the URL can poll
        # it), so shipping any OTHER player's full player_id here would let a stranger replay
        # it against server.py and act as that character. Truncate to the same 6 chars the
        # page already shows for every OTHER player (see the JS `who` span in the SSE handler
        # below, and highlightKnown -- neither ever needs more than that to render). The
        # viewer's OWN identity is unaffected: that still flows through the full, caller-
        # supplied ?player= query param handled separately below, never through this list.
        players = [{"player_id": p["player_id"][:6], "name": p["name"], "location_id": p["location_id"]}
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
                # Never echo the full bearer credential back into a JSON body — the browser-
                # chat path resolves player_id from an HttpOnly cookie specifically so it never
                # touches client-readable state; leaving it in this dict would defeat that the
                # instant this response reaches the page's JS. Nothing in the page's own JS
                # reads ch.player_id (name/level/klass/hp/inventory only), so this is a pure
                # subtraction, not a behavior change for the existing ?player= share-link flow.
                char.pop("player_id", None)
        log = [dict(r) for r in c.execute(
            # art.generated excluded: high-volume pipeline bookkeeping (one per room, bursty
            # during prefetch) that drowns actual story events out of an 8-row panel — it
            # still counts in the Flash counter and still appears on the unfiltered stream.
            "SELECT text FROM log WHERE campaign_id=? AND kind != 'art.generated'"
            " ORDER BY seq DESC LIMIT 8", (campaign_id,)
        ).fetchall()][::-1]
        # room.generated is the highest-volume Flash use, but entity.spawned (NPC persona
        # generation) and npc.talked (NPC dialogue) also call Flash — count all three so the
        # counter doesn't undercount just because personas are generated far more sparsely
        # (deterministic density gate — see server.py::_maybe_spawn_entity_persona).
        # Deliberately NOT scoped to campaign_id, unlike everything else in this response —
        # the header counter is meant to read as "how much real GPU work has this whole
        # server done," not just the one world you happen to be viewing (same server-wide-
        # by-default convention /metrics and /flash-calls use with no ?campaign= given).
        flash_calls = c.execute(
            "SELECT COUNT(*) FROM log"
            " WHERE kind IN ('room.generated','entity.spawned','npc.talked','item.picked_up','story.exported','art.generated')"
            " AND text LIKE '%(flash)%'"
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
            "SELECT ts, kind, text, player_id, subject_type, subject_id FROM log"
            " WHERE campaign_id=? AND (player_id=? OR player_id IS NULL) ORDER BY seq ASC",
            (campaign_id, player_id),
        ).fetchall()
        # A null-player_id event (room.generated, entity.spawned, art.generated — the
        # background-prefetch/system events) is about the WHOLE campaign, not this player —
        # _prefetch_frontier speculatively generates every exit's destination ahead of time,
        # so most rooms/NPCs that ever exist at any moment are ones THIS player never actually
        # entered. Confirmed live: an unvisited room + its NPC (generated via prefetch, never
        # discovered) leaked wholesale into a different player's own story. Filter null-
        # player_id events down to rooms this player has actually discovered.
        discovered = {r["to_id"] for r in c.execute(
            "SELECT to_id FROM edges WHERE from_type='character' AND from_id=?"
            " AND to_type='room' AND edge_type='discovered'", (player_id,)).fetchall()}
        entity_room = {r["id"]: r["location_id"] for r in c.execute(
            "SELECT id, location_id FROM entity WHERE campaign_id=?", (campaign_id,)).fetchall()}
    finally:
        c.close()

    def relevant(e) -> bool:
        if e["player_id"]:
            return True  # this player's own action — always theirs to tell
        if e["subject_type"] == "room":
            return e["subject_id"] in discovered
        if e["subject_type"] == "entity":
            return entity_room.get(e["subject_id"]) in discovered
        return False  # no subject to check against — safer to omit than leak another area

    theme = camp["theme"] if camp else "adventure"
    premise = camp["premise"] if camp else ""
    timeline_lines = [f"- {e['text']}" for e in events if relevant(e)] or ["- (nothing has happened yet)"]
    timeline_text = "\n".join(timeline_lines)

    markdown = await worldgen.generate_story(char["name"], char["klass"], theme, premise, timeline_text,
                                             is_main=campaign_id == MAIN_CAMPAIGN_ID)
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
    """Every Flash call EVER made, in full, on its own page — not a capped/truncated panel.
    What #flashcount in the header links to (opens in a new tab). With no ?campaign=, this is
    the SERVER-WIDE list across every world — matching the header counter, which is now also
    server-wide (see /state's flash_calls query). Pass ?campaign=X to scope down to one
    world's own call history instead, same all-worlds-by-default convention /metrics uses."""
    campaign_id = request.query_params.get("campaign")
    all_worlds = not campaign_id
    where = "WHERE text LIKE '%(flash)%'" if all_worlds else "WHERE campaign_id=? AND text LIKE '%(flash)%'"
    args = () if all_worlds else (campaign_id,)
    c = _db()
    try:
        rows = c.execute(
            f"SELECT ts, kind, text, player_id, campaign_id, subject_type, subject_id FROM log"
            f" {where} ORDER BY seq DESC",
            args,
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        c.close()

    def row_html(r: sqlite3.Row) -> str:
        ts = datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S") if r["ts"] else "?"
        who = f'<span class=who>{html.escape(r["player_id"][:6])}</span> ' if r["player_id"] else ""
        world = (f'<span class=world>{html.escape(r["campaign_id"] or "main")}</span> '
                if all_worlds else "")
        subj = (f'<span class=subj>{html.escape(r["subject_type"])}:{html.escape(r["subject_id"])}</span>'
               if r["subject_type"] else "")
        return (f'<div class=row><span class=ts>{ts}</span><span class=kind>{html.escape(r["kind"])}</span>'
               f'{world}{who}<span class=text>{html.escape(r["text"])}</span>{subj}</div>')

    body = "".join(row_html(r) for r in rows) or '<div class=empty>No Flash calls yet.</div>'
    title = "all worlds" if all_worlds else html.escape(campaign_id)
    return f"""<!doctype html><html><head><meta charset=utf-8><title>Flash calls — {title}</title>
<link rel=icon href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚔</text></svg>">
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
.world{{color:var(--warm-bright)}}
.who{{color:var(--ghost)}}
.subj{{color:var(--muted);margin-left:auto}}
.text{{color:var(--text)}}
.empty{{color:var(--muted);padding:20px 0}}
</style></head><body>
<header><h1>⚡ Flash calls</h1><span class=count>{len(rows)} total{' across all worlds' if all_worlds else ' in this world'}</span></header>
<main>{body}</main>
</body></html>"""


@app.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: Request) -> str:
    """System-wide counters. With no ?campaign=, this is the SERVER-WIDE root: every world,
    every character across all of them (with alive/dead status), not just "main" — a bare
    /metrics link used to silently default to one world, which made every OTHER world's
    activity invisible. Pass ?campaign=X (what the per-world header Metrics button does) to
    scope back down to one world's own counters/players/kind-breakdown, same as before.
    All derived from the existing `log`/`character`/`campaigns` tables via aggregate queries —
    no new table, same "just use what's there more fully" approach as EVENT_STREAM_SPEC.md.
    Hackathon-demo surface, not an ops dashboard: counters + plain tables, no charting lib."""
    campaign_id = request.query_params.get("campaign")
    all_worlds = not campaign_id
    where_camp = "" if all_worlds else "WHERE campaign_id=?"
    camp_args = () if all_worlds else (campaign_id,)
    c = _db()
    try:
        total_events = c.execute(
            f"SELECT COUNT(*) FROM log {where_camp}", camp_args).fetchone()[0]
        unique_players = c.execute(
            f"SELECT COUNT(DISTINCT player_id) FROM log {where_camp}"
            f" {'AND' if where_camp else 'WHERE'} player_id IS NOT NULL", camp_args).fetchone()[0]
        unique_ips = c.execute(
            f"SELECT COUNT(DISTINCT ip) FROM log {where_camp}"
            f" {'AND' if where_camp else 'WHERE'} ip IS NOT NULL", camp_args).fetchone()[0]
        # Same Flash-kind definition /state's header counter and /flash-calls use.
        flash_calls = c.execute(
            f"SELECT COUNT(*) FROM log {where_camp}"
            f" {'AND' if where_camp else 'WHERE'} kind IN"
            " ('room.generated','entity.spawned','npc.talked','item.picked_up','story.exported','art.generated')"
            " AND text LIKE '%(flash)%'", camp_args).fetchone()[0]
        by_kind = c.execute(
            f"SELECT kind, COUNT(*) AS n FROM log {where_camp} GROUP BY kind ORDER BY n DESC LIMIT 20",
            camp_args).fetchall()
        hourly = c.execute(
            f"SELECT strftime('%Y-%m-%d %H:00', ts, 'unixepoch') AS bucket, COUNT(*) AS n"
            f" FROM log {where_camp} {'AND' if where_camp else 'WHERE'} ts >= ?"
            " GROUP BY bucket ORDER BY bucket ASC",
            (*camp_args, time.time() - 86400)).fetchall()
        # hp<=0 is the only "dead" signal a player character has (see server.py's attack() —
        # there's no separate is_dead flag, damage() just clamps hp at 0 and narrates it).
        players = c.execute(
            "SELECT ch.player_id AS player_id, ch.campaign_id AS campaign_id, ch.name AS name,"
            " ch.klass AS klass, ch.hp AS hp, ch.max_hp AS max_hp,"
            " (SELECT COUNT(*) FROM log WHERE player_id=ch.player_id AND campaign_id=ch.campaign_id) AS events,"
            " (SELECT MAX(ts) FROM log WHERE player_id=ch.player_id AND campaign_id=ch.campaign_id) AS last_seen"
            f" FROM character ch {where_camp} ORDER BY last_seen DESC", camp_args).fetchall()
        worlds = c.execute(
            "SELECT cp.id AS id, cp.name AS name, cp.theme AS theme,"
            " (SELECT COUNT(*) FROM rooms WHERE campaign_id=cp.id) AS rooms,"
            " (SELECT COUNT(*) FROM character WHERE campaign_id=cp.id) AS characters,"
            " (SELECT COUNT(*) FROM log WHERE campaign_id=cp.id) AS events"
            " FROM campaigns cp ORDER BY cp.created_at DESC"
        ).fetchall() if all_worlds else []
    except sqlite3.OperationalError:
        total_events = unique_players = unique_ips = flash_calls = 0
        by_kind = hourly = players = worlds = []
    finally:
        c.close()

    def ts_fmt(ts: float | None) -> str:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "—"

    dead_count = sum(1 for p in players if (p["hp"] or 0) <= 0)

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

    def status_html(p: sqlite3.Row) -> str:
        dead = (p["hp"] or 0) <= 0
        cls = "dead" if dead else "alive"
        label = "💀 Dead" if dead else "🟢 Alive"
        return f'<span class=status-{cls}>{label} ({p["hp"]}/{p["max_hp"]} HP)</span>'

    player_rows = "".join(
        f'<div class=row><span class=who>{html.escape(p["player_id"][:8])}</span>'
        f'<span class=pname>{html.escape(p["name"] or "?")} <span class=muted>({html.escape(p["klass"] or "?")})</span></span>'
        + (f'<span class=world><a href="/?campaign={html.escape(p["campaign_id"])}">{html.escape(p["campaign_id"])}</a></span>' if all_worlds else "")
        + f'<span class=status>{status_html(p)}</span>'
        f'<span class=n>{p["events"]} events</span>'
        f'<span class=ts>{ts_fmt(p["last_seen"])}</span></div>'
        for p in players
    ) or '<div class=empty>No players yet.</div>'

    world_rows = "".join(
        f'<div class=row><span class=world><a href="/metrics?campaign={html.escape(w["id"])}">{html.escape(w["id"])}</a></span>'
        f'<span class=pname>{html.escape(w["name"] or w["theme"] or "?")}</span>'
        f'<span class=n>{w["rooms"]} rooms</span>'
        f'<span class=n>{w["characters"]} chars</span>'
        f'<span class=n>{w["events"]} events</span></div>'
        for w in worlds
    ) or '<div class=empty>No worlds yet.</div>'

    title = "All worlds" if all_worlds else campaign_id
    worlds_card = (
        f'<div class=card><div class=num>{len(worlds)}</div><div class=label>Worlds</div></div>'
        if all_worlds else ""
    )
    worlds_section = (
        f'<section><h2>Worlds</h2>{world_rows}</section>' if all_worlds else ""
    )

    return f"""<!doctype html><html><head><meta charset=utf-8><title>Metrics — {html.escape(title)}</title>
<link rel=icon href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚔</text></svg>">
<style>
:root{{--bg:#0a0713;--panel:#150f24;--border:#2b2145;--border-soft:#221a38;--text:#e7e1f5;
  --muted:#8d7fae;--warm:#e8b339;--warm-bright:#f5cc66;--ghost:#4fd8c4;--ghost-bright:#8ff0e0;
  --bad:#e85d5d;--bad-bright:#ff8a8a}}
body{{margin:0;background:var(--bg);color:var(--text);font:13px 'IBM Plex Mono',ui-monospace,Menlo,monospace}}
header{{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:baseline}}
h1{{font-size:16px;margin:0;color:var(--warm-bright)}}
.count{{color:var(--muted)}}
main{{padding:14px 20px;max-width:960px}}
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
.n{{color:var(--text);flex-shrink:0;width:90px;text-align:right}}
.ts{{color:var(--muted);flex-shrink:0;width:150px}}
.who{{color:var(--ghost);flex-shrink:0;width:80px}}
.pname{{flex:1}}
.muted{{color:var(--muted)}}
.world{{flex-shrink:0;width:110px}}
.world a{{color:var(--ghost-bright)}}
.status{{flex-shrink:0;width:150px}}
.status-alive{{color:var(--ghost-bright)}}
.status-dead{{color:var(--bad-bright)}}
.empty{{color:var(--muted);padding:10px 0}}
</style></head><body>
<header><h1>📊 Metrics</h1><span class=count>{html.escape(title)}</span>
{'<span class=count><a href="/metrics" style="color:var(--ghost-bright)">← all worlds</a></span>' if not all_worlds else ''}
</header>
<main>
<div class=cards>
{worlds_card}
 <div class=card><div class=num>{total_events}</div><div class=label>Events</div></div>
 <div class=card><div class=num>{unique_players}</div><div class=label>Players</div></div>
 <div class=card><div class=num>{unique_ips}</div><div class=label>Unique IPs</div></div>
 <div class=card><div class=num>{flash_calls}</div><div class=label>Flash calls</div></div>
 <div class=card><div class=num>{len(players) - dead_count}</div><div class=label>Characters alive</div></div>
 <div class=card><div class=num>{dead_count}</div><div class=label>Characters dead</div></div>
</div>
{worlds_section}
<section><h2>Events by kind</h2>{kind_rows}</section>
<section><h2>Activity, last 24h (hourly)</h2>{hour_rows}</section>
<section><h2>Characters{' — all worlds' if all_worlds else ''}</h2>{player_rows}</section>
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
    # backfill=1 -> everything since seq 0 (the Flash-history page); backfill=recent -> just
    # the trailing ~20 matching events, so a fresh tab shows the world's recent life instead
    # of an empty "waiting for the world to move..." until someone acts (user request).
    backfill = request.query_params.get("backfill") == "1"
    backfill_recent = request.query_params.get("backfill") == "recent"

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
                if backfill_recent:
                    # The seq BEFORE the campaign's 20th-newest matching event — the poll
                    # loop below then naturally serves those 20 as its first batch. Scoped
                    # to the same filters as the live stream so "recent" means recent HERE.
                    row = c.execute(
                        f"SELECT MIN(seq) FROM (SELECT seq FROM log WHERE 1=1{extra_where}"
                        f" ORDER BY seq DESC LIMIT 20)", params).fetchone()
                    last_seq = (row[0] - 1) if row and row[0] else 0
                else:
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
                # player_id IS the game's bearer credential (see /state above) -- this SSE
                # feed is the SAME public, unauthenticated surface, and dict(r) here pulls the
                # raw log row straight from sqlite, full player_id included. The page's own JS
                # only ever does ev.player_id.slice(0,6) for DISPLAY -- that's cosmetic, not a
                # guarantee, since the full value is still sitting in the wire payload for
                # anyone to read with devtools or curl. Truncate server-side before it ever
                # leaves the process, same 6 chars the UI already shows, so there's nothing
                # longer to capture. subject_id carries the identical credential when
                # subject_type='character' (e.g. story.exported writes subject_id=player_id),
                # so it needs the same treatment or it'd leak right back through that field.
                payload = dict(r)
                if payload.get("player_id"):
                    payload["player_id"] = payload["player_id"][:6]
                if payload.get("subject_type") == "character" and payload.get("subject_id"):
                    payload["subject_id"] = payload["subject_id"][:6]
                yield {"event": "world-event", "data": json.dumps(payload)}
            await asyncio.sleep(1)
    return EventSourceResponse(gen())


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("GUI_PORT", "8001")),
                log_level="warning")


if __name__ == "__main__":
    main()
