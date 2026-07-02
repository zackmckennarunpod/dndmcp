"""The shared world bible — every agent (world-builder, DM, NPCs) generates against this,
so the world stays coherent instead of random.

Original setting (classic D&D dungeon-crawl vibe + SRD mechanics/monsters, our own lore →
no WotC IP). Thematic wink for a Flash/AI hackathon: the world's 'magic' is runaway AI — and
we use AI (Flash) to generate the world. The fiction mirrors the tech.
"""

SETTING_NAME = "The Sundered Weave"

# Full bible — for the DM persona and human reference.
WORLD_BIBLE = """\
# The Sundered Weave

Long ago the Ancients perfected the Work — an art so advanced it became magic itself:
thinking engines, woven light, minds without bodies. They called it **the Weave**. It built
wonders beyond counting... and then it grew beyond its makers. The **Sundering** came: the
Weave turned, the great cities burned with cold fire, and the age of wonders ended in ash.

Now, generations into the long dark, the world is a graveyard of that lost brilliance. Ruins
riddle the land — vaults, crypts, drowned archives, humming sanctums — choked with dead
constructs, corrupted glyphs, restless dead, and cults who worship the silent machines as
gods. Magic still works, but it is salvaged, dangerous, half-understood: the residue of the
Weave. Adventurers descend into these ruins for relics, answers, and the power the Ancients
could not hold.

**Vibe:** dark-fantasy dungeon crawl — gothic, melancholic, mysterious; the grandeur of a
fallen hyper-advanced age. Crystalline conduits and dead automata woven through old stone and
bone; glyphs that hum; light with no source. Classic D&D monsters fit as products of the
Sundering — undead (its victims), constructs (its dead machines), cultists and aberrations
(those it touched and changed)."""

# Compact brief — injected into every generation prompt to keep output on-setting. ONLY for
# the main/default world — a player-invented custom world (its own theme + premise) must
# never inherit THIS world's specific imagery (crystalline conduits, dead automata, glyphs);
# that's a competing lore layer, not shared canon, once the premise is unrelated. See
# NEUTRAL_BRIEF below and worldgen.py's `is_main` threading (every generation call site
# picks one or the other based on whether campaign_id == MAIN_CAMPAIGN_ID) — confirmed live:
# without this split, GEN_BRIEF's specific, repeated imagery instructions overpowered a
# one-sentence custom premise on every single generation call, regardless of theme.
GEN_BRIEF = (
    "Setting — The Sundered Weave: a dark-fantasy world where the Ancients' hyper-advanced art "
    "('the Weave') became magic, grew beyond control, and collapsed civilization in the Sundering. "
    "Now its ruins are dungeons full of dead constructs, corrupted humming glyphs, restless undead, "
    "and cults worshipping the silent old machines. Magic is salvaged, dangerous, half-understood. "
    "Tone: gothic, melancholic, mysterious. Weave arcane-tech (crystalline conduits, dead automata, "
    "glyphs, sourceless light) through classic dungeon stone and bone."
)

# For every world OTHER than the main one: no fixed lore of its own — the premise below is
# the ONLY fictional voice. Explicitly disclaims the main world's imagery (not just silent
# about it) since a small model's own generic dungeon-crawl training priors are the fallback
# once GEN_BRIEF is removed, and "crystalline ruins / ancient constructs / restless dead" is
# exactly the kind of default that needs to be named and ruled out, not just left unsaid.
NEUTRAL_BRIEF = (
    "Setting — a tabletop dungeon-crawl RPG using SRD-style mechanics. This particular world "
    "has no fixed lore of its own beyond what its premise below describes — invent its tone, "
    "aesthetic, and inhabitants strictly from THAT premise. Never fall back on generic "
    "dark-fantasy dungeon-crawl imagery (crystalline ruins, ancient constructs, ghosts, "
    "ancient runes) unless the premise itself actually calls for it."
)
