> ⚠ **STALE API WARNING** — read from local **v1.4.2** checkout. Current Flash is **v1.17.0** with a different API (`@Endpoint(...)`, not `@remote`/`LiveServerless`). Use for SDK internals/mechanics only. For current API, trust `../KNOWLEDGE.md` and `_skills-repo/flash/SKILL.md`.

# Flash TypeScript SDK (@runpod/flash) — Reference Digest
> Source: /work/runpod-flash-ts (branch feat/graphql-codegen-and-cli). Agent-extracted.

## Install
- `@runpod/flash` (v0.1.0), Node>=18, `bun add @runpod/flash`. Auth RUNPOD_API_KEY / `flash login`.
- GPU image hardcoded: `FLASH_TS_GPU_IMAGE` default `zackmckennarunpod/flash-ts:latest` (override via env).

## API
- `remote({resource})(async fn)` HOF, or `@remoteDecorator({resource})` class method.
- Resource classes mirror Python: LiveServerless, CpuLiveServerless, LiveLoadBalancer (+ prod variants), enums GpuGroup/CudaVersion/DataCenter/ServerlessScalerType.
- Execution modes: stub(local dev deploy+call) / local (RUNPOD_ENDPOINT_ID set) / LB route handler (Express).
- **Code-over-wire hot reload**: function source sent per call if not in registry ⇒ edit logic w/o redeploy. Helpers must be top-level decls.
- Experimental: LLM/vLLM model classes (OpenAI-compatible chat/embeddings).

## CLI (richer than Python)
`flash init [--cpu] | login | run [--port] | build | deploy [--env --dry-run --skip-build] | undeploy [--force] | app list/create/get/delete | env list/create/get/delete`

## GraphQL codegen
codegen.ts → api.runpod.dev/graphql (dev introspection on). documents src/graphql/*.graphql → src/generated/graphql.ts. `bun run codegen`. getSdk(client).
Ops: auth, apps (List/Get/Create/DeleteFlashApp), environments (Create/Update/Delete, AddEndpoint, AddNetworkVolume), builds (PrepareArtifactUpload/Finalize/UpdateManifest/DeployBuildToEnvironment), endpoints (Save/Delete/List, SaveTemplate).

## Notes
- Artifact tarball max 500MB. User deps isolated via NODE_PATH.
- Branch stable, ready to merge. This is the repo the user's CLAUDE.md says implements the codegen pattern.

## RECOMMENDATION (agent): TS for control plane (full CLI+typed GraphQL). BUT user's whole prep-kit & gpu-toolbelt primitives are Python. DECISION DEFERRED — see synthesis.
