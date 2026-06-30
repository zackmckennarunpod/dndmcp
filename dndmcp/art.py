"""Art layer — GPU scene/portrait generation via Flash.

STUBBED for now (no GPU spend): returns a deterministic placeholder ref + an ASCII banner
so the game plays end-to-end with zero cost. Step 2 of BUILD.md wires real Flash image gen
behind the SAME interface, so nothing else changes.
"""

from __future__ import annotations

import hashlib

_ENABLED = False  # flip on once a Flash image endpoint is wired


def is_enabled() -> bool:
    return _ENABLED


def _ascii_banner(prompt: str) -> str:
    """Cheap deterministic ASCII placeholder so the terminal shows *something* visual."""
    h = hashlib.sha1(prompt.encode()).hexdigest()
    glyphs = "░▒▓█▄▀"
    rows = []
    for r in range(4):
        rows.append("".join(glyphs[int(h[(r * 8 + c) % len(h)], 16) % len(glyphs)] for c in range(24)))
    return "\n".join(rows)


def generate(prompt: str, *, kind: str = "scene") -> dict:
    """Return an image descriptor for a scene/portrait/map.

    Stub: {ref, ascii, image_b64=None, enabled=False}. Real impl will fill image_b64 from a
    Flash GPU endpoint and keep the same shape.
    """
    ref = f"{kind}:{hashlib.sha1(prompt.encode()).hexdigest()[:10]}"
    return {"ref": ref, "kind": kind, "prompt": prompt,
            "ascii": _ascii_banner(prompt), "image_b64": None, "enabled": _ENABLED}
