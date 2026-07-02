#!/usr/bin/env bash
# Live status of every running bot slot (state, current character, last action/narration,
# when it last updated) — written by dndmcp/bot_player.py on every state transition, so this
# works without inspecting the running process directly. Use this to check whether a bot
# looks stuck before deciding to unstick it (see README below).
#
# To force-kill a stuck bot: shrink bots_count to 0 then back up — bot_player.py's
# supervisor actually cancels the task (not just a cooperative flag), so this works even for
# a bot hung mid-turn, not just one that's cleanly idling between turns.
#   scripts/pod_set_flag.sh bots_count 0
#   scripts/pod_set_flag.sh bots_count <N>
#
# Usage: scripts/pod_bot_status.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/pod_ssh.sh" run "
python3.11 -c \"
import json, pathlib, time
p = pathlib.Path('/data/bot_status.json')
if not p.exists():
    print('No bot_status.json yet — no bot has taken a turn since the last restart.')
else:
    data = json.loads(p.read_text())
    now = time.time()
    for slot, s in data.items():
        age = now - s.get('updated_at', now)
        print(f\\\"{slot}: {s.get('state','?')} — {s.get('character_name','(no character yet)')} \\\"
              f\\\"(player_id={s.get('player_id')}) — last update {age:.0f}s ago\\\")
        if s.get('last_action'):
            print(f\\\"  last action: {s['last_action']}\\\")
        if s.get('last_narration'):
            print(f\\\"  last narration: {s['last_narration']}\\\")
\"
"
