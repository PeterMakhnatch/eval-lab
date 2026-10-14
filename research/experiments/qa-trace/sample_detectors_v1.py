#!/usr/bin/env python3
"""Seeded dev / held-out sample of detectors-v1 rows for the ``qa_trace`` detector.

Agentic sources only (the pipeline audits tool-using runs): every
``contamination-audit`` row, plus a balanced sample of ``tw`` (sanitized) and
``cb``. ``ours`` is left out because it overlaps ``contamination-audit`` (same
trajectories, older labels). Dev rows are for prompt iteration and are never
written to the corpus cache; held-out rows are the only ones scored.

Writes ``detectors_v1_sample.json`` and one id file per held-out source
(``ids/heldout-<source>.txt``) for ``evallab detectors run --ids``.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE.parents[1] / "calibration" / "detectors-v1" / "manifest.jsonl"
SEED = "qa-trace-20261014-detectors"
#: per source: (dev positives, dev negatives, held-out positives, held-out negatives); None = all the rest
PLAN = {
    "contamination-audit": (4, 12, None, None),
    "tw": (5, 5, 30, 30),
    "cb": (5, 5, 50, 50),
}


def main() -> int:
    rows = [json.loads(x) for x in MANIFEST.read_text().splitlines() if x.strip()]
    rng = random.Random(SEED)
    sample: dict[str, dict[str, list[str]]] = {"dev": {}, "heldout": {}}
    for source, (dev_pos, dev_neg, held_pos, held_neg) in PLAN.items():
        mine = [
            r
            for r in rows
            if r["source"] == source and r["labels"].get("reward_hacking") is not None
        ]
        pos = sorted(r["id"] for r in mine if r["labels"]["reward_hacking"])
        neg = sorted(r["id"] for r in mine if not r["labels"]["reward_hacking"])
        rng.shuffle(pos)
        rng.shuffle(neg)
        sample["dev"][source] = pos[:dev_pos] + neg[:dev_neg]
        rest_pos, rest_neg = pos[dev_pos:], neg[dev_neg:]
        sample["heldout"][source] = rest_pos[:held_pos] + rest_neg[:held_neg]
    (HERE / "ids").mkdir(exist_ok=True)
    for split, by_source in sample.items():
        for source, ids in by_source.items():
            (HERE / "ids" / f"{split}-{source}.txt").write_text(
                "".join(f"{i}\n" for i in sorted(ids))
            )
    (HERE / "detectors_v1_sample.json").write_text(
        json.dumps({"seed": SEED, "plan": PLAN, **sample}, indent=1, sort_keys=True) + "\n"
    )
    print(json.dumps({f"{s}/{src}": len(v) for s, d in sample.items() for src, v in d.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
