# 2026 Landscape — where FORGE sits (live web research, Jun 26)

## Verdict: the FORGE core is genuine white space (not invented)
Two independent research passes agree: **nobody owns "an agent mints a durable, callable GPU tool/endpoint at runtime, exposed back over MCP, with cost metering + teardown."**

The market splits into two camps, neither of which is this:
- **Ephemeral GPU sandboxes for agent code-exec** — Modal (the leader: OpenAI Agents SDK's GPU sandbox provider, gVisor, GPU memory-snapshot cold starts ~10x), Beam (sub-10s snapshots), E2B (CPU/Firecracker, GPU only DIY), Cerebrium (2–4s). These run code then vanish — no durable endpoint.
- **Human-deployed inference endpoints** — Baseten (closing ~$1.5B @ ~$12B), Fal (media, ~$8B raise), Together (model serving), Replicate (acquired by Cloudflare → Workers AI). A human clicks deploy.

**Runpod's own MCP server** is control-plane only — it provisions pods/endpoints/volumes but *explicitly cannot run arbitrary GPU code or mint tools*. It's chatops, not a compute plane.

→ The unclaimed lane = **agent grows its own GPU toolset on demand.** Flash's ~60s no-Docker runtime deploy is the exact primitive nobody has fused with MCP + cost + teardown. That fusion is FORGE.

## It rides three confirmed 2026 trends
- **Self-extending agents / meta-tooling / CodeAct** — AWS Strands self-extending CLIs, Microsoft Agent Framework (Build 2026). Live trend.
- **MCP resurgence** — Anthropic's 2026 Agentic Coding report calls it out, alongside long-running agents + multi-agent fan-out.
- **GPU compute for agents** — the hottest infra fight (Modal etc.), but all ephemeral.

## Sharper pitch (use this language)
> "Modal gives an agent a GPU *scratchpad*. Baseten gives a *human* a deploy button. Flash + FORGE gives an agent a GPU *tool factory* — it grows its own callable, cost-metered GPU toolset at runtime. Nobody else does this."

## Flagship reality-check (the research changed the picks)
- **Plain LoRA sweep → commodity.** Serverless fine-tuning already shipped (SageMaker Serverless Customization, ServerlessLLM). Reads as "I rented GPUs." Only novel if reframed as **GRPO/RLVR / RL-environment fan-out** (DeepSeek-R1's algorithm — the hot 2026 layer).
- **"LLM writes a kernel" → approaching done-to-death** to a GPU crowd. 2026 already has KernelBench v3/X, NVIDIA CUDA Agent, PyTorch KernelAgent (1.56x > torch.compile), AutoKernel. STILL impressive only if it leans on Flash's real edge: **cheap massively-parallel autotuning across many REAL GPUs, live measured speedup on an audience-picked memory-bound kernel** (RMSNorm 5.29x, softmax 2.82x are reliable wins; matmul still loses to cuBLAS — avoid).
- **Higher-ceiling crowd-pleaser → world models / real-time interactive video gen** (Genie 3, Marble, NVIDIA Cosmos). Biggest "impossible without a GPU" wow, but heavy + latency-sensitive = demo risk.

**Flagship ranking by (novelty × wow × achievability):**
1. **Parallel kernel autotuning on real GPUs** — Flash's parallel angle is the fresh part; crowded but defensible; medium build risk (triton+torch).
2. **GRPO/RL-environment fan-out** — hottest algorithm, novel framing, leans on fan-out; higher build complexity (trl/vllm, all-or-nothing build risk).
3. **World model / interactive video** — max wow, max risk.

**Bottom line:** the *spine* (FORGE) is the winner and is confirmed white space — the flagship is depth, not the thesis. Pick a flagship that visibly uses Flash's parallel-real-GPU fan-out; keep it achievable.

## Sources
Modal sandboxes (modal.com/resources/best-gpu-enabled-sandboxes-ai-agents), Replicate→Cloudflare (blog.cloudflare.com/why-replicate-joining-cloudflare), Baseten raise (finsmes.com 2026/06), Beam (beam.cloud/blog/2026-sandbox-guide), Runpod Flash + MCP (runpod.io/blog introducing-flash; docs.runpod.io/get-started/mcp-servers), Cloudflare Sandboxes GA (infoq.com 2026/04), Strands self-extending (noise.getoto.net 2026/05), MS Agent Framework Build 2026 (devblogs.microsoft.com), Anthropic 2026 Agentic Coding Trends, KernelAgent (pytorch.org/blog), AutoKernel (marktechpost.com 2026/04), KernelBench, serverless FT (github ServerlessLLM; aws sagemaker), world models (spheron.network, ai.cc 2026).
