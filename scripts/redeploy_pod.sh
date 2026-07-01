#!/usr/bin/env bash
# Redeploy DNDMCP on the live pod: git pull latest main, restart the app process.
# Auto-resolves the pod's current SSH host:port (changes across restarts) via pod_ssh.sh.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# DND_FLASH_LLM=1 is load-bearing, not optional: without it worldgen falls back to a ~7-name
# procedural room pool (visible as repeated room names within a few hops) instead of real
# vLLM generation. RUNPOD_API_KEY has to travel with it — flash_llm._api_key()'s fallback
# (macOS Keychain `security` command) doesn't exist on this Linux pod, so without the key
# here Flash silently no-ops back to procedural too, with no error surfaced.
RUNPOD_API_KEY="$(security find-generic-password -s runpod-api-key-prod -w)"

"$SCRIPT_DIR/pod_ssh.sh" run "
cd /app
echo '--- git pull ---'
git pull origin main
echo '--- installing any new deps ---'
python3.11 -m pip install --no-cache-dir -q -r dndmcp/requirements.txt
echo '--- restarting app ---'
# -fx (exact full-command match), NOT plain -f: the remote bash -c invocation running THIS
# script has 'dndmcp.app' in its own command-line text (this line, the launch line below,
# the pgrep below), so a substring match kills the shell running the redeploy itself —
# manifests as the ssh connection dying mid-script (exit 255). Learned the hard way.
pkill -fx 'python3.11 -m dndmcp.app' || true
sleep 1
mkdir -p /data
# setsid fully detaches into a new session so the SSH channel can close immediately —
# nohup+disown alone can still hold the channel open waiting on inherited fds, hanging
# the ssh command until it is killed (learned the hard way: showed up as exit 255).
DNDMCP_STATE_DIR=/data DNDMCP_TRANSPORT=http PORT=8000 GUI_PORT=8002 DND_FLASH_LLM=1 RUNPOD_API_KEY='$RUNPOD_API_KEY' \
  setsid nohup python3.11 -m dndmcp.app > /tmp/dndmcp.log 2>&1 < /dev/null &
sleep 3
echo '--- status ---'
pgrep -af 'dndmcp.app' || echo 'FAILED TO START'
tail -20 /tmp/dndmcp.log
"
