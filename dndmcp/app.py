"""Combined launcher — runs the GUI map + the MCP brain together (for the pod container).

GUI on GUI_PORT (default 8001), MCP HTTP on PORT (default 8000). Both read/write the same
DNDMCP_STATE_DIR, so the map stays synced to play. Used as the container CMD.

Local dev tip: you usually DON'T need this — run the GUI (`python -m dndmcp.web`) and attach
Claude Desktop to the stdio server (`python -m dndmcp.server`) separately, sharing DNDMCP_STATE_DIR.
"""

from __future__ import annotations

import os
import threading

from . import server, web


def main() -> None:
    gui = threading.Thread(target=web.main, daemon=True)
    gui.start()
    os.environ.setdefault("DNDMCP_TRANSPORT", "http")
    server.main()  # MCP HTTP (blocks)


if __name__ == "__main__":
    main()
