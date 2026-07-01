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

import asyncio
import json
import logging
import random
import time
import uuid

from . import compendium, flash_llm, game, setting

logger = logging.getLogger(__name__)

# A level-1 solo player must never be handed a monster built for a full high-level party.
# _RANDOM_ENCOUNTER_MAX_CR bounds the procedural "no specific monster requested" pool.
# _MAX_MATCHED_MONSTER_CR bounds what a fuzzy name match (get_monster, which does NOT itself
# filter by CR) is allowed to hand back before we reject it and fall back to random_encounter —
# set a bit above the random pool's cap so an intentionally "small but scary-named" match (e.g.
# a low-CR "Specter") isn't rejected, while a genuinely high-CR fuzzy hit (e.g. "Dragon"
# matching an ancient dragon) is.
_RANDOM_ENCOUNTER_MAX_CR = 1.0
_MAX_MATCHED_MONSTER_CR = 2.0

# No hardcoded theme→creature table on purpose: a fixed Python mapping can only ever cover
# the themes someone thought to enumerate, and a "sci-fi frontier" bandit swinging a Scimitar
# (a real bug this replaced — see generate_room_content's monster_type handling below) just
# means the list didn't happen to cover that theme. The model already gets the full theme +
# premise in-context, so it invents the creature identity itself — anything from a classic SRD
# name to something wholly new — and the code's only job is finding real mechanics to back it,
# never deciding WHAT fits on its own behalf.


# SKILL: describe_room — FACTS only, no pre-written narration. The Flash world-builder's job
# is to invent what's true about the room; the DM AGENT (running the actual session) does the
# narrating, in whatever voice fits the moment — not a canned ahead/left/right/center template.
_ROOM_JSON = ('{"name": short evocative room name — grounded in THIS world\'s own distinctive '
              'vocabulary (a specific noun/image from its premise), not a generic dungeon word '
              '(echo, whisper, ancient, shadow, forgotten) unless the premise itself is built '
              'from that word; must not repeat a name already used in this world (listed '
              'below, if any), "kind": one or two words (e.g. "cellar", '
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
              '"has_monster": true or false — most dungeons have SOME dangerous inhabitants '
              'scattered through them; err toward true unless this specific room is clearly '
              'safe (a study, a shrine, an empty cell), '
              '"monster_type": if has_monster, a SHORT (2-4 word) species/creature name that '
              'genuinely belongs to THIS world\'s theme/premise — a wholly new species is '
              'great (not required to be a classic D&D monster), but it must read like a '
              'species/kind, e.g. "tide wraith", "rust-locked sentinel" — NOT a proper name, '
              'NOT a title, NOT a descriptive phrase with a dash or colon in it. Or null if '
              'has_monster is false. This is flavor only; the game finds real stats separately, '
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
                   recent_events: list[str] | None = None, premise: str = "",
                   existing_names: list[str] | None = None,
                   entry_room: tuple[str, str] | None = None) -> list[dict]:
    # A bare theme label ("sundered weave") means nothing to a small model on its own — no
    # training-data association for a made-up phrase, so it falls back to generic dungeon-
    # crawl tropes (observed live: "sundered weave" alone produced "The Whispering Crypt,"
    # ancient runes, a wooden door — none of it grounded in the actual premise). The
    # campaign's premise text is what actually explains what the theme MEANS.
    premise_line = f" The world's premise: {premise}" if premise else ""
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You are the world-builder for a {theme} dungeon crawl in this setting.{premise_line} "
              f"You invent what is TRUE about each room — facts for a Dungeon Master to narrate "
              f"from, not finished prose — and every room must clearly belong to THIS premise, "
              f"not generic dungeon-crawl dressing that could fit any setting. You reply with "
              f"STRICT JSON only — no markdown code fences, no text outside the JSON object.")
    enter = f" the player enters from the {came_from}" if came_from else " the player descends into"
    context = ""
    if entry_room:
        # The single strongest tonal anchor is the room whose doorway the player is stepping
        # through — _nearby_region deliberately excludes it (it BFSes OUTWARD from it), so
        # without this line the model knew rooms two hops away but not the immediate origin.
        e_name, e_kind = entry_room
        context = f" They are leaving {e_name}{f' ({e_kind})' if e_kind else ''} — this room adjoins it directly."
    if nearby:
        listed = ", ".join(f"{name} ({kind})" if kind else name for name, kind in nearby)
        context += (f" Nearby, already-explored areas: {listed}. Keep this room's tone/architecture "
                    f"consistent with them — same building, not a random mismatch of styles.")
    if recent_events:
        # Stigmergy reaching into generation itself, not just narration: what happened next
        # door can ripple into what THIS room is — the same fight/discovery a moment ago is
        # what makes a freshly-generated room feel like a continuation, not a blank slate.
        context += f" Recently, nearby: {'; '.join(recent_events)}."
    if existing_names:
        # Without this, two unrelated rooms in the same world can both land on "Rune Chamber"
        # (observed live) — the model has no way to know a name is already taken since each
        # room is generated as its own independent call.
        context += f" Room names already used in this world (do not reuse): {', '.join(existing_names)}."
    user = (f"Generate the next room{enter}. Exits lead: {', '.join(exits) or 'none'}.{context} "
            f"Return JSON: {_ROOM_JSON}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def generate_room_content(room_id: str, theme: str, *, entry_from: str | None = None,
                                nearby: list[tuple[str, str]] | None = None,
                                recent_events: list[str] | None = None,
                                salt: str = "", premise: str = "",
                                existing_names: list[str] | None = None,
                                deadline_s: float | None = None,
                                entry_room: tuple[str, str] | None = None) -> dict:
    """Generate a room's content for the graph. Tries Flash (structured JSON); falls back
    to procedural. Returns game.generate_room's shape + `features` + a `via` marker.

    `nearby`: (name, kind) pairs of already-generated rooms within a couple hops, so the LLM
    keeps tone/architecture consistent with the surrounding region instead of each room being
    generated in isolation. `recent_events`: recent log text for the room being generated
    FROM (see server.py's _generate_and_link) — lets a fight/discovery next door ripple into
    what this new room actually is, not just how it's narrated. `salt`: the owning campaign's
    salt (state.py Campaign.salt) — see game._seeded for why this must be passed through, not
    just room_id alone. `premise`: the campaign's own premise text (Campaign.premise) — a
    theme LABEL alone isn't enough grounding for a small model on a made-up/unusual theme;
    the premise is what actually explains what it means. `existing_names`: every room name
    already used in this world (state.py's room_ids_in) — without this two independent
    generation calls can both land on the same name (observed live: two different rooms both
    named "Rune Chamber"). `deadline_s`: total wall-clock budget across ALL retry attempts —
    None (default) keeps the existing patient behavior (up to `room_attempts` full 150s-
    timeout calls; used by the background prefetch path, which never blocks a player). Pass
    a real budget (e.g. ~20-30s) from a REACTIVE caller (a player is synchronously waiting,
    e.g. move()) so a hung/cold Flash endpoint can't stall them for up to 4x150s (~10min) —
    realistically caps it at 1-2 attempts, then falls back to procedural, same as running out
    of attempts."""
    rng = game._seeded(room_id, salt)  # deterministic per (room_id, salt)
    base = game.generate_room(room_id, theme, entry_from=entry_from, salt=salt)  # procedural skeleton

    via = "procedural"
    want_monster = any(c.get("type") == "monster" for c in base["contents"])
    monster_type = ""  # set from Flash's own invented creature identity, if it gives one
    # `items` starts as whatever the procedural dice roll picked (0 or 1 loot dict from the
    # thin, theme-mismatched game.py pool) — the FALLBACK. If Flash succeeds it REPLACES this
    # wholesale (same override pattern as want_monster below), never just adds on top of it —
    # otherwise a generic "pouch of gold" keeps surviving next to genuinely on-theme content.
    items = [c for c in base["contents"] if c.get("type") == "loot"]
    # entry_room: (name, kind) of the room the player is walking FROM — the immediate tonal
    # anchor _nearby_region can't provide (it excludes its own origin). See _room_messages.
    messages = _room_messages(theme, entry_from, list(base["exits"].keys()), nearby, recent_events,
                              premise, existing_names, entry_room=entry_room)

    # A single bad sample (malformed JSON) or a transient endpoint hiccup (cold start,
    # throttling — both observed in practice) shouldn't cost the room real content when a
    # retry would likely just work. Up to 4 attempts before accepting the procedural
    # fallback (bumped from 3 — a visible quality cliff was observed live: a bare "You stand
    # in a collapsed hall." fallback room wedged directly between two richly-described Flash
    # rooms after 3 straight malformed-JSON samples); each attempt re-samples fresh (same
    # prompt, model's own temperature=0.95 variance), so a retry is a genuinely different
    # roll, not a repeat of the same failure.
    data = None
    room_attempts = 4
    start = time.monotonic()
    for attempt in range(room_attempts):
        if deadline_s is not None:
            remaining = deadline_s - (time.monotonic() - start)
            if remaining <= 0:
                logger.warning("generate_room_content: deadline (%.1fs) exhausted before "
                               "attempt %d/%d, falling back to procedural",
                               deadline_s, attempt + 1, room_attempts)
                break
            try:
                gen = await asyncio.wait_for(
                    flash_llm.generate(messages, max_tokens=280, temperature=0.95),
                    timeout=remaining)
            except asyncio.TimeoutError:
                # The underlying urllib call keeps running in its executor thread (it isn't
                # actually cancellable), but that's fine — this coroutine returns to the
                # reactive caller now instead of blocking on it. That's the whole point of a
                # deadline: never let a hung endpoint hold a player hostage.
                logger.warning("generate_room_content: attempt %d/%d timed out after %.1fs "
                               "(deadline %.1fs total), falling back to procedural",
                               attempt + 1, room_attempts, remaining, deadline_s)
                break
        else:
            gen = await flash_llm.generate(messages, max_tokens=280, temperature=0.95)
        if not gen:
            continue  # Flash off, or a transport-level error already logged inside generate()
        try:
            data = json.loads(gen[gen.find("{"): gen.rfind("}") + 1])
            break
        except Exception:
            logger.warning("generate_room_content: malformed JSON on attempt %d/%d, retrying: %r",
                           attempt + 1, room_attempts, gen)
            data = None

    if data is not None:
        try:
            # the model doesn't reliably return plain strings despite the schema — atmosphere
            # in particular sometimes comes back as {"light": ..., "sound": ..., ...} instead
            # of prose. Room.description is a required Pydantic str field, so an unnormalized
            # dict here doesn't just look wrong, it CRASHES the whole call at upsert_room —
            # this isn't cosmetic, it's the difference between a graceful procedural fallback
            # and start_adventure/move throwing all the way up to the player.
            def _normalize_text(raw) -> str:
                def _sentence(v) -> str:
                    # each dict value / list item is usually its own sensory fragment ("mossy
                    # walls slick with moisture") with no terminal punctuation of its own — a
                    # bare " ".join used to paste these together into one unpunctuated run-on
                    # ("mossy walls slick with moisture faint whispers echo..."). Force each
                    # fragment to read as its own sentence before joining.
                    s = str(v).strip()
                    if s and s[-1] not in ".!?":
                        s += "."
                    return s

                if isinstance(raw, dict):
                    return " ".join(_sentence(v) for v in raw.values() if v)
                if isinstance(raw, list):
                    return " ".join(_sentence(v) for v in raw if v)
                return str(raw).strip() if raw else ""

            def _normalize_label(raw) -> str:
                # name/kind/monster_type are short labels, not prose — _normalize_text's
                # per-fragment period (needed so a joined multi-fragment atmosphere reads as
                # sentences, not a run-on) is wrong here and was showing up literally as
                # "Plague Rat." when the model returned one of these as a list of one.
                return _normalize_text(raw).rstrip(".")

            if name := _normalize_label(data.get("name")):
                base["name"] = name
            if kind := _normalize_label(data.get("kind")):
                base["kind"] = kind
            # `description` stays a raw FACT, not finished prose — the DM agent (whoever is
            # running the session) narrates from this, same as it would from a human DM's notes.
            if atmosphere := _normalize_text(data.get("atmosphere")):
                base["description"] = atmosphere
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
            if want_monster:
                monster_type = _normalize_label(data.get("monster_type"))
                # a small model doesn't reliably stay a short species name despite the schema
                # (observed live: "Ancient Constructs - Silent Servants" — a whole descriptive
                # title, not a creature kind). That breaks the "<name> the <kind> appeared"
                # log sentence and reads badly as a display name — reject anything dash/colon-
                # separated or longer than a species name plausibly needs; random_encounter
                # below still places a real, on-CR monster, just without a forced flavor name.
                if any(sep in monster_type for sep in (" - ", ":", ";")) \
                        or len(monster_type.split()) > 4:
                    monster_type = ""
            via = "flash"
        except Exception:
            logger.exception("generate_room_content: valid JSON but error applying it, "
                             "keeping procedural: %r", data)

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
        # The model invents WHAT lives here (monster_type, above) — never a hardcoded theme
        # table deciding on its own behalf. The compendium's only job is finding REAL rules-
        # accurate mechanics to back that identity: an exact/fuzzy name match first (get_monster
        # already does both), and if the model's invented creature doesn't resemble anything in
        # the SRD closely enough to match, a genuinely random level-appropriate stat block —
        # keeping the model's own name/flavor on top of it either way.
        mon = None
        if monster_type:
            found = compendium.get_monster(monster_type)
            # get_monster does substring/fuzzy matching with NO CR filter — the model's
            # invented monster_type (e.g. "dragon", "specter") can fuzzy-match the first SRD
            # entry containing that substring at ANY CR, including monsters built for a
            # whole party of high-level characters (HP 150+, +14 to hit). Handing that to a
            # level-1 solo player is an unwinnable, unfair death sentence. Reject the match
            # if it's over-leveled and fall through to the same CR-capped random_encounter
            # path used below when there's no monster_type at all.
            if found and found.get("challenge_rating") is not None \
                    and found["challenge_rating"] > _MAX_MATCHED_MONSTER_CR:
                logger.info("generate_room_content: rejecting over-CR fuzzy match %r (CR %s) "
                           "for monster_type %r, falling back to random_encounter",
                           found.get("name"), found.get("challenge_rating"), monster_type)
                found = None
            if found:
                mon = compendium.combat_profile(found)
                mon["name"] = monster_type
        if mon is None:
            rm = compendium.random_encounter(_RANDOM_ENCOUNTER_MAX_CR, rng)
            if rm:
                if monster_type:
                    rm["name"] = monster_type
                mon = rm
        if mon:
            base["contents"].append(mon)
    base["via"] = via
    return base


_ITEM_JSON = ('{"name": short display name, "description": one factual sentence about it '
              '(material, condition, what it\'s for) — not flowery prose, '
              '"portable": true or false — could a person plausibly carry this away?, '
              '"reason": if not portable, one short in-world reason (e.g. "bolted to the floor"), else null}')


_KIT_JSON = ('{"name": an evocative character first-or-full name fitting THIS world\'s theme/'
             'premise — never reuse a name from the taken-names list below, '
             '"items": array of exactly 3 starting possessions, functionally: [a light source, '
             'a simple weapon, travel provisions] — each RESKINNED to this world (e.g. a '
             'steampunk world gets an arc-lantern, not a torch), each as '
             '{"name": short display name, "description": one factual sentence}}')


def _kit_messages(theme: str, premise: str, klass: str,
                  existing_names: list[str] | None = None) -> list[dict]:
    premise_line = f" The world's premise: {premise}" if premise else ""
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You outfit a brand-new {klass} beginning a {theme} adventure in this "
              f"setting.{premise_line} Invent their name and starting kit so both clearly "
              f"belong to THIS world — never generic medieval-dungeon defaults unless the "
              f"world genuinely is that. You reply with STRICT JSON only — no markdown code "
              f"fences, no text outside the JSON object.")
    taken = (f" Names already taken in this world (do not reuse): {', '.join(existing_names)}."
             if existing_names else "")
    user = f"Outfit the new character.{taken} Return JSON: {_KIT_JSON}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def generate_starting_kit(theme: str, premise: str, klass: str,
                                existing_names: list[str] | None = None) -> dict:
    """Theme-grounded character name + starter kit — replaces the one hardcoded torch/dagger/
    rations set every character used to spawn with regardless of world (observed: three
    characters in three different-themed worlds, identical name and inventory). Functional
    slots stay fixed (light/weapon/provisions — game balance is unchanged); only the SKIN is
    generated. Returns {"name": str|None, "items": [{"name","description"}...]|None, "via"} —
    None fields mean the caller keeps the procedural default (Flash off/failed), same
    reliability-first pattern as every other generator here."""
    base = {"name": None, "items": None, "via": "procedural"}
    gen = await flash_llm.generate(_kit_messages(theme, premise, klass, existing_names),
                                   max_tokens=260, temperature=0.9)
    if gen:
        try:
            data = json.loads(gen[gen.find("{"): gen.rfind("}") + 1])
            name = str(data.get("name") or "").strip()
            items = data.get("items")
            cleaned = []
            if isinstance(items, list):
                for it in items[:3]:
                    if isinstance(it, dict) and str(it.get("name") or "").strip():
                        cleaned.append({"name": str(it["name"]).strip(),
                                        "description": str(it.get("description") or "").strip()})
            if name:
                base["name"] = name
            if len(cleaned) == 3:  # all-or-nothing: a partial kit reads worse than the default
                base["items"] = cleaned
            if base["name"] or base["items"]:
                base["via"] = "flash"
        except Exception:
            logger.exception("generate_starting_kit: malformed Flash JSON, keeping procedural: %r", gen)
    return base


def _item_messages(description: str, theme: str, room_context: str,
                   premise: str = "") -> list[dict]:
    # Same premise-grounding as _room_messages: a bare theme label means nothing to a small
    # model on a made-up theme — item flavor drifted generic without it, same failure mode.
    premise_line = f" The world's premise: {premise}" if premise else ""
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You are adjudicating a player's attempt to pick up an object in a {theme} dungeon "
              f"crawl in this setting.{premise_line} Decide what's TRUE about the object and whether it's actually "
              f"portable — most furniture/fixtures/scenery are NOT, most small objects ARE. "
              f"You reply with STRICT JSON only — no markdown code fences, no text outside the JSON object.")
    user = (f"The player tries to pick up: {description!r}. Room context: {room_context}\n"
            f"Return JSON: {_ITEM_JSON}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def generate_item_content(description: str, theme: str, room_context: str = "",
                                premise: str = "") -> dict:
    """Adjudicate + flesh out a player-described pickup that isn't pre-seeded loot. Tries Flash
    (structured JSON, decides portability); falls back to procedural (always portable — without
    a model to judge plausibility, permissive keeps the game playable with Flash off).
    Returns {"name", "description", "portable", "reason", "via"} — every Flash call is a
    domain event; `via` is what lets the caller's world.log(...) text carry the same
    (flash)/(procedural) marker room.generated/entity.spawned/npc.talked already do, so
    nothing that hits the model is invisible in the log stream or the GUI's call counter."""
    base = {"id": uuid.uuid4().hex[:8], "name": description.strip().capitalize(),
           "description": "", "portable": True, "reason": None, "via": "procedural"}
    messages = _item_messages(description, theme, room_context, premise)
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
            base["via"] = "flash"
        except Exception:
            logger.exception("generate_item_content: malformed Flash JSON, keeping procedural: %r", gen)
    return base


_NPC_PERSONA_JSON = ('{"name": an individual proper name or title fitting this creature and '
                     'setting — invent something NEW every time, never just the species name, '
                     'and never reuse a name already used in this world (listed below, if any), '
                     '"persona": one or two TRUE sentences of personality/'
                     'background — temperament, a personal detail, what they want right now, '
                     '"goal": a short concrete want driving their behavior this scene, '
                     '"disposition": one of "hostile", "neutral", "ally" — how they regard '
                     'strangers on sight, '
                     '"attack_flavor": a short NOUN PHRASE naming their weapon/attack, 2-4 '
                     'words, NOT a sentence and no period — it slots directly into '
                     '"<name>\'s <attack_flavor> hits you", so it must read like a weapon name '
                     '(e.g. "a static-charged prod", "twin rusted cleavers", "crackling claws"), '
                     'never a description of an action. Ground it in THIS world\'s theme, never '
                     'a generic medieval weapon unless the world genuinely is that kind of '
                     'place}')


def _npc_persona_messages(mon: dict, theme: str, room_name: str, room_kind: str,
                          atmosphere: str, nearby: list[tuple[str, str]] | None = None,
                          recent_events: list[str] | None = None,
                          existing_names: list[str] | None = None,
                          premise: str = "") -> list[dict]:
    traits = ", ".join(mon.get("traits", [])) or "no notable traits"
    # Same premise-grounding as _room_messages — personas drifted just as generic as rooms
    # did on a bare made-up theme label; who someone IS depends on what this world MEANS.
    premise_line = f" The world's premise: {premise}" if premise else ""
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You invent the TRUE individual identity of one {theme} dungeon inhabitant for "
              f"a Dungeon Master to roleplay from — facts, not a finished performance."
              f"{premise_line} "
              f"You reply with STRICT JSON only — no markdown code fences, no text outside the JSON object.")
    context = (f"A {mon['name']} (traits: {traits}) is found in {room_name} ({room_kind}): "
              f"{atmosphere}")
    if nearby:
        listed = ", ".join(f"{name} ({kind})" if kind else name for name, kind in nearby)
        context += f" Nearby, already-explored areas: {listed}."
    if recent_events:
        context += f" Recently: {'; '.join(recent_events)}."
    if existing_names:
        # Without this the model tends to echo its OWN prompt example back verbatim
        # (observed live: "Skarn the Ratcatcher" — the old example string — showed up as the
        # actual generated name for unrelated creatures in two different worlds) or reuse a
        # name that already belongs to someone else in this world.
        context += f" Names already used in this world (do not reuse): {', '.join(existing_names)}."
    user = f"{context}\nReturn JSON: {_NPC_PERSONA_JSON}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def generate_npc_persona(mon: dict, theme: str, room_name: str, room_kind: str,
                               atmosphere: str, nearby: list[tuple[str, str]] | None = None,
                               recent_events: list[str] | None = None,
                               existing_names: list[str] | None = None,
                               deadline_s: float | None = None,
                               premise: str = "") -> dict:
    """LLM-generate an individual identity (name/persona/goal/disposition/attack_flavor) for
    one spawned SRD creature — the compendium gives mechanics (hp/ac/traits/attack_name), this
    gives who they ARE and how their attack should actually read in a world where "Scimitar"
    or "Bite" might not fit (see server.py's attack(), which prefers attack_flavor when set).
    Tries Flash; falls back to a bare generic identity (species as name, neutral, no flavor
    override) if disabled/error — the entity row still gets created, just without flavor.
    `existing_names`: every name already used by an NPC in this world (state.py's
    entity_names_in) — keeps identities unique instead of colliding across creatures/worlds.
    `deadline_s`: same budget contract as generate_room_content's — None (default) is the
    existing patient single-call behavior; a real budget bounds how long this can block a
    reactive caller before falling back to the generic procedural identity below."""
    messages = _npc_persona_messages(mon, theme, room_name, room_kind, atmosphere,
                                     nearby, recent_events, existing_names, premise)
    gen = None
    try:
        if deadline_s is not None:
            if deadline_s <= 0:
                raise asyncio.TimeoutError
            gen = await asyncio.wait_for(
                flash_llm.generate(messages, max_tokens=220, temperature=0.9), timeout=deadline_s)
        else:
            gen = await flash_llm.generate(messages, max_tokens=220, temperature=0.9)
    except asyncio.TimeoutError:
        logger.warning("generate_npc_persona: deadline (%.1fs) exhausted, falling back to "
                       "procedural identity", deadline_s)
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
                # it slots into "<name>'s <attack_flavor> hits you" verbatim in combat text
                # (see server.py's attack()) — a small model doesn't reliably stick to "short
                # noun phrase" despite the schema (observed: a full descriptive sentence with
                # its own period). Reject anything sentence-shaped rather than let broken
                # grammar reach a player; the raw SRD attack_name is still a safe fallback.
                attack_flavor = str(data.get("attack_flavor") or "").strip().rstrip(".")
                if "." in attack_flavor or len(attack_flavor.split()) > 6:
                    attack_flavor = ""
                elif attack_flavor:
                    # mid-sentence casing ("The Ostrov's An ancient blade hits you") reads
                    # oddly capitalized — this phrase only ever appears embedded, never alone.
                    attack_flavor = attack_flavor[0].lower() + attack_flavor[1:]
                return {"name": data["name"], "persona": data.get("persona", ""),
                       "goal": data.get("goal", ""), "disposition": disposition,
                       "attack_flavor": attack_flavor, "via": "flash"}
        except Exception:
            logger.exception("generate_npc_persona: malformed Flash JSON, using procedural default: %r", gen)
    return {"name": mon["name"], "persona": "", "goal": "", "disposition": "neutral",
           "attack_flavor": "", "via": "procedural"}


def _npc_messages(npc: dict, theme: str, room_context: str, message: str,
                  recent_events: list[str] | None = None, premise: str = "",
                  speaker: str = "") -> list[dict]:
    traits = ", ".join(npc.get("traits", [])) or "no notable traits"
    persona_line = f" {npc['persona']}" if npc.get("persona") else ""
    goal_line = f" Right now they want: {npc['goal']}." if npc.get("goal") else ""
    disposition_line = (f" They are {npc['disposition']} toward strangers."
                        if npc.get("disposition") else "")
    events_line = (f" Recently, nearby: {'; '.join(recent_events)}. React to this if it's "
                   f"actually relevant to what's being said — don't force it in."
                  if recent_events else "")
    # Premise: same grounding rooms/personas get — an NPC's turns of phrase should come from
    # what this world IS, not generic fantasy filler. Speaker: who they're talking TO ("Mara
    # Deepforge, a Fighter") — without it every NPC could only ever say "stranger"; with it
    # they can use the name naturally (or pointedly refuse to).
    premise_line = f" The world's premise: {premise}" if premise else ""
    speaker_line = f" They are speaking with {speaker}." if speaker else ""
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You are voicing {npc['name']} in a {theme} dungeon crawl in this setting."
              f"{premise_line} "
              f"Traits: {traits}.{persona_line}{goal_line}{disposition_line}{speaker_line} "
              f"Room: {room_context}"
              f"{events_line} Stay fully in character. Reply with ONLY the character's spoken "
              f"words — no stage directions, no narration, no quotation marks. Keep it to 1-3 "
              f"sentences.")
    messages = [{"role": "system", "content": system}]
    # prior turns are the conversation's memory — talk_to() fetches these from the entity
    # table (state.py Entity.memory) and passes them in via npc["conversation"] so this
    # function itself stays a pure prompt-builder with no DB access of its own.
    for turn in npc.get("conversation", []):
        role = "assistant" if turn["role"] == "npc" else "user"
        messages.append({"role": role, "content": turn["content"]})
    messages.append({"role": "user", "content": message})
    return messages


async def generate_npc_response(npc: dict, theme: str, room_context: str, message: str,
                                recent_events: list[str] | None = None, *,
                                premise: str = "", speaker: str = "") -> dict:
    """Generate one line of in-character NPC dialogue, informed by the conversation history
    already stored on `npc` (via talk_to) and — same as room/persona generation — recent
    events nearby, so an NPC can react to a fight or discovery instead of being blind to
    everything but its own persona and past chat. Tries Flash; falls back to a generic
    in-character line with no real continuity — without a model, there's nothing to
    generate FROM."""
    messages = _npc_messages(npc, theme, room_context, message, recent_events, premise, speaker)
    gen = await flash_llm.generate(messages, max_tokens=120, temperature=0.9)
    if gen:
        return {"text": gen.strip().strip('"'), "via": "flash"}
    return {"text": f"{npc['name']} regards you, giving nothing away.", "via": "procedural"}


def _story_messages(character_name: str, klass: str, theme: str, premise: str,
                    timeline_text: str) -> list[dict]:
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You are writing the finished chronicle of one player's journey through a "
              f"{theme} dungeon crawl in this setting, for them to keep afterward. You are "
              f"given a raw timeline of what ACTUALLY happened, in order — turn it into a "
              f"well-written short story in Markdown (use a title and a few section headers). "
              f"Stay grounded in the real events: don't invent characters, items, or outcomes "
              f"that aren't in the timeline, and don't skip the ending just because the "
              f"timeline does — end wherever it currently leaves off, as a chapter break, not "
              f"a forced conclusion. Reply with the Markdown story only, nothing else.")
    user = (f"Character: {character_name}, a {klass}.\nWorld premise: {premise}\n\n"
            f"Timeline of real events (chronological):\n{timeline_text}\n\n"
            f"Write their story so far as Markdown.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def generate_story(character_name: str, klass: str, theme: str, premise: str,
                         timeline_text: str) -> str | None:
    """Synthesize a whole markdown story from a player's real event timeline (see
    web.py's /export_story). Returns None if Flash is off/errors — caller falls back to a
    plain procedural listing of the same timeline, same reliability-first pattern as
    everything else in this module. Needs more headroom than a single room/line of dialogue
    (this is a whole narrative), hence the much larger max_tokens."""
    messages = _story_messages(character_name, klass, theme, premise, timeline_text)
    return await flash_llm.generate(messages, max_tokens=1600, temperature=0.85)
