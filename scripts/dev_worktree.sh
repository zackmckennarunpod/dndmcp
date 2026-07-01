#!/usr/bin/env bash
# Run an isolated dndmcp GUI+MCP server for whichever git worktree you're currently in.
#
# Safe for multiple agents to run concurrently, one per worktree: ports are auto-picked free
# (no manual coordination, no collisions) and the state dir is unique per worktree, so nobody
# shares a DB and nobody touches ~/.dndmcp (the shared local dev world) or the live pod.
#
# Usage: cd into your worktree, then run this script. Ctrl-C stops it.
set -euo pipefail

MAIN_REPO="$(dirname "$(git rev-parse --git-common-dir)")"
WORKTREE_ROOT="$(git rev-parse --show-toplevel)"
WORKTREE_NAME="$(basename "$WORKTREE_ROOT")"

if [ ! -x "$MAIN_REPO/.venv/bin/python3" ]; then
  echo "error: no venv at $MAIN_REPO/.venv — set one up there first (see dndmcp/SETUP.md step 0)." >&2
  exit 1
fi

# Grab two free ports in one interpreter call so they can't race each other to the same port.
read -r PORT GUI_PORT < <("$MAIN_REPO/.venv/bin/python3" -c '
import socket
socks = [socket.socket() for _ in range(2)]
for s in socks:
    s.bind(("", 0))
ports = [s.getsockname()[1] for s in socks]
for s in socks:
    s.close()
print(*ports)
')

export PORT GUI_PORT
export DNDMCP_STATE_DIR="${DNDMCP_STATE_DIR:-$HOME/.dndmcp_worktrees/$WORKTREE_NAME}"
export DNDMCP_TRANSPORT=http
export PYTHONPATH="$WORKTREE_ROOT"
mkdir -p "$DNDMCP_STATE_DIR"

cat <<EOF
worktree:   $WORKTREE_NAME  ($WORKTREE_ROOT)
state dir:  $DNDMCP_STATE_DIR   (fresh, isolated — not the shared dev world, not the live pod)
MCP  PORT:  $PORT
GUI  PORT:  $GUI_PORT  ->  http://localhost:$GUI_PORT

Running dndmcp code from THIS worktree, using the venv/deps from $MAIN_REPO/.venv
(if this branch changed dndmcp/requirements.txt, ctrl-C and make a venv inside this
worktree instead: python3 -m venv .venv && .venv/bin/pip install -r dndmcp/requirements.txt)
EOF

exec "$MAIN_REPO/.venv/bin/python3" -m dndmcp.app
