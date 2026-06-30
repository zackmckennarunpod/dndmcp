"""Auth + host wiring for Flash.

THE friction-saver from prior prep: Flash defaults *every* host to PROD
(`api.runpod.io`). A **dev** API key 401s/404s against prod unless all three hosts
are overridden. We make that a one-liner.

On hack day you will most likely be handed a *prod* key for a real account — call
`load_env()` (profile="prod", the default) and you're done. Use profile="dev" only
when testing against the dev control plane with a dev key.
"""

from __future__ import annotations

import os
import subprocess

# Each profile sets the three hosts Flash reads. Miss one and you get a confusing
# 401 (control plane) or 404 (endpoint invocation) that looks like a code bug.
HOST_PROFILES: dict[str, dict[str, str]] = {
    "prod": {
        "RUNPOD_API_BASE_URL": "https://api.runpod.io",
        "RUNPOD_REST_API_URL": "https://rest.runpod.io/v1",
        "RUNPOD_ENDPOINT_BASE_URL": "https://api.runpod.ai/v2",
    },
    "dev": {
        "RUNPOD_API_BASE_URL": "https://api.runpod.dev",
        "RUNPOD_REST_API_URL": "https://rest.runpod.dev/v1",
        "RUNPOD_ENDPOINT_BASE_URL": "https://api.runpod.dev/v2",
    },
}


def _api_key_from_keychain(profile: str) -> str | None:
    """Best-effort macOS Keychain lookup so we never hardcode a key.

    Looks for `runpod-api-key-<profile>` (matches the user's keychain convention).
    """
    service = f"runpod-api-key-{profile}"
    try:
        found = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    key = found.stdout.strip()
    return key or None


def load_env(profile: str = "prod", *, api_key: str | None = None) -> str:
    """Wire Flash's hosts for `profile` and ensure RUNPOD_API_KEY is set.

    Resolution order for the key: explicit arg → existing env var → keychain
    (`runpod-api-key-<profile>`). Returns the resolved key. Raises if none found.
    """
    if profile not in HOST_PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {list(HOST_PROFILES)}")

    for host_var, url in HOST_PROFILES[profile].items():
        os.environ[host_var] = url

    resolved_key = api_key or os.environ.get("RUNPOD_API_KEY") or _api_key_from_keychain(profile)
    if not resolved_key:
        raise RuntimeError(
            f"No Runpod API key. Set RUNPOD_API_KEY, pass api_key=, or store it in "
            f"keychain as 'runpod-api-key-{profile}'."
        )
    os.environ["RUNPOD_API_KEY"] = resolved_key
    return resolved_key


def active_profile() -> str:
    """Infer which profile the current env points at (for display/telemetry)."""
    base = os.environ.get("RUNPOD_API_BASE_URL", "")
    return "dev" if ".runpod.dev" in base else "prod"
