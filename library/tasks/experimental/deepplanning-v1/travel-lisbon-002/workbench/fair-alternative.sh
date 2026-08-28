#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import json
import os
import re
from pathlib import Path

root = Path(os.environ.get("APP_ROOT", "/"))
task = json.loads((root / "app/data/task.json").read_text(encoding="utf-8"))
sources = {row["source_id"]: row for row in task["sources"]}
required = list(task["required_sources"])
total = 0.0
for source_id in required:
    content = str(sources[source_id]["content"])
    total += sum(
        float(value)
        for value in re.findall(r"price\s+(\d+(?:\.\d+)?)", content, re.IGNORECASE)
    )
budget = next(
    float(row["value"])
    for row in task["constraints"]
    if row.get("type") == "budget_lte"
)
if total > budget:
    total_value = int(total) if total.is_integer() else total
    budget_value = int(budget) if budget.is_integer() else budget
    answer = {
        "status": "infeasible",
        "refusal_reason": (
            f"minimum sourced cost is {total_value}, exceeding budget {budget_value}"
        ),
        "acquired_sources": required,
    }
else:
    answer = {"status": "success", "acquired_sources": required, "steps": []}
output = root / "app/answer.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(answer, indent=2), encoding="utf-8")
PY
