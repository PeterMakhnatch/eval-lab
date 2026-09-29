#!/usr/bin/env python3
"""Build Scout validation cases for the probe03_wedge scanner.

Trial-level stuck-terminal yes/no from the blind har99-wedge hand key
(``stretches`` non-empty -> true). Case ids are the Scout transcript ids
in the NORMALIZED db (one transcript per trial), so run this after the
normalized import. Re-run if the db is rebuilt (transcript ids embed the
session id + file stem and change only if the source files move).

    uv run --no-project --python 3.12 --with pyarrow python build_wedge_validation.py

$0: local files only; no model calls, no uploads.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
TRACE_LAB = HERE.parent
HAND_KEY = TRACE_LAB / "probe-03-capabilities" / "validation" / "har99-wedge.hand-key.jsonl"
OUT = HERE / "validation" / "wedge_stuck.json"


def main() -> None:
    stuck: dict[str, bool] = {}
    for line in HAND_KEY.open():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        trial = Path(row["trial_dir"]).name
        stuck[trial] = bool(row.get("stretches"))
    cases = []
    trials_seen = set()
    for path in sorted(glob.glob(str(HERE / "data" / "transcripts_norm" / "*.parquet"))):
        table = pq.read_table(path, columns=["transcript_id", "source_uri"])
        for row in table.to_pylist():
            trial = Path(row["source_uri"]).parent.parent.name
            trials_seen.add(trial)
            if trial not in stuck:
                raise SystemExit(f"hand key has no row for trial {trial}")
            cases.append({"id": row["transcript_id"], "target": stuck[trial]})
    missing = set(stuck) - trials_seen
    if missing:
        raise SystemExit(f"db is missing hand-key trials: {sorted(missing)}")
    OUT.write_text(json.dumps(cases, indent=1) + "\n")
    n_pos = sum(1 for c in cases if c["target"])
    print(f"wrote {OUT} ({len(cases)} cases, {n_pos} stuck-positive)")


if __name__ == "__main__":
    main()
