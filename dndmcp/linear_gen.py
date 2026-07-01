"""Ticket-graph generation — same procedural+Flash+fallback pattern as worldgen.py, different
domain. Proves the engine generalizes: same flash_llm.generate() call, a different
prompt-builder, a different node type."""

from __future__ import annotations

import json

from . import flash_llm
from .linear_world import Ticket

_TICKET_JSON = ('{"title": short actionable ticket title, '
                '"description": skimmable in 30 seconds — see style rules, '
                '"priority": exactly one of "low", "medium", "high" — pick ONE, never list more than one}')

# Real house style (this team's actual Linear conventions, not invented for this demo) — the
# "specific skill" that makes generated tickets read like this team's tickets, not generic
# LLM-speak.
_TICKET_STYLE = (
    "Ticket style rules (skimmable by a teammate in 30 seconds):\n"
    "- Lead with 1-2 plain sentences: what needs to happen + the one constraint that matters.\n"
    "- Then a 'Scope:' bullet list in plain product language.\n"
    "- Then a 'Done when:' bullet list of concrete completion criteria.\n"
    "- NO architecture jargon (DDD, aggregate, seam, ACL) in the main description.\n"
    "- Put the description fields inside the JSON `description` value, newline-separated."
)


def _followup_messages(ticket: Ticket, neighbors: list[tuple[str, Ticket]]) -> list[dict]:
    system = (f"You are a project-planning assistant with a specific skill: writing tickets in "
              f"this team's exact house style, not generic project-management prose.\n\n"
              f"{_TICKET_STYLE}\n\n"
              f"Given a just-completed engineering task and its related tickets, propose ONE "
              f"realistic, specific follow-up task that makes sense next — not generic busywork. "
              f"Reply with STRICT JSON only — one value per field, no trailing extras.")
    context = ""
    if neighbors:
        listed = "; ".join(f"{rel}: {t.title}" for rel, t in neighbors)
        context = f" Related tickets: {listed}."
    user = (f"Completed: {ticket.title!r} — {ticket.description}{context} "
            f"Propose one follow-up ticket. Return JSON: {_TICKET_JSON}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def generate_followup_ticket(ticket: Ticket, neighbors: list[tuple[str, Ticket]]) -> dict:
    """Generate a plausible follow-up ticket given a completed one + its graph neighbors.
    Tries Flash; falls back to a generic (but honest, non-fake) placeholder if Flash is
    off/errors — never blocks completion on generation succeeding."""
    base = {"title": f"Follow up on: {ticket.title}", "description": "Review outcome and next steps.",
           "priority": "medium", "via": "procedural"}
    messages = _followup_messages(ticket, neighbors)
    gen = await flash_llm.generate(messages, max_tokens=200, temperature=0.8)
    if gen:
        try:
            data = json.loads(gen[gen.find("{"): gen.rfind("}") + 1])
            if data.get("title"):
                base["title"] = data["title"]
            if data.get("description"):
                base["description"] = data["description"]
            if data.get("priority") in ("low", "medium", "high"):
                base["priority"] = data["priority"]
            base["via"] = "flash"
        except Exception:
            pass  # malformed JSON → keep the procedural placeholder
    return base
