#!/usr/bin/env bash
# Scoped, admin-side world deletion — the counterpart to reset_world.sh's "wipe everything"
# for when you only want ONE junk/test campaign gone, not the whole shared DB.
#
# Uses state.py's own World.delete_campaign(), which the delete_world MCP tool already wraps
# for players (see server.py) — this script skips ONLY that tool's "sole remaining player"
# guard (an admin acting directly on the DB doesn't need to be a player in the world to clean
# it up), but keeps the same non-negotiable "never main" guard, enforced here too.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CAMPAIGN_ID="${1:-}"
if [ -z "$CAMPAIGN_ID" ] || [ "${2:-}" != "--yes" ]; then
  echo "Usage: $0 <campaign_id> --yes" >&2
  echo "Deletes ONE campaign's rooms/characters/log/entities/quests/edges. Never touches main." >&2
  exit 1
fi
if [ "$CAMPAIGN_ID" = "main" ]; then
  echo "Refusing: never deletes the shared 'main' world." >&2
  exit 1
fi

"$SCRIPT_DIR/pod_ssh.sh" run "
cd /app && python3.11 -c \"
import os
os.environ.setdefault('DNDMCP_STATE_DIR', '/data')
from dndmcp.state import World, MAIN_CAMPAIGN_ID
campaign_id = '$CAMPAIGN_ID'
assert campaign_id != MAIN_CAMPAIGN_ID, 'refusing to delete main'
w = World()
camp = w.campaign(campaign_id)
if not camp:
    print(f'No such campaign: {campaign_id!r} (already gone, or never existed)')
else:
    print(f'Deleting campaign {campaign_id!r} ({camp.theme!r})...')
    w.delete_campaign(campaign_id)
    print('Done.')
\"
"
