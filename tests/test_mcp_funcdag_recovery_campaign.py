from __future__ import annotations

import json
from pathlib import Path

from evallab.queue import read_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "research/experiments/specs/mcp-funcdag-recovery-qualified-profile"


def test_qualified_mcp_campaign_is_bounded_and_not_authorized() -> None:
    campaign = json.loads((SPEC_ROOT / "campaign.json").read_text(encoding="utf-8"))
    specs = [read_spec(ROOT / relative) for relative in campaign["run_specs"]]

    assert campaign["schema_version"] == "mcp-funcdag-recovery-campaign/v1"
    assert campaign["status"] == "dependency_ready_not_authorized"
    assert campaign["authorization"]["model_run"] is False
    assert campaign["qualified_profile"]["profile_id"] == "zai-opencode-glm-5.3"
    assert campaign["qualified_profile"]["model"] == "zai-coding-plan/glm-5.3"
    assert all(spec.agent == "zai-opencode" for spec in specs)
    assert all(spec.model == "zai-coding-plan/glm-5.3" for spec in specs)
    assert all(spec.attempts == 1 and spec.concurrency == 1 for spec in specs)

    limits = campaign["campaign_limits"]
    assert sum(spec.max_requests or 0 for spec in specs) <= limits["max_requests"]
    assert sum(spec.max_input_tokens or 0 for spec in specs) <= limits["max_input_tokens"]
    assert sum(spec.max_output_tokens or 0 for spec in specs) <= limits["max_output_tokens"]
    assert sum(spec.max_total_tokens or 0 for spec in specs) <= limits["max_total_tokens"]
    assert sum(spec.cost_limit_usd or 0 for spec in specs) <= limits["max_cost_usd"]
    assert sum(spec.timeout_seconds for spec in specs) <= limits["max_wall_clock_seconds"]

    fault, clean = campaign["cells"][1:]
    assert fault["fault_record"]["persistence"] == 1
    assert clean["fault_record"]["persistence"] == 0
    assert fault["twin_task_id"] == clean["task_id"]
    assert clean["twin_task_id"] == fault["task_id"]
    assert "unavailable" in campaign["outcomes"]["failed_prefix_cost"]
    assert "Do not pool" in campaign["outcomes"]["persistent_pooling"]
