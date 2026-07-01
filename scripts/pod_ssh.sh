#!/usr/bin/env bash
# Shared helper: resolve the live pod's current direct-TCP SSH host:port and either print it
# or exec an SSH command through it. Host/port CHANGE across pod stop/start (learned the hard
# way) so nothing should hardcode them — always re-resolve from the Runpod API.
#
# Usage:
#   scripts/pod_ssh.sh resolve                  -> prints "HOST PORT"
#   scripts/pod_ssh.sh run "<remote command>"   -> ssh's in and runs it
#   scripts/pod_ssh.sh copy <local> <remote>    -> scp-style single file push (via ssh cat)
set -euo pipefail

POD_ID="${DNDMCP_POD_ID:-ldghdgi0xxn6jj}"
KEY="${DNDMCP_SSH_KEY:-$HOME/.ssh/id_ed25519}"

resolve() {
  local api_key
  api_key=$(security find-generic-password -s runpod-api-key-prod -w)
  curl -s -X POST https://api.runpod.io/graphql \
    -H "Authorization: Bearer $api_key" -H "Content-Type: application/json" \
    -d "{\"query\":\"query { pod(input:{podId:\\\"${POD_ID}\\\"}) { runtime { ports { ip privatePort publicPort type } } } }\"}" \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
rt = d['data']['pod']['runtime']
if not rt:
    print('', ''); sys.exit(1)
for p in rt['ports'] or []:
    if p['privatePort'] == 22 and p['type'] == 'tcp':
        print(p['ip'], p['publicPort']); sys.exit(0)
print('', ''); sys.exit(1)
"
}

case "${1:-resolve}" in
  resolve)
    resolve
    ;;
  run)
    read -r HOST PORT <<< "$(resolve)"
    [ -n "$HOST" ] || { echo "Could not resolve pod SSH endpoint (is it running?)" >&2; exit 1; }
    ssh -o StrictHostKeyChecking=accept-new -i "$KEY" -p "$PORT" root@"$HOST" "$2"
    ;;
  *)
    echo "usage: $0 {resolve|run <cmd>}" >&2; exit 1
    ;;
esac
