"""Canonical vLLM-on-Flash — the pattern from tetra-rp's own working example.

NOT the worker-v1-vllm image. Instead: a @Endpoint CLASS with dependencies=["vllm"],
model loaded ONCE in __init__ (persistence), with the stability env vars that make vLLM
work on serverless (VLLM_USE_V1=0, spawn, enforce_eager). This is what actually works.

    python -m scripts.deploy_vllm_class [--keep]
"""

from __future__ import annotations

import asyncio
import sys
import time

import forge  # for env/auth + teardown
from runpod_flash import Endpoint, GpuGroup

MODEL = "Qwen/Qwen2.5-1.5B-Instruct"


# PIN vLLM to a CUDA-12 build — the latest vllm needs libcudart.so.13 (CUDA 13) which the
# runpod/flash:latest worker container does NOT have. 0.7.x is CUDA-12.1 + supports Qwen2.5.
# vllm 0.7.3 = CUDA-12 (avoids libcudart.so.13) + needs transformers 4.48.2 (else
# 'Could not import ProcessorMixin'). Pin both to a matched pair.
@Endpoint(name="dnd-llm", gpu=GpuGroup.AMPERE_24, workers=(1, 1),
          idle_timeout=300, dependencies=["vllm==0.7.3", "transformers==4.48.2"])
class DnDLLM:
    def __init__(self):
        import os
        from vllm import LLM, SamplingParams
        os.environ["VLLM_USE_V1"] = "0"                       # stability: old engine
        os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
        self.llm = LLM(model="Qwen/Qwen2.5-1.5B-Instruct",
                       enforce_eager=True,                    # disable CUDA graphs
                       gpu_memory_utilization=0.6,
                       max_model_len=2048)
        self.SamplingParams = SamplingParams
        print("vLLM ready")

    def chat(self, messages: list, max_tokens: int = 256, temperature: float = 0.9):
        # build a simple chat prompt (works for Qwen instruct)
        parts = []
        for m in messages:
            parts.append(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>")
        prompt = "\n".join(parts) + "\n<|im_start|>assistant\n"
        sp = self.SamplingParams(temperature=temperature, max_tokens=max_tokens,
                                 stop=["<|im_end|>"])
        out = self.llm.generate([prompt], sp)
        return {"text": out[0].outputs[0].text.strip()}


async def main(keep: bool = False) -> int:
    forge.load_env("prod")
    print("deploying canonical vLLM CLASS endpoint (dependencies=[vllm]) ...")
    t0 = time.time()
    ok = False
    try:
        llm = DnDLLM()  # instantiating provisions + loads the model remotely (first call slow)
        print("calling chat (cold start: vllm install + model load, minutes) ...")
        r = await llm.chat(
            messages=[{"role": "system", "content": "Reply with STRICT JSON only."},
                      {"role": "user", "content": 'Generate a dungeon room. JSON: {"name":"...","look":"..."}'}],
            max_tokens=120)
        print(f"\n  ✅ RESPONSE in {time.time()-t0:.0f}s:\n  {r}")
        ok = True
    except Exception as exc:
        print(f"  ERROR: {type(exc).__name__}: {str(exc)[:400]}")
    finally:
        if keep:
            print("\n  --keep: leaving dnd-llm warm.")
        else:
            print("\n  teardown ...")
            print("  ", await forge.undeploy("dnd-llm"))
    print("\nRESULT:", "vLLM CLASS WORKS ✅" if ok else "see error")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(keep="--keep" in sys.argv)))
