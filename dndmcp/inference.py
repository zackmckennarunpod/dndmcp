"""NPC inference — the Flash GPU anchor.

`ask_npc` proxies a persona-conditioned inference request to a Flash GPU LLM endpoint:
the MCP server builds (persona + memory + situation) and Flash returns the NPC's reply.
This is "an MCP that proxies GPU requests to Flash," made concrete — and it's what makes
the world alive (NPCs you can actually talk to, each unique).

Reliability-first design (Flash has been flaky):
- STUB by default → the game ALWAYS works with no GPU. Flip FLASH_NPC=1 to use real Flash.
- Real path: a PRE-WARMED Flash LLM endpoint (small fast model on ADA_24, workers_min>=1) so
  there's no cold-start hang mid-conversation. We connect by endpoint id or mint once and reuse.
"""

from __future__ import annotations

import os

# A live Flash LLM endpoint id to proxy to (set once it's deployed + pre-warmed).
NPC_ENDPOINT_ID = os.environ.get("FLASH_NPC_ENDPOINT_ID")
USE_FLASH = os.environ.get("FLASH_NPC", "0") == "1"


def _build_messages(persona: str, memory: list[dict], situation: str, player_says: str) -> list[dict]:
    """Chat messages: persona as system, recent memory as history, current line as user."""
    system = (f"{persona}\n\nYou are an NPC in a dark fantasy RPG. Stay fully in character. "
              f"Reply in 1-3 vivid sentences — speech and maybe a small action. Never break character, "
              f"never mention being an AI. Current situation: {situation}")
    msgs = [{"role": "system", "content": system}]
    for ex in memory[-6:]:
        msgs.append({"role": "user", "content": ex.get("player", "")})
        msgs.append({"role": "assistant", "content": ex.get("npc", "")})
    msgs.append({"role": "user", "content": player_says})
    return msgs


def _stub_reply(persona: str, player_says: str) -> str:
    """Deterministic in-character placeholder so the game works with zero GPU.
    Good enough to demo the loop; real Flash inference replaces it for quality."""
    # pull a one-word vibe from the persona to vary the line
    vibe = "wary"
    for w in ("cruel", "broken", "zealous", "frightened", "ancient", "hungry", "proud"):
        if w in persona.lower():
            vibe = w
            break
    return (f"*The figure regards you, {vibe}.* \"You speak to me as if words could save you. "
            f"State your business, and be quick about it.\"  [stub reply — set FLASH_NPC=1 for live GPU inference]")


async def _flash_reply(messages: list[dict]) -> str:
    """Proxy the inference to a Flash GPU LLM endpoint (OpenAI-style chat)."""
    from runpod_flash import Endpoint  # lazy

    if NPC_ENDPOINT_ID:
        ep = Endpoint(id=NPC_ENDPOINT_ID)
    else:
        raise RuntimeError("no FLASH_NPC_ENDPOINT_ID set")
    # vLLM-style OpenAI-compatible chat completion (LB endpoint)
    resp = await ep.post("/v1/chat/completions", {
        "messages": messages, "max_tokens": 160, "temperature": 0.9,
    })
    try:
        return resp["choices"][0]["message"]["content"].strip()
    except Exception:
        return str(resp)[:300]


async def npc_reply(*, persona: str, memory: list[dict], situation: str, player_says: str) -> dict:
    """Return {text, via} for an NPC's response. Falls back to stub on any Flash error."""
    if USE_FLASH and NPC_ENDPOINT_ID:
        try:
            messages = _build_messages(persona, memory, situation, player_says)
            text = await _flash_reply(messages)
            return {"text": text, "via": "flash"}
        except Exception as exc:
            return {"text": _stub_reply(persona, player_says),
                    "via": f"stub (flash error: {type(exc).__name__})"}
    return {"text": _stub_reply(persona, player_says), "via": "stub"}


# --- general generation proxy — reused by the WORLD-BUILDER (primary Flash use) ----------
WORLDGEN_ENABLED = os.environ.get("FLASH_WORLDGEN", "0") == "1"


async def complete(prompt: str, *, max_tokens: int = 400, temperature: float = 0.9) -> str | None:
    """Proxy a freeform completion to the Flash LLM endpoint. Returns None if unavailable
    (caller falls back to procedural generation). Same endpoint as NPC dialogue."""
    if not (WORLDGEN_ENABLED and NPC_ENDPOINT_ID):
        return None
    try:
        from runpod_flash import Endpoint
        ep = Endpoint(id=NPC_ENDPOINT_ID)
        resp = await ep.post("/v1/chat/completions", {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": temperature,
        })
        return resp["choices"][0]["message"]["content"].strip()
    except Exception:
        return None
