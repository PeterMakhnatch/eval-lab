"""G2 attempt-1 Docent-opus predictions combiner (frozen mapping: model-readers reading.py + predictions_g2_a1/MAPPING_ADDENDUM.md).

Concatenates the blind reading batches (opaque trial ids), un-maps opaque
-> real trial names via the LOCAL id map (never uploaded), adds
first_failure_step (mapped step of the first citation block; null on passes),
and writes `predictions_g2_a1/docent_opus.jsonl` (real trial names, trials.json
order) with the model-readers row shape. Reading outputs themselves are reused
as-is (frozen prompt/schema/model); this script only re-keys them.

Usage (from worktree root): uv run python research/explorations/trace-lab/har128/predictions_g2_a1/docent_opus_g2.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve()
PRED = HERE.parent  # predictions_g2_a1
DOCENT = PRED / "docent"


def ref_step(ref: str | None) -> int | None:
    if not ref:
        return None
    m = re.search(r"#(\d+)$", ref)
    return int(m.group(1)) if m else None


def main() -> int:
    trials = json.loads((PRED / "trials.json").read_text())
    order = [t["trial"] for t in trials]
    id_map: dict[str, str] = json.loads((PRED / "docent_id_map.json").read_text())
    assert len(id_map) == len(order)

    rows: dict[str, dict] = {}
    batches = sorted(DOCENT.glob("batch*.jsonl"))
    assert batches, "no batch files"
    for b in batches:
        for line in b.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            oid = r["trial"]
            real = id_map.get(oid)
            assert real is not None, oid
            assert real not in rows, f"duplicate {real}"
            r["trial"] = real
            r["first_failure_step"] = ref_step(r.get("first_failure_ref"))
            rows[real] = r
    missing = [t for t in order if t not in rows]
    for t in missing:
        # No transcript uploaded (infra failure before agent start): null row.
        rows[t] = {
            "trial": t,
            "stop_reason": None,
            "first_failure_ref": None,
            "blame": None,
            "loop_kind": None,
            "loop_span": None,
            "pass_copied": None,
            "first_failure_step": None,
            "raw_error": "no transcript",
            "tool": "docent_strong",
            "source": "no transcript uploaded (infra failure before agent start)",
        }
    print(f"null rows for missing transcripts: {missing or 'none'}")
    out = [json.dumps(rows[t]) for t in order]
    (PRED / "docent_opus.jsonl").write_text("\n".join(out) + "\n")
    print(f"wrote {len(out)} rows to predictions_g2_a1/docent_opus.jsonl")
    kinds = [json.loads(row)["loop_kind"] for row in out]
    stops = [json.loads(row)["stop_reason"] for row in out]
    print("loop_kind:", {k: kinds.count(k) for k in sorted(set(kinds), key=str)})
    print("stop_reason:", {k: stops.count(k) for k in sorted(set(stops), key=str)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
