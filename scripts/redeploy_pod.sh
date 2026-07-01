#!/usr/bin/env bash
# Redeploy DNDMCP on the live pod: git pull latest main, restart the app process.
# Usage: scripts/redeploy_pod.sh <ssh_host> <ssh_port>
# (host/port come from the pod's "SSH over exposed TCP" panel in the Runpod console —
# they can change if the pod restarts, so this isn't hardcoded.)
set -euo pipefail

HOST="${1:?usage: redeploy_pod.sh <host> <port>}"
PORT="${2:?usage: redeploy_pod.sh <host> <port>}"
KEY="${DNDMCP_SSH_KEY:-$HOME/.ssh/id_ed25519}"

ssh -o StrictHostKeyChecking=accept-new -i "$KEY" -p "$PORT" root@"$HOST" bash -s <<'REMOTE'
set -e
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
REMOTE
