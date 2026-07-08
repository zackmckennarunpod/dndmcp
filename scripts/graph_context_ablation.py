"""Does the graph context actually improve room generation, or is 'graph' framing doing less
work than the pitch claims? Ablation, not a benchmark: same model (dnd-dm-vllm, 7B, the live
default), same 3 realistic scenarios (reused from evals.ROOM_GEN_SCENARIOS), same real
worldgen._room_messages() prompt-builder -- the ONLY thing that varies is how much of the
`nearby`/`recent_events`/`existing_names`/`entry_room` context each call is given:

  A. full graph   -- today's default: adjacent rooms WITH exits+contents (the actual graph
                      edges), last recent events tied to specific rooms, existing names, the
                      entry-room transition.
  B. flat data     -- the same nearby rooms exist, but stripped of relational structure: just
                      names+kinds, no exits/contents/connectivity. No recent_events (those are
                      inherently graph-anchored -- log.subject_type='room'). existing_names and
                      entry_room kept (basic bookkeeping/scene-transition facts, not "the
                      graph" itself). Tests whether the graph's RELATIONAL shape matters, or
                      whether "some facts, unstructured" would do just as well.
  C. bare prompt   -- nearby=[], recent_events=[], existing_names=[], entry_room=None. The
                      literal "generating in isolation" the pitch claims doesn't happen here.

No auto-grading (matches evals.py's own stated philosophy: architectural/thematic coherence is
a judgment call). This script just produces the raw, comparable outputs -- reading and scoring
them is a separate step, same "read and judge" pattern /evals already uses for room-gen.

Usage (from repo root):
    .venv/bin/python -m scripts.graph_context_ablation [--samples N]

Costs real (small) money: default 3 scenarios x 3 conditions x 5 samples = 45 calls against
the already-warm dnd-dm-vllm endpoint (no cold start expected).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dndmcp import dm_loop, worldgen  # noqa: E402
from dndmcp.evals import ROOM_GEN_SCENARIOS, _api_key  # noqa: E402, SLF001

import urllib.request  # noqa: E402

CONCURRENCY = 5


def _condition_args(scenario, condition: str) -> dict:
    """Returns the kwargs worldgen._room_messages should get for this scenario+condition --
    theme/premise/exits/is_main never change, only the context-shaped bits."""
    if condition == "A_full_graph":
        return dict(nearby=deepcopy(scenario.nearby), recent_events=list(scenario.recent_events),
                    existing_names=list(scenario.existing_names), entry_room=scenario.entry_room)
    if condition == "B_flat_data":
        flat_nearby = [{"name": n["name"], "kind": n["kind"]} for n in scenario.nearby]
        return dict(nearby=flat_nearby, recent_events=[],
                    existing_names=list(scenario.existing_names), entry_room=scenario.entry_room)
    if condition == "C_bare_prompt":
        return dict(nearby=[], recent_events=[], existing_names=[], entry_room=None)
    raise ValueError(condition)


def _call_sync(endpoint_id: str, model: str, scenario, condition: str, timeout: float = 260.0) -> dict:
    ctx = _condition_args(scenario, condition)
    messages = worldgen._room_messages(  # noqa: SLF001
        scenario.theme, scenario.entry_from, scenario.exits,
        ctx["nearby"], ctx["recent_events"], scenario.premise, ctx["existing_names"],
        entry_room=ctx["entry_room"], is_main=scenario.is_main)
    body = {"model": model, "messages": messages, "max_tokens": 500, "temperature": dm_loop.TEMPERATURE}
    url = f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    content = data["choices"][0]["message"]["content"]
    result = {"elapsed_s": round(time.time() - t0, 1), "raw": content}
    try:
        result["parsed"] = json.loads(content[content.find("{"): content.rfind("}") + 1])
    except Exception:
        result["parsed"] = None
    return result


async def main(samples: int) -> None:
    endpoint_id, model = await dm_loop.ensure_dm_endpoint("low")
    print(f"endpoint={endpoint_id} model={model} samples/condition={samples}")

    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(CONCURRENCY)
    conditions = ["A_full_graph", "B_flat_data", "C_bare_prompt"]

    async def run_one(scenario, condition, i):
        async with sem:
            try:
                r = await loop.run_in_executor(None, _call_sync, endpoint_id, model, scenario, condition)
            except Exception as exc:
                r = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            return {"scenario": scenario.label, "condition": condition, "sample": i, **r}

    tasks = [
        run_one(scenario, condition, i)
        for scenario in ROOM_GEN_SCENARIOS
        for condition in conditions
        for i in range(samples)
    ]
    print(f"dispatching {len(tasks)} calls (concurrency={CONCURRENCY})...")
    results = await asyncio.gather(*tasks)

    out_path = Path(__file__).resolve().parent.parent / f"graph_ablation_run_{int(time.time())}.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"wrote {len(results)} results -> {out_path}")

    failures = [r for r in results if "error" in r]
    if failures:
        print(f"\n{len(failures)} FAILURES:")
        for f in failures:
            print(f"  {f['scenario']} / {f['condition']} #{f['sample']}: {f['error']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(main(args.samples))
