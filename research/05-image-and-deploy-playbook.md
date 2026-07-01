# 05 — Image & Deploy Playbook (pre-build for hack day)

> Goal: walk into Tue Jun 30 with a **working Docker image + a provisioned pod environment** where
> everything is already installed and wired, so hack day is pure composition. This doc is the concrete
> build/deploy mechanics. Sources: skills `live-dev-workflow`, `ssh-loop`, `runpod-coding-providers`,
> `runpod-monitor`; `notes/deployment.md`; `research/00–02`; the `mcp__runpod__*` tool surface.
> (`container-permissions` skill is a broken symlink locally — its essentials are folded into §6.)

---

## 0. The shape of what we're building

Two distinct image roles — **do not conflate them**:

| Role | What it is | Base | Where it runs |
|------|-----------|------|---------------|
| **Control-plane image** | thin CPU Python service: registry + MCP/HTTP adapters + SQLite + dashboard, calls Flash via `RUNPOD_API_KEY` | small CPU base **for prod**, `runpod/base` **for dev** | one long-running **CPU pod** |
| **Flash GPU worker image** | the image Flash workers boot for `@remote` GPU functions | `zackmckennarunpod/flash-ts:latest` (TS) or Flash default (Python) | serverless, Flash-managed — **we don't host it** |

The GPU layer *is* Flash. We are only pre-building (a) the control-plane image, and (b) optionally a
**custom GPU worker base** with heavy deps pre-baked so cold start is short. The dev pod is scaffolding
to iterate on the control plane live.

---

## 1. Base image

### Control-plane pod (CPU) — two-base rule (live-dev hard rules #4/#5)

- **DEV pod base: `runpod/base:0.6.2-cuda12.4.1`.** Counterintuitive (it's CUDA, we're CPU) but it's the
  only family that ships `sshd` + `nginx` running as a **foreground process**. Plain images
  (`python:3.11-slim`, `oven/bun:1`) have no foreground process and **infinite-restart on Runpod CPU pods**.
  Use this for the iterate-live pod.
- **PROD control-plane image base: `python:3.11-slim`** (CPU Python). Tiny, fast push, fast cold start.
  This is the image we actually `docker buildx ... --push` and run as the deployed pod.

### CUDA considerations (only relevant for a custom GPU worker base)

- The control plane needs **no CUDA** — it never touches a GPU; it calls Flash.
- If we bake a **custom GPU worker base** for primitives: match the **torch/CUDA pair exactly**.
  - `pip install torch --index-url https://download.pytorch.org/whl/cu124` — a bare `pip install torch`
    pulls cu130 and **silently falls back to CPU (10×–slower)**. This is the single biggest GPU footgun.
  - Production GPU base: `nvidia/cuda:12.4.0-runtime-ubuntu22.04` (runtime, not devel — smaller).
- The Runpod-recommended default pod image (per `create-pod` tool) is
  `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` (CUDA 12.8.1 / torch 2.8.0 / Ubuntu 24.04). Fine as a
  GPU **scratch/dev** pod base; heavy for a worker image.

### What `zackmckennarunpod/flash-ts:latest` implies

- Verified: it's an **amd64-only** manifest (correct for Runpod — confirms the build-for-amd64 rule).
- It is the **hardcoded default GPU worker image** for the TS SDK (`FLASH_TS_GPU_IMAGE`, overridable by env).
  So the TS SDK already has a known-good GPU worker base published under our Docker Hub. Implication:
  - For TS `@remote` GPU functions, we **inherit a working GPU image for free** — no GPU image build needed
    unless we want heavy weights/deps pre-baked.
  - If we want a fat primitive base (Whisper/SDXL/torch pre-installed) we can `FROM` it and layer, then
    point `FLASH_TS_GPU_IMAGE` at our tag. Keeps the Flash wiring intact.
  - Python SDK uses its own default worker image (not this tag) — primitives written in Python won't use it.

---

## 2. What to bake into the control-plane image

Bake everything that has wall-clock cost or that we'd otherwise re-discover under pressure. Cold start of
the *control plane* should be ~seconds.

**Runtime + SDKs**
- `python:3.11-slim` + system deps (`git`, `curl`, `ca-certificates`, `openssh-server` if we want SSH into
  the prod pod too — optional).
- `pip install --no-cache-dir runpod-flash` (Python SDK, v1.4.2) — the primary control surface.
- Bun + `@runpod/flash` (TS SDK) **only if** we choose TS for the control plane. Decision still open
  (research 02): TS has the richer CLI + typed GraphQL; Python matches the primitives. Recommendation
  below picks **Python control plane** for single-language simplicity; keep TS SDK out of the image unless
  we commit to it.
- `--no-cache-dir` on *every* pip install (hard rule #2) — 10–20 GB pod disks fill in one fat install.

**Control-plane skeleton (the `kit/` spine, baked at a stable path e.g. `/app`)**
- Shared core: registry + Flash-call wrappers + telemetry writer.
- **MCP-stdio adapter** and **HTTP adapter** (both — the announced theme picks which we light up).
- Fan-out helper (`asyncio.gather` + `Semaphore(workersMax)` + partial-failure handling) — there's no `.map()`.
- Cost/latency readout from `JobOutput` (`delayTime + executionTime → $`).
- Live dashboard (worker-count graph + cost ticker) served by the HTTP adapter — this *is* the demo UI.

**Auth wiring**
- Read `RUNPOD_API_KEY` and `AUTH_TOKEN` from env at boot (don't bake secrets into the image).
- The Flash SDK auto-resolves `RUNPOD_API_KEY` env → `.env` → `~/.config/runpod/credentials.toml`. Env is
  cleanest for a pod.

**Pre-staged tooling (so day-of is composition)**
- The **availability-aware deploy helper** (research 00's "biggest gap / winning idea") — pre-build the
  `GpuType.availability()` / `DataCenter.available_gpus()` MCP tool now.
- Runtime-deploy helper (~15-line code→live-endpoint loop) for the agent/tool theme.
- SQLite schema migration baked + applied at boot against the volume path.
- Teardown script (`flash undeploy` loop) — dev→deploy leaves orphan endpoints (research 00).

**Do NOT bake**: model weights (→ network volume, §5), secrets, anything theme-specific.

**Dockerfile build/push (global rule):**
```bash
docker buildx build --platform linux/amd64 \
  -t zackmckennarunpod/flash-control:latest --push .
```

---

## 3. dev → cement → deploy loop (iterate live, no Docker rebuilds)

The whole point: **edit the control plane on a running pod, no Docker round-trips**, then cement back to the
image before the demo. Order is sacred (live-dev): `DEV (rsync) → CEMENT (image) → DEPLOY (pod)`.

### Resolve SSH every iteration (ports are dynamic)
```bash
RP_KEY=$(security find-generic-password -s runpod-api-key-prod -w)   # prod pod ⇒ prod key
ENDPOINT=$(curl -s -X POST https://api.runpod.io/graphql \
  -H "Authorization: Bearer $RP_KEY" -H "Content-Type: application/json" \
  -d '{"query":"query{pod(input:{podId:\"<POD_ID>\"}){runtime{ports{ip publicPort type}}}}"}' \
  | jq -r '.data.pod.runtime.ports[]|select(.type=="tcp")|"\(.ip):\(.publicPort)"')
HOST=${ENDPOINT%:*}; PORT=${ENDPOINT#*:}
```
- **Re-query every loop** — Runpod assigns a new TCP port on every pod start, and pods can silently restart
  (hard rule #9: `runtime.ports` can flip to `null`, `uptimeInSeconds:0`).
- **`*.proxy.runpod.net` is HTTP-only** — never works for raw SSH/TCP. Use the IP:port from the API.
- **SSH-ready ≠ port-up** (hard rule #8): port 22 registers 5–30 s before sshd accepts. Probe with a real
  `ssh ... echo ok` until success. Prefer direct TCP SSH over the `ssh.runpod.io` gateway (5+ min lag).

### Push code (rsync — NO `-z`)
```bash
rsync -av --delete \
  -e "ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no -p $PORT" \
  ./app/ root@$HOST:/app/
```
- **Never `-avz`** (hard rule #6): compression breaks between rsync 3.2.7 endpoints. `-av` only.
- `mkdir -p /app` on the remote first.

### Restart the service (full FD redirect or SSH won't return)
```bash
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no -p $PORT root@$HOST \
  "pkill -f 'python3.11 -m app'; sleep 1; cd /app && \
   nohup python3.11 -m app > /tmp/cp.log 2>&1 </dev/null & disown; echo PID:\$!"
```
- `nohup ... </dev/null & disown` is required (hard rule #7) — without `</dev/null` AND `disown` the SSH
  call hangs because the child inherits FDs.
- Use **`python3.11` explicitly** (hard rule #1): `python3` symlinks to 3.10 but pip installs to 3.11 →
  `ModuleNotFoundError`.

### Test via the stable HTTP proxy
```bash
curl -s https://<POD_ID>-<PORT>.proxy.runpod.net/health
```
The proxy URL is **stable across restarts** (same pod, same private port) even though the SSH port churns.

### Cement (before the demo) — non-negotiable
> ⚠️ **Nothing on a running pod survives a redeploy.** `/app` is ephemeral; only `/runpod-volume` persists.
> The local copy is the source of truth. **Never SCP as the delivery mechanism.**

When the loop converges:
1. Distill the install/CMD history into the **Dockerfile** (prod base `python:3.11-slim`, not the dev
   `runpod/base`).
2. `docker buildx build --platform linux/amd64 -t zackmckennarunpod/flash-control:latest --push .`
3. Recreate the prod pod from that image (§4). The deployed pod now boots exactly what we demo.
4. Do this **the night before**, then only touch the live loop for tiny safe tweaks on the day.

---

## 4. Pod provisioning (control-plane CPU pod)

### CRITICAL footgun: `mcp__runpod__create-pod` is GPU-only
The MCP `create-pod` tool schema has `gpuCount`/`gpuTypeIds` but **no CPU instance field** — it cannot
create a CPU pod. CPU pods require the **`deployCpuPod` GraphQL mutation** directly. Plan for GraphQL, not
the MCP tool, for the control plane. (Use `mcp__runpod__create-pod` only for a throwaway GPU scratch pod.)

```graphql
# api.runpod.io/graphql (prod), Bearer runpod-api-key-prod
mutation {
  deployCpuPod(input: {
    cloudType: SECURE
    instanceId: "cpu3c-1-2"          # ~$0.03/hr; container disk caps ~10GB on smallest flavor
    imageName: "zackmckennarunpod/flash-control:latest"
    dataCenterId: "EU-RO-1"          # same DC as Flash + volume to keep calls in-region
    containerDiskInGb: 10
    volumeInGb: 10                    # SQLite + dashboard state live here
    volumeMountPath: "/runpod-volume"
    ports: "22/tcp,8080/http"        # 22 for ssh-loop, 8080 for HTTP adapter/dashboard
    env: [
      { key: "RUNPOD_API_KEY", value: "<rpa_...>" }
      { key: "AUTH_TOKEN", value: "<bearer>" }
    ]
  }) { id }
}
```

### Ingress — built-in Runpod proxy (zero setup, TLS)
- Pattern: **`https://{podId}-{privatePort}.proxy.runpod.net`** — note it uses the **private/container
  port** (e.g. `8080`), not a remapped public port. TLS terminated by Runpod, no cert work.
- This is the demo URL and it's **stable across pod restarts**.
- **Cloudflare Tunnel only if the proxy bites** (per `notes/deployment.md`): stable custom domain,
  websockets/SSE the proxy buffers, or Cloudflare Access as free auth. Have the recipe ready, don't deploy
  it pre-emptively.

### Ports
- Declare every port you need at create time: `22/tcp` (SSH) + `8080/http` (adapter). HTTP ports get a
  proxy URL; TCP ports (22) get a dynamic public port for SSH.
- For the **MCP-stdio** theme you need **no public ingress at all** — the MCP process runs over stdio and
  calls Flash outbound. Only the HTTP theme needs the proxy + `AUTH_TOKEN`.

### Env vars
- `RUNPOD_API_KEY` — Flash SDK auth (prod key, `rpa_...`).
- `AUTH_TOKEN` — single bearer the HTTP adapter checks. Skip identity/OAuth (hackathon scope).

### Registry auth (only if the control image goes private)
`mcp__runpod__create-container-registry-auth { name, username: "zackmckennarunpod", password: <dockerhub-PAT> }`,
then reference it on the pod/template. Public Docker Hub images need none — keep it public for simplicity.

---

## 5. Network-volume staging (model weights, before the day)

Weights staging has **real wall-clock cost** and **EU-RO-1 is the only Flash DC** — do this days ahead, not
on stage. No model-cache support in Flash (research 00) → the network volume *is* the weight cache.

### Create the volume (EU-RO-1)
```
mcp__runpod__create-network-volume {
  name: "flash-weights",
  size: 200,                 # GB, 1–4000; size for SDXL+Whisper+embeddings+small-LLM headroom
  dataCenterId: "EU-RO-1"
}
```
(Or Flash `NetworkVolume(name, size, dataCenterId="EU-RO-1")` — idempotent by name; Flash can't delete it.)

### Stage weights onto it
1. Spin a **temporary GPU pod in EU-RO-1** with the volume mounted (verify mount path — docs say
   `/runpod-volume`, but confirm on the pod; research flagged this ⚠).
2. Pre-download weights into the volume (HF token in keychain `hf-token` / Runpod secret `hf_token` for
   gated models):
   ```bash
   HF_HOME=/runpod-volume/hf huggingface-cli download <repo> --local-dir /runpod-volume/<model>
   ```
3. Point primitives at the volume: set resource `networkVolumeId=<id>` and have the handler load from the
   mounted path. With weights pre-staged, cold start drops to worker-boot + dep-install (no multi-GB
   download), and warm calls are ~1–2 s.
4. Terminate the staging pod (volume persists independently).

> Cold-start math (research 00 / prep-kit): the ~1-min first call is **dep install + weight download**, not
> compute (`Delay 51842 ms` vs `Exec 1533 ms`). Pre-staging weights + lean deps (`workersMin≥1`) is what
> makes the warm-pool demo feel instant.

---

## 6. Platform footguns (what eats hackathon time)

**Pod / SSH lifecycle**
- **Pods silently restart during boot** — `runtime.ports` flips to `null`, `uptimeInSeconds:0`. Re-resolve
  host/port every SSH op; never cache.
- **SSH port changes on every start** — always re-query GraphQL.
- **SSH-ready ≠ port-up** — port 22 registers 5–30 s before sshd accepts; probe with real `ssh echo ok`.
- **`ssh.runpod.io` gateway lags 5+ min** with intermittent PTY issues — use direct `root@{ip} -p {port}`.
- **Non-`runpod/base` images infinite-restart on CPU pods** (no foreground process) — use `runpod/base` for
  dev, ensure the prod control image's CMD is a **foreground** long-running server (not a backgrounded one).

**Ephemerality**
- **`/app` (and anything outside the volume) is wiped on redeploy** — cement to the image before the demo;
  `/runpod-volume` is the only thing that persists. Never rely on SCP'd state.

**Networking / ingress**
- **`*.proxy.runpod.net` is HTTP-only** — won't carry SSH/TCP.
- **Proxy URL uses the private port**, not a public remap (`{podId}-{8080}.proxy.runpod.net`).
- **Raw GraphQL from automation/datacenter IPs → 403 Cloudflare** (research 00) — use the SDK / an authed
  client, not scraping.

**CUDA / GPU (only if baking a worker base)**
- **Bare `pip install torch` → cu130 → silent CPU fallback (10× slower).** Always pin
  `--index-url .../whl/cu124`.
- **`torch is unavailable under `flash dev`** (Live Serverless) — works only in
  `flash deploy`. Don't plan torch/GPU demos on `flash dev`; test the torch path via `flash deploy` early.
- **Multi-endpoint build is all-or-nothing** — one `@Endpoint`'s transitive dep with no prebuilt wheel fails
  the *whole* build. Keep deps lean, vet wheels, isolate risky endpoints.

**Python / deps**
- Use **`python3.11` explicitly**; `python3` → 3.10, pip installs to 3.11.
- **`pip install --no-cache-dir` always** — small pod disks fill fast.

**Container permissions** (skill unavailable locally — essentials):
- `runpod/base` images run as **root (UID 0)**; some slim/prod bases may run non-root → files written to a
  network volume can end up with permissions another worker can't read. Standardize on root or `chown` the
  volume paths at boot.
- Pre-create + `chmod` the volume subdirs (`/runpod-volume/hf`, `/runpod-volume/db`) so SQLite and HF cache
  aren't blocked by a read-only or wrong-owner mount.

**Flash hygiene**
- **`flash deploy` doesn't print the endpoint URL** — our control plane should surface it (registry).
- **dev→deploy leaves orphan endpoints** (one user had 6, no auto-cleanup) — keep a teardown script.
- **Cold starts rough at low max-workers** — raise `workersMax`, set `workersMin≥1` for the warm pool.

---

## Recommended concrete spec (TL;DR)

- **Control-plane image:** `FROM python:3.11-slim`; `pip install --no-cache-dir runpod-flash`; bake the
  `kit/` spine (shared core + MCP-stdio + HTTP adapters + fan-out helper + cost readout + dashboard +
  availability-deploy helper + SQLite schema) at `/app`; read `RUNPOD_API_KEY` + `AUTH_TOKEN` from env;
  foreground server CMD. Build amd64 → `zackmckennarunpod/flash-control:latest`.
- **Dev pod (iterate):** `runpod/base:0.6.2-cuda12.4.1`, CPU `cpu3c-1-2`, EU-RO-1, ports `22/tcp,8080/http`,
  ssh-loop via direct TCP (re-resolve every loop, `rsync -av` no `-z`).
- **Prod pod:** `deployCpuPod` GraphQL (MCP `create-pod` can't do CPU) from the control image, EU-RO-1,
  volume `/runpod-volume`, proxy URL `https://{podId}-8080.proxy.runpod.net`.
- **GPU worker base:** reuse `zackmckennarunpod/flash-ts:latest` (amd64, the TS SDK default); only fork it to
  pre-bake heavy deps, pinning torch `cu124`.
- **Network volume:** `flash-weights`, ~200 GB, **EU-RO-1**, stage Whisper/SDXL/embeddings via a temp
  GPU pod days ahead; primitives mount it with `networkVolumeId`.
- **Cement the night before; never SCP as delivery; nothing off-volume survives redeploy.**
