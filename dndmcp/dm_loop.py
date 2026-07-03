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

from . import admin_flags, server
from .state import MAIN_CAMPAIGN_ID

logger = logging.getLogger(__name__)

# --- backend config -----------------------------------------------------------------------
# Two known-good (endpoint_name, model) tiers, toggled LIVE via admin_flags -- no redeploy:
#   scripts/pod_set_flag.sh dm_use_14b 1   # switch the DM to the 14B endpoint
#   scripts/pod_set_flag.sh dm_use_14b 0   # back to the 7B default
# "low" is the long-running default (workers=(1,3), always-warm -- avoids the THROTTLED-
# worker problem flash_llm.py's own comment documents for scale-to-zero). "high" is the 14B
# endpoint validated via /evals (2026-07-03, two separate runs: ~50-58% vs ~82-92% tool-
# calling correctness) -- self-heals at workers=(0,3) if ever re-minted from scratch, since
# it's meant to stay bump-on-demand rather than commit to continuous cost by default. If you
# DO want "high" to be the standing live default (not just an eval/spot-check target), bump
# dnd-dm-vllm-14b's workersMin to 1 via mcp__runpod__update-endpoint first -- the toggle
# alone doesn't do that, and leaving it at min=0 means every idle gap re-pays the ~225s cold
# start on whichever real player's turn happens to land next.
DM_MODEL_TIERS: dict[str, tuple[str, str]] = {
    "low": (os.environ.get("DND_DM_ENDPOINT", "dnd-dm-vllm"),
            os.environ.get("DND_DM_MODEL", "Qwen/Qwen2.5-7B-Instruct")),
    "high": ("dnd-dm-vllm-14b", "Qwen/Qwen2.5-14B-Instruct"),
}
# Explicit override escape hatch (e.g. pointing at a different host entirely for local
# testing) -- empty/unset means "use the tier system above", same as before this existed.
DND_DM_BASE_URL = os.environ.get("DND_DM_BASE_URL", "")
DND_DM_IMAGE = "runpod/worker-v1-vllm:v2.22.4"

MAX_TOOL_CALLS_PER_TURN = 6   # hard cap — a stuck/looping model must not hang a player's turn
MAX_HISTORY_MESSAGES = 24     # excluding the system message; see _truncate_history
MAX_TOKENS = 350
TEMPERATURE = 0.6
_CHAT_TIMEOUT_S = 280         # covers cold start on EITHER tier -- the 7B endpoint's is ~60-90s,
                              # but the 14B endpoint's (ADA_48_PRO, bigger checkpoint) measured
                              # ~225s live (2026-07-04); 120s meant every first call on a cold
                              # 14B worker failed outright before the model ever responded --
                              # confirmed live as bot_player's "I wait and watch, uncertain what
                              # to do next" fallback firing on turn 1 of a fresh world


def current_dm_tier() -> str:
    """Read fresh every call (no caching) so flipping the flag takes effect on the very next
    turn, same contract as every other admin_flags toggle."""
    return "high" if admin_flags.enabled("dm_use_14b", default=False) else "low"


# Self-heal via the Flash SDK, same ensure()-by-name pattern already proven in flash_llm.py/
# flash_art.py — both endpoints already exist (created once), so in NORMAL operation this
# only ever hits the resolve-by-name branch below and returns instantly, per tier. The
# construct+deploy branch is a pure safety net: if a tier's endpoint is ever deleted, the
# next _chat call recreates it with this exact config instead of the DM silently staying
# broken until someone notices and redeploys by hand.
_DM_ENDPOINT_STATE: dict[str, str] = {}   # tier -> resolved endpoint_id
_DM_ENDPOINT_LOCK = asyncio.Lock()


async def _resolve_dm_endpoint_by_name(client, name: str) -> str | None:
    r = await client._execute_graphql("query { myself { endpoints { id name } } }", {})  # noqa: SLF001
    for e in r["myself"]["endpoints"]:
        if e["name"] == name:
            return e["id"]
    return None


async def ensure_dm_endpoint(tier: str | None = None) -> tuple[str, str]:
    """Resolve (or, only if it's ever gone, re-mint) the DM chat endpoint for `tier` (default:
    current_dm_tier(), the live admin_flags toggle). Returns (endpoint_id, model) — callers
    build the OpenAI-compatible base URL from the id and send `model` in the request body."""
    tier = tier or current_dm_tier()
    endpoint_name, model = DM_MODEL_TIERS[tier]
    if tier in _DM_ENDPOINT_STATE:
        return _DM_ENDPOINT_STATE[tier], model
    async with _DM_ENDPOINT_LOCK:
        if tier in _DM_ENDPOINT_STATE:
            return _DM_ENDPOINT_STATE[tier], model
        os.environ.setdefault("RUNPOD_API_BASE_URL", "https://api.runpod.io")
        os.environ.setdefault("RUNPOD_ENDPOINT_BASE_URL", "https://api.runpod.ai/v2")
        _api_key()
        from runpod_flash.core.api import RunpodGraphQLClient

        client = RunpodGraphQLClient()
        try:
            existing_id = await _resolve_dm_endpoint_by_name(client, endpoint_name)
        finally:
            await client.close()
        if existing_id:
            logger.info("dm_loop.ensure_dm_endpoint: %s (%s) already deployed -> %s (skipped re-trigger)",
                       tier, endpoint_name, existing_id)
            _DM_ENDPOINT_STATE[tier] = existing_id
            return existing_id, model

        from runpod_flash import CudaVersion, Endpoint, GpuGroup, PodTemplate

        # Mirrors each tier's actual deployed config exactly (verified via
        # mcp__runpod__list-endpoints 2026-07-03) so a self-heal re-deploy behaves identically
        # to what's there today, not a differently-configured replacement.
        gpu = GpuGroup.ADA_24 if tier == "low" else GpuGroup.ADA_48_PRO
        workers = (1, 3) if tier == "low" else (0, 3)
        disk_gb = 50 if tier == "low" else 90
        ep = Endpoint(
            name=endpoint_name, image=DND_DM_IMAGE, gpu=gpu,
            workers=workers, idle_timeout=300, template=PodTemplate(containerDiskInGb=disk_gb),
            min_cuda_version=CudaVersion.V13_0,
            env={"MODEL_NAME": model, "MAX_MODEL_LEN": "16384", "GPU_MEMORY_UTILIZATION": "0.90",
                 "ENABLE_AUTO_TOOL_CHOICE": "true", "TOOL_CALL_PARSER": "hermes"},
        )
        try:
            await asyncio.wait_for(ep.run({"input": {}}), timeout=10)
        except Exception:
            pass  # expected -- this throwaway call only exists to trigger first deploy

        client = RunpodGraphQLClient()
        try:
            for _ in range(10):
                resolved_id = await _resolve_dm_endpoint_by_name(client, endpoint_name)
                if resolved_id:
                    logger.info("dm_loop.ensure_dm_endpoint: resolved %s (%s) -> %s",
                               tier, endpoint_name, resolved_id)
                    _DM_ENDPOINT_STATE[tier] = resolved_id
                    return resolved_id, model
                await asyncio.sleep(2)
            raise RuntimeError(f"{endpoint_name!r} not found after deploy")
        finally:
            await client.close()


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
   number, HP total, or an NPC's reply. NEVER just restate the player's own action back to
   them in different words ("I examine the automaton" -> "You examine the automaton..." is
   NOT narration, it's an echo) — describe what's NEW: a detail they notice, a consequence,
   a change in the scene. If the tool result genuinely has nothing new to add, say so briefly
   rather than padding with a reworded restatement of what they just said.
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
6. If a tool result says a call was malformed, unknown, or failed, that is YOUR mistake to
   fix silently — call it again correctly (or a different, valid tool), never the player's
   business. NEVER say anything like "there was a mistake," "let's try that again," or
   otherwise acknowledge a tool/error to the player — stay fully in character and narrate as
   if nothing went wrong.
"""


# Pre-adventure "intake" mode (see handle_message): shown INSTEAD of SYSTEM_PROMPT while the
# session has no character. Deliberately contains ZERO scenery vocabulary — observed live: the
# 7B parroted SYSTEM_PROMPT's own exit-descriptor examples ("a warped iron door," "a spiral
# stair") into a fully hallucinated room for a character that didn't exist yet. An instruction
# not to invent scenery loses to in-context examples OF scenery; the fix is a prompt with
# nothing to parrot, paired with a tool list where start_adventure is the only option.
#
# TWO variants (e0b.10 addendum), picked per-turn by handle_message via _needs_theme_question:
# server.start_adventure only ever USES its `theme` argument the very first time a campaign_id
# is created (see its own `if not camp:` branch) — asking about a theme for a world that
# already exists is pure friction, and worse, actively misleads the model (and the player)
# into thinking the model just chose which world this is. Observed live on prod: a player on
# an existing shared world's page said "start a new character in this world," the intake had
# no idea what world that was, asked for/invented a theme, and the resulting confusion read as
# "wrong world." _state_line's per-turn [SERVER STATE] line (below) is what actually NAMES the
# target world every turn; these prompts just tell the model whether it's allowed to ask about
# one at all.
INTAKE_PROMPT_NEW_WORLD = """You are the host welcoming a new player to a tabletop RPG, in a \
browser chat. No game exists for this player yet — there are no rooms, no exits, no scenes, \
and describing any is an error. The player chose to found a BRAND NEW world just now, so your \
job includes picking its theme: learn a short theme (e.g. 'gothic horror', 'deep-space \
salvage') and a character name + class, offering to invent any of them if the player would \
rather you pick, then IMMEDIATELY call start_adventure with what you have. If the player \
leaves anything up to you, choose something evocative yourself and call the tool without \
asking again. Keep replies to 1-3 friendly sentences."""

INTAKE_PROMPT_EXISTING_WORLD = """You are the host welcoming a new player to a tabletop RPG, \
in a browser chat. No CHARACTER exists for this player yet — there are no rooms, no exits, no \
scenes, and describing any is an error — but the WORLD itself already exists and is named for \
you in a [SERVER STATE] message below (its theme, id, and premise). Do NOT ask the player for \
a theme, and do NOT invent a different one — that choice was already made when this world was \
founded, long before this conversation. Your only job: learn a character name + class, \
offering to invent either if the player would rather you pick (or if they've already told you \
to just make one, e.g. "make me a character" / "surprise me"), then IMMEDIATELY call \
start_adventure with what you have. If the player asks what world this is, answer from the \
[SERVER STATE] line, in your own words. Keep replies to 1-3 friendly sentences."""


def create_session(campaign_id: str = MAIN_CAMPAIGN_ID) -> "DMSession":
    """Factory for one browser session, for ONE world. campaign_id defaults to the shared
    "main" world, but callers (chat_sessions.get_or_create, e0b.10) always pass the PAGE's own
    campaign_id explicitly — the browser's world-selection choice card (web.py's PAGE script)
    is what actually decides which world a given DMSession is for."""
    return DMSession(campaign_id=campaign_id,
                     messages=[{"role": "system", "content": SYSTEM_PROMPT}])


# Injected only into a RESUMED session (see create_resumed_session) — a fresh session's
# messages never carry this, so the model only ever sees it the one time it's actually true.
_RESUME_NOTE = (
    "The player's browser already has a character from an earlier visit — the server "
    "process just restarted, so this is a NEW session with no memory of the conversation, "
    "but the character itself (name, class, HP, inventory, location) is real and unchanged "
    "in the world. Do NOT start a new adventure or ask for a theme/name again. Greet the "
    "player back in character and call character_sheet or look to reorient both of you, "
    "then continue play from wherever they actually are."
)


def create_resumed_session(player_id: str, campaign_id: str = MAIN_CAMPAIGN_ID) -> "DMSession":
    """Rebuild a DMSession for a browser player whose in-memory session (chat_sessions.
    _sessions) was lost to a process restart/redeploy, but whose session_id -> player_id
    mapping survived in state.py's web_session table (see chat_sessions.get_or_create).

    Message HISTORY is NOT recovered — it never persisted anywhere but the in-memory store
    (an accepted tradeoff, see chat_sessions.py's module docstring) — but the character
    itself is real, already in the World, so this just points a fresh session at it and lets
    the model re-orient via a normal tool call instead of asking the player to start over.
    exit_map is rebuilt immediately (not lazily on the next move/look) so a resumed player
    can act on their actual exits right away, same as a session that just called look."""
    session = DMSession(campaign_id=campaign_id, player_id=player_id,
                        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                                  {"role": "system", "content": _RESUME_NOTE}])
    _rebuild_exit_map(session)
    return session


@dataclass
class DMSession:
    """One browser player's session, for ONE world (e0b.10 — a browser can hold one of these
    per (session_id, campaign_id), see chat_sessions.py). `player_id` is minted server-side
    inside _tool_start_adventure and stored here — it is NEVER a field the model can set (no
    tool schema below accepts it), which is the whole prompt-injection boundary this module
    exists to enforce. `exit_map` is derived, per-current-room state (descriptor -> real
    direction), rebuilt every time a tool changes/reveals the player's room — it's what lets
    the model address exits by descriptor only (see _resolve_direction) while server.move()
    still gets a real compass direction underneath.

    `pending_new_world` (e0b.10): set True by web.py's POST /chat, ONLY while this session has
    no character yet, when the browser's "Create my world" choice-card button fired. The next
    start_adventure this turn calls swaps in campaign_id="new" instead of session.campaign_id
    (see _tool_start_adventure) — after it succeeds, session.campaign_id is updated to the
    REAL new campaign id and this flag is cleared, so it can only ever fire once per session.
    """
    campaign_id: str = MAIN_CAMPAIGN_ID
    player_id: str | None = None
    messages: list[dict] = field(default_factory=list)
    exit_map: dict[str, str] = field(default_factory=dict)
    pending_new_world: bool = False


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
        "description": ("Roll dice for any check not already covered by attack, e.g. "
                        "'1d20+3', '2d6'. Never invent a roll yourself. Rolling dice for an "
                        "ambush/trap/hazard does NOT apply real damage on its own — call "
                        "take_damage too, or the player's actual HP never changes."),
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "Dice expression, e.g. '1d20+2'."},
        }, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "take_damage",
        "description": ("Apply REAL damage to the player from anything other than attack()'s "
                        "own monster-retaliation step — an ambush, a trap, a fall, a surprise "
                        "strike before the player can act. Narrating damage without calling "
                        "this leaves the player's actual HP unchanged."),
        "parameters": {"type": "object", "properties": {
            "amount": {"type": "integer"},
            "source": {"type": "string", "description": "What caused it, e.g. \"the ambush\"."},
        }, "required": ["amount"]}}},
    {"type": "function", "function": {
        "name": "heal",
        "description": ("Restore REAL HP — resting, a potion, an NPC's aid. Narrating a "
                        "rest/heal without calling this leaves the player's actual HP "
                        "unchanged (same trap as take_damage, in reverse)."),
        "parameters": {"type": "object", "properties": {
            "amount": {"type": "integer"},
            "source": {"type": "string", "description": "What caused it, e.g. \"a night's rest\"."},
        }, "required": ["amount"]}}},
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
        "name": "give_item",
        "description": ("Hand something from the player's inventory to an NPC or another "
                        "player standing in the current room. Pass exactly one of npc_name "
                        "or to_player_name."),
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string"},
            "npc_name": {"type": "string"},
            "to_player_name": {"type": "string"},
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
        "name": "update_quest",
        "description": ("Update a quest already in progress — mark a step done "
                        "(complete_step, 0-indexed, see active_quests for indices), add a "
                        "newly-discovered objective (add_step), or resolve it "
                        "(state='done'|'failed'). quest_id comes from active_quests' own "
                        "[quest_id: ...] tag."),
        "parameters": {"type": "object", "properties": {
            "quest_id": {"type": "string"},
            "complete_step": {"type": "integer"},
            "add_step": {"type": "string"},
            "state": {"type": "string", "enum": ["active", "done", "failed"]},
        }, "required": ["quest_id"]}}},
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

# Intake mode's entire tool surface: start_adventure alone. Paired with the INTAKE_PROMPT_*
# variants (see their comment) — a session with no character can't call move/attack/look
# because those tools simply don't exist for it, which is a harder guarantee than any
# instruction.
INTAKE_TOOLS: list[dict] = [t for t in TOOLS if t["function"]["name"] == "start_adventure"]


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
    # "Create my world" (e0b.10): a session with pending_new_world set (web.py's POST /chat,
    # only ever set while this session had no character yet) passes campaign_id="new" instead
    # of its own campaign_id — server.start_adventure mints a brand-new world for that. The
    # REAL id it minted is resolved below via world.character(...).campaign_id once the call
    # returns, per the task's own preference over re-parsing the reply's world-id line: it's
    # one authoritative lookup instead of a second regex alongside the player_id one just
    # below, and it can't ever drift from server.py's actual contract the way text-scraping
    # could.
    requesting_new_world = session.pending_new_world
    campaign_id = "new" if requesting_new_world else session.campaign_id
    raw = await server.start_adventure(theme=theme, character_name=character_name,
                                       character_class=character_class,
                                       campaign_id=campaign_id)
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
    if requesting_new_world:
        ch = server.world.character(session.player_id)
        session.campaign_id = ch.campaign_id if ch else session.campaign_id
        session.pending_new_world = False
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
    return _sanitize(await server.attack(session.player_id))


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
    # player_id is injected here, never in the model-facing schema (same boundary as every
    # other wrapper in this module) — this is what makes a standalone roll (a trap, a skill
    # check) show up in the world stream/metrics/story at all; previously it was invisible
    # even though attack()'s own rolls (via combat.resolved) always were.
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.roll_dice(expression, player_id=session.player_id))


async def _tool_take_damage(session: DMSession, amount: int, source: str = "") -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.take_damage(session.player_id, amount, source=source))


async def _tool_heal(session: DMSession, amount: int, source: str = "") -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.heal(session.player_id, amount, source=source))


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


async def _tool_give_item(session: DMSession, item_name: str, npc_name: str | None = None,
                          to_player_name: str | None = None) -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.give_item(session.player_id, item_name=item_name,
                                      npc_name=npc_name, to_player_name=to_player_name))


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


async def _tool_update_quest(session: DMSession, quest_id: str, complete_step: int | None = None,
                             add_step: str | None = None, state: str | None = None) -> str:
    err = _require_started(session)
    if err:
        return err
    return _sanitize(server.update_quest(session.player_id, quest_id, complete_step=complete_step,
                                         add_step=add_step, state=state))


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
    "take_damage": _tool_take_damage,
    "heal": _tool_heal,
    "pick_up_item": _tool_pick_up_item,
    "drop_item": _tool_drop_item,
    "give_item": _tool_give_item,
    "talk_to": _tool_talk_to,
    "start_quest": _tool_start_quest,
    "active_quests": _tool_active_quests,
    "update_quest": _tool_update_quest,
    "log_event": _tool_log_event,
    "remember": _tool_remember,
}


_DND_DM_BASE_URL_OVERRIDE = os.environ.get("DND_DM_BASE_URL")  # empty/unset unless explicitly set


async def _resolve_endpoint() -> tuple[str, str]:
    """Prefer the Flash-resolved (endpoint_id, model) for the CURRENT tier (self-heals if that
    endpoint is ever recreated under the same name with a different id, and picks up a live
    dm_use_14b toggle flip immediately, no restart) — DND_DM_BASE_URL is both an explicit
    override escape hatch when actually SET (e.g. pointing at a different host for local
    testing; uses the low tier's model in that case) and the fallback if Flash resolution
    itself fails for any reason. A GraphQL hiccup must never take the whole DM down over what
    used to be a plain hardcoded URL."""
    if _DND_DM_BASE_URL_OVERRIDE:
        return _DND_DM_BASE_URL_OVERRIDE, DM_MODEL_TIERS["low"][1]
    try:
        endpoint_id, model = await ensure_dm_endpoint()
        return f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1", model
    except Exception:
        logger.exception("dm_loop._resolve_endpoint: Flash resolution failed, "
                        "falling back to hardcoded low-tier default")
        low_name, low_model = DM_MODEL_TIERS["low"]
        return f"https://api.runpod.ai/v2/{low_name}/openai/v1", low_model


# --- the OpenAI-compatible chat call ---------------------------------------------------------
def _chat_sync(base_url: str, model: str, messages: list[dict], tools: list[dict], *,
              temperature: float = TEMPERATURE) -> dict:
    """One POST to <base>/chat/completions — urllib in a thread executor, exactly
    flash_llm._chat_sync's proven shape (no new deps: no `openai`/`requests` package), which
    is what makes this loop work against ANY OpenAI-compatible host by just changing
    base_url/model. Returns the raw `message` dict (content + tool_calls) — the loop needs
    tool_calls verbatim to drive itself, not just the text half flash_llm.generate() returns.
    An empty `tools` list (bot_player.py's plain-text "what does the character do next" call,
    which never needs tool-calling) omits tools/tool_choice entirely rather than sending an
    empty array — some OpenAI-compatible servers reject tool_choice="auto" with no tools.

    `temperature` is overridable per-call (default TEMPERATURE, the DM's own resolution
    temperature) so bot_player.py's player-persona decision can run hotter than the DM without
    touching the DM's own tool-calling reliability — see PLAYER_TEMPERATURE there."""
    body = {"model": model, "messages": messages,
            "max_tokens": MAX_TOKENS, "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    body = json.dumps(body).encode()
    url = f"{base_url.rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_CHAT_TIMEOUT_S) as resp:  # cold start ~90s worst case
        data = json.load(resp)
    return data["choices"][0]["message"]


async def _chat(messages: list[dict], tools: list[dict], *, temperature: float = TEMPERATURE) -> dict:
    base_url, model = await _resolve_endpoint()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _chat_sync(base_url, model, messages, tools, temperature=temperature))


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


def _needs_theme_question(session: DMSession) -> bool:
    """True exactly when start_adventure's `theme` argument will actually be USED — i.e. this
    turn is going to CREATE a brand-new world, not join one that already exists.
    server.start_adventure only ever consumes/persists `theme` the first time a given
    campaign_id is founded (see its own `if not camp:` branch) — every other case, asking the
    player for one is pure friction and actively misleading (see the INTAKE_PROMPT_* comment
    for the live prod incident this fixes). Two ways a turn ends up creating a new world:
    the player explicitly chose to (session.pending_new_world, "Create my world"), or this
    session's own target campaign_id genuinely hasn't been founded yet (world.campaign(...) is
    None) — the one case where even MAIN itself might still need its founding theme, on a
    truly fresh install nobody has ever started an adventure on."""
    if session.pending_new_world:
        return True
    return server.world.campaign(session.campaign_id) is None


def _state_line(session: DMSession) -> str:
    """One authoritative sentence of server-known session state, refreshed every turn (see
    handle_message). The model must never have to infer whether an adventure exists, who the
    character is, or whether they're alive — the server knows all three for free. Also the
    ONLY place (besides the intake system prompt swap in handle_message) that tells the model
    WHICH WORLD it's actually in — critical while no character exists yet, since a session's
    campaign_id is otherwise invisible to the model (see _needs_theme_question's comment)."""
    if not session.player_id:
        if _needs_theme_question(session):
            return ("[SERVER STATE] No character exists for this session yet. This is a BRAND "
                    "NEW world being founded right now — there are NO rooms, exits, or scenes "
                    "yet, do NOT describe or invent any. Your ONLY valid opening: ask for a "
                    "theme + character name/class (or offer to invent them), then call "
                    "start_adventure. Every other tool will fail until then.")
        camp = server.world.campaign(session.campaign_id)
        theme = (camp.theme if camp else "") or "an unnamed world"
        premise = (camp.premise if camp else "") or ""
        premise_first = premise.split(". ")[0].strip().rstrip(".") if premise else ""
        world_label = (theme if session.campaign_id == MAIN_CAMPAIGN_ID
                      else f"{theme} ({session.campaign_id})")
        premise_note = f" Premise: {premise_first}." if premise_first else ""
        return (f"[SERVER STATE] No character exists for this session yet. This session is "
                f"joining an ALREADY-EXISTING world: {world_label}.{premise_note} That world's "
                f"theme is fixed — do NOT ask the player for a theme or invent a different "
                f"one; if asked what world this is, answer from this line. There are NO rooms, "
                f"exits, or scenes revealed to you yet — do not describe or invent any. Your "
                f"ONLY valid opening: ask for (or offer to invent) a character name and class, "
                f"then call start_adventure. Every other tool will fail until then.")
    ch = server.world.character(session.player_id)
    if not ch:
        return ("[SERVER STATE] This session's character no longer exists (world was reset). "
                "Treat as no character: offer to start_adventure a new one; invent nothing.")
    if ch.hp <= 0:
        return (f"[SERVER STATE] {ch.name} the {ch.klass} is DEAD (0 HP). Action tools will "
                f"refuse. Narrate the aftermath if asked, and offer start_adventure for a "
                f"new character.")
    room = server.world.room(ch.location_id)
    where = room.name if room else "an unknown place"
    camp = server.world.campaign(session.campaign_id)
    world_note = (f" in the world \"{camp.theme}\" ({session.campaign_id})"
                 if camp and session.campaign_id != MAIN_CAMPAIGN_ID else "")
    # A live monster's presence otherwise only ever enters the model's context ONCE, in
    # whatever tool result first described the room — and ages out of _truncate_history's
    # window after a few combat rounds (each round burns 2+ messages: the assistant's
    # tool_calls, plus the result). Confirmed live (2026-07-04): once that happened, the model
    # kept narrating swings/strikes in prose WITHOUT ever calling attack again — no dice, no
    # damage, no combat.resolved event, just flavor text pretending a fight was happening.
    # Re-asserting it here, fresh every turn like HP/room already are, means it can never
    # silently fall out of context mid-fight.
    monster_note = ""
    if room:
        live = [c for c in room.contents if c.get("type") == "monster" and c.get("hp", 0) > 0]
        if live:
            names = ", ".join(f"{m['name']} ({m['hp']}/{m.get('max_hp', m['hp'])} HP)" for m in live)
            monster_note = (f" A live monster is in this room right now: {names} — call attack "
                            f"if the player's action is aggressive toward it; never narrate a "
                            f"hit, miss, or its death without that tool's result.")
    # Same "re-assert every turn, never rely on it surviving truncation" fix as monster_note
    # above, for the exact same reason — confirmed live (2026-07-04): a character sat in the
    # SAME room for a dozen+ turns of increasingly elaborate narration (a fight, a pedestal, a
    # chest, a journal) because "what exits exist here" had scrolled out of the truncated
    # history and the model had nothing concrete left to call move against, so it just kept
    # improvising more content for the room it was already in instead.
    exit_note = ""
    if room and room.exits:
        descs = server.world.room_exit_descriptions(room.id)
        labels = [descs.get(d, d) for d in room.exits]
        exit_note = f" Exits from here: {', '.join(labels)}."
    return (f"[SERVER STATE] Playing {ch.name} the {ch.klass}, HP {ch.hp}/{ch.max_hp}, "
            f"currently in {where}{world_note}.{monster_note}{exit_note} Narrate ONLY from "
            f"tool results, never from memory of rooms not returned by a tool this session.")


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

    Session-death guard (playtest-reported "worst finding"): session.player_id being SET is
    not the same thing as the character still existing. It goes stale when the underlying
    character/world disappears out from under a live session (a world reset via
    scripts/reset_world.sh or delete_world.sh, or -- same shape -- a redeploy landing between
    this session minting player_id and its next turn). Before this guard, that case fell
    through into full DM mode: session.player_id truthy meant `in_intake` was False, so every
    tool (look/move/etc, see server.py) just returned "Unknown player_id. Call start_adventure
    first." as an ordinary tool RESULT for the model to narrate around -- confirmed live as a
    silent, confusing failure (chat kept accepting input, the model improvised something
    vague, and separately /state's tick() quietly relabeled the header back to "Spectating"
    with no explanation tying the two together). Checked here, first, before touching the
    model at all -- cheaper (no wasted LLM/Flash call on a turn that can't possibly resolve)
    and gives the browser a single, unambiguous signal (`type: "session_expired"`) instead of
    letting the model's improvisation stand in for a real error.
    """
    if session.player_id and server.world.character(session.player_id) is None:
        yield {"type": "session_expired",
               "text": "This character's thread was lost on the server (the world may have "
                       "been reset). Your progress up to now is gone from this browser tab -- "
                       'hit "Start again" below to begin a new character.'}
        return

    # SERVER-KNOWN STATE, injected fresh every turn — never model-inferred. Observed live
    # without this: a fresh session asked "I want to go to the next room" got a fully
    # HALLUCINATED room ("a sturdy wooden door to the north...") for a character that did
    # not exist — the model had no in-context signal for "has an adventure started?", so it
    # guessed, and guessed wrong. The server always knows; tell it. Old state lines are
    # removed first so exactly one (current) line exists regardless of turn count.
    session.messages = [m for m in session.messages
                        if not (m.get("role") == "system"
                                and str(m.get("content", "")).startswith("[SERVER STATE]"))]
    session.messages.append({"role": "system", "content": _state_line(session)})
    session.messages.append({"role": "user", "content": user_text})
    tool_call_count = 0
    malformed_count = 0
    no_tool_retry_used = False

    while True:
        session.messages = _truncate_history(session.messages)
        # Intake mode while no character exists: swap in the scenery-free INTAKE_PROMPT_*
        # (session.messages[0] is always the stored SYSTEM_PROMPT — swapped at CALL time
        # only, the stored history keeps its real prompt) and offer start_adventure as the
        # ONLY tool. Which variant depends on whether THIS turn is founding a brand-new world
        # or joining one that already exists (_needs_theme_question, e0b.10) — only the
        # former should ever ask the player for a theme. The moment start_adventure lands
        # mid-turn, session.player_id is set and the very next loop iteration proceeds in full
        # DM mode with the full tool list.
        in_intake = not session.player_id
        if in_intake:
            intake_prompt = (INTAKE_PROMPT_NEW_WORLD if _needs_theme_question(session)
                             else INTAKE_PROMPT_EXISTING_WORLD)
            call_messages = [{"role": "system", "content": intake_prompt}] + session.messages[1:]
        else:
            call_messages = session.messages
        tools = (INTAKE_TOOLS if in_intake else TOOLS)
        try:
            message = await _chat(call_messages, tools)
        except Exception:
            logger.exception("dm_loop.handle_message: chat completion failed")
            yield {"type": "text",
                  "text": "The DM pauses, momentarily lost in thought... (connection trouble — try again?)"}
            return

        tool_calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()

        # A zero-tool-call response on the FIRST attempt of a turn means the model just
        # narrated in prose without resolving anything against real game state — confirmed
        # live (2026-07-04): a character sat in one room for a dozen+ turns of increasingly
        # elaborate hallucinated content (a fight, a chest, a journal) because the model kept
        # choosing to just narrate instead of calling move/attack/etc, and nothing ever
        # corrected it mid-session. One retry with an explicit nudge, not a silent accept — if
        # the SECOND attempt is also tool-free, it really was just conversation/a question with
        # nothing to resolve, so accept it rather than looping forever. Nudge is transient
        # (only sent on this one retry call, never persisted to session.messages) so it can't
        # pollute history the model reads on later turns.
        if not tool_calls and not in_intake and tool_call_count == 0 and not no_tool_retry_used:
            no_tool_retry_used = True
            logger.info("dm_loop.handle_message: zero tool calls on first attempt "
                       "(player_id=%s, action=%r) -- retrying with nudge",
                       session.player_id, user_text[:100])
            nudge = {"role": "system", "content": (
                "[SERVER STATE] You just replied with no tool call at all. If the player's "
                "message described an action — moving, attacking, searching, picking "
                "something up, talking to someone, resting — call the matching tool now "
                "instead of narrating in prose. If it was genuinely just a question or chat "
                "with no action to resolve, you may reply in plain text.")}
            try:
                message = await _chat(call_messages + [nudge], tools)
            except Exception:
                logger.exception("dm_loop.handle_message: no-tool-call retry failed "
                                "(player_id=%s)", session.player_id)
            else:
                tool_calls = message.get("tool_calls") or []
                content = (message.get("content") or "").strip()
                if tool_calls:
                    logger.info("dm_loop.handle_message: retry recovered %d tool call(s) "
                               "(player_id=%s)", len(tool_calls), session.player_id)
                else:
                    logger.warning("dm_loop.handle_message: still zero tool calls after nudge "
                                  "-- accepting as a real no-action turn (player_id=%s)",
                                  session.player_id)

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

            # Captured BEFORE the handler runs — the only way to notice a world switch below
            # is to compare against what session.campaign_id was walking in, since
            # _tool_start_adventure (e0b.10) mutates it in place on success.
            campaign_before_call = session.campaign_id
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
            # New-world flow (e0b.10): this turn's start_adventure just landed in a DIFFERENT
            # campaign than it started the turn in — i.e. "Create my world" actually minted
            # one. Surface it as its own event, BEFORE the turn's final narration text, so the
            # page's own JS can show a "your world is ready" line and redirect once the turn
            # finishes (see web.py's PAGE script and POST /chat's finally-bookkeeping, which
            # persists the durable web_session_world row under this NEW campaign_id).
            if name == "start_adventure" and session.campaign_id != campaign_before_call:
                yield {"type": "world", "campaign_id": session.campaign_id}

            if malformed_count >= 2:
                stop_after_tools = True
                break

        if stop_after_tools:
            yield {"type": "text",
                  "text": content or "The DM pauses, considering... What do you do?"}
            return
