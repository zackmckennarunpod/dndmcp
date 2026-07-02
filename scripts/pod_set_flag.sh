#!/usr/bin/env bash
# Toggle an admin safety flag on the live pod WITHOUT restarting the app — the change is
# picked up on the very next request (see dndmcp/admin_flags.py). This is the fast kill
# switch: for anything that needs a real restart, use redeploy_pod.sh instead.
#
# Usage: scripts/pod_set_flag.sh <flag-name> <0|1|integer>
#   scripts/pod_set_flag.sh flash_art 0     # kill switch: turn OFF art gen right now
#   scripts/pod_set_flag.sh flash_art 1     # turn it back on (or clear the override: omit
#                                            # the flag entirely to fall back to the env var)
#   scripts/pod_set_flag.sh bots_count 3    # INT_FLAGS below (admin_flags.get_int) always
#                                            # take a plain integer, even 0 or 1 — see INT_FLAGS
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Flags read via admin_flags.get_int() rather than admin_flags.enabled() — "1" for one of
# these means the integer 1, NOT the boolean True, which otherwise collides with the 0/1
# boolean shorthand below (bots_count 1 must not silently become bots_count=true). Add a
# name here whenever a new get_int()-backed flag is introduced.
INT_FLAGS=(bots_count)

NAME="${1:?usage: pod_set_flag.sh <flag-name> <0|1|integer>}"
VALUE="${2:?usage: pod_set_flag.sh <flag-name> <0|1|integer>}"
# Python literals, not JSON/JS ones (this gets spliced straight into a python3.11 -c snippet
# below) — never arbitrary code: VALUE is validated to be nothing but True/False/an integer
# before it ever reaches the snippet.
IS_INT_FLAG=false
for f in "${INT_FLAGS[@]}"; do [[ "$NAME" == "$f" ]] && IS_INT_FLAG=true; done

if [[ "$VALUE" =~ ^[0-9]+$ ]] && [[ "$IS_INT_FLAG" == true ]]; then
  PY_VALUE="$VALUE"
else
  case "$VALUE" in
    0) PY_VALUE=False ;;
    1) PY_VALUE=True ;;
    *) echo "value must be 0 or 1 (got: $VALUE) — or a plain integer, but only for one of: ${INT_FLAGS[*]}" >&2; exit 1 ;;
  esac
fi

"$SCRIPT_DIR/pod_ssh.sh" run "
python3.11 -c \"
import json, pathlib
p = pathlib.Path('/data/admin_flags.json')
data = json.loads(p.read_text()) if p.exists() else {}
data['$NAME'] = $PY_VALUE
p.write_text(json.dumps(data, indent=2))
print('admin_flags.json now:', json.dumps(data))
\"
"
