# DNDMCP — agent operating guide

The brain runs on a live Runpod pod, not locally. Read this before touching the pod.

## Where it lives

- **Pod:** `ldghdgi0xxn6jj` (EU-RO-1, CPU). Repo cloned at `/app` via a dedicated GitHub
  deploy key (read-write, private repo `zackmckennarunpod/dndmcp`).
- **Persistence:** `/data` is a real Runpod **network volume** (`z1ovh5r8wg`), not ephemeral
  container disk — survives restarts AND pod termination. `DNDMCP_STATE_DIR=/data`.
- **Public URLs (stable across restarts):** MCP `https://ldghdgi0xxn6jj-8000.proxy.runpod.net/mcp`,
  GUI `https://ldghdgi0xxn6jj-8002.proxy.runpod.net`.
- **SSH host:port is NOT stable** — it changes on every pod restart / `update-pod` call.
  Never hardcode it. `scripts/pod_ssh.sh` re-resolves it fresh from the Runpod API every time.

## The only sanctioned workflow

1. Edit code **locally**, never directly on the pod (the pod's `/app` is a plain git clone,
   not your editor target).
2. `git commit` + `git push origin main` from your local checkout.
3. `scripts/redeploy_pod.sh` — pulls on the pod and restarts the app process. This is the
   ONLY way the pod should be restarted; it re-resolves the SSH endpoint itself.
4. `scripts/pod_status.sh` — confirm the right commit is deployed and both ports respond.

Never `ssh` in and hand-edit files on the pod, and never restart the process by any path
other than `redeploy_pod.sh` — both bypass the one thing that makes this safe: the pod
always ends up running exactly what's on `origin/main`.

## Test locally before you push — one command, fully isolated

Every worktree can run its own GUI+MCP server with zero setup and zero collision risk with
other agents, the shared local dev world, or the live pod:

```bash
cd <your worktree>
../scripts/dev_worktree.sh   # or scripts/dev_worktree.sh if you're at the worktree's own root
```

This auto-picks two free ports, uses a state dir unique to that worktree
(`~/.dndmcp_worktrees/<worktree-name>`), and runs the code from *that* worktree using the main
repo's already-installed `.venv` (deps are shared unless your branch touched
`dndmcp/requirements.txt` — the script tells you if you need your own venv). Ctrl-C stops it;
nothing it touches is shared state, so there's nothing to clean up or coordinate with other
agents. The script prints the GUI URL to open and the state dir path if you want to inspect
or `rm` the DB.

This is the ONLY thing multiple agents working in parallel worktrees should run to test
changes — never point a worktree at `~/.dndmcp` (the single shared local dev DB) or anywhere
near the pod flow below until your change is verified and merged.

## The database is precious — do not wipe it casually

`/data/campaign.db` (and `tickets.db`) hold the ENTIRE shared world — every player's
progress, every stigmergic trace. It is never touched by a normal redeploy.

The only sanctioned way to wipe it is `scripts/reset_world.sh --yes` (destructive,
requires the explicit flag on purpose). Don't `rm` anything under `/data` directly, and
don't add code that does.

## Other scripts

- `scripts/pod_logs.sh [n]` — tail the live app log.
- `scripts/install_claude_code.sh` — what judges/players/other agents run to connect their
  own Claude Code to the shared world (`claude mcp add --transport http`). This is also the
  install path documented in `dndmcp/SETUP.md`.

## Admin flags — live kill switches, no redeploy

`dndmcp/admin_flags.py` reads `/data/admin_flags.json` fresh on every call — flipping a value
takes effect on the pod's very next request/poll, no restart needed. Controlled entirely over
SSH (no HTTP auth surface for this — deliberately kept simple; see the bot-player section
below for why that tradeoff was made on purpose, not by default):

```bash
scripts/pod_get_flags.sh              # read the current overrides
scripts/pod_set_flag.sh <name> <0|1>       # boolean flags, e.g. flash_art
scripts/pod_set_flag.sh <name> <integer>   # numeric flags, e.g. bots_count
```

### Self-playing bot character (`dndmcp/bot_player.py`)

An autonomous character that plays itself against your own hosted Flash LLM — a small
"player" persona decides an action each turn, `dm_loop.py` resolves/narrates it exactly like
a real browser player's turn (same tool surface, same sanitization). Off by default.

```bash
scripts/pod_set_flag.sh bots_enabled 1   # turn bots on
scripts/pod_set_flag.sh bots_count 2     # how many bot characters (default 0 = none)
scripts/pod_set_flag.sh bots_enabled 0   # turn them all off
scripts/pod_get_flags.sh                 # check current bots_enabled/bots_count
```

A background supervisor (started once in `web.py`'s FastAPI startup hook) polls these every
15s and starts/stops individual bot loops to match — no restart required either direction.

### Warm-on-visit (`dndmcp/flash_art.py`/`flash_llm.py`'s `maybe_warm`)

A page load (`GET /`) or a real chat turn (`POST /chat`) fires a fire-and-forget nudge at
both Flash endpoints so a cold, scale-to-zero worker (art's `workers=(0,3)`) starts spinning
up before anyone's actually waiting on a generation, instead of eating that cold start live.
Self-debouncing (a cheap `/health` check first — costs nothing if a worker's already up), and
scales back down for free via each endpoint's existing `idle_timeout=300`. On by default.

```bash
scripts/pod_set_flag.sh warm_on_visit 0   # stop nudging on page load/chat (still shows the badge)
scripts/pod_set_flag.sh warm_on_visit 1   # back on (the default)
```

The header's 🟢/🟡/⚪ badge (art/LLM worker status) keeps working regardless of this flag —
it's a read-only `/health` poll, never a spend.
Status doesn't need SSH at all: `/metrics` badges every bot character with 🤖, and the world
map's "Active now" spectate strip shows them live while they're playing.

Deliberately NOT behind an authenticated HTTP endpoint: that was considered (using
`RUNPOD_API_KEY` as the bearer token) and rejected — that key is full Runpod-account-scoped,
so a leak from a public-facing admin endpoint would expose far more than "someone can toggle
this hackathon demo's bot flag." SSH-only keeps the blast radius of any leak limited to what
SSH access already implies. If this ever needs remote (non-SSH) control, mint a separate,
narrowly-scoped token for it rather than reusing the account key.

### Model evals (`dndmcp/evals.py`, `GET /evals`, `GET /evals/history`)

A real, re-runnable comparison harness — not one-off test scripts — across two tracks:
tool-calling reliability (auto-graded pass/fail against `dm_loop`'s actual `SYSTEM_PROMPT`/
`TOOLS`, so it reflects production behavior, not a synthetic prompt) and room-generation
coherence (not auto-graded — raw output shown side by side on the page for a human to judge).
Every completed run persists to its own file (`DNDMCP_STATE_DIR/evals_runs/<run_id>.json`),
so `/evals/history` (with a `?model=` substring filter) can look back across runs, not just
the latest one.

`POST /evals/run` is gated OFF by default, same SSH-only admin_flags shape as `bots_enabled`:

```bash
scripts/pod_set_flag.sh evals_enabled 1   # open the window so the page's "run" button works
scripts/pod_set_flag.sh evals_enabled 0   # close it again
```

The public page's disabled-state message deliberately does NOT name the flag/script — same
"don't document the admin control surface on the surface it controls" principle as
`bots_enabled` having no public docs on how to flip it.

Currently compares two fixed endpoints (`web.py`'s `_EVAL_CONFIGS`): the live `dnd-dm-vllm`
(Qwen2.5-7B) and a second, DORMANT endpoint `dnd-dm-vllm-14b` (Qwen2.5-14B, `ADA_48_PRO`,
`workers=(0,3)`) minted via the Flash SDK (`runpod_flash.Endpoint`, same pattern as
`flash_llm.ensure()`/`dm_loop.ensure_dm_endpoint()`) specifically to A/B test a bigger model
without touching live traffic. Real measured result (2026-07-03, two separate runs): 7B
correct on ~50-58% of tool-calling scenarios, 14B on ~82-92% — but 14B did NOT fix the
existing "same descriptive word every room" naming bias (7B converges on "Rusty", 14B on
"Ashen" — even repeating the literal name "The Ashen Forge" across two unrelated worlds,
reproduced twice). An anti-cliché prompt addition tested well in `evals.py`'s experiments but
isn't shipped into `worldgen.py` yet.

**Gotcha, confirmed live:** the 14B endpoint should sit at `workersMax=0` between uses (true
$0 at rest) and only get bumped to a real max right before a run. But restoring `workersMax`
from 0 to ANY nonzero value immediately spins up a fresh worker, regardless of
`workersMin=0` — there's no way to "make it ready but idle" without that costing a cold
start. Bump it, use it, then set it straight back to 0 rather than leaving it at a nonzero
max "just in case."

## Known gotchas (see the `bd remember` entry `dndmcp-live-pod-ops-...` for the full list)

- FastMCP's DNS-rebinding protection 421s any request whose Host header isn't
  localhost — `server.py`'s `main()` disables it specifically for http/sse transport. Don't
  re-enable it; the pod-hosted premise requires the public host to work.
- The `runpod/base:*-cuda*` image has nginx already bound to port 8001 — GUI uses 8002.
- CPU pods with a network volume need the `deployCpuPod` GraphQL mutation directly
  (`networkVolumeId` field) — the `mcp__runpod__create-pod` tool silently drops volume
  requests for CPU pods.
- Multiple sessions can be editing `dndmcp/*.py` concurrently (this has happened) — always
  `git pull` / check `git status` before assuming your view of the file is current.
- `dm_loop.py`'s DM chat endpoint is resolved via the Flash SDK now (`ensure_dm_endpoint()`,
  same resolve-by-name-or-mint pattern as `flash_llm.py`/`flash_art.py`), not a hardcoded
  endpoint id — self-heals if `dnd-dm-vllm` is ever deleted/recreated. `DND_DM_BASE_URL` still
  works as an explicit override and as the fallback if Flash resolution itself errors.
- Room generation (`worldgen._room_messages`) now gets a real adjacency structure for nearby
  rooms (`server._graph_context` — each room's own exits, named when the destination is also
  in view, plus its contents), not a flat name/kind list — the DB's `edges`/`room.contents`
  data reaching the prompt for the first time. `recent_events` bumped 5→8 for the same reason
  (the whole prompt runs 500-1000 tokens against a 16384-token context, nowhere near the
  ceiling — 5 was convention, not a budget limit).
