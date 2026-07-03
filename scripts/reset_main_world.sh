#!/usr/bin/env bash
# Reset ONLY the shared "main" world -- every other campaign (custom worlds players/agents
# started via start_adventure) is left completely untouched. The counterpart to reset_world.sh
# (which wipes the ENTIRE campaign.db, every world) for when you specifically want a fresh
# main demo without nuking everyone else's custom world too.
#
# Uses state.py's own World.delete_campaign(), same mechanism delete_world.sh/the delete_world
# MCP tool use -- this script is the one deliberate exception to "never target main" (that
# guard exists in delete_world.sh/the MCP tool because routinely wiping the shared world is
# almost never what you want; resetting main FOR a fresh demo is the one case it's exactly
# what's being asked for, so this script exists as its own explicit, separately-named action
# rather than a flag on the general-purpose one).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" != "--yes" ]; then
  echo "This deletes every room/character/log/entity in the shared 'main' world (every OTHER" >&2
  echo "campaign is left untouched). Re-run with --yes to confirm." >&2
  exit 1
fi

"$SCRIPT_DIR/pod_ssh.sh" run "
cd /app && python3.11 -c \"
import os
os.environ.setdefault('DNDMCP_STATE_DIR', '/data')
from dndmcp.state import World, MAIN_CAMPAIGN_ID
w = World()
camp = w.campaign(MAIN_CAMPAIGN_ID)
if not camp:
    print('main already has no campaign row -- nothing to delete.')
else:
    print(f'Deleting main (theme: {camp.theme!r})...')
    w.delete_campaign(MAIN_CAMPAIGN_ID)
    print('Done -- main will be recreated fresh on the next start_adventure, same as any new world.')
\"
"
