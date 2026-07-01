#!/usr/bin/env bash
# Install DNDMCP into Claude Code, pointed at the live shared-world pod.
# Run once: connects your Claude Code to the same persistent world everyone else is in.
#
# Usage:
#   scripts/install_claude_code.sh                       # uses the default live pod
#   scripts/install_claude_code.sh <pod-id>               # a different pod
#   scripts/install_claude_code.sh --local                # local stdio dev server instead
set -euo pipefail

if [ "${1:-}" = "--local" ]; then
  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  claude mcp add dndmcp -s user \
    -e PYTHONPATH="$REPO_DIR" \
    -e DNDMCP_STATE_DIR="$HOME/.dndmcp_dev" \
    -- "$REPO_DIR/.venv/bin/python" -m dndmcp.server
  echo "Installed dndmcp (local stdio) — restart Claude Code to connect."
  exit 0
fi

POD_ID="${1:-ldghdgi0xxn6jj}"
URL="https://${POD_ID}-8000.proxy.runpod.net/mcp"

claude mcp add --transport http dndmcp -s user "$URL"
echo "Installed dndmcp -> $URL (restart Claude Code / run /mcp to connect)"
echo "You're joining the shared world — say 'start an adventure' to begin."
