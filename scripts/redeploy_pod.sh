#!/usr/bin/env bash
# Redeploy DNDMCP on the live pod: git pull latest main, restart the app process.
# Auto-resolves the pod's current SSH host:port (changes across restarts) via pod_ssh.sh.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/pod_ssh.sh" run '
cd /app
echo "--- git pull ---"
git pull origin main
echo "--- installing any new deps ---"
python3.11 -m pip install --no-cache-dir -q -r dndmcp/requirements.txt
echo "--- restarting app ---"
pkill -f "dndmcp.app" || true
sleep 1
mkdir -p /data
DNDMCP_STATE_DIR=/data DNDMCP_TRANSPORT=http PORT=8000 GUI_PORT=8002 \
  nohup python3.11 -m dndmcp.app > /tmp/dndmcp.log 2>&1 </dev/null & disown
sleep 3
echo "--- status ---"
pgrep -af "dndmcp.app" || echo "FAILED TO START"
tail -20 /tmp/dndmcp.log
'
