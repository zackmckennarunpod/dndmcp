"""Cost / latency telemetry — the "cost awareness" rubric beat.

Flash's `EndpointJob` in 1.7.0 exposes output/error/status but NOT delay/exec
timing, so we measure wall-clock latency client-side (always correct) and derive
cost = rate($/hr) x seconds x workers. If a future Flash exposes real delay/exec
ms, feed them into `cost_usd` instead of wall-clock — the math is identical.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# APPROXIMATE Runpod serverless *flex* $/hr per GpuGroup. These are estimates — VERIFY
# against current pricing before quoting on stage. Override with a rates.json next to
# the state dir, or via FORGE_RATES_JSON.
DEFAULT_GPU_RATES_USD_PER_HR: dict[str, float] = {
    "ADA_24": 0.69,       # RTX 4090
    "ADA_32_PRO": 0.90,   # RTX 5090
    "ADA_48_PRO": 0.79,   # L40 / L40S / 6000 Ada
    "ADA_80_PRO": 2.99,   # H100
    "AMPERE_16": 0.34,    # A4000 / A4500
    "AMPERE_24": 0.43,    # A5000 / L4 / 3090
    "AMPERE_48": 0.59,    # A40 / A6000
    "AMPERE_80": 1.19,    # A100 80GB
    "HOPPER_141": 3.99,   # H200
    "ANY": 0.69,
}


def _rates() -> dict[str, float]:
    override_path = os.environ.get("FORGE_RATES_JSON")
    if override_path and Path(override_path).is_file():
        loaded = json.loads(Path(override_path).read_text())
        return {**DEFAULT_GPU_RATES_USD_PER_HR, **loaded}
    return DEFAULT_GPU_RATES_USD_PER_HR


def cost_usd(gpu: str, seconds: float, workers: int = 1) -> float:
    """$ for `seconds` of `workers` GPUs of group `gpu`."""
    rate = _rates().get(gpu, DEFAULT_GPU_RATES_USD_PER_HR["ANY"])
    hours = seconds / 3600.0
    return rate * hours * workers


def idle_burn_usd_per_hr(gpu: str, workers_min: int) -> float:
    """Ongoing $/hr a pinned-warm pool costs while idle — the number cleanup drops."""
    return _rates().get(gpu, DEFAULT_GPU_RATES_USD_PER_HR["ANY"]) * max(workers_min, 0)


def summarize(call_records: list[dict]) -> dict:
    """Aggregate a flat list of call records ({gpu, seconds, ok, workers}) into a
    dashboard-ready rollup: total $, p50/p99 latency, success rate, $/call."""
    if not call_records:
        return {"calls": 0, "total_usd": 0.0, "p50_s": 0.0, "p99_s": 0.0, "success_rate": 1.0}

    latencies = sorted(r["seconds"] for r in call_records)
    total = sum(cost_usd(r["gpu"], r["seconds"], r.get("workers", 1)) for r in call_records)
    ok = sum(1 for r in call_records if r.get("ok", True))

    def pct(p: float) -> float:
        idx = min(len(latencies) - 1, int(p * len(latencies)))
        return round(latencies[idx], 3)

    return {
        "calls": len(call_records),
        "total_usd": round(total, 6),
        "usd_per_call": round(total / len(call_records), 6),
        "p50_s": pct(0.50),
        "p99_s": pct(0.99),
        "success_rate": round(ok / len(call_records), 3),
    }
