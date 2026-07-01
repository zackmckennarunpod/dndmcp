#!/usr/bin/env bash
# DESTRUCTIVE: wipes the shared campaign DB on the live pod and restarts the app fresh.
# Every connected player's progress is lost. Requires --yes to actually run.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" != "--yes" ]; then
  echo "This wipes the LIVE shared world (every player's progress). Re-run with --yes to confirm." >&2
  exit 1
fi

"$SCRIPT_DIR/pod_ssh.sh" run '
pkill -f "dndmcp.app" || true
sleep 1
rm -f /data/campaign.db /data/tickets.db
cd /app
DNDMCP_STATE_DIR=/data DNDMCP_TRANSPORT=http PORT=8000 GUI_PORT=8002 \
  nohup python3.11 -m dndmcp.app > /tmp/dndmcp.log 2>&1 </dev/null & disown
sleep 3
pgrep -af "dndmcp.app" || echo "FAILED TO START"
'
