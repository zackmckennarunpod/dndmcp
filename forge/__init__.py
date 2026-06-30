"""FORGE — GPU-on-tap for agents.

An agent-native control layer over Runpod Flash. Five verbs an agent gets:
  available -> discover live GPU stock (fills the SDK gap)
  mint      -> code string -> live GPU endpoint in ~60s (the moat)
  call/fanout -> run it, with cost + latency telemetry
  cleanup   -> tear it all down (kills orphan sprawl)

Heavy imports (runpod_flash) are lazy inside functions so `import forge` works
without env wired. Call `forge.load_env(...)` once before deploying.
"""

from __future__ import annotations

from .availability import available_gpus, pick
from .cost import cost_usd, idle_burn_usd_per_hr, summarize
from .env import active_profile, load_env
from .minting import MintedTool, materialize_handler, mint, unique_endpoint_name, warm
from .registry import Registry
from .run import CallResult, call, fanout
from .diagnostics import diagnose, endpoint_full, fetch_logs, logs
from .teardown import (
    clear_local_cache,
    delete_endpoint,
    server_endpoints,
    undeploy,
    undeploy_tools,
)

__all__ = [
    "load_env", "active_profile",
    "available_gpus", "pick",
    "mint", "warm", "MintedTool", "materialize_handler", "unique_endpoint_name",
    "call", "fanout", "CallResult",
    "cost_usd", "idle_burn_usd_per_hr", "summarize",
    "Registry",
    "delete_endpoint", "undeploy", "undeploy_tools", "server_endpoints", "clear_local_cache",
    "diagnose", "logs", "fetch_logs", "endpoint_full",
]
