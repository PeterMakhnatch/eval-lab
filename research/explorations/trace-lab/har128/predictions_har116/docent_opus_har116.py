"""HAR-128 Docent-opus predictions combiner (frozen mapping: model-readers reading.py + har128 MAPPING_ADDENDUM.md).

Concatenates the four blind reading batches (opaque trial ids), un-maps opaque
-> real trial names via the LOCAL id map (never uploaded), adds
first_failure_step (mapped step of the first citation block; null on passes),
and writes `predictions_har116/docent_opus.jsonl` (40 rows, real trial names)
with the model-readers row shape. Reading outputs themselves are reused
as-is (frozen prompt/schema/model); this script only re-keys them.

Usage (from worktree root): uv run python research/explorations/trace-lab/har128/predictions_har116/docent_opus_har116.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve()
PRED = HERE.parent  # predictions_har116
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
    inv = {v: k for k, v in id_map.items()}
    assert len(inv) == 40 and len(id_map) == 40

    rows: dict[str, dict] = {}
    for b in ("batch1", "batch2", "batch3", "batch4"):
        for line in (DOCENT / f"{b}.jsonl").read_text().splitlines():
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
    assert not missing, missing
    out = [json.dumps(rows[t]) for t in order]
    (PRED / "docent_opus.jsonl").write_text("\n".join(out) + "\n")
    print(f"wrote {len(out)} rows to predictions_har116/docent_opus.jsonl")
    kinds = [json.loads(row)["loop_kind"] for row in out]
    stops = [json.loads(row)["stop_reason"] for row in out]
    print("loop_kind:", {k: kinds.count(k) for k in sorted(set(kinds))})
    print("stop_reason:", {k: stops.count(k) for k in sorted(set(stops))})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
