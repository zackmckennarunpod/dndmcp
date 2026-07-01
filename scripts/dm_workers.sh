#!/usr/bin/env bash
# Scale the game's one vLLM endpoint (dnd-dm-vllm — world-gen AND the browser DM) down to
# zero warm workers when nobody's playing, and back up to min 1 before play/demo/judging.
#
#   scripts/dm_workers.sh status   # current min/max + live worker/job counts
#   scripts/dm_workers.sh down     # workersMin=0 — scale-to-zero (~$0.69/hr saved)
#   scripts/dm_workers.sh up       # workersMin=1 + warm-up call, verifies it actually serves
#
# CAVEAT (hard-won, see dndmcp/flash_llm.py's docstring): min=0 has previously hit Runpod's
# THROTTLED worker state on cold reallocation — a request arrives, no worker materializes,
# gameplay stalls for minutes. That's exactly why `up` doesn't just flip the number: it fires
# a real completion and waits until one comes back. Run `up` BEFORE a demo, not during it.
# While scaled down, the game still works: world-gen falls back to procedural and the browser
# DM will just be slow/failing until a worker wakes — so treat `down` as "nobody is playing."
set -euo pipefail

ENDPOINT_NAME="dnd-dm-vllm"
MODEL="Qwen/Qwen2.5-7B-Instruct"
API_KEY="${RUNPOD_API_KEY:-$(security find-generic-password -s runpod-api-key-prod -w)}"

resolve_id() {
  curl -s -X POST https://api.runpod.io/graphql \
    -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
    -d '{"query":"query { myself { endpoints { id name } } }"}' \
    | python3 -c "import json,sys; print(next(e['id'] for e in json.load(sys.stdin)['data']['myself']['endpoints'] if e['name']=='$ENDPOINT_NAME'))"
}

EP_ID="$(resolve_id)"

set_min() {
  curl -s -X PATCH "https://rest.runpod.io/v1/endpoints/$EP_ID" \
    -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
    -d "{\"workersMin\": $1}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"workers now min={d['workersMin']} max={d['workersMax']}\")"
}

status() {
  curl -s "https://rest.runpod.io/v1/endpoints/$EP_ID" -H "Authorization: Bearer $API_KEY" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"{d['name']} ({d['id']}): min={d['workersMin']} max={d['workersMax']}\")"
  curl -s "https://api.runpod.ai/v2/$EP_ID/health" -H "Authorization: Bearer $API_KEY" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('workers:', d.get('workers'), '| jobs:', d.get('jobs'))"
}

warm() {
  echo "warming (cold start can take 90s+)..."
  for i in $(seq 1 12); do
    OUT=$(curl -s --max-time 60 -X POST "https://api.runpod.ai/v2/$EP_ID/openai/v1/chat/completions" \
      -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
      -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say ready.\"}],\"max_tokens\":4}" \
      | python3 -c "import json,sys
try: print(json.load(sys.stdin)['choices'][0]['message']['content'])
except Exception: print('')" )
    if [ -n "$OUT" ]; then echo "✓ worker serving: $OUT"; return 0; fi
    echo "  not up yet ($((i*20))s)..."; sleep 20
  done
  echo "✗ worker never answered — check THROTTLED state: python -m forge.diagnostics $EP_ID" >&2
  return 1
}

case "${1:-status}" in
  down) set_min 0 && echo "scaled to zero — run '$0 up' before anyone plays." ;;
  up)   set_min 1 && warm && status ;;
  status) status ;;
  *) echo "usage: $0 [status|down|up]" >&2; exit 1 ;;
esac
