"""CEO-Bench benchmark adapter: bridge harness runs to Harbor trial dirs."""

from library.adapters.ceo_bench.bridge import (
    AGENT_COST_PURPOSE,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    CeoBenchBridgeError,
    bridge_ceo_bench_run,
    is_completed_week_advance,
    is_state_changing_call,
)

__all__ = [
    "AGENT_COST_PURPOSE",
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
    "CeoBenchBridgeError",
    "bridge_ceo_bench_run",
    "is_completed_week_advance",
    "is_state_changing_call",
]
