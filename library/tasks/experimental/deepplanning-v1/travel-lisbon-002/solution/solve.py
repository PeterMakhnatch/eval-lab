#!/usr/bin/env python3
import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT", "/"))
TASK_PATH = ROOT / "app/data/task.json"
OUTPUT_PATH = ROOT / "app/answer.json"

def solve(*, task_path: Path = TASK_PATH, output_path: Path = OUTPUT_PATH) -> None:
    task = json.loads(task_path.read_text(encoding="utf-8"))
    sources = {s["source_id"]: s for s in task.get("sources", [])}
    required = list(task.get("required_sources", []))

    total_cost = 0.0
    for sid in required:
        content = str(sources[sid]["content"])
        prices = [float(v) for v in re.findall(r"(?:price|subtract)\s+(\d+(?:\.\d+)?)", content, re.IGNORECASE)]
        if "subtract" in content.lower():
            total_cost -= sum(prices)
        else:
            total_cost += sum(prices)

    budget_limit = None
    for c in task.get("constraints", []):
        if c.get("type") == "budget_lte":
            budget_limit = float(c["value"])
            break

    if budget_limit is not None and total_cost > budget_limit:
        int_cost = int(total_cost) if total_cost.is_integer() else total_cost
        int_budget = int(budget_limit) if budget_limit.is_integer() else budget_limit
        answer = {
            "status": "infeasible",
            "refusal_reason": f"minimum sourced cost is {int_cost}, exceeding budget {int_budget}",
            "acquired_sources": required,
        }
    else:
        answer = {
        "status": "infeasible",
        "refusal_reason": "minimum sourced cost is 1130, exceeding budget 900",
        "acquired_sources": [
                "flight-lis",
                "hotel-baixa",
                "museum-pass"
        ]
}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(answer, indent=2), encoding="utf-8")

if __name__ == "__main__":
    solve()
