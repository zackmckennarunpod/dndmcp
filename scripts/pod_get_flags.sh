#!/usr/bin/env bash
# Read-only companion to pod_set_flag.sh — prints the live pod's current admin_flags.json so
# you can check what's actually toggled (bots_enabled, bots_count, flash_art, ...) without
# guessing or re-setting a value just to see it. Same SSH path, no app restart involved.
#
# Usage: scripts/pod_get_flags.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/pod_ssh.sh" run "
python3.11 -c \"
import json, pathlib
p = pathlib.Path('/data/admin_flags.json')
print(json.dumps(json.loads(p.read_text()), indent=2) if p.exists() else '{} (no overrides set yet)')
\"
"
