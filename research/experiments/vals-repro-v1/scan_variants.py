"""Emit readiness-variants.csv: per-task hardened-lineage inventory ($0, file-local).

Reads library/task-variants/<task>/*.json and reports, for the closure-ladder
transforms (strip-future-history, purge-installed-copies, purge-build-caches,
mtime-normalize, leak-close-pypi), the newest record digest12 + status.
Run: python3 research/experiments/vals-repro-v1/scan_variants.py
"""
import csv
import glob
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

TASKS = ["000552", "000792", "001269", "002139", "002391", "002402",
         "002486", "002552", "002864", "002938", "001985"]
TRANSFORMS = ["strip-future-history@1", "purge-installed-copies@1",
              "purge-build-caches@1", "mtime-normalize@1", "leak-close-pypi@1"]

rows = []
for t in TASKS:
    pat = os.path.join(ROOT, "library", "task-variants",
                       f"mimo-v2.6-rl__format-code-task-{t}", "*.json")
    recs = {}
    for f in sorted(glob.glob(pat)):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        tx = d.get("transform")
        if tx in TRANSFORMS:
            prev = recs.get(tx)
            # Prefer a validated record over a candidate one.
            if prev is None or (prev[1] != "validated"
                               and d.get("status") == "validated"):
                recs[tx] = (os.path.basename(f)[:12], d.get("status"))
    for tx in TRANSFORMS:
        digest12, status = recs.get(tx, ("-", "-"))
        rows.append({"task": t, "transform": tx,
                     "record_digest12": digest12, "status": status})

out = os.path.join(os.path.dirname(__file__), "readiness-variants.csv")
with open(out, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["task", "transform",
                                       "record_digest12", "status"])
    w.writeheader()
    w.writerows(rows)
print(f"wrote {out} ({len(rows)} rows)")
