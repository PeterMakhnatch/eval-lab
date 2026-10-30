#!/usr/bin/env python3
"""DT-11 paired comparison: L0 (logistic OOF) vs length-only, both at 5% FAR.

Writes receipts/mcnemar.json. Zero model calls. Reuses screen.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import numpy as np  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("screen", os.path.join(HERE, "screen.py"))
screen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screen)

sys.path.insert(0, "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad")
from stats import mcnemar_exact  # noqa: E402


def main(out_dir):
    cohorts = screen.load_cohorts()
    for rows in cohorts.values():
        for r in rows:
            r["f"] = screen.features(screen.run_text(r["blind_id"]))
    names = sorted({k for rows in cohorts.values() for r in rows for k in r["f"]})
    result = {}
    for corpus in ("goodfire", "cheatbench"):
        rows = cohorts[corpus]
        X, y = screen.build_matrix(rows, names)
        scores = screen.oof_scores(X, y, screen.logistic)
        length = np.array([r["length"] for r in rows], dtype=float)
        a_l0 = scores >= screen.threshold_at_far(scores[y == 0])
        a_len = length >= screen.threshold_at_far(length[y == 0])
        b = int((a_l0 & ~a_len & (y == 1)).sum() + (a_l0 & ~a_len & (y == 0)).sum())
        c = int((~a_l0 & a_len & (y == 1)).sum() + (~a_l0 & a_len & (y == 0)).sum())
        result[corpus] = {
            "l0_only": int((a_l0 & ~a_len).sum()),
            "length_only": int((~a_l0 & a_len).sum()),
            "both": int((a_l0 & a_len).sum()),
            "neither": int((~a_l0 & ~a_len).sum()),
            "l0_only_positive": int((a_l0 & ~a_len & (y == 1)).sum()),
            "length_only_positive": int((~a_l0 & a_len & (y == 1)).sum()),
            "mcnemar_exact_p": float(mcnemar_exact(b, c)),
        }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "mcnemar.json"), "w") as fh:
        json.dump(result, fh, indent=1)
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
