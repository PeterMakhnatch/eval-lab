#!/usr/bin/env python3
"""Build Scout's probe-03 lookup from the deterministic capabilities files.

Reads the read-only probe-03 outputs (never modifies them) and writes one
small JSON map for the Scout scanners: trial dir name -> outcome /
first_failure / stop / wedge fields the scanners need. Re-run if the
capabilities files change.

    uv run --no-project --python 3.12 python build_probe03.py

$0: local files only; no model calls, no uploads.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACE_LAB = HERE.parent
CAP_FILES = [
    TRACE_LAB / "probe-03-capabilities" / "har81" / "capabilities.jsonl",
    TRACE_LAB / "probe-03-capabilities" / "har90" / "capabilities.jsonl",
]
OUT = HERE / "probe03_lookup.json"


def _pick_failure(obj: dict | None) -> dict | None:
    if not obj:
        return None
    return {
        "step_ref": obj.get("step_ref"),
        "evidence_step_refs": obj.get("evidence_step_refs") or [],
        "tag": obj.get("tag"),
        "attribution": obj.get("attribution"),
        "rule_id": obj.get("rule_id"),
        "note": obj.get("note"),
        "recovered": obj.get("recovered"),
    }


def main() -> None:
    lookup: dict[str, dict] = {}
    for path in CAP_FILES:
        for line in path.open():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            wedge = row.get("wedge") or {}
            stop = row.get("stop") or {}
            lookup[row["trial"]] = {
                "job": row.get("job_dir") or row.get("job"),
                "source_file": str(path.relative_to(TRACE_LAB)),
                "outcome": _pick_failure(row.get("outcome_relevant_failure")),
                "first_failure": _pick_failure(row.get("first_failure")),
                "stop": {
                    "reason": stop.get("reason"),
                    "exception_type": stop.get("exception_type"),
                    "natural_completion": stop.get("natural_completion"),
                    "task_complete_refs": stop.get("task_complete_refs") or [],
                },
                "wedge": {
                    "stretches": [
                        {
                            "trigger_ref": s.get("trigger_ref"),
                            "start_ref": s.get("start_ref"),
                            "end_ref": s.get("end_ref"),
                            "interrupt_ref": s.get("interrupt_ref"),
                            "turns": s.get("turns"),
                            "cause": s.get("cause"),
                            "prompt_returned": s.get("prompt_returned"),
                            "until_run_end": s.get("until_run_end"),
                        }
                        for s in (wedge.get("stretches") or [])
                    ],
                },
            }
    OUT.write_text(json.dumps(lookup, indent=1) + "\n")
    n_wedge = sum(1 for v in lookup.values() if v["wedge"]["stretches"])
    print(f"wrote {OUT} ({len(lookup)} trials, {n_wedge} with wedge stretches)")


if __name__ == "__main__":
    main()
