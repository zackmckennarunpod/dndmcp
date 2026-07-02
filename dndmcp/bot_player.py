"""bot_player.py — an autonomous, self-playing character.

Turns the browser-DM harness (dm_loop.py) inward: instead of a human typing an action, a
second small LLM persona (PLAYER_SYSTEM_PROMPT below) decides what the character does each
turn, and dm_loop.handle_message resolves/narrates it exactly the same way it does for a real
browser player — same sanitization, same tool surface, same reliability behavior. The two
roles ping-pong against the SAME Flash-hosted model dm_loop.py already uses in production
(DND_DM_BASE_URL/DND_DM_MODEL) — one prompt plays the character, the other plays the table.

(An earlier attempt ran this through OpenCode connected directly to the MCP server instead —
abandoned after OpenCode's @ai-sdk/openai-compatible provider got silently empty responses
from this exact vLLM endpoint despite raw curl working fine; not worth blocking on an
unresolved third-party compatibility gap when dm_loop.py already does the same job, proven,
in production.)

Controlled entirely through admin_flags (no redeploy needed): bots_enabled (bool),
bots_count (int) — see scripts/pod_set_flag.sh. A background supervisor task
(start_supervisor, called once from web.py's startup) polls these every SUPERVISOR_POLL_S and
starts/stops individual bot loops to match, live. Bots only ever play in MAIN_CAMPAIGN_ID for
now — spawning them into an arbitrary world is a plausible follow-up, not needed yet.
"""

from __future__ import annotations

import asyncio
import logging

from . import admin_flags, dm_loop
from .state import MAIN_CAMPAIGN_ID

logger = logging.getLogger(__name__)

SUPERVISOR_POLL_S = 15    # how often the supervisor reconciles desired vs running bots
BOT_TURN_INTERVAL_S = 20  # pacing between one bot's actions — real GPU work each turn, and
                          # shouldn't drown a small world in activity
MAX_PLAYER_HISTORY = 16   # a plain chat history (no tool_calls to keep paired, unlike
                          # dm_loop's own _truncate_history), so a trailing slice is safe

PLAYER_SYSTEM_PROMPT = """You are playing a character in a live tabletop RPG, entirely on \
your own — there is no human telling you what to do. Every message you receive is either \
"begin" (you have no character yet) or the Dungeon Master's narration of what just happened. \
Reply with ONE short, first-person sentence describing your NEXT action — something a \
curious, mildly cautious adventurer would actually do: explore, fight, talk to someone, pick \
things up, pursue a goal. Never ask a question, never narrate an outcome yourself, and don't \
default to repeating "look around" turn after turn. If the DM says you're dead, reply with a \
one-line wish to start over as a new character in a different invented theme."""


async def _decide_action(history: list[dict]) -> str:
    """One call to the SAME Flash endpoint dm_loop.py's DM uses, no tools — just a short
    plain-text decision. Reuses dm_loop's own proven request plumbing instead of a second
    bespoke HTTP client."""
    messages = [{"role": "system", "content": PLAYER_SYSTEM_PROMPT}] + history
    try:
        message = await dm_loop._chat(messages, tools=[])
    except Exception:
        logger.exception("bot_player._decide_action: chat completion failed")
        return "I wait and watch, uncertain what to do next."
    return (message.get("content") or "").strip() or "I press onward."


async def run_bot(stop_event: asyncio.Event) -> None:
    """One bot character's whole lifetime loop — runs until stop_event is set (the
    supervisor shrank bots_count) or the app exits. Bootstraps its own character on the
    first turn, then alternates: the player decides an action -> dm_loop resolves/narrates
    it -> the narration feeds back into the player's own history as what it just observed."""
    session = dm_loop.create_session(MAIN_CAMPAIGN_ID)
    history: list[dict] = [{"role": "user", "content": "begin"}]
    marked_bot = False
    while not stop_event.is_set():
        try:
            action = await _decide_action(history)
            history.append({"role": "assistant", "content": action})

            narration = ""
            async for event in dm_loop.handle_message(session, action):
                if event["type"] == "text":
                    narration = event["text"]

            # Only needs to happen once, right after start_adventure mints the real
            # player_id — everything else about this character is completely normal from
            # here on.
            if not marked_bot and session.player_id:
                from . import server  # deferred: avoids a module-load cycle with dm_loop
                server.world.mark_bot(session.player_id)
                marked_bot = True

            history.append({"role": "user", "content": narration or "(nothing seems to happen)"})
            if len(history) > MAX_PLAYER_HISTORY:
                history = history[-MAX_PLAYER_HISTORY:]
        except Exception:
            # A bad turn must never silently kill the whole bot — same reliability-first
            # posture as every other tool call in this codebase. Back off a full interval so
            # a persistently broken turn doesn't spin-loop against the Flash endpoint.
            logger.exception("bot_player.run_bot: turn failed, will retry next interval")

        for _ in range(BOT_TURN_INTERVAL_S):
            if stop_event.is_set():
                return
            await asyncio.sleep(1)


# {slot_key: (task, stop_event)} — module-level since start_supervisor is a singleton, one
# per app process (see web.py's startup hook).
_running: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}


async def _reconcile() -> None:
    enabled = admin_flags.enabled("bots_enabled", default=False)
    count = admin_flags.get_int("bots_count", default=0) if enabled else 0
    desired = {f"bot-{i}" for i in range(count)}

    for key in list(_running):
        if key not in desired:
            task, stop_event = _running.pop(key)
            stop_event.set()

    for key in desired:
        if key not in _running:
            stop_event = asyncio.Event()
            task = asyncio.create_task(run_bot(stop_event))
            _running[key] = (task, stop_event)


async def start_supervisor() -> None:
    """Call once at app startup (see web.py). Runs forever, polling admin_flags so bots can
    be turned on/off/scaled live via scripts/pod_set_flag.sh — no redeploy needed:
        scripts/pod_set_flag.sh bots_enabled 1
        scripts/pod_set_flag.sh bots_count 2
    """
    while True:
        try:
            await _reconcile()
        except Exception:
            logger.exception("bot_player supervisor: reconcile failed")
        await asyncio.sleep(SUPERVISOR_POLL_S)
