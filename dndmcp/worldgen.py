"""World-builder — the PRIMARY Flash use. Generates the world ahead of the player.

Traversal-driven: when the player heads toward unexplored space, a Flash model cheaply
generates the next room's rich content (vivid description, specific features, what dwells
there) and we store it in the graph. By the time the agent enters and queries the room,
it's ready. Burst to generate, scale to zero when exploration pauses.

Stub→real: with Flash off (default), falls back to the procedural generator (game.py) so the
world still builds — just template-rich instead of model-rich. Flip DND_FLASH_LLM=1 to use
Flash (see flash_llm.py — that's the real switch; ignore any other FLASH_* flag you see
referenced elsewhere, they gate unrelated/unused code paths).
"""

from __future__ import annotations

import json
import logging
import random
import uuid

from . import compendium, flash_llm, game, setting

logger = logging.getLogger(__name__)

# Theme → real SRD creatures that fit (so spawns are rules-accurate AND on-theme).
_THEME_CREATURES = {
    "gothic horror": ["Ghoul", "Skeleton", "Zombie", "Ghost", "Specter", "Shadow",
                      "Wight", "Swarm of Rats", "Cultist", "Giant Rat"],
    "default": ["Goblin", "Kobold", "Giant Spider", "Skeleton", "Giant Rat",
                "Bandit", "Wolf", "Stirge"],
}


def _creatures_for(theme: str) -> list[str]:
    for key in _THEME_CREATURES:
        if key in (theme or "").lower():
            return _THEME_CREATURES[key]
    return _THEME_CREATURES["default"]


# SKILL: describe_room — FACTS only, no pre-written narration. The Flash world-builder's job
# is to invent what's true about the room; the DM AGENT (running the actual session) does the
# narrating, in whatever voice fits the moment — not a canned ahead/left/right/center template.
_ROOM_JSON = ('{"name": short evocative room name, "kind": one or two words (e.g. "cellar", '
              '"great hall", "attic" — informs how it connects to the world), '
              '"atmosphere": 2-4 sentences of vivid, SPECIFIC sensory detail (sight, smell, '
              'sound, light, texture) — substantial enough to actually paint the room, not a '
              'single bare fact. Still facts for a Dungeon Master to narrate FROM, not finished '
              'scene-prose — but ground every detail in THIS world\'s actual theme/premise, '
              'never generic dungeon-crawl dressing (no "collapsed pillar" filler unless this '
              'world genuinely is that kind of place), '
              '"features": array of exactly 2 specific, examinable details — fixed, NOT '
              'portable (architecture, furniture, scenery), each tied to this world\'s actual '
              'theme/premise (not interchangeable with any other setting), '
              '"has_monster": true or false, '
              '"notable_items": array of 1-2 SMALL, PORTABLE objects a player could actually '
              'pick up and carry — a scroll, a trinket, a tool, a coin pouch, a key, a '
              'weapon — distinct from features (which are fixed/scenery, never portable). '
              'Most rooms have at least one loose object worth finding; only use an empty '
              'array on the rare room that is genuinely bare of anything portable, '
              '"exits": {"<direction>": short physical description (4-8 words) of THAT '
              'exit\'s threshold as seen from THIS room — a door/archway/stairwell/gap, '
              'material + condition, NOT what lies beyond it (unknown/unexplored) — one '
              'entry per direction listed below, keys must match exactly}, '
              '"branch": null, OR {"direction": one compass word NOT already listed above '
              '(north/south/east/west/up/down), "description": short physical description '
              'of that new exit\'s threshold} if — and ONLY if — this room\'s nature '
              'genuinely calls for an extra way out (a crossroads, a fork, a collapsed wall '
              'opening a second path, a room that feels like a hub). Most rooms should NOT '
              'branch — leave this null unless it is a real, deliberate exception.}')


def _room_messages(theme: str, came_from: str | None, exits: list[str],
                   nearby: list[tuple[str, str]] | None = None,
                   recent_events: list[str] | None = None) -> list[dict]:
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You are the world-builder for a {theme} dungeon crawl in this setting. You invent "
              f"what is TRUE about each room — facts for a Dungeon Master to narrate from, not "
              f"finished prose. You reply with STRICT JSON only — no text outside the JSON object.")
    enter = f" the player enters from the {came_from}" if came_from else " the player descends into"
    context = ""
    if nearby:
        listed = ", ".join(f"{name} ({kind})" if kind else name for name, kind in nearby)
        context = (f" Nearby, already-explored areas: {listed}. Keep this room's tone/architecture "
                   f"consistent with them — same building, not a random mismatch of styles.")
    if recent_events:
        # Stigmergy reaching into generation itself, not just narration: what happened next
        # door can ripple into what THIS room is — the same fight/discovery a moment ago is
        # what makes a freshly-generated room feel like a continuation, not a blank slate.
        context += f" Recently, nearby: {'; '.join(recent_events)}."
    user = (f"Generate the next room{enter}. Exits lead: {', '.join(exits) or 'none'}.{context} "
            f"Return JSON: {_ROOM_JSON}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def generate_room_content(room_id: str, theme: str, *, entry_from: str | None = None,
                                nearby: list[tuple[str, str]] | None = None,
                                recent_events: list[str] | None = None,
                                salt: str = "") -> dict:
    """Generate a room's content for the graph. Tries Flash (structured JSON); falls back
    to procedural. Returns game.generate_room's shape + `features` + a `via` marker.

    `nearby`: (name, kind) pairs of already-generated rooms within a couple hops, so the LLM
    keeps tone/architecture consistent with the surrounding region instead of each room being
    generated in isolation. `recent_events`: recent log text for the room being generated
    FROM (see server.py's _generate_and_link) — lets a fight/discovery next door ripple into
    what this new room actually is, not just how it's narrated. `salt`: the owning campaign's
    salt (state.py Campaign.salt) — see game._seeded for why this must be passed through, not
    just room_id alone."""
    rng = game._seeded(room_id, salt)  # deterministic per (room_id, salt)
    base = game.generate_room(room_id, theme, entry_from=entry_from, salt=salt)  # procedural skeleton

    via = "procedural"
    want_monster = any(c.get("type") == "monster" for c in base["contents"])
    # `items` starts as whatever the procedural dice roll picked (0 or 1 loot dict from the
    # thin, theme-mismatched game.py pool) — the FALLBACK. If Flash succeeds it REPLACES this
    # wholesale (same override pattern as want_monster below), never just adds on top of it —
    # otherwise a generic "pouch of gold" keeps surviving next to genuinely on-theme content.
    items = [c for c in base["contents"] if c.get("type") == "loot"]
    messages = _room_messages(theme, entry_from, list(base["exits"].keys()), nearby, recent_events)
    gen = await flash_llm.generate(messages, max_tokens=280, temperature=0.95)
    if gen:
        try:
            data = json.loads(gen[gen.find("{"): gen.rfind("}") + 1])
            if data.get("name"):
                base["name"] = data["name"]
            if data.get("kind"):
                base["kind"] = data["kind"]
            # `description` stays a raw FACT, not finished prose — the DM agent (whoever is
            # running the session) narrates from this, same as it would from a human DM's notes.
            if data.get("atmosphere"):
                base["description"] = data["atmosphere"]
            feats_from_flash = data.get("features")
            if isinstance(feats_from_flash, list):
                for f in feats_from_flash:
                    if isinstance(f, str) and f.strip():
                        base.setdefault("features", []).append(f.strip())
            elif data.get("feature"):  # back-compat: older prompt/response shape (singular)
                base.setdefault("features", []).append(data["feature"])
            def _normalize_item(raw):
                # the model doesn't reliably return a plain string here despite the schema —
                # sometimes a dict like {"item_name": ..., "description": ...} with varying
                # key names. Normalize to a single display string either way.
                if isinstance(raw, dict):
                    raw = (raw.get("description") or raw.get("name") or raw.get("item_name")
                          or raw.get("type") or raw.get("item_type") or next(iter(raw.values()), ""))
                return str(raw).strip() if raw else ""

            items_from_flash = data.get("notable_items")
            if isinstance(items_from_flash, list):
                # full override, same as want_monster below — an empty list here is a real
                # answer ("nothing of note in this room"), not "keep the procedural pick".
                items = [{"type": "loot", "id": uuid.uuid4().hex[:8], "name": name}
                        for raw in items_from_flash if (name := _normalize_item(raw))]
            elif data.get("notable_item"):  # back-compat: older prompt/response shape (singular)
                if name := _normalize_item(data["notable_item"]):
                    items = [{"type": "loot", "id": uuid.uuid4().hex[:8], "name": name}]
            # per-exit threshold descriptors — only override the procedural default for
            # directions the model actually addressed AND that are real exits of this room;
            # never trust an exit key the model invented on its own.
            exit_text = data.get("exits")
            if isinstance(exit_text, dict):
                for direction, desc in exit_text.items():
                    if direction in base["exits"] and isinstance(desc, str) and desc.strip():
                        base.setdefault("exit_descriptions", {})[direction] = desc.strip()
            # occasional extra branch — this is the ONE place exit COUNT can exceed the
            # procedural skeleton's deterministic 1-3, and only when the model deliberately
            # asks for it (a crossroads, a fork, a collapsed wall). Validated hard: must be a
            # real direction, must not already be an exit, model-invented directions ignored.
            branch = data.get("branch")
            if isinstance(branch, dict) and branch.get("direction") in game.DIRECTIONS \
                    and branch["direction"] not in base["exits"]:
                direction = branch["direction"]
                base["exits"][direction] = f"{room_id}:{direction}"
                desc = branch.get("description")
                if isinstance(desc, str) and desc.strip():
                    base.setdefault("exit_descriptions", {})[direction] = desc.strip()
            want_monster = bool(data.get("has_monster", want_monster))
            via = "flash"
        except Exception:
            logger.exception("generate_room_content: malformed Flash JSON, keeping procedural: %r", gen)

    # The generic procedural pool (game.py._THEMES) only really knows "gothic horror" and a
    # bland "default" bucket — for any custom/agent-authored theme (the common case once a
    # world's premise is agent-written, e.g. "drowned-tide folk horror"), this pool is
    # thematically mismatched filler, not texture. Top up ONLY if Flash didn't already give
    # real on-theme content (failed entirely, or returned fewer than 2 features) — never pile
    # generic dressing on top of good Flash output just because it's "the liveness layer."
    t = game._theme(theme)
    feats = base.setdefault("features", [])
    if len(feats) < 2:
        for f in rng.sample(t["features"], k=min(2 - len(feats), len(t["features"]))):
            if f not in feats:
                feats.append(f)

    # place a REAL SRD monster (rules-accurate) if wanted, and the final decided item list —
    # both strip whatever the procedural roll originally put in base["contents"] and replace
    # it wholesale, so a Flash-decided "no monster"/"no items" actually means none, not
    # "procedural filler plus whatever Flash added."
    base["contents"] = [c for c in base["contents"] if c.get("type") not in ("monster", "loot")]
    base["contents"].extend(items)
    if want_monster:
        mon = compendium.encounter_from_names(_creatures_for(theme), rng)
        if mon:
            base["contents"].append(mon)
        elif (rm := compendium.random_encounter(1.0, rng)):
            base["contents"].append(rm)
    base["via"] = via
    return base


_ITEM_JSON = ('{"name": short display name, "description": one factual sentence about it '
              '(material, condition, what it\'s for) — not flowery prose, '
              '"portable": true or false — could a person plausibly carry this away?, '
              '"reason": if not portable, one short in-world reason (e.g. "bolted to the floor"), else null}')


def _item_messages(description: str, theme: str, room_context: str) -> list[dict]:
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You are adjudicating a player's attempt to pick up an object in a {theme} dungeon "
              f"crawl in this setting. Decide what's TRUE about the object and whether it's actually "
              f"portable — most furniture/fixtures/scenery are NOT, most small objects ARE. "
              f"You reply with STRICT JSON only — no text outside the JSON object.")
    user = (f"The player tries to pick up: {description!r}. Room context: {room_context}\n"
            f"Return JSON: {_ITEM_JSON}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def generate_item_content(description: str, theme: str, room_context: str = "") -> dict:
    """Adjudicate + flesh out a player-described pickup that isn't pre-seeded loot. Tries Flash
    (structured JSON, decides portability); falls back to procedural (always portable — without
    a model to judge plausibility, permissive keeps the game playable with Flash off).
    Returns {"name", "description", "portable", "reason"}."""
    base = {"id": uuid.uuid4().hex[:8], "name": description.strip().capitalize(),
           "description": "", "portable": True, "reason": None}
    messages = _item_messages(description, theme, room_context)
    gen = await flash_llm.generate(messages, max_tokens=120, temperature=0.8)
    if gen:
        try:
            data = json.loads(gen[gen.find("{"): gen.rfind("}") + 1])
            if data.get("name"):
                base["name"] = data["name"]
            if data.get("description"):
                base["description"] = data["description"]
            if "portable" in data:
                base["portable"] = bool(data["portable"])
            if data.get("reason"):
                base["reason"] = data["reason"]
        except Exception:
            logger.exception("generate_item_content: malformed Flash JSON, keeping procedural: %r", gen)
    return base


_NPC_PERSONA_JSON = ('{"name": an individual proper name or title fitting this creature and '
                     'setting (e.g. "Skarn the Ratcatcher", "Old Ember") — never just the '
                     'species name, "persona": one or two TRUE sentences of personality/'
                     'background — temperament, a personal detail, what they want right now, '
                     '"goal": a short concrete want driving their behavior this scene, '
                     '"disposition": one of "hostile", "neutral", "ally" — how they regard '
                     'strangers on sight}')


def _npc_persona_messages(mon: dict, theme: str, room_name: str, room_kind: str,
                          atmosphere: str, nearby: list[tuple[str, str]] | None = None,
                          recent_events: list[str] | None = None) -> list[dict]:
    traits = ", ".join(mon.get("traits", [])) or "no notable traits"
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You invent the TRUE individual identity of one {theme} dungeon inhabitant for "
              f"a Dungeon Master to roleplay from — facts, not a finished performance. "
              f"You reply with STRICT JSON only — no text outside the JSON object.")
    context = (f"A {mon['name']} (traits: {traits}) is found in {room_name} ({room_kind}): "
              f"{atmosphere}")
    if nearby:
        listed = ", ".join(f"{name} ({kind})" if kind else name for name, kind in nearby)
        context += f" Nearby, already-explored areas: {listed}."
    if recent_events:
        context += f" Recently: {'; '.join(recent_events)}."
    user = f"{context}\nReturn JSON: {_NPC_PERSONA_JSON}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def generate_npc_persona(mon: dict, theme: str, room_name: str, room_kind: str,
                               atmosphere: str, nearby: list[tuple[str, str]] | None = None,
                               recent_events: list[str] | None = None) -> dict:
    """LLM-generate an individual identity (name/persona/goal/disposition) for one spawned
    SRD creature — the compendium gives mechanics (hp/ac/traits), this gives who they ARE.
    Tries Flash; falls back to a bare generic identity (species as name, neutral) if
    disabled/error — the entity row still gets created, just without flavor."""
    messages = _npc_persona_messages(mon, theme, room_name, room_kind, atmosphere,
                                     nearby, recent_events)
    gen = await flash_llm.generate(messages, max_tokens=200, temperature=0.9)
    if gen:
        try:
            data = json.loads(gen[gen.find("{"): gen.rfind("}") + 1])
            if data.get("name"):
                # the model doesn't reliably match the schema's exact casing (e.g. "Neutral")
                # despite the JSON schema spelling out lowercase — normalize + validate against
                # the enum so downstream code can safely do `disposition == "hostile"` etc.
                disposition = str(data.get("disposition") or "neutral").strip().lower()
                if disposition not in ("hostile", "neutral", "ally"):
                    disposition = "neutral"
                return {"name": data["name"], "persona": data.get("persona", ""),
                       "goal": data.get("goal", ""), "disposition": disposition, "via": "flash"}
        except Exception:
            logger.exception("generate_npc_persona: malformed Flash JSON, using procedural default: %r", gen)
    return {"name": mon["name"], "persona": "", "goal": "", "disposition": "neutral",
           "via": "procedural"}


def _npc_messages(npc: dict, theme: str, room_context: str, message: str) -> list[dict]:
    traits = ", ".join(npc.get("traits", [])) or "no notable traits"
    persona_line = f" {npc['persona']}" if npc.get("persona") else ""
    goal_line = f" Right now they want: {npc['goal']}." if npc.get("goal") else ""
    disposition_line = (f" They are {npc['disposition']} toward strangers."
                        if npc.get("disposition") else "")
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You are voicing {npc['name']} in a {theme} dungeon crawl in this setting. "
              f"Traits: {traits}.{persona_line}{goal_line}{disposition_line} Room: {room_context} "
              f"Stay fully in character. Reply with ONLY the character's spoken words — "
              f"no stage directions, no narration, no quotation marks. Keep it to 1-3 sentences.")
    messages = [{"role": "system", "content": system}]
    # prior turns are the conversation's memory — talk_to() fetches these from the entity
    # table (state.py Entity.memory) and passes them in via npc["conversation"] so this
    # function itself stays a pure prompt-builder with no DB access of its own.
    for turn in npc.get("conversation", []):
        role = "assistant" if turn["role"] == "npc" else "user"
        messages.append({"role": role, "content": turn["content"]})
    messages.append({"role": "user", "content": message})
    return messages


async def generate_npc_response(npc: dict, theme: str, room_context: str, message: str) -> dict:
    """Generate one line of in-character NPC dialogue, informed by the conversation history
    already stored on `npc` (via talk_to). Tries Flash; falls back to a generic in-character
    line with no real continuity — without a model, there's nothing to generate FROM."""
    messages = _npc_messages(npc, theme, room_context, message)
    gen = await flash_llm.generate(messages, max_tokens=120, temperature=0.9)
    if gen:
        return {"text": gen.strip().strip('"'), "via": "flash"}
    return {"text": f"{npc['name']} regards you, giving nothing away.", "via": "procedural"}
