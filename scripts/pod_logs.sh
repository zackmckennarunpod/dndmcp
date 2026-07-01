#!/usr/bin/env bash
# Tail the app's log on the live pod. Usage: scripts/pod_logs.sh [n_lines]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
N="${1:-60}"
"$SCRIPT_DIR/pod_ssh.sh" run "tail -n $N /tmp/dndmcp.log"
