"""Runs ON the pod (via regen_art.sh) — force-regenerates every room's art in a campaign.

Deletes each room's cached PNG and calls art.prefetch() fresh, so it re-runs through
whatever the CURRENT deployed code does (current prompt suffix, current palette) — this is
how you re-style a whole world's art after an art-pipeline change, without waiting for
players to naturally re-trigger it (which never happens — art only ever generates once per
room, on creation).
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")

from dndmcp import art  # noqa: E402
from dndmcp.state import World  # noqa: E402


async def main() -> None:
    campaign_id = sys.argv[1] if len(sys.argv) > 1 else "main"
    world = World()
    rows = world._c.execute(  # noqa: SLF001 -- one-off admin script, not a public World method
        "SELECT id, name, description, image_ref FROM rooms "
        "WHERE campaign_id=? AND image_ref IS NOT NULL", (campaign_id,)
    ).fetchall()
    print(f"{len(rows)} rooms with art in campaign {campaign_id!r}")
    if not rows:
        return

    art_dir = art._art_dir()  # noqa: SLF001
    ok = 0
    for row in rows:
        room_id, name, description, ref = row["id"], row["name"], row["description"], row["image_ref"]
        path = art_dir / f"{ref}.png"
        path.unlink(missing_ok=True)  # force prefetch() to actually regenerate, not cache-hit
        success = await art.prefetch(ref, f"{name}: {description}")
        print(f"  {room_id} ({name}): {'ok' if success else 'FAILED'}")
        if success:
            ok += 1
            world.set_room_image(room_id, ref)
            world.log("art.generated", f"art for {name} (flash, regenerated)",
                      campaign_id=campaign_id, subject_type="room", subject_id=room_id)
    print(f"done: {ok}/{len(rows)} regenerated")


if __name__ == "__main__":
    asyncio.run(main())
