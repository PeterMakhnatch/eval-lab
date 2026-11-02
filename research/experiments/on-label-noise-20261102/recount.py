"""Independent recount of the D4 headline counts from the raw per-call files (plain loops; no code from label_noise.py)."""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path.home() / "Developer/eval-lab-results/2026-11-02/D4"


def main() -> int:
    sample = json.loads((OUT / "sample.json").read_text())
    calls = {}
    for path in (OUT / "out").glob("*.json"):
        record = json.loads(path.read_text())
        ok = record["parsed"] is not None and record["attestation"]["attested"]
        calls[(record["case_id"], record["reader"], record["wording"])] = record["parsed"]["verdict"] if ok else None
    expected = json.loads((Path(__file__).resolve().parent / "results.json").read_text())["classes"]
    failures = 0
    for cls in ("publisher", "publisher_cot_judged", "container_verified", "all"):
        k = n = 0
        for row in sample:
            if cls != "all" and row["provenance"] != cls:
                continue
            finals = []
            for reader in "AB":
                v1, v2 = calls.get((row["case_id"], reader, "w1")), calls.get((row["case_id"], reader, "w2"))
                finals.append(v1 if v1 is not None and v1 == v2 else None)
            if finals[0] == finals[1] and finals[0] in ("cheated", "honest"):
                n += 1
                k += (finals[0] == "cheated") != row["label"]
        got = expected[cls]["consensus"]["disagree_with_label"]
        ok = (k, n) == (got["k"], got["n"])
        failures += not ok
        print(f"{cls}: recount {k}/{n} vs results {got['k']}/{got['n']} {'ok' if ok else 'MISMATCH'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
