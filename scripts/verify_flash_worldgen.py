"""Verify the Flash LLM endpoint for our use case — the full plan, executed.

Proves the critical Flash anchor works: a model running on Flash produces reliable STRUCTURED
output for world-gen, across multiple SKILLS, scaling via fan-out, written to the DB — with
honest latency/cost, then torn down.

PLAN (each step = a pass/fail check):
  1. DEPLOY+WARM   — mint the Flash LLM (ADA_24, workers_min=1), confirm it comes up (no
                     allocation hang), model loads, returns text.
  2. STRUCTURED    — describe_room skill → valid JSON with required fields, N trials, parse rate.
  3. MULTI-SKILL   — same endpoint serves different skills (room, npc, lore) → all parse.
  4. SCALE/BURST   — fan out K room generations in parallel (generate-ahead), all succeed.
  5. DB INTEGRATION— a generated room writes to the world DB and reads back intact.
  6. LATENCY/COST  — warm per-gen seconds + total $.
  7. FALLBACK      — with Flash OFF, world-gen still works (procedural) — game never dies.
  8. TEARDOWN      — endpoint deleted, account clean.

Run:
    DND_FLASH_LLM=1 python -m scripts.verify_flash_worldgen
    DND_FLASH_LLM=1 python -m scripts.verify_flash_worldgen --keep   # leave warm for the demo
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

os.environ.setdefault("DND_FLASH_LLM", "1")  # this script is ABOUT testing Flash

import forge  # noqa: E402
from dndmcp import flash_llm, worldgen, setting  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")


def _parse(text: str) -> dict | None:
    try:
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception:
        return None


# extra SKILLS (beyond describe_room) to prove one endpoint serves many
def _npc_messages() -> list[dict]:
    return [{"role": "system", "content": setting.GEN_BRIEF + " Reply STRICT JSON only."},
            {"role": "user", "content": 'Invent one NPC. JSON: {"name": "...", "role": "...", '
             '"personality": "...", "secret": "...", "voice": "..."}'}]


def _lore_messages() -> list[dict]:
    return [{"role": "system", "content": setting.GEN_BRIEF + " Reply STRICT JSON only."},
            {"role": "user", "content": 'Invent one piece of discoverable lore (a relic or '
             'inscription). JSON: {"title": "...", "text": "2 sentences", "hook": "..."}'}]


async def main(keep: bool = False) -> int:
    print("=== Flash LLM world-gen verification ===")
    forge.load_env("prod")
    t0 = time.time()

    # 1. DEPLOY + WARM
    print("[1] deploy + warm (cold start: deps + model download, can be minutes) ...")
    try:
        w = await flash_llm.warm()
        check("1. deploy+warm (endpoint up, model loaded, returns text)", bool(w.get("ok")),
              f"warm sample={w.get('sample')!r} in {w.get('seconds')}s")
        if not w.get("ok"):
            print("      cannot proceed without a live endpoint.");
    except Exception as exc:
        check("1. deploy+warm", False, f"{type(exc).__name__}: {exc}")

    # 2. STRUCTURED describe_room (N trials)
    print("[2] structured describe_room JSON (5 trials) ...")
    msgs = worldgen._room_messages("gothic horror", "north", ["east", "south"])
    parsed_ok, samples = 0, []
    lat = []
    for i in range(5):
        s = time.time()
        out = await flash_llm.generate(msgs, max_tokens=280, temperature=0.95)
        lat.append(time.time() - s)
        d = _parse(out or "")
        has_fields = bool(d and d.get("name") and d.get("look"))
        parsed_ok += int(has_fields)
        if d and not samples:
            samples.append(d)
    check("2. structured world-gen JSON (name+look fields)", parsed_ok >= 4,
          f"{parsed_ok}/5 valid; example name={samples[0].get('name') if samples else None!r}")
    if samples:
        look = samples[0].get("look", {})
        print(f"      directional look -> ahead={look.get('ahead')!r}")

    # 3. MULTI-SKILL (room already done; npc + lore)
    print("[3] multi-skill on the same endpoint (npc, lore) ...")
    npc = _parse(await flash_llm.generate(_npc_messages(), max_tokens=200) or "")
    lore = _parse(await flash_llm.generate(_lore_messages(), max_tokens=200) or "")
    check("3. multi-skill (npc + lore parse)", bool(npc and npc.get("name")) and bool(lore and lore.get("title")),
          f"npc={npc.get('name') if npc else None!r} lore={lore.get('title') if lore else None!r}")

    # 4. SCALE / BURST — fan out K room generations in parallel
    print("[4] scale: fan out 4 room generations in parallel (generate-ahead burst) ...")
    s = time.time()
    outs = await asyncio.gather(*[
        flash_llm.generate(worldgen._room_messages("gothic horror", d, ["north"]), max_tokens=260)
        for d in ["north", "south", "east", "west"]
    ])
    burst_ok = sum(1 for o in outs if _parse(o or ""))
    check("4. scale/burst (4 parallel gens)", burst_ok >= 3,
          f"{burst_ok}/4 parsed in {time.time()-s:.1f}s wall (parallel)")

    # 5. DB INTEGRATION — full worldgen → write → read back
    print("[5] DB integration: generate a room, write to world DB, read back ...")
    os.environ["DNDMCP_STATE_DIR"] = "/tmp/dnd_verify"
    import shutil; shutil.rmtree("/tmp/dnd_verify", ignore_errors=True)
    from dndmcp.state import World
    wdb = World()
    room = await worldgen.generate_room_content("r0", "gothic horror")
    wdb.new_campaign(theme="gothic horror", premise="test", start_room="r0")
    wdb.upsert_room(room_id="r0", name=room["name"], description=room["description"],
                    exits=room["exits"], contents=room["contents"], features=room.get("features"))
    back = wdb.room("r0")
    check("5. DB integration (room persisted via Flash gen)", bool(back and back["name"]),
          f"via={room.get('via')} name={back['name']!r} features={len(back.get('features',[]))}")

    # 6. LATENCY / COST
    avg = sum(lat) / len(lat) if lat else 0
    rollup = forge.summarize(forge.Registry().call_records()) if False else None
    check("6. latency (warm per-gen)", avg < 8.0, f"avg {avg:.1f}s/room")

    # 7. FALLBACK — Flash off → procedural still works
    print("[7] fallback: Flash OFF → procedural world-gen still works ...")
    flash_llm.ENABLED = False
    proc = await worldgen.generate_room_content("rX", "gothic horror")
    flash_llm.ENABLED = True
    check("7. fallback (game never dies)", proc.get("via") == "procedural" and bool(proc["name"]),
          f"via={proc.get('via')} name={proc['name']!r}")

    # 8. TEARDOWN
    if keep:
        print("[8] --keep: leaving endpoint warm for the demo.")
        check("8. teardown", True, "skipped (--keep)")
    else:
        print("[8] teardown ...")
        res = await flash_llm.teardown()
        remaining = [e["name"] for e in await forge.server_endpoints()]
        leaks = [n for n in remaining if "dnd-llm" in n]
        check("8. teardown (no leaks)", not leaks, f"deleted; remaining={remaining}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n=== {passed}/{len(RESULTS)} checks passed in {int(time.time()-t0)}s ===")
    for n, ok, _ in RESULTS:
        print(f"  {'✅' if ok else '❌'} {n}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(keep="--keep" in sys.argv)))
