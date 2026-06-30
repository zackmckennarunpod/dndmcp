"""Flash LLM — the GPU content factory the MCP proxies to.

Mints (once) a Flash GPU endpoint running a small fast instruct model, and exposes
`generate(messages)`. Built on our PROVEN forge kit (mint/call/teardown) rather than
unproven vLLM image config — reliability-first given Flash's flakiness.

Warm-caching: the handler stashes the loaded model in the worker's persistent namespace
(`sys.modules['__main__']`) so it loads ONCE per warm worker, not per call. With workers
pre-warmed (workers_min>=1), per-call generation is ~1-2s.

Off by default (DND_FLASH_LLM!=1) → callers fall back to procedural generation, so the
game always works. Flip on to make world-gen run live through Flash.
"""

from __future__ import annotations

import os

MODEL = os.environ.get("DND_LLM_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
ENABLED = os.environ.get("DND_FLASH_LLM", "0") == "1"
_TOOL = {"obj": None}

# Handler runs on the Flash GPU worker. Caches the model in the worker's persistent
# namespace so it survives across calls. Reads model name from the worker env.
HANDLER = '''
def handler(req):
    import os, sys, torch
    cache = sys.modules["__main__"].__dict__.setdefault("_DND_LLM", {})
    if "model" not in cache:
        from transformers import AutoModelForCausalLM, AutoTokenizer
        name = os.environ.get("DND_LLM_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
        cache["tok"] = AutoTokenizer.from_pretrained(name)
        cache["model"] = AutoModelForCausalLM.from_pretrained(
            name, torch_dtype=torch.float16, device_map="cuda")
    tok, model = cache["tok"], cache["model"]
    messages = req["messages"]
    prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tok(prompt, return_tensors="pt").to("cuda")
    with torch.no_grad():
        out = model.generate(
            **inputs, max_new_tokens=int(req.get("max_tokens", 250)),
            temperature=float(req.get("temperature", 0.9)), do_sample=True,
            pad_token_id=tok.eos_token_id)
    text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    return {"text": text.strip(), "device": torch.cuda.get_device_name(0)}
'''


async def ensure(profile: str = "prod"):
    """Mint + (lazily) warm the Flash LLM endpoint. Returns the minted tool."""
    import forge

    if _TOOL["obj"] is None:
        forge.load_env(profile)
        _TOOL["obj"] = forge.mint(
            "dnd-llm", code=HANDLER, gpu="ADA_24",
            dependencies=["torch", "transformers", "accelerate"],
            workers=(1, 1),           # pre-warmed: always 1 worker (no cold-start hang mid-game)
            idle_timeout=600,
            env={"DND_LLM_MODEL": MODEL},
        )
    return _TOOL["obj"]


async def generate(messages: list[dict], *, max_tokens: int = 250,
                   temperature: float = 0.9) -> str | None:
    """Generate via Flash. Returns None (→ caller falls back) if disabled or on error."""
    if not ENABLED:
        return None
    try:
        import forge
        tool = await ensure()
        r = await forge.call(tool, {"messages": messages, "max_tokens": max_tokens,
                                    "temperature": temperature})
        if r.ok and isinstance(r.output, dict):
            return r.output.get("text")
    except Exception:
        return None
    return None


async def warm() -> dict:
    """Force a deploy + one generation so the worker is hot before play/recording."""
    import forge
    await ensure()
    r = await forge.call(_TOOL["obj"], {"messages": [{"role": "user", "content": "Say 'ready'."}],
                                        "max_tokens": 8})
    return {"ok": r.ok, "via": "flash", "sample": (r.output or {}).get("text") if r.ok else r.error,
            "seconds": round(r.seconds, 1)}


async def teardown() -> dict:
    import forge
    if _TOOL["obj"] is not None:
        res = await forge.undeploy("dnd-llm")
        _TOOL["obj"] = None
        return res
    return {"deleted": 0}
