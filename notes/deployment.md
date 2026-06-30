# Deployment / host architecture

**Key realization that shapes the whole stack:** the GPU layer *is* Flash — serverless,
auto-managed, we don't host it. What *we* deploy is a **thin CPU control plane** that holds the
`@remote` functions / registry / telemetry and calls out to Flash. So don't over-engineer the host;
a single CPU pod covers it.

```
┌─ Runpod CPU pod ───────────────────────────┐
│  Control plane (one Python service)         │
│   • registry + API/MCP server               │        ┌─ Flash (serverless) ─┐
│   • SQLite (on pod volume)                  │ ──────▶│  GPU endpoints,       │
│   • live observability dashboard (= demo)   │ RUNPOD_│  auto-scale 0→N,      │
│  Ingress: Runpod proxy (default) /          │ API_KEY│  we don't host these  │
│           Cloudflare Tunnel (if needed)     │        └──────────────────────┘
└─────────────────────────────────────────────┘
```

## Component picks (and the anti-pattern to skip)

| Concern        | Pick (hackathon-right)                                         | Skip                          |
|----------------|----------------------------------------------------------------|-------------------------------|
| Compute        | **CPU pod on Runpod** — fits "all on Runpod" narrative          | SST/Lambda (heavier, deploy rules) |
| Ingress        | **Built-in Runpod proxy** `{podid}-{port}.proxy.runpod.net` (TLS, zero setup) | preemptive Cloudflare Tunnel |
| Auth           | **single bearer token** on HTTP; Flash uses `RUNPOD_API_KEY`    | Okta / OAuth / real identity  |
| DB             | **SQLite on a pod volume** — registry + telemetry, zero ops     | Postgres / PlanetScale / Dolt |
| Observability  | **self-contained live dashboard** (worker count, p50/p99, $/call, success) — *is* the demo UI, on the rubric | Datadog (external, invisible on stage) |

**Cloudflare Tunnel — only when the proxy bites:** reach for it for a *stable custom domain*,
*websockets/SSE the proxy buffers*, or *Cloudflare Access* as free auth. Not before.

## The fork that depends on the unannounced theme — support BOTH cheaply
- **Claude Desktop / MCP client → stdio transport: no public ingress, no auth, nothing to expose.**
  The MCP process sits on the pod/laptop and calls Flash over the network. Simplest, most robust.
- **Web app the room hits → public ingress** (proxy or tunnel) + the bearer token.

So: one shared core (registry + Flash calls + telemetry) with **two adapters** — MCP-stdio and HTTP.
The announcement just decides which adapter we light up.

## Deploy mechanics (respect repo safety rules)
- **Clean path:** build amd64 Docker image of the control plane → deploy as a pod. Reproducible.
- **Fast iteration:** `ssh-loop` / `live-dev` to edit on the pod during the sprint.
- ⚠️ Nothing on a running pod survives a redeploy — **cement to the image before the demo.**
  Never SCP as the delivery mechanism.

## Prep checklist (folds into `kit/`)
- [ ] Control-plane skeleton: shared core + **MCP-stdio adapter** + **HTTP adapter**
- [ ] SQLite schema: registry + telemetry
- [ ] Live observability/cost dashboard (reusable demo UI)
- [ ] Dockerfile (amd64) + `.env` template (`RUNPOD_API_KEY`, `AUTH_TOKEN`)
- [ ] Ingress notes: proxy URL pattern + Cloudflare Tunnel fallback recipe (ready, not required)
