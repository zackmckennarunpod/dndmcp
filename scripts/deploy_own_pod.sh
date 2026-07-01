#!/usr/bin/env bash
# Stand up your OWN independent dndmcp instance — a fresh Runpod CPU pod + network volume,
# running the published image (zackmckennarunpod/dndmcp, public on Docker Hub). Completely
# separate from the shared "main" world at ldghdgi0xxn6jj — your own campaign, your own DB.
#
# Costs real money while running (small CPU pod + volume storage — check current Runpod
# pricing for cpu3c). scripts/destroy_own_pod.sh tears it down when you're done.
#
# Requires: RUNPOD_API_KEY (Runpod console -> Settings -> API Keys).
#
# Usage:
#   RUNPOD_API_KEY=... scripts/deploy_own_pod.sh [name] [datacenter]
#   name        defaults to "dndmcp-<random 6 hex>"
#   datacenter  defaults to EU-RO-1 (where the reference deployment runs; override if that
#               datacenter doesn't have cpu3c stock when you run this)
set -euo pipefail

IMAGE="zackmckennarunpod/dndmcp:latest"
DC="${2:-EU-RO-1}"
NAME="${1:-dndmcp-$(python3 -c 'import secrets; print(secrets.token_hex(3))')}"
VOLUME_GB="${DNDMCP_VOLUME_GB:-10}"
# cpu3c-2-4: 2 vCPU / 4GB RAM, Compute-Optimized flavor — matches the resource profile the
# reference "main" world actually runs on (verified via the live pod's own vcpuCount/
# memoryInGb). Not yet round-tripped through a real deploy call as of writing this script —
# if `cpu3c-2-4` gets rejected, list valid ids with the cpuFlavors GraphQL query
# (id/minVcpu/maxVcpu/ramMultiplier) and build "<flavorId>-<vcpu>-<ramGB>" from that.
INSTANCE_ID="${DNDMCP_INSTANCE_ID:-cpu3c-2-4}"

API_KEY="${RUNPOD_API_KEY:-}"
if [ -z "$API_KEY" ] && command -v security >/dev/null 2>&1; then
  API_KEY="$(security find-generic-password -s runpod-api-key-prod -w 2>/dev/null || true)"
fi
if [ -z "$API_KEY" ]; then
  echo "Set RUNPOD_API_KEY (Runpod console -> Settings -> API Keys) and re-run." >&2
  exit 1
fi

gql() {
  # $1 = JSON body (already fully formed, incl. "query"/"variables"). Never interpolates
  # user-controlled strings (NAME, DC) directly into GraphQL query TEXT — those travel as
  # variables instead, built via python's json.dumps so quoting is never our problem.
  curl -s -X POST https://api.runpod.io/graphql \
    -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
    -d "$1"
}

echo "== creating a ${VOLUME_GB}GB network volume in $DC =="
VOLUME_BODY="$(python3 -c "
import json
print(json.dumps({
    'query': 'mutation Create(\$input: CreateNetworkVolumeInput!) { createNetworkVolume(input: \$input) { id } }',
    'variables': {'input': {'name': '$NAME-data', 'size': $VOLUME_GB, 'dataCenterId': '$DC'}},
}))
")"
VOLUME_RESP="$(gql "$VOLUME_BODY")"
VOLUME_ID="$(echo "$VOLUME_RESP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d.get('errors'):
    print('ERROR creating volume:', d['errors'], file=sys.stderr); sys.exit(1)
print(d['data']['createNetworkVolume']['id'])
")"
echo "volume: $VOLUME_ID"

echo
echo "== deploying pod \"$NAME\" ($INSTANCE_ID, image $IMAGE) =="
POD_BODY="$(python3 -c "
import json
variables = {
    'input': {
        'name': '$NAME',
        'imageName': '$IMAGE',
        'instanceId': '$INSTANCE_ID',
        'cloudType': 'SECURE',
        'containerDiskInGb': 15,
        'networkVolumeId': '$VOLUME_ID',
        'volumeMountPath': '/data',
        'dataCenterIds': ['$DC'],
        'ports': '22/tcp,8000/http,8002/http',
        # Same four vars the Dockerfile also sets — explicit here too because pod-level env
        # is what actually reaches the container's init process (verified against the live
        # reference pod's own config; Docker ENV alone would be enough for `docker run`
        # but Runpod's own env mechanism is the one guaranteed to apply here).
        'env': [
            {'key': 'DNDMCP_STATE_DIR', 'value': '/data'},
            {'key': 'DNDMCP_TRANSPORT', 'value': 'http'},
            {'key': 'PORT', 'value': '8000'},
            {'key': 'GUI_PORT', 'value': '8002'},
        ],
    }
}
print(json.dumps({
    'query': 'mutation Deploy(\$input: deployCpuPodInput!) { deployCpuPod(input: \$input) { id } }',
    'variables': variables,
}))
")"
POD_RESP="$(gql "$POD_BODY")"
POD_ID="$(echo "$POD_RESP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d.get('errors'):
    print('ERROR deploying pod:', d['errors'], file=sys.stderr); sys.exit(1)
print(d['data']['deployCpuPod']['id'])
")"

echo
echo "== deployed =="
echo "pod id:  $POD_ID"
echo "volume:  $VOLUME_ID (${VOLUME_GB}GB, $DC)"
echo "MCP:     https://${POD_ID}-8000.proxy.runpod.net/mcp"
echo "GUI:     https://${POD_ID}-8002.proxy.runpod.net"
echo
echo "Give it ~30-60s to boot (pulling the image), then:"
echo "  connect:   scripts/install_claude_code.sh $POD_ID"
echo "  tear down: scripts/destroy_own_pod.sh $POD_ID --yes"
