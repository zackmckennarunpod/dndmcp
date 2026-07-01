#!/usr/bin/env bash
# Toggle an admin safety flag on the live pod WITHOUT restarting the app — the change is
# picked up on the very next request (see dndmcp/admin_flags.py). This is the fast kill
# switch: for anything that needs a real restart, use redeploy_pod.sh instead.
#
# Usage: scripts/pod_set_flag.sh <flag-name> <0|1>
#   scripts/pod_set_flag.sh flash_art 0     # kill switch: turn OFF art gen right now
#   scripts/pod_set_flag.sh flash_art 1     # turn it back on (or clear the override: omit
#                                            # the flag entirely to fall back to the env var)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAME="${1:?usage: pod_set_flag.sh <flag-name> <0|1>}"
VALUE="${2:?usage: pod_set_flag.sh <flag-name> <0|1>}"
case "$VALUE" in
  0) BOOL=false ;;
  1) BOOL=true ;;
  *) echo "value must be 0 or 1, got: $VALUE" >&2; exit 1 ;;
esac

"$SCRIPT_DIR/pod_ssh.sh" run "
python3.11 -c \"
import json, pathlib
p = pathlib.Path('/data/admin_flags.json')
data = json.loads(p.read_text()) if p.exists() else {}
data['$NAME'] = $BOOL
p.write_text(json.dumps(data, indent=2))
print('admin_flags.json now:', json.dumps(data))
\"
"
