"""chat_sessions.py — server-side session store for the browser chat pane (e0b.3).

WHY its own module and not just more of web.py: web.py is already a giant PAGE HTML string
plus a dozen routes; this owns exactly one job — mapping an HttpOnly cookie to one browser
player's dm_loop.DMSession, plus the concurrency guards a single warm LLM worker needs. That
job has its own small pile of state (a dict, a per-session lock dict, a semaphore) that has
nothing to do with rendering the map, so it gets to live on its own.

SECURITY BOUNDARY: session_id is a server-minted secrets.token_hex(16), delivered ONLY as an
HttpOnly cookie — client JS can never read it, so it can never be exfiltrated via XSS the way
a JS-visible token could. dm_loop.DMSession.player_id (the game's actual bearer credential)
never leaves this process at all: it's looked up server-side from session_id, same "the model/
browser only ever acts through what we hand it" boundary dm_loop.py's own docstring describes
one layer up.

PERSISTENCE: in-memory only, keyed by session_id. Dies on redeploy/process restart — an
accepted hackathon tradeoff (see the bead): a player's browser still holds the (now orphaned)
cookie, gets a fresh DMSession with player_id=None on their next message, and dm_loop's own
_require_started flow just asks them to start a new adventure, same as a first-time visitor.
No crash, no confusing error — just a fresh character.
"""

from __future__ import annotations

import asyncio
import secrets

from . import dm_loop

COOKIE_NAME = "dm_session"

# Full turn concurrency cap across ALL sessions — the one warm vLLM worker dm_loop talks to
# (see dm_loop.DND_DM_BASE_URL) can't usefully serve unlimited simultaneous browser turns.
# Full rate limiting (per-IP, per-session cooldowns, etc.) is a separate bead (e0b.4); this is
# just the floor so a burst of browser tabs can't hang the one shared endpoint for everyone.
MAX_CONCURRENT_TURNS = 4
turn_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TURNS)

_sessions: dict[str, dm_loop.DMSession] = {}
_locks: dict[str, asyncio.Lock] = {}


def new_session_id() -> str:
    """32 hex chars, server-minted — never accepted FROM the client as an input, only ever
    handed out (see web.py's POST /chat: a session_id from cookies is looked up, never a
    session_id supplied any other way)."""
    return secrets.token_hex(16)


def get_or_create(session_id: str) -> dm_loop.DMSession:
    """Look up this browser session's DMSession, minting a fresh one (campaign_id defaults to
    the shared "main" world, per dm_loop.create_session) the first time this session_id is
    seen — including the "seen before a redeploy, cookie survived, session store didn't" case
    described in the module docstring."""
    session = _sessions.get(session_id)
    if session is None:
        session = dm_loop.create_session()
        _sessions[session_id] = session
    return session


def get(session_id: str) -> dm_loop.DMSession | None:
    """Read-only lookup for callers that must NOT mint a new session as a side effect — e.g.
    /state resolving "who is this cookie's player" for map highlighting: a spectator tab with
    a stale/foreign cookie should see nothing, not silently get a brand new empty session."""
    return _sessions.get(session_id)


def lock_for(session_id: str) -> asyncio.Lock:
    """One asyncio.Lock per session, created lazily — this is what makes 'one turn in flight
    per session' enforceable: web.py checks .locked() (409s if already held) then acquires it
    for the duration of the streamed turn, see POST /chat."""
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock
