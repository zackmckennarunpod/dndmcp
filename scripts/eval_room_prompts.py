"""Eval harness: compare room-generation prompt variants against the live vLLM endpoint.

Bare SDK-free HTTP calls (no forge) against the already-deployed dnd-vllm4 endpoint
(runpod/worker-v1-vllm:v2.22.4, scale-to-zero). First call cold-starts (~15-20s).

python -m scripts.eval_room_prompts
"""
from __future__ import annotations

import json
import subprocess
import urllib.request

ENDPOINT_ID = "1c756461ke9pvr"
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"

KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "runpod-api-key-prod", "-w"],
    capture_output=True, text=True, check=True,
).stdout.strip()

# --- variant A: current worldgen.py prompt (baseline) ---
def prompt_a(theme, came_from, exits):
    room_json = ('{"name": short evocative room name, "kind": one or two words, '
                 '"look": {"ahead": "...", "left": "...", "right": "...", "center": "..."}, '
                 '"feature": one specific examinable detail, "has_monster": true or false}')
    system = (f"You are the world-builder for a {theme} dungeon crawl. You invent vivid rooms "
              f"described SPATIALLY (what is ahead, to the sides, in the center), and you reply "
              f"with STRICT JSON only — no text outside the JSON object.")
    enter = f" the player enters from the {came_from}" if came_from else " the player descends into"
    user = (f"Generate the next room{enter}. Exits lead: {', '.join(exits) or 'none'}. "
            f"Describe it directionally so the player knows what is where. Return JSON: {room_json}")
    return system, user

# --- variant B: refined schema (multiple features, exit-keyed description, item hook) ---
def prompt_b(theme, came_from, exits):
    room_json = ('{"name": short evocative room name, "kind": one or two words, '
                 '"description": {<one key per exit direction + "center">: vivid spatial text}, '
                 '"features": [2-3 specific examinable details], '
                 '"has_monster": true or false, "notable_item": short item description or null}')
    system = (f"You are the world-builder for a {theme} dungeon crawl. You invent vivid rooms. "
              f"CRITICAL: the room has EXACTLY these exits: {', '.join(exits) or 'none'} — your "
              f"'description' object must have one key per exit direction listed (describing what "
              f"is visible/audible in that direction) plus a 'center' key, and must NOT describe "
              f"a passage/exit in any direction not listed. Reply with STRICT JSON only.")
    enter = f" the player enters from the {came_from}." if came_from else " the player descends into darkness."
    user = f"Generate the next room.{enter} Return JSON: {room_json}"
    return system, user


SCENARIOS = [
    {"theme": "gothic horror", "came_from": "south", "exits": ["north", "east"]},
    {"theme": "gothic horror", "came_from": "west", "exits": ["north"]},
]


def call(system, user, max_tokens=280):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens, "temperature": 0.9,
    }).encode()
    url = f"https://api.runpod.ai/v2/{ENDPOINT_ID}/openai/v1/chat/completions"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    return resp["choices"][0]["message"]["content"]


def main():
    for i, scenario in enumerate(SCENARIOS):
        print(f"\n{'='*70}\nSCENARIO {i+1}: {scenario}\n{'='*70}")
        for label, fn in [("A (baseline)", prompt_a), ("B (refined)", prompt_b)]:
            system, user = fn(**scenario)
            print(f"\n--- Variant {label} ---")
            try:
                text = call(system, user)
                print(text)
                try:
                    parsed = json.loads(text[text.find("{"):text.rfind("}")+1])
                    print(f"  [parsed OK, keys={list(parsed.keys())}]")
                except Exception as e:
                    print(f"  [JSON PARSE FAILED: {e}]")
            except Exception as exc:
                print(f"  FAILED: {type(exc).__name__}: {str(exc)[:300]}")


if __name__ == "__main__":
    main()
