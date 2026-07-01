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
import logging
import os
from pathlib import Path

from . import flash_art

logger = logging.getLogger(__name__)

_STYLE_SUFFIX = ", 16-bit pixel art, retro RPG sprite, limited color palette, pixelated"
_NEGATIVE_PROMPT = "blurry, photorealistic, smooth gradients, 3d render, high detail, realistic"

# DawnBringer 16 (DB16) — a well-known, freely-used curated palette purpose-built for pixel
# art, moody/desaturated enough to fit this game's dark-fantasy tone. "limited color palette"
# in the prompt above is a REQUEST, not a guarantee — SDXL-Turbo doesn't reliably follow it,
# so two rooms generated back to back can come back with completely different palettes/moods.
# Quantizing every real image down to this exact palette (see _quantize_to_palette) is what
# actually GUARANTEES the whole world reads as one consistent art style, regardless of
# per-prompt variance.
_PALETTE_RGB = [
    (20, 12, 28), (68, 36, 52), (48, 52, 109), (78, 74, 78),
    (133, 76, 48), (52, 101, 36), (208, 70, 72), (117, 113, 97),
    (89, 125, 206), (210, 125, 44), (133, 149, 161), (109, 170, 44),
    (210, 170, 153), (109, 194, 202), (218, 212, 94), (222, 238, 214),
]


def is_enabled() -> bool:
    return flash_art.enabled()


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


def _quantize_to_palette(png_bytes: bytes) -> bytes:
    """Snap a real generated image onto _PALETTE_RGB exactly — every room in the world ends
    up drawing from the identical 16 colors, so the map reads as one cohesive art style no
    matter how differently SDXL-Turbo interpreted each room's prompt. Dithering (PIL's
    default, Floyd-Steinberg) is deliberate: flat nearest-color mapping bands badly on
    gradients, while dithering gives the kind of textured look genuine 16-color-era pixel
    art actually has, instead of looking like a broken palette swap."""
    from PIL import Image

    palette_img = Image.new("P", (1, 1))
    flat = [c for rgb in _PALETTE_RGB for c in rgb]
    flat += [0] * (768 - len(flat))  # PIL palettes are always 256 entries; pad with black
    palette_img.putpalette(flat)

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    quantized = img.quantize(palette=palette_img, dither=Image.Dither.FLOYDSTEINBERG)
    out = io.BytesIO()
    quantized.convert("RGB").save(out, "PNG")
    return out.getvalue()


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
    # The Flash GPU call above is already paid for by this point — losing the image now (e.g.
    # a quantize-side bug or missing dependency) would waste that spend for nothing. Cache the
    # raw image rather than the whole generation on any quantize failure; confirmed live this
    # isn't hypothetical (a missing Pillow install did exactly this — see flash-hackathon-lg3).
    try:
        path.write_bytes(_quantize_to_palette(png))
    except Exception:
        logger.warning("art.prefetch: palette quantize failed, caching raw image instead",
                       exc_info=True)
        path.write_bytes(png)
    return True


def generate(prompt: str, *, kind: str = "scene", ref: str | None = None) -> dict:
    """Return an image descriptor for a scene/portrait/map.

    `ref` should be the room's persisted image_ref (set by prefetch() via
    world.set_room_image() once generation completes) — if a real cached image exists there,
    renders it as ANSI. Otherwise falls back to the ASCII placeholder. Synchronous and
    non-blocking either way; the only place a live Flash call happens is prefetch()."""
    resolved_ref = ref or f"{kind}:{hashlib.sha1(prompt.encode()).hexdigest()[:10]}"
    ansi = _cached_ansi(resolved_ref) if flash_art.enabled() else None
    return {"ref": resolved_ref, "kind": kind, "prompt": prompt,
            "ascii": ansi or _ascii_banner(prompt), "image_b64": None, "enabled": ansi is not None}
