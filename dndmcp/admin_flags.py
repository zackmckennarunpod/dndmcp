"""Live-toggleable safety flags for the pod.

Every other DND_* flag is an env var, fixed at process start — changing one means editing
redeploy_pod.sh and doing a full restart. That's fine for day-to-day config, but too slow
for "something's wrong, kill this feature right now" close to a demo/submission. This is
that kill switch: an admin flips one JSON file over SSH (scripts/pod_set_flag.sh), no
restart needed — the very next call sees the new value, since nothing here is cached.

Falls back to whatever the caller's env-var default already resolved to when the file is
missing/corrupt/doesn't mention the flag, so this is purely an override layer — nothing
changes unless it's actually used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _flags_path() -> Path:
    state_dir = Path(os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp")))
    return state_dir / "admin_flags.json"


def enabled(name: str, *, default: bool) -> bool:
    """Read fresh every call (no caching) so a toggle takes effect immediately. Never
    raises: a missing file, corrupt JSON, or a name it doesn't mention all just mean
    "no override, use the env-var default" — same as not having a flags file at all."""
    try:
        data = json.loads(_flags_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    return bool(data.get(name, default))
