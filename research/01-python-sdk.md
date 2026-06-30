> ⚠ **STALE API WARNING** — read from local **v1.4.2** checkout. Current Flash is **v1.17.0** with a different API (`@Endpoint(...)`, not `@remote`/`LiveServerless`). Use for SDK internals/mechanics only. For current API, trust `../KNOWLEDGE.md` and `_skills-repo/flash/SKILL.md`.

# Flash Python SDK (runpod-flash) — Reference Digest
> Source: /work/flash (src/runpod_flash, docs/*.md), /work/tetra-rp. Agent-extracted; verify ⚠ items against source before relying.

(Captured from deep-dive agent — see conversation for full text. Key facts below; re-verify network-volume API & "gotchas" against source.)

## Install / auth
- `pip install runpod-flash` (v1.4.2; Python 3.10–3.14). Deps: cloudpickle>=3.1.1, runpod, pydantic>=2, typer, rich.
- Auth: `RUNPOD_API_KEY` env > `.env` > `~/.config/runpod/credentials.toml` (`flash login` OAuth).
- CLI: `flash login | init [dir] | run [--auto-provision] | build | deploy --env <env> | undeploy | env list/create/delete | app ...`

## @remote
- `remote(resource_config, dependencies=[pip], system_dependencies=[apt], accelerate_downloads=True, local=False, method=None, path=None)`
- Always awaited. Function serialized via cloudpickle ⇒ ONLY params/locals/inside-fn imports/builtins. NO module globals/imports/helpers. ⚠ #1 footgun.
- Args/returns JSON-serializable (queue) — pass URLs not file handles.
- LB endpoints: pass method+path; returns raw dict. Queue: returns JobOutput.

## Resource configs (Pydantic, from stubs/ + config.py)
LiveServerless / ServerlessEndpoint / CpuLiveServerless / CpuServerlessEndpoint / LiveLoadBalancer / LoadBalancerSlsResource (+Cpu variants).
Common: name, workersMin=0, workersMax=3, idleTimeout=60, scalerType=QUEUE_DELAY, scalerValue=4, env={}, networkVolumeId, datacenter=EU_RO_1.
GPU: gpus=[GpuGroup...], cudaVersions=[]. CPU: cpuCount, instanceIds.
GpuGroup: ANY, AMPERE_16/24/48/80, ADA_24/32_PRO/48_PRO/80_PRO, HOPPER_141.
- ⚠ Live* names get `live-` prefix / `-fb` suffix; config changes don't update existing endpoints (immutable by name+hash).

## JobOutput (queue) — telemetry
id, workerId, status, delayTime(ms, queue+coldstart), executionTime(ms), output, error. Total latency = delay+exec. Always check `.error`.

## Cold start
Worker boot 5–10s + dep install 20–60s (torch dominates) + exec 1–5s. Warm ~1–2s. Minimize: lean deps (exclude torch), workersMin=1, pre-stage weights on network volume, accelerate_downloads, stable names.

## Network volumes ⚠ verify API
NetworkVolume(name,size,dataCenterId="EU-RO-1"); deploy first; mount via networkVolumeId; mounted under /runpod-volume (⚠ verify path). EU-RO-1 only. Idempotent by name. No delete via Flash.

## Fan-out — no .map()
`await asyncio.gather(*[fn(x) for x in items])`; cap with asyncio.Semaphore(workersMax).

## Deploy model
flash build → scans @remote → manifest + artifact.tar.gz in .flash/. flash deploy → upload S3 → provision one endpoint per config. Peer-to-peer discovery via ServiceRegistry/State Manager. .flash/ holds config.json, deployments.json, artifact.
