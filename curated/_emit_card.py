#!/usr/bin/env python3
"""Emit CARD.md from a completed oracle/nop pair + notes file."""
from __future__ import annotations

import json
import sys
from pathlib import Path

RUNS = Path("/Users/petermakhnatch/Developer/helab-curator/runs")
CURATED = Path("/Users/petermakhnatch/Developer/helab-curator/curated")
FB = Path("/Users/petermakhnatch/Developer/agent-evals/frontier-bench/tasks")
FB_COMMIT = "3d694e919871dbf21ea5ff618782c99a3cb3663f"
TB_COMMIT = "4e77c91dc523107eedd9440b659159d470209188"


def toml_field(text: str, key: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(key) and "=" in s:
            return s.split("=", 1)[1].strip().strip('"')
    return ""


def main() -> None:
    name = sys.argv[1]
    notes = Path(sys.argv[2]).read_text() if len(sys.argv) > 2 else "See verifier notes in this card."
    odir = RUNS / f"oracle-{name}"
    ndir = RUNS / f"nop-{name}"
    o = json.loads((odir / "result.json").read_text())
    n = json.loads((ndir / "result.json").read_text())
    omean = list(o["stats"]["evals"].values())[0]["metrics"][0]["mean"]
    nmean = list(n["stats"]["evals"].values())[0]["metrics"][0]["mean"]
    if omean != 1.0 or nmean != 0.0:
        raise SystemExit(f"{name}: oracle={omean} nop={nmean}")
    trials = []
    digest = ""
    for t in odir.iterdir():
        if t.is_dir() and (t / "result.json").exists():
            tr = json.loads((t / "result.json").read_text())
            digest = tr.get("task_checksum") or digest
            trials.append(t.name)
    meta = (FB / name / "task.toml").read_text()
    card = f"""# {name}

## Provenance

- **Source repo:** `harbor-framework/frontier-bench` (clone `~/Developer/agent-evals/frontier-bench`)
- **Commit:** `{FB_COMMIT}`
- **Path:** `tasks/{name}`
- **Also present at:** terminal-bench `{TB_COMMIT}`
- **License:** Apache License 2.0
- **Package name:** `terminal-bench/{name}`

## Pinned digest

- **Harbor `task_checksum`:** `{digest}`

## Difficulty / domain / runtime

- **Domain:** {toml_field(meta, "category")} / {toml_field(meta, "subcategory")}
- **Difficulty:** expert_time_estimate_hours = {toml_field(meta, "expert_time_estimate_hours") or "n/a"}; see task.toml agent timeout
- **Resources:** {toml_field(meta, "cpus") or "?"} CPU, {toml_field(meta, "memory_mb") or "?"} MB, 0 GPU
- **Verifier:** separate (environment_mode from task.toml)

## Local verification (free oracle / nop)

| Run | Agent | k | -n | jobs_dir | Result |
| --- | --- | --- | --- | --- | --- |
| `runs/oracle-{name}` | oracle | 3 | 2 | `~/Developer/helab-curator/runs` | 3/3 reward **1.0** |
| `runs/nop-{name}` | nop | 1 | 1 | same | reward **0.0** |

Trials: {", ".join(sorted(trials))}.

## Verifier read-through

{notes.strip()}

## Canary note

Locally verified; see README for canary nomination.
"""
    out = CURATED / name / "CARD.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(card)
    print("wrote", out)


if __name__ == "__main__":
    main()
