#!/usr/bin/env bash
# Tear down a self-deployed dndmcp instance (see deploy_own_pod.sh) — terminates the pod and,
# unless --keep-volume is passed, deletes its network volume too (volume storage bills even
# while the pod is stopped, so an orphaned volume after you're done costs money for nothing).
#
# Usage: scripts/destroy_own_pod.sh <pod-id> --yes [--keep-volume]
set -euo pipefail

# The shared "main" world everyone else plays in — this script must NEVER be able to touch
# it, no matter what flags get passed. Hardcoded on purpose, not configurable.
LIVE_POD_ID="ldghdgi0xxn6jj"

POD_ID="${1:-}"
YES=false
KEEP_VOLUME=false
for arg in "$@"; do
  case "$arg" in
    --yes) YES=true ;;
    --keep-volume) KEEP_VOLUME=true ;;
  esac
done

if [ -z "$POD_ID" ] || [ "$POD_ID" = "--yes" ] || [ "$POD_ID" = "--keep-volume" ]; then
  echo "Usage: $0 <pod-id> --yes [--keep-volume]" >&2
  exit 1
fi
if [ "$POD_ID" = "$LIVE_POD_ID" ]; then
  echo "Refusing: $LIVE_POD_ID is the live shared world, not a self-deployed instance. This script will never touch it." >&2
  exit 1
fi
if [ "$YES" != true ]; then
  msg="This permanently terminates pod $POD_ID"
  [ "$KEEP_VOLUME" = false ] && msg="$msg and deletes its network volume"
  echo "$msg. Re-run with --yes to confirm." >&2
  exit 1
fi

API_KEY="${RUNPOD_API_KEY:-}"
if [ -z "$API_KEY" ] && command -v security >/dev/null 2>&1; then
  API_KEY="$(security find-generic-password -s runpod-api-key-prod -w 2>/dev/null || true)"
fi
if [ -z "$API_KEY" ]; then
  echo "Set RUNPOD_API_KEY (Runpod console -> Settings -> API Keys) and re-run." >&2
  exit 1
fi

gql() {
  curl -s -X POST https://api.runpod.io/graphql \
    -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
    -d "$1"
}

VOLUME_ID=""
if [ "$KEEP_VOLUME" = false ]; then
  echo "== looking up pod $POD_ID's network volume =="
  LOOKUP_BODY="$(python3 -c "
import json
print(json.dumps({
    'query': 'query Lookup(\$id: String!) { pod(input: {podId: \$id}) { networkVolumeId } }',
    'variables': {'id': '$POD_ID'},
}))
")"
  VOLUME_ID="$(gql "$LOOKUP_BODY" | python3 -c "
import json, sys
d = json.load(sys.stdin)
p = (d.get('data') or {}).get('pod')
print((p or {}).get('networkVolumeId') or '')
")"
fi

echo "== terminating pod $POD_ID =="
TERMINATE_BODY="$(python3 -c "
import json
print(json.dumps({
    'query': 'mutation Terminate(\$id: String!) { podTerminate(input: {podId: \$id}) }',
    'variables': {'id': '$POD_ID'},
}))
")"
gql "$TERMINATE_BODY"
echo

if [ -n "$VOLUME_ID" ]; then
  echo "== deleting network volume $VOLUME_ID =="
  DELETE_BODY="$(python3 -c "
import json
print(json.dumps({
    'query': 'mutation Delete(\$id: String!) { deleteNetworkVolume(input: {id: \$id}) }',
    'variables': {'id': '$VOLUME_ID'},
}))
")"
  gql "$DELETE_BODY"
  echo
elif [ "$KEEP_VOLUME" = false ]; then
  echo "(no network volume found on that pod — nothing else to clean up)"
fi

echo "done."
