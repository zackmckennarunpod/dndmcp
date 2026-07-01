#!/usr/bin/env bash
# Quick health check: is the app running, are both ports up, what commit is deployed.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/pod_ssh.sh" run '
cd /app
echo "commit: $(git log --oneline -1)"
echo "process: $(pgrep -af "dndmcp.app" || echo NOT RUNNING)"
curl -s -o /dev/null -w "GUI  (8002) local: %{http_code}\n" http://localhost:8002/ || true
curl -s -o /dev/null -w "MCP  (8000) local: %{http_code}\n" http://localhost:8000/mcp || true
'
