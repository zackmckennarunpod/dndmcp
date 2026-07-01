"""chat_sessions.py — server-side session store for the browser chat pane (e0b.3).

WHY its own module and not just more of web.py: web.py is already a giant PAGE HTML string
plus a dozen routes; this owns exactly one job — mapping an HttpOnly cookie to one browser
player's dm_loop.DMSession, plus the concurrency/abuse guards a single warm LLM worker needs.
That job has its own small pile of state (a dict, a per-session lock dict, a semaphore, the
rate-limit windows) that has nothing to do with rendering the map, so it gets to live on its
own.

SECURITY BOUNDARY: session_id is a server-minted secrets.token_hex(16), delivered ONLY as an
HttpOnly cookie — client JS can never read it, so it can never be exfiltrated via XSS the way
a JS-visible token could. dm_loop.DMSession.player_id (the game's actual bearer credential)
never leaves this process at all: it's looked up server-side from session_id, same "the model/
browser only ever acts through what we hand it" boundary dm_loop.py's own docstring describes
one layer up.

PERSISTENCE (e0b.3 baseline, extended by e0b.4): _sessions (in-memory DMSession, including
message HISTORY) dies on redeploy/process restart — an accepted tradeoff (see the bead): full
conversational context is lost. But state.py's `web_session` table durably remembers WHICH
character this browser was playing, so get_or_create below can rebuild a fresh DMSession
already pointed at that same real character instead of silently starting a new one — see
_resume_from_durable_store.
"""

from __future__ import annotations

import asyncio
import collections
import secrets
import time

from . import dm_loop, server
from .state import MAIN_CAMPAIGN_ID

COOKIE_NAME = "dm_session"

# Full turn concurrency cap across ALL sessions — the one warm vLLM worker dm_loop talks to
# (see dm_loop.DND_DM_BASE_URL) can't usefully serve unlimited simultaneous browser turns.
MAX_CONCURRENT_TURNS = 4
turn_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TURNS)

_sessions: dict[str, dm_loop.DMSession] = {}
_locks: dict[str, asyncio.Lock] = {}


def new_session_id() -> str:
    """32 hex chars, server-minted — never accepted FROM the client as an input, only ever
    handed out (see web.py's POST /chat: a session_id from cookies is looked up, never a
    session_id supplied any other way)."""
    return secrets.token_hex(16)


def _resume_from_durable_store(session_id: str) -> dm_loop.DMSession | None:
    """The "seen before a redeploy, cookie survived, the in-memory _sessions store didn't"
    case: look up session_id in state.py's web_session table (survives a process restart
    unlike _sessions above) and, if it names a player_id that STILL has a real character
    (world.character — a wiped/reset world means no character, same as never having played),
    rebuild a DMSession already pointed at it. Returns None (caller falls back to a brand new
    session) when there's no durable mapping, or its player_id no longer resolves to a
    character."""
    ws = server.world.get_web_session(session_id)
    if not ws or not ws.player_id:
        return None
    if not server.world.character(ws.player_id):
        return None
    return dm_loop.create_resumed_session(ws.player_id, ws.campaign_id or MAIN_CAMPAIGN_ID)


def get_or_create(session_id: str) -> dm_loop.DMSession:
    """Look up this browser session's DMSession, minting one the first time this session_id
    is seen. "First time seen" first tries to resume this session's own character from the
    durable web_session mapping (see _resume_from_durable_store) before falling back to a
    genuinely fresh session (campaign_id defaults to the shared "main" world, per
    dm_loop.create_session) — that fallback covers both a true first-time visitor and a
    resume attempt that found no (or no longer valid) durable mapping."""
    session = _sessions.get(session_id)
    if session is None:
        session = _resume_from_durable_store(session_id) or dm_loop.create_session()
        _sessions[session_id] = session
    return session


def drop(session_id: str) -> None:
    """Forget this browser session entirely — the "new character" flow (web.py's POST
    /chat/reset). Clears the in-memory session/lock/cap-notified state only; the caller also
    deletes the durable web_session row (state.delete_web_session) and rotates the cookie.
    The character the session pointed at is deliberately NOT touched — it stays in the world
    as an abandoned ghost, same as any character whose player closed the tab forever."""
    _sessions.pop(session_id, None)
    _locks.pop(session_id, None)
    _session_cap_notified.discard(session_id)


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


# --- abuse guards (e0b.4) --------------------------------------------------------------------
# Per-IP sliding-window throttle: at most this many /chat turns in any trailing 60s window,
# per client IP (web.py's _client_ip — X-Forwarded-For-first, since the pod sits behind
# Runpod's proxy). In-memory/per-process on purpose, same tradeoff as _sessions above: it
# resets on redeploy, which is the safe direction to be wrong in for an abuse guard (a fresh
# process never falsely blocks a legitimate burst that happens to follow a restart).
MAX_MESSAGES_PER_IP_PER_MINUTE = 10
_RATE_WINDOW_SECONDS = 60.0

_ip_hits: dict[str, collections.deque[float]] = {}
# Tracks which IPs are CURRENTLY over the limit — what lets web.py log exactly one
# dm.throttled event per throttle window instead of one per rejected request (a 50-request
# burst would otherwise write 40+ near-identical log rows for the same underlying fact).
_ip_currently_throttled: set[str] = set()


def check_ip_rate_limit(ip: str | None) -> tuple[bool, bool]:
    """Returns (allowed, first_throttle_in_window). `hits` holds only the timestamps of
    ALLOWED requests, so once an IP is throttled the deque stops growing and naturally empties
    itself out 60s after its last allowed request — that's what re-opens the window without
    any separate reset/expiry bookkeeping. `first_throttle_in_window` is True only the first
    time THIS IP gets rejected since it last dropped back under the limit."""
    if ip is None:
        return True, False  # no IP to key on — never throttle blind rather than crash
    now = time.monotonic()
    hits = _ip_hits.setdefault(ip, collections.deque())
    while hits and now - hits[0] > _RATE_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= MAX_MESSAGES_PER_IP_PER_MINUTE:
        first_throttle = ip not in _ip_currently_throttled
        _ip_currently_throttled.add(ip)
        return False, first_throttle
    hits.append(now)
    _ip_currently_throttled.discard(ip)
    return True, False


# Per-session lifetime cap — generous for real play, stops infinite burn from one abandoned
# (or deliberately abusive) browser session. Backed by state.py's web_session.message_count
# (see World.touch_web_session), NOT an in-memory counter — it must survive a redeploy the
# same way the identity mapping itself does, otherwise a restart would silently refill
# everyone's budget.
MAX_SESSION_MESSAGES = 300

# Same "log only the first rejection" idea as _ip_currently_throttled above, scoped to
# session_id instead of ip — once a session trips the lifetime cap it STAYS tripped forever,
# so without this a client that keeps posting after being capped would log on every attempt.
_session_cap_notified: set[str] = set()


def session_cap_exceeded(session_id: str, message_count: int) -> tuple[bool, bool]:
    """Returns (exceeded, first_time_over_cap) for the per-session lifetime message cap."""
    exceeded = message_count >= MAX_SESSION_MESSAGES
    if not exceeded:
        return False, False
    first_time = session_id not in _session_cap_notified
    if first_time:
        _session_cap_notified.add(session_id)
    return True, first_time
