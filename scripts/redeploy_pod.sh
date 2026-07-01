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
# DND_LLM_ENDPOINT/DND_LLM_MODEL: ALL game generation (world-gen + browser DM) runs on the
# one 7B endpoint now — one warm worker instead of two, and 7B beats the 1.5B on JSON/tool
# compliance (fewer malformed-sample retries). The model value must match that endpoint's
# own loaded MODEL_NAME; flash_llm resolves the endpoint by name and never reconfigures it.
# DND_FLASH_ART=1: real per-room pixel art via Flash (flash_art.py/art.py) instead of the
# ASCII placeholder. This is just the default — admin_flags.py can override it live with no
# restart (scripts/pod_set_flag.sh flash_art 0/1), for backing it out fast if needed.
# Log to /data (the persistent network volume), APPENDING (>>) not truncating (>) — this bit
# us 3 times today: a redeploy between a room generating and someone asking "why didn't it
# get art" wiped the only evidence of what actually happened, every time. /data has 400+TB
# free; a few hundred KB/restart is nothing. /tmp/dndmcp.log is still written too (a symlink)
# so pod_logs.sh/reset_world.sh's existing /tmp path keeps working without editing them.
DNDMCP_STATE_DIR=/data DNDMCP_TRANSPORT=http PORT=8000 GUI_PORT=8002 DND_FLASH_LLM=1 DND_FLASH_ART=1 \
  DND_LLM_ENDPOINT=dnd-dm-vllm DND_LLM_MODEL='Qwen/Qwen2.5-7B-Instruct' RUNPOD_API_KEY='$RUNPOD_API_KEY' \
  setsid nohup python3.11 -m dndmcp.app >> /data/dndmcp.log 2>&1 < /dev/null &
ln -sf /data/dndmcp.log /tmp/dndmcp.log
sleep 3
echo '--- status ---'
# -fx here too (not plain -f): a substring match also catches the SSH wrapper's own bash -c
# invocation, whose command-line text includes this whole script INCLUDING the RUNPOD_API_KEY
# assignment above — printing that straight into whatever captured this script's stdout.
# Learned the hard way: leaked a live prod API key into a terminal transcript this way.
pgrep -afx 'python3.11 -m dndmcp.app' || echo 'FAILED TO START'
tail -20 /data/dndmcp.log
"
