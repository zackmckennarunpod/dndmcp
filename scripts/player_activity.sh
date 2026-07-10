#!/usr/bin/env bash
# Internal, app-owner-only view of who actually played: SSHes into the pod and reads
# /data/campaign.db directly (stdlib sqlite3, no dndmcp package import needed — sidesteps
# the "system python3 has no mcp module" gotcha the other pod scripts hit). Nothing here is
# reachable over HTTP; this is the private counterpart to the public, unauthenticated
# /metrics page.
#
#   scripts/player_activity.sh                # every world
#   scripts/player_activity.sh <campaign_id>   # one world only (e.g. the shared "main" id,
#                                               # or a shareable world id from start_adventure)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAMPAIGN="${1:-}"

read -r -d '' PYCODE <<'EOF' || true
import sqlite3, sys, time

campaign = sys.argv[1] if len(sys.argv) > 1 else None
c = sqlite3.connect("/data/campaign.db")
c.row_factory = sqlite3.Row

where = "WHERE ch.campaign_id=?" if campaign else ""
args = (campaign,) if campaign else ()
players = c.execute(f"""
    SELECT ch.player_id, ch.name, ch.klass, ch.campaign_id, ch.is_bot,
           MIN(l.ts) AS first_seen, MAX(l.ts) AS last_seen, COUNT(*) AS events,
           (SELECT ip FROM log WHERE player_id = ch.player_id AND ip IS NOT NULL
            ORDER BY ts DESC LIMIT 1) AS last_ip
    FROM character ch JOIN log l ON l.player_id = ch.player_id
    {where}
    GROUP BY ch.player_id ORDER BY last_seen DESC
""", args).fetchall()

if not players:
    print("No player activity" + (f" in world {campaign!r}" if campaign else "") + ".")
    sys.exit(0)

kinds = {}
for row in c.execute("SELECT player_id, kind, COUNT(*) AS n FROM log "
                     "WHERE player_id IS NOT NULL GROUP BY player_id, kind"):
    kinds.setdefault(row["player_id"], []).append((row["kind"], row["n"]))

def fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"

def fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

print(f"{'player':<10} {'name':<20} {'world':<12} {'duration':<9} {'events':<7} {'last ip':<16} {'last seen'}")
print("-" * 100)
for p in players:
    dur = fmt_dur(p["last_seen"] - p["first_seen"])
    top = sorted(kinds.get(p["player_id"], []), key=lambda kv: -kv[1])[:3]
    top_str = ", ".join(f"{k}×{n}" for k, n in top)
    bot = " 🤖" if p["is_bot"] else ""
    name = (p["name"] or "?") + bot
    print(f"{p['player_id'][:8]:<10} {name[:20]:<20} {p['campaign_id'][:12]:<12} "
         f"{dur:<9} {p['events']:<7} {p['last_ip'] or '—':<16} {fmt_ts(p['last_seen'])}")
    print(f"           actions: {top_str or '—'}")
EOF

"$SCRIPT_DIR/pod_ssh.sh" run "python3.11 -c '$PYCODE' ${CAMPAIGN}"
