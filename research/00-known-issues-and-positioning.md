# Flash — Known Issues, Gaps & Positioning (from team Slack, ~Jun 2026)
> THE most valuable doc for hack day: these are the real surprises to avoid AND the gaps a winning project could solve. The Flash team cares about these → judge-resonant.

## Confirmed bugs / gotchas (avoid day-of pain)
- **torch unavailable under `flash dev` (Live Serverless)** — works only in `flash deploy`. Linear AE-3186. ⇒ Do NOT plan GPU/torch demos on `flash dev`; test torch primitives via `flash deploy` early.
- **Multi-endpoint build is all-or-nothing**: if ANY `@Endpoint`'s `dependencies=[...]` has a transitive dep with no pre-built wheel, the WHOLE build fails — even unrelated endpoints. ⇒ Keep deps lean; vet wheels; isolate risky endpoints.
- **No model-cache support with Flash** (Tim P.) — no config found to use Runpod model cache. ⇒ Use network volume for weights instead.
- **`flash deploy` can fail during worker init** (container won't even start) — seen by multiple. ⇒ Build a known-good base image; test deploy path before stage.
- **Cold starts rough at low max-workers** on live endpoints; ask to raise max workers. ⇒ Pre-warm (workersMin>=1), raise workersMax.
- **dev→deploy leaves orphan test endpoints** (one user had 6). No auto-cleanup. ⇒ Track + `flash undeploy` in a teardown script.
- **LB bug**: cpu lb_worker shows "running" after deploy, /ping hammered while other workers idle.
- **`flash deploy` does NOT print the endpoint URL** — Rambo had to dig for it. ⇒ Our control plane should surface URLs.
- **Raw GraphQL from automation IPs → 403 Cloudflare** (Cursor). ⇒ Use SDK / authed client, not scraping.

## Biggest gap = potential WINNING idea
**No live GPU stock/availability in the SDK.** Agents (Codex, Copilot, Cursor) all had to reverse-engineer Runpod GraphQL to find which DC has stock (e.g. RTX PRO 6000). Team's own recommendation: add `GpuType.availability()` / `DataCenter.available_gpus()`.
→ A hackathon project that adds a clean **availability-aware deploy helper / MCP tool** directly fills a gap the team explicitly wants. Pre-build this into the kit regardless.

## Positioning / narrative (for the pitch)
- Dean's **Journey Map + JTBD** (Figma/PDF) frames "why Flash over traditional Serverless" — get this doc; mirror its language in our demo pitch. Highlights gaps Flash fills.
- Core moat narrative (matches our prep-kit): code→live GPU endpoint in ~60s as a runtime call vs Docker build+push minutes on Modal/Replicate/SageMaker.
- EU-RO-1 is the primary DC; 3.13 support "coming soon"; pypistats tracks adoption.

## Ecosystem assets to pull (the user wants these)
- **runpod/skills → /flash skill** (github.com/runpod/skills/tree/main/flash). PR #22 syncs it to latest Flash — REVIEW/PULL that version. Used for the Databricks demo.
- **docs.runpod.io/flash/apps/overview** (+ whole /flash section).
- **runpod/runpod-mcp** — MCP server; Justin adding Flash setup directions to upcoming MCP PRs. (We also have mcp__runpod__* tools locally.)
- Flash repo **examples/** as canonical reference.
- Open question org-side: unifying Runpod CTL + Flash; Flash-for-pods (Ashley). LB endpoint creation over API (Reddit interest).
