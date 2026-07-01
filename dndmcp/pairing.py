"""Onboarding pairing codes — how a browser learns "that agent-driven character is ME."

The onboarding wizard (web GUI) mints a short, memorable, single-use code and tells the
visitor: say "start an adventure, pairing code <code>" to your agent. The DM agent, seeing a
code mentioned, calls the claim_pairing MCP tool right after start_adventure; the wizard
polls the claim state and, on success, greets the visitor by their character's name and
hands them their personal ?player= map link.

WHY this exists: the live event stream can prove *someone* joined, never that it was YOU —
and since the player_id-leak fix, a browser has no legitimate way to learn its own full
player link for an agent-driven character. A code minted by the same browser that redeems
the result is that channel, done safely: single-use, ~10 min TTL, unguessable enough for its
lifetime (two words from 64-word lists = 4096 combos, and a claim consumes the code so
brute-force has one winning guess at most before the real user notices theirs "was already
used").

STRICTLY OPTIONAL at every layer: an agent that never mentions a code onboards exactly as
before (the tool is additive); the wizard's join-feed fallback covers players who forget.
In-memory on purpose — codes are a 10-minute handshake, not state worth persisting; a
redeploy mid-handshake just means minting a fresh code.
"""

from __future__ import annotations

import secrets
import time

_TTL_SECONDS = 10 * 60

# Small, distinct, easy-to-say-to-an-agent words — no homophones of game terms.
_LEFT = ["amber", "ashen", "bright", "cinder", "coral", "dusk", "ember", "fable",
         "frost", "gilded", "hollow", "iron", "ivory", "jade", "keen", "lunar",
         "marble", "night", "oaken", "opal", "pale", "quiet", "raven", "rust",
         "sable", "silver", "storm", "thorn", "umber", "velvet", "wild", "zephyr"]
_RIGHT = ["antler", "badger", "crane", "drake", "falcon", "fox", "heron", "ibex",
          "jackal", "kestrel", "lynx", "marten", "newt", "otter", "owl", "pike",
          "quail", "raccoon", "salmon", "shrike", "stoat", "swift", "tern", "viper",
          "vole", "wren", "moth", "hare", "boar", "crow", "eel", "finch"]

# code -> {"created_at": ts, "player_id": str|None, "name": str|None, "campaign_id": str|None}
_codes: dict[str, dict] = {}


def _prune() -> None:
    cutoff = time.time() - _TTL_SECONDS
    for code in [c for c, v in _codes.items() if v["created_at"] < cutoff]:
        del _codes[code]


def mint() -> str:
    """A fresh unclaimed code for one onboarding attempt (called by the wizard endpoint)."""
    _prune()
    while True:
        code = f"{secrets.choice(_LEFT)}-{secrets.choice(_RIGHT)}"
        if code not in _codes:
            _codes[code] = {"created_at": time.time(), "player_id": None,
                            "name": None, "campaign_id": None}
            return code


def claim(code: str, *, player_id: str, name: str, campaign_id: str) -> bool:
    """Bind a just-created character to a live code (called by the MCP tool). False when the
    code is unknown/expired/already used — the tool surfaces that as guidance, never an
    error that would interrupt the adventure itself."""
    _prune()
    entry = _codes.get((code or "").strip().lower())
    if entry is None or entry["player_id"] is not None:
        return False
    entry.update(player_id=player_id, name=name, campaign_id=campaign_id)
    return True


def status(code: str) -> dict | None:
    """Wizard poll: None = unknown/expired; else {"claimed": bool, "name", "player_id",
    "campaign_id"} — player_id is only ever returned HERE, to the browser that minted the
    code (see module docstring for why that's the sanctioned exception)."""
    _prune()
    entry = _codes.get((code or "").strip().lower())
    if entry is None:
        return None
    return {"claimed": entry["player_id"] is not None, "name": entry["name"],
            "player_id": entry["player_id"], "campaign_id": entry["campaign_id"]}
