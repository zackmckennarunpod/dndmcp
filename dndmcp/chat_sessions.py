"""chat_sessions.py — server-side session store for the browser chat pane (e0b.3).

WHY its own module and not just more of web.py: web.py is already a giant PAGE HTML string
plus a dozen routes; this owns exactly one job — mapping an HttpOnly cookie (PLUS the page's
own world) to one browser player's dm_loop.DMSession, plus the concurrency/abuse guards a
single warm LLM worker needs. That job has its own small pile of state (a dict, a per-session
lock dict, a semaphore, the rate-limit windows) that has nothing to do with rendering the map,
so it gets to live on its own.

SECURITY BOUNDARY: session_id is a server-minted secrets.token_hex(16), delivered ONLY as an
HttpOnly cookie — client JS can never read it, so it can never be exfiltrated via XSS the way
a JS-visible token could. dm_loop.DMSession.player_id (the game's actual bearer credential)
never leaves this process at all: it's looked up server-side from session_id, same "the model/
browser only ever acts through what we hand it" boundary dm_loop.py's own docstring describes
one layer up.

PER-WORLD IDENTITY (e0b.10): a single dm_session cookie now anchors ONE browser across MANY
worlds at once — the chat operates in whatever world the PAGE it's on names (see web.py's
POST /chat "campaign" body field), and the same browser can hold an independent character in
main AND in any number of other worlds simultaneously. Every piece of state this module keeps
is therefore keyed by the PAIR (session_id, campaign_id), not session_id alone: _sessions,
_locks, and the per-session-per-world lifetime message cap. session_id by itself no longer
identifies "a player" — it only identifies "a browser"; (session_id, campaign_id) identifies
one specific character in one specific world.

PERSISTENCE (e0b.3 baseline, extended by e0b.4, widened to per-world by e0b.10): _sessions
(in-memory DMSession, including message HISTORY) dies on redeploy/process restart — an
accepted tradeoff (see the bead): full conversational context is lost. But state.py's
`web_session_world` table durably remembers WHICH character this browser was playing IN EACH
world, so get_or_create below can rebuild a fresh DMSession already pointed at that same real
character instead of silently starting a new one — see _resume_from_durable_store.
"""

from __future__ import annotations

import asyncio
import collections
import secrets
import time

from . import dm_loop, server

COOKIE_NAME = "dm_session"

# A (session_id, campaign_id) pair — the actual identity key everywhere in this module now
# that one browser can hold a character in more than one world at once (see module docstring).
SessionKey = tuple[str, str]

# Full turn concurrency cap across ALL sessions/worlds — the one warm vLLM worker dm_loop
# talks to (see dm_loop.DND_DM_BASE_URL) can't usefully serve unlimited simultaneous browser
# turns.
MAX_CONCURRENT_TURNS = 4
turn_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TURNS)

_sessions: dict[SessionKey, dm_loop.DMSession] = {}
_locks: dict[SessionKey, asyncio.Lock] = {}


def new_session_id() -> str:
    """32 hex chars, server-minted — never accepted FROM the client as an input, only ever
    handed out (see web.py's POST /chat: a session_id from cookies is looked up, never a
    session_id supplied any other way)."""
    return secrets.token_hex(16)


def _resume_from_durable_store(session_id: str, campaign_id: str) -> dm_loop.DMSession | None:
    """The "seen before a redeploy, cookie survived, the in-memory _sessions store didn't"
    case (now scoped to ONE world): look up (session_id, campaign_id) in state.py's
    web_session_world table (survives a process restart unlike _sessions above) and, if it
    names a player_id that STILL has a real character (world.character — a wiped/reset world
    means no character, same as never having played), rebuild a DMSession already pointed at
    it. Returns None (caller falls back to a brand new session) when there's no durable
    mapping for THIS world, or its player_id no longer resolves to a character."""
    ws = server.world.get_web_session_world(session_id, campaign_id)
    if not ws or not ws.player_id:
        return None
    if not server.world.character(ws.player_id):
        return None
    return dm_loop.create_resumed_session(ws.player_id, campaign_id)


def get_or_create(session_id: str, campaign_id: str) -> dm_loop.DMSession:
    """Look up this browser's DMSession FOR THIS WORLD, minting one the first time this
    (session_id, campaign_id) pair is seen. "First time seen" first tries to resume this
    session's own character in THIS world from the durable web_session_world mapping (see
    _resume_from_durable_store) before falling back to a genuinely fresh session pointed at
    campaign_id — that fallback covers both a true first-time visitor to this world and a
    resume attempt that found no (or no longer valid) durable mapping."""
    key = (session_id, campaign_id)
    session = _sessions.get(key)
    if session is None:
        session = _resume_from_durable_store(session_id, campaign_id) or dm_loop.create_session(campaign_id)
        _sessions[key] = session
    return session


def drop(session_id: str, campaign_id: str) -> None:
    """Forget this browser's session IN ONE WORLD ONLY — the "new character" flow (web.py's
    POST /chat/reset), now per-world (e0b.10): resetting your character on a friend's world
    must not also abandon your character back in main, so only the (session_id, campaign_id)
    key for THIS page's world is cleared. Clears the in-memory session/lock/cap-notified state
    only; the caller also deletes the durable web_session_world row
    (state.delete_web_session_world). The dm_session cookie itself is never rotated anymore —
    it's a browser-wide anchor shared across every world, not a per-character credential (see
    module docstring). The character the session pointed at is deliberately NOT touched — it
    stays in the world as an abandoned ghost, same as any character whose player closed the
    tab forever."""
    key = (session_id, campaign_id)
    _sessions.pop(key, None)
    _locks.pop(key, None)
    _session_cap_notified.discard(key)


def get(session_id: str, campaign_id: str) -> dm_loop.DMSession | None:
    """Read-only lookup for callers that must NOT mint a new session as a side effect — e.g.
    /state resolving "who is this cookie's player, IN THIS WORLD" for map highlighting: a
    spectator tab with a stale/foreign cookie, or a cookie that only has a character in some
    OTHER world, should see nothing here, not silently get a brand new empty session."""
    return _sessions.get((session_id, campaign_id))


def get_if_resumable(session_id: str, campaign_id: str) -> dm_loop.DMSession | None:
    """Like get() — never mints a genuinely NEW (empty, no character) session as a side
    effect — but WILL resume an already-real character from the durable web_session_world
    mapping if one exists and isn't already in memory under this key. This is what lets
    /state show "you" immediately after the e0b.10 new-world redirect: a turn that just
    minted a brand-new world updates session.campaign_id in place (see
    dm_loop._tool_start_adventure) and writes the durable row under that NEW campaign_id
    (POST /chat's finally-bookkeeping), but the in-memory DMSession object itself is still
    only reachable under the OLD (session_id, campaign_id) key it was created with — nothing
    ever re-keys it (an accepted simplification, see chat_sessions.py's module docstring).
    Without this, the redirected page's very first /state poll would show no character at
    all until the player's first /chat message on that page (get_or_create) resumed it.
    Safe to use from a passive GET precisely because it can only ever surface a character
    that TRULY already exists in this world — a stranger's foreign/stale cookie with no real
    mapping here still resolves to None, exactly like get()."""
    key = (session_id, campaign_id)
    session = _sessions.get(key)
    if session is not None:
        return session
    session = _resume_from_durable_store(session_id, campaign_id)
    if session is not None:
        _sessions[key] = session
    return session


def lock_for(session_id: str, campaign_id: str) -> asyncio.Lock:
    """One asyncio.Lock per (session, world), created lazily — this is what makes 'one turn
    in flight per session per world' enforceable: web.py checks .locked() (409s if already
    held) then acquires it for the duration of the streamed turn, see POST /chat. Scoped per
    world so a turn running in main never blocks (or gets blocked by) a turn running in some
    other world under the same browser."""
    key = (session_id, campaign_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


# --- abuse guards (e0b.4) --------------------------------------------------------------------
# Per-IP sliding-window throttle: at most this many /chat turns in any trailing 60s window,
# per client IP (web.py's _client_ip — X-Forwarded-For-first, since the pod sits behind
# Runpod's proxy). In-memory/per-process on purpose, same tradeoff as _sessions above: it
# resets on redeploy, which is the safe direction to be wrong in for an abuse guard (a fresh
# process never falsely blocks a legitimate burst that happens to follow a restart). NOT
# per-world — a browser hammering across many worlds at once is exactly the burst pattern
# this guard exists to catch, so it stays IP-wide.
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


# Per-(session, world) lifetime cap (widened from per-session by e0b.10 — see module
# docstring) — generous for real play, stops infinite burn from one abandoned (or
# deliberately abusive) browser session in any ONE world. Backed by state.py's
# web_session_world.message_count (see World.touch_web_session_world), NOT an in-memory
# counter — it must survive a redeploy the same way the identity mapping itself does,
# otherwise a restart would silently refill everyone's budget. Note this means the cap is now
# counted PER WORLD, not per browser overall — an accepted consequence of identity itself
# being scoped per world (a browser with characters in 5 worlds effectively has 5x the old
# budget, spread across them; see the e0b.10 task notes).
MAX_SESSION_MESSAGES = 300

# Same "log only the first rejection" idea as _ip_currently_throttled above, scoped to
# (session_id, campaign_id) instead of ip — once a session trips the lifetime cap IN ONE
# WORLD it STAYS tripped forever for that world, so without this a client that keeps posting
# after being capped would log on every attempt.
_session_cap_notified: set[SessionKey] = set()


def session_cap_exceeded(key: SessionKey, message_count: int) -> tuple[bool, bool]:
    """Returns (exceeded, first_time_over_cap) for the per-(session, world) lifetime message
    cap. `key` is (session_id, campaign_id)."""
    exceeded = message_count >= MAX_SESSION_MESSAGES
    if not exceeded:
        return False, False
    first_time = key not in _session_cap_notified
    if first_time:
        _session_cap_notified.add(key)
    return True, first_time
