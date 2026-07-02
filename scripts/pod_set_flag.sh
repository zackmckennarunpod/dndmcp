#!/usr/bin/env bash
# Toggle an admin safety flag on the live pod WITHOUT restarting the app — the change is
# picked up on the very next request (see dndmcp/admin_flags.py). This is the fast kill
# switch: for anything that needs a real restart, use redeploy_pod.sh instead.
#
# Usage: scripts/pod_set_flag.sh <flag-name> <0|1|integer>
#   scripts/pod_set_flag.sh flash_art 0     # kill switch: turn OFF art gen right now
#   scripts/pod_set_flag.sh flash_art 1     # turn it back on (or clear the override: omit
#                                            # the flag entirely to fall back to the env var)
#   scripts/pod_set_flag.sh bots_count 3    # non-boolean flags (admin_flags.get_int) take a
#                                            # plain integer instead of 0/1
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAME="${1:?usage: pod_set_flag.sh <flag-name> <0|1|integer>}"
VALUE="${2:?usage: pod_set_flag.sh <flag-name> <0|1|integer>}"
# 0/1 stay booleans (admin_flags.enabled's contract, unchanged for flash_art etc); anything
# else must be a bare integer (admin_flags.get_int, e.g. bots_count) — never arbitrary JSON,
# so this can't be used to inject something admin_flags.py doesn't expect.
case "$VALUE" in
  0) JSON_VALUE=false ;;
  1) JSON_VALUE=true ;;
  ''|*[!0-9]*) echo "value must be 0, 1, or a positive integer, got: $VALUE" >&2; exit 1 ;;
  *) JSON_VALUE="$VALUE" ;;
esac

"$SCRIPT_DIR/pod_ssh.sh" run "
python3.11 -c \"
import json, pathlib
p = pathlib.Path('/data/admin_flags.json')
data = json.loads(p.read_text()) if p.exists() else {}
data['$NAME'] = $JSON_VALUE
p.write_text(json.dumps(data, indent=2))
print('admin_flags.json now:', json.dumps(data))
\"
"
