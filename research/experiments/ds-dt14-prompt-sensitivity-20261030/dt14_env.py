"""Step projections for the DT-14 sample, rendered exactly as DT-8 rendered them."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

EXPERIMENT = Path(__file__).resolve().parent
sys.path.insert(0, str(EXPERIMENT.parent / "ds-dt8-verified-reader-fullscale-20261029"))

import dt8_reader  # noqa: E402

WORK = Path("/tmp/dt14")


def corpus_steps(corpus: str) -> dict[str, dict[str, str]]:
    """blind_id -> {step_id: whitespace-normalized rendered step text}, cached on disk."""
    cache = WORK / f"steps-{corpus}.json"
    if cache.exists():
        return {blind: {int(step): text for step, text in steps.items()}
                for blind, steps in json.loads(cache.read_text()).items()}
    from evallab.production.run_atlas import render_step

    meta = json.loads((WORK / "sample.json").read_text())
    wanted = {row["blind_id"] for row in meta["corpora"][corpus]["runs"]}
    rows = dt8_reader.goodfire_rows() if corpus == "goodfire" else dt8_reader.cheatbench_rows()
    pattern = dt8_reader.pattern_for(corpus)
    out: dict[str, Any] = {}
    for row in rows:
        if row["blind_id"] not in wanted:
            continue
        out[row["blind_id"]] = {
            step["step_id"]: dt8_reader.normalize(render_step(step, pattern=pattern))
            for step in row["steps"]
        }
    if len(out) != len(wanted):
        raise ValueError(f"{corpus}: {len(out)} of {len(wanted)} runs rendered")
    WORK.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out
