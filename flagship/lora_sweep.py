"""FLAGSHIP BEAT — LoRA hyperparameter sweep fanned across GPU workers.

The Tier-1 "couldn't-do-this-without-a-GPU" moment: an agent fans out N fine-tuning
configs across N live GPU workers, each trains a tiny LoRA, and we pick the winner by
eval loss — minutes, not a day. This is the differentiator vs every "I called SDXL"
demo: real GPU work *beyond inference*, parallelized by Flash.

Design notes:
  - The training function is authored as a STRING and minted, exactly like any agent
    tool — so the demo narrative ("the agent wrote and deployed this") holds.
  - Deps install on the worker (Flash `dependencies=[...]`), NOT locally. torch is the
    big one; keep the rest lean (transformers/peft/datasets) to avoid the all-or-nothing
    build trap. Stage base weights on a NetworkVolume for repeat runs.
  - Each config is one payload; `forge.fanout` runs them concurrently with a worker cap.
  - SAFE-DEFAULT scale: tiny model + tiny steps so a sweep finishes fast and cheap.
    Crank `model`/`max_steps` for a beefier demo once the path is proven.

Run (after `forge.load_env(...)`):
    python -m flagship.lora_sweep
"""

from __future__ import annotations

import asyncio

import forge

# Authored-as-string so it ships as a minted tool. EVERYTHING the handler uses is
# imported INSIDE the body (Flash ships only the body — KNOWLEDGE.md gotcha #1).
TRAIN_CODE = '''
def handler(config):
    # config: {"lr": float, "rank": int, "max_steps": int, "model": str}
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments, Trainer
    from peft import LoraConfig, get_peft_model
    from datasets import load_dataset

    model_name = config.get("model", "sshleifer/tiny-gpt2")
    tok = AutoTokenizer.from_pretrained(model_name)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    base = AutoModelForCausalLM.from_pretrained(model_name).to("cuda")

    lora = LoraConfig(r=config.get("rank", 8), lora_alpha=16, lora_dropout=0.05, task_type="CAUSAL_LM")
    model = get_peft_model(base, lora)

    ds = load_dataset("imdb", split="train[:64]")
    def tokenize(batch):
        out = tok(batch["text"], truncation=True, padding="max_length", max_length=64)
        out["labels"] = out["input_ids"].copy()
        return out
    ds = ds.map(tokenize, batched=True, remove_columns=ds.column_names)
    ds.set_format("torch")

    args = TrainingArguments(
        output_dir="/tmp/lora_out", per_device_train_batch_size=4,
        max_steps=config.get("max_steps", 10), learning_rate=config["lr"],
        logging_steps=1, report_to=[], save_strategy="no",
    )
    trainer = Trainer(model=model, args=args, train_dataset=ds)
    result = trainer.train()
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return {
        "config": config,
        "final_loss": float(result.training_loss),
        "trainable_params": trainable,
        "gpu": torch.cuda.get_device_name(0),
    }
'''

SWEEP = [
    {"lr": 5e-4, "rank": 4, "max_steps": 10, "model": "sshleifer/tiny-gpt2"},
    {"lr": 1e-3, "rank": 8, "max_steps": 10, "model": "sshleifer/tiny-gpt2"},
    {"lr": 2e-3, "rank": 8, "max_steps": 10, "model": "sshleifer/tiny-gpt2"},
    {"lr": 1e-3, "rank": 16, "max_steps": 10, "model": "sshleifer/tiny-gpt2"},
]


async def run_sweep(profile: str = "prod", configs: list[dict] | None = None) -> dict:
    forge.load_env(profile)
    configs = configs or SWEEP

    # One pool of workers sized to the sweep so every config trains in parallel.
    tool = forge.mint(
        "lora-sweep",
        code=TRAIN_CODE,
        gpu="AMPERE_24",  # 24GB is plenty for a tiny LoRA; pick via forge.pick() for stock
        dependencies=["torch", "transformers", "peft", "datasets", "accelerate"],
        workers=(0, len(configs)),
        idle_timeout=30,
    )
    registry = forge.Registry()

    print(f"fanning out {len(configs)} LoRA configs across up to {len(configs)} workers ...")
    results = await forge.fanout(tool, configs, registry=registry)

    trained = [r.output for r in results if r.ok]
    failed = [r.error for r in results if not r.ok]
    winner = min(trained, key=lambda o: o["final_loss"]) if trained else None

    rollup = forge.summarize([{"gpu": tool.gpu, "seconds": r.seconds, "ok": r.ok} for r in results])
    print(f"winner: {winner}")
    print(f"cost rollup: {rollup}")
    if failed:
        print(f"{len(failed)} failed: {failed}")

    await forge.undeploy("lora-sweep")  # server-truth teardown, scoped to this tool
    return {"winner": winner, "all": trained, "failed": failed, "rollup": rollup}


if __name__ == "__main__":
    asyncio.run(run_sweep())
