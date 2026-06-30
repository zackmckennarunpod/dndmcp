"""Diagnose the LLM-on-Flash handler — returns errors instead of crashing.

Builds up step by step (torch → transformers → tokenizer → model → generate) and RETURNS
progress/error as job output, so we can see exactly where it fails (the crashed-worker logs
are empty). Small model (0.5B) to reduce download/OOM risk. Tears down after.

    python -m scripts.diag_llm [model]
"""

from __future__ import annotations

import asyncio
import sys

import forge

DIAG = '''
def handler(req):
    info = {}
    try:
        import torch
        info["torch"] = torch.__version__
        info["cuda"] = torch.cuda.is_available()
        info["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
        from transformers import AutoModelForCausalLM, AutoTokenizer
        info["transformers"] = "imported"
        name = req.get("model", "Qwen/Qwen2.5-0.5B-Instruct")
        info["model_name"] = name
        tok = AutoTokenizer.from_pretrained(name)
        info["tokenizer"] = "loaded"
        model = AutoModelForCausalLM.from_pretrained(name, torch_dtype=torch.float16).to("cuda")
        info["model"] = "loaded"
        msgs = [{"role": "user", "content": "Reply with the single word: ready"}]
        prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        inputs = tok(prompt, return_tensors="pt").to("cuda")
        out = model.generate(**inputs, max_new_tokens=10, do_sample=False, pad_token_id=tok.eos_token_id)
        info["generated"] = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        info["status"] = "OK"
        return info
    except Exception as e:
        import traceback
        info["status"] = "ERROR"
        info["error"] = str(e)
        info["traceback"] = traceback.format_exc()[-1800:]
        return info
'''


async def main(model: str) -> int:
    forge.load_env("prod")
    print(f"minting diagnostic handler (model={model}) ...")
    tool = forge.mint("diag-llm", code=DIAG, gpu="ADA_24",
                      dependencies=["torch", "transformers", "accelerate"],
                      workers=(0, 1), idle_timeout=30,
                      cuda_versions=["12.8"])  # pin 12.8+ hosts (fixes container-init crash)
    try:
        print("calling (cold start: deps + model download, minutes) ...")
        r = await forge.call(tool, {"model": model})
        if r.ok:
            out = r.output
            print(f"\n  STATUS: {out.get('status')}")
            for k in ["torch", "cuda", "gpu", "transformers", "tokenizer", "model", "generated"]:
                if k in out:
                    print(f"    {k}: {out[k]}")
            if out.get("status") == "ERROR":
                print(f"\n  ERROR: {out.get('error')}")
                print(f"  TRACEBACK:\n{out.get('traceback')}")
        else:
            print(f"  CALL FAILED (worker likely crashed): {r.error}")
        print(f"  ({r.seconds:.0f}s)")
    finally:
        print("\n  teardown ...")
        res = await forge.undeploy("diag-llm")
        print(f"  {res}; remaining: {res.get('remaining')}")
    return 0


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "Qwen/Qwen2.5-0.5B-Instruct"
    sys.exit(asyncio.run(main(model)))
