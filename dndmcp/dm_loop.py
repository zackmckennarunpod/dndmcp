"""dm_loop.py — the server-side Dungeon Master agent loop (browser/GUI path, e0b.2).

WHY this exists as its own module (not folded into server.py or web.py): server.py's tools
are built for the MCP case — a human's OWN agent (Claude Desktop, Claude Code, ...) reads
DM_PERSONA and BECOMES the Dungeon Master, calling these tools directly. A browser player
has no agent of their own in that loop at all. Something on OUR side has to drive an actual
LLM through its own tool-calling turn: read the player's chat line, decide which game tools
to call, execute them for real against the World, and narrate the result. That's this file.

It's a plain OpenAI-chat-completions agent loop — provider-agnostic on purpose (urllib in a
thread executor, no vendor SDK, exactly flash_llm._chat_sync's proven pattern) so it works
against ANY OpenAI-compatible /v1/chat/completions host, not just the one Flash endpoint
DND_DM_BASE_URL defaults to.

THE ACTUAL DESIGN CENTER OF THIS MODULE — the prompt-injection boundary:
A browser player's chat text becomes part of the model's own context, and a model can be
talked into saying almost anything. But a model can only ever ACT through the tool calls it
emits, and every tool exposed here is a closure over ONE DMSession's player_id, captured once
at session/adventure-start time. player_id (and item_id/quest_id) never appear in any tool's
JSON schema — there is no parameter shape in which the model could name a different session's
character — and they never appear in any tool RESULT text fed back into the model's context
either (see _sanitize/_sanitize_scene: this is server.py's own "internal plumbing, never
quote it back to the player" rule, enforced mechanically here instead of by an agent's good
behavior). A rogue model can act as its own character and nothing else.

Public API:
    create_session(campaign_id=MAIN_CAMPAIGN_ID) -> DMSession
    async def handle_message(session, user_text) -> AsyncIterator[dict]
        yields {"type": "tool", "name": ..., "summary": ...} as each tool executes, then
        exactly one {"type": "text", "text": ...} with the final narration. This is the exact
        event shape the future /chat SSE endpoint (e0b.3) forwards to the browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import urllib.request
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from . import server
from .state import MAIN_CAMPAIGN_ID

logger = logging.getLogger(__name__)

# --- backend config -----------------------------------------------------------------------
# Defaults point at the live Qwen2.5-7B-Instruct Flash endpoint verified (2026-07-01) to speak
# OpenAI tool-calling correctly via vLLM's hermes parser (see flash_llm.py's ENABLE_AUTO_
# TOOL_CHOICE/TOOL_CALL_PARSER env, deployed for that endpoint). Every knob is overridable so
# this module works against any OpenAI-compatible host — that's the point of hand-rolling the
# HTTP call instead of hardcoding a client for one provider.
DND_DM_BASE_URL = os.environ.get(
    "DND_DM_BASE_URL", "https://api.runpod.ai/v2/q1ruzcnbog3oz1/openai/v1")
DND_DM_MODEL = os.environ.get("DND_DM_MODEL", "Qwen/Qwen2.5-7B-Instruct")

MAX_TOOL_CALLS_PER_TURN = 6   # hard cap — a stuck/looping model must not hang a player's turn
MAX_HISTORY_MESSAGES = 24     # excluding the system message; see _truncate_history
MAX_TOKENS = 350
TEMPERATURE = 0.6
_CHAT_TIMEOUT_S = 120         # cold start on a scaled-to-zero Flash endpoint can take ~90s


def _api_key() -> str:
    """Resolve the DM loop's own API key: DND_DM_API_KEY first (this module's own override,
    since it may point at a completely different OpenAI-compatible host than flash_llm.py's
    world-gen endpoint), then RUNPOD_API_KEY (env), then the macOS keychain fallback — same
    chain/shape as flash_llm._api_key, duplicated rather than imported so this module has no
    hard dependency on flash_llm's endpoint-specific ensure()/teardown() lifecycle."""
    if os.environ.get("DND_DM_API_KEY"):
        return os.environ["DND_DM_API_KEY"]
    if os.environ.get("RUNPOD_API_KEY"):
        return os.environ["RUNPOD_API_KEY"]
    key = subprocess.run(
        ["security", "find-generic-password", "-s", "runpod-api-key-prod", "-w"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    os.environ["RUNPOD_API_KEY"] = key
    return key


# --- system prompt -------------------------------------------------------------------------
# Adapted from server.py's DM_PERSONA for the browser context, using the tightened absolute-
# rules framing that scored 6/7 on the live 7B endpoint in the multiturn spike (see
# multiturn_probe.py) — short numbered RULES, not DM_PERSONA's long prose, reads far more
# reliably as a system prompt for a 7B model driving real tool calls turn after turn.
SYSTEM_PROMPT = """You are the Dungeon Master for a solo/shared tabletop RPG, playing live in \
a browser chat window. RULES, absolute:

1. Call a tool for EVERY player action — moving, fighting, looking around, talking to
   someone, picking things up, resting, searching. Never narrate an action's outcome
   yourself; wait for the tool's result and narrate ONLY from what it actually returned. A
   monster only dies when a tool result says its hp reached 0 — if it's still above 0 it is
   STILL ALIVE, never narrate a death a tool didn't report. Never invent a dice roll, damage
   number, HP total, or an NPC's reply.
2. Unexplored means unknown. Never invent what's beyond an exit no one has gone through, or
   what's in a room no one has looked at yet — say so honestly instead.
3. NEVER mention compass directions (north/south/east/west/up/down), and NEVER mention any
   id (player_id, item_id, quest_id) or tool/mechanics plumbing, to the player. Describe
   exits ONLY by their own physical descriptor — "a warped iron door," "a spiral stair," "a
   gap in the collapsed wall." When the player says which way they're going ("I go through
   the iron door," "I take the stairs down"), call move with that same descriptor (or its
   listed number) — the game resolves which way that actually is.
4. If no adventure has started yet, ask the player for a short theme and a character
   name (offer to invent something evocative if they'd rather you pick), then call
   start_adventure. Do this before anything else.
5. Keep replies to 2-5 sentences, vivid but concise, and always end your turn with
   "What do you do?"
"""


def create_session(campaign_id: str = MAIN_CAMPAIGN_ID) -> "DMSession":
    """Factory for one browser session. campaign_id defaults to the shared "main" world per
    this task's scope (browser sessions join the shared world for now — world-choice UI is a
    later task, same as DM_PERSONA's numbered choice is for the MCP-agent path today)."""
    return DMSession(campaign_id=campaign_id,
                     messages=[{"role": "system", "content": SYSTEM_PROMPT}])


@dataclass
class DMSession:
    """One browser player's session. `player_id` is minted server-side inside
    _tool_start_adventure and stored here — it is NEVER a field the model can set (no tool
    schema below accepts it), which is the whole prompt-injection boundary this module exists
    to enforce. `exit_map` is derived, per-current-room state (descriptor -> real direction),
    rebuilt every time a tool changes/reveals the player's room — it's what lets the model
    address exits by descriptor only (see _resolve_direction) while server.move() still gets
    a real compass direction underneath."""
    campaign_id: str = MAIN_CAMPAIGN_ID
    player_id: str | None = None
    messages: list[dict] = field(default_factory=list)
    exit_map: dict[str, str] = field(default_factory=dict)


# --- tool schemas exposed to the model -------------------------------------------------------
# Deliberately a SUBSET of server.py's tools (see module docstring's SANITIZED TOOL SURFACE):
# no get_state (raw dict dump — leaks ids), no delete_world (destructive, not a browser-chat
# action), no dev_* tools, and update_quest's involve_entity/involve_location plumbing is left
# out too (that's DM_PERSONA judgment-call territory for a human-run agent, not something a
# 7B model driving its own turn needs exposed). None of these schemas accept player_id,
# item_id, or quest_id — that is the injection boundary, not a convenience.
TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "start_adventure",
        "description": ("Begin the adventure: create the player's character and drop them "
                        "into the opening scene. Call this once, right after you've asked "
                        "for (or offered to invent) a theme and a character name/class."),
        "parameters": {"type": "object", "properties": {
            "theme": {"type": "string",
                      "description": "Short tone/setting, e.g. 'gothic horror', 'deep-space salvage', 'high fantasy'."},
            "character_name": {"type": "string"},
            "character_class": {"type": "string", "description": "e.g. Fighter, Rogue, Wizard."},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "move",
        "description": ("Move the player through one of the CURRENT room's exits. `exit` "
                        "must be the exit's own descriptor as you narrated it to the player "
                        "(e.g. \"the warped iron door\", \"the spiral stair\") — NEVER a "
                        "compass direction — or its listed number."),
        "parameters": {"type": "object", "properties": {
            "exit": {"type": "string", "description": "The exit's descriptor, or its 1-based number."},
        }, "required": ["exit"]}}},
    {"type": "function", "function": {
        "name": "attack",
        "description": "Attack the monster in the player's current room. Resolves real dice — never invent the outcome yourself.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "look",
        "description": "Re-describe the current room: its scene, contents, and exits.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "sense_surroundings",
        "description": ("Call when the player investigates a noise or searches for something "
                        "unseen without moving. Returns only what's actually known — never "
                        "invent beyond it, and a quiet 'nothing' is a real, valid answer."),
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "character_sheet",
        "description": "Show the player's own stats, HP, AC, and inventory.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "roll_dice",
        "description": "Roll dice for any check not already covered by attack, e.g. '1d20+3', '2d6'. Never invent a roll yourself.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "Dice expression, e.g. '1d20+2'."},
        }, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "pick_up_item",
        "description": "Pick up something in the current room and add it to the player's inventory. Name it in plain words.",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string"},
        }, "required": ["item_name"]}}},
    {"type": "function", "function": {
        "name": "drop_item",
        "description": "Leave something from the player's inventory in the current room.",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string"},
        }, "required": ["item_name"]}}},
    {"type": "function", "function": {
        "name": "talk_to",
        "description": "Talk to an NPC/monster in the current room. Generates their in-character reply.",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string"},
            "npc_name": {"type": "string", "description": "Only needed if more than one NPC is present."},
        }, "required": ["message"]}}},
    {"type": "function", "function": {
        "name": "start_quest",
        "description": ("Make a job/goal/plot thread real, trackable world state — call "
                        "right after an NPC offers one or the player sets a concrete goal."),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
        }, "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "active_quests",
        "description": "List the player's currently active quests.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "log_event",
        "description": ("Record something noteworthy the player did that no other tool "
                        "covers (reading a diary, searching a corpse, a detail you invented) "
                        "— becomes a durable trace later players can discover."),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
        }, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "remember",
        "description": "Your own private continuity note (an NPC's true motive, a lie you told) — never shown to the player, just kept for your own consistency.",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"},
        }, "required": ["note"]}}},
]


# --- result sanitization -------------------------------------------------------------------
# server.py's tool results are written for an AGENT reading raw MCP output (see its own
# docstrings/DM_PERSONA: "internal plumbing... never quote or paraphrase back to the
# player"). That's fine when a human's own agent enforces it by instruction; here the model
# never even gets the chance — every marker below is stripped before the text becomes part
# of the model's context.
_BRACKET_RE = re.compile(r"\s*\[(?:direction|item_id|quest_id):[^\]]*\]")
_ITEM_ID_NOTE_RE = re.compile(r"\s*\(item_id is for your own[^)]*\)")
# start_adventure's reply carries the player_id twice: the "**player_id: `...`**" callout
# line AND baked into the live-map link's query string (?player=<id>) — both must go, not
# just the bracketed markers above, since this one is a bare id in prose/URL, not `[x: y]`.
_PLAYER_ID_LINE_RE = re.compile(r"\n*\*\*player_id:[^\n]*\n?")
_MAP_LINK_LINE_RE = re.compile(r"\n*🗺[^\n]*\n?")
# The exits section of a rendered scene is rebuilt wholesale (see _sanitize_scene) rather
# than regex-patched, so the model gets clean descriptor-only lines instead of the leftover
# "Exits (describe by descriptor ONLY...)" agent-instruction header once its brackets are
# stripped. Matches server.py::_render_scene's exact block shape (header line + indented
# per-exit lines) so it can be sliced out and replaced.
_EXITS_BLOCK_RE = re.compile(r"\nExits \([^\n]*\):\n(?:  .*\n?)*")


def _sanitize(text: str) -> str:
    """General-purpose strip — safe to apply to ANY server.py tool result before it enters
    the model's context. Does not know about exits (see _sanitize_scene for that)."""
    text = _PLAYER_ID_LINE_RE.sub("\n", text)
    text = _MAP_LINK_LINE_RE.sub("\n", text)
    text = _BRACKET_RE.sub("", text)
    text = _ITEM_ID_NOTE_RE.sub("", text)
    return text.strip()


def _rebuild_exit_map(session: DMSession) -> None:
    """Recompute session.exit_map {descriptor: direction} for the player's CURRENT room,
    straight from server._adjacent_rooms — not by parsing rendered text — so it stays correct
    even if _render_scene's copy ever changes. Call this after any tool that moves the player
    or reveals a room (start_adventure, move, look). `descriptor` always mirrors
    _render_scene's own fallback ("an unmarked passage") so what the model sees and what
    _resolve_direction matches against are the same string."""
    ch = server.world.character(session.player_id) if session.player_id else None
    room = server.world.room(ch.location_id) if ch else None
    if not room:
        session.exit_map = {}
        return
    exit_map: dict[str, str] = {}
    for adj in server._adjacent_rooms(room, session.player_id):
        descriptor = adj["descriptor"] or "an unmarked passage"
        exit_map[descriptor] = adj["direction"]
    session.exit_map = exit_map


def _rebuild_exits_text(session: DMSession) -> str:
    """The descriptor-only exits block the model actually sees, numbered so `move` can also
    accept a 1-based index (handy when a model reasons better over a short numbered list than
    over free text matching)."""
    if not session.exit_map:
        return "\nExits: none known.\n"
    lines = ["\nExits:"]
    for i, descriptor in enumerate(session.exit_map, start=1):
        lines.append(f"  {i}. {descriptor}")
    return "\n".join(lines) + "\n"


def _sanitize_scene(raw: str, session: DMSession) -> str:
    """Sanitize a tool result that renders a full scene (start_adventure/move/look) — rebuilds
    the exits section from session.exit_map (already refreshed by the caller via
    _rebuild_exit_map) instead of just stripping brackets from server's own copy, then runs
    the same general _sanitize pass for everything else (player_id line, map link, any
    leftover item_id/quest_id markers in loot/quest lines elsewhere in the scene)."""
    raw = _EXITS_BLOCK_RE.sub(_rebuild_exits_text(session), raw)
    return _sanitize(raw)


def _resolve_direction(session: DMSession, exit_descriptor: str) -> tuple[str | None, str]:
    """Resolve a model-given exit descriptor (or 1-based index) to the real compass direction
    server.move() needs. Fuzzy substring match, case-insensitive, either direction (the
    descriptor contains the model's phrase, or vice versa — models paraphrase). Returns
    (direction, "") on a match, or (None, error_text) where error_text lists ONLY descriptors
    — never leaking a direction into the very error meant to guide the model away from
    guessing one."""
    exits = session.exit_map
    if not exits:
        return None, "There's nowhere to go from here yet — try look first."
    text = (exit_descriptor or "").strip()
    if text.isdigit():
        items = list(exits.items())
        idx = int(text) - 1
        if 0 <= idx < len(items):
            return items[idx][1], ""
        return None, f"No exit #{text}. Current exits: {', '.join(d for d, _ in items)}."
    needle = text.lower()
    if needle:
        for descriptor, direction in exits.items():
            low = descriptor.lower()
            if needle in low or low in needle:
                return direction, ""
    return None, f"No exit matches {exit_descriptor!r}. Current exits: {', '.join(exits)}."


def _require_started(session: DMSession) -> str | None:
    """Every tool but start_adventure needs a live character. Returns an error tool-result
    string (never raises) when there isn't one yet, so the model gets a normal tool result it
    can react to ("ask for a theme, call start_adventure") instead of a crash."""
    if not session.player_id:
        return ("No adventure has started yet for this player — ask for a theme and a "
                "character name (or offer to invent one), then call start_adventure.")
    return None


# --- tool wrappers: thin closures over ONE session's player_id ------------------------------
# Every wrapper is async (even where the underlying server.py function is sync) so
# handle_message can `await` all of them uniformly. None of these accept player_id as a
# parameter from the model — it is always session.player_id, injected here, never in any
# TOOLS[] schema above (see module docstring's security boundary).

async def _tool_start_adventure(session: DMSession, theme: str = "gothic horror",
                                character_name: str = "Wanderer",
                                character_class: str = "Fighter") -> str:
    raw = await server.start_adventure(theme=theme, character_name=character_name,
                                       character_class=character_class,
                                       campaign_id=session.campaign_id)
    # Extract the freshly-minted player_id from server.start_adventure's own reply — the
    # task brief calls out this is fine over re-implementing world-creation ourselves, since
    # the format ("**player_id: `<id>`**") is fixed and grep-able. It's captured into the
    # session and immediately scrubbed back out before the text goes anywhere near the model
    # (see _sanitize_scene) — this is the ONE moment player_id crosses from server text into
    # our process, and it never crosses again from here into the model's context.
    m = re.search(r"player_id:\s*`([0-9a-f]+)`", raw)
    if not m:
        raise RuntimeError("start_adventure did not return a player_id — server.py contract changed?")
    session.player_id = m.group(1)
    _rebuild_exit_map(session)
    return _sanitize_scene(raw, session)


async def _tool_move(session: DMSession, exit: str) -> str:
    err = _require_started(session)
    if err:
        return err
    direction, error = _resolve_direction(session, exit)
    if error:
        return error
    raw = await server.move(session.player_id, direction)
    _rebuild_exit_map(session)
    return _sanitize_scene(raw, session)


async def _tool_attack(session: DMSession) -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.attack(session.player_id))


async def _tool_look(session: DMSession) -> str:
    err = _require_started(session)
    if err:
        return err
    raw = server.look(session.player_id)
    _rebuild_exit_map(session)
    return _sanitize_scene(raw, session)


async def _tool_sense_surroundings(session: DMSession) -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.sense_surroundings(session.player_id))


async def _tool_character_sheet(session: DMSession) -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.character_sheet(session.player_id))


async def _tool_roll_dice(session: DMSession, expression: str = "1d20") -> str:
    # No player_id concept on this one (server.roll_dice takes only `expression`) — still
    # gated on an adventure being underway so a model can't roll dice before the game exists.
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.roll_dice(expression))


async def _tool_pick_up_item(session: DMSession, item_name: str) -> str:
    err = _require_started(session)
    if err:
        return err
    raw = await server.pick_up_item(session.player_id, item_name=item_name)
    return _sanitize(raw)


async def _tool_drop_item(session: DMSession, item_name: str) -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.drop_item(session.player_id, item_name=item_name))


async def _tool_talk_to(session: DMSession, message: str, npc_name: str | None = None) -> str:
    err = _require_started(session)
    if err:
        return err
    raw = await server.talk_to(session.player_id, message, npc_name=npc_name)
    return _sanitize(raw)


async def _tool_start_quest(session: DMSession, title: str, description: str = "") -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.start_quest(session.player_id, title, description=description))


async def _tool_active_quests(session: DMSession) -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.active_quests(session.player_id))


async def _tool_log_event(session: DMSession, text: str) -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.log_event(session.player_id, text))


async def _tool_remember(session: DMSession, note: str) -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.remember(session.player_id, note))


TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "start_adventure": _tool_start_adventure,
    "move": _tool_move,
    "attack": _tool_attack,
    "look": _tool_look,
    "sense_surroundings": _tool_sense_surroundings,
    "character_sheet": _tool_character_sheet,
    "roll_dice": _tool_roll_dice,
    "pick_up_item": _tool_pick_up_item,
    "drop_item": _tool_drop_item,
    "talk_to": _tool_talk_to,
    "start_quest": _tool_start_quest,
    "active_quests": _tool_active_quests,
    "log_event": _tool_log_event,
    "remember": _tool_remember,
}


# --- the OpenAI-compatible chat call ---------------------------------------------------------
def _chat_sync(messages: list[dict], tools: list[dict]) -> dict:
    """One POST to <base>/chat/completions — urllib in a thread executor, exactly
    flash_llm._chat_sync's proven shape (no new deps: no `openai`/`requests` package), which
    is what makes this loop work against ANY OpenAI-compatible host by just changing
    DND_DM_BASE_URL. Returns the raw `message` dict (content + tool_calls) — the loop needs
    tool_calls verbatim to drive itself, not just the text half flash_llm.generate() returns."""
    body = json.dumps({
        "model": DND_DM_MODEL, "messages": messages, "tools": tools, "tool_choice": "auto",
        "max_tokens": MAX_TOKENS, "temperature": TEMPERATURE,
    }).encode()
    url = f"{DND_DM_BASE_URL.rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_CHAT_TIMEOUT_S) as resp:  # cold start ~90s worst case
        data = json.load(resp)
    return data["choices"][0]["message"]


async def _chat(messages: list[dict], tools: list[dict]) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _chat_sync, messages, tools)


def _truncate_history(messages: list[dict]) -> list[dict]:
    """Keep the system message plus roughly the last MAX_HISTORY_MESSAGES entries — but NEVER
    split an assistant `tool_calls` message from the `tool` result message(s) that answered
    it, since an orphaned `role: tool` message with no matching preceding tool_calls (or vice
    versa) 400s an OpenAI-shaped API. Group the non-system history into atomic chunks (one
    assistant-with-tool_calls + all the tool replies that immediately follow it, or one plain
    user/assistant message on its own), then keep whole chunks from the most recent end
    until the budget runs out."""
    system = [m for m in messages if m["role"] == "system"][:1]
    rest = [m for m in messages if m["role"] != "system"]
    if len(rest) <= MAX_HISTORY_MESSAGES:
        return system + rest

    chunks: list[list[dict]] = []
    for m in rest:
        if m["role"] == "tool" and chunks:
            chunks[-1].append(m)
        else:
            chunks.append([m])

    kept: list[dict] = []
    budget = MAX_HISTORY_MESSAGES
    for chunk in reversed(chunks):
        if kept and len(chunk) > budget:
            break
        kept = chunk + kept
        budget -= len(chunk)
        if budget <= 0:
            break
    return system + kept


async def handle_message(session: DMSession, user_text: str) -> AsyncIterator[dict]:
    """Run one full player turn.

    Loop: call the model with the running history + TOOLS[] -> if it asks for tool_calls,
    execute each against the real game (via TOOL_HANDLERS, player_id injected, results
    sanitized), append the results, and call the model again -> repeat until it returns plain
    text with no tool_calls, or MAX_TOOL_CALLS_PER_TURN is reached. Yields events as it goes;
    this shape is intentionally identical to what e0b.3's /chat SSE endpoint will forward
    straight to the browser:
        {"type": "tool", "name": <tool>, "summary": <first line of its result, truncated>}
        {"type": "text", "text": <final narration>}   -- exactly once, always last

    Robustness: an unknown tool name or a tool_call whose arguments don't parse/match gets an
    error tool-result appended (so the model can see what went wrong and retry) — that's ONE
    free retry. A SECOND such failure in the same turn stops the loop rather than looping
    forever against a model that can't recover, and returns whatever text the model already
    produced (often none, alongside a bad tool_calls message) or a safe in-character line.
    """
    session.messages.append({"role": "user", "content": user_text})
    tool_call_count = 0
    malformed_count = 0

    while True:
        session.messages = _truncate_history(session.messages)
        try:
            message = await _chat(session.messages, TOOLS)
        except Exception:
            logger.exception("dm_loop.handle_message: chat completion failed")
            yield {"type": "text",
                  "text": "The DM pauses, momentarily lost in thought... (connection trouble — try again?)"}
            return

        tool_calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()

        if not tool_calls or tool_call_count >= MAX_TOOL_CALLS_PER_TURN:
            session.messages.append({"role": "assistant", "content": content})
            yield {"type": "text", "text": content or "The DM pauses. What do you do?"}
            return

        session.messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        stop_after_tools = False
        for tc in tool_calls:
            if tool_call_count >= MAX_TOOL_CALLS_PER_TURN:
                break
            tool_call_count += 1
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args: dict | None = json.loads(raw_args)
            except json.JSONDecodeError:
                args = None

            handler = TOOL_HANDLERS.get(name)
            if handler is None or args is None:
                malformed_count += 1
                result_text = (f"Unknown or malformed tool call {name!r}. Valid tools: "
                              f"{', '.join(TOOL_HANDLERS)}. Try again with valid arguments.")
            else:
                try:
                    result_text = await handler(session, **args)
                except TypeError as exc:
                    malformed_count += 1
                    result_text = f"Bad arguments for {name}: {exc}"
                except Exception:
                    logger.exception("dm_loop.handle_message: tool %s raised", name)
                    result_text = f"{name} failed unexpectedly — try a different action."

            summary = (result_text.splitlines()[0] if result_text else "")[:160]
            yield {"type": "tool", "name": name, "summary": summary}
            session.messages.append({
                "role": "tool", "tool_call_id": tc.get("id") or uuid.uuid4().hex,
                "content": result_text,
            })

            if malformed_count >= 2:
                stop_after_tools = True
                break

        if stop_after_tools:
            yield {"type": "text",
                  "text": content or "The DM pauses, considering... What do you do?"}
            return
