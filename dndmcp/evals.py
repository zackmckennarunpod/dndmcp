"""evals.py -- side-by-side model comparison harness for the DM chat/tool-calling loop.

Formalizes the ad-hoc scenario testing done live 2026-07-04 comparing Qwen2.5-7B (the current
dnd-dm-vllm) against Qwen2.5-14B (a dormant dnd-dm-vllm-14b-test, min workers=0) on the exact
failure modes observed in real gameplay that session: dropped tool calls, echoed narration,
movement/combat not resolving. Not a generic benchmark -- every scenario below is grounded in
something that actually broke live, so a new model candidate gets checked against real,
previously-observed failures, not synthetic cases.

Run via run_eval() (async, real GPU calls against real endpoints -- costs real money/time and
can cold-start a scaled-to-zero endpoint, so this is only ever triggered explicitly from
web.py's POST /evals/run, never on a page load). Results persist to
DNDMCP_STATE_DIR/evals_last_run.json so GET /evals can render the last run without re-running
it every time someone looks at the page.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import dm_loop, worldgen

logger = logging.getLogger(__name__)


@dataclass
class ModelConfig:
    label: str          # e.g. "Qwen2.5-7B (live)"
    endpoint_id: str
    model: str


@dataclass
class Scenario:
    label: str
    category: str        # move | attack | search | item | uncertain | dialogue | rest | no_action
    state_line: str
    action: str
    expects_tool: bool = True
    expected_tools: tuple[str, ...] = field(default_factory=tuple)   # empty = any tool is fine


SCENARIOS: list[Scenario] = [
    Scenario("move: through a named exit", "move",
             "[SERVER STATE] Playing Thorne the Fighter, HP 10/10, currently in the Grim Forge. "
             "Exits from here: the warped iron door, the spiral stair.",
             "I go through the warped iron door.", expected_tools=("move",)),
    Scenario("move: vaguer phrasing", "move",
             "[SERVER STATE] Playing Thorne the Fighter, HP 10/10, currently in the Grim Forge. "
             "Exits from here: the warped iron door, the spiral stair.",
             "I head up the spiral stair.", expected_tools=("move",)),
    Scenario("attack: monster present", "attack",
             "[SERVER STATE] Playing Thorne the Fighter, HP 8/10, currently in the Desiccated Den. "
             "A live monster is in this room right now: Eve Reaper (18/18 HP) -- call attack if "
             "the player's action is aggressive toward it.",
             "I charge forward and swing my warhammer at the beast's head.", expected_tools=("attack",)),
    Scenario("attack: vaguer phrasing", "attack",
             "[SERVER STATE] Playing Thorne the Fighter, HP 8/10, currently in the Desiccated Den. "
             "A live monster is in this room right now: Eve Reaper (10/18 HP).",
             "I strike again with everything I've got.", expected_tools=("attack",)),
    Scenario("search/sense surroundings", "search",
             "[SERVER STATE] Playing Thorne the Fighter, HP 10/10, currently in the Corpse Archive.",
             "I search the room for anything useful, listening for any sound.",
             expected_tools=("sense_surroundings", "look")),
    Scenario("pick up a named item", "item",
             "[SERVER STATE] Playing Thorne the Fighter, HP 10/10, currently in the Rotting "
             "Reception. A crumpled report sits on the desk.",
             "I grab the crumpled report from the desk.", expected_tools=("pick_up_item",)),
    Scenario("uncertain/risky action (should roll dice)", "uncertain",
             "[SERVER STATE] Playing Thorne the Rogue, HP 9/9, currently in a narrow corridor. "
             "A sleeping guard is slumped against the wall.",
             "I try to sneak past the sleeping guard without waking him.",
             expected_tools=("roll_dice",)),
    Scenario("look around", "search",
             "[SERVER STATE] Playing Thorne the Fighter, HP 10/10, currently in the Lamenting Lobby.",
             "I look around to get my bearings.", expected_tools=("look",)),
    Scenario("factual question (HP)", "no_action",
             "[SERVER STATE] Playing Thorne the Fighter, HP 6/10, currently in the Grim Forge.",
             "How much HP do I have left?", expected_tools=("character_sheet",)),
    Scenario("banter, no real action", "no_action",
             "[SERVER STATE] Playing Thorne the Fighter, HP 10/10, currently in the Grim Forge.",
             "Ha, this place gives me the creeps.", expects_tool=False),
    Scenario("talk to an NPC", "dialogue",
             "[SERVER STATE] Playing Thorne the Fighter, HP 10/10, currently in the Lamenting "
             "Lobby. Dana Kowalski, a dead-eyed receptionist, is here.",
             "I ask Dana what happened here.", expected_tools=("talk_to",)),
    Scenario("rest to recover", "rest",
             "[SERVER STATE] Playing Thorne the Fighter, HP 4/10, currently in a quiet alcove. "
             "No monsters nearby.",
             "I sit down and rest to recover.", expected_tools=("heal",)),
]


@dataclass
class RoomGenScenario:
    """A room-generation coherence check -- unlike SCENARIOS above, there's no automated
    pass/fail here (architectural/thematic coherence is a judgment call, not a clean grade),
    so this exists purely to produce comparable raw examples for a human to eyeball on the
    /evals page. `is_main` MUST match the scenario's own premise -- confirmed live
    (2026-07-04): passing is_main=True for a custom-premise scenario forcibly injects
    setting.GEN_BRIEF's fixed "Sundered Weave" lore (ruins, dead automata, "ended in ash")
    regardless of the premise text, contaminating the output with a mismatched setting. Every
    scenario below has is_main deliberately set to match what it's actually testing."""
    label: str
    theme: str
    premise: str
    is_main: bool
    entry_from: str
    exits: list[str]
    nearby: list[dict]
    recent_events: list[str]
    existing_names: list[str]
    entry_room: tuple[str, str]


ROOM_GEN_SCENARIOS: list[RoomGenScenario] = [
    RoomGenScenario(
        "custom world: manor mystery (is_main=False, NEUTRAL_BRIEF)",
        theme="gothic manor mystery",
        premise="A vanished aristocrat's manor, frozen since the night of the disappearance.",
        is_main=False,
        entry_from="the narrow archway", exits=["north", "east"],
        nearby=[
            {"name": "the Grand Foyer", "kind": "foyer",
             "exits": ["a tall oak door -> Reception Corridor"], "contents": []},
            {"name": "Reception Corridor", "kind": "corridor",
             "exits": ["a tall oak door -> the Grand Foyer"], "contents": ["a dusty ledger"]},
        ],
        recent_events=["Someone searched Reception Corridor and found a ledger."],
        existing_names=["the Grand Foyer", "Reception Corridor"],
        entry_room=("Reception Corridor", "corridor"),
    ),
    RoomGenScenario(
        "main world: Sundered Weave dungeon (is_main=True, GEN_BRIEF)",
        theme="dark-fantasy dungeon crawl", premise="", is_main=True,
        entry_from="the collapsed archway", exits=["north", "down"],
        nearby=[
            {"name": "the Humming Sanctum", "kind": "sanctum",
             "exits": ["a corroded blast door -> the Vault Antechamber"], "contents": []},
            {"name": "the Vault Antechamber", "kind": "antechamber",
             "exits": ["a corroded blast door -> the Humming Sanctum"],
             "contents": ["a corrupted glyph-plate"]},
        ],
        recent_events=["Someone examined a corrupted glyph-plate in the Vault Antechamber."],
        existing_names=["the Humming Sanctum", "the Vault Antechamber"],
        entry_room=("the Vault Antechamber", "antechamber"),
    ),
    RoomGenScenario(
        "custom world: space-salvage (is_main=False, NEUTRAL_BRIEF)",
        theme="deep-space salvage horror",
        premise="A derelict mining vessel, life support failing, crew missing for three years.",
        is_main=False,
        entry_from="the jammed airlock", exits=["forward", "aft"],
        nearby=[
            {"name": "the Cargo Bay", "kind": "cargo bay",
             "exits": ["a warped bulkhead -> Engineering Access"], "contents": ["a flickering data slate"]},
            {"name": "Engineering Access", "kind": "corridor",
             "exits": ["a warped bulkhead -> the Cargo Bay"], "contents": []},
        ],
        recent_events=["Someone picked up a flickering data slate in the Cargo Bay."],
        existing_names=["the Cargo Bay", "Engineering Access"],
        entry_room=("Engineering Access", "corridor"),
    ),
]


def _api_key() -> str:
    if os.environ.get("RUNPOD_API_KEY"):
        return os.environ["RUNPOD_API_KEY"]
    key = subprocess.run(
        ["security", "find-generic-password", "-s", "runpod-api-key-prod", "-w"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    os.environ["RUNPOD_API_KEY"] = key
    return key


def _call_sync(endpoint_id: str, model: str, state_line: str, action: str,
              timeout: float = 260.0) -> dict:
    """One real chat-completions call using the SAME system prompt + tool schema the live DM
    uses (dm_loop.SYSTEM_PROMPT/TOOLS) -- an eval that grades against a different prompt than
    production actually runs would tell us nothing real about production behavior. timeout is
    generous enough to absorb a cold start from a scaled-to-zero endpoint."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": dm_loop.SYSTEM_PROMPT},
            {"role": "system", "content": state_line},
            {"role": "user", "content": action},
        ],
        "tools": dm_loop.TOOLS, "tool_choice": "auto",
        "max_tokens": 300, "temperature": dm_loop.TEMPERATURE,
    }
    url = f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    message = data["choices"][0]["message"]
    return {"elapsed_s": round(time.time() - t0, 1),
            "tool_calls": [tc["function"]["name"] for tc in (message.get("tool_calls") or [])],
            "tool_args": [tc["function"]["arguments"] for tc in (message.get("tool_calls") or [])],
            "content": (message.get("content") or "").strip()}


def _grade(scenario: Scenario, result: dict) -> bool:
    called = bool(result["tool_calls"])
    if not scenario.expects_tool:
        return not called
    if not called:
        return False
    if not scenario.expected_tools:
        return True
    return any(t in scenario.expected_tools for t in result["tool_calls"])


def _call_room_gen_sync(endpoint_id: str, model: str, scenario: RoomGenScenario,
                        timeout: float = 260.0) -> dict:
    """Builds the EXACT same prompt worldgen.generate_room_content would (via the real
    _room_messages, not a hand-rolled copy) and sends it straight to `endpoint_id` -- no
    automated grade, just the raw model output for the /evals page to show side by side."""
    messages = worldgen._room_messages(  # noqa: SLF001
        scenario.theme, scenario.entry_from, scenario.exits, scenario.nearby,
        scenario.recent_events, scenario.premise, scenario.existing_names,
        entry_room=scenario.entry_room, is_main=scenario.is_main)
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
        result["parsed"] = json.loads(content)
    except json.JSONDecodeError:
        result["parsed"] = None
    return result


async def run_eval(configs: list[ModelConfig]) -> dict:
    """Run every SCENARIO + every ROOM_GEN_SCENARIO against every ModelConfig, sequentially
    (shared Runpod account -- no reason to hammer multiple endpoints at once for a demo eval).
    Persists to DNDMCP_STATE_DIR/evals_last_run.json on completion; see load_last_run()."""
    loop = asyncio.get_running_loop()
    run: dict = {"started_at": time.time(), "configs": [c.label for c in configs],
                "scenarios": [], "room_gen": []}
    for scenario in SCENARIOS:
        row: dict = {"label": scenario.label, "category": scenario.category,
                    "state_line": scenario.state_line, "action": scenario.action, "results": {}}
        for cfg in configs:
            try:
                result = await loop.run_in_executor(
                    None, _call_sync, cfg.endpoint_id, cfg.model,
                    scenario.state_line, scenario.action)
                result["correct"] = _grade(scenario, result)
            except Exception as exc:
                logger.exception("evals.run_eval: %s / %s failed", scenario.label, cfg.label)
                result = {"error": str(exc), "correct": False}
            row["results"][cfg.label] = result
        run["scenarios"].append(row)
        logger.info("evals.run_eval: %s -- %s", scenario.label,
                   {k: v.get("correct") for k, v in row["results"].items()})
    for rg_scenario in ROOM_GEN_SCENARIOS:
        row = {"label": rg_scenario.label, "premise": rg_scenario.premise,
              "is_main": rg_scenario.is_main, "results": {}}
        for cfg in configs:
            try:
                result = await loop.run_in_executor(
                    None, _call_room_gen_sync, cfg.endpoint_id, cfg.model, rg_scenario)
            except Exception as exc:
                logger.exception("evals.run_eval: room_gen %s / %s failed",
                                rg_scenario.label, cfg.label)
                result = {"error": str(exc)}
            row["results"][cfg.label] = result
        run["room_gen"].append(row)
        logger.info("evals.run_eval: room_gen %s -- done", rg_scenario.label)
    run["finished_at"] = time.time()
    _save_run(run)
    return run


def _results_path() -> Path:
    state_dir = Path(os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp")))
    return state_dir / "evals_last_run.json"


def _save_run(run: dict) -> None:
    try:
        _results_path().write_text(json.dumps(run, indent=2))
    except Exception:
        logger.exception("evals._save_run: failed to persist")


def load_last_run() -> dict | None:
    p = _results_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        logger.exception("evals.load_last_run: failed to read")
        return None
