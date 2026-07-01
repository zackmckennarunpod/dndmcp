#!/usr/bin/env bash
# Force-regenerate every room's art in a campaign, using whatever the CURRENTLY deployed
# code does (current prompt suffix, current palette). Use this to re-style a world's art
# after an art-pipeline change lands — art otherwise only ever generates once per room, on
# creation, so there's no other way to bring old rooms up to a new style.
#
# Usage: scripts/regen_art.sh [campaign_id]   (default: main)
#
# Costs one real Flash GPU call per room with existing art in that campaign — not free,
# don't run this against every world casually.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CAMPAIGN="${1:-main}"
RUNPOD_API_KEY="$(security find-generic-password -s runpod-api-key-prod -w)"

"$SCRIPT_DIR/pod_ssh.sh" run "
cd /app
echo '--- syncing latest code (no restart — the running server is untouched) ---'
git pull origin main
echo \"--- regenerating art for campaign '$CAMPAIGN' ---\"
DNDMCP_STATE_DIR=/data DND_FLASH_ART=1 RUNPOD_API_KEY='$RUNPOD_API_KEY' \
  python3.11 scripts/regen_art_remote.py '$CAMPAIGN'
"
