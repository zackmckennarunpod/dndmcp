"""Art layer — GPU scene generation via Flash, retro pixel-art style.

Real path (DND_FLASH_ART=1): flash_art.generate_image() -> PNG bytes, cached to disk keyed
by room, rendered as ANSI truecolor half-block terminal art (NOT grayscale ASCII — pixel
art's whole appeal is color). Falls back to a deterministic ASCII placeholder banner with
zero GPU spend when disabled/not-yet-cached, so the game always plays.

generate() itself NEVER calls Flash inline / never blocks — that would hang look()/move()
for up to ~90s on cold start. Real generation only happens in prefetch(), fired-and-forgotten
by server.py the same way room TEXT is speculatively prefetched (_prefetch_frontier). Art
interface stays fixed regardless of which path served it — see BUILD.md.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

from . import flash_art

_STYLE_SUFFIX = ", 16-bit pixel art, retro RPG sprite, limited color palette, pixelated"
_NEGATIVE_PROMPT = "blurry, photorealistic, smooth gradients, 3d render, high detail, realistic"


def is_enabled() -> bool:
    return flash_art.ENABLED


def _art_dir() -> Path:
    state_dir = Path(os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp")))
    d = state_dir / "art"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ascii_banner(prompt: str) -> str:
    """Cheap deterministic ASCII placeholder so the terminal shows *something* visual when
    Flash art is off/not-yet-cached — zero GPU spend."""
    h = hashlib.sha1(prompt.encode()).hexdigest()
    glyphs = "░▒▓█▄▀"
    rows = []
    for r in range(4):
        rows.append("".join(glyphs[int(h[(r * 8 + c) % len(h)], 16) % len(glyphs)] for c in range(24)))
    return "\n".join(rows)


def _image_to_ansi(png_bytes: bytes, *, cols: int = 40, rows: int = 18) -> str:
    """Real image -> terminal ANSI truecolor art. The Unicode upper-half-block ('▀') encodes
    TWO vertical pixel rows per terminal line (foreground=top pixel, background=bottom pixel)
    — doubles effective vertical resolution for free. NEAREST resampling (not bicubic/
    lanczos) keeps hard pixel edges instead of smoothing them away, matching the pixel-art
    aesthetic rather than fighting it."""
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    img = img.resize((cols, rows * 2), Image.Resampling.NEAREST)
    px = img.load()
    lines = []
    for y in range(0, rows * 2, 2):
        line = []
        for x in range(cols):
            tr, tg, tb = px[x, y]
            br, bg, bb = px[x, y + 1]
            line.append(f"\x1b[38;2;{tr};{tg};{tb}m\x1b[48;2;{br};{bg};{bb}m▀")
        lines.append("".join(line) + "\x1b[0m")
    return "\n".join(lines)


def _cached_ansi(ref: str) -> str | None:
    path = _art_dir() / f"{ref}.png"
    if not path.exists():
        return None
    return _image_to_ansi(path.read_bytes())


async def prefetch(ref: str, prompt: str) -> bool:
    """Generate (via Flash) and cache an image for `ref`, ahead of when a player actually
    looks at it — the same speculative-prefetch pattern already used for room TEXT
    (server.py's _prefetch_frontier). Returns True if a real image got cached, False if
    Flash is off/unavailable (caller keeps using the placeholder). Never raises."""
    path = _art_dir() / f"{ref}.png"
    if path.exists():
        return True
    png = await flash_art.generate_image(prompt + _STYLE_SUFFIX, negative_prompt=_NEGATIVE_PROMPT,
                                         width=256, height=256, steps=4)
    if not png:
        return False
    path.write_bytes(png)
    return True


def generate(prompt: str, *, kind: str = "scene", ref: str | None = None) -> dict:
    """Return an image descriptor for a scene/portrait/map.

    `ref` should be the room's persisted image_ref (set by prefetch() via
    world.set_room_image() once generation completes) — if a real cached image exists there,
    renders it as ANSI. Otherwise falls back to the ASCII placeholder. Synchronous and
    non-blocking either way; the only place a live Flash call happens is prefetch()."""
    resolved_ref = ref or f"{kind}:{hashlib.sha1(prompt.encode()).hexdigest()[:10]}"
    ansi = _cached_ansi(resolved_ref) if flash_art.ENABLED else None
    return {"ref": resolved_ref, "kind": kind, "prompt": prompt,
            "ascii": ansi or _ascii_banner(prompt), "image_b64": None, "enabled": ansi is not None}
